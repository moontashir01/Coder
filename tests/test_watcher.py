"""Tests for the live auto-reindex watcher (Step 4 / P3).

Only the debounce + dispatch + filtering logic is exercised — no real watchdog
Observer and no filesystem race. Events are injected synthetically.
"""

from pathlib import Path

from app.rag.watcher import ProjectWatcher


class _SpyRetriever:
    def __init__(self):
        self.indexed: list[str] = []
        self.deleted: list[str] = []

    def index_file(self, file_path):
        self.indexed.append(str(Path(file_path).resolve()))
        return 1

    def delete_file(self, file_path):
        self.deleted.append(str(Path(file_path).resolve()))


def _watcher(tmp_path, **kw):
    return ProjectWatcher(tmp_path, _SpyRetriever(), debounce_seconds=999, **kw)


def test_flush_reindexes_modified_file(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    w.on_event(f, deleted=False)
    w.flush()
    assert str(f.resolve()) in w._retriever.indexed
    assert w._retriever.deleted == []


def test_flush_dispatches_delete(tmp_path):
    f = tmp_path / "gone.py"
    w = _watcher(tmp_path)
    w.on_event(f, deleted=True)
    w.flush()
    assert str(f.resolve()) in w._retriever.deleted
    assert w._retriever.indexed == []


def test_debounce_coalesces_repeated_events(tmp_path):
    f = tmp_path / "busy.py"
    f.write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    for _ in range(5):
        w.on_event(f, deleted=False)
    # Nothing dispatched until the debounce fires (here, via explicit flush).
    assert w._retriever.indexed == []
    w.flush()
    assert w._retriever.indexed == [str(f.resolve())]  # coalesced to one


def test_ignores_non_indexable_and_hidden(tmp_path):
    w = _watcher(tmp_path)
    w.on_event(tmp_path / "image.png", deleted=False)
    w.on_event(tmp_path / ".hidden.py", deleted=False)
    w.on_event(tmp_path / "__pycache__" / "c.py", deleted=False)
    w.on_event(tmp_path / "node_modules" / "lib.py", deleted=False)
    w.flush()
    assert w._retriever.indexed == []
    assert w._retriever.deleted == []


def test_ignores_paths_outside_root(tmp_path):
    w = _watcher(tmp_path)
    outside = tmp_path.parent / "elsewhere.py"
    w.on_event(outside, deleted=False)
    w.flush()
    assert w._retriever.indexed == []


def test_respects_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    w = _watcher(tmp_path)  # spec read at construction
    w.on_event(tmp_path / "build" / "out.py", deleted=False)
    w.on_event(tmp_path / "keep.py", deleted=False)
    w.flush()
    assert w._retriever.indexed == [str((tmp_path / "keep.py").resolve())]


def test_stop_is_safe_without_observer(tmp_path):
    w = _watcher(tmp_path)
    w.stop()  # never started — must not raise
