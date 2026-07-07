import hashlib
from pathlib import Path
from typing import Any

from app.database.vector_store import VectorStore, vector_store
from app.rag.chunker import LANGUAGE_MAP, Chunk, chunk_file
from app.rag.embedder import embed_documents, embed_query
from app.rag.symbols import SymbolIndex
from app.rag.symbols import symbol_index as _default_symbol_index
from config.settings import settings

# Extensions we attempt to index
_INDEXABLE_SUFFIXES = set(LANGUAGE_MAP.keys()) | {
    ".md",
    ".txt",
    ".rst",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
}


def _chunk_id(file_path: str, chunk_index: int) -> str:
    key = f"{file_path}::{chunk_index}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class Retriever:
    def __init__(
        self,
        store: VectorStore | None = None,
        symbol_index: SymbolIndex | None = None,
    ) -> None:
        self._store = store or vector_store
        self._symbols = symbol_index or _default_symbol_index
        self._current_project: str | None = None

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    def load_project(self, project_path: str | Path) -> str:
        """Set the active project; returns collection name."""
        self._current_project = str(project_path)
        return self._store.get_or_create_collection(project_path).name

    def _require_project(self):
        if self._current_project is None:
            raise RuntimeError("No project loaded. Call load_project() first.")
        return self._store.get_or_create_collection(self._current_project)

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_project(self, project_path: str | Path) -> dict[str, int]:
        """Chunk, embed, and store all indexable files under project_path."""
        self._current_project = str(project_path)
        col = self._store.get_or_create_collection(project_path)
        root = Path(project_path)

        files = [
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in _INDEXABLE_SUFFIXES
            and not any(part.startswith(".") for part in p.parts)
            and "__pycache__" not in p.parts
            and "node_modules" not in p.parts
        ]

        total_chunks = 0
        for file_path in files:
            total_chunks += self._index_single_file(col, file_path)

        return {"files": len(files), "chunks": total_chunks}

    def index_file(self, file_path: str | Path) -> int:
        """Re-embed a single file (call after edits)."""
        col = self._require_project()
        return self._index_single_file(col, Path(file_path))

    def _index_single_file(self, col, file_path: Path) -> int:
        chunks: list[Chunk] = chunk_file(file_path)
        if not chunks:
            return 0

        # Remove stale embeddings for this file before re-inserting
        self._store.delete_by_file(col, str(file_path))

        texts = [c.content for c in chunks]
        embeddings = embed_documents(texts)

        ids = [_chunk_id(str(file_path), c.chunk_index) for c in chunks]
        metadatas = [
            {
                "file_path": str(file_path),
                "start_line": c.start_line,
                "end_line": c.end_line,
                "language": c.language,
            }
            for c in chunks
        ]

        self._store.add_chunks(col, ids, embeddings, texts, metadatas)

        # Symbol index + dependency graph (best-effort; never block embedding).
        try:
            self._symbols.index_file(file_path, project_root=self._current_project)
        except Exception:
            pass

        return len(chunks)

    def delete_file(self, file_path: str | Path) -> None:
        col = self._require_project()
        self._store.delete_by_file(col, str(file_path))
        try:
            self._symbols.remove_file(str(file_path))
        except Exception:
            pass

    def clear_collection(self) -> None:
        if self._current_project:
            self._store.delete_collection(self._current_project)
            self._store.get_or_create_collection(self._current_project)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def query(self, question: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """Return top_k most relevant chunks for the question."""
        col = self._require_project()
        k = top_k or settings.retrieval_top_k
        vec = embed_query(question)
        return self._store.query(col, vec, top_k=k)

    def format_context(
        self, results: list[dict[str, Any]], max_tokens: int = 1500
    ) -> str:
        """Format retrieved chunks into a context string, respecting token budget."""
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        parts: list[str] = []
        used = 0
        for r in results:
            meta = r.get("metadata", {})
            header = f"# {meta.get('file_path', '?')} L{meta.get('start_line', '?')}-{meta.get('end_line', '?')}"
            block = f"{header}\n{r['content']}"
            tokens = len(enc.encode(block))
            if used + tokens > max_tokens:
                break
            parts.append(block)
            used += tokens
        return "\n\n".join(parts)

    def stats(self) -> dict[str, Any]:
        col = self._require_project()
        return {
            "project": self._current_project,
            "collection": col.name,
            "chunks": self._store.count(col),
        }


# Module-level singleton
retriever = Retriever()
