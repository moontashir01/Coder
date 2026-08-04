"""Requirements Blueprint (app/agent/blueprint.py, runtime_probe.py) + its seam
in AgentCore.chat().

All offline: the gate/parsing are pure functions; the seam tests monkeypatch the
one LLM-calling method (`_expand_requirements`) and `_multi_file_flow`, so no
real Ollama is ever reached.
"""

from types import SimpleNamespace

import pytest

from app.agent.blueprint import (
    HOME_TEMPLATE,
    TIER_CORE,
    TIER_OPTIONAL,
    TIER_REQUESTED,
    ApiContract,
    Blueprint,
    Endpoint,
    Feature,
    PlannedFile,
    blueprint_from_data,
    derive_pages_from_entities,
    should_blueprint,
)
from app.agent.core import AgentCore
from app.agent.projectspec import Entity, Field
from app.agent.runtime_probe import STDLIB_STACK, Stack, detect_stack
from config.settings import settings


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# should_blueprint — the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "build me a login page",
        "create a todo app",
        "make a dashboard for sales figures",
        "scaffold a blog website",
        "design a signup form",
        "build an ecommerce store",
        "generate a full stack CRUD app",
        "implement a login system",
    ],
)
def test_should_blueprint_fires_on_greenfield_builds(msg):
    assert should_blueprint(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "explain how a login page works",  # question
        "what does a login page need?",  # question
        "add a login form to index.html",  # incremental edit
        "put a button into the page",  # incremental edit
        "split styles.css into two files",  # explicit split
        "refactor the login page",  # refactor
        "create a css file",  # single-file request
        "make a new html file",  # single-file request
        "write a python function that adds two numbers",  # snippet, no build verb+noun
        "fix the navbar",  # repair, no build verb
    ],
)
def test_should_blueprint_skips_non_greenfield(msg):
    assert should_blueprint(msg) is False


# ---------------------------------------------------------------------------
# detect_stack — grounded in what's installed
# ---------------------------------------------------------------------------


def test_detect_stack_defaults_to_stdlib_when_nothing_present():
    stack = detect_stack(
        prefer="auto", _has_module=lambda n: False, _which=lambda n: None
    )
    assert stack.backend == "stdlib"
    assert stack.language == "python"
    assert stack.runnable is True


def test_detect_stack_prefers_flask_when_installed():
    stack = detect_stack(
        prefer="auto", _has_module=lambda n: n == "flask", _which=lambda n: None
    )
    assert stack.backend == "flask"


# Phase A (docs/always-fullstack-plan.md): a FORCED stack is honoured whether or
# not it's installed. Silently returning stdlib instead is the bug — downstream
# cannot tell that apart from a build that was always meant to be stdlib.


def test_forced_flask_is_runnable_when_present():
    stack = detect_stack(prefer="flask", _has_module=lambda n: n == "flask")
    assert (stack.backend, stack.runnable) == ("flask", True)
    assert stack.install_hint == ""


def test_forced_flask_is_reported_not_downgraded_when_absent():
    stack = detect_stack(
        prefer="flask", _has_module=lambda n: False, _which=lambda n: None
    )
    assert stack.backend == "flask"  # NOT "stdlib"
    assert stack.runnable is False
    assert "pip install flask" in stack.install_hint
    # The generation instruction must still describe Flask: the model writes the
    # app, the *user* installs the package. Folding the warning into `note` would
    # make prompts/blueprint.md's "don't use what isn't installed" rule fire and
    # quietly produce a stdlib app — the downgrade this test exists to prevent.
    assert "flask" in stack.note.lower()
    assert "not installed" not in stack.note.lower()


def test_forced_fastapi_absent_is_reported_not_downgraded():
    stack = detect_stack(
        prefer="fastapi", _has_module=lambda n: False, _which=lambda n: None
    )
    assert (stack.backend, stack.runnable) == ("fastapi", False)
    assert "pip install fastapi" in stack.install_hint


def test_forced_node_absent_is_reported_not_downgraded():
    stack = detect_stack(
        prefer="node", _has_module=lambda n: False, _which=lambda n: None
    )
    assert (stack.language, stack.runnable) == ("node", False)
    assert stack.install_hint


def test_forced_stdlib_ignores_an_installed_flask():
    stack = detect_stack(prefer="stdlib", _has_module=lambda n: n == "flask")
    assert (stack.backend, stack.runnable) == ("stdlib", True)


def test_unknown_prefer_falls_through_to_auto():
    stack = detect_stack(
        prefer="flsak", _has_module=lambda n: n == "flask", _which=lambda n: None
    )
    assert (stack.backend, stack.runnable) == ("flask", True)


def test_detect_stack_uses_fastapi_when_only_fastapi_present():
    stack = detect_stack(_has_module=lambda n: n == "fastapi", _which=lambda n: None)
    assert stack.backend == "fastapi"


def test_detect_stack_none_prefers_no_backend():
    stack = detect_stack(prefer="none")
    assert stack.backend == "none"


def test_detect_stack_node_needs_network_to_use_express():
    node_which = lambda n: "/usr/bin/node" if n == "node" else None
    # Node present but offline → can't confirm express is vendored → stdlib.
    offline = detect_stack(
        allow_network=False, _has_module=lambda n: False, _which=node_which
    )
    assert offline.backend == "stdlib"
    # Node present and network allowed → express is fair game.
    online = detect_stack(
        allow_network=True, _has_module=lambda n: False, _which=node_which
    )
    assert online.language == "node"
    assert online.backend == "express"


# ---------------------------------------------------------------------------
# blueprint_from_data — parsing, filtering, tiers
# ---------------------------------------------------------------------------


def _login_data():
    return {
        "summary": "A login page with email/password auth and password reset",
        "features": [
            {
                "name": "Login form",
                "tier": "requested",
                "files": ["login.html", "login.js"],
            },
            {"name": "Auth backend", "tier": "core", "files": ["server.py"]},
            {"name": "OAuth sign-in", "tier": "optional", "files": ["oauth.py"]},
            {"name": "Phantom", "tier": "core", "files": ["does-not-exist.py"]},
        ],
        "files": [
            {"filename": "login.html", "action": "create", "role": "frontend"},
            {"filename": "login.js", "action": "create", "role": "frontend"},
            {"filename": "server.py", "action": "create", "role": "backend"},
            {"filename": "oauth.py", "action": "create", "role": "backend"},
            {"filename": "../evil.py"},  # path escape → rejected
            {"filename": "passwd"},  # extensionless, not allowlisted → rejected
        ],
        "contract": {
            "endpoints": [
                {
                    "method": "post",
                    "path": "/api/login",
                    "request": "{email, password}",
                    "response": "200 {ok} | 401 {error}",
                },
                {"method": "GET", "path": "no-leading-slash"},  # rejected
            ],
            "form_bindings": [
                "#login-form submits POST /api/login with email, password"
            ],
            "data_schema": ["users(email TEXT PRIMARY KEY, password_hash TEXT)"],
        },
    }


