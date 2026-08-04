"""Runtime-defect checks for generated Python — what `compile()` cannot see.

The module is named for its one *repair* (missing imports); it also holds the
report-only checks that share the same machinery and the same blind spot —
`unresolved_local_calls`, `missing_tables`, `duplicate_definitions`. All of them
catch code that compiles, passes every existing check, and then fails at
runtime. None of them ever fabricates domain logic.

Missing-import repair — the runtime failure `compile()` can't see.

`verify.check_file` compiles a `.py` file, so it catches a `SyntaxError`. It
cannot catch a **NameError**, because that only happens when the line actually
runs. The local model exploits this gap relentlessly: it writes a Flask route
using `request`, `redirect`, `url_for` and `get_db`, imports none of them, and
the file passes every check as "verified OK" — then the route 500s the moment
anyone clicks the button.

Measured, not theorised. Four for four across live builds:
`docs/phase0-baseline.md` (`bp_signup_reset` crashed with
`NameError: name 'app' is not defined`) and all three `build me a blog` runs in
`docs/phase1-notes.md` (`get_db`, `models`, `request`, `flash`, `redirect`,
`url_for` used, only `Flask, render_template` imported).

The fix is deterministic, per the "deterministic beats generated" rule: parse
with stdlib `ast`, find names that are **loaded but never bound anywhere in the
module**, and add the import for the ones we recognise.

Two deliberate conservatism rules make false positives near-impossible:

  * **Binding is collected flat, across the whole module** — every import,
    def, class, assignment, argument, `with ... as`, `except ... as`, loop and
    comprehension target, plus `global`/`nonlocal` declarations. This
    *over*-approximates what is in scope (a name bound only inside one function
    counts as bound everywhere), which errs toward doing nothing.
  * **Only an allowlist is ever added.** An unknown missing name is left alone
    and reported, never guessed at. We import `request` because there is exactly
    one thing `request` means in a Flask app; we would not import `utils`.

Pure and offline — `ast` only, no LLM, no filesystem. The caller passes which
local modules actually exist on disk.
"""

from __future__ import annotations

import ast
import builtins
import re

# Names that mean exactly one thing in a Flask project, and the import that
# supplies each. Anything not in here is reported, never invented.
_FLASK_EXPORTS = frozenset(
    {
        "Flask",
        "Blueprint",
        "Response",
        "abort",
        "flash",
        "g",
        "jsonify",
        "make_response",
        "redirect",
        "render_template",
        "request",
        "send_file",
        "send_from_directory",
        "session",
        "url_for",
    }
)

# name -> the exact import statement that binds it.
_KNOWN_IMPORTS: dict[str, str] = {
    "secure_filename": "from werkzeug.utils import secure_filename",
    "generate_password_hash": "from werkzeug.security import generate_password_hash",
    "check_password_hash": "from werkzeug.security import check_password_hash",
    "Path": "from pathlib import Path",
    "os": "import os",
    "json": "import json",
    "sqlite3": "import sqlite3",
    "secrets": "import secrets",
    "uuid": "import uuid",
}

# Helpers the scaffold puts in db.py. Only added when db.py is really there.
_DB_EXPORTS = frozenset({"get_db", "init_db", "ensure_column"})

_FLASK_IMPORT_LINE_RE = re.compile(r"^from flask import ([^(\\\n]+)$", re.MULTILINE)
_USES_FLASK_RE = re.compile(
    r"^\s*(?:from flask import|import flask)|@\w+\.route\(", re.MULTILINE
)


def uses_flask(source: str) -> bool:
    """Is this a Flask module? Gates the whole pass, so an unrelated script is
    never touched."""
    return bool(_USES_FLASK_RE.search(source or ""))


def _bound_names(tree: ast.AST) -> set[str]:
    """Every name bound anywhere in the module.

    Deliberately flat (no scope analysis): a name bound inside one function is
    treated as bound everywhere. That over-approximation is the safe direction —
    it can only cause us to skip a repair, never to add a wrong import.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            # `import a.b` binds `a`; `import a.b as c` binds `c`.
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def _loaded_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


# Bound by the interpreter in every module, so never "undefined". Most are
# attributes of the builtins module and would be covered by dir(builtins), but
# `__file__` is NOT — which made `BASE_DIR = Path(__file__).resolve()`, a line
# the scaffold itself ships, get reported as an undefined name on a live build.
_MODULE_DUNDERS = frozenset(
    {
        "__file__",
        "__name__",
        "__doc__",
        "__package__",
        "__spec__",
        "__loader__",
        "__builtins__",
        "__path__",
        "__cached__",
        "__annotations__",
        "__dict__",
    }
)


def undefined_names(source: str) -> set[str]:
    """Names this module reads but never binds. Empty when it doesn't parse."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return set()  # the syntax repair owns that; don't double-report
    return (
        _loaded_names(tree) - _bound_names(tree) - set(dir(builtins)) - _MODULE_DUNDERS
    )


