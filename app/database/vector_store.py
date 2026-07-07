import re
from pathlib import Path
from typing import Any

import chromadb
from chromadb import Collection

from config.settings import settings


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
        except Exception:
            pass

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


# Module-level singleton
vector_store = VectorStore()