def test_blueprint_parses_and_sanitizes_files():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    names = {f.filename for f in bp.files}
    assert names == {"login.html", "login.js", "server.py", "oauth.py"}
    assert "../evil.py" not in names
    assert "passwd" not in names
    assert bp.stack.backend == "stdlib"


def test_blueprint_endpoints_validated_and_normalized():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    assert len(bp.contract.endpoints) == 1  # the no-leading-slash one is dropped
    ep = bp.contract.endpoints[0]
    assert ep.method == "POST"  # lowercased input uppercased
    assert ep.path == "/api/login"


def test_blueprint_feature_files_filtered_to_known():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    phantom = next(f for f in bp.features if f.name == "Phantom")
    assert phantom.files == ()  # does-not-exist.py isn't in the file list


def test_blueprint_default_build_excludes_optional_only_files():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    default = {f.filename for f in bp.build_files()}
    assert default == {"login.html", "login.js", "server.py"}  # no oauth.py
    with_opt = {f.filename for f in bp.build_files(include_optional=True)}
    assert "oauth.py" in with_opt


def test_blueprint_optional_note_offers_unbuilt_features():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    note = bp.optional_note()
    assert "OAuth sign-in" in note


def test_blueprint_context_block_states_the_contract():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    block = bp.to_context_block()
    assert "/api/login" in block
    assert "users(email TEXT PRIMARY KEY" in block
    assert "standard library" in block.lower()  # the chosen stack is stated


def test_blueprint_unknown_tier_defaults_to_core_and_is_built():
    data = {
        "files": [{"filename": "a.py"}, {"filename": "b.py"}],
        "features": [{"name": "X", "tier": "banana", "files": ["a.py"]}],
    }
    bp = blueprint_from_data(data, "build a thing", STDLIB_STACK)
    assert bp.features[0].tier == TIER_CORE
    assert {f.filename for f in bp.build_files()} == {"a.py", "b.py"}


def test_blueprint_actionable_needs_two_build_files():
    one = blueprint_from_data(
        {"files": [{"filename": "index.html"}]}, "build a page", STDLIB_STACK
    )
    assert one.is_actionable() is False
    two = blueprint_from_data(
        {"files": [{"filename": "index.html"}, {"filename": "app.js"}]},
        "build a page",
        STDLIB_STACK,
    )
    assert two.is_actionable() is True


def test_blueprint_from_none_is_empty_and_inert():
    bp = blueprint_from_data(None, "build a page", STDLIB_STACK)
    assert bp.is_actionable() is False
    assert bp.build_files() == ()


# ---------------------------------------------------------------------------
# _ensure_backend — synthesize the server file the model declared but forgot
# ---------------------------------------------------------------------------


def test_ensure_backend_synthesizes_from_declared_endpoint():
    """The exact live failure: contact form declares POST /submit + a 'Backend
    Server' feature but no server file in `files` → net adds one."""
    data = {
        "files": [{"filename": "contact.html", "role": "frontend"}],
        "features": [
            {"name": "Contact Form", "tier": "requested", "files": ["contact.html"]},
            {"name": "Backend Server", "tier": "core", "files": []},
        ],
        "contract": {
            "endpoints": [
                {"method": "POST", "path": "/submit", "request": "{name,email,message}"}
            ]
        },
    }
    bp = blueprint_from_data(data, "make a contact form", STDLIB_STACK)
    names = [f.filename for f in bp.build_files()]
    assert "server.py" in names
    assert bp.is_actionable() is True  # was 1 file, now 2


def test_ensure_backend_from_feature_name_without_endpoint():
    data = {
        "files": [{"filename": "todo.html"}],
        "features": [{"name": "Backend Logic", "tier": "core", "files": []}],
    }
    bp = blueprint_from_data(data, "build a todo app", STDLIB_STACK)
    assert "server.py" in [f.filename for f in bp.build_files()]


def test_ensure_backend_not_added_when_no_signal():
    """A genuinely static build (no endpoints, no backend feature) is left alone."""
    data = {
        "files": [{"filename": "index.html"}, {"filename": "styles.css"}],
        "features": [
            {"name": "Landing hero", "tier": "requested", "files": ["index.html"]}
        ],
    }
    bp = blueprint_from_data(data, "build a landing page", STDLIB_STACK)
    assert "server.py" not in [f.filename for f in bp.files]


def test_ensure_backend_not_duplicated_when_present():
    data = {
        "files": [
            {"filename": "login.html"},
            {"filename": "server.py", "role": "backend"},
        ],
        "contract": {"endpoints": [{"method": "POST", "path": "/api/login"}]},
    }
    bp = blueprint_from_data(data, "build a login page", STDLIB_STACK)
    server_files = [f for f in bp.files if f.filename == "server.py"]
    assert len(server_files) == 1  # not duplicated


def test_ensure_backend_uses_node_filename_for_node_stack():
    node = Stack("node", "express", True, "node available")
    data = {
        "files": [{"filename": "index.html"}],
        "contract": {"endpoints": [{"method": "GET", "path": "/api/todos"}]},
    }
    bp = blueprint_from_data(data, "build a todo app", node)
    names = [f.filename for f in bp.build_files()]
    assert "server.js" in names
    assert "server.py" not in names


# ---------------------------------------------------------------------------
# The chat() seam — inert when off, drives _multi_file_flow when on
# ---------------------------------------------------------------------------


def _actionable_blueprint():
    return Blueprint(
        summary="A login page with auth",
        features=(
            Feature("Login", TIER_REQUESTED, ("login.html",)),
            Feature("Backend", TIER_CORE, ("server.py",)),
            Feature("OAuth", TIER_OPTIONAL, ("oauth.py",)),
        ),
        files=(
            PlannedFile("login.html", "create", "the form"),
            PlannedFile("server.py", "create", "the /api/login route"),
            PlannedFile("oauth.py", "create", "optional oauth"),
        ),
        contract=ApiContract(
            endpoints=(Endpoint("POST", "/api/login", "{email,password}", "200|401"),),
        ),
        stack=STDLIB_STACK,
    )


