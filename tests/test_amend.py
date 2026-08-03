"""The amendment flow (core._amend_project) — Phase 3. Fully offline.

Turn N changing a project built in turn 1 is what the whole plan exists for, so
these cover the routing gate, the five steps, and — most importantly — the
silent failure the plan warns about: an amendment turn that skips the coverage
check and the smoke test because both are gated on `self._blueprint`.
"""

from types import SimpleNamespace

import pytest

from app.agent.blueprint import should_amend
from app.agent.core import AgentCore
from app.agent.impact import APP_FILE, DB_FILE, MODELS_FILE, SEED_FILE
from app.agent.projectspec import (
    Entity,
    Field,
    Page,
    ProjectSpec,
    SpecEndpoint,
    delta_from_data,
)
from config.settings import settings


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(m.content) for m in messages))
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# should_amend — the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "add an admin page where I can add a product with a picture",
        "add a shopping cart",
        "now let customers search products by title",
        "also show the author on each book",
        "change the price field to a decimal",
        "remove the login page",
    ],
)
def test_amend_gate_fires_on_the_demo_turns(message):
    assert should_amend(message, True) is True


@pytest.mark.parametrize(
    "message",
    [
        "build me an e-commerce site for selling books",  # greenfield, no verb
        "how does the cart work?",  # a question
        "what should I add next?",  # a question containing "add"
        "split the styles into a css file",  # _multi_file_flow owns this
    ],
)
def test_amend_gate_stays_out_of_the_way(message):
    assert should_amend(message, True) is False


def test_amend_gate_is_inert_without_a_spec():
    """Without memory there is nothing to amend, so routing is unchanged."""
    assert should_amend("add a shopping cart", False) is False


# ---------------------------------------------------------------------------
# delta_from_data — validation
# ---------------------------------------------------------------------------


def _bookshop_spec() -> ProjectSpec:
    return ProjectSpec(
        name="bookshop",
        revision=1,
        language="python",
        backend="flask",
        entities=(
            Entity(
                "product",
                "products",
                (
                    Field("id", "INTEGER", pk=True, required=True),
                    Field("title", "TEXT", required=True),
                ),
            ),
        ),
        endpoints=(SpecEndpoint("GET", "/", template="templates/index.html"),),
        pages=(Page("/", "templates/index.html", "Home", "storefront", ("product",)),),
        files={"app.py": "backend", "db.py": "data"},
    )


def test_delta_adds_a_field_to_an_existing_entity():
    delta = delta_from_data(
        {
            "summary": "add product images",
            "entities": [
                {
                    "name": "product",
                    "add_fields": [{"name": "image_path", "type": "IMAGE"}],
                }
            ],
        },
        _bookshop_spec(),
    )

    assert delta.add_fields == (("product", Field("image_path", "TEXT")),)
    assert delta.add_entities == ()  # existing entity, not a new one
    assert delta.summary == "add product images"


def test_delta_treats_an_unknown_entity_as_a_new_one():
    delta = delta_from_data(
        {
            "entities": [
                {"name": "cart", "add_fields": [{"name": "qty", "type": "INTEGER"}]}
            ]
        },
        _bookshop_spec(),
    )
    assert [e.name for e in delta.add_entities] == ["cart"]
    assert delta.add_entities[0].table == "carts"


def test_delta_drops_things_that_already_exist():
    """Re-adding an existing column would produce a duplicate; re-adding an
    existing route would make the model redefine it."""
    delta = delta_from_data(
        {
            "entities": [{"name": "product", "add_fields": [{"name": "title"}]}],
            "endpoints": [{"method": "GET", "path": "/"}],
            "pages": [{"route": "/", "template": "templates/index.html"}],
        },
        _bookshop_spec(),
    )
    assert delta.is_empty()


def test_delta_infers_the_file_for_a_page_the_model_forgot_to_list():
    """Same "declared it, then omitted the file" failure _ensure_backend catches."""
    delta = delta_from_data(
        {
            "pages": [
                {
                    "route": "/admin",
                    "template": "templates/admin.html",
                    "nav_label": "Admin",
                }
            ]
        },
        _bookshop_spec(),
    )
    assert any(name == "templates/admin.html" for name, _ in delta.new_files)


