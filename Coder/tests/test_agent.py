"""Tests for the agent layer: tool registry, executor, planner, agent loop.

The LLM is always a scripted fake — Ollama is never contacted.
"""

from types import SimpleNamespace

import pytest

from app.agent.tool_registry import ToolDefinition, ToolRegistry, create_registry
from app.agent.executor import Executor
from app.agent import planner as planner_mod
from app.agent.planner import Planner, _extract_json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Returns the next canned string each time `.invoke` is called."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


def _echo_tool():
    def echo(text: str):
        return {"success": True, "result": f"echo:{text}", "error": None}

    return ToolDefinition(
        name="echo",
        description="Echo text back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        source="builtin",
        handler=echo,
    )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


def test_registry_register_and_get():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    assert reg.get("echo").name == "echo"
    assert "echo" in reg.names()


def test_registry_get_unknown_raises():
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.get("missing")


def test_registry_list_and_unregister_by_source():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    mcp_tool = _echo_tool()
    mcp_tool.name = "remote"
    mcp_tool.source = "mcp:server1"
    reg.register(mcp_tool)

    assert len(reg.list_all()) == 2
    assert len(reg.list_by_source("mcp:")) == 1

    removed = reg.unregister_by_source("mcp:")
    assert removed == 1
    assert len(reg.list_all()) == 1
    assert reg.get("echo")  # builtin survives


