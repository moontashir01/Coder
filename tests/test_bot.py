"""Phase T2 — the Telegram front-end, tested with no Telegram.

`BotService` talks to the `Transport` protocol and `render.py` is pure, so all
of the behaviour that matters — authorization, streaming, approvals, chunking,
who holds the project — is exercised here with a fake transport and a fake
agent. `telegram_bot.py` is wiring and is deliberately not covered: anything
worth testing that ends up in it has ended up somewhere the suite cannot reach.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.sessions import SessionRegistry
from app.bot import audit, render
from app.bot.auth import DEVELOPER, OWNER, VIEWER, UserStore, denied_permissions_for
from app.bot.service import BotService, LiveMessage, RateLimiter
from app.bot.transport import TIMED_OUT
from app.database.sqlite_db import Base
from config.settings import settings

# ── fakes ───────────────────────────────────────────────────────────────────


class FakeTransport:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.questions: list[str] = []
        self.typing_calls = 0
        self._answers = list(answers or [])
        self._next_id = 100

    async def send(self, chat_id: int, html: str) -> int:
        self.sent.append((chat_id, html))
        self._next_id += 1
        return self._next_id

    async def edit(self, chat_id: int, message_id: int, html: str) -> None:
        self.edits.append((chat_id, message_id, html))

    async def typing(self, chat_id: int) -> None:
        self.typing_calls += 1

    async def ask(self, chat_id, html, options, timeout):
        self.questions.append(html)
        return self._answers.pop(0) if self._answers else TIMED_OUT

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.sent] + [t for _, _, t in self.edits]


class FakeExecutor:
    def __init__(self) -> None:
        self.hook = None

    def set_approval_hook(self, hook) -> None:
        self.hook = hook


class FakeAgent:
    """Only what the service touches."""

    def __init__(self, session_id: str = "s", answer: str = "done") -> None:
        self.memory = type("M", (), {"session_id": session_id})()
        self.turn_source = "cli"
        self.status_hook = None
        self.executor = FakeExecutor()
        self.answer = answer
        self.seen: list[str] = []
        self.tokens: list[str] = []
        self.trace: list[dict] = []
        self.during = None

    async def load_project(self, path: str) -> dict:
        return {}

    async def chat(self, message: str, on_token=None):
        self.seen.append(message)
        if self.during is not None:
            await self.during(self)
        for token in self.tokens:
            if on_token is not None:
                on_token(token)
        return self.answer, self.trace

    def close(self) -> None:
        pass


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
async def store(tmp_path, monkeypatch):
    """A `UserStore` on a throwaway database.

    Passed explicitly everywhere: `AsyncSessionLocal` is bound to an engine
    built at import from `settings.sqlite_path`, so a test that let the store
    fall back to it would grant and revoke rows in the REPO's real
    `bot_users` — an authorization table, in a file that ships with the project.
    """
    monkeypatch.setattr(settings, "telegram_allowed_users", [])
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield UserStore(session_factory=async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


@pytest.fixture
def service(project, agent, store, monkeypatch):
    monkeypatch.setattr(settings, "telegram_edit_interval", 0.01)
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    registry = SessionRegistry(agent_factory=lambda session_id: agent)
    transport = FakeTransport()
    svc = BotService(registry, transport, project, allowed_users=[7], users=store)
    svc.transport_ref = transport  # convenience for the tests
    return svc


def _t(service) -> FakeTransport:
    return service.transport  # type: ignore[return-value]


# ── render: HTML ────────────────────────────────────────────────────────────


def test_prose_is_escaped():
    assert render.to_html("a < b & c") == "a &lt; b &amp; c"


def test_a_fence_becomes_a_pre_block():
    html = render.to_html("here:\n```python\nx = 1 < 2\n```")
    assert '<pre><code class="language-python">' in html
    assert "x = 1 &lt; 2" in html


def test_a_fence_with_no_language_still_renders():
    assert render.to_html("```\nplain\n```") == "<pre>plain</pre>"


def test_an_unterminated_fence_is_still_rendered():
    """The model's mistake must not swallow the code or emit an open tag."""
    html = render.to_html("```js\nconst a = 1;")
    assert "const a = 1;" in html
    assert html.count("<pre") == 1 and html.count("</pre>") == 1
    assert html.rstrip().endswith("</pre>")


