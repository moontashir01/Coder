"""Tests for the AST-based symbol index + dependency graph (slice 1).

Pure stdlib `ast` extraction — no Ollama, no tree-sitter, fully offline.
"""

from pathlib import Path

import pytest

from app.rag.symbols import SymbolIndex, extract_symbols


def _write(tmp_path: Path, rel: str, src: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# extract_symbols
# ----------------------------------------------------------------------


def test_extract_functions_classes_methods(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
import os
from pathlib import Path

def top_level():
    return 1

class Service:
    def handle(self):
        return top_level()
""",
    )
    fs = extract_symbols(f)
    by_name = {s.name: s for s in fs.symbols}

    assert by_name["top_level"].kind == "function"
    assert by_name["top_level"].parent is None
    assert by_name["Service"].kind == "class"
    assert by_name["handle"].kind == "method"
    assert by_name["handle"].parent == "Service"
    # line numbers are 1-based and point at the def line
    assert by_name["top_level"].start_line == 5


def test_extract_imports(tmp_path):
    f = _write(
        tmp_path, "mod.py", "import os\nfrom pathlib import Path\nimport a.b.c\n"
    )
    fs = extract_symbols(f)
    assert "os" in fs.imports
    assert "pathlib" in fs.imports
    assert "a.b.c" in fs.imports


def test_extract_references_call_sites(tmp_path):
    f = _write(
        tmp_path,
        "mod.py",
        """
def authenticate_user():
    pass

def login():
    authenticate_user()
    obj.authenticate_user()
""",
    )
    fs = extract_symbols(f)
    assert "authenticate_user" in {r.name for r in fs.references}


def test_syntax_error_file_is_safe(tmp_path):
    f = _write(tmp_path, "broken.py", "def oops(:\n    pass\n")
    fs = extract_symbols(f)  # must not raise
    assert fs.symbols == []
    assert fs.imports == []


# ----------------------------------------------------------------------
# SymbolIndex
# ----------------------------------------------------------------------


@pytest.fixture
def index():
    idx = SymbolIndex(db_path=":memory:")
    yield idx
    idx.close()


def test_index_and_lookup_definition(tmp_path, index):
    f = _write(tmp_path, "auth.py", "def authenticate_user():\n    return True\n")
    index.index_file(f)

    hits = index.lookup("authenticate_user")
    assert len(hits) == 1
    assert hits[0]["kind"] == "function"
    assert hits[0]["file_path"] == str(f)
    assert hits[0]["start_line"] == 1


def test_search_by_prefix(tmp_path, index):
    f = _write(
        tmp_path,
        "auth.py",
        "def authenticate_user():\n    pass\n\ndef authorize():\n    pass\n",
    )
    index.index_file(f)
    names = {h["name"] for h in index.search("auth")}
    assert names == {"authenticate_user", "authorize"}


def test_find_references_across_files(tmp_path, index):
    a = _write(tmp_path, "auth.py", "def authenticate_user():\n    pass\n")
    b = _write(
        tmp_path,
        "login.py",
        "from auth import authenticate_user\n\ndef login():\n    authenticate_user()\n",
    )
    index.index_file(a)
    index.index_file(b)

    refs = index.references("authenticate_user")
    ref_files = {r["file_path"] for r in refs}
    assert str(b) in ref_files


def test_dependency_graph_edges(tmp_path, index):
    root = tmp_path
    _write(root, "auth.py", "def authenticate_user():\n    pass\n")
    main = _write(root, "main.py", "import auth\n\nauth.authenticate_user()\n")
    index.index_file(root / "auth.py", project_root=root)
    index.index_file(main, project_root=root)

    deps = index.dependencies(str(main))
    assert str(root / "auth.py") in deps

    dependents = index.dependents(str(root / "auth.py"))
    assert str(main) in dependents


def test_reindex_replaces_no_duplicates(tmp_path, index):
    f = _write(tmp_path, "mod.py", "def a():\n    pass\n")
    index.index_file(f)
    # Edit: rename a -> b, re-index the same path
    f.write_text("def b():\n    pass\n", encoding="utf-8")
    index.index_file(f)

    assert index.lookup("a") == []
    assert len(index.lookup("b")) == 1


def test_remove_file(tmp_path, index):
    f = _write(tmp_path, "mod.py", "def gone():\n    pass\n")
    index.index_file(f)
    index.remove_file(str(f))
    assert index.lookup("gone") == []


# ----------------------------------------------------------------------
# Retriever integration — indexing populates the symbol index too
# ----------------------------------------------------------------------


@pytest.fixture
def fake_embed_ret(monkeypatch):
    import app.rag.retriever as ret_mod

    monkeypatch.setattr(
        ret_mod, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    monkeypatch.setattr(ret_mod, "embed_query", lambda q: [1.0, 0.0, 0.0])


def test_retriever_populates_symbol_index(tmp_path, index, fake_embed_ret):
    from app.rag.retriever import Retriever
    from tests.test_rag import _FakeStore

    _write(tmp_path, "auth.py", "def authenticate_user():\n    return True\n")
    retr = Retriever(store=_FakeStore(), symbol_index=index)
    retr.index_project(tmp_path)

    hits = index.lookup("authenticate_user")
    assert len(hits) == 1
    assert hits[0]["kind"] == "function"


def test_retriever_delete_file_clears_symbols(tmp_path, index, fake_embed_ret):
    from app.rag.retriever import Retriever
    from tests.test_rag import _FakeStore

    f = _write(tmp_path, "gone.py", "def gone():\n    pass\n")
    retr = Retriever(store=_FakeStore(), symbol_index=index)
    retr.index_project(tmp_path)
    assert index.lookup("gone")

    retr.delete_file(f)
    assert index.lookup("gone") == []


# ----------------------------------------------------------------------
# find_symbol / find_references tools
# ----------------------------------------------------------------------


def test_find_symbol_tool_reports_location(tmp_path, monkeypatch):
    import app.tools.symbols_tool as st

    idx = SymbolIndex(db_path=":memory:")
    f = _write(tmp_path, "auth.py", "def authenticate_user():\n    return True\n")
    idx.index_file(f)
    monkeypatch.setattr(st, "symbol_index", idx)

    out = st.find_symbol(name="authenticate_user")
    assert out["success"] is True
    assert out["error"] is None
    assert "authenticate_user" in out["result"]
    assert "auth.py" in out["result"]


def test_find_symbol_tool_missing_is_not_error(monkeypatch):
    import app.tools.symbols_tool as st

    monkeypatch.setattr(st, "symbol_index", SymbolIndex(db_path=":memory:"))
    out = st.find_symbol(name="does_not_exist")
    assert out["success"] is True
    assert "no symbol" in out["result"].lower()


def test_find_references_tool(tmp_path, monkeypatch):
    import app.tools.symbols_tool as st

    idx = SymbolIndex(db_path=":memory:")
    _write(tmp_path, "auth.py", "def authenticate_user():\n    pass\n")
    b = _write(tmp_path, "login.py", "def login():\n    authenticate_user()\n")
    idx.index_file(tmp_path / "auth.py")
    idx.index_file(b)
    monkeypatch.setattr(st, "symbol_index", idx)

    out = st.find_references(name="authenticate_user")
    assert out["success"] is True
    assert "login.py" in out["result"]


def test_find_symbol_registered_as_builtin():
    from app.agent.tool_registry import create_registry

    reg = create_registry()
    assert "find_symbol" in reg.names()
    assert "find_references" in reg.names()
