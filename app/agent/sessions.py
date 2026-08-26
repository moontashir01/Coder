"""One agent per project, one turn at a time (Phase T1, docs/telegram-bot-plan.md).

Everything in Coder assumed a single front-end: one `AgentCore`, one project,
one turn in flight. A second front-end (the Telegram bot, Phase T2) breaks three
things at once, and this module is where all three are answered — BEFORE the bot
exists, so "the app and the bot at the same time" is a property of this seam
rather than a retrofit onto the transport.

1. **`settings` is a process-global mutable singleton.** `load_project` assigns
   `settings.sandbox_root`, and the path jail and the backup directory are both
   read from it at call time. Two projects loaded in one process means the
   second load silently moves the first project's jail — a security-relevant
   failure, not a cosmetic one. `project_settings()` scopes it per turn through
   `app/agent/scope.py`; a save-and-restore of the global is NOT enough, and
   measurably was not (see that module).
2. **`AgentCore` carries per-turn state.** `_blueprint`, `_spec_doc`,
   `_entry_routes` and `_last_write_path` live across the whole of `chat()`.
   Two turns interleaved on one core read each other's: a build verified
   against another turn's blueprint. Hence one `asyncio.Lock` per project.
3. **Two front-ends on one project must share MEMORY.** Two `AgentCore`s would
   each hold their own `_spec`, and turn 2's amendment would plan against turn
   1's stale contract — the staleness the multi-turn eval suite exists to catch.
   Hence one core per project, shared.

(1) and (2) are independent and both are needed. The lock keeps two turns on
ONE project apart; the scope keeps two turns on DIFFERENT projects apart, which
no lock can do because those are supposed to overlap.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from app.agent.projectlock import ProjectLock
from app.agent.scope import reset_scope, set_scope
from app.memory import turnlog
from config.settings import settings

logger = logging.getLogger(__name__)


@contextmanager
def project_settings(
    root: Path | str | None, denied_permissions: list[str] | None = None
) -> Iterator[None]:
    """Scope this turn to `root`, and undo anything it does to the globals.

    Two halves, and both are needed:

    - **The scope** (`app/agent/scope.py`) is a `ContextVar`, so two turns on
      two projects running at once each see their own. A save-and-restore of
      `settings.sandbox_root` is NOT enough and was measured failing: the
      second turn's assignment moved the first turn's jail while the first was
      still inside its own body. The per-project lock cannot help — different
      projects are supposed to overlap.
    - **The restore** puts `settings.sandbox_root` back regardless, because
      `AgentCore.load_project` assigns it directly (that is the single-front-end
      model, and it stays). Without this, loading a second project would leave
      the first one's REPL jailed to the second.

    Only the sandbox root is scoped, deliberately: the path jail
    (`filesystem._jail_check`) and the per-project backup directory
    (`filesystem._backup_root`) both derive from it, so scoping it scopes both.
    `web_stack` is not — it is a user preference, and the project's own spec
    already outranks it per turn (`AgentCore._select_stack`).
    """
    previous = settings.sandbox_root
    token = (
        set_scope(root, denied_permissions)
        if root is not None or denied_permissions is not None
        else None
    )
    try:
        yield
    finally:
        if token is not None:
            reset_scope(token)
        settings.sandbox_root = previous


def default_session_id(root: Path | str) -> str:
    """A stable conversation id for a project path.

    Named after the folder so `/export sessions` is readable, hashed so two
    projects with the same folder name are still separate conversations.
    """
    resolved = Path(root).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"proj-{resolved.name}-{digest}"


@dataclass
class ProjectSession:
    """One project's agent, its turn lock, and when it was last used."""

    root: Path
    agent: Any
    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    turns: int = 0

    def touch(self) -> None:
        self.last_used = time.monotonic()


