"""Tests for the RAG pipeline: chunker, embedder cache, retriever.

Ollama is never contacted — embeddings are monkeypatched with deterministic fakes.
"""

import pytest

from app.rag import chunker
from app.rag import embedder
from app.rag.retriever import Retriever

# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def test_chunk_python_file(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    chunks = chunker.chunk_file(src)
    assert len(chunks) >= 1
    assert all(c.language == "python" for c in chunks)
    joined = "\n".join(c.content for c in chunks)
    assert "def foo" in joined
    assert "def bar" in joined
    # metadata sanity
    assert all(c.start_line >= 1 for c in chunks)
    assert all(c.end_line >= c.start_line for c in chunks)


def test_chunk_python_is_semantic_not_token_fallback(tmp_path):
    """Two top-level functions must split into 2 semantic chunks.

    Guards against tree-sitter silently breaking: the token-window fallback
    would emit a single chunk for this small file.
    """
    src = tmp_path / "two.py"
    src.write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n", encoding="utf-8"
    )
    chunks = chunker.chunk_file(src)
    assert len(chunks) == 2
    firsts = sorted(c.content.splitlines()[0] for c in chunks)
    assert firsts == ["def bar():", "def foo():"]


def test_chunk_empty_file_returns_nothing(tmp_path):
    src = tmp_path / "empty.py"
    src.write_text("   \n\n", encoding="utf-8")
    assert chunker.chunk_file(src) == []


def test_chunk_plain_text_fallback(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_text("line one\nline two\nline three\n", encoding="utf-8")
    chunks = chunker.chunk_file(src)
    assert len(chunks) == 1
    assert chunks[0].language == "text"
    assert "line two" in chunks[0].content


def test_chunk_text_inline():
    chunks = chunker.chunk_text("hello world", file_path="<x>", language="md")
    assert len(chunks) == 1
    assert chunks[0].content.strip() == "hello world"
    assert chunks[0].file_path == "<x>"


def test_chunk_index_is_monotonic(tmp_path):
    src = tmp_path / "many.py"
    body = "\n\n".join(f"def f{i}():\n    return {i}" for i in range(5))
    src.write_text(body, encoding="utf-8")
    chunks = chunker.chunk_file(src)
    indices = [c.chunk_index for c in chunks]
    assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# Embedder cache
# ---------------------------------------------------------------------------


class _FakeEmbeddings:
    def __init__(self):
        self.doc_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts):
        self.doc_calls += 1
        return [[float(len(t)), 1.0, 2.0] for t in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return [float(len(text)), 0.5, 0.5]


def test_embed_documents_uses_cache(monkeypatch):
    fake = _FakeEmbeddings()
    monkeypatch.setattr(embedder, "_get_embeddings", lambda: fake)
    embedder.clear_cache()

    v1 = embedder.embed_documents(["alpha", "beta"])
    v2 = embedder.embed_documents(["alpha", "beta"])

    assert v1 == v2
    # Second call fully served from cache → underlying invoked exactly once
    assert fake.doc_calls == 1


def test_embed_documents_partial_cache(monkeypatch):
    fake = _FakeEmbeddings()
    monkeypatch.setattr(embedder, "_get_embeddings", lambda: fake)
    embedder.clear_cache()

    embedder.embed_documents(["one"])
    embedder.embed_documents(["one", "two"])  # only "two" is new
    assert fake.doc_calls == 2
    assert len(embedder.embed_documents(["one", "two"])) == 2


def test_embed_query_cached(monkeypatch):
    fake = _FakeEmbeddings()
    monkeypatch.setattr(embedder, "_get_embeddings", lambda: fake)
    embedder.clear_cache()

    a = embedder.embed_query("hi")
    b = embedder.embed_query("hi")
    assert a == b
    assert fake.query_calls == 1


# ---------------------------------------------------------------------------
# Retriever (with an in-memory fake vector store)
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self, name):
        self.name = name


class _FakeStore:
    """Minimal stand-in for VectorStore that keeps chunks in a dict."""

    def __init__(self):
        self.data = {}  # id -> (doc, metadata)

    def get_or_create_collection(self, project_path):
        from pathlib import Path

        return _FakeCollection(Path(project_path).name)

    def delete_by_file(self, col, file_path):
        self.data = {
            k: v for k, v in self.data.items() if v[1].get("file_path") != file_path
        }

    def add_chunks(self, col, ids, embeddings, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.data[i] = (doc, meta)

    def query(self, col, query_embedding, top_k=5):
        out = []
        for doc, meta in list(self.data.values())[:top_k]:
            out.append({"content": doc, "metadata": meta, "distance": 0.0})
        return out

    def count(self, col):
        return len(self.data)

    def delete_collection(self, project_path):
        self.data.clear()


@pytest.fixture
def fake_embed(monkeypatch):
    """Patch the embed functions used inside the retriever module."""
    import app.rag.retriever as ret_mod

    monkeypatch.setattr(
        ret_mod, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    monkeypatch.setattr(ret_mod, "embed_query", lambda q: [1.0, 0.0, 0.0])


def test_index_project_and_query(tmp_path, fake_embed):
    (tmp_path / "a.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# Project\nSome docs.\n", encoding="utf-8")

    store = _FakeStore()
    retr = Retriever(store=store)

    stats = retr.index_project(tmp_path)
    assert stats["files"] == 2
    assert stats["chunks"] >= 2

    results = retr.query("hello", top_k=5)
    assert len(results) >= 1
    assert "file_path" in results[0]["metadata"]


def test_index_skips_hidden_and_pycache(tmp_path, fake_embed):
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    hidden = tmp_path / ".secret"
    hidden.mkdir()
    (hidden / "h.py").write_text("y = 2\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "c.py").write_text("z = 3\n", encoding="utf-8")

    store = _FakeStore()
    retr = Retriever(store=store)
    stats = retr.index_project(tmp_path)
    assert stats["files"] == 1


def test_delete_file_removes_chunks(tmp_path, fake_embed):
    f = tmp_path / "gone.py"
    f.write_text("def g():\n    pass\n", encoding="utf-8")

    store = _FakeStore()
    retr = Retriever(store=store)
    retr.index_project(tmp_path)
    assert store.count(None) >= 1

    retr.delete_file(f)
    assert store.count(None) == 0


def test_query_without_project_raises():
    retr = Retriever(store=_FakeStore())
    with pytest.raises(RuntimeError):
        retr.query("anything")


def test_format_context_respects_budget():
    retr = Retriever(store=_FakeStore())
    results = [
        {
            "content": "x " * 500,
            "metadata": {"file_path": "a.py", "start_line": 1, "end_line": 9},
        },
        {
            "content": "y " * 500,
            "metadata": {"file_path": "b.py", "start_line": 1, "end_line": 9},
        },
    ]
    ctx = retr.format_context(results, max_tokens=50)
    # budget is tiny → at most the first block fits, second is dropped
    assert "b.py" not in ctx