def test_delta_rejects_hostile_values():
    delta = delta_from_data(
        {
            "endpoints": [
                {"method": "STEAL", "path": "/x"},
                {"method": "GET", "path": "not-absolute"},
            ],
            "new_files": [{"filename": "../../etc/passwd"}],
        },
        _bookshop_spec(),
    )
    assert delta.add_endpoints == ()
    assert delta.new_files == ()


def test_delta_from_a_failed_call_is_empty():
    assert delta_from_data(None, _bookshop_spec()).is_empty()


# ---------------------------------------------------------------------------
# _amend_project — the flow
# ---------------------------------------------------------------------------


def _write_project(root):
    """A minimal build of the canonical layout, as turn 1 would leave it."""
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / APP_FILE).write_text(
        "from flask import Flask, render_template\n\n"
        "app = Flask(__name__)\n\n\n"
        '@app.route("/")\n'
        "def index():\n"
        '    return render_template("index.html")\n',
        encoding="utf-8",
    )
    (root / DB_FILE).write_text(
        "import sqlite3\n\n\n"
        "def get_db():\n    return sqlite3.connect('app.db')\n\n\n"
        "def ensure_column(conn, table, column, decl):\n"
        "    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}\n"
        "    if column not in cols:\n"
        "        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')\n\n\n"
        "def init_db():\n"
        "    conn = get_db()\n"
        "    try:\n"
        '        conn.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, title TEXT)")\n'
        "        conn.commit()\n"
        "    finally:\n"
        "        conn.close()\n",
        encoding="utf-8",
    )
    (root / MODELS_FILE).write_text(
        "from db import get_db\n\n\ndef list_products():\n"
        "    return get_db().execute('SELECT id, title FROM products').fetchall()\n",
        encoding="utf-8",
    )
    (root / SEED_FILE).write_text(
        "import db\n\n\ndef seed():\n    pass\n", encoding="utf-8"
    )
    (root / "templates" / "base.html").write_text(
        "<html><body><nav></nav>{% block content %}{% endblock %}</body></html>",
        encoding="utf-8",
    )
    (root / "templates" / "index.html").write_text(
        '{% extends "base.html" %}{% block content %}{% endblock %}', encoding="utf-8"
    )


_IMAGE_DELTA_JSON = (
    '{"summary": "add product images", "entities": '
    '[{"name": "product", "add_fields": [{"name": "image_path", "type": "IMAGE"}]}]}'
)


async def _agent_for_amend(tmp_path, monkeypatch, delta_json=_IMAGE_DELTA_JSON):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    _write_project(tmp_path)
    spec = _bookshop_spec()
    spec.save(tmp_path)

    agent = AgentCore(session_id="pytest_amend")
    agent._llm_blueprint = ScriptedLLM([delta_json])
    return agent, spec


async def test_amend_writes_the_migration_deterministically(tmp_path, monkeypatch):
    """Schema changes are NOT generated — db.py's migration comes from the spec."""
    agent, spec = await _agent_for_amend(tmp_path, monkeypatch)

    edited: list[str] = []

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        edited.append(target)
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(agent, "_file_op_flow", _fake_file_op)

    answer, _ = await agent._amend_project("add a picture to products", spec, [])

    db_source = (tmp_path / DB_FILE).read_text(encoding="utf-8")
    assert 'ensure_column(conn, "products", "image_path", "TEXT")' in db_source
    assert "schema migration" in answer
    # db.py is never handed to the model.
    assert DB_FILE not in edited


async def test_amend_updates_the_files_the_change_breaks(tmp_path, monkeypatch):
    """The capability the plan is about: turn 1's files are updated, not orphaned."""
    agent, spec = await _agent_for_amend(tmp_path, monkeypatch)

    instructions: dict[str, str] = {}

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        instructions[target] = msg
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(agent, "_file_op_flow", _fake_file_op)

    answer, _ = await agent._amend_project("add a picture to products", spec, [])

    assert MODELS_FILE in instructions
    assert SEED_FILE in instructions
    assert "templates/index.html" in instructions
    # Each file is told precisely what to change, not handed the request alone.
    assert "image_path" in instructions[MODELS_FILE]
    assert "Updated" in answer and "models.py" in answer


