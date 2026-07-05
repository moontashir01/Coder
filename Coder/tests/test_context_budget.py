"""Tests for token-aware history trimming (roadmap Tier 2 #5).

Fully offline: tiktoken is local, no Ollama.
"""

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.context_budget import count_tokens, trim_history_to_budget


def test_count_tokens_positive_and_monotonic():
    assert count_tokens("") == 0
    a = count_tokens("hello world")
    b = count_tokens("hello world and then some more words here")
    assert a > 0
    assert b > a


def _history(n):
    """n human/ai pairs, each a distinct marker word so we can see what survives."""
    msgs = []
    for i in range(n):
        msgs.append(HumanMessage(content=f"H{i} " + "word " * 20))
        msgs.append(AIMessage(content=f"A{i} " + "word " * 20))
    return msgs


def test_trim_keeps_all_when_under_budget():
    history = _history(3)
    kept = trim_history_to_budget("sys", history, "latest", max_tokens=100_000)
    assert kept == history


def test_trim_drops_oldest_first():
    history = _history(10)  # ~ hundreds of tokens
    kept = trim_history_to_budget("sys", history, "latest", max_tokens=300)
    assert len(kept) < len(history)
    joined = " ".join(m.content for m in kept)
    # oldest gone, newest retained
    assert "H0" not in joined
    assert "A9" in joined
    # order preserved (a suffix of the original)
    assert kept == history[len(history) - len(kept) :]


def test_trim_keeps_nothing_but_never_errors_when_core_exceeds_budget():
    history = _history(5)
    # budget smaller than even the fixed system+latest → history fully dropped,
    # but the function must not raise and must return a list.
    kept = trim_history_to_budget(
        "system " * 100, history, "latest " * 100, max_tokens=10
    )
    assert kept == []


def test_trim_empty_history():
    assert trim_history_to_budget("sys", [], "latest", max_tokens=50) == []


# ---------------------------------------------------------------------------
# _build_messages actually applies the budget
# ---------------------------------------------------------------------------


async def test_build_messages_trims_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from app.agent.core import AgentCore
    from config.settings import settings

    a = AgentCore(session_id="pytest_budget_build")
    for i in range(10):
        await a.memory.add_human(f"H{i} " + "word " * 200)
        await a.memory.add_ai(f"A{i} " + "word " * 200)

    monkeypatch.setattr(settings, "max_context_tokens", 500)
    msgs = await a._build_messages("latest question", include_tool_protocol=False)

    assert msgs[0].__class__.__name__ == "SystemMessage"
    assert msgs[-1].content == "latest question"
    history_msgs = msgs[1:-1]
    # 20 turns seeded but only a few fit in a 500-token budget
    assert 0 < len(history_msgs) < 20
    joined = " ".join(m.content for m in history_msgs)
    assert "H0" not in joined  # oldest dropped
    assert "A9" in joined  # newest kept