def test_inline_code_and_bold():
    html = render.to_html("run `npm install` and **wait**")
    assert "<code>npm install</code>" in html
    assert "<b>wait</b>" in html


def test_html_inside_a_code_block_cannot_inject_tags():
    html = render.to_html("```\n<script>alert(1)</script>\n```")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── render: chunking ────────────────────────────────────────────────────────


def test_a_short_answer_is_one_chunk():
    assert len(render.render_chunks("hello")) == 1


def test_a_long_answer_is_split_under_the_limit():
    chunks = render.render_chunks("line of text\n" * 800)
    assert len(chunks) > 1
    assert all(len(c) <= render.MESSAGE_LIMIT for c in chunks)


def test_the_budget_is_measured_after_escaping():
    """A source-length cap under-counts `&` by five times — a diff or a URL."""
    chunks = render.render_chunks("&" * 6000)
    assert all(len(c) <= render.MESSAGE_LIMIT for c in chunks)


def test_a_code_block_split_across_chunks_stays_balanced():
    """An unclosed <pre> is rejected by Telegram and loses the whole message."""
    body = "\n".join(f"line_{i} = {i}" for i in range(600))
    chunks = render.render_chunks(f"```python\n{body}\n```")
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("<pre") == chunk.count("</pre>")
        assert chunk.count("<pre") >= 1


def test_one_enormous_line_is_hard_split():
    chunks = render.render_chunks("x" * 20_000)
    assert len(chunks) > 1
    assert all(len(c) <= render.MESSAGE_LIMIT for c in chunks)
    assert "".join(chunks).count("x") == 20_000


def test_empty_text_still_produces_a_message():
    assert render.render_chunks("") == [""]


def test_the_approval_question_names_the_target():
    """ "Allow write_file?" is unanswerable — the question is WHICH file."""
    text = render.approval_question("write_file", {"path": "app.py"}, ["fs:write"])
    assert "write_file" in text and "app.py" in text and "fs:write" in text


def test_a_failed_tool_line_carries_the_error():
    line = render.tool_line("run_command", {"success": False, "error": "denied"})
    assert "✗" in line and "denied" in line


# ── authorization ───────────────────────────────────────────────────────────


async def test_an_unknown_user_is_refused(service):
    await service.handle(chat_id=1, user_id=999, text="build me a blog")
    assert "Not authorized" in _t(service).texts[0]
    assert "999" in _t(service).texts[0]


async def test_an_unknown_user_never_reaches_the_agent(service, agent):
    await service.handle(chat_id=1, user_id=999, text="delete everything")
    assert agent.seen == []


async def test_an_empty_allowlist_refuses_everyone(project, agent, store, monkeypatch):
    """Deny by default: an unconfigured bot refuses its owner too."""
    monkeypatch.setattr(settings, "telegram_allowed_users", [])
    registry = SessionRegistry(agent_factory=lambda s: agent)
    svc = BotService(registry, FakeTransport(), project, users=store)
    await svc.handle(chat_id=1, user_id=7, text="hello")
    assert agent.seen == []


async def test_an_authorized_user_gets_a_turn(service, agent):
    await service.handle(chat_id=1, user_id=7, text="build me a blog")
    assert agent.seen == ["build me a blog"]


async def test_the_turn_is_attributed_to_the_telegram_user(service, agent):
    seen = {}

    async def during(a):
        seen["source"] = a.turn_source

    agent.during = during
    await service.handle(chat_id=1, user_id=7, text="hi")
    assert seen["source"] == "telegram:7"


async def test_the_source_is_restored_after_the_turn(service, agent):
    await service.handle(chat_id=1, user_id=7, text="hi")
    assert agent.turn_source == "cli"


# ── rate limiting ───────────────────────────────────────────────────────────


def test_the_bucket_allows_a_burst_then_refuses():
    limiter = RateLimiter(capacity=2, per_seconds=60)
    assert limiter.allow(1, now=0) and limiter.allow(1, now=0)
    assert limiter.allow(1, now=0) is False


def test_the_bucket_refills_over_time():
    limiter = RateLimiter(capacity=2, per_seconds=60)
    limiter.allow(1, now=0)
    limiter.allow(1, now=0)
    assert limiter.allow(1, now=60) is True