async def test_seam_inert_when_flag_off(tmp_path, monkeypatch):
    """Flag off (the default): the blueprint method is never even consulted, and
    a build request routes exactly as it does today."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", False)
    a = AgentCore(session_id="pytest_bp_off")

    async def _boom(*args, **kwargs):
        raise AssertionError("_expand_requirements must not run when flag is off")

    monkeypatch.setattr(a, "_expand_requirements", _boom)
    a._llm_direct = ScriptedLLM(["FILENAME: login.html\n<html></html>"])

    answer, trace = await a.chat("build me a login page")

    assert (tmp_path / "login.html").is_file()  # ordinary single-file flow ran


async def test_seam_skips_non_build_when_flag_on(tmp_path, monkeypatch):
    """Flag on, but a question isn't a build — the gate skips expansion."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    a = AgentCore(session_id="pytest_bp_question")

    async def _boom(*args, **kwargs):
        raise AssertionError("_expand_requirements must not run for a question")

    monkeypatch.setattr(a, "_expand_requirements", _boom)

    routed = {}

    async def _fake_route(msg, refs, **kwargs):
        routed["msg"] = msg
        return "answered", []

    monkeypatch.setattr(a, "_route_one", _fake_route)

    answer, _ = await a.chat("explain how a login page works")
    assert answer == "answered"


async def test_seam_runs_blueprint_when_on_and_actionable(tmp_path, monkeypatch):
    """Flag on + a greenfield build + an actionable blueprint → the blueprint's
    files become preplanned_ops and its contract rides in extra_context."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "blueprint_optional_tier", False)
    a = AgentCore(session_id="pytest_bp_on")

    async def _fake_expand(msg, entities=()):
        return _actionable_blueprint()

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)

    captured = {}

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        captured["ops"] = preplanned_ops
        captured["extra"] = extra_context
        return "Handled 2 file(s)", []  # empty trace → ref-repair block skipped

    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    answer, _ = await a.chat("build me a login page")

    ops = captured["ops"]
    assert [o.filename for o in ops] == ["login.html", "server.py"]  # oauth excluded
    assert "/api/login" in captured["extra"]  # contract threaded in
    assert "OAuth" in answer  # optional feature reported, not built


async def test_seam_falls_through_when_blueprint_not_actionable(tmp_path, monkeypatch):
    """A one-file blueprint doesn't expand anything → defer to normal routing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    a = AgentCore(session_id="pytest_bp_thin")

    async def _fake_expand(msg, entities=()):
        return Blueprint(files=(PlannedFile("index.html"),))  # 1 file, not actionable

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)

    async def _boom(*args, **kwargs):
        raise AssertionError("_run_blueprint must not run for a thin blueprint")

    monkeypatch.setattr(a, "_run_blueprint", _boom)

    async def _fake_route(msg, refs, **kwargs):
        return "normal routing", []

    monkeypatch.setattr(a, "_route_one", _fake_route)

    answer, _ = await a.chat("build me a login page")
    assert answer == "normal routing"


# ---------------------------------------------------------------------------
# Blueprint coverage verification (Phase 2) — weaknesses.md #3
# ---------------------------------------------------------------------------


def _bp_with(files, endpoints=()):
    return Blueprint(
        summary="a build",
        features=(),
        files=tuple(PlannedFile(f) for f in files),
        contract=ApiContract(endpoints=tuple(endpoints)),
        stack=STDLIB_STACK,
    )


def test_unwired_endpoints_flags_routes_absent_from_backend(tmp_path):
    a = AgentCore(session_id="pytest_unwired")
    (tmp_path / "server.py").write_text("# handles /api/login here\n", encoding="utf-8")
    bp = _bp_with(
        ["server.py"],
        endpoints=[
            Endpoint("POST", "/api/login"),
            Endpoint("POST", "/api/reset"),  # not in server.py
        ],
    )
    unwired = a._unwired_endpoints(bp, tmp_path)
    assert unwired == ["/api/reset"]


def test_unwired_endpoints_empty_when_all_defined(tmp_path):
    a = AgentCore(session_id="pytest_wired")
    (tmp_path / "server.py").write_text(
        "routes = ['/api/login', '/api/reset']\n", encoding="utf-8"
    )
    bp = _bp_with(
        ["server.py"],
        endpoints=[Endpoint("POST", "/api/login"), Endpoint("POST", "/api/reset")],
    )
    assert a._unwired_endpoints(bp, tmp_path) == []


async def test_coverage_creates_missing_planned_file(tmp_path, monkeypatch):
    """The exact failure the user reported: the backend file never got written.
    Coverage creates it, threading the contract."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_create")
    (tmp_path / "login.html").write_text("<html></html>", encoding="utf-8")
    a._llm_direct = ScriptedLLM(["FILENAME: server.py\nprint('serving /api/login')\n"])
    bp = _bp_with(
        ["login.html", "server.py"],  # login.html exists, server.py is missing
        endpoints=[Endpoint("POST", "/api/login")],
    )

    note, cov_trace = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "server.py").is_file()
    assert "server.py" in note
    assert "Created missing" in note


async def test_coverage_reports_unwired_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_report")
    (tmp_path / "server.py").write_text("# only /api/login\n", encoding="utf-8")
    bp = _bp_with(
        ["server.py"],
        endpoints=[Endpoint("POST", "/api/login"), Endpoint("POST", "/api/reset")],
    )

    note, _ = await a._verify_blueprint_coverage(bp, [])
    assert "may not meet" in note
    assert "/api/reset" in note


async def test_coverage_creates_a_template_a_route_renders(tmp_path, monkeypatch):
    """Check 1 covers the files the blueprint PLANNED; nothing covered the ones
    generation invented. Live build: the model added `/signup` to app.py and
    rendered `signup.html`, which no pass had planned or written — a
    TemplateNotFound 500 on a link in the site's own nav."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_template")
    (tmp_path / "templates").mkdir()
    (tmp_path / "app.py").write_text(
        '@app.route("/signup")\n'
        "def signup():\n"
        '    return render_template("signup.html")\n',
        encoding="utf-8",
    )
    a._llm_direct = ScriptedLLM(
        [
            "FILENAME: templates/signup.html\n"
            '{% extends "base.html" %}{% block content %}<form></form>{% endblock %}\n'
        ]
    )
    bp = _bp_with(["app.py"])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "templates" / "signup.html").is_file()
    assert "signup.html" in note


async def test_coverage_leaves_an_existing_rendered_template_alone(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_template_ok")
    (tmp_path / "templates").mkdir()
    original = "<p>mine</p>"
    (tmp_path / "templates" / "signup.html").write_text(original, encoding="utf-8")
    (tmp_path / "app.py").write_text(
        '@app.route("/signup")\n'
        "def signup():\n"
        '    return render_template("signup.html")\n',
        encoding="utf-8",
    )
    bp = _bp_with(["app.py"])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "templates" / "signup.html").read_text() == original
    assert "signup.html" not in note


