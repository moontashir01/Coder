"""Symbol index and dependency graph.

Python is parsed with stdlib `ast` — deterministic, offline, and more accurate
than tree-sitter for Python (real names, imports, and call sites, plus the
import→file dependency edges the graph depends on). Other languages (JS/TS/
Go/Rust/Java/C/C++) are parsed with tree-sitter (Step 11 / A3), reusing the
parsers the chunker already pins (tree-sitter 0.21.3 + tree-sitter-languages
1.10.2). Files in an unsupported language degrade to no symbols, never crash.

Storage is a standalone synchronous sqlite3 DB (default `.symbols.db`). This
matches the *synchronous* retriever indexing path and avoids contending with
the async SQLAlchemy connection on `.coder.db`.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.rag.chunker import LANGUAGE_MAP
from config.settings import settings

try:
    from tree_sitter_languages import get_parser

    _TS_AVAILABLE = True
except Exception:  # pragma: no cover - environment without tree-sitter
    _TS_AVAILABLE = False


@dataclass
class Symbol:
    name: str
    kind: str  # "function" | "class" | "method"
    file_path: str
    start_line: int
    end_line: int
    parent: str | None  # enclosing class name, or None


@dataclass
class Reference:
    name: str
    line: int


@dataclass
class FileSymbols:
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # dotted module names
    references: list[Reference] = field(default_factory=list)
    # Raw import records kept for dependency resolution: (module, level)
    _import_records: list[tuple[str, int]] = field(default_factory=list)


# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------


class _Walker(ast.NodeVisitor):
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.result = FileSymbols()
        self._class_stack: list[str] = []

    def _add_func(self, node) -> None:
        parent = self._class_stack[-1] if self._class_stack else None
        kind = "method" if parent else "function"
        self.result.symbols.append(
            Symbol(
                name=node.name,
                kind=kind,
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                parent=parent,
            )
        )
        # Walk the body so nested defs / call sites are captured.
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_FunctionDef(self, node):  # noqa: N802
        self._add_func(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._add_func(node)

    def visit_ClassDef(self, node):  # noqa: N802
        self.result.symbols.append(
            Symbol(
                name=node.name,
                kind="class",
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                parent=self._class_stack[-1] if self._class_stack else None,
            )
        )
        self._class_stack.append(node.name)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._class_stack.pop()

    def visit_Import(self, node):  # noqa: N802
        for alias in node.names:
            self.result.imports.append(alias.name)
            self.result._import_records.append((alias.name, 0))

    def visit_ImportFrom(self, node):  # noqa: N802
        module = node.module or ""
        if module:
            self.result.imports.append(module)
        self.result._import_records.append((module, node.level or 0))

    def visit_Call(self, node):  # noqa: N802
        func = node.func
        if isinstance(func, ast.Name):
            self.result.references.append(Reference(func.id, node.lineno))
        elif isinstance(func, ast.Attribute):
            self.result.references.append(Reference(func.attr, node.lineno))
        self.generic_visit(node)


def _extract_symbols_py(file_path: str | Path) -> FileSymbols:
    """Python path: stdlib `ast` (names, imports, and call sites)."""
    path = Path(file_path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return FileSymbols()

    walker = _Walker(str(file_path))
    for node in ast.iter_child_nodes(tree):
        walker.visit(node)
    return walker.result


# ----------------------------------------------------------------------
# Tree-sitter path for non-Python languages (Step 11 / A3)
# ----------------------------------------------------------------------

# Definition node types → symbol kind, per tree-sitter language.
_TS_TYPESCRIPT_DEFS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}
_TS_JAVASCRIPT_DEFS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
}
_TS_DEFS: dict[str, dict[str, str]] = {
    "javascript": _TS_JAVASCRIPT_DEFS,
    "typescript": _TS_TYPESCRIPT_DEFS,
    "tsx": _TS_TYPESCRIPT_DEFS,
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
    },
    "java": {
        "method_declaration": "method",
        "constructor_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
    },
    "c": {"function_definition": "function", "struct_specifier": "struct"},
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
    },
}

# Kinds that open a scope, so nested methods record the enclosing name.
_TS_SCOPE_KINDS = {"class", "struct", "interface", "trait", "impl"}

_ID_TYPES = {
    "identifier",
    "type_identifier",
    "field_identifier",
    "property_identifier",
}
# Body/list nodes whose identifiers are members, not the definition's own name.
_SKIP_INTO = {
    "block",
    "statement_block",
    "field_declaration_list",
    "declaration_list",
    "class_body",
    "enum_body",
    "compound_statement",
    "parameter_list",
    "parameters",
    "formal_parameters",
}
_CALL_TYPES = {"call_expression", "method_invocation", "macro_invocation"}


def _node_text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _find_name(node) -> str | None:
    """The declared name of a definition node — the `name` field when present,
    else the first identifier reached without descending into bodies/params."""
    nm = node.child_by_field_name("name")
    if nm is not None and nm.type in _ID_TYPES:
        return _node_text(nm)
    for child in node.children:
        if child.type in _SKIP_INTO:
            continue
        if child.type in _ID_TYPES:
            return _node_text(child)
    for child in node.children:
        if child.type in _SKIP_INTO:
            continue
        got = _find_name(child)
        if got:
            return got
    return None


def _call_name(node) -> str | None:
    fn = node.child_by_field_name("function")
    if fn is None:
        fn = node.child_by_field_name("name")  # java method_invocation
    target = fn if fn is not None else node
    if target.type in _ID_TYPES:
        return _node_text(target)
    for field_name in ("property", "field", "name"):
        sub = target.child_by_field_name(field_name)
        if sub is not None and sub.type in _ID_TYPES:
            return _node_text(sub)
    ids = [c for c in target.children if c.type in _ID_TYPES]
    return _node_text(ids[-1]) if ids else None


def _extract_symbols_ts(source: str, language: str, file_path: str) -> FileSymbols:
    result = FileSymbols()
    try:
        parser = get_parser(language)
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        return result

    defs = _TS_DEFS.get(language, {})
    class_stack: list[str] = []

    def visit(node) -> None:
        pushed = False
        kind = defs.get(node.type)
        if kind:
            name = _find_name(node)
            if name:
                parent = class_stack[-1] if class_stack else None
                result.symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        file_path=file_path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent=parent,
                    )
                )
                if kind in _TS_SCOPE_KINDS:
                    class_stack.append(name)
                    pushed = True
        if node.type in _CALL_TYPES:
            cn = _call_name(node)
            if cn:
                result.references.append(Reference(cn, node.start_point[0] + 1))
        for child in node.children:
            visit(child)
        if pushed:
            class_stack.pop()

    visit(tree.root_node)
    return result


def extract_symbols(file_path: str | Path) -> FileSymbols:
    """Parse a file into symbols, imports, and references.

    Python uses stdlib `ast`; other supported languages use tree-sitter. Returns
    an empty FileSymbols on syntax errors, unreadable files, or unsupported
    languages — never raises.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _extract_symbols_py(path)

    language = LANGUAGE_MAP.get(suffix)
    if language and _TS_AVAILABLE and language in _TS_DEFS:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return FileSymbols()
        if not source.strip():
            return FileSymbols()
        return _extract_symbols_ts(source, language, str(file_path))

    return FileSymbols()