def _module_level_names(source: str) -> set[str]:
    """Top-level names a module exports: defs, classes and assignments."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
    return out


# Tables a query reads or writes. `sqlite_*` is SQLite's own metadata.
_SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"'`\[]?(?P<t>\w+)",
    re.IGNORECASE,
)
_SQL_CREATE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(?P<t>\w+)", re.IGNORECASE
)
_SQL_NOISE = frozenset({"select", "where", "set", "values", "sqlite_master"})
# A literal only counts as SQL if it actually contains a statement.
_SQL_STATEMENT_RE = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|"
    r"DROP\s+TABLE)\b",
    re.IGNORECASE,
)


def searchable_sql(source: str) -> list[str]:
    """Where SQL may legitimately live in a source file.

    String literals only, for a module that parses — a `# CREATE TABLE ...`
    comment creates no table, and the scaffold ships exactly such comments as
    examples. Only a file that does NOT parse falls back to its raw text, since
    a half-written file is still worth reading. `sql_strings` returns [] for both
    cases, so parseability has to be checked rather than inferred.

    Shared by `missing_tables` here, `projectspec.entities_from_sql`, and
    `crud._creates_table` — every one of which was bitten by treating a
    commented example as real schema.
    """
    try:
        ast.parse(source or "")
    except SyntaxError:
        return [source or ""]
    return sql_strings(source)


def sql_strings(source: str) -> list[str]:
    """String literals in a module — the only place SQL can legitimately live.

    Scanning raw source instead would read Python's own `from flask import ...`
    as `FROM <table>` (caught by the tests, not by inspection). Comments are
    excluded too, which is exactly right: the scaffold's *commented* example
    `CREATE TABLE` must not count as creating anything.
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def missing_tables(sources: dict[str, str], extract=None) -> list[str]:
    """Tables the project queries but never creates.

    ``extract(source) -> [literal, …]`` says where SQL may live in THIS
    language; it defaults to Python's. That parameter is not decoration:
    `searchable_sql` falls back to the whole raw file when `ast.parse` fails,
    which every `.js` file does — so running this on a Node project read the
    prose in its comments and reported tables called `a` and `the`. Measured on
    a real build, and a confident wrong complaint is worse than a missed one.

    Measured on a live build: `db.py`'s `init_db()` kept the scaffold's *commented
    example* and added no real `CREATE TABLE`, while `app.py` ran
    `SELECT * FROM posts`. Everything compiles, the server starts, and every
    route touching the table 500s with "no such table: posts".

    Reported, never fixed here — inventing a schema means inventing the columns,
    which is exactly what the ProjectSpec entities in Phase 2 exist to make
    deterministic. Case-insensitive, since SQLite is.
    """
    extract = extract or searchable_sql
    created: set[str] = set()
    queried: set[str] = set()
    for text in sources.values():
        for literal in extract(text):
            # Prose is not SQL. A docstring reading "printed from the same
            # definition" matched `FROM <table>` and reported a table called
            # "the" on a live build.
            if not _SQL_STATEMENT_RE.search(literal):
                continue
            for match in _SQL_CREATE_RE.finditer(literal):
                created.add(match.group("t").lower())
            for match in _SQL_TABLE_RE.finditer(literal):
                name = match.group("t").lower()
                if name not in _SQL_NOISE and not name.startswith("sqlite_"):
                    queried.add(name)
    return sorted(queried - created)