async def test_coverage_does_not_invent_a_dynamic_template_name(tmp_path, monkeypatch):
    """`render_template(name + ".html")` cannot be resolved, and writing a file
    for a guessed name is worse than the 500 it replaces."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_template_dynamic")
    (tmp_path / "templates").mkdir()
    (tmp_path / "app.py").write_text(
        '@app.route("/p/<slug>")\n'
        "def page(slug):\n"
        '    return render_template(slug + ".html")\n',
        encoding="utf-8",
    )
    bp = _bp_with(["app.py"])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert list((tmp_path / "templates").iterdir()) == []
    assert "Created template" not in note


def _flask_app(routes: str) -> str:
    return (
        "from flask import Flask, render_template\n"
        "app = Flask(__name__)\n" + routes + '\nif __name__ == "__main__":\n'
        "    app.run()\n"
    )


async def test_wiring_adds_a_route_the_pages_need(tmp_path, monkeypatch):
    """The blueprint plans eleven routes, the model's one surgical edit lands
    six, and the pages the SAME build wrote then 500 on `url_for('new_category')`.
    Coverage already computed that list and only reported it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "wire_missing_endpoints", True)
    a = AgentCore(session_id="pytest_wire_add")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "categories.html").write_text(
        "<a href=\"{{ url_for('new_category') }}\">Add</a>", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        _flask_app(
            '@app.route("/categories")\n'
            "def categories():\n"
            '    return render_template("categories.html")\n'
        ),
        encoding="utf-8",
    )
    # app.py exists, so the edit goes through `_surgical_edit` → `_llm_edit`.
    # Unscripted it reaches a real ChatOllama and the test silently stops being
    # offline — conftest's `_no_intent_check` trap in another hat.
    a._llm_edit = ScriptedLLM(["no blocks"])  # force the whole-file rewrite
    a._llm_direct = ScriptedLLM(
        [
            "FILENAME: app.py\n"
            + _flask_app(
                '@app.route("/categories")\n'
                "def categories():\n"
                '    return render_template("categories.html")\n\n'
                '@app.route("/categories/new")\n'
                "def new_category():\n"
                '    return render_template("new_category.html")\n'
            )
        ]
    )
    bp = _bp_with(["app.py"], endpoints=[Endpoint("GET", "/categories/new")])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert "new_category" in (tmp_path / "app.py").read_text()
    assert "Wired" in note
    assert "may not meet: still no route" not in note


async def test_wiring_is_skipped_when_nothing_is_missing(tmp_path, monkeypatch):
    """A correct build costs no LLM call and comes out byte-for-byte the same."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "wire_missing_endpoints", True)
    a = AgentCore(session_id="pytest_wire_noop")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "categories.html").write_text(
        "<a href=\"{{ url_for('categories') }}\">All</a>", encoding="utf-8"
    )
    source = _flask_app(
        '@app.route("/categories")\n'
        "def categories():\n"
        '    return render_template("categories.html")\n'
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")

    def _boom(*args, **kwargs):
        raise AssertionError("a build with nothing missing must not be edited")

    a._llm_direct = SimpleNamespace(invoke=_boom)
    bp = _bp_with(["app.py"], endpoints=[Endpoint("GET", "/categories")])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "app.py").read_text() == source
    assert "Wired" not in note


async def test_wiring_puts_the_index_route_back(tmp_path, monkeypatch):
    """This is a whole-file rewrite of the very file
    `_restore_scaffold_invariants` protects, and it runs AFTER it — so it
    deletes the `/` route straight back out. Measured: `/` 404'd on a build
    whose answer said, truthfully, that the home page had been restored."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "wire_missing_endpoints", True)
    a = AgentCore(session_id="pytest_wire_index")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "categories.html").write_text(
        "<a href=\"{{ url_for('new_category') }}\">Add</a>", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        _flask_app(
            '@app.route("/")\n'
            "def index():\n"
            '    return render_template("index.html")\n'
        ),
        encoding="utf-8",
    )
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(  # a rewrite that drops "/", as 7B builds do
        [
            "FILENAME: app.py\n"
            + _flask_app(
                '@app.route("/categories/new")\n'
                "def new_category():\n"
                '    return render_template("new_category.html")\n'
            ),
            # Restoring `/` gives the file a route rendering index.html, which
            # `_create_rendered_templates` then writes. ScriptedLLM repeats its
            # LAST output forever, so this one has to be a template — replaying
            # the app.py block would land the routeless source back on disk and
            # the test would "reproduce" a bug it had itself caused.
            "FILENAME: templates/index.html\n<p>home</p>\n",
        ]
    )
    bp = _bp_with(["app.py"], endpoints=[Endpoint("GET", "/categories/new")])

    await a._verify_blueprint_coverage(bp, [])

    source = (tmp_path / "app.py").read_text()
    assert "new_category" in source  # the wiring landed
    assert '@app.route("/")' in source  # ...and did not cost the home page


async def test_wiring_reverts_an_edit_that_breaks_the_entry_file(tmp_path, monkeypatch):
    """Every page is downstream of this one file, so a bad edit is a total
    outage rather than one 500 — `_intent_repair`'s revert rule, load-bearing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "wire_missing_endpoints", True)
    a = AgentCore(session_id="pytest_wire_revert")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "categories.html").write_text(
        "<a href=\"{{ url_for('new_category') }}\">Add</a>", encoding="utf-8"
    )
    source = _flask_app(
        '@app.route("/categories")\n'
        "def categories():\n"
        '    return render_template("categories.html")\n'
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\ndef broken(:\n"])
    bp = _bp_with(["app.py"], endpoints=[Endpoint("GET", "/categories/new")])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "app.py").read_text() == source  # reverted, byte-for-byte
    assert "reverted" in note


async def test_wiring_reports_what_it_could_not_fix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "wire_missing_endpoints", True)
    a = AgentCore(session_id="pytest_wire_partial")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "categories.html").write_text(
        "<a href=\"{{ url_for('new_category') }}\">Add</a>", encoding="utf-8"
    )
    source = _flask_app(
        '@app.route("/categories")\n'
        "def categories():\n"
        '    return render_template("categories.html")\n'
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    # A syntactically fine rewrite that adds nothing — one attempt, no loop.
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\n" + source])
    bp = _bp_with(["app.py"])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert "may not meet: still no route" in note
    assert "new_category" in note


async def test_wiring_off_reports_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "wire_missing_endpoints", False)
    a = AgentCore(session_id="pytest_wire_off")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "categories.html").write_text(
        "<a href=\"{{ url_for('new_category') }}\">Add</a>", encoding="utf-8"
    )
    source = _flask_app(
        '@app.route("/categories")\n'
        "def categories():\n"
        '    return render_template("categories.html")\n'
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")

    def _boom(*args, **kwargs):
        raise AssertionError("the flag is off; nothing may be edited")

    a._llm_direct = SimpleNamespace(invoke=_boom)
    bp = _bp_with(["app.py"], endpoints=[Endpoint("GET", "/categories/new")])

    note, _ = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "app.py").read_text() == source
    assert "may not meet" in note  # the old report is unchanged


async def test_final_restore_puts_the_index_route_back(tmp_path, monkeypatch):
    """The smoke repair is the LAST pass that rewrites the entry file, and it
    rewrites it wholesale. Measured across three live builds: the answer said,
    truthfully, that the home page had been restored, and the finished site
    still 404'd on its front door."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_final_restore")
    (tmp_path / "app.py").write_text(
        _flask_app(
            '@app.route("/items")\n'
            "def items():\n"
            '    return render_template("items.html")\n'
        ),
        encoding="utf-8",
    )

    note = await a._restore_entry_route_note()

    assert '@app.route("/")' in (tmp_path / "app.py").read_text()
    assert "front page" in note


