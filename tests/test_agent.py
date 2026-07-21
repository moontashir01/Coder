"""Tests for the agent layer: tool registry, executor, planner, agent loop.

The LLM is always a scripted fake — Ollama is never contacted.
"""

from types import SimpleNamespace

import pytest

from app.agent import planner as planner_mod
from app.agent.executor import Executor
from app.agent.planner import Planner, _extract_json
from app.agent.tool_registry import (ToolDefinition, ToolRegistry,
                                     create_registry)

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


def test_mcp_tool_cannot_shadow_builtin():
    """An MCP server exposing write_file/read_file must not take over the name.

    Regression: @modelcontextprotocol/server-filesystem advertises read_file,
    write_file, edit_file, list_directory and search_files. Registering those
    over the builtins meant a later disconnect (unregister_by_source) deleted
    them outright, and every file flow died with "Tool not found: 'write_file'".
    """
    reg = create_registry()

    mcp_write = _echo_tool()
    mcp_write.name = "write_file"
    mcp_write.source = "mcp:filesystem"
    alias = reg.register(mcp_write)

    assert alias == "filesystem_write_file"
    assert reg.get("write_file").source == "builtin"

    reg.unregister_by_source("mcp:filesystem")
    assert reg.get("write_file").source == "builtin"
    assert "filesystem_write_file" not in reg.names()


def test_registry_alias_collision_gets_suffixed():
    reg = create_registry()

    def _mcp(source: str):
        t = _echo_tool()
        t.name = "write_file"
        t.source = source
        return reg.register(t)

    # Re-registering the same server reuses its own alias, it doesn't pile up.
    assert _mcp("mcp:filesystem") == "filesystem_write_file"
    assert _mcp("mcp:filesystem") == "filesystem_write_file"
    # A different server with the same tool name gets its own namespace.
    assert _mcp("mcp:other") == "other_write_file"


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
# AgentCore tool loop — native function calling (bind_tools + AIMessage.tool_calls)
# ---------------------------------------------------------------------------


from langchain_core.messages import AIMessage, ToolMessage


class ToolCallingLLM:
    """Scripted fake for the native tool-calling loop.

    `outputs` is a list of AIMessage objects; `.tool_calls` on each drives the
    loop. bind_tools records what was bound and returns self, like the real
    ChatOllama API.
    """

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.bound_tools = None
        self.seen_messages: list[list] = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.seen_messages.append(list(messages))
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return out


def _tc(name, args, call_id="call_1"):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@pytest.fixture
def agent():
    from app.agent.core import AgentCore

    a = AgentCore(session_id="pytest_session")
    a.registry.register(_echo_tool())
    return a


async def test_agent_tool_loop_runs_tool_then_answers(agent):
    agent._llm = ToolCallingLLM(
        [
            AIMessage(content="", tool_calls=[_tc("echo", {"text": "hi"})]),
            AIMessage(content="all done"),
        ]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "all done"
    assert len(trace) == 1
    assert trace[0]["tool"] == "echo"
    assert trace[0]["result"]["result"] == "echo:hi"


async def test_agent_tool_loop_direct_final_answer(agent):
    agent._llm = ToolCallingLLM([AIMessage(content="quick")])
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "quick"
    assert trace == []


async def test_tool_loop_binds_registry_tools(agent):
    llm = ToolCallingLLM([AIMessage(content="ok")])
    agent._llm = llm
    await agent._run_tool_loop(messages=[])
    bound_names = {t["function"]["name"] for t in llm.bound_tools}
    assert "echo" in bound_names
    assert "write_file" in bound_names


async def test_tool_loop_feeds_back_tool_message_with_call_id(agent):
    llm = ToolCallingLLM(
        [
            AIMessage(content="", tool_calls=[_tc("echo", {"text": "hi"}, "call_42")]),
            AIMessage(content="done"),
        ]
    )
    agent._llm = llm
    await agent._run_tool_loop(messages=[])
    # 2nd invoke must see the assistant tool-call message + a paired ToolMessage
    second = llm.seen_messages[1]
    tool_msgs = [m for m in second if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_42"
    assert "echo:hi" in tool_msgs[0].content


async def test_tool_loop_executes_multiple_calls_in_one_turn(agent):
    agent._llm = ToolCallingLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc("echo", {"text": "a"}, "call_a"),
                    _tc("echo", {"text": "b"}, "call_b"),
                ],
            ),
            AIMessage(content="both done"),
        ]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "both done"
    assert [t["result"]["result"] for t in trace] == ["echo:a", "echo:b"]