def _resolve_import(
    module: str, level: int, project_root: Path, importing_file: Path
) -> str | None:
    """Best-effort map a Python import to a file path inside project_root.

    Returns the path string (built from the given root, not resolved, so it
    matches how callers refer to files) or None for external/unresolvable
    modules.
    """
    if level and level > 0:
        base = importing_file.parent
        for _ in range(level - 1):
            base = base.parent
        parts = module.split(".") if module else []
    else:
        base = project_root
        parts = module.split(".") if module else []

    if not parts:
        return None

    candidate_dir = base
    for part in parts[:-1]:
        candidate_dir = candidate_dir / part
    last = parts[-1]

    module_file = candidate_dir / f"{last}.py"
    if module_file.exists():
        return str(module_file)
    pkg_init = candidate_dir / last / "__init__.py"
    if pkg_init.exists():
        return str(pkg_init)
    return None


# ----------------------------------------------------------------------
# Index
# ----------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    parent TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    file_path TEXT NOT NULL,
    module TEXT NOT NULL,
    resolved_path TEXT
);
CREATE TABLE IF NOT EXISTS refs (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_path);
CREATE INDEX IF NOT EXISTS idx_imports_resolved ON imports(resolved_path);
CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
"""


class SymbolIndex:
    """Synchronous sqlite3-backed symbol + dependency-graph index.

    Thread-safe: the singleton is hit from more than one thread (the retriever's
    indexing path and the ProjectWatcher's debounce Timer thread), so the single
    connection is opened with ``check_same_thread=False`` and every use of it is
    serialized by ``self._lock``.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = getattr(settings, "symbols_path", ".symbols.db")
        self._db_path = str(db_path)
        # RLock: index_file() takes the lock and then calls remove_file().
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- mutation -------------------------------------------------------

    def index_file(
        self, file_path: str | Path, project_root: str | Path | None = None
    ) -> int:
        """Extract and (re)store all symbols/imports/refs for one file.

        Replaces any existing rows for the file, so it is safe to call
        repeatedly for incremental re-indexing. Returns symbol count.
        """
        fp = str(file_path)
        fs = extract_symbols(file_path)
        root = Path(project_root) if project_root is not None else None
        import_rows = []
        for module, lvl in fs._import_records:
            resolved = (
                _resolve_import(module, lvl, root, Path(file_path))
                if root is not None
                else None
            )
            import_rows.append((fp, module, resolved))

        with self._lock:
            self.remove_file(fp)
            cur = self._conn.cursor()
            cur.executemany(
                "INSERT INTO symbols (name, kind, file_path, start_line, end_line, parent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (s.name, s.kind, s.file_path, s.start_line, s.end_line, s.parent)
                    for s in fs.symbols
                ],
            )
            cur.executemany(
                "INSERT INTO imports (file_path, module, resolved_path) VALUES (?, ?, ?)",
                import_rows,
            )
            cur.executemany(
                "INSERT INTO refs (file_path, name, line) VALUES (?, ?, ?)",
                [(fp, r.name, r.line) for r in fs.references],
            )
            self._conn.commit()
        return len(fs.symbols)

    def remove_file(self, file_path: str | Path) -> None:
        fp = str(file_path)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM symbols WHERE file_path = ?", (fp,))
            cur.execute("DELETE FROM imports WHERE file_path = ?", (fp,))
            cur.execute("DELETE FROM refs WHERE file_path = ?", (fp,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM symbols")
            cur.execute("DELETE FROM imports")
            cur.execute("DELETE FROM refs")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- queries --------------------------------------------------------

    def lookup(self, name: str) -> list[dict]:
        """Exact-name symbol definitions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, kind, file_path, start_line, end_line, parent "
                "FROM symbols WHERE name = ? ORDER BY file_path, start_line",
                (name,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, prefix: str, limit: int = 20) -> list[dict]:
        """Symbols whose name starts with `prefix`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, kind, file_path, start_line, end_line, parent "
                "FROM symbols WHERE name LIKE ? ORDER BY name LIMIT ?",
                (prefix + "%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def references(self, name: str) -> list[dict]:
        """Call/usage sites of a name across the project."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_path, name, line FROM refs WHERE name = ? ORDER BY file_path, line",
                (name,),
            ).fetchall()
        return [dict(r) for r in rows]

    def dependencies(self, file_path: str | Path) -> list[str]:
        """Resolved files that `file_path` imports (project-internal only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT resolved_path FROM imports "
                "WHERE file_path = ? AND resolved_path IS NOT NULL",
                (str(file_path),),
            ).fetchall()
        return [r["resolved_path"] for r in rows]

    def dependents(self, file_path: str | Path) -> list[str]:
        """Files that import `file_path` (reverse edges)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT file_path FROM imports WHERE resolved_path = ?",
                (str(file_path),),
            ).fetchall()
        return [r["file_path"] for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()
        return row["c"]


# Lazy singleton (Step 12 / A1): constructing a SymbolIndex opens (and creates)
# the on-disk .symbols.db, so we don't do it at import time. get_symbol_index()
# builds it on first real use and caches it.
_symbol_index: SymbolIndex | None = None


def get_symbol_index() -> SymbolIndex:
    global _symbol_index
    if _symbol_index is None:
        _symbol_index = SymbolIndex()
    return _symbol_index