def test_create_registry_has_all_builtins():
    reg = create_registry()
    names = set(reg.names())
    expected = {
        "read_file",
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "list_directory",
        "search_files",
        "run_command",
        "git_status",
        "git_diff",
        "git_commit",
        "git_log",
    }
    assert expected.issubset(names)
    assert len(names) >= 12


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@pytest.fixture
def exec_registry():
    reg = ToolRegistry()

    def add(a: int, b: int):
        return {"success": True, "result": str(a + b), "error": None}

    reg.register(
        ToolDefinition(
            name="add",
            description="Add two integers.",
            parameters={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
            source="builtin",
            handler=add,
        )
    )

    async def aecho(x: str):
        return {"success": True, "result": x, "error": None}

    reg.register(
        ToolDefinition(
            name="aecho",
            description="Async echo.",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            source="builtin",
            handler=aecho,
        )
    )
    return reg


async def test_executor_sync_handler(exec_registry):
    ex = Executor(exec_registry)
    res = await ex.execute("add", {"a": 2, "b": 3})
    assert res["success"] is True
    assert res["result"] == "5"


async def test_executor_async_handler(exec_registry):
    ex = Executor(exec_registry)
    res = await ex.execute("aecho", {"x": "hi"})
    assert res["success"] is True
    assert res["result"] == "hi"


async def test_executor_missing_argument(exec_registry):
    ex = Executor(exec_registry)
    res = await ex.execute("add", {"a": 1})
    assert res["success"] is False
    assert "missing" in res["error"].lower()


async def test_executor_type_mismatch(exec_registry):
    ex = Executor(exec_registry)
    res = await ex.execute("add", {"a": "x", "b": 2})
    assert res["success"] is False
    assert "validation" in res["error"].lower()


async def test_executor_unknown_tool(exec_registry):
    ex = Executor(exec_registry)
    res = await ex.execute("nope", {})
    assert res["success"] is False
    assert res["error"]


# ---------------------------------------------------------------------------
# Planner JSON extraction + classify/plan with a fake LLM
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_markdown_fence():
    assert _extract_json('```json\n{"task_type": "simple_qa"}\n```') == {
        "task_type": "simple_qa"
    }


def test_extract_json_embedded_in_text():
    assert _extract_json('Here you go: {"x": 2} done') == {"x": 2}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        _extract_json("no json here")


def test_planner_classify(monkeypatch):
    p = Planner()
    p._llm_fast = ScriptedLLM(['{"task_type": "code_generation"}'])
    assert p.classify("write a function") == "code_generation"


def test_planner_classify_invalid_falls_back():
    p = Planner()
    p._llm_fast = ScriptedLLM(['{"task_type": "banana"}'])
    assert p.classify("x") == "simple_qa"


def test_planner_plan_simple_single_step():
    p = Planner()
    p._llm_fast = ScriptedLLM(['{"task_type": "simple_qa"}'])
    plan = p.plan("what is python?")
    assert plan["task_type"] == "simple_qa"
    assert len(plan["steps"]) == 1


def test_planner_plan_multi_step():
    p = Planner()
    p._llm_fast = ScriptedLLM(['{"task_type": "multi_step"}'])
    p._llm_plan = ScriptedLLM(
        [
            '{"task_type": "multi_step", "steps": ['
            '{"step_description": "read", "suggested_tool": "read_file", "expected_output": "contents"},'
            '{"step_description": "edit", "suggested_tool": "edit_file", "expected_output": "patched"}]}'
        ]
    )
    plan = p.plan("refactor file then run tests")
    assert plan["task_type"] == "multi_step"
    assert len(plan["steps"]) == 2
    assert plan["steps"][0]["suggested_tool"] == "read_file"


# ---------------------------------------------------------------------------
# AgentCore tool loop (full ReAct cycle with scripted LLM)
# ---------------------------------------------------------------------------


@pytest.fixture
def agent():
    from app.agent.core import AgentCore

    a = AgentCore(session_id="pytest_session")
    a.registry.register(_echo_tool())
    return a


async def test_agent_tool_loop_runs_tool_then_answers(agent):
    agent._llm = ScriptedLLM(
        [
            '{"action": "tool_call", "tool": "echo", "arguments": {"text": "hi"}}',
            '{"action": "final_answer", "answer": "all done"}',
        ]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "all done"
    assert len(trace) == 1
    assert trace[0]["tool"] == "echo"
    assert trace[0]["result"]["result"] == "echo:hi"


async def test_agent_tool_loop_direct_final_answer(agent):
    agent._llm = ScriptedLLM(['{"action": "final_answer", "answer": "quick"}'])
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "quick"
    assert trace == []


async def test_agent_tool_loop_handles_unparseable_output(agent):
    agent._llm = ScriptedLLM(["not json", "still not", "nope", "garbage"])
    answer, trace = await agent._run_tool_loop(messages=[])
    assert "could not parse" in answer.lower()


def test_agent_parse_action(agent):
    assert (
        agent._parse_action('{"action": "final_answer", "answer": "x"}')["action"]
        == "final_answer"
    )
    assert agent._parse_action("garbage") is None


# ---------------------------------------------------------------------------
# §10 Tool metadata
# ---------------------------------------------------------------------------


def test_tool_definition_metadata_defaults():
    t = _echo_tool()
    assert t.timeout is None
    assert t.output_schema is None
    assert t.permissions == []
    assert t.error_hints is None


def test_tool_definition_accepts_metadata():
    t = ToolDefinition(
        name="x",
        description="d",
        parameters={"type": "object", "properties": {}},
        source="builtin",
        handler=lambda: None,
        timeout=10,
        output_schema={"type": "object"},
        permissions=["fs:write"],
        error_hints="Pass an absolute path.",
    )
    assert t.timeout == 10
    assert t.output_schema == {"type": "object"}
    assert t.permissions == ["fs:write"]
    assert t.error_hints == "Pass an absolute path."


# ---------------------------------------------------------------------------
# §11 Error classification + recovery hints
# ---------------------------------------------------------------------------


def test_classify_error_categories():
    from app.agent.recovery import classify_error

    assert classify_error("Tool not found: 'foo'") == "not_found_tool"
    assert (
        classify_error(
            "Argument validation failed: Missing required arguments: ['path']"
        )
        == "invalid_args"
    )
    assert (
        classify_error(
            "Tool execution error: [Errno 2] No such file or directory: 'x.py'"
        )
        == "file_not_found"
    )
    assert (
        classify_error("PermissionError: [Errno 13] Permission denied")
        == "permission_denied"
    )
    assert classify_error("Command timed out after 30s") == "timeout"
    assert classify_error("kaboom unexpected") == "unknown"


def test_recovery_hint_names_tool_and_category():
    from app.agent.recovery import recovery_hint

    hint = recovery_hint(
        "read_file", "Tool execution error: [Errno 2] No such file or directory: 'x.py'"
    )
    assert "read_file" in hint
    assert "file_not_found" in hint


def test_recovery_hint_includes_tool_specific_hint():
    from app.agent.recovery import recovery_hint

    hint = recovery_hint("mytool", "kaboom", tool_error_hints="Try an absolute path.")
    assert "Try an absolute path." in hint


# ---------------------------------------------------------------------------
# §11 Tool loop recovery integration
# ---------------------------------------------------------------------------


def _failing_tool():
    def boom():
        return {
            "success": False,
            "result": "",
            "error": "Tool execution error: [Errno 2] No such file or directory: 'x.py'",
        }

    return ToolDefinition(
        name="boom",
        description="Always fails.",
        parameters={"type": "object", "properties": {}},
        source="builtin",
        handler=boom,
    )


async def test_tool_loop_recovers_after_single_failure(agent):
    agent.registry.register(_failing_tool())
    agent._llm = ScriptedLLM(
        [
            '{"action": "tool_call", "tool": "boom", "arguments": {}}',
            '{"action": "final_answer", "answer": "recovered"}',
        ]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "recovered"
    assert trace[0]["result"]["success"] is False


async def test_tool_loop_gives_up_after_repeated_failures(agent):
    agent.registry.register(_failing_tool())
    # Model stubbornly calls the same failing tool every turn.
    agent._llm = ScriptedLLM(
        ['{"action": "tool_call", "tool": "boom", "arguments": {}}']
    )
    answer, trace = await agent._run_tool_loop(messages=[], max_steps=8)
    # Must bail out well before max_steps instead of looping 8 times.
    assert len(trace) <= 3
    assert "repeatedly" in answer.lower()
    assert "file_not_found" in answer
