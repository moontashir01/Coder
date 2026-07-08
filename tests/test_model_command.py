"""Step 15 / U5 — /model switches the Ollama model at runtime.

Offline: ChatOllama is constructed but never invoked, so no Ollama is needed.
"""
import io

import pytest
from rich.console import Console

import app.cli.commands as commands_mod
from app.cli.commands import handle_command
from config.settings import settings


def test_set_model_rebuilds_agent_and_planner_llms():
    from app.agent.core import AgentCore

    original = settings.llm_model
    try:
        agent = AgentCore(session_id="pytest_model")
        previous = agent.set_model("qwen2.5-coder:14b")

        assert previous == original
        assert settings.llm_model == "qwen2.5-coder:14b"
        # Every cached LLM (agent + planner) now uses the new model.
        assert agent._llm.model == "qwen2.5-coder:14b"
        assert agent._llm_edit.model == "qwen2.5-coder:14b"
        assert agent.planner._llm_fast.model == "qwen2.5-coder:14b"
    finally:
        settings.llm_model = original


class _FakeAgent:
    def __init__(self):
        self.switched_to = None

    def set_model(self, name):
        self.switched_to = name
        return "previous-model"


class _FakeRepl:
    def __init__(self):
        self.agent = _FakeAgent()


@pytest.fixture
def captured_console(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(
        commands_mod, "console", Console(file=buf, force_terminal=False, width=100)
    )
    return buf


async def test_model_command_switches(captured_console):
    repl = _FakeRepl()
    handled = await handle_command("/model qwen2.5-coder:14b", repl)

    assert handled is True
    assert repl.agent.switched_to == "qwen2.5-coder:14b"
    out = captured_console.getvalue()
    assert "Model switched" in out
    assert "qwen2.5-coder:14b" in out


async def test_model_command_no_args_shows_current(captured_console, monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "qwen2.5-coder:7b")
    handled = await handle_command("/model", _FakeRepl())

    assert handled is True
    assert "qwen2.5-coder:7b" in captured_console.getvalue()
