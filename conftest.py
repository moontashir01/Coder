"""Pytest bootstrap: put the project root on sys.path so `app` and `config` import."""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def _isolate_embed_cache(tmp_path_factory, monkeypatch):
    """Keep the persistent embedding cache out of the repo cwd during tests,
    and give each test a fresh directory so cache state never leaks between them."""
    monkeypatch.setattr(
        settings, "embed_cache_dir", tmp_path_factory.mktemp("embed_cache")
    )


@pytest.fixture(autouse=True)
def _no_intent_check(monkeypatch):
    """Default the intent check OFF in tests — it must be opted into explicitly.

    It runs inside `_verify_and_repair`, so it fires on EVERY file-writing test,
    and it calls `_llm_edit` — which the file-flow tests script only when they
    exercise a surgical edit. Left on, the rest reach a real ChatOllama and the
    suite stops being offline (measured: 28s → 611s, all still "passing", which
    is exactly how a silent network dependency gets into a test suite).

    `tests/test_intent.py` turns it back on for the tests that are about it.
    """
    monkeypatch.setattr(settings, "check_intent", False)


@pytest.fixture(autouse=True)
def _no_blueprint(monkeypatch):
    """Default the blueprint stage OFF in tests — same reason as `_no_intent_check`.

    `expand_requirements` and `blueprint_smoke_test` ship ON (Phase 0 of
    docs/fullstack-web-plan.md), but `should_blueprint()` matches ordinary
    file-writing prompts like "Create an index.html file for a simple landing
    page" (evals/tasks.py::create_html_page, and several tests). Left on, those
    tests would call `_expand_requirements` → a real ChatOllama, and the suite
    would silently stop being offline while still reporting green — exactly the
    trap `_no_intent_check` documents (28s → 611s, all still "passing").
    The smoke test is worse: it would *execute* a scripted-LLM file as a server
    subprocess.

    `tests/test_blueprint.py` and `tests/test_evals.py` opt back in explicitly.

    `schema_first` (Phase C) is defaulted off for a sharper version of the same
    reason: it runs BEFORE `_expand_requirements`, so a test that opts back into
    the blueprint stage and scripts/monkeypatches only that method would still
    reach a real ChatOllama through `_extract_schema`. A test that wants the
    schema stage must script it.
    """
    monkeypatch.setattr(settings, "expand_requirements", False)
    monkeypatch.setattr(settings, "blueprint_smoke_test", False)
    monkeypatch.setattr(settings, "schema_first", False)
    # Phase W7's visual critique — the same trap a third time. It fires inside
    # the smoke stage, calls the VISION model, and ships off precisely so a
    # machine without one behaves as before; a test that turns the smoke test
    # back on must not silently acquire an Ollama dependency with it.
    monkeypatch.setattr(settings, "check_visual", False)
    # Same trap again for Phase B's tier-2 classifier: it fires when the regex
    # gate MISSES, which is most test prompts, and it runs before any method a
    # blueprint test would think to patch.
    monkeypatch.setattr(settings, "web_intent_fallback", False)
