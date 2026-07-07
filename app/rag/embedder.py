import hashlib

from langchain_ollama import OllamaEmbeddings

from config.settings import settings

# Simple file-based cache: hash(text) → embedding vector
_cache: dict[str, list[float]] = {}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _get_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        model=settings.embedding_model,
        base_url=settings.ollama_base_url,
    )


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, using in-process cache for unchanged content."""
    results: list[list[float] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    for i, text in enumerate(texts):
        key = _hash(text)
        if key in _cache:
            results[i] = _cache[key]
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    if uncached_texts:
        vecs = _get_embeddings().embed_documents(uncached_texts)
        for idx, text, vec in zip(uncached_indices, uncached_texts, vecs):
            _cache[_hash(text)] = vec
            results[idx] = vec

    return results  # type: ignore[return-value]


def embed_query(text: str) -> list[float]:
    """Embed a single query string."""
    key = _hash(text)
    if key not in _cache:
        _cache[key] = _get_embeddings().embed_query(text)
    return _cache[key]


def clear_cache() -> None:
    _cache.clear()
