import hashlib
import json
import os
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from langchain_ollama import OllamaEmbeddings

from config.settings import settings

# Two-tier embedding cache keyed by SHA-256(text) → vector:
#   1. an in-process LRU dict (fast path, bounded by _MEMORY_LRU_MAX);
#   2. a persistent on-disk cache under settings.embed_cache_dir, one JSON file
#      per key, so embeddings survive process restarts and the disk layer is
#      pruned LRU-style to settings.max_embed_cache_entries.
_MEMORY_LRU_MAX = 4096
_cache: "OrderedDict[str, list[float]]" = OrderedDict()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@lru_cache(maxsize=1)
def _get_embeddings() -> OllamaEmbeddings:
    """Memoized Ollama client — building it per call re-opens the HTTP session."""
    return OllamaEmbeddings(
        model=settings.embedding_model,
        base_url=settings.ollama_base_url,
    )


# ---------------------------------------------------------------------------
# In-memory LRU layer
# ---------------------------------------------------------------------------


def _mem_get(key: str) -> list[float] | None:
    vec = _cache.get(key)
    if vec is not None:
        _cache.move_to_end(key)
    return vec


def _mem_put(key: str, vec: list[float]) -> None:
    _cache[key] = vec
    _cache.move_to_end(key)
    while len(_cache) > _MEMORY_LRU_MAX:
        _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Persistent on-disk layer (best-effort — a cache miss is never fatal)
# ---------------------------------------------------------------------------


def _cache_dir() -> Path | None:
    try:
        d = Path(settings.embed_cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _disk_get(key: str) -> list[float] | None:
    d = _cache_dir()
    if d is None:
        return None
    f = d / f"{key}.json"
    try:
        vec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        os.utime(f, None)  # touch so LRU pruning treats it as recently used
    except OSError:
        pass
    return vec


def _disk_put(key: str, vec: list[float]) -> None:
    d = _cache_dir()
    if d is None:
        return
    try:
        (d / f"{key}.json").write_text(json.dumps(vec), encoding="utf-8")
    except OSError:
        return
    _prune_disk(d)


def _prune_disk(d: Path) -> None:
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return
    excess = len(files) - settings.max_embed_cache_entries
    if excess <= 0:
        return
    files.sort(key=lambda f: f.stat().st_mtime)
    for old in files[:excess]:
        try:
            old.unlink()
        except OSError:
            pass  # pruning is best-effort


def _get_cached(key: str) -> list[float] | None:
    vec = _mem_get(key)
    if vec is not None:
        return vec
    vec = _disk_get(key)
    if vec is not None:
        _mem_put(key, vec)
    return vec


def _store_cached(key: str, vec: list[float]) -> None:
    _mem_put(key, vec)
    _disk_put(key, vec)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, serving unchanged content from the cache."""
    results: list[list[float] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    for i, text in enumerate(texts):
        vec = _get_cached(_hash(text))
        if vec is not None:
            results[i] = vec
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    if uncached_texts:
        vecs = _get_embeddings().embed_documents(uncached_texts)
        for idx, text, vec in zip(uncached_indices, uncached_texts, vecs):
            _store_cached(_hash(text), vec)
            results[idx] = vec

    return results  # type: ignore[return-value]


def embed_query(text: str) -> list[float]:
    """Embed a single query string."""
    key = _hash(text)
    vec = _get_cached(key)
    if vec is None:
        vec = _get_embeddings().embed_query(text)
        _store_cached(key, vec)
    return vec


def clear_cache() -> None:
    """Drop both cache layers (memory and disk)."""
    _cache.clear()
    d = _cache_dir()
    if d is None:
        return
    for f in d.glob("*.json"):
        try:
            f.unlink()
        except OSError:
            pass
