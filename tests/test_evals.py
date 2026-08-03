"""Tests for the offline eval harness (roadmap Tier 2 #6).

The harness itself is exercised fully offline with a scripted LLM. The golden
suite's *live* run against Ollama is a separate manual invocation (evals/run.py)
and is NOT part of pytest.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.checks import (
    answer_contains,
    any_file_matches,
    backend_defines_route,
    backend_reads_fields,
    file_contains,
    file_excludes,
    file_exists,
    frontend_calls_route,
    has_backend_server,
    min_files_written,
    route_wired,
    used_tool,
)
from evals.harness import CheckContext, EvalTask, run_suite, run_task
from evals.tasks import BLUEPRINT_TASKS, GOLDEN_TASKS


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# Check factories — pure, no agent needed
# ---------------------------------------------------------------------------


def _ctx(answer="", trace=None, workdir=Path(".")):
    return CheckContext(answer=answer, trace=trace or [], workdir=Path(workdir))


def test_answer_contains_case_insensitive():
    ok, _ = answer_contains("HELLO")(_ctx(answer="well hello there"))
    assert ok is True
    bad, detail = answer_contains("zzz")(_ctx(answer="nope"))
    assert bad is False
    assert "zzz" in detail


def test_file_exists(tmp_path):
    (tmp_path / "a.py").write_text("x=1", encoding="utf-8")
    assert file_exists("a.py")(_ctx(workdir=tmp_path))[0] is True
    assert file_exists("ghost.py")(_ctx(workdir=tmp_path))[0] is False


def test_file_contains(tmp_path):
    (tmp_path / "s.css").write_text("body{color:red}", encoding="utf-8")
    assert file_contains("s.css", "color:red")(_ctx(workdir=tmp_path))[0] is True
    assert file_contains("s.css", "blue")(_ctx(workdir=tmp_path))[0] is False
    # missing file → fail, not crash
    assert file_contains("no.css", "x")(_ctx(workdir=tmp_path))[0] is False


def test_file_excludes(tmp_path):
    (tmp_path / "i.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    assert file_excludes("i.html", "<style>")(_ctx(workdir=tmp_path))[0] is True
    assert file_excludes("i.html", "body")(_ctx(workdir=tmp_path))[0] is False
    # missing file cannot contain the string → treated as excluded (pass)
    assert file_excludes("gone.html", "x")(_ctx(workdir=tmp_path))[0] is True


def test_used_tool():
    trace = [{"tool": "write_file", "result": {"success": True}}]
    assert used_tool("write_file")(_ctx(trace=trace))[0] is True
    assert used_tool("run_command")(_ctx(trace=trace))[0] is False


def test_min_files_written():
    trace = [
        {"tool": "write_file", "result": {"success": True}},
        {"tool": "write_file", "result": {"success": True}},
        {"tool": "read_file", "result": {"success": True}},
    ]
    assert min_files_written(2)(_ctx(trace=trace))[0] is True
    assert min_files_written(3)(_ctx(trace=trace))[0] is False


# ---------------------------------------------------------------------------
# Golden task suite is well-formed
# ---------------------------------------------------------------------------


def test_golden_tasks_wellformed():
    assert len(GOLDEN_TASKS) >= 10
    ids = [t.id for t in GOLDEN_TASKS]
    assert len(ids) == len(set(ids))  # unique ids
    for t in GOLDEN_TASKS:
        assert t.prompt.strip()
        assert t.checks  # every task asserts at least one outcome


def test_blueprint_tasks_wellformed():
    assert len(BLUEPRINT_TASKS) >= 3
    ids = [t.id for t in BLUEPRINT_TASKS]
    assert len(ids) == len(set(ids))
    golden_ids = {t.id for t in GOLDEN_TASKS}
    for t in BLUEPRINT_TASKS:
        assert t.prompt.strip()
        assert t.checks
        assert t.id not in golden_ids  # no collision with the default suite


# ---------------------------------------------------------------------------
# Coherence checks (weaknesses.md #7) — pure, over files on disk
# ---------------------------------------------------------------------------


def test_has_backend_server_python(tmp_path):
    (tmp_path / "server.py").write_text("print('hi')", encoding="utf-8")
    assert has_backend_server()(_ctx(workdir=tmp_path))[0] is True


def test_has_backend_server_rejects_frontend_only(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "script.js").write_text("console.log('client only')", encoding="utf-8")
    ok, detail = has_backend_server()(_ctx(workdir=tmp_path))
    assert ok is False
    assert "frontend/static" in detail


def test_has_backend_server_node_with_marker(tmp_path):
    (tmp_path / "app.js").write_text(
        "const http=require('http'); http.createServer(()=>{}).listen(3000)",
        encoding="utf-8",
    )
    assert has_backend_server()(_ctx(workdir=tmp_path))[0] is True


def test_any_file_matches_finds_across_files(tmp_path):
    (tmp_path / "a.html").write_text("<form onsubmit='send()'>", encoding="utf-8")
    ok, _ = any_file_matches(["fetch(", "onsubmit"], exts=(".html",))(
        _ctx(workdir=tmp_path)
    )
    assert ok is True
    miss, detail = any_file_matches(["fetch("], exts=(".html",))(_ctx(workdir=tmp_path))
    assert miss is False


def test_route_wired_needs_both_sides(tmp_path):
    (tmp_path / "login.html").write_text(
        "<script>fetch('/api/login')</script>", encoding="utf-8"
    )
    # frontend calls it but no backend defines it yet
    assert route_wired("/api/login")(_ctx(workdir=tmp_path))[0] is False
    assert frontend_calls_route("/api/login")(_ctx(workdir=tmp_path))[0] is True
    assert backend_defines_route("/api/login")(_ctx(workdir=tmp_path))[0] is False
    # add the backend → now wired
    (tmp_path / "server.py").write_text("# route /api/login\n", encoding="utf-8")
    assert route_wired("/api/login")(_ctx(workdir=tmp_path))[0] is True


def test_backend_reads_fields(tmp_path):
    (tmp_path / "server.py").write_text(
        "email = form['email']\npassword = form['password']\n", encoding="utf-8"
    )
    assert (
        backend_reads_fields(["email", "password"])(_ctx(workdir=tmp_path))[0] is True
    )
    ok, detail = backend_reads_fields(["email", "token"])(_ctx(workdir=tmp_path))
    assert ok is False
    assert "token" in detail


def test_coherence_checks_skip_vendored_dirs(tmp_path):
    node = tmp_path / "node_modules" / "pkg"
    node.mkdir(parents=True)
    (node / "server.py").write_text("http.server stuff", encoding="utf-8")
    # a backend inside node_modules must NOT count
    assert has_backend_server()(_ctx(workdir=tmp_path))[0] is False


# ---------------------------------------------------------------------------
# run_task / run_suite against a scripted agent
# ---------------------------------------------------------------------------


def _agent(monkeypatch, direct_outputs, task_type="code_generation"):
    from app.agent.core import AgentCore

    a = AgentCore(session_id="pytest_evals")
    monkeypatch.setattr(a.planner, "classify", lambda msg: task_type)
    a._llm_direct = ScriptedLLM(direct_outputs)
    a._llm_edit = ScriptedLLM(["no blocks"])
    return a


async def test_run_task_passes(tmp_path, monkeypatch):
    a = _agent(monkeypatch, ["FILENAME: hi.py\nx = 1\n"])
    task = EvalTask(
        id="create_hi",
        prompt="make hi.py that sets x to 1",
        checks=[file_exists("hi.py"), file_contains("hi.py", "x = 1")],
    )
    res = await run_task(a, task, workdir=tmp_path)
    assert res.passed is True
    assert res.task_id == "create_hi"


async def test_run_task_fails_reports_failing_check(tmp_path, monkeypatch):
    a = _agent(monkeypatch, ["FILENAME: hi.py\nx = 1\n"])
    task = EvalTask(
        id="wrong_expectation",
        prompt="make hi.py",
        checks=[file_contains("hi.py", "THIS_IS_NOT_THERE")],
    )
    res = await run_task(a, task, workdir=tmp_path)
    assert res.passed is False
    assert any("THIS_IS_NOT_THERE" in d for d in res.details)


async def test_run_task_survives_agent_exception(tmp_path, monkeypatch):
    from app.agent.core import AgentCore

    a = AgentCore(session_id="pytest_evals_boom")

    async def boom(msg):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(a, "chat", boom)
    task = EvalTask(id="boom", prompt="x", checks=[answer_contains("x")])
    res = await run_task(a, task, workdir=tmp_path)
    assert res.passed is False
    assert any("kaboom" in d for d in res.details)


async def test_blueprint_task_end_to_end_offline(tmp_path, monkeypatch):
    """Run a blueprint golden task through run_task with the flag on, a canned
    blueprint and scripted file bodies — proving the coherence checks pass
    against the files the pipeline actually writes (no live Ollama)."""
    from app.agent.blueprint import (
        ApiContract,
        Blueprint,
        Endpoint,
        Feature,
        PlannedFile,
    )
    from app.agent.core import AgentCore
    from config.settings import settings

    monkeypatch.setattr(settings, "expand_requirements", True)
    a = AgentCore(session_id="pytest_bp_e2e")

    bp = Blueprint(
        summary="login page + backend",
        features=(
            Feature("Login", "requested", ("login.html",)),
            Feature("Auth backend", "core", ("server.py",)),
        ),
        files=(
            PlannedFile("login.html", "create", "the login form"),
            PlannedFile("server.py", "create", "the /api/login route"),
        ),
        contract=ApiContract(
            endpoints=(Endpoint("POST", "/api/login", "{email,password}", "200|401"),),
        ),
    )

    async def _fake_expand(msg, entities=()):
        return bp

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(
        [
            "FILENAME: login.html\n<!doctype html>\n<html><body>"
            "<form id=\"login-form\" onsubmit=\"fetch('/api/login',{method:'POST'})\">"
            '<input name="email"><input name="password"></form></body></html>\n',
            "FILENAME: server.py\nfrom http.server import BaseHTTPRequestHandler\n"
            "# POST /api/login reads email and password\n"
            "class H(BaseHTTPRequestHandler):\n    pass\n",
        ]
    )

    task = next(t for t in BLUEPRINT_TASKS if t.id == "bp_login_fullstack")
    res = await run_task(a, task, workdir=tmp_path)

    assert res.passed is True, res.details
    assert (tmp_path / "login.html").is_file()
    assert (tmp_path / "server.py").is_file()


async def test_run_suite_scores(tmp_path, monkeypatch):
    a = _agent(monkeypatch, ["FILENAME: ok.py\nx = 1\n"])
    tasks = [
        EvalTask(id="good", prompt="make ok.py", checks=[file_exists("ok.py")]),
        EvalTask(id="bad", prompt="make ok.py", checks=[file_exists("missing.py")]),
    ]
    report = await run_suite(a, tasks, base_dir=tmp_path)
    assert report.total == 2
    assert report.passed == 1
    assert report.score == 0.5
    # each task gets an isolated subdir so files don't collide
    assert {r.task_id for r in report.results} == {"good", "bad"}


# ---------------------------------------------------------------------------
# Phase E checks (docs/always-fullstack-plan.md) — the spec-driven ones.
#
# `entities_are_usable` starts a REAL Flask app in a subprocess, like
# tests/test_functional_probe.py: a check that only ever runs against a fake has
# not been tested at all, and this one exists to catch apps that answer but do
# nothing.
# ---------------------------------------------------------------------------

import json
import sqlite3
import textwrap

from evals.checks import (
    _entity_routes,
    app_serves,
    entities_are_usable,
    every_entity_has_a_table,
    is_full_stack_app,
)


def _wctx(workdir):
    """A context for a check that only looks at the workdir. Distinct from the
    module's `_ctx`, which builds one from an answer/trace."""
    return CheckContext(workdir=Path(workdir), answer="", trace=[])


