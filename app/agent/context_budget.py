"""Token-aware conversation-history trimming (roadmap Tier 2 #5).

``conversation_buffer_size`` caps history by *message count*; it says nothing
about tokens, so a handful of long turns can still overflow the model's context
window. These pure helpers trim history against a token budget
(``settings.max_context_tokens``) before it is sent, dropping the OLDEST turns
first and always preserving the system prompt and the latest user message.

Offline: uses the same local tiktoken encoding as the chunker.
"""

from __future__ import annotations

import tiktoken
from langchain_core.messages import BaseMessage

_enc = tiktoken.get_encoding("cl100k_base")

# Rough per-message framing overhead (role markers, separators). The model's
# real tokenizer differs from cl100k_base, so this whole budget is an estimate
# — deliberately a little conservative so we under-fill rather than overflow.
_PER_MESSAGE_OVERHEAD = 4


def count_tokens(text: str) -> int:
    """Token count of ``text`` under the local cl100k_base encoding."""
    return len(_enc.encode(text or ""))


def _message_tokens(message: BaseMessage) -> int:
    return count_tokens(str(message.content)) + _PER_MESSAGE_OVERHEAD


def trim_history_to_budget(
    system_text: str,
    history: list[BaseMessage],
    latest_text: str,
    max_tokens: int,
) -> list[BaseMessage]:
    """Return the newest suffix of ``history`` that fits within ``max_tokens``.

    The system prompt and the latest user message are the irreducible core of
    the turn and are always counted but never dropped — if they alone exceed the
    budget, history is trimmed to empty (never raises). History order is
    preserved; only the oldest messages are removed.
    """
    fixed = (
        count_tokens(system_text)
        + count_tokens(latest_text)
        + 2 * _PER_MESSAGE_OVERHEAD
    )
    kept = list(history)
    running = fixed + sum(_message_tokens(m) for m in kept)
    while kept and running > max_tokens:
        running -= _message_tokens(kept.pop(0))
    return kept
