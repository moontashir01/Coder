"""Tests for the skills system: SKILL.md parsing, loader, matcher.

Matching is tested in keyword-only mode (use_embeddings=False) so Ollama is
never contacted.
"""
import pytest

from app.skills.loader import SkillLoader, SkillDefinition, _parse_skill_md
from app.skills.matcher import (
    match_skills,
    build_skills_context,
    _keyword_score,
)


SKILL_MD = """# Skill Name: FastAPI Builder

## Description
Helps build FastAPI applications with routing and dependency injection.

## Trigger Keywords
fastapi, api, rest, endpoint

## Instructions
1. Use APIRouter for grouping.
2. Use Pydantic models for schemas.
"""


@pytest.fixture
def skills_dir(tmp_path):
    skill_folder = tmp_path / "fastapi_builder"
    skill_folder.mkdir()
    (skill_folder / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_skill_md(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL_MD, encoding="utf-8")
    skill = _parse_skill_md(path)
    assert skill is not None
    assert skill.name == "FastAPI Builder"
    assert "FastAPI applications" in skill.description
    assert "fastapi" in skill.trigger_keywords
    assert "api" in skill.trigger_keywords
    assert "APIRouter" in skill.instructions


def test_parse_skill_md_empty_returns_none(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("# Just a heading\n", encoding="utf-8")
    assert _parse_skill_md(path) is None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_loader_discovers_skill(skills_dir):
    loader = SkillLoader(skills_dir=skills_dir)
    count = loader.load_all()
    assert count == 1
    skills = loader.list_skills()
    assert skills[0].name == "FastAPI Builder"


def test_loader_get_by_key_and_name(skills_dir):
    loader = SkillLoader(skills_dir=skills_dir)
    loader.load_all()
    assert loader.get("fastapi_builder") is not None          # directory key
    assert loader.get("FastAPI Builder") is not None           # display name
    assert loader.get("fastapi builder") is not None           # case-insensitive
    assert loader.get("does_not_exist") is None


def test_loader_enable_disable(skills_dir):
    loader = SkillLoader(skills_dir=skills_dir)
    loader.load_all()
    assert len(loader.enabled_skills()) == 1

    assert loader.disable("fastapi_builder") is True
    assert len(loader.enabled_skills()) == 0

    assert loader.enable("fastapi_builder") is True
    assert len(loader.enabled_skills()) == 1

    assert loader.disable("missing") is False


def test_loader_missing_dir_returns_zero(tmp_path):
    loader = SkillLoader(skills_dir=tmp_path / "nope")
    assert loader.load_all() == 0


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def _skill(**kw):
    defaults = dict(
        name="S",
        description="d",
        trigger_keywords=["fastapi", "api"],
        instructions="do the thing",
        source_path=None,
    )
    defaults.update(kw)
    return SkillDefinition(**defaults)


def test_keyword_score():
    skill = _skill(trigger_keywords=["fastapi", "api"])
    assert _keyword_score("build a fastapi api", skill) == 1.0
    assert _keyword_score("nothing relevant", skill) == 0.0
    assert _keyword_score("no keywords here", _skill(trigger_keywords=[])) == 0.0


def test_match_skills_keyword_only(skills_dir):
    loader = SkillLoader(skills_dir=skills_dir)
    loader.load_all()
    matched = match_skills("build a fastapi endpoint", loader, use_embeddings=False)
    assert len(matched) == 1
    assert matched[0].name == "FastAPI Builder"


def test_match_skills_below_threshold(skills_dir):
    loader = SkillLoader(skills_dir=skills_dir)
    loader.load_all()
    matched = match_skills("how do I bake bread", loader, use_embeddings=False)
    assert matched == []


def test_match_skills_respects_disabled(skills_dir):
    loader = SkillLoader(skills_dir=skills_dir)
    loader.load_all()
    loader.disable("fastapi_builder")
    matched = match_skills("build a fastapi endpoint", loader, use_embeddings=False)
    assert matched == []


def test_match_skills_caps_at_two(tmp_path):
    # create three matching skills
    for i in range(3):
        d = tmp_path / f"skill{i}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"# Skill Name: S{i}\n\n## Description\nApi tool {i}\n\n"
            f"## Trigger Keywords\napi\n\n## Instructions\nstep {i}\n",
            encoding="utf-8",
        )
    loader = SkillLoader(skills_dir=tmp_path)
    loader.load_all()
    matched = match_skills("use the api", loader, use_embeddings=False)
    assert len(matched) <= 2


def test_build_skills_context():
    skills = [_skill(name="Alpha", instructions="alpha steps"),
              _skill(name="Beta", instructions="beta steps")]
    ctx = build_skills_context(skills)
    assert "Alpha" in ctx
    assert "alpha steps" in ctx
    assert "Beta" in ctx


def test_build_skills_context_empty():
    assert build_skills_context([]) == ""
