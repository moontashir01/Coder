import logging
import re
from pathlib import Path
from typing import Any

import chromadb
from chromadb import Collection

from config.settings import settings

logger = logging.getLogger(__name__)


def _chroma_client() -> chromadb.PersistentClient:
    persist_dir = Path(settings.chroma_persist_dir).resolve()
    persist_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(persist_dir))


def _safe_name(project_path: str | Path) -> str:
    """Convert a project path to a valid ChromaDB collection name."""
    name = Path(project_path).name
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    # ChromaDB requires 3-63 chars, must start/end with alphanumeric
    name = name.strip("_-")[:60] or "project"
    return name


class VectorStore:
    """Thin wrapper around a ChromaDB PersistentClient."""

    def __init__(self) -> None:
        self._client = _chroma_client()

    def get_or_create_collection(self, project_path: str | Path) -> Collection:
        name = _safe_name(project_path)
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def delete_collection(self, project_path: str | Path) -> None:
        name = _safe_name(project_path)
        try:
            self._client.delete_collection(name)
        except Exception as e:
            # Best-effort: a missing/already-deleted collection is fine, but
            # log so a real failure (locked DB, corruption) is observable.
            logger.debug("delete_collection(%s) failed: %s", name, e)

    def list_collections(self) -> list[str]:
        return [c.name for c in self._client.list_collections()]

    def add_chunks(
        self,
        collection: Collection,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def delete_by_file(self, collection: Collection, file_path: str) -> None:
        collection.delete(where={"file_path": file_path})

    def get_file_hashes(self, collection: Collection) -> dict[str, str]:
        """Map each indexed file_path to its stored content_hash.

        Used by incremental indexing (Step 2 / P1) to skip files whose content
        is unchanged. Files indexed before content hashing was added simply
        won't appear here, so they get re-indexed once.
        """
        try:
            got = collection.get(include=["metadatas"])
        except Exception as e:
            logger.debug("get_file_hashes failed; will re-index everything: %s", e)
            return {}
        out: dict[str, str] = {}
        for meta in got.get("metadatas") or []:
            if not meta:
                continue
            fp = meta.get("file_path")
            h = meta.get("content_hash")
            if fp and h:
                out[str(fp)] = str(h)
        return out

    def query(
        self,
        collection: Collection,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        chunks: list[dict[str, Any]] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            chunks.append({"content": doc, "metadata": meta, "distance": dist})
        return chunks

    def count(self, collection: Collection) -> int:
        return collection.count()


# Lazy singleton (Step 12 / A1): constructing a VectorStore builds the ChromaDB
# PersistentClient, which creates .chroma_db/ on disk — so we DON'T build it at
# import time. get_vector_store() constructs it on first real use and caches it.
_vector_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
