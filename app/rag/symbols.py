"""AST-based symbol index and dependency graph.

Why stdlib `ast` and not tree-sitter: the installed tree-sitter-languages
(1.10.2) is incompatible with tree-sitter 0.25.x in this environment, so the
parser is unavailable. `ast` is always present, deterministic, fully offline,
and more accurate than tree-sitter for Python (real names, imports, call
sites). Non-Python files degrade to no symbols rather than crashing.

Storage is a standalone synchronous sqlite3 DB (default `.symbols.db`). This
matches the *synchronous* retriever indexing path and avoids contending with
the async SQLAlchemy connection on `.coder.db`.
"""

from __future__ import annotations

import ast
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings


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


def extract_symbols(file_path: str | Path) -> FileSymbols:
    """Parse a Python file into symbols, imports, and references.

    Returns an empty FileSymbols on syntax errors, unreadable files, or
    non-Python extensions — never raises.
    """
    path = Path(file_path)
    if path.suffix.lower() != ".py":
        return FileSymbols()
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return FileSymbols()

    walker = _Walker(str(file_path))
    for node in ast.iter_child_nodes(tree):
        walker.visit(node)
    return walker.result


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
    """Synchronous sqlite3-backed symbol + dependency-graph index."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = getattr(settings, "symbols_path", ".symbols.db")
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
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
        root = Path(project_root) if project_root is not None else None
        import_rows = []
        for module, lvl in fs._import_records:
            resolved = (
                _resolve_import(module, lvl, root, Path(file_path))
                if root is not None
                else None
            )
            import_rows.append((fp, module, resolved))
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
        cur = self._conn.cursor()
        cur.execute("DELETE FROM symbols WHERE file_path = ?", (fp,))
        cur.execute("DELETE FROM imports WHERE file_path = ?", (fp,))
        cur.execute("DELETE FROM refs WHERE file_path = ?", (fp,))
        self._conn.commit()

    def clear(self) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM symbols")
        cur.execute("DELETE FROM imports")
        cur.execute("DELETE FROM refs")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- queries --------------------------------------------------------

    def lookup(self, name: str) -> list[dict]:
        """Exact-name symbol definitions."""
        rows = self._conn.execute(
            "SELECT name, kind, file_path, start_line, end_line, parent "
            "FROM symbols WHERE name = ? ORDER BY file_path, start_line",
            (name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search(self, prefix: str, limit: int = 20) -> list[dict]:
        """Symbols whose name starts with `prefix`."""
        rows = self._conn.execute(
            "SELECT name, kind, file_path, start_line, end_line, parent "
            "FROM symbols WHERE name LIKE ? ORDER BY name LIMIT ?",
            (prefix + "%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def references(self, name: str) -> list[dict]:
        """Call/usage sites of a name across the project."""
        rows = self._conn.execute(
            "SELECT file_path, name, line FROM refs WHERE name = ? ORDER BY file_path, line",
            (name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def dependencies(self, file_path: str | Path) -> list[str]:
        """Resolved files that `file_path` imports (project-internal only)."""
        rows = self._conn.execute(
            "SELECT DISTINCT resolved_path FROM imports "
            "WHERE file_path = ? AND resolved_path IS NOT NULL",
            (str(file_path),),
        ).fetchall()
        return [r["resolved_path"] for r in rows]

    def dependents(self, file_path: str | Path) -> list[str]:
        """Files that import `file_path` (reverse edges)."""
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM imports WHERE resolved_path = ?",
            (str(file_path),),
        ).fetchall()
        return [r["file_path"] for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()["c"]


# Module-level singleton (uses the configured on-disk path)
symbol_index = SymbolIndex()
