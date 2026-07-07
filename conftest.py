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
