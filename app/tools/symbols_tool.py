"""Tools exposing the symbol index to the agent.

Lets a small model answer "where is X defined?" / "where is X used?" with an
exact file:line instead of guessing from RAG chunks. Handlers follow the
universal tool contract: return {"success": bool, "result": str, "error": str|None}.
"""

from app.rag.symbols import get_symbol_index

# Tests monkeypatch this module attribute to inject an in-memory index; when it
# is None (normal use) we lazily resolve the shared singleton, so importing this
# module doesn't open .symbols.db (Step 12 / A1).
symbol_index = None


def _index():
    return symbol_index if symbol_index is not None else get_symbol_index()


def _ok(result: str) -> dict:
    return {"success": True, "result": result, "error": None}


def _fail(error: str) -> dict:
    return {"success": False, "result": "", "error": error}


def find_symbol(name: str) -> dict:
    """Locate where a function/class/method named `name` is defined."""
    idx = _index()
    try:
        hits = idx.lookup(name)
    except Exception as e:  # pragma: no cover - defensive
        return _fail(f"symbol lookup failed: {e}")

    if not hits:
        near = idx.search(name, limit=5)
        if near:
            names = ", ".join(sorted({h["name"] for h in near}))
            return _ok(f"No symbol named '{name}'. Similar: {names}")
        return _ok(f"No symbol named '{name}' found in the index.")

    lines = [f"{len(hits)} definition(s) of '{name}':"]
    for h in hits:
        loc = f"{h['file_path']}:{h['start_line']}"
        parent = f" (in {h['parent']})" if h.get("parent") else ""
        lines.append(f"  - {h['kind']} {h['name']}{parent} @ {loc}")
    return _ok("\n".join(lines))


def find_references(name: str) -> dict:
    """List call/usage sites of `name` across the indexed project."""
    idx = _index()
    try:
        refs = idx.references(name)
    except Exception as e:  # pragma: no cover - defensive
        return _fail(f"reference lookup failed: {e}")

    if not refs:
        return _ok(f"No references to '{name}' found in the index.")

    lines = [f"{len(refs)} reference(s) to '{name}':"]
    for r in refs:
        lines.append(f"  - {r['file_path']}:{r['line']}")
    return _ok("\n".join(lines))
