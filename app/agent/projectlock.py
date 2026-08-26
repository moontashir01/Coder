"""One turn at a time per project, ACROSS processes (Phase T1).

`SessionRegistry` serializes turns inside one process with an `asyncio.Lock`.
That is the whole story while the REPL and the bot share a process — and none
of it while they don't: `coder --bot-only` in one terminal and `coder` in
another are two processes, each with its own lock object, and nothing stops
their writes from interleaving. Two turns writing the same file means one
`write_file` landing between another turn's write and the `_verify_and_repair`
read that judges it, which reads as the model producing a file it did not.

So the lock lives on disk, at `<project>/.coder/coder.lock`. `.coder/` is
already a dot-directory, so the RAG indexer, `project_memory._scan_project` and
`_locate_named_file` all skip it: the lock is never embedded, never adopted as
part of a spec and never chosen as an edit target.

Rules the callers depend on:

- **A held lock makes the other front-end WAIT, and say who holds it.** Never
  fail silently, never barge. `on_wait` is called once with a human-readable
  description of the holder, which is what turns "nothing is happening" into
  "the CLI is running a turn (started 8s ago)".
- **A stale lock is reclaimed by PID liveness, not by age alone.** A build turn
  legitimately runs for minutes, so a timeout-only rule would break the exact
  case this protects. Age is only a backstop against PID reuse, and it is long.
- **The holder is never killed and never signalled.** `os.kill(pid, 0)` is a
  liveness probe on POSIX and, on Windows, `TerminateProcess` — the same call
  that would end the other front-end's turn. Windows liveness goes through
  `OpenProcess` instead.
- **A lock is only released by the process that holds it.** Each acquire writes
  a random token; release deletes the file only when the token still matches,
  so a process whose lock was reclaimed cannot delete its successor's.
- **Every failure degrades to "no cross-process lock".** An unwritable
  `.coder/`, a read-only checkout or a filesystem without atomic create must
  cost the in-process serialization nothing — the turn still runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

LOCK_NAME = "coder.lock"

_IS_WIN = sys.platform.startswith("win")
_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class LockInfo:
    """Who is holding a project's turn lock."""

    pid: int
    front_end: str
    started_at: float
    message: str
    host: str
    token: str

    def describe(self, now: float | None = None) -> str:
        age = max(0.0, (now if now is not None else time.time()) - self.started_at)
        what = f' on "{self.message}"' if self.message else ""
        return (
            f"{self.front_end} (pid {self.pid}) is running a turn{what} "
            f"— started {age:.0f}s ago"
        )


def lock_path(root: Path | str) -> Path:
    return Path(root) / ".coder" / LOCK_NAME


def pid_alive(pid: int) -> bool:
    """Is this PID a live process on this machine?

    Errs toward **True**: an unknown answer must not reclaim a lock that is
    genuinely held, because that is the failure this module exists to prevent.
    """
    if pid <= 0:
        return False
    if _IS_WIN:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows liveness WITHOUT `os.kill`.

    `os.kill(pid, 0)` on Windows calls `TerminateProcess(handle, 0)` — it would
    kill the other front-end mid-turn rather than ask about it.
    """
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
        )
        if not handle:
            # Access denied means the process exists and is not ours.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        logger.debug("windows pid liveness probe failed", exc_info=True)
        return True


def read_lock(root: Path | str) -> LockInfo | None:
    """The current holder, or None if the project is not locked / unreadable."""
    path = lock_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LockInfo(
            pid=int(data.get("pid") or 0),
            front_end=str(data.get("front_end") or "another front-end"),
            started_at=float(data.get("started_at") or 0.0),
            message=str(data.get("message") or ""),
            host=str(data.get("host") or ""),
            token=str(data.get("token") or ""),
        )
    except (TypeError, ValueError):
        return None


def is_reclaimable(
    info: LockInfo, stale_after: float, now: float | None = None
) -> bool:
    """May this lock be taken from its recorded holder?

    Pure, so the policy is testable without processes. Three ways to say yes,
    and each of them means "the holder cannot be running":

    - the file says nothing usable (no pid);
    - the pid is not a live process **on this host**;
    - the pid is alive but the lock is absurdly old — the PID-reuse backstop.
      Age alone is never enough, which is why this arm also requires
      `stale_after` to have elapsed rather than a build-turn-length timeout.

    A lock written by a DIFFERENT host (a project on a network share) is never
    reclaimed: our PID table says nothing about their processes.
    """
    now = time.time() if now is None else now
    if info.pid <= 0:
        return True
    if info.host and info.host != socket.gethostname():
        return False
    if not pid_alive(info.pid):
        return True
    return stale_after > 0 and (now - info.started_at) > stale_after


class ProjectLock:
    """An advisory, cross-process turn lock for one project directory."""

    def __init__(
        self,
        root: Path | str,
        front_end: str = "cli",
        stale_after: float = 3600.0,
    ) -> None:
        self.root = Path(root)
        self.front_end = front_end
        self.stale_after = stale_after
        self.path = lock_path(self.root)
        self._token: str | None = None

    # ── acquire / release ─────────────────────────────────────────────────

    def _write_lock(self, message: str) -> bool:
        """Atomically create the lock file. False if someone else has it."""
        token = uuid.uuid4().hex
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "front_end": self.front_end,
                "started_at": time.time(),
                "message": message[:200],
                "host": socket.gethostname(),
                "token": token,
            }
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            # No usable lock file — degrade to in-process serialization only.
            logger.debug("project lock unavailable at %s", self.path, exc_info=True)
            raise
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        self._token = token
        return True

    async def acquire(
        self,
        message: str = "",
        timeout: float | None = None,
        on_wait: Callable[[str], None] | None = None,
    ) -> bool:
        """Take the lock, waiting for the current holder. True if we hold it.

        Returns False on timeout — the caller decides what to tell the user;
        raising here would make a busy project look like a crash.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        announced = False
        while True:
            try:
                if self._write_lock(message):
                    return True
            except OSError:
                # The lock file cannot exist here at all. In-process
                # serialization still applies; say so and let the turn run.
                return True

            info = read_lock(self.root)
            if info is None:
                # Created between our attempt and our read, or unreadable.
                # Treat an unreadable lock as reclaimable: it can never be
                # released by anyone, and the alternative is a project that
                # is locked forever.
                self._steal()
                continue
            if info.token == self._token and self._token is not None:
                return True
            if is_reclaimable(info, self.stale_after):
                logger.warning("reclaiming stale project lock held by pid %s", info.pid)
                self._steal()
                continue

            if on_wait is not None and not announced:
                announced = True
                try:
                    on_wait(info.describe())
                except Exception:
                    logger.debug("lock wait callback failed", exc_info=True)

            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL_SECONDS)

    def _steal(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            logger.debug("could not remove stale lock %s", self.path, exc_info=True)

    def release(self) -> None:
        """Release the lock — only if this process still holds it.

        A lock reclaimed from us while we ran belongs to someone else now, and
        deleting it would hand a third turn a project two turns are inside.
        """
        if self._token is None:
            return
        info = read_lock(self.root)
        if info is not None and info.token != self._token:
            self._token = None
            return
        self._token = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not release project lock %s", self.path)

    def holder(self) -> LockInfo | None:
        return read_lock(self.root)

    @property
    def held_by_us(self) -> bool:
        return self._token is not None