class SessionRegistry:
    """Project path -> the one `AgentCore` that serves it."""

    def __init__(self, agent_factory: Callable[[str], Any] | None = None) -> None:
        # Injectable so tests need neither a real `AgentCore` (~2.3s each) nor a
        # real project index.
        self._agent_factory = agent_factory or _default_agent_factory
        self._sessions: dict[Path, ProjectSession] = {}
        # Guards the dict, NOT the turns: creating a session loads and indexes a
        # project, and two front-ends asking for the same one at the same moment
        # must not build two cores and race to register them.
        self._registry_lock = asyncio.Lock()

    # ── lookup ────────────────────────────────────────────────────────────

    async def get(
        self, project_path: Path | str, session_id: str | None = None
    ) -> ProjectSession:
        """The session for a project, creating and loading it on first use."""
        root = Path(project_path).resolve()
        async with self._registry_lock:
            existing = self._sessions.get(root)
            if existing is not None:
                existing.touch()
                return existing

            agent = self._agent_factory(session_id or default_session_id(root))
            session = ProjectSession(
                root=root,
                agent=agent,
                session_id=session_id or default_session_id(root),
            )
            self._sessions[root] = session

        # Outside the registry lock: indexing a large project takes seconds, and
        # holding the lock through it would stall an unrelated project's lookup.
        # Inside the settings pin, because `load_project` assigns sandbox_root
        # and would otherwise leave it pointing at whichever project loaded last.
        try:
            with project_settings(root):
                await session.agent.load_project(str(root))
        except Exception:
            logger.warning("could not load project %s", root, exc_info=True)
        return session

    def peek(self, project_path: Path | str) -> ProjectSession | None:
        """The session for a project if one exists. Never creates one."""
        return self._sessions.get(Path(project_path).resolve())

    def adopt(
        self, project_path: Path | str, agent: Any, session_id: str
    ) -> ProjectSession:
        """Register an ALREADY-loaded agent as a project's session.

        This is how the REPL joins the registry: it built its own `AgentCore`
        at startup and loaded a project into it, and a second core for the same
        folder is exactly what (3) above forbids. Replaces any existing entry
        for that path — the caller is asserting this is the one.
        """
        root = Path(project_path).resolve()
        session = ProjectSession(root=root, agent=agent, session_id=session_id)
        self._sessions[root] = session
        return session

    # ── one turn ──────────────────────────────────────────────────────────

    @asynccontextmanager
    async def turn(
        self,
        project_path: Path | str,
        front_end: str = "cli",
        source: str = turnlog.SOURCE_CLI,
        message: str = "",
        on_wait: Callable[[str], None] | None = None,
        timeout: float | None = None,
        denied_permissions: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        """Hold everything one turn on this project needs, then put it back.

        Yields the project's `AgentCore` with its jail scoped, its in-process
        lock held, its cross-process lock held, `turn_source` set, and — when
        the caller passes `denied_permissions` — the executor refusing that
        caller's forbidden tools before they run. So the caller's whole body is
        `answer, trace = await agent.chat(msg)`.

        Raises `TurnBusy` if another PROCESS holds the project past `timeout`;
        the in-process lock is waited on without a timeout, because that is our
        own queue and dropping a turn out of it would be losing work we accepted.
        """
        session = await self.get(project_path)
        async with session.lock:
            session.touch()
            session.turns += 1
            lock = ProjectLock(
                session.root,
                front_end=front_end,
                stale_after=settings.turn_lock_stale_after,
            )
            took_file_lock = False
            if settings.cross_process_lock:
                took_file_lock = await lock.acquire(
                    message=message,
                    timeout=settings.turn_lock_timeout if timeout is None else timeout,
                    on_wait=on_wait,
                )
                if not took_file_lock:
                    holder = lock.holder()
                    raise TurnBusy(
                        holder.describe()
                        if holder is not None
                        else "another process is running a turn on this project"
                    )
            previous_source = getattr(session.agent, "turn_source", turnlog.SOURCE_CLI)
            try:
                with project_settings(session.root, denied_permissions):
                    session.agent.turn_source = source
                    yield session.agent
            finally:
                # Restored even on failure: a turn that raised must not leave
                # the next one attributed to the front-end that crashed.
                session.agent.turn_source = previous_source
                if took_file_lock:
                    lock.release()
                session.touch()

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def close_idle(self, max_idle: float | None = None) -> list[Path]:
        """Close sessions untouched for `max_idle` seconds. Returns what closed.

        A long-running bot would otherwise hold one watchdog observer, one
        Chroma collection and one core per project it ever saw. A session whose
        lock is held is never closed — that is a turn in flight.
        """
        limit = settings.session_idle_timeout if max_idle is None else max_idle
        if limit <= 0:
            return []
        now = time.monotonic()
        closed: list[Path] = []
        async with self._registry_lock:
            for root, session in list(self._sessions.items()):
                if session.lock.locked() or now - session.last_used < limit:
                    continue
                _close(session)
                del self._sessions[root]
                closed.append(root)
        return closed

    async def close_all(self) -> None:
        async with self._registry_lock:
            for session in self._sessions.values():
                _close(session)
            self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def roots(self) -> list[Path]:
        return list(self._sessions)


class TurnBusy(RuntimeError):
    """Another process holds this project's turn lock."""


def _close(session: ProjectSession) -> None:
    close = getattr(session.agent, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        logger.debug("closing session for %s failed", session.root, exc_info=True)


def _default_agent_factory(session_id: str) -> Any:
    from app.agent.core import AgentCore

    return AgentCore(session_id=session_id)


# ── the process's registry ──────────────────────────────────────────────────

_REGISTRY: SessionRegistry | None = None


def session_registry() -> SessionRegistry:
    """The one registry this process uses. Lazy, never built at import.

    A cached accessor rather than a module-level singleton, for the reason
    `get_registry`/`get_retriever` are: importing the package must not build
    state. Every front-end in a process shares this one — that is what makes
    "the CLI and the bot on the same project" a single `AgentCore` with a
    single lock, rather than two of each that happen to point at one folder.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SessionRegistry()
    return _REGISTRY


def reset_session_registry() -> None:
    """Drop the cached registry. For tests; never call this with turns in flight."""
    global _REGISTRY
    _REGISTRY = None