async def test_final_restore_is_a_noop_when_the_route_is_there(tmp_path, monkeypatch):
    """Idempotent: a build that never lost `/` is untouched and says nothing."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_final_restore_noop")
    source = _flask_app(
        '@app.route("/")\ndef index():\n    return render_template("index.html")\n'
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")

    note = await a._restore_entry_route_note()

    assert (tmp_path / "app.py").read_text() == source
    assert note == ""


async def test_coverage_inert_when_no_blueprint_ran(tmp_path, monkeypatch):
    """A non-blueprint turn (self._blueprint is None) never runs coverage."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", False)
    a = AgentCore(session_id="pytest_cov_inert")

    async def _boom(*args, **kwargs):
        raise AssertionError("coverage must not run when no blueprint drove the turn")

    monkeypatch.setattr(a, "_verify_blueprint_coverage", _boom)
    a._llm_direct = ScriptedLLM(["FILENAME: notes.md\n# notes\n"])

    await a.chat("create a notes.md file")  # ordinary single-file turn
    assert (tmp_path / "notes.md").is_file()


# ---------------------------------------------------------------------------
# Phase C — schema first, layout derived from it
# ---------------------------------------------------------------------------


FLASK = Stack(language="python", backend="flask", note="Flask + Jinja2 + sqlite3")


def _entities(*specs):
    """Build entities the way `entities_from_data` would, from (table, fields)."""
    from app.agent.projectspec import entities_from_data

    return entities_from_data(
        {
            "entities": [
                {"name": name, "table": table, "fields": fields}
                for name, table, fields in specs
            ]
        }
    )


_PRODUCT = ("product", "products", [{"name": "title", "type": "TEXT"}])
_REVIEW = ("review", "reviews", [{"name": "body", "type": "TEXT"}])


def test_entities_from_data_parses_the_schema_calls_shape():
    from app.agent.projectspec import entities_from_data

    entities = entities_from_data(
        {
            "entities": [
                {
                    "name": "product",
                    "table": "products",
                    "fields": [
                        {"name": "id", "type": "INTEGER", "pk": True},
                        {"name": "title", "type": "VARCHAR(200)", "required": True},
                        {"name": "image_path", "type": "IMAGE"},
                    ],
                }
            ]
        }
    )

    assert len(entities) == 1
    product = entities[0]
    assert (product.name, product.table) == ("product", "products")
    assert product.field("title").type == "TEXT"  # VARCHAR normalised
    assert product.field("title").required is True
    assert product.field("image_path").type == "TEXT"  # IMAGE stored as a path
    assert product.field("image_path").is_upload() is True


def test_entities_from_data_adds_a_primary_key_when_the_model_forgets():
    """A products table with no id makes edit and delete unwriteable."""
    entities = _entities(("product", "products", [{"name": "title"}]))
    assert entities[0].fields[0].name == "id"
    assert entities[0].fields[0].pk is True


def test_entities_from_data_drops_what_it_cannot_use():
    from app.agent.projectspec import entities_from_data

    assert entities_from_data(None) == ()
    assert entities_from_data({"entities": []}) == ()
    assert entities_from_data({"entities": [{"table": "no fields here"}]}) == ()
    assert entities_from_data({"entities": ["not a dict"]}) == ()


def test_the_schema_is_authoritative_over_the_models_own_free_text():
    """Two sources of truth for the tables is one too many: the data layer is
    generated from `entities`, so `data_schema` must print from the same list."""
    bp = blueprint_from_data(
        {
            "files": [{"filename": "app.py"}, {"filename": "templates/x.html"}],
            "contract": {"data_schema": ["widgets(name TEXT)"]},
        },
        "build a shop",
        FLASK,
        _entities(_PRODUCT),
    )
    assert bp.contract.data_schema == ("products(id INTEGER, title TEXT)",)
    assert [e.table for e in bp.entities] == ["products"]


def test_every_entity_gets_a_list_page_a_form_and_routes():
    """Phase C3's postcondition. The model planned pages for products only; the
    reviews table must not silently become unreachable."""
    bp = blueprint_from_data(
        {
            "files": [
                {"filename": "app.py", "role": "backend"},
                {"filename": "templates/products.html", "reads": ["product"]},
            ],
            "contract": {"endpoints": [{"method": "GET", "path": "/products"}]},
        },
        "build a shop with reviews",
        FLASK,
        _entities(_PRODUCT, _REVIEW),
    )

    planned = {pf.filename for pf in bp.files}
    assert "templates/reviews.html" in planned  # list page, synthesized
    assert "templates/new_review.html" in planned  # create form, synthesized
    assert "templates/new_product.html" in planned  # products had no form either
    routes = {(e.method, e.path) for e in bp.contract.endpoints}
    assert ("GET", "/reviews") in routes
    assert ("POST", "/reviews/new") in routes
    assert ("POST", "/products/new") in routes


