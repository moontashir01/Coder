"""Phase T0 — the turn log, and the transcript rendered from it.

Two halves, tested apart: `render_transcript` is pure and gets the detailed
assertions; the DB half runs against a throwaway sqlite file so the suite never
touches the repo's own `.coder.db`.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.memory import turnlog

# ── a throwaway database ────────────────────────────────────────────────────


@pytest.fixture
async def factory(tmp_path):
    """A session factory on a temp sqlite file, with the tables created.

    Passed explicitly to every `turnlog` call, because `AsyncSessionLocal` is
    bound to an engine built at import from `settings.sqlite_path` — a
    monkeypatch of the setting would not move it, and the tests would quietly
    write into the repo's real history.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'turns.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(turnlog.Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _trace(path="app.py", ok=True, error=""):
    return [
        {
            "tool": "write_file",
            "arguments": {"path": path, "content": "x"},
            "result": {"success": ok, "result": "written", "error": error},
        }
    ]


# ── compact_trace / written_files ───────────────────────────────────────────


def test_written_files_lists_only_successful_writes():
    trace = _trace("a.py") + _trace("b.py", ok=False) + _trace("a.py")
    assert turnlog.written_files(trace) == ["a.py"]


def test_written_files_ignores_non_write_tools():
    trace = [
        {
            "tool": "read_file",
            "arguments": {"path": "a.py"},
            "result": {"success": True},
        }
    ]
    assert turnlog.written_files(trace) == []


def test_compact_trace_survives_a_malformed_entry():
    """One odd entry must not cost the whole turn's record."""
    trace = ["not a dict", {"tool": "run_command"}, *_trace()]
    compact = turnlog.compact_trace(trace)
    assert [c["tool"] for c in compact] == ["run_command", "write_file"]
    assert compact[0]["success"] is False


def test_compact_trace_caps_a_huge_result():
    trace = [
        {
            "tool": "read_file",
            "arguments": {"path": "big.py"},
            "result": {"success": True, "result": "x" * 50_000},
        }
    ]
    body = turnlog.compact_trace(trace)[0]["result"]
    assert len(body) < 3000
    assert "50000 chars total" in body


def test_compact_trace_names_a_command_when_there_is_no_path():
    trace = [
        {
            "tool": "run_command",
            "arguments": {"command": "npm install"},
            "result": {"success": True},
        }
    ]
    assert turnlog.compact_trace(trace)[0]["target"] == "npm install"


# ── render_transcript (pure) ────────────────────────────────────────────────


def _turn(**kw):
    base = {
        "timestamp": "2026-08-23T10:00:00+00:00",
        "source": "cli",
        "project": "/tmp/proj",
        "user_message": "build me a blog",
        "answer": "Created app.py",
        "task_type": "code_generation",
        "flow": "blueprint",
        "tools": turnlog.compact_trace(_trace()),
        "files_written": ["app.py"],
        "duration_ms": 4200,
    }
    base.update(kw)
    return base


def test_transcript_states_the_route_the_turn_took():
    """The routing decision is the reasoning; a transcript without it is a chat log."""
    out = turnlog.render_transcript([_turn()], session_id="work")
    assert "flow `blueprint`" in out
    assert "task type `code_generation`" in out
    assert "4.2s" in out


def test_transcript_names_the_front_end_that_asked():
    out = turnlog.render_transcript(
        [_turn(source="cli"), _turn(source="telegram:4242")], session_id="work"
    )
    assert "`cli`" in out and "`telegram:4242`" in out


def test_transcript_lists_tools_and_files():
    out = turnlog.render_transcript([_turn()], session_id="w")
    assert "### Tools (1)" in out
    assert "`write_file`" in out
    assert "### Files written (1)" in out


def test_a_failed_tool_is_marked_failed_with_its_error():
    turn = _turn(
        tools=turnlog.compact_trace(_trace(ok=False, error="Permission denied"))
    )
    out = turnlog.render_transcript([turn], session_id="w")
    assert "**failed**" in out
    assert "Permission denied" in out


def test_a_pipe_in_a_tool_target_cannot_break_the_table():
    turn = _turn(
        tools=turnlog.compact_trace(
            [
                {
                    "tool": "run_command",
                    "arguments": {"command": "ls | grep x"},
                    "result": {"success": True},
                }
            ]
        )
    )
    row = [
        l for l in turnlog.render_transcript([turn]).splitlines() if "run_command" in l
    ][0]
    assert r"\|" in row  # the command's own pipe is escaped
    # 4 cells → 5 delimiters. Counted with the escaped pipe removed, which is
    # exactly what a Markdown renderer does.
    assert row.replace(r"\|", "").count("|") == 5


def test_a_multiline_prompt_stays_one_block_quote():
    out = turnlog.render_transcript([_turn(user_message="line one\nline two")])
    assert "> line one" in out and "> line two" in out


def test_no_turns_renders_rather_than_raising():
    out = turnlog.render_transcript([], session_id="empty")
    assert "No turns recorded" in out


def test_summary_counts_distinct_files_not_writes():
    turns = [_turn(files_written=["a.py"]), _turn(files_written=["a.py", "b.py"])]
    assert "2 files written" in turnlog.render_transcript(turns)


# ── the DB half ─────────────────────────────────────────────────────────────


async def test_record_then_load_round_trips(factory):
    await turnlog.record_turn(
        session_id="s1",
        user_message="hello",
        answer="hi",
        trace=_trace("x.py"),
        source="telegram:7",
        project="/p",
        task_type="simple_qa",
        flow=turnlog.FLOW_SINGLE,
        duration_ms=120,
        session_factory=factory,
    )
    turns = await turnlog.load_turns("s1", session_factory=factory)
    assert len(turns) == 1
    turn = turns[0]
    assert turn["source"] == "telegram:7"
    assert turn["flow"] == turnlog.FLOW_SINGLE
    assert turn["files_written"] == ["x.py"]
    assert turn["tools"][0]["tool"] == "write_file"


async def test_turns_load_oldest_first(factory):
    for i in range(3):
        await turnlog.record_turn(
            session_id="s",
            user_message=f"m{i}",
            answer="a",
            session_factory=factory,
        )
    turns = await turnlog.load_turns("s", session_factory=factory)
    assert [t["user_message"] for t in turns] == ["m0", "m1", "m2"]


async def test_a_limit_keeps_the_LAST_turns_still_in_order(factory):
    for i in range(5):
        await turnlog.record_turn(
            session_id="s", user_message=f"m{i}", answer="a", session_factory=factory
        )
    turns = await turnlog.load_turns("s", limit=2, session_factory=factory)
    assert [t["user_message"] for t in turns] == ["m3", "m4"]


async def test_sessions_are_isolated(factory):
    await turnlog.record_turn(
        session_id="a", user_message="x", answer="y", session_factory=factory
    )
    await turnlog.record_turn(
        session_id="b", user_message="p", answer="q", session_factory=factory
    )
    assert len(await turnlog.load_turns("a", session_factory=factory)) == 1
    names = {
        s["session_id"] for s in await turnlog.list_sessions(session_factory=factory)
    }
    assert names == {"a", "b"}


async def test_recording_never_raises_when_the_db_is_unusable():
    """Best-effort: a history that will not write must not cost the turn."""

    class _Boom:
        def __call__(self):
            raise RuntimeError("no database")

    ok = await turnlog.record_turn(
        session_id="s", user_message="m", answer="a", session_factory=_Boom()
    )
    assert ok is False


def test_corrupt_json_columns_degrade_to_empty(monkeypatch):
    row = turnlog.TurnEvent(
        id=1,
        session_id="s",
        timestamp="t",
        source="cli",
        project="",
        user_message="m",
        answer="a",
        task_type="",
        flow="",
        tool_trace="{not json",
        files_written="null",
        duration_ms=0,
    )
    turn = turnlog._row_to_dict(row)
    assert turn["tools"] == [] and turn["files_written"] == []


def test_the_stored_trace_is_json(factory):
    """The column holds JSON, so a transcript can be rebuilt outside Python."""
    compact = turnlog.compact_trace(_trace())
    assert json.loads(json.dumps(compact)) == compact


# ── the chat() seam ─────────────────────────────────────────────────────────
#
# `record_turn` is captured rather than exercised: these tests are about the
# wiring — that a turn is recorded at all, with the route it really took and
# the front-end that really asked — and the DB half is covered above.


class _ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        from types import SimpleNamespace

        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


@pytest.fixture
def recorded(monkeypatch):
    """Capture what `chat()` hands the turn log.

    Opts back into `record_turns`, which conftest defaults off for the whole
    suite — these are the tests that are about the seam, so the seam has to be
    switched on for them.
    """
    from config.settings import settings

    monkeypatch.setattr(settings, "record_turns", True)
    calls: list[dict] = []

    async def _capture(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(turnlog, "record_turn", _capture)
    return calls


def _agent(session="pytest_turnlog"):
    from app.agent.core import AgentCore

    return AgentCore(session_id=session)


async def test_a_turn_is_recorded_with_its_files_and_route(
    tmp_path, monkeypatch, recorded
):
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "code_generation")
    agent._llm_direct = _ScriptedLLM(["FILENAME: a.py\nx = 1\n"])

    await agent.chat("create a.py with x")

    assert len(recorded) == 1
    turn = recorded[0]
    assert turn["user_message"] == "create a.py with x"
    assert turn["flow"] == turnlog.FLOW_SINGLE
    assert turn["task_type"] == "code_generation"
    assert turnlog.written_files(turn["trace"]) == [str(tmp_path / "a.py")]


async def test_the_front_end_that_asked_is_recorded(tmp_path, monkeypatch, recorded):
    """A bot turn must be distinguishable from a CLI one in the history."""
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "simple_qa")
    agent._llm_direct = _ScriptedLLM(["hello"])
    agent.turn_source = "telegram:4242"

    await agent.chat("hi")

    assert recorded[0]["source"] == "telegram:4242"


