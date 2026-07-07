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


def _file_content_hash(path: str | Path) -> str | None:
    """SHA-256 of a file's raw bytes, or None if it can't be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _gitignore_spec(root: Path):
    """Build a PathSpec from the project's root .gitignore, or None if there
    isn't one (or pathspec is unavailable). Matching is best-effort."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return None
    try:
        import pathspec

        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
        # "gitignore" is the current factory name; older pathspec only has the
        # (now-deprecated) "gitwildmatch" alias — fall back to it.
        try:
            return pathspec.PathSpec.from_lines("gitignore", lines)
        except (KeyError, ValueError, LookupError):
            return pathspec.PathSpec.from_lines("gitwildmatch", lines)
    except Exception:
        return None


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
        """Chunk, embed, and store all indexable files under project_path.

        Incremental (Step 2 / P1): files whose content hash matches what's
        already stored are skipped, so re-loading an unchanged repo re-embeds
        nothing. Only changed/new files are re-chunked and re-embedded.
        """
        self._current_project = str(project_path)
        col = self._store.get_or_create_collection(project_path)
        root = Path(project_path)

        spec = _gitignore_spec(root)
        size_cap = settings.max_index_file_bytes
        files = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in _INDEXABLE_SUFFIXES:
                continue
            if any(part.startswith(".") for part in p.parts):
                continue
            if "__pycache__" in p.parts or "node_modules" in p.parts:
                continue
            # .gitignore (P4): skip vendored/generated files the repo ignores.
            if spec is not None:
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    rel = None
                if rel is not None and spec.match_file(rel):
                    continue
            # Size cap (C4): don't try to embed huge/generated blobs.
            try:
                if p.stat().st_size > size_cap:
                    continue
            except OSError:
                continue
            files.append(p)

        # Content hashes of already-indexed files, to skip unchanged ones.
        # getattr keeps the retriever working with minimal test doubles that
        # don't implement get_file_hashes (they just re-index everything).
        existing_hashes: dict[str, str] = {}
        get_hashes = getattr(self._store, "get_file_hashes", None)
        if get_hashes is not None:
            try:
                existing_hashes = get_hashes(col) or {}
            except Exception:
                existing_hashes = {}

        total_chunks = 0
        indexed = 0
        skipped = 0
        for file_path in files:
            content_hash = _file_content_hash(file_path)
            if (
                content_hash is not None
                and existing_hashes.get(str(file_path)) == content_hash
            ):
                skipped += 1
                continue
            total_chunks += self._index_single_file(col, file_path, content_hash)
            indexed += 1

        return {
            "files": len(files),
            "chunks": total_chunks,
            "indexed": indexed,
            "skipped": skipped,
        }

    def index_file(self, file_path: str | Path) -> int:
        """Re-embed a single file (call after edits)."""
        col = self._require_project()
        return self._index_single_file(col, Path(file_path))

    def _index_single_file(
        self, col, file_path: Path, content_hash: str | None = None
    ) -> int:
        chunks: list[Chunk] = chunk_file(file_path)
        if not chunks:
            return 0

        if content_hash is None:
            content_hash = _file_content_hash(file_path)

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
                "content_hash": content_hash or "",
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
