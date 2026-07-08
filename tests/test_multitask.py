"""Tests for multi-task decomposition and routing (M1, M2, M4, M6).

All offline: the LLM is a scripted fake, file writes go to tmp_path.
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.agent.core import AgentCore, _split_compound
from app.agent.planner import Planner
from app.agent.tool_registry import ToolDefinition


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# _split_compound — the cheap decomposition heuristic (M1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("create index.html, add a login form, and write a README", 3),
        ("update the readme and then commit the changes", 2),
        ("create a.py, then create b.py", 2),
        ("First create notes.md, then create todo.md", 2),
        ("make alpha.py; make beta.py; make gamma.py", 3),
        ("build the model, also write a test for it", 2),
    ],
)
def test_split_compound_splits(msg, expected):
    assert len(_split_compound(msg)) == expected


@pytest.mark.parametrize(
    "msg",
    [
        # relative clause with a bare "and" — must NOT split
        "write a function that adds a and b",
        # ordinary single-file create
        "create an index.html file for a landing page",
        # a noun list after one imperative is one task
        "make a website with a navbar, footer and hero section",
        "explain what a decorator does",
        "create a class that represents a point, with x and y coordinates",
    ],
)
def test_split_compound_keeps_single(msg):
    assert _split_compound(msg) == [msg]


def test_split_compound_numbered_list():
    tasks = _split_compound("Do these: 1. create a.py 2. create b.py 3. run the tests")
    assert tasks == ["create a.py", "create b.py", "run the tests"]


def test_split_compound_bulleted_list():
    tasks = _split_compound("- create a.py\n- create b.py")
    assert tasks == ["create a.py", "create b.py"]


def test_split_compound_drops_noun_leadin():
    # a non-imperative lead-in before the enumeration is dropped as prose
    tasks = _split_compound("please do the following, create a.py, and create b.py")
    assert tasks == ["create a.py", "create b.py"]


def test_split_compound_empty():
    assert _split_compound("") == [""]


# ---------------------------------------------------------------------------
# Planner.decompose — the robust (LLM) pass (M1)
# ---------------------------------------------------------------------------


def test_planner_decompose_returns_ordered_steps():
    p = Planner()
    p._llm_plan = ScriptedLLM(
        [
            '{"task_type": "multi_step", "steps": ['
            '{"step_description": "create a.py"},'
            '{"step_description": "create b.py"}]}'
        ]
    )
    assert p.decompose("build a thing") == ["create a.py", "create b.py"]


def test_planner_decompose_single_step_returns_empty():
    # A one-step plan is not a decomposition — signal "route as one task".
    p = Planner()
    p._llm_plan = ScriptedLLM(['{"steps": [{"step_description": "only one"}]}'])
    assert p.decompose("x") == []


def test_planner_decompose_bad_json_returns_empty():
    p = Planner()
    p._llm_plan = ScriptedLLM(["not json at all"])
    assert p.decompose("x") == []


# ---------------------------------------------------------------------------
# split_tasks public accessor (M6)
# ---------------------------------------------------------------------------


def test_split_tasks_strips_at_refs():
    a = AgentCore(session_id="pytest_splitpub")
    tasks = a.split_tasks("edit @a.py to add logging, and create b.py")
    assert tasks == ["edit a.py to add logging", "create b.py"]


# ---------------------------------------------------------------------------
# chat() decomposes and runs EVERY sub-task (M1)
# ---------------------------------------------------------------------------


async def test_chat_runs_each_subtask(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_multitask")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(["FILENAME: a.py\nx = 1\n", "FILENAME: b.py\ny = 2\n"])

    answer, trace = await a.chat("create a.py with x, and create b.py with y")

    assert (tmp_path / "a.py").is_file()
    assert (tmp_path / "b.py").is_file()
    assert "Completed 2 tasks" in answer
    writes = [t for t in trace if t["tool"] == "write_file"]
    assert len(writes) == 2


async def test_chat_uses_planner_decompose_for_multistep(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_plannerdecomp")
    # Cheap split sees ONE task (no separators), but classify says multi_step,
    # so the planner's decomposition drives the two sub-tasks.
    monkeypatch.setattr(a.planner, "classify", lambda msg: "multi_step")
    monkeypatch.setattr(
        a.planner,
        "decompose",
        lambda msg: ["create a.py with x", "create b.py with y"],
    )
    a._llm_direct = ScriptedLLM(["FILENAME: a.py\nx = 1\n", "FILENAME: b.py\ny = 2\n"])

    answer, trace = await a.chat("scaffold a tiny two-module package")

    assert (tmp_path / "a.py").is_file()
    assert (tmp_path / "b.py").is_file()
    assert "Completed 2 tasks" in answer


async def test_chat_single_task_unchanged(tmp_path, monkeypatch):
    # A non-compound request must still route through a single flow (no split).
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_single_unchanged")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html></html>"])

    answer, trace = await a.chat("create an index.html file for a landing page")

    assert (tmp_path / "index.html").is_file()
    assert "Completed" not in answer  # not the multi-task summary header
    assert "Created" in answer


# ---------------------------------------------------------------------------
# _run_subtasks internals: per-task @ref filtering + threaded context (M1)
# ---------------------------------------------------------------------------


async def test_run_subtasks_filters_refs_per_task(monkeypatch):
    a = AgentCore(session_id="pytest_subref")
    seen: list[tuple[str, list[str]]] = []

    async def fake_route(
        message, at_refs, task_type=None, extra_context="", on_token=None
    ):
        seen.append((message, at_refs))
        return f"did {message}", []

    monkeypatch.setattr(a, "_route_one", fake_route)

    answer, trace = await a._run_subtasks(
        ["edit a.py to add logging", "create b.py"], at_refs=["a.py"]
    )

    assert seen[0] == ("edit a.py to add logging", ["a.py"])
    assert seen[1] == ("create b.py", [])  # a.py not named here → not applied
    assert "Completed 2 tasks" in answer


async def test_run_subtasks_threads_prior_context(monkeypatch):
    a = AgentCore(session_id="pytest_thread")
    seen_extra: list[str] = []

    async def fake_route(
        message, at_refs, task_type=None, extra_context="", on_token=None
    ):
        seen_extra.append(extra_context)
        return f"ok {message}", []

    monkeypatch.setattr(a, "_route_one", fake_route)

    await a._run_subtasks(["create a.py", "create b.py"], at_refs=[])

    assert seen_extra[0] == ""  # the first task has no prior context
    assert "Already done" in seen_extra[1]  # the second sees a summary
    assert "create a.py" in seen_extra[1]


# ---------------------------------------------------------------------------
# M2 — multi_step tool loop runs without a loaded project
# ---------------------------------------------------------------------------


async def test_route_one_multistep_runs_tool_loop_without_project(monkeypatch):
    a = AgentCore(session_id="pytest_m2")
    assert a.project_path is None
    called: dict = {}

    async def fake_build(message, extra_context="", include_tool_protocol=True):
        return ["built-messages"]

    async def fake_loop(messages, max_steps=None):
        called["messages"] = messages
        return "loop done", []

    monkeypatch.setattr(a, "_build_messages", fake_build)
    monkeypatch.setattr(a, "_run_tool_loop", fake_loop)

    answer, trace = await a._route_one(
        "do a multi-step thing", at_refs=[], task_type="multi_step"
    )

    assert called["messages"] == ["built-messages"]
    assert answer == "loop done"


# ---------------------------------------------------------------------------
# M4 — tool loop honors settings.max_tool_steps and reports partial completion
# ---------------------------------------------------------------------------


async def test_tool_loop_respects_settings_max_steps(monkeypatch):
    from config.settings import settings

    a = AgentCore(session_id="pytest_m4")

    def ok():
        return {"success": True, "result": "ok", "error": None}

    a.registry.register(
        ToolDefinition(
            name="ok",
            description="always succeeds",
            parameters={"type": "object", "properties": {}},
            source="builtin",
            handler=ok,
        )
    )

    class LoopLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{"name": "ok", "args": {}, "id": "c", "type": "tool_call"}],
            )

    a._llm = LoopLLM()
    monkeypatch.setattr(settings, "max_tool_steps", 3)

    answer, trace = await a._run_tool_loop(messages=[])

    assert len(trace) == 3  # capped at settings.max_tool_steps
    assert "Stopped after 3" in answer