def test_completion_keeps_what_the_model_already_planned():
    """It fills holes; it never renames or replaces the model's own pages."""
    bp = blueprint_from_data(
        {
            "files": [
                {"filename": "app.py", "role": "backend"},
                {
                    "filename": "templates/catalogue.html",
                    "reads": ["product"],
                    "instruction": "the shop front",
                },
                {"filename": "templates/add_product.html", "reads": ["product"]},
            ],
            "contract": {
                "endpoints": [
                    {"method": "GET", "path": "/catalogue", "entity": "product"},
                    {"method": "POST", "path": "/catalogue/add", "entity": "product"},
                ]
            },
        },
        "build a shop",
        FLASK,
        _entities(_PRODUCT),
    )

    planned = {pf.filename for pf in bp.files}
    assert "templates/catalogue.html" in planned
    assert "templates/add_product.html" in planned
    # Its own listing and form were recognised, so no duplicates were invented.
    assert "templates/products.html" not in planned
    assert "templates/new_product.html" not in planned


def test_completion_is_flask_only():
    """The fixed templates/ layout is what makes the synthesized paths correct;
    on another stack the file layout is the model's own."""
    bp = blueprint_from_data(
        {"files": [{"filename": "server.py"}, {"filename": "index.html"}]},
        "build a shop",
        STDLIB_STACK,
        _entities(_PRODUCT),
    )
    assert "templates/products.html" not in {pf.filename for pf in bp.files}


def test_no_entities_leaves_the_blueprint_exactly_as_before():
    """Phase C is inert when the schema call failed or the app stores nothing."""
    data = {
        "files": [{"filename": "index.html"}, {"filename": "style.css"}],
        "contract": {"data_schema": ["notes(body TEXT)"]},
    }
    assert blueprint_from_data(data, "build a page", FLASK) == blueprint_from_data(
        data, "build a page", FLASK, ()
    )


def test_declared_entity_and_reads_survive_parsing():
    bp = blueprint_from_data(
        {
            "files": [
                {"filename": "templates/products.html", "reads": ["product", "Review"]}
            ],
            "contract": {
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/products",
                        "entity": "product",
                        "template": "templates/products.html",
                    }
                ]
            },
        },
        "build a shop",
        FLASK,
    )
    assert bp.files[0].reads == ("product", "review")  # normalised, lowercased
    assert bp.contract.endpoints[0].entity == "product"
    assert bp.contract.endpoints[0].template == "templates/products.html"


async def test_the_schema_is_decided_before_the_layout(tmp_path, monkeypatch):
    """The seam: one temp-0 call decides what is stored, and its entities are
    handed to the layout call rather than being invented there."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "schema_first", True)
    a = AgentCore(session_id="pytest_schema_seam")
    a._llm_blueprint = ScriptedLLM(
        [
            '{"summary": "a shop", "entities": [{"name": "product", "table": '
            '"products", "fields": [{"name": "title", "type": "TEXT"}]}]}'
        ]
    )

    seen = {}

    async def _fake_expand(msg, entities=()):
        seen["entities"] = entities
        return _actionable_blueprint()

    async def _fake_run(msg, blueprint, refs):
        return "built", []

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_run_blueprint", _fake_run)

    await a.chat("build me a shop")

    assert [e.table for e in seen["entities"]] == ["products"]
    assert a._llm_blueprint.calls == 1  # exactly one extra call, not one per file


async def test_schema_extraction_failure_falls_back_to_the_old_path(
    tmp_path, monkeypatch
):
    """A failed schema call must cost nothing: the layout call then behaves
    exactly as it did before Phase C."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "schema_first", True)
    a = AgentCore(session_id="pytest_schema_fail")
    a._llm_blueprint = ScriptedLLM(["not json at all"])

    seen = {}

    async def _fake_expand(msg, entities=()):
        seen["entities"] = entities
        return _actionable_blueprint()

    async def _fake_run(msg, blueprint, refs):
        return "built", []

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_run_blueprint", _fake_run)

    await a.chat("build me a shop")

    assert seen["entities"] == ()


async def test_schema_stage_is_skipped_when_switched_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "schema_first", False)
    a = AgentCore(session_id="pytest_schema_off")
    a._llm_blueprint = ScriptedLLM(["{}"])

    async def _fake_expand(msg, entities=()):
        return _actionable_blueprint()

    async def _fake_run(msg, blueprint, refs):
        return "built", []

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_run_blueprint", _fake_run)

    await a.chat("build me a shop")

    assert a._llm_blueprint.calls == 0  # no schema call was made


def test_an_image_column_keeps_its_upload_signal():
    """IMAGE/FILE are not SQLite types and normalise to TEXT, which would throw
    away the only marker that this column holds an upload. The signal moves into
    the name, which is what the upload wiring actually keys off."""
    entities = _entities(
        ("book", "books", [{"name": "cover", "type": "IMAGE"}, {"name": "title"}])
    )
    cover = entities[0].field("cover_path")
    assert cover is not None and cover.type == "TEXT"
    assert cover.is_upload() is True
    # A column already named for it is left alone, not doubled up.
    already = _entities(("book", "books", [{"name": "cover_path", "type": "IMAGE"}]))
    assert already[0].field("cover_path") is not None
    assert already[0].field("cover_path_path") is None


def test_a_synthesized_form_page_gets_both_of_its_routes():
    """A form page with only a POST route cannot be opened at all — the same
    dead end, one step earlier."""
    bp = blueprint_from_data(
        {
            "files": [
                {"filename": "app.py", "role": "backend"},
                {"filename": "templates/books.html", "reads": ["book"]},
            ],
            "contract": {
                "endpoints": [{"method": "GET", "path": "/books", "entity": "book"}]
            },
        },
        "build a bookshop",
        FLASK,
        _entities(("book", "books", [{"name": "title"}])),
    )

    routes = {(e.method, e.path) for e in bp.contract.endpoints}
    assert ("GET", "/books/new") in routes  # serves the form
    assert ("POST", "/books/new") in routes  # accepts it


def test_completion_does_not_re_route_pages_the_model_planned():
    """Routes are synthesized only for templates this pass creates. Adding
    `GET /books` beside the model's own listing route would leave one of them
    rendering a template nobody created."""
    bp = blueprint_from_data(
        {
            "files": [
                {"filename": "app.py", "role": "backend"},
                {"filename": "templates/catalogue.html", "reads": ["book"]},
                {"filename": "templates/add_book.html", "reads": ["book"]},
            ],
            "contract": {
                "endpoints": [
                    {"method": "GET", "path": "/catalogue", "entity": "book"},
                    {"method": "POST", "path": "/catalogue/add", "entity": "book"},
                ]
            },
        },
        "build a bookshop",
        FLASK,
        _entities(("book", "books", [{"name": "title"}])),
    )

    paths = {e.path for e in bp.contract.endpoints}
    # The model's own list and form pages keep their own routes and gain no
    # duplicates. `/books/<id>` is not an exception to that rule but an
    # instance of it: the detail page is a template THIS pass creates, and the
    # model planned neither a page for one book nor a route to it.
    assert paths == {"/catalogue", "/catalogue/add", "/books/<id>"}
    assert not any(f.filename.startswith("templates/books") for f in bp.files)
    assert "templates/book_detail.html" in {f.filename for f in bp.files}