async def test_amend_bumps_the_revision_and_persists(tmp_path, monkeypatch):
    agent, spec = await _agent_for_amend(tmp_path, monkeypatch)

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(agent, "_file_op_flow", _fake_file_op)

    await agent._amend_project("add a picture to products", spec, [])

    reloaded = ProjectSpec.load(tmp_path)
    assert reloaded.revision == 2
    field = reloaded.entity("product").field("image_path")
    assert field is not None and field.added_in == 2
    assert reloaded.history[-1].request == "add a picture to products"


async def test_amend_threads_the_spec_into_every_edit(tmp_path, monkeypatch):
    """The model must see what already exists rather than re-infer it."""
    agent, spec = await _agent_for_amend(tmp_path, monkeypatch)

    contexts: list[str] = []

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        contexts.append(extra_context)
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(agent, "_file_op_flow", _fake_file_op)
    await agent._amend_project("add a picture to products", spec, [])

    assert contexts and all("products(" in c for c in contexts)


async def test_the_delta_prompt_states_the_existing_contract(tmp_path, monkeypatch):
    agent, spec = await _agent_for_amend(tmp_path, monkeypatch)

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(agent, "_file_op_flow", _fake_file_op)
    await agent._amend_project("add a picture to products", spec, [])

    prompt = agent._llm_blueprint.prompts[0]
    assert "products(" in prompt  # the schema
    assert "GET /" in prompt  # the routes
    assert "add a picture to products" in prompt


async def test_an_empty_delta_falls_through_to_normal_routing(tmp_path, monkeypatch):
    agent, spec = await _agent_for_amend(
        tmp_path, monkeypatch, delta_json='{"summary": "nothing"}'
    )

    answer, trace = await agent._amend_project("tweak the wording", spec, [])

    assert answer is None and trace == []


async def test_a_failed_delta_call_falls_through(tmp_path, monkeypatch):
    agent, spec = await _agent_for_amend(
        tmp_path, monkeypatch, delta_json="not json at all"
    )
    answer, _ = await agent._amend_project("add something", spec, [])
    assert answer is None


# ---------------------------------------------------------------------------
# Step 5 — the silent failure the plan warns about
# ---------------------------------------------------------------------------


async def test_an_amendment_turn_still_gets_the_smoke_test(tmp_path, monkeypatch):
    """The regression test the plan explicitly asks for.

    `chat()` gates BOTH the coverage check and the smoke test on
    `self._blueprint is not None`, and clears it every turn. An amendment that
    didn't set it would be the only kind of turn that is never verified and
    never run — invisibly, because the turn still reports success.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "blueprint_smoke_test", True)
    monkeypatch.setattr(settings, "check_blueprint_coverage", True)
    monkeypatch.setattr(settings, "check_references", False)
    _write_project(tmp_path)
    _bookshop_spec().save(tmp_path)

    agent = AgentCore(session_id="pytest_amend_smoke")
    agent._llm_blueprint = ScriptedLLM([_IMAGE_DELTA_JSON])

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(agent, "_file_op_flow", _fake_file_op)

    called = {"smoke": False, "coverage": False}

    async def _fake_smoke(blueprint):
        called["smoke"] = True
        return "", []

    async def _fake_coverage(blueprint, trace):
        called["coverage"] = True
        return "", []

    monkeypatch.setattr(agent, "_smoke_test_backend", _fake_smoke)
    monkeypatch.setattr(agent, "_verify_blueprint_coverage", _fake_coverage)

    await agent.chat("add a picture to products")

    assert called["smoke"] is True, "amendment turns must still be RUN"
    assert called["coverage"] is True, "amendment turns must still be verified"


async def test_chat_routes_an_amendment_and_leaves_builds_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    _write_project(tmp_path)
    _bookshop_spec().save(tmp_path)

    agent = AgentCore(session_id="pytest_amend_route")

    amended, blueprinted = [], []

    async def _fake_amend(msg, spec, refs):
        amended.append(msg)
        return "amended", []

    async def _fake_expand(msg):
        blueprinted.append(msg)
        return None

    monkeypatch.setattr(agent, "_amend_project", _fake_amend)
    monkeypatch.setattr(agent, "_expand_requirements", _fake_expand)

    await agent.chat("add a shopping cart")
    assert amended == ["add a shopping cart"]
    assert blueprinted == []


async def test_chat_falls_through_to_the_blueprint_when_the_amendment_declines(
    tmp_path, monkeypatch
):
    """An amendment that finds nothing structural must not swallow the turn."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    _write_project(tmp_path)
    _bookshop_spec().save(tmp_path)

    agent = AgentCore(session_id="pytest_amend_fallthrough")

    async def _fake_amend(msg, spec, refs):
        return None, []

    routed = []

    async def _fake_route(msg, refs, **kwargs):
        routed.append(msg)
        return "normal routing", []

    monkeypatch.setattr(agent, "_amend_project", _fake_amend)
    monkeypatch.setattr(agent, "_route_one", _fake_route)

    answer, _ = await agent.chat("add a footer to the page")
    assert answer == "normal routing"
    assert routed


