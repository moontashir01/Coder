"""Step 1 / C1 — the RAG + symbol index is refreshed after the agent edits files.

All offline: the LLM is scripted, embeddings are faked, the vector store is an
in-memory dict. Verifies that after an edit a follow-up query returns the NEW
content (not the stale pre-edit content), and that every mutating path
(`_file_op_flow`, `_surgical_edit`, and the native tool loop) triggers a reindex.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agent.core import AgentCore
from app.rag.retriever import Retriever

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _ScriptedLLM:
    """Returns canned `.content` for each successive invoke (for file flows)."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


class _ToolCallingLLM:
    """Fake chat model for `_run_tool_loop`: yields scripted AIMessages."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


def _tc(name, args, call_id="call_1"):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _sr_block(search, replace):
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


class _SpyRetriever:
    """Records which paths were (re)indexed / dropped, without embedding."""

    def __init__(self):
        self.indexed: list[str] = []
        self.deleted: list[str] = []
        self._current_project = None

    def index_project(self, project_path):
        self._current_project = str(project_path)
        return {"files": 0, "chunks": 0}

    def index_file(self, file_path):
        self.indexed.append(str(file_path))
        return 1

    def delete_file(self, file_path):
        self.deleted.append(str(file_path))

    def query(self, question, top_k=None):
        return []

    def format_context(self, results, max_tokens=1200):
        return ""


class _MemCollection:
    def __init__(self, name):
        self.name = name


class _MemStore:
    """Minimal in-memory VectorStore (id -> (doc, metadata))."""

    def __init__(self):
        self.data: dict = {}

    def get_or_create_collection(self, project_path):
        from pathlib import Path

        return _MemCollection(Path(project_path).name)

    def delete_by_file(self, col, file_path):
        self.data = {
            k: v for k, v in self.data.items() if v[1].get("file_path") != file_path
        }

    def add_chunks(self, col, ids, embeddings, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.data[i] = (doc, meta)

    def query(self, col, query_embedding, top_k=5):
        return [
            {"content": doc, "metadata": meta, "distance": 0.0}
            for doc, meta in list(self.data.values())[:top_k]
        ]

    def count(self, col):
        return len(self.data)

    def delete_collection(self, project_path):
        self.data.clear()


class _NoSymbols:
    def index_file(self, *a, **k):
        pass

    def remove_file(self, *a, **k):
        pass


# --------------------------------------------------------------------------- #
# Integration: an edit is retrievable; stale content is gone (roadmap Verify)
# --------------------------------------------------------------------------- #


async def test_file_op_edit_reindexes_and_query_sees_new_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import app.rag.retriever as ret_mod

    monkeypatch.setattr(
        ret_mod, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    monkeypatch.setattr(ret_mod, "embed_query", lambda q: [1.0, 0.0, 0.0])

    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\nOLD_MARKER stale content\n", encoding="utf-8")

    retr = Retriever(store=_MemStore(), symbol_index=_NoSymbols())
    retr.index_project(tmp_path)  # initial index contains OLD_MARKER

    a = AgentCore(session_id="pytest_edit_reindex", retriever=retr)
    a._project_path = str(tmp_path)
    a._llm_edit = _ScriptedLLM(["no blocks"])  # force the whole-file rewrite path
    a._llm_direct = _ScriptedLLM(
        ["FILENAME: notes.md\n# Notes\nNEW_MARKER fresh content\n"]
    )

    await a._file_op_flow("update the notes", target="notes.md")

    on_disk = notes.read_text(encoding="utf-8")
    assert "NEW_MARKER" in on_disk and "OLD_MARKER" not in on_disk

    retrieved = "\n".join(r["content"] for r in retr.query("content", top_k=10))
    assert "NEW_MARKER" in retrieved  # the edit is retrievable without /index
    assert "OLD_MARKER" not in retrieved  # and the stale content is gone


# --------------------------------------------------------------------------- #
# Each mutating path triggers a reindex / drop
# --------------------------------------------------------------------------- #


async def test_surgical_edit_reindexes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    spy = _SpyRetriever()
    a = AgentCore(session_id="pytest_surgical_reindex", retriever=spy)
    a._project_path = str(tmp_path)
    a._llm_edit = _ScriptedLLM([_sr_block("value = 1", "value = 2")])

    res = await a._surgical_edit("app.py", target, "value = 1\n", "bump the value")

    assert res is not None
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert str(target) in spy.indexed


async def test_tool_loop_write_reindexes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "new.py"

    spy = _SpyRetriever()
    a = AgentCore(session_id="pytest_toolloop_write", retriever=spy)
    a._project_path = str(tmp_path)
    a._llm = _ToolCallingLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc("write_file", {"path": str(target), "content": "x = 1\n"})
                ],
            ),
            AIMessage(content="done"),
        ]
    )

    await a._run_tool_loop(messages=[])

    assert target.read_text(encoding="utf-8") == "x = 1\n"
    assert str(target) in spy.indexed


async def test_tool_loop_delete_drops_from_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    doomed = tmp_path / "gone.py"
    doomed.write_text("x = 1\n", encoding="utf-8")

    spy = _SpyRetriever()
    a = AgentCore(session_id="pytest_toolloop_delete", retriever=spy)
    a._project_path = str(tmp_path)
    a._llm = _ToolCallingLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_tc("delete_file", {"path": str(doomed), "confirm": True})],
            ),
            AIMessage(content="done"),
        ]
    )

    await a._run_tool_loop(messages=[])

    assert not doomed.exists()
    assert str(doomed) in spy.deleted
    assert spy.indexed == []


# --------------------------------------------------------------------------- #
# Guard: no reindex when no project is loaded
# --------------------------------------------------------------------------- #


async def test_no_reindex_without_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spy = _SpyRetriever()
    a = AgentCore(session_id="pytest_noproject_reindex", retriever=spy)
    # _project_path deliberately left as None
    a._llm_direct = _ScriptedLLM(["FILENAME: notes.txt\nhello world"])

    await a._file_op_flow("make a notes.txt file")

    assert (tmp_path / "notes.txt").exists()
    assert spy.indexed == []  # nothing indexed because no project is active