def _write_spec(workdir, tables):
    """A `.coder/project.json` declaring `tables` = {name: (table, [fields])}."""
    entities = []
    for name, (table, fields) in tables.items():
        entities.append(
            {
                "name": name,
                "table": table,
                "fields": [{"name": "id", "type": "INTEGER", "pk": True}]
                + [{"name": f, "type": "TEXT"} for f in fields],
            }
        )
    (Path(workdir) / ".coder").mkdir(parents=True, exist_ok=True)
    (Path(workdir) / ".coder" / "project.json").write_text(
        json.dumps({"spec_version": 1, "revision": 1, "entities": entities}),
        encoding="utf-8",
    )


def test_a_static_build_is_not_a_full_stack_app(tmp_path):
    """Phase B's regression signal: plausible HTML, no server."""
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")
    (tmp_path / "style.css").write_text("body{}")

    ok, detail = is_full_stack_app()(_wctx(tmp_path))

    assert ok is False
    assert "static" in detail and "index.html" in detail


def test_an_app_py_with_no_routes_is_not_a_full_stack_app(tmp_path):
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    (tmp_path / "templates").mkdir()

    ok, detail = is_full_stack_app()(_wctx(tmp_path))

    assert ok is False
    assert "no route" in detail


def test_a_flask_build_is_a_full_stack_app(tmp_path):
    (tmp_path / "app.py").write_text(
        'from flask import Flask\napp = Flask(__name__)\n@app.route("/")\ndef i(): ...\n'
    )
    (tmp_path / "templates").mkdir()

    ok, detail = is_full_stack_app()(_wctx(tmp_path))

    assert ok is True and "flask" in detail


