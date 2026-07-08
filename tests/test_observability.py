"""Step 9 / C2 — best-effort failures are logged, not silently swallowed."""
import logging

from app.rag.retriever import Retriever
from tests.test_rag import _FakeStore


class _BoomSymbols:
    """A symbol index whose every operation fails."""

    def index_file(self, *a, **k):
        raise RuntimeError("boom-index")

    def remove_file(self, *a, **k):
        raise RuntimeError("boom-remove")


def _patch_embed(monkeypatch):
    import app.rag.retriever as ret_mod

    monkeypatch.setattr(
        ret_mod, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    monkeypatch.setattr(ret_mod, "embed_query", lambda q: [1.0, 0.0, 0.0])


def test_symbol_index_failure_is_logged_and_write_survives(
    tmp_path, monkeypatch, caplog
):
    _patch_embed(monkeypatch)
    f = tmp_path / "m.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")

    retr = Retriever(store=_FakeStore(), symbol_index=_BoomSymbols())
    retr.load_project(tmp_path)

    with caplog.at_level(logging.WARNING, logger="app.rag.retriever"):
        n = retr.index_file(f)

    # The embedding write still succeeded despite the symbol-index failure...
    assert n >= 1
    # ...and the failure was logged rather than swallowed.
    assert any("symbol index failed" in r.message for r in caplog.records)


def test_symbol_removal_failure_is_logged(tmp_path, monkeypatch, caplog):
    _patch_embed(monkeypatch)
    f = tmp_path / "m.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")

    retr = Retriever(store=_FakeStore(), symbol_index=_BoomSymbols())
    retr.load_project(tmp_path)

    with caplog.at_level(logging.WARNING, logger="app.rag.retriever"):
        retr.delete_file(f)

    assert any("symbol index removal failed" in r.message for r in caplog.records)
