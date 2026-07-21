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


class RecordingLLM(ScriptedLLM):
    """ScriptedLLM that also records the full prompt text of every call."""

    def __init__(self, outputs):
        super().__init__(outputs)
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(getattr(m, "content", m)) for m in messages))
        return super().invoke(messages)


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


def test_split_compound_heading_labels_do_not_split():
    """A numbered feature/spec list ("1. Search Bar: …") describes ONE build,
    not many tasks — even when a label starts with a verb-lookalike ("Search").
    Regression: a weather-dashboard spec was severed from its build sentence."""
    msg = (
        "Build a weather dashboard in three separate files "
        "(index.html, styles.css, script.js).\n"
        "1. Search Bar: Input to search for a city.\n"
        "2. Current Weather: Displays city name and temperature.\n"
        "3. Dark Mode: A toggle button to switch themes.\n"
        "Provide complete code for all files."
    )
    assert _split_compound(msg) == [msg]


def test_split_compound_uppercase_imperatives_still_split():
    # Title-case IMPERATIVES (no colon) must still open new tasks.
    tasks = _split_compound(
        "1. Create alpha.py with a hello function 2. Create beta.py importing alpha"
    )
    assert len(tasks) == 2


# ---------------------------------------------------------------------------
# _looks_multipart — the gate for "spend an LLM planning call?" (M1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        # natural language, no explicit "then"/"also" separators
        "create a website with a login page. it redirects to the homepage. add a logout button.",
        "build the backend api and write unit tests for it",
        "generate a react app. add routing. set up a theme.",
    ],
)
def test_looks_multipart_true(msg):
    from app.agent.core import _looks_multipart

    assert _looks_multipart(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "create an index.html file for a landing page",
        "write a python function that adds two numbers",
        "explain what a decorator does",
        "make a website with a navbar, footer and hero section",
    ],
)
def test_looks_multipart_false(msg):
    from app.agent.core import _looks_multipart

    assert _looks_multipart(msg) is False


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


async def test_chat_plans_natural_language_multipart(tmp_path, monkeypatch):
    # A plain-prose multi-part build (no "then"/"also", not classified
    # multi_step) is decomposed by the LLM planner and every file gets written.
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_nl_plan")
    monkeypatch.setattr(a.planner, "classify", lambda m: "code_generation")
    monkeypatch.setattr(
        a.planner,
        "decompose",
        lambda m: [
            "Create login.html: a login form",
            "Create home.html: homepage linking login.html",
        ],
    )
    a._llm_direct = ScriptedLLM(
        [
            "FILENAME: login.html\n<html><body>login</body></html>",
            "FILENAME: home.html\n<html><body>home</body></html>",
        ]
    )

    answer, trace = await a.chat(
        "create a login page. it should show a homepage. add nice styling."
    )

    assert (tmp_path / "login.html").is_file()
    assert (tmp_path / "home.html").is_file()
    assert "Completed 2 tasks" in answer


async def test_chat_simple_create_does_not_plan(tmp_path, monkeypatch):
    # A single-file create must NOT trigger the LLM planner (no decompose call).
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_no_plan")
    monkeypatch.setattr(a.planner, "classify", lambda m: "code_generation")

    def _boom(msg):  # decompose must not be called for a simple create
        raise AssertionError("decompose should not run for a single-file create")

    monkeypatch.setattr(a.planner, "decompose", _boom)
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html></html>"])

    answer, trace = await a.chat("create an index.html file for a landing page")

    assert (tmp_path / "index.html").is_file()
    assert "Completed" not in answer


