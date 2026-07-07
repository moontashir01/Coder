"""Live auto-reindex of the loaded project (Step 4 / P3).

A debounced watchdog observer on the project root feeds file changes back into
the retriever's incremental index (`index_file` / `delete_file`), so retrieval
stays fresh when files change on disk — no manual `/index` needed.

The dispatch logic (`on_event` / `flush`) is deliberately decoupled from the
watchdog Observer so it can be unit-tested with synthetic events and no real
filesystem race.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol

from app.rag.retriever import _INDEXABLE_SUFFIXES, _gitignore_spec


class _ReindexTarget(Protocol):
    def index_file(self, file_path: str | Path) -> int: ...
    def delete_file(self, file_path: str | Path) -> None: ...


class ProjectWatcher:
    """Debounces filesystem events and reindexes changed files.

    `debounce_seconds` coalesces bursts of events (editors write-then-rename,
    formatters rewrite files) so a file is reindexed once things settle.
    """

    def __init__(
        self,
        root: str | Path,
        retriever: _ReindexTarget,
        debounce_seconds: float = 1.0,
    ) -> None:
        self._root = Path(root).resolve()
        self._retriever = retriever
        self._debounce = debounce_seconds
        self._pending: dict[str, bool] = {}  # resolved path -> deleted?
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._observer = None
        self._spec = _gitignore_spec(self._root)

    # ------------------------------------------------------------------
    # Filtering + dispatch (unit-testable without watchdog)
    # ------------------------------------------------------------------

    def _relevant(self, path: str | Path) -> bool:
        p = Path(path)
        if p.suffix.lower() not in _INDEXABLE_SUFFIXES:
            return False
        try:
            rel = p.resolve().relative_to(self._root)
        except ValueError:
            return False  # outside the watched root
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            return False
        if "__pycache__" in parts or "node_modules" in parts:
            return False
        if self._spec is not None and self._spec.match_file(rel.as_posix()):
            return False
        return True

    def on_event(self, path: str | Path, deleted: bool = False) -> None:
        """Record a filesystem event and (re)arm the debounce timer."""
        if not self._relevant(path):
            return
        key = str(Path(path).resolve())
        with self._lock:
            self._pending[key] = deleted
            self._arm_timer()

    def _arm_timer(self) -> None:
        # Caller holds self._lock.
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def flush(self) -> None:
        """Apply all pending events. Best-effort: a failure on one file never
        blocks the others or the watcher itself."""
        with self._lock:
            pending = self._pending
            self._pending = {}
            self._timer = None
        for path, deleted in pending.items():
            try:
                if deleted:
                    self._retriever.delete_file(path)
                else:
                    self._retriever.index_file(path)
            except Exception:
                pass  # keeping the index fresh must never crash the watcher

    # ------------------------------------------------------------------
    # Watchdog wiring
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin watching the project root. Silent no-op if watchdog is
        unavailable — live reindex is a convenience, not a requirement."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        watcher.on_event(event.src_path, deleted=False)

                def on_modified(self, event):
                    if not event.is_directory:
                        watcher.on_event(event.src_path, deleted=False)

                def on_deleted(self, event):
                    if not event.is_directory:
                        watcher.on_event(event.src_path, deleted=True)

                def on_moved(self, event):
                    if not event.is_directory:
                        watcher.on_event(event.src_path, deleted=True)
                        watcher.on_event(event.dest_path, deleted=False)

            observer = Observer()
            observer.schedule(_Handler(), str(self._root), recursive=True)
            observer.start()
            self._observer = observer
        except Exception:
            self._observer = None

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None
