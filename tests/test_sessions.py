"""Phase T1 — two front-ends, one machine.

The registry and the lock are tested with a stand-in agent: a real `AgentCore`
costs ~2.3s to build and would index a project per test, and none of what is
under test here is about the agent — it is about serialization, the settings
pin, and who is allowed to take a lock away from whom.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path

import pytest

from app.agent import projectlock
from app.agent.projectlock import LockInfo, ProjectLock, is_reclaimable, lock_path
from app.agent.scope import effective_sandbox_root
from app.agent.sessions import (
    SessionRegistry,
    TurnBusy,
    default_session_id,
    project_settings,
)
from config.settings import settings


class FakeAgent:
    """Everything the registry touches on an `AgentCore`, and nothing else."""

    def __init__(self, session_id: str) -> None:
        self.memory = type("M", (), {"session_id": session_id})()
        self.turn_source = "cli"
        self.loaded: list[str] = []
        self.sandbox_seen: list[Path | None] = []
        self.closed = 0

    async def load_project(self, path: str) -> dict:
        self.loaded.append(path)
        # Exactly what the real one does, and the reason the pin exists.
        settings.sandbox_root = Path(path).resolve()
        return {"files": 0}

    async def chat(self, message: str):
        # Read the way the file tools read it, not off the setting: that is the
        # distinction this whole module turns on.
        self.sandbox_seen.append(effective_sandbox_root())
        return "ok", []

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def registry():
    return SessionRegistry(agent_factory=FakeAgent)


@pytest.fixture(autouse=True)
def _restore_sandbox():
    before = settings.sandbox_root
    yield
    settings.sandbox_root = before


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


# ── project_settings ────────────────────────────────────────────────────────


def test_the_scope_is_what_the_file_tools_read(tmp_path):
    settings.sandbox_root = tmp_path / "before"
    with project_settings(tmp_path / "during"):
        assert effective_sandbox_root() == (tmp_path / "during").resolve()
        # The GLOBAL is deliberately untouched — scoping it globally is the
        # thing that broke two concurrent turns.
        assert settings.sandbox_root == tmp_path / "before"
    assert effective_sandbox_root() == tmp_path / "before"


def test_without_a_scope_the_setting_is_still_the_jail(tmp_path):
    """A single front-end, a library import and every old test are unchanged."""
    settings.sandbox_root = tmp_path / "only"
    assert effective_sandbox_root() == tmp_path / "only"


def test_the_scope_falls_back_to_none(tmp_path):
    settings.sandbox_root = None
    with project_settings(tmp_path):
        assert effective_sandbox_root() is not None
    assert effective_sandbox_root() is None


def test_a_load_inside_the_scope_cannot_move_the_outer_jail(tmp_path):
    """`load_project` assigns the global; the scope has to undo that."""
    settings.sandbox_root = tmp_path / "before"
    with project_settings(tmp_path / "during"):
        settings.sandbox_root = (
            tmp_path / "during"
        ).resolve()  # what load_project does
    assert settings.sandbox_root == tmp_path / "before"


def test_the_scope_restores_after_an_exception(tmp_path):
    settings.sandbox_root = tmp_path / "before"
    with pytest.raises(RuntimeError):
        with project_settings(tmp_path / "during"):
            raise RuntimeError("turn failed")
    assert effective_sandbox_root() == tmp_path / "before"


# ── the registry ────────────────────────────────────────────────────────────


async def test_one_agent_per_project(registry, tmp_path):
    """Two cores on one project each hold their own spec — the staleness bug."""
    root = _project(tmp_path, "a")
    first = await registry.get(root)
    second = await registry.get(root)
    assert first is second
    assert first.agent.loaded == [str(root.resolve())]


async def test_different_projects_get_different_agents(registry, tmp_path):
    a = await registry.get(_project(tmp_path, "a"))
    b = await registry.get(_project(tmp_path, "b"))
    assert a.agent is not b.agent
    assert len(registry) == 2


async def test_the_same_path_written_differently_is_one_session(registry, tmp_path):
    root = _project(tmp_path, "a")
    first = await registry.get(root)
    second = await registry.get(str(root) + os.sep + "." + os.sep)
    assert first is second


async def test_session_ids_are_stable_and_distinct(tmp_path):
    a = _project(tmp_path, "a")
    b = _project(tmp_path, "b")
    assert default_session_id(a) == default_session_id(a)
    assert default_session_id(a) != default_session_id(b)
    assert "proj-a-" in default_session_id(a)


async def test_adopt_registers_an_already_loaded_agent(registry, tmp_path):
    """The REPL joins the registry with the core it already built."""
    root = _project(tmp_path, "a")
    agent = FakeAgent("work")
    registry.adopt(root, agent, session_id="work")
    session = await registry.get(root)
    assert session.agent is agent
    assert agent.loaded == []  # not re-indexed


async def test_a_failed_load_still_yields_a_session(tmp_path):
    class Broken(FakeAgent):
        async def load_project(self, path):
            raise OSError("no such directory")

    registry = SessionRegistry(agent_factory=Broken)
    session = await registry.get(tmp_path / "gone")
    assert session.agent is not None


# ── serialization ───────────────────────────────────────────────────────────


async def test_turns_on_one_project_are_serialized(registry, tmp_path):
    root = _project(tmp_path, "a")
    events: list[str] = []

    async def one(tag: str, hold: float):
        async with registry.turn(root, message=tag):
            events.append(f"enter-{tag}")
            await asyncio.sleep(hold)
            events.append(f"exit-{tag}")

    await asyncio.gather(one("a", 0.05), one("b", 0.0))

    # Whichever went first, the other's enter comes after its exit.
    assert events[0].startswith("enter-")
    assert events[1].startswith("exit-")
    assert events[1][5:] == events[0][6:]


async def test_turns_on_different_projects_overlap(registry, tmp_path):
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    inside = []

    async def one(root, tag):
        async with registry.turn(root, message=tag):
            inside.append(tag)
            await asyncio.sleep(0.05)
            # The other turn must have entered while this one is still inside.
            return list(inside)

    both = await asyncio.gather(one(a, "a"), one(b, "b"))
    assert sorted(both[0]) == ["a", "b"]


async def test_each_turn_sees_its_own_sandbox_root(registry, tmp_path):
    """The failure this whole module exists for: two projects, one global."""
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    seen = {}

    async def one(root, tag):
        async with registry.turn(root, message=tag) as agent:
            await asyncio.sleep(0.02)
            await agent.chat("x")
            seen[tag] = agent.sandbox_seen[-1]

    await asyncio.gather(one(a, "a"), one(b, "b"))
    assert seen["a"] == a.resolve()
    assert seen["b"] == b.resolve()


async def test_the_turn_sets_and_restores_the_source(registry, tmp_path):
    root = _project(tmp_path, "a")
    async with registry.turn(root, source="telegram:9") as agent:
        assert agent.turn_source == "telegram:9"
    assert agent.turn_source == "cli"


async def test_a_raised_turn_releases_everything(registry, tmp_path):
    root = _project(tmp_path, "a")
    before = settings.sandbox_root
    with pytest.raises(RuntimeError):
        async with registry.turn(root, source="telegram:9") as agent:
            raise RuntimeError("boom")
    assert settings.sandbox_root == before
    assert agent.turn_source == "cli"
    assert not lock_path(root).exists()
    # And the project is usable again.
    async with registry.turn(root):
        pass


async def test_the_file_lock_is_taken_and_released(registry, tmp_path):
    root = _project(tmp_path, "a")
    async with registry.turn(root, front_end="cli", message="build a blog"):
        info = projectlock.read_lock(root)
        assert info is not None
        assert info.pid == os.getpid()
        assert info.message == "build a blog"
    assert not lock_path(root).exists()


async def test_another_process_holding_the_lock_makes_the_turn_wait(
    registry, tmp_path, monkeypatch
):
    """A live foreign holder is waited on, and the waiter is told who it is."""
    root = _project(tmp_path, "a")
    _write_foreign_lock(root, pid=os.getpid(), front_end="cli")  # alive => not stale
    monkeypatch.setattr(settings, "turn_lock_timeout", 0.3)
    told: list[str] = []

    with pytest.raises(TurnBusy) as excinfo:
        async with registry.turn(root, on_wait=told.append):
            pass

    assert told and "is running a turn" in told[0]
    assert "cli" in str(excinfo.value)


async def test_a_busy_project_does_not_stay_locked_in_process(
    registry, tmp_path, monkeypatch
):
    """`TurnBusy` must not leave the in-process lock held by the failed turn."""
    root = _project(tmp_path, "a")
    _write_foreign_lock(root, pid=os.getpid(), front_end="cli")
    monkeypatch.setattr(settings, "turn_lock_timeout", 0.2)

    with pytest.raises(TurnBusy):
        async with registry.turn(root):
            pass

    assert (await registry.get(root)).lock.locked() is False
    lock_path(root).unlink()
    async with registry.turn(root):
        pass


async def test_a_dead_holders_lock_is_reclaimed(registry, tmp_path, monkeypatch):
    root = _project(tmp_path, "a")
    _write_foreign_lock(root, pid=999_999, front_end="telegram")
    monkeypatch.setattr(projectlock, "pid_alive", lambda pid: False)

    async with registry.turn(root):
        assert projectlock.read_lock(root).pid == os.getpid()


async def test_the_cross_process_lock_can_be_turned_off(
    registry, tmp_path, monkeypatch
):
    root = _project(tmp_path, "a")
    monkeypatch.setattr(settings, "cross_process_lock", False)
    async with registry.turn(root):
        assert not lock_path(root).exists()


# ── lifecycle ───────────────────────────────────────────────────────────────


async def test_idle_sessions_are_closed(registry, tmp_path):
    root = _project(tmp_path, "a")
    session = await registry.get(root)
    session.last_used -= 10_000
    closed = await registry.close_idle(max_idle=60)
    assert closed == [root.resolve()]
    assert session.agent.closed == 1
    assert len(registry) == 0


async def test_a_session_with_a_turn_in_flight_is_never_closed(registry, tmp_path):
    root = _project(tmp_path, "a")
    session = await registry.get(root)

    async def hold():
        async with registry.turn(root):
            session.last_used -= 10_000
            await asyncio.sleep(0.05)

    task = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    assert await registry.close_idle(max_idle=1) == []
    await task


async def test_close_all_closes_every_agent(registry, tmp_path):
    a = await registry.get(_project(tmp_path, "a"))
    b = await registry.get(_project(tmp_path, "b"))
    await registry.close_all()
    assert a.agent.closed == 1 and b.agent.closed == 1
    assert len(registry) == 0


# ── the lock itself ─────────────────────────────────────────────────────────


def _write_foreign_lock(root: Path, pid: int, front_end: str, age: float = 5.0) -> None:
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "front_end": front_end,
                "started_at": time.time() - age,
                "message": "something",
                "host": socket.gethostname(),
                "token": "foreign",
            }
        ),
        encoding="utf-8",
    )


def _info(**kw) -> LockInfo:
    base = dict(
        pid=os.getpid(),
        front_end="cli",
        started_at=time.time(),
        message="",
        host=socket.gethostname(),
        token="t",
    )
    base.update(kw)
    return LockInfo(**base)


def test_a_live_holder_is_not_reclaimable():
    assert is_reclaimable(_info(), stale_after=3600) is False


def test_a_dead_holder_is_reclaimable(monkeypatch):
    monkeypatch.setattr(projectlock, "pid_alive", lambda pid: False)
    assert is_reclaimable(_info(pid=999_999), stale_after=3600) is True


def test_age_alone_does_not_reclaim_a_running_build():
    """A build turn legitimately runs for minutes — that is the case protected."""
    ten_minutes = _info(started_at=time.time() - 600)
    assert is_reclaimable(ten_minutes, stale_after=3600) is False


def test_an_absurdly_old_lock_is_the_pid_reuse_backstop():
    ancient = _info(started_at=time.time() - 10_000)
    assert is_reclaimable(ancient, stale_after=3600) is True


def test_a_lock_from_another_host_is_never_reclaimed():
    """Our PID table says nothing about another machine's processes."""
    foreign = _info(pid=1, host="some-other-box")
    assert is_reclaimable(foreign, stale_after=1) is False


