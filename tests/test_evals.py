"""Tests for the offline eval harness (roadmap Tier 2 #6).

The harness itself is exercised fully offline with a scripted LLM. The golden
suite's *live* run against Ollama is a separate manual invocation (evals/run.py)
and is NOT part of pytest.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.checks import (answer_contains, any_file_matches,
                          backend_defines_route, backend_reads_fields,
                          file_contains, file_excludes, file_exists,
                          frontend_calls_route, has_backend_server,
                          min_files_written, route_wired, used_tool)
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
    assert backend_reads_fields(["email", "password"])(_ctx(workdir=tmp_path))[0] is True
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
    from app.agent.blueprint import (ApiContract, Blueprint, Endpoint, Feature,
                                     PlannedFile)
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

    async def _fake_expand(msg):
        return bp

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(
        [
            'FILENAME: login.html\n<!doctype html>\n<html><body>'
            '<form id="login-form" onsubmit="fetch(\'/api/login\',{method:\'POST\'})">'
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