# ---------------------------------------------------------------------------
# Phase B — every website request reaches the full-stack path
# ---------------------------------------------------------------------------


async def _fake_route_one(*args, **kwargs):
    """Stand-in for ordinary routing, so a turn that must NOT blueprint still
    finishes without reaching a real Ollama."""
    return "routed the ordinary way", []


@pytest.mark.parametrize(
    "msg",
    [
        # Unanchored, "add ... to" matched the trailing clause of an ordinary
        # greenfield request and vetoed it.
        "build a shop and add reviews to it",
        "create a blog with comments added to each post",
        # The single-file veto swallowed builds that merely mentioned a file.
        "build me a website with a css file for the styling",
        "make a portfolio site and put the js file in static",
    ],
)
def test_should_blueprint_no_longer_vetoes_these_greenfield_builds(msg):
    assert should_blueprint(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "add a login form to index.html",  # opens with the edit verb
        "put a button into the page",
        "please add a footer to the header file",
        "create a css file",  # genuinely one file, no application named
        "make a new html file",
        "make a new html file for the about page",  # "page" is not an app noun
    ],
)
def test_should_blueprint_still_skips_these(msg):
    assert should_blueprint(msg) is False


@pytest.mark.parametrize(
    "msg",
    [
        "build me a recipe organizer",  # verb, but no noun in the list
        "I need somewhere to track my expenses",  # no build verb at all
        "help me make a place where my club can post events",
        "i want an inventory thing for my workshop",
        # A bare noun phrase, no verb and no want-phrasing at all. This is the
        # shape the Phase E eval task uses, and it did NOT reach tier 2 until
        # writing that task exposed it.
        "something to organize my recipes and what goes in them",
        "a place my club can post events",
    ],
)
def test_tier_two_candidates_are_the_ones_the_regex_misses(msg):
    from app.agent.blueprint import may_be_web_build

    assert should_blueprint(msg) is False  # tier 1 misses it
    assert may_be_web_build(msg) is True  # tier 2 gets to ask


@pytest.mark.parametrize(
    "msg",
    [
        "build me a login page",  # tier 1 already said yes — don't ask twice
        "what does a login page need?",  # question
        "show me how routing works",  # question tier 1's regex misses
        "split styles.css into two files",  # split
        "add a footer to index.html",  # opening edit
        "create a css file",  # single file, no application
        "the tests are failing",  # no sign anything should be built
        "run the build",
        # The report-a-problem sense of the same noun phrase: a bug report, not
        # a request for an application.
        "something is wrong with the parser",
        "something broke in the login flow",
        "",
    ],
)
def test_tier_two_is_not_even_asked_about_these(msg):
    from app.agent.blueprint import may_be_web_build

    assert may_be_web_build(msg) is False


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("build me a portfolio site, just html", True),
        ("make a landing page with no backend", True),
        ("build a static-only brochure site", True),
        ("build me a shop", False),
        ("build a site with a backend", False),
    ],
)
def test_static_only_opt_out_is_recognised(msg, expected):
    from app.agent.blueprint import wants_static_only

    assert wants_static_only(msg) is expected


async def test_tier_two_routes_an_unusual_request_to_the_blueprint(
    tmp_path, monkeypatch
):
    """The headline: "a recipe organizer" is not in any noun list, and must
    still get a full-stack build instead of a static page."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "web_intent_fallback", True)
    a = AgentCore(session_id="pytest_tier2_yes")
    a._llm_blueprint = ScriptedLLM(["YES"])

    ran = {}

    async def _fake_expand(msg, entities=()):
        return _actionable_blueprint()

    async def _fake_run(msg, blueprint, refs):
        ran["built"] = True
        return "built", []

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_run_blueprint", _fake_run)

    await a.chat("build me a recipe organizer")

    assert ran.get("built") is True
    assert a._llm_blueprint.calls == 1


async def test_tier_two_no_leaves_routing_alone(tmp_path, monkeypatch):
    """A false positive costs a multi-file build in place of a one-line answer,
    so anything but a clear YES must not blueprint."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "web_intent_fallback", True)
    a = AgentCore(session_id="pytest_tier2_no")
    a._llm_blueprint = ScriptedLLM(["NO"])

    async def _boom(*args, **kwargs):
        raise AssertionError("_run_blueprint must not run on a NO verdict")

    monkeypatch.setattr(a, "_run_blueprint", _boom)
    monkeypatch.setattr(a, "_route_one", _fake_route_one)

    await a.chat("i want to understand my expenses better")


async def test_tier_two_failure_is_a_no(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "web_intent_fallback", True)
    a = AgentCore(session_id="pytest_tier2_boom")

    class _Boom:
        def invoke(self, messages):
            raise RuntimeError("ollama is down")

    a._llm_blueprint = _Boom()

    async def _boom(*args, **kwargs):
        raise AssertionError("_run_blueprint must not run when the call failed")

    monkeypatch.setattr(a, "_run_blueprint", _boom)
    monkeypatch.setattr(a, "_route_one", _fake_route_one)

    await a.chat("i want a recipe organizer")


async def test_tier_two_is_never_asked_when_tier_one_fires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "web_intent_fallback", True)
    a = AgentCore(session_id="pytest_tier2_skip")
    a._llm_blueprint = ScriptedLLM(["YES"])

    async def _fake_expand(msg, entities=()):
        return _actionable_blueprint()

    async def _fake_run(msg, blueprint, refs):
        return "built", []

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_run_blueprint", _fake_run)

    await a.chat("build me a login page")  # tier 1 matches

    assert a._llm_blueprint.calls == 0  # no classifier call was made


# ---------------------------------------------------------------------------
# derive_home_page — the front door nothing used to write
# ---------------------------------------------------------------------------
# The scaffold ships templates/index.html and never overwrites it, the layout
# call is not asked for a home page, and derive_pages_from_entities covers
# entities — which the home page is not one of. So every build shipped the same
# "This project was scaffolded by Coder" placeholder as its first page.

_FLASK = Stack(language="python", backend="flask", runnable=True)


