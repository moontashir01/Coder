"""Step 12 / A1 — importing the core modules must not create on-disk state.

Constructing the ChromaDB client or opening .symbols.db is deferred behind
get_vector_store()/get_symbol_index(); merely importing the package (or the
modules that used to hold eager singletons) writes nothing to the cwd.
"""
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_import_creates_no_chroma_or_symbols_db(tmp_path):
    code = (
        "import app\n"
        "import app.agent.core\n"
        "import app.rag.retriever\n"
        "import app.database.vector_store\n"
        "import app.rag.symbols\n"
        "import app.tools.symbols_tool\n"
        "import app.agent.tool_registry\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / ".chroma_db").exists()
    assert not (tmp_path / ".symbols.db").exists()


def test_factories_are_cached_singletons():
    from app.agent.tool_registry import get_registry
    from app.database.vector_store import get_vector_store
    from app.rag.retriever import get_retriever
    from app.rag.symbols import get_symbol_index

    assert get_registry() is get_registry()
    assert get_retriever() is get_retriever()
    # These touch disk, so only assert identity once each is built.
    assert get_symbol_index() is get_symbol_index()
    assert get_vector_store() is get_vector_store()
