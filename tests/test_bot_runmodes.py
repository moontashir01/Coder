"""Phase T4 — the entry points, and the claim they make possible.

The headline is `test_the_bot_waits_for_the_terminals_turn`: it drives the
REPL's own turn path and the bot's, on one project, in one process, and asserts
they serialize and that the bot is TOLD who is holding it. Everything before
this phase made that possible; nothing before it made it true, because the REPL
called `agent.chat` directly and so never took the project's lock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent import sessions
from app.agent.sessions import SessionRegistry, session_registry
from app.bot.service import BotService
from app.cli import commands as cli_commands
from app.cli.repl import CoderREPL
from config.settings import settings
from tests.test_bot import FakeAgent, FakeTransport


@pytest.fixture(autouse=True)
def _fresh_registry():
    sessions.reset_session_registry()
    yield
    sessions.reset_session_registry()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


class ReplAgent(FakeAgent):
    """A `FakeAgent` with the two attributes the REPL reaches for."""

    def __init__(self, session_id: str = "cli", project: Path | None = None) -> None:
        super().__init__(session_id)
        self.project_path = str(project) if project else None
        self.skill_loader = None
        self.executor = _NullExecutor()

    async def load_project(self, path: str) -> dict:
        self.project_path = path
        return {"files": 1, "chunks": 1}


class _NullExecutor:
    def set_approval_hook(self, hook) -> None:
        self.hook = hook


def _repl(agent) -> CoderREPL:
    return CoderREPL(agent=agent)


# ── the registry is shared, and the REPL joins it ────────────────────────────


def test_the_process_registry_is_one_object():
    assert session_registry() is session_registry()


def test_the_registry_is_not_built_at_import():
    """The lazy-accessor rule: importing must not create state."""
    sessions.reset_session_registry()
    assert sessions._REGISTRY is None


async def test_loading_a_project_adopts_the_repls_own_agent(project):
    agent = ReplAgent()
    repl = _repl(agent)
    await repl.load_project(str(project))
    session = repl.registry.peek(project)
    assert session is not None and session.agent is agent


async def test_the_bot_reuses_the_repls_agent_rather_than_building_one(project):
    """Two cores on one project is the stale-spec bug — assert there is one."""
    agent = ReplAgent()
    repl = _repl(agent)
    await repl.load_project(str(project))

    service = BotService(
        repl.registry, FakeTransport(), project, allowed_users=[7], users=_AllowAll()
    )
    await service.handle(1, 7, "add a footer")
    assert agent.seen == ["add a footer"]


class _AllowAll:
    """A `UserStore` stand-in: everyone is an owner, nothing touches a DB."""

    async def role_for(self, user_id: int) -> str:
        return "owner"


# ── the REPL's own turn goes through the registry ───────────────────────────


async def test_the_cli_turn_takes_the_project_lock(project):
    from app.agent import projectlock

    agent = ReplAgent()
    repl = _repl(agent)
    await repl.load_project(str(project))

    held = {}

    async def during(a):
        held["info"] = projectlock.read_lock(project)

    agent.during = during
    await repl._chat_in_turn("hello", on_token=None, on_status=lambda s: None)

    assert held["info"] is not None
    assert held["info"].front_end == "cli"
    # And it is released afterwards.
    assert projectlock.read_lock(project) is None


async def test_with_no_project_the_turn_is_unchanged(tmp_path):
    """Nothing to lock and nothing to share — exactly the pre-registry path."""
    agent = ReplAgent()
    repl = _repl(agent)
    answer, trace = await repl._chat_in_turn(
        "hello", on_token=None, on_status=lambda s: None
    )
    assert agent.seen == ["hello"]
    assert answer == "done"


async def test_the_cli_turn_is_attributed_to_the_cli(project):
    agent = ReplAgent()
    repl = _repl(agent)
    await repl.load_project(str(project))
    seen = {}

    async def during(a):
        seen["source"] = a.turn_source

    agent.during = during
    await repl._chat_in_turn("hi", on_token=None, on_status=lambda s: None)
    assert seen["source"] == "cli"


# ── the headline: both front-ends, one project ──────────────────────────────


async def test_the_bot_waits_for_the_terminals_turn(project, monkeypatch):
    """The demo's central claim, measured.

    One process, one project, both front-ends. The bot's message must not
    interleave with the terminal's turn, and the person on Telegram must be
    told what is happening rather than watching nothing.
    """
    monkeypatch.setattr(settings, "telegram_edit_interval", 0.01)
    agent = ReplAgent()
    repl = _repl(agent)
    await repl.load_project(str(project))

    order: list[str] = []
    started = asyncio.Event()

    async def during(a):
        order.append("cli-in")
        started.set()
        await asyncio.sleep(0.15)
        order.append("cli-out")

    agent.during = during

    transport = FakeTransport()
    service = BotService(
        repl.registry, transport, project, allowed_users=[7], users=_AllowAll()
    )

    async def bot_turn():
        await started.wait()
        agent.during = None  # the CLI's hold is the only one we want
        order.append("bot-start")
        await service.handle(1, 7, "add a footer")
        order.append("bot-done")

    await asyncio.gather(
        repl._chat_in_turn("build it", on_token=None, on_status=lambda s: None),
        bot_turn(),
    )

    assert order.index("bot-start") < order.index("cli-out")  # it really overlapped
    assert order.index("cli-out") < order.index("bot-done")  # and it waited
    assert agent.seen == ["build it", "add a footer"]


async def test_two_projects_progress_at_once(tmp_path, monkeypatch):
    """The other half of the brief: different projects, no queueing."""
    monkeypatch.setattr(settings, "telegram_edit_interval", 0.01)
    monkeypatch.setattr(settings, "telegram_max_concurrent_turns", 2)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()

    inside: list[str] = []
    agents: dict[str, ReplAgent] = {}

    def factory(session_id):
        agent = ReplAgent(session_id)

        async def during(_a, tag=session_id):
            inside.append(tag)
            await asyncio.sleep(0.08)
            inside.remove(tag)

        agent.during = during
        agents[session_id] = agent
        return agent

    registry = SessionRegistry(agent_factory=factory)
    service = BotService(
        registry, FakeTransport(), a, allowed_users=[7], users=_AllowAll()
    )
    await service.handle(1, 7, f"/load {b}")

    peak: list[int] = []

    async def watch():
        for _ in range(30):
            peak.append(len(inside))
            await asyncio.sleep(0.01)

    await asyncio.gather(
        service.handle(1, 7, "work on b"),
        service.handle(2, 7, "work on a"),
        watch(),
    )
    assert max(peak) == 2  # both were inside a turn at the same moment


# ── /bot ────────────────────────────────────────────────────────────────────


async def test_bot_status_reports_stopped_and_why(project, monkeypatch, capsys):
    monkeypatch.setattr(settings, "telegram_enabled", False)
    repl = _repl(ReplAgent(project=project))
    assert await cli_commands.handle_command("/bot status", repl) is True
    out = capsys.readouterr().out
    assert "stopped" in out
    assert "TELEGRAM_ENABLED" in out


async def test_bot_start_refuses_with_a_reason_rather_than_silently(
    project, monkeypatch, capsys
):
    """A front-end that quietly fails to come up looks like one nobody uses."""
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_token", "")
    repl = _repl(ReplAgent(project=project))
    await cli_commands.handle_command("/bot start", repl)
    assert "TELEGRAM_TOKEN" in capsys.readouterr().out
    assert repl.bot is None


async def test_bot_stop_when_nothing_runs(project, capsys):
    repl = _repl(ReplAgent(project=project))
    await cli_commands.handle_command("/bot stop", repl)
    assert "not running" in capsys.readouterr().out


async def test_an_unknown_bot_action_prints_usage(project, capsys):
    repl = _repl(ReplAgent(project=project))
    await cli_commands.handle_command("/bot wobble", repl)
    assert "Usage" in capsys.readouterr().out


async def test_bot_pair_mints_a_usable_code(project, monkeypatch, capsys, tmp_path):
    """Minting from the terminal is how the FIRST remote user gets in."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot import auth
    from app.database.sqlite_db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    store = auth.UserStore(session_factory=factory)
    monkeypatch.setattr(auth, "UserStore", lambda *a, **k: store)
    monkeypatch.setattr(settings, "telegram_allowed_users", [])

    repl = _repl(ReplAgent(project=project))
    await cli_commands.handle_command("/bot pair viewer", repl)
    out = capsys.readouterr().out
    code = out.split("/login ")[1].split()[0].strip()

    grant = await store.redeem(code, 4242)
    assert grant.role == auth.VIEWER
    await engine.dispose()


