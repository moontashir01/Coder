"""Tier 3 #7 — real token streaming through chat() → _direct_answer.

The streaming LLM is faked with an object exposing .astream() as an async
generator yielding langchain-style chunk objects (SimpleNamespace(content=str)).
No Ollama needed.
"""
import io
from types import SimpleNamespace

import pytest

from app.agent.core import AgentCore


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


class StreamLLM:
    """Fake ChatOllama for .astream(): yields chunk objects with .content."""

    def __init__(self, pieces):
        self._pieces = list(pieces)
        self.astream_calls = 0

    async def astream(self, messages):
        self.astream_calls += 1
        for p in self._pieces:
            yield SimpleNamespace(content=p)


class ExplodingStreamLLM:
    async def astream(self, messages):
        raise RuntimeError("kaboom")
        yield  # pragma: no cover — makes this an async generator


# ---------------------------------------------------------------------------
# _direct_answer
# ---------------------------------------------------------------------------


async def test_direct_answer_streams_tokens_in_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_stream_order")
    a._llm_stream = StreamLLM(["Hello", " wor", "ld"])

    got: list[str] = []
    answer = await a._direct_answer("hi", on_token=got.append)

    assert answer == "Hello world"
    assert got == ["Hello", " wor", "ld"]
    assert a._llm_stream.astream_calls == 1


async def test_direct_answer_stream_skips_empty_chunks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_stream_empty")
    a._llm_stream = StreamLLM(["", "a", ""])

    got: list[str] = []
    answer = await a._direct_answer("hi", on_token=got.append)

    assert answer == "a"
    assert got == ["a"]


async def test_direct_answer_without_callback_uses_invoke(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_stream_no_cb")
    a._llm_direct = ScriptedLLM(["plain answer"])
    a._llm_stream = None  # must not be touched when no callback given

    answer = await a._direct_answer("hi")

    assert answer == "plain answer"


async def test_direct_answer_stream_error_returns_llm_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_stream_err")
    a._llm_stream = ExplodingStreamLLM()

    answer = await a._direct_answer("hi", on_token=lambda t: None)

    assert answer.startswith("LLM error:")
    assert "kaboom" in answer


# ---------------------------------------------------------------------------
# chat() threads on_token into the direct-answer branch
# ---------------------------------------------------------------------------


async def test_chat_threads_on_token_to_direct_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_stream_chat")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "simple_qa")
    a._llm_stream = StreamLLM(["Tokyo", " is", " big"])

    got: list[str] = []
    answer, trace = await a.chat("tell me about tokyo", on_token=got.append)

    assert answer == "Tokyo is big"
    assert got == ["Tokyo", " is", " big"]
    assert trace == []


async def test_chat_without_on_token_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_stream_chat_plain")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "simple_qa")
    a._llm_direct = ScriptedLLM(["the answer"])
    a._llm_stream = None  # untouched without a callback

    answer, trace = await a.chat("tell me about tokyo")

    assert answer == "the answer"
    assert trace == []


# ---------------------------------------------------------------------------
# REPL wiring — _agent_turn passes an on_token callback and renders the answer
# ---------------------------------------------------------------------------


async def test_repl_agent_turn_streams_and_renders(monkeypatch):
    from rich.console import Console

    import app.cli.repl as repl_mod

    buf = io.StringIO()
    monkeypatch.setattr(
        repl_mod, "console", Console(file=buf, force_terminal=False, width=80)
    )

    class FakeAgent:
        def __init__(self):
            self.received_on_token = False

        async def chat(self, msg, on_token=None):
            self.received_on_token = on_token is not None
            if on_token:
                on_token("streamed ")
                on_token("tokens")
            return "final answer", []

    agent = FakeAgent()
    r = repl_mod.CoderREPL(agent=agent)
    await r._agent_turn("hello")

    assert agent.received_on_token is True
    assert "final answer" in buf.getvalue()