def test_one_users_burst_does_not_block_another():
    limiter = RateLimiter(capacity=1, per_seconds=60)
    assert limiter.allow(1, now=0)
    assert limiter.allow(1, now=0) is False
    assert limiter.allow(2, now=0) is True


async def test_a_rate_limited_message_is_answered_not_dropped(
    service, agent, monkeypatch
):
    monkeypatch.setattr(settings, "telegram_rate_burst", 1)
    service._limiter = RateLimiter(1, 60)
    await service.handle(1, 7, "one")
    await service.handle(1, 7, "two")
    assert any("Slow down" in t for t in _t(service).texts)
    assert agent.seen == ["one"]


# ── approvals ───────────────────────────────────────────────────────────────


async def _approve_during(service, agent, answer):
    _t(service)._answers = [answer]
    result = {}

    async def during(a):
        result["allowed"] = await a.executor.hook(
            "write_file", {"path": "x.py"}, ["fs:write"]
        )

    agent.during = during
    await service.handle(1, 7, "write x.py")
    return result["allowed"]


async def test_allow_lets_the_tool_run(service, agent):
    assert await _approve_during(service, agent, "allow") is True


async def test_deny_stops_the_tool(service, agent):
    assert await _approve_during(service, agent, "deny") is False


async def test_a_timeout_is_a_deny(service, agent):
    """An unanswered write must never proceed because nobody was looking."""
    assert await _approve_during(service, agent, TIMED_OUT) is False


async def test_an_unrecognised_answer_is_a_deny(service, agent):
    assert await _approve_during(service, agent, "maybe") is False


async def test_allow_for_session_is_remembered(service, agent):
    _t(service)._answers = ["session"]
    calls = []

    async def during(a):
        calls.append(await a.executor.hook("write_file", {"path": "x"}, ["fs:write"]))
        calls.append(await a.executor.hook("write_file", {"path": "y"}, ["fs:write"]))

    agent.during = during
    await service.handle(1, 7, "write two files")
    assert calls == [True, True]
    assert len(_t(service).questions) == 1  # only asked once


async def test_a_session_approval_is_per_chat(service, agent):
    _t(service)._answers = ["session", TIMED_OUT]

    async def during(a):
        await a.executor.hook("write_file", {"path": "x"}, ["fs:write"])

    agent.during = during
    await service.handle(1, 7, "chat one")

    allowed = {}

    async def during2(a):
        allowed["v"] = await a.executor.hook("write_file", {"path": "x"}, ["fs:write"])

    agent.during = during2
    await service.handle(2, 7, "chat two")
    assert allowed["v"] is False


async def test_the_approval_hook_does_not_outlive_the_turn(service, agent):
    """The CLI shares this executor — its next write must not ask Telegram."""
    await service.handle(1, 7, "hi")
    assert agent.executor.hook is None


# ── streaming ───────────────────────────────────────────────────────────────


async def test_tokens_are_shown_while_the_turn_runs(service, agent):
    agent.tokens = ["Hel", "lo ", "world"]
    agent.answer = "Hello world"

    async def during(a):
        for token in a.tokens:
            pass

    await service.handle(1, 7, "say hello")
    assert any("Hello world" in t for t in _t(service).texts)


async def test_a_live_message_edits_rather_than_spamming():
    transport = FakeTransport()
    live = LiveMessage(transport, chat_id=1, interval=0.01)
    await live.start()
    live.feed("abc")
    await live.tick()
    live.feed("def")
    await live.tick()
    assert len(transport.sent) == 1  # one message, edited twice
    assert len(transport.edits) == 2


async def test_an_unchanged_preview_is_not_re_sent():
    """Telegram rejects an unchanged edit; spending the call is pure waste."""
    transport = FakeTransport()
    live = LiveMessage(transport, chat_id=1, interval=0.01)
    await live.start()
    live.feed("abc")
    await live.tick()
    await live.tick()
    assert len(transport.edits) == 1


async def test_the_preview_shows_the_TAIL_of_a_long_stream():
    transport = FakeTransport()
    live = LiveMessage(transport, chat_id=1)
    await live.start()
    live.feed("start" + "x" * 5000 + "END")
    text = live.preview()
    assert text.endswith("END")
    assert len(text) < 3200


