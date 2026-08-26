"""Phase T3 — authentication and authorization.

The load-bearing test in this file is
`test_a_viewers_write_is_refused_by_the_EXECUTOR`: a role is only real if the
refusal happens in the enforcement layer, not in the bot's own `if`. Everything
else here is about who gets which role and how they got it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.executor import Executor
from app.agent.scope import (
    effective_denied_permissions,
    effective_sandbox_root,
    reset_scope,
    set_scope,
)
from app.agent.sessions import SessionRegistry
from app.agent.tool_registry import ToolDefinition, ToolRegistry
from app.bot import audit
from app.bot.auth import (
    DEVELOPER,
    OWNER,
    OWNER_ONLY,
    ROLES,
    VIEWER,
    UserStore,
    denied_permissions_for,
    hash_code,
    may_run_command,
    new_code,
    normalize_role,
)
from app.bot.service import BotService
from app.database.sqlite_db import Base
from config.settings import settings
from tests.test_bot import FakeAgent, FakeTransport


@pytest.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_users", [])
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield UserStore(session_factory=async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _service(project, store, agent=None, owners=()):
    agent = agent or FakeAgent()
    registry = SessionRegistry(agent_factory=lambda s: agent)
    svc = BotService(
        registry, FakeTransport(), project, allowed_users=list(owners), users=store
    )
    return svc, agent


def _texts(service) -> str:
    return " ".join(service.transport.texts)


# ── roles map onto enforcement that already exists ───────────────────────────


def test_a_viewer_is_denied_the_write_permissions():
    denied = denied_permissions_for(VIEWER)
    assert set(denied) == {"fs:write", "fs:delete", "shell"}


def test_a_developer_denies_nothing_outright():
    """A developer is gated by APPROVAL, not refused — that is the difference."""
    assert denied_permissions_for(DEVELOPER) == []


def test_an_unknown_role_gets_the_strictest_answer():
    assert set(denied_permissions_for("superuser")) == set(
        denied_permissions_for(VIEWER)
    )
    assert normalize_role("superuser") == VIEWER
    assert normalize_role(None) == VIEWER


def test_every_role_has_a_deny_list():
    for role in ROLES:
        assert isinstance(denied_permissions_for(role), list)


async def test_a_viewers_write_is_refused_by_the_EXECUTOR():
    """The point of the whole phase: the bot's own code is not the gate.

    The refusal must come from `Executor.execute`, so it would still happen if
    every line of `service.py` were wrong.
    """
    calls = []

    def writer(path: str):
        calls.append(path)
        return {"success": True, "result": "written", "error": None}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fake_write",
            description="write",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            source="builtin",
            handler=writer,
            permissions=["fs:write"],
        )
    )
    executor = Executor(registry)

    token = set_scope(None, denied_permissions_for(VIEWER))
    try:
        result = await executor.execute("fake_write", {"path": "x.py"})
    finally:
        reset_scope(token)

    assert result["success"] is False
    assert "fs:write" in result["error"]
    assert calls == []  # the handler never ran


async def test_the_same_tool_runs_for_a_developer():
    def writer(path: str):
        return {"success": True, "result": "written", "error": None}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fake_write",
            description="write",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            source="builtin",
            handler=writer,
            permissions=["fs:write"],
        )
    )
    executor = Executor(registry)
    token = set_scope(None, denied_permissions_for(DEVELOPER))
    try:
        result = await executor.execute("fake_write", {"path": "x.py"})
    finally:
        reset_scope(token)
    assert result["success"] is True


def test_a_scope_can_only_ADD_refusals(monkeypatch):
    """A per-caller scope must never be a way to ask for MORE than the process."""
    monkeypatch.setattr(settings, "denied_permissions", ["shell"])
    token = set_scope(None, [])
    try:
        assert "shell" in effective_denied_permissions()
    finally:
        reset_scope(token)


def test_two_roles_in_flight_do_not_share_a_deny_list():
    """A global would let whichever turn started last decide for both."""

    async def as_role(role, hold):
        token = set_scope(None, denied_permissions_for(role))
        try:
            await asyncio.sleep(hold)
            return effective_denied_permissions()
        finally:
            reset_scope(token)

    async def both():
        return await asyncio.gather(as_role(VIEWER, 0.02), as_role(OWNER, 0.0))

    viewer_saw, owner_saw = asyncio.run(both())
    assert "fs:write" in viewer_saw
    assert "fs:write" not in owner_saw


def test_the_scope_still_carries_the_sandbox_root(tmp_path):
    token = set_scope(tmp_path, denied_permissions_for(VIEWER))
    try:
        assert effective_sandbox_root() == tmp_path.resolve()
        assert "shell" in effective_denied_permissions()
    finally:
        reset_scope(token)


# ── the user store ──────────────────────────────────────────────────────────


async def test_an_unknown_user_has_no_role(store):
    assert await store.role_for(4242) is None


async def test_a_granted_user_keeps_their_role(store):
    await store.grant(42, DEVELOPER)
    assert await store.role_for(42) == DEVELOPER


async def test_the_env_allowlist_outranks_the_table(store, monkeypatch):
    """A paired user must not be able to demote the owner by acquiring a row."""
    await store.grant(42, VIEWER)
    monkeypatch.setattr(settings, "telegram_allowed_users", [42])
    assert await store.role_for(42) == OWNER


async def test_granting_twice_updates_rather_than_duplicates(store):
    await store.grant(42, VIEWER)
    await store.grant(42, OWNER)
    assert await store.role_for(42) == OWNER
    assert len([u for u in await store.list_users() if u["user_id"] == 42]) == 1


async def test_revoke_removes_a_paired_user(store):
    await store.grant(42, DEVELOPER)
    assert await store.revoke(42) is True
    assert await store.role_for(42) is None


async def test_revoke_reports_false_for_someone_who_was_not_there(store):
    assert await store.revoke(999) is False


async def test_a_bootstrap_owner_cannot_be_revoked(store, monkeypatch):
    """That id lives in .env; reporting a removal that did not happen is worse."""
    monkeypatch.setattr(settings, "telegram_allowed_users", [42])
    assert await store.revoke(42) is False
    assert await store.role_for(42) == OWNER


async def test_an_unreadable_database_denies_rather_than_admits():
    """A store we cannot read is not a reason to let someone in."""

    class Broken:
        def __call__(self):
            raise RuntimeError("no database")

    assert await UserStore(session_factory=Broken()).role_for(42) is None


# ── pairing ─────────────────────────────────────────────────────────────────


def test_a_code_is_readable_off_a_screen():
    code = new_code()
    assert len(code) == 8
    assert not (set(code) & set("OI01"))  # no ambiguous glyphs


def test_codes_are_not_repeated():
    assert len({new_code() for _ in range(200)}) > 190


async def test_a_code_grants_the_role_it_was_minted_for(store):
    code, _ = await store.mint_code(VIEWER)
    grant = await store.redeem(code, 42)
    assert grant.role == VIEWER
    assert await store.role_for(42) == VIEWER


async def test_a_code_works_once(store):
    code, _ = await store.mint_code(DEVELOPER)
    assert (await store.redeem(code, 42)).role == DEVELOPER
    assert (await store.redeem(code, 43)).role is None
    assert await store.role_for(43) is None


async def test_an_expired_code_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_users", [])
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    clock = {"t": 1000.0}
    store = UserStore(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        now=lambda: clock["t"],
    )
    code, ttl = await store.mint_code(DEVELOPER, ttl=300)
    clock["t"] += ttl + 1
    assert (await store.redeem(code, 42)).role is None
    await engine.dispose()


async def test_an_unknown_code_is_refused(store):
    assert (await store.redeem("ZZZZZZZZ", 42)).role is None


async def test_an_empty_code_is_refused(store):
    assert (await store.redeem("", 42)).role is None


async def test_every_refusal_reads_the_same(store):
    """Distinguishing expired from used from unknown helps only a guesser."""
    code, _ = await store.mint_code(DEVELOPER)
    await store.redeem(code, 42)
    used = await store.redeem(code, 43)
    unknown = await store.redeem("ZZZZZZZZ", 43)
    assert used.reason == unknown.reason


async def test_the_plaintext_code_is_never_stored(store, tmp_path):
    code, _ = await store.mint_code(DEVELOPER)
    blob = (tmp_path / "auth.db").read_bytes()
    assert code.encode() not in blob
    assert hash_code(code).encode() in blob


async def test_a_code_is_spent_even_if_the_grant_fails(store, monkeypatch):
    """A code shown to someone must not stay live after a failed redemption."""
    code, _ = await store.mint_code(DEVELOPER)

    async def boom(*a, **k):
        raise RuntimeError("grant failed")

    monkeypatch.setattr(store, "grant", boom)
    with pytest.raises(RuntimeError):
        await store.redeem(code, 42)
    monkeypatch.undo()
    assert (await store.redeem(code, 43)).role is None


async def test_purge_drops_expired_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_users", [])
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    clock = {"t": 0.0}
    store = UserStore(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        now=lambda: clock["t"],
    )
    await store.mint_code(DEVELOPER, ttl=10)
    clock["t"] = 100
    assert await store.purge_expired() == 1
    await engine.dispose()


# ── the bot, end to end ─────────────────────────────────────────────────────


async def test_an_unpaired_user_is_refused_and_told_their_id(project, store):
    service, agent = _service(project, store)
    await service.handle(1, 4242, "build me a blog")
    assert "Not authorized" in _texts(service) and "4242" in _texts(service)
    assert agent.seen == []


async def test_login_is_the_one_thing_an_unpaired_user_may_do(project, store):
    service, agent = _service(project, store, owners=[7])
    code, _ = await store.mint_code(DEVELOPER)
    await service.handle(2, 4242, f"/login {code}")
    assert await service.role_for(4242) == DEVELOPER
    assert agent.seen == []  # pairing is not a turn


async def test_a_bad_login_leaves_the_user_out(project, store):
    service, _ = _service(project, store)
    await service.handle(2, 4242, "/login WRONGCODE")
    assert await service.role_for(4242) is None


async def test_after_pairing_the_user_can_work(project, store):
    service, agent = _service(project, store)
    code, _ = await store.mint_code(DEVELOPER)
    await service.handle(2, 4242, f"/login {code}")
    await service.handle(2, 4242, "add a footer")
    assert agent.seen == ["add a footer"]


async def test_pair_is_owner_only(project, store):
    service, _ = _service(project, store)
    await store.grant(4242, DEVELOPER)
    await service.handle(1, 4242, "/pair")
    assert "owner-only" in _texts(service)


async def test_an_owner_can_mint_a_code(project, store):
    service, _ = _service(project, store, owners=[7])
    await service.handle(1, 7, "/pair viewer")
    assert "/login " in _texts(service)


async def test_a_minted_code_really_works(project, store):
    service, _ = _service(project, store, owners=[7])
    await service.handle(1, 7, "/pair viewer")
    code = _texts(service).split("/login ")[1].split("<")[0].strip()
    await service.handle(2, 555, f"/login {code}")
    assert await service.role_for(555) == VIEWER


async def test_load_is_owner_only(project, store, tmp_path):
    service, _ = _service(project, store)
    await store.grant(4242, DEVELOPER)
    other = tmp_path / "other"
    other.mkdir()
    await service.handle(1, 4242, f"/load {other}")
    assert "owner-only" in _texts(service)
    assert service.state(1).project == project.resolve()


async def test_every_owner_only_command_is_refused_for_a_viewer():
    for command in OWNER_ONLY:
        assert may_run_command(VIEWER, command) is False
        assert may_run_command(DEVELOPER, command) is False
        assert may_run_command(OWNER, command) is True


async def test_whoami_states_the_role_and_what_it_refuses(project, store):
    service, _ = _service(project, store)
    await store.grant(4242, VIEWER)
    await service.handle(1, 4242, "/whoami")
    said = _texts(service)
    assert "viewer" in said and "fs:write" in said


async def test_users_lists_paired_and_bootstrap(project, store, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_users", [7])
    service, _ = _service(project, store, owners=[7])
    await store.grant(4242, DEVELOPER)
    await service.handle(1, 7, "/users")
    said = _texts(service)
    assert "4242" in said and "7" in said


async def test_revoke_takes_a_user_out(project, store):
    service, _ = _service(project, store, owners=[7])
    await store.grant(4242, DEVELOPER)
    await service.handle(1, 7, "/revoke 4242")
    assert await service.role_for(4242) is None


async def test_revoke_needs_a_numeric_id(project, store):
    service, _ = _service(project, store, owners=[7])
    await service.handle(1, 7, "/revoke somebody")
    assert "Usage" in _texts(service)


async def test_a_viewers_turn_carries_the_deny_list(project, store):
    """The role reaches the turn scope, which is where it is enforced."""
    seen = {}

    agent = FakeAgent()

    async def during(a):
        seen["denied"] = effective_denied_permissions()

    agent.during = during
    service, _ = _service(project, store, agent=agent)
    await store.grant(4242, VIEWER)
    await service.handle(1, 4242, "write a file")
    assert "fs:write" in seen["denied"]


async def test_an_owners_turn_carries_no_extra_denials(project, store):
    seen = {}
    agent = FakeAgent()

    async def during(a):
        seen["denied"] = effective_denied_permissions()

    agent.during = during
    service, _ = _service(project, store, agent=agent, owners=[7])
    await service.handle(1, 7, "write a file")
    assert "fs:write" not in seen["denied"]


# ── audit ───────────────────────────────────────────────────────────────────


def _entries(project) -> list[dict]:
    return audit.read_entries(project)


async def test_a_refusal_is_recorded(project, store, monkeypatch):
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    service, _ = _service(project, store)
    await service.handle(1, 4242, "delete everything")
    events = _entries(project)
    assert events and events[-1]["event"] == audit.REFUSED
    assert events[-1]["user_id"] == 4242


async def test_a_turn_is_recorded_with_who_and_what(project, store, monkeypatch):
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    service, _ = _service(project, store, owners=[7])
    await service.handle(1, 7, "build me a blog")
    turn = [e for e in _entries(project) if e["event"] == audit.TURN][-1]
    assert turn["user_id"] == 7 and turn["role"] == OWNER
    assert "build me a blog" in turn["message"]


async def test_pairing_is_recorded(project, store, monkeypatch):
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    service, _ = _service(project, store)
    code, _unused = await store.mint_code(VIEWER)
    await service.handle(1, 555, f"/login {code}")
    assert any(e["event"] == audit.PAIRED for e in _entries(project))


def test_the_audit_log_lives_in_dot_coder(project, monkeypatch):
    """`.coder/` is skipped by the indexer — an audit log must never be RAG'd."""
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    assert audit.audit_path(project) == project / ".coder" / "bot_audit.log"


def test_audit_lines_are_json(project, monkeypatch):
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    audit.record(audit.TURN, user_id=1, project=project, message="hi")
    line = (project / ".coder" / "bot_audit.log").read_text(encoding="utf-8").strip()
    assert json.loads(line)["message"] == "hi"


def test_audit_appends_rather_than_replaces(project, monkeypatch):
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    audit.record(audit.TURN, user_id=1, project=project)
    audit.record(audit.TURN, user_id=2, project=project)
    assert len(_entries(project)) == 2


def test_an_unwritable_audit_log_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "bot_audit_log", tmp_path / "nope" / "x" / "a.log")
    monkeypatch.setattr(
        audit.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    assert audit.record(audit.TURN, user_id=1) is False


def test_unreadable_audit_lines_are_skipped(project, monkeypatch):
    monkeypatch.setattr(settings, "bot_audit_log", Path(".coder/bot_audit.log"))
    audit.record(audit.TURN, user_id=1, project=project)
    path = audit.audit_path(project)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert len(_entries(project)) == 1
