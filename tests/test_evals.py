"""Tests for the offline eval harness (roadmap Tier 2 #6).

The harness itself is exercised fully offline with a scripted LLM. The golden
suite's *live* run against Ollama is a separate manual invocation (evals/run.py)
and is NOT part of pytest.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.checks import (answer_contains, file_contains, file_excludes,
                          file_exists, min_files_written, used_tool)
from evals.harness import CheckContext, EvalTask, run_suite, run_task
from evals.tasks import GOLDEN_TASKS


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