def test_every_entity_has_a_table_asks_the_database(tmp_path):
    """A CREATE TABLE in a file nobody executes proves nothing."""
    _write_spec(tmp_path, {"recipe": ("recipes", ["title"])})
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    ok, detail = every_entity_has_a_table()(_wctx(tmp_path))

    assert ok is True and "recipes" in detail


def test_every_entity_has_a_table_catches_the_table_that_was_skipped(tmp_path):
    """The four-table build that shipped two — invisible to a check that names
    the one table it already knew about."""
    _write_spec(
        tmp_path,
        {"recipe": ("recipes", ["title"]), "ingredient": ("ingredients", ["name"])},
    )
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    ok, detail = every_entity_has_a_table()(_wctx(tmp_path))

    assert ok is False
    assert "ingredients" in detail and "missing" in detail


def test_every_entity_has_a_table_catches_a_missing_column(tmp_path):
    _write_spec(tmp_path, {"recipe": ("recipes", ["title", "prep_minutes"])})
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    ok, detail = every_entity_has_a_table()(_wctx(tmp_path))

    assert ok is False and "prep_minutes" in detail


def test_entity_routes_fall_back_to_the_synthesized_convention():
    from app.agent.projectspec import Entity, Field, ProjectSpec, SpecEndpoint

    entity = Entity("recipe", "recipes", (Field("id", "INTEGER", pk=True),))
    bare = ProjectSpec()
    assert _entity_routes(bare, entity) == ("/recipes", "/recipes/new")

    # …but the spec's own routes win when it recorded them.
    named = ProjectSpec(
        endpoints=(
            SpecEndpoint("GET", "/cookbook", entity="recipe"),
            SpecEndpoint("POST", "/cookbook/add", entity="recipe"),
        )
    )
    assert _entity_routes(named, entity) == ("/cookbook", "/cookbook/add")