def test_pid_zero_means_an_unusable_lock():
    assert is_reclaimable(_info(pid=0), stale_after=3600) is True


def test_pid_alive_is_true_for_this_process():
    assert projectlock.pid_alive(os.getpid()) is True


def test_pid_alive_is_false_for_a_pid_that_cannot_exist():
    assert projectlock.pid_alive(-1) is False


async def test_release_does_not_delete_a_lock_that_was_reclaimed(tmp_path):
    """A process whose lock was taken must not delete its successor's."""
    root = _project(tmp_path, "a")
    lock = ProjectLock(root)
    assert await lock.acquire(timeout=1)
    _write_foreign_lock(root, pid=os.getpid(), front_end="telegram")
    lock.release()
    assert lock_path(root).exists()
    assert projectlock.read_lock(root).token == "foreign"


async def test_release_is_idempotent_and_safe_without_a_lock(tmp_path):
    lock = ProjectLock(_project(tmp_path, "a"))
    lock.release()
    lock.release()


async def test_an_unreadable_lock_file_is_reclaimed(tmp_path):
    """A lock nobody can parse can never be released by anyone."""
    root = _project(tmp_path, "a")
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("}{ not json", encoding="utf-8")

    lock = ProjectLock(root)
    assert await lock.acquire(timeout=1) is True
    assert projectlock.read_lock(root).pid == os.getpid()