async def test_a_bot_turn_gives_the_terminal_its_approval_hook_back(project):
    """Clearing it would leave the CLI with the executor's default: allow."""
    from app.agent.executor import Executor
    from app.agent.tool_registry import ToolRegistry

    async def cli_hook(tool, args, permissions):
        return True

    agent = ReplAgent()
    agent.executor = Executor(ToolRegistry())
    agent.executor.set_approval_hook(cli_hook)
    repl = _repl(agent)
    await repl.load_project(str(project))

    service = BotService(
        repl.registry, FakeTransport(), project, allowed_users=[7], users=_AllowAll()
    )
    await service.handle(1, 7, "write a file")

    assert agent.executor.approval_hook is cli_hook


async def test_the_bots_own_hook_is_installed_during_its_turn(project):
    from app.agent.executor import Executor
    from app.agent.tool_registry import ToolRegistry

    async def cli_hook(tool, args, permissions):
        return True

    agent = ReplAgent()
    agent.executor = Executor(ToolRegistry())
    agent.executor.set_approval_hook(cli_hook)
    seen = {}

    async def during(a):
        seen["hook"] = a.executor.approval_hook

    agent.during = during
    repl = _repl(agent)
    await repl.load_project(str(project))
    service = BotService(
        repl.registry, FakeTransport(), project, allowed_users=[7], users=_AllowAll()
    )
    await service.handle(1, 7, "write a file")

    assert seen["hook"] is not cli_hook and seen["hook"] is not None
