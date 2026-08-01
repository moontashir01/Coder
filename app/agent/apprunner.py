"""Keep the generated app running in the background, across turns.

Phase 6 of `docs/fullstack-web-plan.md`. The smoke test starts the app, probes
it and kills it within seconds — deliberately, because it executes generated
code. That is the wrong shape for a demo: the person watching wants a URL they
can open, that keeps working while the next turn amends the project.

So this holds ONE long-lived process, owned by the REPL rather than by a turn:

    coder> /run          -> http://127.0.0.1:5000
    coder> add a cart    (the app stays up)
    coder> /run restart  (pick up the change)

Deliberately one process, not a pool. Two copies of the same app would fight
over the port and over `app.db`, and "which one am I looking at?" is not a
question anyone should have to ask mid-demo.

Safety is the same posture as `smoke.py`, whose process handling this reuses:
localhost only, no arguments passed to the app, the whole process tree killed on
stop (Windows `taskkill /T`, POSIX process-group kill), and an `atexit` hook so
a crashed REPL cannot leave an orphan holding port 5000. That last one is not
theoretical — orphaned model runners from killed parents cost this project a
build (see `docs/phase4-notes.md`).
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import sys
import time
from pathlib import Path

from app.agent.smoke import _IS_WIN, _kill_tree, _port_open, detect_ports

logger = logging.getLogger(__name__)

# How long to wait for the app to start answering before reporting back.
STARTUP_TIMEOUT = 12.0


class AppRunner:
    """One background app process, owned by the session rather than a turn."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._workdir: Path | None = None
        self._entry: str = ""
        self._port: int | None = None

    # -- state -----------------------------------------------------------

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}" if self._port else ""

    @property
    def workdir(self) -> Path | None:
        return self._workdir

    def status(self) -> str:
        if not self.is_running():
            return "not running"
        where = f" ({self._workdir})" if self._workdir else ""
        return f"running at {self.url}{where}"

    # -- control ---------------------------------------------------------

    def start(self, workdir: Path | str, entry: str = "app.py") -> tuple[bool, str]:
        """Start the app and wait until it answers. Returns ``(ok, message)``.

        Never raises: a failure to start is a message, not an exception, because
        this runs from a REPL command mid-demo.
        """
        if self.is_running():
            return True, f"already {self.status()} — `/run restart` to reload it"

        workdir = Path(workdir)
        target = workdir / entry
        if not target.is_file():
            return False, f"no {entry} in {workdir} — build something first"

        try:
            source = target.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            source = ""
        ports = detect_ports(source)

        popen_kwargs: dict = dict(
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if _IS_WIN:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen([sys.executable, entry], **popen_kwargs)
        except Exception as e:
            return False, f"could not start {entry}: {e}"

        self._proc = proc
        self._workdir = workdir
        self._entry = entry
        self._port = None

        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = self._drain_stderr()
                self._proc = None
                return False, f"{entry} exited on startup: {err}"
            for port in ports:
                if _port_open(port):
                    self._port = port
                    return True, f"running at {self.url}"
            time.sleep(0.3)

        # Alive but silent. Keep it — the user may want to look at its output —
        # and say so plainly rather than claiming a URL that answers nothing.
        return False, (
            f"{entry} is running but nothing answered on "
            f"{', '.join(map(str, ports[:4]))} within {STARTUP_TIMEOUT:.0f}s"
        )

    def stop(self) -> bool:
        """Kill the process tree. True if something was actually running."""
        if self._proc is None:
            return False
        was_running = self.is_running()
        try:
            _kill_tree(self._proc)
        except Exception:
            logger.debug("could not kill the app process", exc_info=True)
        self._proc = None
        self._port = None
        return was_running

    def restart(self) -> tuple[bool, str]:
        """Stop and start again in the same directory — the post-amendment move."""
        workdir, entry = self._workdir, self._entry or "app.py"
        self.stop()
        if workdir is None:
            return False, "nothing has been run yet — use `/run` first"
        return self.start(workdir, entry)

    def _drain_stderr(self) -> str:
        try:
            _, err = self._proc.communicate(timeout=3)
        except Exception:
            return "no output"
        lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
        for line in reversed(lines):
            if "Error" in line or "Exception" in line:
                return line[:200]
        return lines[-1][:200] if lines else "no output"


_RUNNER: AppRunner | None = None


def get_runner() -> AppRunner:
    """The session's single runner (lazy, like the other module singletons)."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = AppRunner()
        atexit.register(_RUNNER.stop)
    return _RUNNER