async def test_acquire_returns_false_rather_than_raising_when_busy(tmp_path):
    root = _project(tmp_path, "a")
    _write_foreign_lock(root, pid=os.getpid(), front_end="cli")
    lock = ProjectLock(root)
    assert await lock.acquire(timeout=0.2) is False


async def test_the_lock_lives_in_the_dot_coder_directory(tmp_path):
    """`.coder/` is skipped by the indexer, the spec scan and target lookup."""
    root = _project(tmp_path, "a")
    assert lock_path(root) == root / ".coder" / "coder.lock"


def test_describe_names_the_holder_and_the_wait():
    text = _info(
        front_end="cli", message="build a blog", started_at=time.time() - 8
    ).describe()
    assert "cli" in text and "build a blog" in text and "8s ago" in text


# ── a real second process ───────────────────────────────────────────────────


_HOLDER = """
import json, os, socket, sys, time
root = sys.argv[1]
path = os.path.join(root, ".coder", "coder.lock")
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, json.dumps({
    "pid": os.getpid(), "front_end": "telegram", "started_at": time.time(),
    "message": "add a level 3", "host": socket.gethostname(), "token": "held",
}).encode())
os.close(fd)
print("held", os.getpid(), flush=True)
time.sleep(30)
"""


async def test_a_lock_held_by_a_real_other_process_is_waited_on(tmp_path):
    """The claim the demo rests on, measured against an actual second process.

    Everything above uses this process's own PID, which proves the policy and
    not that the policy sees a foreign process at all.
    """
    import subprocess
    import sys

    from app.agent.smoke import _kill_tree

    root = _project(tmp_path, "shared")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # The interpreter that WROTE the lock, which on a venv launcher is a
        # child of the process we spawned — read it from the holder itself.
        marker, held_pid = holder.stdout.readline().split()
        held_pid = int(held_pid)
        assert marker == "held"

        info = projectlock.read_lock(root)
        assert info is not None and info.pid == held_pid
        assert projectlock.pid_alive(held_pid) is True
        assert projectlock.is_reclaimable(info, stale_after=3600) is False

        lock = ProjectLock(root, front_end="cli")
        told: list[str] = []
        assert await lock.acquire(timeout=0.4, on_wait=told.append) is False
        assert told and "telegram" in told[0] and "add a level 3" in told[0]
    finally:
        _kill_tree(holder)

    for _ in range(50):
        if not projectlock.pid_alive(held_pid):
            break
        await asyncio.sleep(0.1)

    # The holder is gone; its lock is now reclaimable and the project usable.
    assert projectlock.pid_alive(held_pid) is False
    lock = ProjectLock(root, front_end="cli")
    assert await lock.acquire(timeout=2) is True
    lock.release()