async def test_chat_multifile_spec_goes_whole_to_multi_file_flow(tmp_path, monkeypatch):
    """An explicit multi-file build reaches _multi_file_flow as ONE whole
    message (severing the spec list loses features) and skips the classify and
    decompose LLM calls — the multi-file flow has its own per-file planner."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_mf_whole")

    def _boom(msg):
        raise AssertionError("classify/decompose must not run for a multi-file spec")

    monkeypatch.setattr(a.planner, "classify", _boom)
    monkeypatch.setattr(a.planner, "decompose", _boom)

    seen = {}

    async def fake_mf(message, refs, extra_context=""):
        seen["message"] = message
        return "handled", []

    monkeypatch.setattr(a, "_multi_file_flow", fake_mf)

    answer, trace = await a.chat(
        "Build a weather dashboard web app in three separate files "
        "(index.html, styles.css, script.js).\n"
        "1. Search Bar: Input to search for a city.\n"
        "2. Dark Mode: A toggle button to switch themes.\n"
        "Provide complete code for all files."
    )

    assert "Search Bar" in seen["message"]
    assert "Dark Mode" in seen["message"]
    assert answer == "handled"


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


async def test_run_subtasks_threads_manifest(monkeypatch):
    # Every sub-task sees the full plan manifest (so it knows what else is coming).
    a = AgentCore(session_id="pytest_thread")
    seen_extra: list[str] = []

    async def fake_route(
        message, at_refs, task_type=None, extra_context="", on_token=None
    ):
        seen_extra.append(extra_context)
        return f"ok {message}", []

    monkeypatch.setattr(a, "_route_one", fake_route)

    await a._run_subtasks(["create a.py", "create b.py"], at_refs=[])

    assert "Overall plan" in seen_extra[0]
    assert "create a.py" in seen_extra[0] and "create b.py" in seen_extra[0]
    assert "Overall plan" in seen_extra[1]


async def test_run_subtasks_threads_written_file_contents(tmp_path, monkeypatch):
    # The core Claude-Code-style behavior: a later task's generation prompt must
    # include the ACTUAL contents of a file an earlier task wrote, so links /
    # redirects / ids line up across files.
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_subtask_ctx")
    monkeypatch.setattr(a.planner, "classify", lambda m: "code_generation")
    a._llm_direct = RecordingLLM(
        [
            "FILENAME: login.html\n<html><body>LOGIN-MARKER</body></html>",
            "FILENAME: home.html\n<html><body>HOME</body></html>",
        ]
    )

    answer, trace = await a._run_subtasks(
        [
            "create login.html with a form",
            "create home.html that links back to login.html",
        ],
        at_refs=[],
    )

    assert (tmp_path / "login.html").is_file()
    assert (tmp_path / "home.html").is_file()
    # the SECOND generation call saw the first file's real content + name
    assert "LOGIN-MARKER" in a._llm_direct.prompts[1]
    assert "login.html" in a._llm_direct.prompts[1]
    assert "Completed 2 tasks" in answer


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
# Repair requests never dead-end on the tool-free _direct_answer
# ---------------------------------------------------------------------------


async def test_route_one_repair_request_escalates_to_tool_loop(monkeypatch):
    """"fix the navigation" names no file the gates recognize. It must reach the
    tool loop, not _direct_answer — that path carries no tools, so the model can
    only reply "please paste the file contents" (the reported failure)."""
    a = AgentCore(session_id="pytest_repair")

    async def fake_build(message, extra_context="", include_tool_protocol=True):
        assert include_tool_protocol is True
        return ["built-messages"]

    async def fake_loop(messages, max_steps=None):
        return "loop done", [{"tool": "read_file"}]

    async def fail_direct(*args, **kwargs):
        raise AssertionError("must not fall through to the tool-free path")

    monkeypatch.setattr(a, "_build_messages", fake_build)
    monkeypatch.setattr(a, "_run_tool_loop", fake_loop)
    monkeypatch.setattr(a, "_direct_answer", fail_direct)

    answer, trace = await a._route_one(
        "the checkout is broken, fix it", at_refs=[], task_type="simple_qa"
    )
    assert answer == "loop done"
    assert trace


async def test_route_one_plain_question_still_direct_answers(monkeypatch):
    """The escalation must not swallow ordinary Q&A."""
    a = AgentCore(session_id="pytest_repair_qa")

    async def fake_direct(message, extra_context="", on_token=None):
        return "prose"

    async def fail_loop(*args, **kwargs):
        raise AssertionError("a question must not run the tool loop")

    monkeypatch.setattr(a, "_direct_answer", fake_direct)
    monkeypatch.setattr(a, "_run_tool_loop", fail_loop)

    answer, trace = await a._route_one(
        "how do I fix a memory leak in python", at_refs=[], task_type="explanation"
    )
    assert answer == "prose"
    assert trace == []


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
