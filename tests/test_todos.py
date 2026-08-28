"""The model's per-turn task list (app/agent/todos.py) — pure state, an
`update_todos` builtin, and the block the tool loop restates every round.

All offline: the parsing/rendering is pure, the handler is a closure over a
plain store, and the loop tests drive `_run_tool_loop` with the same scripted
`ToolCallingLLM` shape `tests/test_agent.py` uses.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.todos import (
    MAX_ITEMS,
    MAX_TEXT_CHARS,
    Todo,
    TodoStore,
    build_todo_tool,
    parse_items,
)
from config.settings import settings

# ---------------------------------------------------------------------------
# parse_items — tolerant on shape, strict on meaning
# ---------------------------------------------------------------------------


def test_parse_reads_dicts_with_text_and_status():
    items = parse_items(
        [
            {"text": "create app.py", "status": "done"},
            {"text": "write the test", "status": "working"},
            {"text": "run the test"},
        ]
    )
    assert [t.status for t in items] == ["done", "working", "todo"]
    assert items[0].text == "create app.py"


def test_parse_accepts_plain_strings_as_todo():
    items = parse_items(["step one", "step two"])
    assert [t.text for t in items] == ["step one", "step two"]
    assert all(t.status == "todo" for t in items)


def test_parse_normalizes_status_aliases_a_7b_actually_writes():
    items = parse_items(
        [
            {"text": "a", "status": "in_progress"},
            {"text": "b", "status": "COMPLETED"},
            {"text": "c", "status": "pending"},
        ]
    )
    assert [t.status for t in items] == ["working", "done", "todo"]


def test_parse_unknown_status_reads_as_todo_never_done():
    # The safe misreading is the one that makes the model FINISH the item.
    (item,) = parse_items([{"text": "x", "status": "banana"}])
    assert item.status == "todo"


def test_parse_garbage_is_none_not_empty():
    # None means "keep the old list"; [] means "the model cleared its list".
    assert parse_items("not a list") is None
    assert parse_items({"text": "x"}) is None
    assert parse_items([42, None]) is None  # non-empty input, nothing readable
    assert parse_items([]) == []


def test_parse_caps_count_and_text_length():
    items = parse_items([f"step {i}" for i in range(MAX_ITEMS + 10)])
    assert len(items) == MAX_ITEMS
    (long_item,) = parse_items(["x" * (MAX_TEXT_CHARS * 2)])
    assert len(long_item.text) == MAX_TEXT_CHARS


def test_parse_collapses_newlines_so_one_item_is_one_line():
    (item,) = parse_items(["line one\nline   two"])
    assert item.text == "line one line two"


# ---------------------------------------------------------------------------
# TodoStore — render + remaining
# ---------------------------------------------------------------------------


def test_render_shows_marks_and_progress_count():
    store = TodoStore()
    store.replace([Todo("a", "done"), Todo("b", "working"), Todo("c", "todo")])
    block = store.render()
    assert "CURRENT TASK LIST (1/3 done):" in block
    assert "[x] a" in block
    assert "[>] b" in block
    assert "[ ] c" in block


def test_render_is_empty_string_with_no_list():
    assert TodoStore().render() == ""


def test_remaining_excludes_done():
    store = TodoStore()
    store.replace([Todo("a", "done"), Todo("b", "working"), Todo("c")])
    assert store.remaining() == ["b", "c"]


# ---------------------------------------------------------------------------
# The update_todos tool — handler contract, and a slip never fails the tool
# ---------------------------------------------------------------------------


def test_handler_updates_store_and_returns_the_rendered_list():
    store = TodoStore()
    tool = build_todo_tool(store)
    out = tool.handler(todos=[{"text": "a", "status": "done"}, {"text": "b"}])
    assert out["success"] is True
    assert "CURRENT TASK LIST (1/2 done):" in out["result"]
    assert store.remaining() == ["b"]


def test_handler_malformed_update_keeps_state_and_still_succeeds():
    # A failed tool counts toward max_tool_failures and two slips would end the
    # whole turn — so a bad update REPORTS itself but never fails.
    store = TodoStore()
    store.replace([Todo("keep me")])
    tool = build_todo_tool(store)
    out = tool.handler(todos="garbage")
    assert out["success"] is True
    assert "unchanged" in out["result"]
    assert store.remaining() == ["keep me"]


def test_tool_has_no_permissions_so_no_gate_ever_fires():
    tool = build_todo_tool(TodoStore())
    assert tool.permissions == []
    assert tool.source == "builtin"


# ---------------------------------------------------------------------------
# AgentCore wiring — registration, per-turn reset, loop restatement
# ---------------------------------------------------------------------------


class ToolCallingLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
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

    return AgentCore(session_id="pytest_todos")


def test_update_todos_is_registered_per_core(agent):
    tool = agent.registry.get("update_todos")
    assert tool.permissions == []
    # The handler is bound to THIS core's store, not a shared one.
    tool.handler(todos=["only mine"])
    assert agent._todos.remaining() == ["only mine"]


def test_setting_off_skips_registration(monkeypatch):
    monkeypatch.setattr(settings, "todo_tool", False)
    from app.agent.core import AgentCore

    a = AgentCore(session_id="pytest_todos_off")
    assert "update_todos" not in a.registry.names()


async def test_loop_restates_the_list_after_a_later_tool_result(agent):
    # Round 1 sets the list; round 2 runs a real tool; the block must ride
    # round 2's ToolMessage so round 3 still sees what remains.
    llm = ToolCallingLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc(
                        "update_todos",
                        {
                            "todos": [
                                {"text": "read x", "status": "working"},
                                {"text": "edit y"},
                            ]
                        },
                        "c1",
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[_tc("list_directory", {"path": "."}, "c2")],
            ),
            AIMessage(content="done"),
        ]
    )
    agent._llm = llm
    answer, trace = await agent._run_tool_loop(messages=[])
    assert answer == "done"
    third = llm.seen_messages[2]
    last_tool_msg = [m for m in third if isinstance(m, ToolMessage)][-1]
    assert "CURRENT TASK LIST" in last_tool_msg.content
    assert "[>] read x" in last_tool_msg.content
    assert "[ ] edit y" in last_tool_msg.content


async def test_update_todos_round_does_not_state_the_list_twice(agent):
    llm = ToolCallingLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_tc("update_todos", {"todos": ["a"]}, "c1")],
            ),
            AIMessage(content="done"),
        ]
    )
    agent._llm = llm
    await agent._run_tool_loop(messages=[])
    second = llm.seen_messages[1]
    (tool_msg,) = [m for m in second if isinstance(m, ToolMessage)]
    assert tool_msg.content.count("CURRENT TASK LIST") == 1


async def test_max_steps_report_names_the_unfinished_tasks(agent):
    llm = ToolCallingLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc(
                        "update_todos",
                        {
                            "todos": [
                                {"text": "a", "status": "done"},
                                {"text": "write seed.py"},
                            ]
                        },
                        "c1",
                    )
                ],
            ),
        ]
    )
    agent._llm = llm
    answer, _ = await agent._run_tool_loop(messages=[], max_steps=1)
    assert "Stopped after 1" in answer
    assert "write seed.py" in answer
    assert "a;" not in answer  # done items are not reported as unfinished


def test_the_list_is_per_turn_state():
    # chat() clears the store at the top of every turn; the store-level
    # behaviour that rule depends on.
    store = TodoStore()
    store.replace([Todo("stale", "working")])
    store.clear()
    assert store.render() == ""
    assert store.remaining() == []
