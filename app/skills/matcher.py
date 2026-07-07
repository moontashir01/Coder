"""Match user queries to relevant skills and inject their instructions."""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.skills.loader import SkillDefinition, SkillLoader

MAX_INJECTED_SKILLS = 2
KEYWORD_WEIGHT = 0.5
EMBEDDING_WEIGHT = 0.5
THRESHOLD = 0.25   # minimum combined score to inject a skill


def _keyword_score(query: str, skill: SkillDefinition) -> float:
    """Fraction of skill trigger keywords found in the query (case-insensitive)."""
    if not skill.trigger_keywords:
        return 0.0
    query_lower = query.lower()
    hits = sum(
        1 for kw in skill.trigger_keywords
        if kw.lower() in query_lower
    )
    return hits / len(skill.trigger_keywords)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_text(text: str) -> list[float] | None:
    try:
        from app.rag.embedder import embed_query
        return embed_query(text)
    except Exception:
        return None


def match_skills(
    query: str,
    loader: SkillLoader,
    use_embeddings: bool = True,
) -> list[SkillDefinition]:
    """Return up to MAX_INJECTED_SKILLS skills relevant to the query."""
    candidates = loader.enabled_skills()
    if not candidates:
        return []

    query_vec: list[float] | None = None
    if use_embeddings:
        query_vec = _embed_text(query)

    scored: list[tuple[float, SkillDefinition]] = []

    for skill in candidates:
        kw_score = _keyword_score(query, skill)

        emb_score = 0.0
        if query_vec is not None:
            skill_text = f"{skill.name}. {skill.description}. {' '.join(skill.trigger_keywords)}"
            skill_vec = _embed_text(skill_text)
            if skill_vec:
                # cosine similarity is in [-1, 1]; normalise to [0, 1]
                emb_score = (_cosine(query_vec, skill_vec) + 1) / 2

        if use_embeddings and query_vec is not None:
            combined = KEYWORD_WEIGHT * kw_score + EMBEDDING_WEIGHT * emb_score
        else:
            # Fallback: keywords only (embeddings unavailable)
            combined = kw_score

        if combined >= THRESHOLD:
            scored.append((combined, skill))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [skill for _, skill in scored[:MAX_INJECTED_SKILLS]]


def build_skills_context(skills: list[SkillDefinition]) -> str:
    """Format matched skills into an injection block."""
    if not skills:
        return ""
    parts: list[str] = []
    for skill in skills:
        parts.append(f"### Skill: {skill.name}\n{skill.instructions}")
    return "\n\n".join(parts)