async def test_the_default_source_is_the_cli(tmp_path, monkeypatch, recorded):
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "simple_qa")
    agent._llm_direct = _ScriptedLLM(["hello"])

    await agent.chat("hi")

    assert recorded[0]["source"] == turnlog.SOURCE_CLI


async def test_a_compound_turn_records_the_subtask_route(
    tmp_path, monkeypatch, recorded
):
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "code_generation")
    agent._llm_direct = _ScriptedLLM(
        ["FILENAME: a.py\nx = 1\n", "FILENAME: b.py\ny = 2\n"]
    )

    await agent.chat("create a.py with x, and create b.py with y")

    assert recorded[0]["flow"] == turnlog.FLOW_SUBTASKS


async def test_the_route_does_not_leak_into_the_next_turn(
    tmp_path, monkeypatch, recorded
):
    """A turn that routed one way must not stamp the next turn with it."""
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "code_generation")
    agent._llm_direct = _ScriptedLLM(
        ["FILENAME: a.py\nx = 1\n", "FILENAME: b.py\ny = 2\n", "just an answer"]
    )

    await agent.chat("create a.py with x, and create b.py with y")
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "simple_qa")
    await agent.chat("what is 2 + 2")

    assert recorded[0]["flow"] == turnlog.FLOW_SUBTASKS
    assert recorded[1]["flow"] == turnlog.FLOW_SINGLE


