from typing import Iterator

import httpx
from langchain_ollama import ChatOllama

from config.settings import settings


def get_llm(temperature: float = 0.1, json_mode: bool = False) -> ChatOllama:
    """Factory for ChatOllama instances.

    Args:
        temperature: 0.1 for coding/tool tasks, 0.7 for planning/explanations.
        json_mode: When True, constrains output to valid JSON via Ollama format param.
    """
    kwargs: dict = {
        "model": settings.llm_model,
        "base_url": settings.ollama_base_url,
        "temperature": temperature,
        "timeout": 30,
    }
    if json_mode:
        kwargs["format"] = "json"
    return ChatOllama(**kwargs)


def get_streaming_llm(temperature: float = 0.1) -> ChatOllama:
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=temperature,
        streaming=True,
        timeout=30,
    )


def test_connection() -> None:
    """Ping Ollama and verify the required models are available.

    Raises:
        RuntimeError: if Ollama is not reachable or models are missing.
    """
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        resp.raise_for_status()
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot reach Ollama at {settings.ollama_base_url}. "
            "Run `ollama serve` and try again."
        )
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Ollama returned HTTP {e.response.status_code}: {e}")

    tags = resp.json()
    available = {m["name"] for m in tags.get("models", [])}

    missing = []
    for model in (settings.llm_model, settings.embedding_model):
        # Ollama tags may include digest suffix; match on name prefix
        if not any(m == model or m.startswith(f"{model}:") for m in available):
            missing.append(model)

    if missing:
        raise RuntimeError(
            f"Required Ollama models not pulled: {missing}. "
            f"Run: ollama pull {' '.join(missing)}"
        )