async def test_status_lines_reach_the_live_message():
    transport = FakeTransport()
    live = LiveMessage(transport, chat_id=1)
    await live.start()
    live.status("[vision] Analyzing shot.png")
    await live.tick()
    assert "Analyzing shot.png" in transport.edits[-1][2]


async def test_only_the_last_few_status_lines_are_kept():
    live = LiveMessage(FakeTransport(), chat_id=1)
    for i in range(10):
        live.status(f"step {i}")
    assert "step 9" in live.preview()
    assert "step 0" not in live.preview()


async def test_a_long_answer_is_delivered_in_several_messages(service, agent):
    agent.answer = "line of text\n" * 800
    await service.handle(1, 7, "write a lot")
    # First chunk edits the live message; the rest are new messages.
    assert len(_t(service).sent) > 1


async def test_the_status_hook_is_restored_after_the_turn(service, agent):
    await service.handle(1, 7, "hi")
    assert agent.status_hook is None


# ── the project, and who is holding it ──────────────────────────────────────


async def test_load_points_the_chat_at_another_project(service, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    await service.handle(1, 7, f"/load {other}")
    assert service.state(1).project == other.resolve()


async def test_load_refuses_a_path_that_is_not_a_folder(service, tmp_path):
    await service.handle(1, 7, f"/load {tmp_path / 'nope'}")
    assert any("No such folder" in t for t in _t(service).texts)
    assert service.state(1).project == service.default_project


async def test_two_chats_can_be_on_different_projects(service, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    await service.handle(1, 7, f"/load {other}")
    assert service.state(2).project == service.default_project


async def test_a_busy_project_tells_the_user_who_holds_it(
    service, project, monkeypatch
):
    """The demo's central claim, from the bot's side."""
    from app.agent import projectlock

    monkeypatch.setattr(settings, "turn_lock_timeout", 0.2)
    lock = projectlock.ProjectLock(project, front_end="cli")
    assert await lock.acquire(message="build a blog", timeout=1)
    try:
        await service.handle(1, 7, "add a footer")
    finally:
        lock.release()

    said = " ".join(_t(service).texts)
    assert "Busy" in said and "cli" in said


async def test_whoami_states_the_id_and_the_project(service):
    await service.handle(1, 7, "/whoami")
    said = " ".join(_t(service).texts)
    assert "7" in said and "proj" in said


async def test_help_is_answered_without_a_turn(service, agent):
    await service.handle(1, 7, "/help")
    assert agent.seen == []
    assert any("Coder" in t for t in _t(service).texts)


async def test_an_unknown_command_is_passed_to_the_agent(service, agent):
    """`/spec` and `/run` keep ONE implementation — the agent's."""
    await service.handle(1, 7, "/spec")
    assert agent.seen == ["/spec"]


# ── robustness ──────────────────────────────────────────────────────────────


async def test_a_failing_turn_is_reported_not_raised(service, agent):
    async def during(a):
        raise RuntimeError("model exploded")

    agent.during = during
    await service.handle(1, 7, "break")  # must not raise
    assert any("failed" in t for t in _t(service).texts)


async def test_a_crash_in_handling_never_escapes(service, monkeypatch):
    """An exception out of `handle` would kill the poller for every chat."""

    def boom(*a, **k):
        raise RuntimeError("bad state")

    monkeypatch.setattr(service, "state", boom)
    await service.handle(1, 7, "/project")


async def test_concurrent_turns_are_bounded(project, store, monkeypatch):
    monkeypatch.setattr(settings, "telegram_max_concurrent_turns", 1)
    inside = []

    class SlowAgent(FakeAgent):
        async def chat(self, message, on_token=None):
            inside.append(message)
            await asyncio.sleep(0.05)
            inside.remove(message)
            return "ok", []

    agents = {}

    def factory(session_id):
        agents.setdefault(session_id, SlowAgent(session_id))
        return agents[session_id]

    registry = SessionRegistry(agent_factory=factory)
    svc = BotService(registry, FakeTransport(), project, allowed_users=[7], users=store)
    peak = []

    async def watch():
        for _ in range(20):
            peak.append(len(inside))
            await asyncio.sleep(0.01)

    await asyncio.gather(svc.handle(1, 7, "a"), svc.handle(2, 7, "b"), watch())
    assert max(peak) <= 1