_USABLE_APP = """
import sqlite3
from flask import Flask, request, redirect

app = Flask(__name__)
DB = "app.db"


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/recipes")
def recipes():
    rows = db().execute("SELECT title FROM recipes").fetchall()
    return "<ul>" + "".join(f"<li>{r['title']}</li>" for r in rows) + "</ul>"


@app.route("/recipes/new", methods=["GET", "POST"])
def new_recipe():
    if request.method == "POST":
        conn = db()
        conn.execute("INSERT INTO recipes (title) VALUES (?)", (request.form["title"],))
        conn.commit()
        return redirect("/recipes")
    return "<form method=post><input name=title></form>"


if __name__ == "__main__":
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()
    app.run(port=int(__import__("os").environ.get("PORT", 5000)))
"""

# Same app, one line removed: it answers 302 and writes nothing. Every other
# check in checks.py passes this; only the persistence probe fails it.
_SILENT_APP = _USABLE_APP.replace(
    'conn.execute("INSERT INTO recipes (title) VALUES (?)", (request.form["title"],))',
    "pass",
)


def _usable_project(tmp_path, source):
    (tmp_path / "app.py").write_text(textwrap.dedent(source), encoding="utf-8")
    _write_spec(tmp_path, {"recipe": ("recipes", ["title"])})
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recipes (id INTEGER PRIMARY KEY, title TEXT)"
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_entities_are_usable_passes_a_working_app(tmp_path):
    pytest.importorskip("flask")
    workdir = _usable_project(tmp_path, _USABLE_APP)

    ok, detail = entities_are_usable()(_wctx(workdir))

    assert ok is True, detail
    assert "recipes" in detail