async def test_tool_loop_corrects_hallucinated_tool(agent):
    llm = ToolCallingLLM(
        [
            AIMessage(content="", tool_calls=[_tc("banana", {})]),
            AIMessage(content="answered directly"),
        ]
    )
    agent._llm = llm
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "answered directly"
    # firm correction listing the real tools went back to the model
    correction = [m for m in llm.seen_messages[1] if isinstance(m, ToolMessage)][0]
    assert "NOT a real tool" in correction.content
    assert "write_file" in correction.content


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
    agent._llm = ToolCallingLLM(
        [
            AIMessage(content="", tool_calls=[_tc("boom", {})]),
            AIMessage(content="recovered"),
        ]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "recovered"
    assert trace[0]["result"]["success"] is False


async def test_tool_loop_gives_up_after_repeated_failures(agent):
    agent.registry.register(_failing_tool())
    # Model stubbornly calls the same failing tool every turn.
    agent._llm = ToolCallingLLM([AIMessage(content="", tool_calls=[_tc("boom", {})])])
    answer, trace = await agent._run_tool_loop(messages=[], max_steps=8)
    # Must bail out well before max_steps instead of looping 8 times.
    assert len(trace) <= 3
    assert "repeatedly" in answer.lower()
    assert "file_not_found" in answer


# ---------------------------------------------------------------------------
# Textual tool-call fallback (old Ollama servers, e.g. 0.31.x, never populate
# message.tool_calls — the model's tool JSON arrives as plain content)
# ---------------------------------------------------------------------------


def test_parse_textual_tool_call_bare_json():
    from app.agent.core import _parse_textual_tool_call

    call = _parse_textual_tool_call(
        '{"name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}'
    )
    assert call == {
        "name": "write_file",
        "args": {"path": "a.txt", "content": "hi"},
        "id": "",
        "type": "tool_call",
    }


def test_parse_textual_tool_call_fenced_json():
    from app.agent.core import _parse_textual_tool_call

    call = _parse_textual_tool_call(
        '```json\n{"name": "echo", "arguments": {"text": "x"}}\n```'
    )
    assert call is not None
    assert call["name"] == "echo"
    assert call["args"] == {"text": "x"}


@pytest.mark.parametrize(
    "text",
    [
        'Here is how you\'d do it: {"name": "write_file", "arguments": {}}',  # prose + JSON
        '{"name": "write_file"}',  # no arguments
        '{"name": "write_file", "arguments": "not a dict"}',
        '{"arguments": {"path": "x"}}',  # no name
        "just a plain answer",
        '["not", "an", "object"]',
    ],
)
def test_parse_textual_tool_call_rejects(text):
    from app.agent.core import _parse_textual_tool_call

    assert _parse_textual_tool_call(text) is None


async def test_tool_loop_falls_back_to_textual_tool_call(agent):
    agent._llm = ToolCallingLLM(
        [
            AIMessage(content='{"name": "echo", "arguments": {"text": "hi"}}'),
            AIMessage(content="done via fallback"),
        ]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "done via fallback"
    assert len(trace) == 1
    assert trace[0]["result"]["result"] == "echo:hi"


async def test_tool_loop_plain_prose_is_still_final_answer(agent):
    agent._llm = ToolCallingLLM(
        [AIMessage(content="The answer is 42, no tools needed.")]
    )
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "The answer is 42, no tools needed."
    assert trace == []