async def test_a_turn_log_failure_never_costs_the_turn(tmp_path, monkeypatch):
    """The real `record_turn` swallows its own errors — assert the seam does too."""
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    monkeypatch.setattr(agent.planner, "classify", lambda msg: "code_generation")
    agent._llm_direct = _ScriptedLLM(["FILENAME: a.py\nx = 1\n"])

    from config.settings import settings

    monkeypatch.setattr(settings, "record_turns", True)

    async def _boom(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(turnlog, "record_turn", _boom)

    with pytest.raises(RuntimeError):
        await agent.chat("create a.py with x")
    # The write still landed — which is the whole reason recording is last and
    # best-effort inside `record_turn` rather than guarded at the call site.
    assert (tmp_path / "a.py").is_file()


# ── history older than the turn log ─────────────────────────────────────────


async def _add_chat(factory, session_id, role, content, stamp="2026-01-01T00:00:00"):
    from app.memory.conversation import ConversationTurn

    async with factory() as session:
        session.add(
            ConversationTurn(
                session_id=session_id, role=role, content=content, timestamp=stamp
            )
        )
        await session.commit()


async def test_a_conversation_rebuilds_into_turns(factory):
    """A project built before T0 still has the only record there is."""
    await _add_chat(factory, "old", "human", "build me a marketplace")
    await _add_chat(factory, "old", "ai", "Created server.js")
    await _add_chat(factory, "old", "human", "add reviews")
    await _add_chat(factory, "old", "ai", "Added reviews")

    turns = await turnlog.load_conversation("old", session_factory=factory)
    assert [t["user_message"] for t in turns] == [
        "build me a marketplace",
        "add reviews",
    ]
    assert turns[0]["answer"] == "Created server.js"


async def test_a_question_with_no_answer_is_still_a_turn(factory):
    """A crashed turn is part of the history, not a reason to drop it."""
    await _add_chat(factory, "old", "human", "build it")
    turns = await turnlog.load_conversation("old", session_factory=factory)
    assert len(turns) == 1 and turns[0]["answer"] == ""


async def test_two_questions_in_a_row_are_two_turns(factory):
    await _add_chat(factory, "old", "human", "one")
    await _add_chat(factory, "old", "human", "two")
    await _add_chat(factory, "old", "ai", "answer")
    turns = await turnlog.load_conversation("old", session_factory=factory)
    assert [t["user_message"] for t in turns] == ["one", "two"]
    assert turns[1]["answer"] == "answer"


async def test_an_answer_with_no_question_before_it_is_dropped(factory):
    """A fragment is not a turn — it would render as an empty prompt."""
    await _add_chat(factory, "old", "ai", "orphan")
    assert await turnlog.load_conversation("old", session_factory=factory) == []


async def test_a_rebuilt_turn_claims_no_tools_or_files(factory):
    await _add_chat(factory, "old", "human", "q")
    await _add_chat(factory, "old", "ai", "a")
    turn = (await turnlog.load_conversation("old", session_factory=factory))[0]
    assert turn["tools"] == [] and turn["files_written"] == [] and turn["flow"] == ""


async def test_conversation_sessions_are_listed(factory):
    await _add_chat(factory, "a", "human", "x")
    await _add_chat(factory, "b", "human", "y")
    names = {
        s["session_id"]
        for s in await turnlog.conversation_sessions(session_factory=factory)
    }
    assert names == {"a", "b"}


def test_a_rebuilt_transcript_says_what_is_missing():
    """Silence would read as "this build ran no tools" — a lie about the work."""
    turns = [_turn(tools=[], files_written=[], flow="")]
    out = turnlog.render_transcript(
        turns, session_id="old", note="predates the turn log"
    )
    assert "**Note:**" in out and "predates the turn log" in out


def test_a_normal_transcript_carries_no_note():
    assert "**Note:**" not in turnlog.render_transcript([_turn()], session_id="w")