def test_entities_are_usable_catches_an_app_that_never_writes(tmp_path):
    """Answers 302, stores nothing. This is the only check that can see it."""
    pytest.importorskip("flask")
    workdir = _usable_project(tmp_path, _SILENT_APP)

    ok, detail = entities_are_usable()(_wctx(workdir))

    assert ok is False
    assert "never appeared" in detail


def test_entities_are_usable_needs_a_spec_and_an_app(tmp_path):
    ok, detail = entities_are_usable()(_wctx(tmp_path))
    assert ok is False and "no project spec" in detail

    _write_spec(tmp_path, {"recipe": ("recipes", ["title"])})
    ok, detail = entities_are_usable()(_wctx(tmp_path))
    assert ok is False and "no app.py" in detail


# ---------------------------------------------------------------------------
# The same suite, either stack (Phase N6, docs/node-stack-plan.md)
# ---------------------------------------------------------------------------
#
# The Phase E checks generalise for free because they name no table and no
# route — they read the project's own spec. What did NOT generalise was how they
# REACH the app: `workdir / "app.py"`, `import flask`, `templates/` and sqlite3
# were written in directly, so on a Node project every one of them reported that
# a working build was static.


def _write_node_spec(workdir, tables=None):
    """A `.coder/project.json` that says this project is a Node one.

    The stack lives under the `stack` key, which is where `ProjectSpec.load`
    reads it from — writing `language`/`backend` at the top level loads as a
    spec with no stack at all, i.e. silently as Flask.
    """
    _write_spec(workdir, tables or {})
    path = Path(workdir) / ".coder" / "project.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["stack"] = {"language": "node", "backend": "express"}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_context_takes_its_stack_from_the_project_that_was_built(tmp_path):
    _write_node_spec(tmp_path)
    assert _wctx(tmp_path).adapter.key == "node"


def test_a_project_with_no_spec_is_flask(tmp_path):
    """Total, like `get_adapter`. Every existing single-stack task depends on
    this: they have no spec until the build writes one."""
    assert _wctx(tmp_path).adapter.key == "flask"


def test_the_context_ignores_the_session_setting(tmp_path, monkeypatch):
    """THE rule, and the same one `stacks.resolve_key` enforces everywhere else.

    A check that trusted `settings.web_stack` would look for `app.py` in a Node
    project the moment someone ran the suite with the default still on Flask,
    and report "this build is static" about an Express app that works.
    """
    from config.settings import settings

    monkeypatch.setattr(settings, "web_stack", "node")
    assert _wctx(tmp_path).adapter.key == "flask"  # no spec: the PROJECT decides

    _write_node_spec(tmp_path)
    monkeypatch.setattr(settings, "web_stack", "flask")
    assert _wctx(tmp_path).adapter.key == "node"


def test_a_node_build_is_a_full_stack_app(tmp_path):
    """The check that used to report every Express build as static HTML."""
    _write_node_spec(tmp_path)
    (tmp_path / "server.js").write_text(
        'const express = require("express");\n'
        "const app = express();\n"
        'app.get("/", (req, res) => res.render("index"));\n',
        encoding="utf-8",
    )
    (tmp_path / "views").mkdir()

    ok, detail = is_full_stack_app()(_wctx(tmp_path))

    assert ok is True, detail
    assert "express" in detail


def test_a_node_build_with_no_routes_is_not_a_full_stack_app(tmp_path):
    """Routes come from `adapter.routes_from_source` now — the same parser the
    agent uses — so this check and the project's own memory cannot disagree."""
    _write_node_spec(tmp_path)
    (tmp_path / "server.js").write_text(
        'const express = require("express");\nconst app = express();\n',
        encoding="utf-8",
    )
    (tmp_path / "views").mkdir()

    ok, detail = is_full_stack_app()(_wctx(tmp_path))

    assert ok is False and "no route" in detail