# ---------------------------------------------------------------------------
# The restyle pass — "now make it purple" on turn 2
# ---------------------------------------------------------------------------
# The whole design system is written in custom properties so a restyle is a
# one-file change. But `write_theme`'s only caller sat beside the scaffold copy,
# and `scaffold_flask` returns nothing once the files exist — so from turn 2 on
# every restyle request was deterministically a no-op, on a demo built in parts.


def _write_theme_file(root):
    css = root / "static" / "css" / "theme.css"
    css.parent.mkdir(parents=True, exist_ok=True)
    css.write_text(":root {\n  --color-accent: #5b9bff;\n}\n", encoding="utf-8")
    return css


async def test_restyle_rewrites_the_theme_on_a_later_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path)
    css = _write_theme_file(tmp_path)
    agent = AgentCore(session_id="pytest_restyle")

    note = agent._restyle_project(tmp_path, "now make it purple")

    written = css.read_text(encoding="utf-8")
    assert "--color-accent" in written and "#5b9bff" not in written
    assert "purple" in note.lower()
    # A restyle changes no markup — that is the point of the token layer.
    assert "no markup changed" in note


async def test_restyle_leaves_the_theme_alone_when_no_look_was_asked_for(
    tmp_path, monkeypatch
):
    """The rule `write_theme` was written to protect: a theme the user
    hand-edits must survive the next turn."""
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path)
    css = _write_theme_file(tmp_path)
    before = css.read_text(encoding="utf-8")
    agent = AgentCore(session_id="pytest_restyle_noop")

    assert agent._restyle_project(tmp_path, "add a checkout page") == ""
    assert css.read_text(encoding="utf-8") == before


async def test_restyle_never_introduces_a_theme_to_a_project_without_one(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path)
    agent = AgentCore(session_id="pytest_restyle_absent")

    assert agent._restyle_project(tmp_path, "make it purple") == ""
    assert not (tmp_path / "static" / "css" / "theme.css").exists()


async def test_a_pure_restyle_is_reported_even_with_an_empty_delta(
    tmp_path, monkeypatch
):
    """A restyle produces no entities, fields or files, so the delta is empty
    and `_amend_project` returned None here — the turn then fell through to
    routing that rewrites no theme, and nothing happened at all."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    _write_project(tmp_path)
    css = _write_theme_file(tmp_path)
    spec = _bookshop_spec()
    spec.save(tmp_path)

    agent = AgentCore(session_id="pytest_restyle_delta")
    agent._llm_blueprint = ScriptedLLM(['{"summary": "restyle"}'])

    answer, trace = await agent._amend_project("now make it purple", spec, [])

    assert answer and "restyled" in answer.lower()
    assert "#5b9bff" not in css.read_text(encoding="utf-8")