def _shop_data():
    return {
        "summary": "A shop",
        "files": [
            {"filename": "app.py", "action": "edit", "role": "backend"},
            {
                "filename": "templates/products.html",
                "action": "create",
                "role": "frontend",
                "reads": ["product"],
            },
            {
                "filename": "templates/new_product.html",
                "action": "create",
                "role": "frontend",
                "reads": ["product"],
            },
        ],
        "contract": {
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/products",
                    "template": "templates/products.html",
                },
                {
                    "method": "GET",
                    "path": "/products/new",
                    "template": "templates/new_product.html",
                },
                {
                    "method": "POST",
                    "path": "/products/new",
                    "template": "templates/new_product.html",
                },
                {
                    "method": "GET",
                    "path": "/products/<int:pid>",
                    "template": "templates/product.html",
                },
            ]
        },
    }


def test_home_page_is_planned_as_an_edit():
    bp = blueprint_from_data(_shop_data(), "build me a shop", _FLASK)
    home = [f for f in bp.files if f.filename == HOME_TEMPLATE]
    assert len(home) == 1
    # The scaffold already wrote the file, so this is an edit — which is what
    # the plan manifest should say and what `_file_op_flow` will really do.
    assert home[0].action == "edit"
    assert "placeholder" in home[0].instruction.lower()
    assert any(f.name == "Home page" for f in bp.features)


def test_home_page_links_the_listing_pages_but_not_the_forms():
    bp = blueprint_from_data(_shop_data(), "build me a shop", _FLASK)
    instruction = next(f.instruction for f in bp.files if f.filename == HOME_TEMPLATE)
    assert "Products (/products)" in instruction
    # "Add a product" is a button on the products page, not a section of the
    # site; a parameterised route has no fixed URL to link at all.
    assert "/products/new" not in instruction
    assert "<int:pid>" not in instruction


def test_home_page_planned_by_the_model_wins():
    """Planning it twice would put one file through two generation passes, the
    second overwriting the first."""
    data = _shop_data()
    data["files"].append(
        {
            "filename": "templates/index.html",
            "action": "edit",
            "instruction": "the model's own home page",
            "role": "frontend",
        }
    )
    bp = blueprint_from_data(data, "build me a shop", _FLASK)
    homes = [f for f in bp.files if f.filename == HOME_TEMPLATE]
    assert len(homes) == 1
    assert homes[0].instruction == "the model's own home page"


def test_home_page_is_flask_only():
    """On another stack the file layout is the model's own, so `templates/`
    would name a path nothing serves — derive_pages_from_entities' rule."""
    node = Stack(language="node", backend="express", runnable=True)
    bp = blueprint_from_data(_shop_data(), "build me a shop", node)
    assert not any(f.filename == HOME_TEMPLATE for f in bp.files)


def test_home_page_without_routes_still_gets_an_instruction():
    data = {"summary": "x", "files": [{"filename": "app.py", "action": "edit"}]}
    bp = blueprint_from_data(data, "build me a page", _FLASK)
    home = next(f for f in bp.files if f.filename == HOME_TEMPLATE)
    assert "what a visitor can do here" in home.instruction


# ---------------------------------------------------------------------------
# A requirements document outranks the model's own tiering
# ---------------------------------------------------------------------------


def test_a_feature_the_document_asks_for_is_not_left_optional():
    """`prompts/blueprint.md` already says every capability a document
    describes is `requested`. On the OpenBazaar PRD the 7B ignored it for
    "Responsive Web Design" — a whole section of that document, with numeric
    budgets — so it was reported as NOT BUILT and the site shipped without it."""
    data = {
        "summary": "a marketplace",
        "features": [
            {"name": "Responsive Web Design", "tier": "optional", "files": ["a.html"]},
            {"name": "Bidding", "tier": "requested", "files": ["a.html"]},
        ],
        "files": [{"filename": "a.html", "action": "create", "instruction": "x"}],
    }
    doc = "## 4. UI/UX & Responsive Web Design Requirements\nLCP under 1.2s."

    bp = blueprint_from_data(data, "build it", spec_doc=doc)

    tiers = {f.name: f.tier for f in bp.features}
    assert tiers["Responsive Web Design"] == "core"
    assert tiers["Bidding"] == "requested"
    assert "Responsive Web Design" not in (bp.optional_note() or "")


def test_an_optional_feature_the_document_never_mentions_stays_optional():
    """Otherwise the tier stops meaning anything and every build grows OAuth."""
    data = {
        "summary": "a marketplace",
        "features": [{"name": "OAuth login", "tier": "optional", "files": ["a.html"]}],
        "files": [{"filename": "a.html", "action": "create", "instruction": "x"}],
    }

    bp = blueprint_from_data(data, "build it", spec_doc="Sellers list items.")

    assert [f.tier for f in bp.features] == ["optional"]


def test_no_document_leaves_every_tier_alone():
    data = {
        "summary": "a marketplace",
        "features": [{"name": "OAuth login", "tier": "optional", "files": ["a.html"]}],
        "files": [{"filename": "a.html", "action": "create", "instruction": "x"}],
    }

    assert [f.tier for f in blueprint_from_data(data, "build it").features] == [
        "optional"
    ]


# ---------------------------------------------------------------------------
# Every entity gets a page for ONE of it, not only a list and a form
# ---------------------------------------------------------------------------


def _entity(name="item", table="items"):
    return Entity(
        name=name,
        table=table,
        fields=(Field("id", "INTEGER", pk=True), Field("title", "TEXT")),
    )


def test_every_entity_gets_a_detail_page_and_its_route():
    """The OpenBazaar PRD builds half the product on the Product Detail Page —
    the carousel, the countdown, the bid buttons, the Buy Now CTA. A build that
    derives only "list" and "new" has nowhere to put any of it."""
    files, features, contract = derive_pages_from_entities(
        (), (), ApiContract(), (_entity(),), FLASK
    )

    assert "templates/item_detail.html" in {f.filename for f in files}
    detail = next(e for e in contract.endpoints if e.path == "/items/<id>")
    assert detail.method == "GET"
    assert detail.template == "templates/item_detail.html"


def test_the_detail_route_is_declared_after_the_create_form():
    """Express matches in registration order, so `/items/:id` declared above
    `/items/new` swallows the form and "new" arrives as an id."""
    node_stack = Stack(language="node", backend="express", note="Express")
    _files, _features, contract = derive_pages_from_entities(
        (), (), ApiContract(), (_entity(),), node_stack
    )
    paths = [e.path for e in contract.endpoints]

    assert paths.index("/items/new") < paths.index("/items/:id")


def test_a_detail_page_the_model_already_planned_is_not_duplicated():
    planned = (
        PlannedFile(
            "templates/item_details.html", instruction="one item", reads=("item",)
        ),
    )
    files, _features, _contract = derive_pages_from_entities(
        planned, (), ApiContract(), (_entity(),), FLASK
    )

    assert "templates/item_detail.html" not in {f.filename for f in files}