def test_a_static_node_build_names_the_file_it_wanted(tmp_path):
    _write_node_spec(tmp_path)
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")

    ok, detail = is_full_stack_app()(_wctx(tmp_path))

    assert ok is False
    assert "server.js" in detail and "static" in detail


def test_the_schema_check_reads_the_database_through_the_adapter(tmp_path):
    """Flask still reads the sqlite file — byte-identical behaviour — but now via
    the seam, so the Node adapter can answer the same question of PostgreSQL."""
    from app.agent.stacks.flask_adapter import FLASK

    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    found = FLASK.table_columns(tmp_path)
    assert found is not None
    assert found["recipes"] == {"id", "title"}


def test_no_database_is_not_the_same_answer_as_no_tables(tmp_path):
    """None means "could not read". Reporting that as an empty schema would turn
    an environment problem into "the build created no tables"."""
    from app.agent.stacks.flask_adapter import FLASK

    assert FLASK.table_columns(tmp_path) is None

    ok, detail = every_entity_has_a_table()(_wctx(tmp_path))
    assert ok is False

    _write_spec(tmp_path, {"recipe": ("recipes", ["title"])})
    ok, detail = every_entity_has_a_table()(_wctx(tmp_path))
    assert ok is False and "could not read the database" in detail


def test_a_blocked_environment_fails_the_check_and_names_the_fix(tmp_path, monkeypatch):
    """W10's rule, not a softening of it: a check that could not run still FAILS
    — a suite scoring well without starting anything is worthless — but it says
    which one command fixes it, exactly as a missing browser does."""
    from app.agent.stacks.node_adapter import NODE

    _write_node_spec(tmp_path, {"recipe": ("recipes", ["title"])})
    (tmp_path / "server.js").write_text("// built\n", encoding="utf-8")
    monkeypatch.setattr(
        NODE, "readiness", lambda root: "`node_modules` is missing — run `npm install`"
    )

    ok, detail = app_serves(["/"])(_wctx(tmp_path))

    assert ok is False
    assert "npm install" in detail


def test_flask_never_pays_for_the_readiness_gate(tmp_path):
    """Flask returns "" always, so nothing that used to run is now gated."""
    from app.agent.stacks.flask_adapter import FLASK

    assert FLASK.readiness(tmp_path) == ""


# --- the prepare hook -------------------------------------------------------


async def test_prepare_runs_before_the_checks_and_is_reported(tmp_path, monkeypatch):
    """`npm install` is the one thing a generated project may need that Coder
    deliberately will not do. A run that installed nothing must say so."""
    seen = []

    async def fake_chat(prompt, **kw):
        Path("built.txt").write_text("x", encoding="utf-8")
        return "done", []

    agent = SimpleNamespace(chat=fake_chat)
    task = EvalTask(id="t", prompt="build", checks=[file_exists("built.txt")])

    result = await run_task(
        agent,
        task,
        workdir=tmp_path / "t",
        prepare=lambda w: seen.append(w) or "npm install completed",
    )

    assert result.passed is True
    assert seen == [tmp_path / "t"]
    assert any("npm install completed" in d for d in result.details)


async def test_a_failing_prepare_never_aborts_the_task(tmp_path):
    async def fake_chat(prompt, **kw):
        Path("built.txt").write_text("x", encoding="utf-8")
        return "done", []

    def explode(workdir):
        raise RuntimeError("npm exploded")

    agent = SimpleNamespace(chat=fake_chat)
    task = EvalTask(id="t", prompt="build", checks=[file_exists("built.txt")])

    result = await run_task(agent, task, workdir=tmp_path / "t", prepare=explode)

    assert result.passed is True  # the build was still measured
    assert any("npm exploded" in d for d in result.details)


def test_the_installer_declines_a_project_with_nothing_to_install(tmp_path):
    from evals.run import _npm_installer

    assert "no package.json" in _npm_installer()(tmp_path)