def duplicate_definitions(source: str) -> list[str]:
    """Top-level functions/classes defined more than once in one module.

    Always a defect: the later definition silently shadows the earlier, so the
    file does something different from what most of it says. Measured live — a
    surgical edit to `db.py` re-inserted the scaffold's whole tail, leaving two
    `init_db`, two `get_db` and two `ensure_column`, and the *second* `init_db`
    (the one that created no tables) is the one that ran.
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    seen: set[str] = set()
    dupes: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen and node.name not in dupes:
                dupes.append(node.name)
            seen.add(node.name)
    return dupes


def unresolved_local_calls(source: str, module_sources: dict[str, str]) -> list[str]:
    """Calls into a sibling module that the sibling never defines.

    The failure this catches, measured live: `app.py` calls
    `models.get_all_posts(...)` while `models.py` only defines `add_post`. Every
    existing check passes — both files compile, the import resolves — and the
    route 500s with an `AttributeError` the moment it is opened. A
    `from db import missing_thing` is worse still: that one kills the process at
    startup.

    Reported, never fabricated: writing the missing function means inventing a
    query, which is generation, not repair. Returns strings like
    ``"models.get_all_posts"``.
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []

    exports = {name: _module_level_names(text) for name, text in module_sources.items()}
    missing: list[str] = []

    for node in ast.walk(tree):
        # models.get_all_posts(...)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in exports
            and node.attr not in exports[node.value.id]
        ):
            ref = f"{node.value.id}.{node.attr}"
            if ref not in missing:
                missing.append(ref)
        # from db import missing_thing
        elif isinstance(node, ast.ImportFrom) and node.module in exports:
            for alias in node.names:
                if alias.name != "*" and alias.name not in exports[node.module]:
                    ref = f"{node.module}.{alias.name}"
                    if ref not in missing:
                        missing.append(ref)
    return missing


def _import_for(name: str, local_modules: frozenset[str]) -> str | None:
    """The import statement that binds ``name``, or None if we don't know one."""
    if name in _KNOWN_IMPORTS:
        return _KNOWN_IMPORTS[name]
    if name in _DB_EXPORTS and "db" in local_modules:
        return f"from db import {name}"
    if name in local_modules:
        return f"import {name}"
    return None


def _insertion_point(source: str, tree: ast.Module) -> int:
    """Line index (0-based) to insert new imports at: after the last top-level
    import, else after the module docstring, else the top of the file."""
    last_import = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = max(last_import, getattr(node, "end_lineno", node.lineno))
    if last_import:
        return last_import
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        return getattr(tree.body[0], "end_lineno", tree.body[0].lineno)
    return 0


def add_missing_imports(
    source: str, local_modules: frozenset[str] = frozenset()
) -> tuple[str, list[str], list[str]]:
    """Add imports for recognised names the module uses but never binds.

    Returns ``(new_source, added, still_missing)``:
      * ``added`` — the import statements written, for the user-facing note.
      * ``still_missing`` — unresolvable names, reported rather than guessed.

    A no-op unless this is a Flask module. The result is re-parsed before being
    returned: if the edit somehow broke the file, the original is returned
    unchanged, so this pass can never make a working file worse.
    """
    text = source or ""
    if not uses_flask(text):
        return source, [], []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return source, [], []

    missing = sorted(undefined_names(text))
    if not missing:
        return source, [], []

    flask_names = [n for n in missing if n in _FLASK_EXPORTS]
    plain_imports: list[str] = []
    unresolved: list[str] = []
    for name in missing:
        if name in _FLASK_EXPORTS:
            continue
        stmt = _import_for(name, local_modules)
        if stmt is None:
            unresolved.append(name)
        elif stmt not in plain_imports:
            plain_imports.append(stmt)

    if not flask_names and not plain_imports:
        return source, [], unresolved

    added: list[str] = []
    lines = text.splitlines()

    # Prefer extending the existing `from flask import ...` line — a second
    # flask import line right under the first reads like a mistake.
    if flask_names:
        match = _FLASK_IMPORT_LINE_RE.search(text)
        if match:
            existing = [n.strip() for n in match.group(1).split(",") if n.strip()]
            merged = sorted(set(existing) | set(flask_names))
            new_line = "from flask import " + ", ".join(merged)
            text = text[: match.start()] + new_line + text[match.end() :]
            lines = text.splitlines()
            added.append(new_line)
        else:
            plain_imports.insert(
                0, "from flask import " + ", ".join(sorted(flask_names))
            )

    if plain_imports:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return source, [], unresolved
        at = _insertion_point(text, tree)
        lines[at:at] = plain_imports
        added.extend(plain_imports)
        text = "\n".join(lines)
        if source.endswith("\n") and not text.endswith("\n"):
            text += "\n"

    try:
        ast.parse(text)
    except SyntaxError:
        return source, [], unresolved  # never ship a file we just broke
    return text, added, unresolved
