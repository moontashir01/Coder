"""ProjectSpec — what the project IS, remembered between turns.

This closes the biggest gap in `docs/fullstack-web-plan.md`: Coder has no memory
of the project it built. `chat()` sets `self._blueprint = None` at the top of
every turn, so the endpoints, the schema and the feature list exist for exactly
one turn and are then thrown away. Turn 2 of the demo — "add an admin page" —
never sees turn 1's contract at all.

What exists instead is not enough. `app/memory/conversation.py` is a sliding
window of raw chat text, so the model has to re-read prose and re-infer the
schema; a 7B model will not do that reliably. `app/memory/project_memory.py`
scans the filesystem for language counts and a module list — it knows `app.py`
exists, but not that `app.py` defines `POST /admin/products` reading
`title, price, image`.

So this module persists the contract itself to `<project>/.coder/project.json`:
inside the project, not in `.coder.db`, so it survives, is inspectable, diffable
in git, and travels with the folder.

**`entities` is the load-bearing addition.** Today's `ApiContract.data_schema` is
a tuple of free-text strings like `"users(email TEXT PRIMARY KEY, ...)"`. Free
text cannot be diffed, so it cannot produce a migration. Structured fields can:
`ddl()` emits `CREATE TABLE`, and `migrations(since=n)` emits exactly the
`ensure_column` calls a schema change needs — which is what lets turn 3 add a
field without deleting turn 1's data.

Pure and offline, like `blueprint.py` and `buildspec.py`: no LLM call lives here.
Validation follows the same discipline as `blueprint._norm_filename` /
`_clean_endpoints` — safe relative paths only, known types only, a cap on every
list — because this file is read back and fed to a model.

Three rules the rest of the codebase depends on:

  * **A corrupt `project.json` returns None, never raises.** Coder then behaves
    exactly as it does today.
  * **Saving is best-effort** and never fails a turn whose files were written.
  * **`save()` writes the file directly** (tmp + `os.replace`), NOT through
    `executor.execute("write_file", …)`. The spec is agent state, not user code.
    Routing it through the tool would put it behind the approval gate — an
    `[a]llow / [s]ession / [d]eny` prompt after every single turn, mid-demo — and
    would push a backup into `.coder_backups/` on every save, evicting the
    user's real undo history against `max_write_backups`. The path is inside
    `sandbox_root` either way.

`.coder/` is a dot-directory, so the RAG indexer and `project_memory._scan_project`
already skip it (both filter `part.startswith(".")`). That is deliberate — the
spec must not be embedded and retrieved back as if it were source. Do not "fix"
that skip.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.agent.blueprint import Blueprint
from app.agent.pyimports import sql_strings, uses_flask
from app.agent.runtime_probe import Stack

logger = logging.getLogger(__name__)

SPEC_VERSION = 1
SPEC_DIRNAME = ".coder"
SPEC_FILENAME = "project.json"

# Marks a README as ours. `to_readme` emits it and the Flask scaffold ships it,
# so `core._write_readme` can tell a README it may regenerate from one a human
# wrote — a distinction that only became necessary with `from_disk`, which lets
# an existing repo reach the amendment path.
README_MARKER = "Written by Coder from the project spec"

# Caps — this whole structure rides in a prompt, and it is parsed from model
# output upstream, so every list is bounded.
MAX_ENTITIES = 12
MAX_FIELDS = 24
MAX_ENDPOINTS = 24
MAX_PAGES = 24
MAX_FEATURES = 20
MAX_FILES = 40
MAX_HISTORY = 20

# The context block rides alongside the plan manifest and sibling context inside
# `llm_num_ctx`, so it is budgeted hard. History and prose are dropped first.
CONTEXT_BUDGET_CHARS = 1200

# SQLite storage classes we will emit. Anything else is normalised into one of
# these, so `ddl()` can never produce a type SQLite rejects.
_SQL_TYPES = ("INTEGER", "TEXT", "REAL", "BLOB", "NUMERIC")
_TYPE_ALIASES = {
    "INT": "INTEGER",
    "BIGINT": "INTEGER",
    "SMALLINT": "INTEGER",
    "TINYINT": "INTEGER",
    "SERIAL": "INTEGER",
    "BOOL": "INTEGER",
    "BOOLEAN": "INTEGER",
    "VARCHAR": "TEXT",
    "CHAR": "TEXT",
    "STRING": "TEXT",
    "UUID": "TEXT",
    "DATE": "TEXT",
    "DATETIME": "TEXT",
    "TIMESTAMP": "TEXT",
    "JSON": "TEXT",
    "FLOAT": "REAL",
    "DOUBLE": "REAL",
    "DECIMAL": "REAL",
    "MONEY": "REAL",
    # Not SQLite types — they are how a build request describes an upload, and
    # what Phase 4b keys the file-upload machinery off. Stored as a path.
    "IMAGE": "TEXT",
    "FILE": "TEXT",
}
# Field types that mean "an uploaded file lives at this path".
UPLOAD_TYPES = frozenset({"IMAGE", "FILE"})

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FILENAME_RE = re.compile(r"^[\w./-]+$")
# "users(email TEXT PRIMARY KEY, password_hash TEXT) — seed one demo user"
_SCHEMA_RE = re.compile(r"^\s*(?P<table>\w+)\s*\((?P<cols>.+)\)", re.DOTALL)
_CREATE_TABLE_HEAD_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(?P<table>\w+)[\"'`\]]?\s*\(",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# SQL dialects (Phase N3, docs/node-stack-plan.md)
# ---------------------------------------------------------------------------
# The entity list is the single source of truth for BOTH stacks, so the only
# thing that may differ between them is how that truth is spelled. Keeping the
# differences in one small table — rather than in two copies of the DDL writer —
# is what stops the sqlite schema and the PostgreSQL schema drifting apart while
# both claim to come from the same `Entity`.
#
# `SQLITE` is the default everywhere, so every existing Flask caller is
# unchanged byte-for-byte.


@dataclass(frozen=True)
class Dialect:
    """How one database spells the handful of things that differ.

    Five differences, and every one of them is a bug if you get it wrong
    silently: an autoincrement key that isn't one, a type the server rejects, a
    placeholder that binds nothing, an insert that cannot report the id it just
    created, and an ALTER that raises on the second startup.
    """

    key: str  # "sqlite" | "postgres"
    # The full declaration for an autoincrement primary key, minus the name.
    serial_pk: str
    # Canonical type (`_SQL_TYPES`) -> the type this server actually has.
    type_map: dict[str, str]
    # `?` vs `$1` — the reason this is a function and not a constant.
    positional: bool
    # How a generated migration is written in the target language.
    migration_template: str

    def column_type(self, canonical: str) -> str:
        return self.type_map.get(canonical, canonical)

    def placeholder(self, index: int) -> str:
        """Bind marker for the ``index``-th value, 1-based."""
        return f"${index}" if self.positional else "?"

    def placeholders(self, count: int) -> str:
        return ", ".join(self.placeholder(i) for i in range(1, count + 1))

    def column_ddl(self, name: str, canonical: str, pk: bool, required: bool) -> str:
        """One column of a CREATE TABLE."""
        if pk and canonical == "INTEGER":
            return f"{name} {self.serial_pk}"
        parts = [name, self.column_type(canonical)]
        if pk:
            parts.append("PRIMARY KEY")
        elif required:
            parts.append("NOT NULL")
        return " ".join(parts)

    def migration_call(self, table: str, column: str, canonical: str) -> str:
        return self.migration_template.format(
            table=table, column=column, decl=self.column_type(canonical)
        )


SQLITE = Dialect(
    key="sqlite",
    serial_pk="INTEGER PRIMARY KEY AUTOINCREMENT",
    type_map={},  # the canonical names ARE the SQLite storage classes
    positional=False,
    # `db.ensure_column` (shipped by the Flask scaffold) is the primitive: a
    # PRAGMA check then an ALTER, because SQLite has no `IF NOT EXISTS` here.
    migration_template='ensure_column(conn, "{table}", "{column}", "{decl}")',
)

POSTGRES = Dialect(
    key="postgres",
    serial_pk="SERIAL PRIMARY KEY",
    # REAL exists in PostgreSQL but is a 4-byte float — wrong for a price, which
    # is what REAL overwhelmingly means in a generated schema. NUMERIC is exact.
    type_map={"REAL": "NUMERIC", "BLOB": "BYTEA"},
    positional=True,
    # PostgreSQL HAS `ADD COLUMN IF NOT EXISTS`, so `ensureColumn` is a one-liner
    # rather than sqlite's read-then-alter. It is still DDL against a live
    # server, which is why a failure must be reported and never swallowed.
    migration_template='await ensureColumn(client, "{table}", "{column}", "{decl}")',
)

DIALECTS = {SQLITE.key: SQLITE, POSTGRES.key: POSTGRES}


def get_dialect(key: str | None) -> Dialect:
    """The dialect for ``key``, defaulting to SQLite for anything unknown.

    Total, for `stacks.get_adapter`'s reason: a spec written before dialects
    existed names none, and that must keep working rather than raising.
    """
    return DIALECTS.get(str(key or "").strip().lower(), SQLITE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One column. `added_in` records the revision that introduced it, which is
    what `migrations(since=…)` diffs on."""

    name: str
    type: str = "TEXT"
    pk: bool = False
    required: bool = False
    added_in: int = 1

    def to_ddl(self, dialect: Dialect = SQLITE) -> str:
        return dialect.column_ddl(self.name, self.type, self.pk, self.required)

    def is_upload(self) -> bool:
        return self.name.endswith(("_path", "_image", "_file")) or self.type in (
            UPLOAD_TYPES
        )


@dataclass(frozen=True)
class Entity:
    """A stored thing: one table, its columns, and the revision each arrived in."""

    name: str
    table: str
    fields: tuple[Field, ...] = ()

    def field(self, name: str) -> Field | None:
        low = (name or "").lower()
        return next((f for f in self.fields if f.name.lower() == low), None)

    def to_ddl(self, dialect: Dialect = SQLITE) -> str:
        """`CREATE TABLE IF NOT EXISTS` for the fields present at revision 1.

        Fields added later are NOT included: they belong in `migrations()` as
        `ensure_column` calls, so an existing database picks them up instead of
        being recreated. That distinction is the whole reason fields carry
        `added_in`.
        """
        base = [f for f in self.fields if f.added_in <= 1] or list(self.fields)
        cols = ",\n    ".join(f.to_ddl(dialect) for f in base)
        return f"CREATE TABLE IF NOT EXISTS {self.table} (\n    {cols}\n)"

    def summary(self) -> str:
        return f"{self.table}({', '.join(f'{f.name} {f.type}' for f in self.fields)})"


@dataclass(frozen=True)
class SpecEndpoint:
    method: str
    path: str
    request: str = ""
    response: str = ""
    handler: str = ""
    template: str = ""
    entity: str = ""
    added_in: int = 1

    def to_line(self) -> str:
        line = f"{self.method} {self.path}"
        if self.request:
            line += f"  body: {self.request}"
        if self.entity:
            line += f"  -> {self.entity}"
        return line


@dataclass(frozen=True)
class Page:
    route: str = ""
    template: str = ""
    nav_label: str = ""
    purpose: str = ""
    reads: tuple[str, ...] = ()
    added_in: int = 1

    def to_line(self) -> str:
        left = self.route or self.template
        bits = [left]
        if self.template and self.route:
            bits.append(f"({self.template})")
        if self.nav_label:
            bits.append(f'nav "{self.nav_label}"')
        if self.reads:
            bits.append("reads " + ", ".join(self.reads))
        return " ".join(bits)


@dataclass(frozen=True)
class SpecFeature:
    name: str
    tier: str = "core"
    files: tuple[str, ...] = ()
    added_in: int = 1


@dataclass(frozen=True)
class FileRecord:
    """What one file in the project IS (D2).

    `files` used to be `path -> role`, four words deep — enough to say "this is a
    page", never enough to answer "which file do I edit for *that*". Routing an
    edit needs to know what a file DEFINES (its routes and view functions) and
    which entities it shows, which is exactly what `_resolve_target_from_spec`
    and `impact.py` have to reconstruct otherwise.

    The on-disk format was already forward-compatible — `_load_files` has always
    read `value.get("role")` when the value is a dict — so an old
    `project.json` still loads, its files simply arriving with only a role.
    """

    role: str = ""  # page | asset | backend | config
    purpose: str = ""  # one line, what it is for
    defines: tuple[str, ...] = ()  # "GET /products", view/function names
    reads: tuple[str, ...] = ()  # entity names this file displays or writes
    revision: int = 1  # the revision it first appeared in

    def to_line(self) -> str:
        bits = [self.role or "file"]
        if self.defines:
            bits.append("defines " + ", ".join(self.defines[:4]))
        if self.reads:
            bits.append("reads " + ", ".join(self.reads))
        return " — ".join(bits)


@dataclass(frozen=True)
class HistoryEntry:
    revision: int
    request: str = ""
    added: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Normalisation — everything here can arrive from model output
# ---------------------------------------------------------------------------


def _ident(raw, limit: int = 40) -> str:
    text = str(raw or "").strip().strip("\"'`[]")
    text = text.replace("-", "_").replace(" ", "_")[:limit]
    return text if _IDENT_RE.match(text) else ""


def _norm_type(raw) -> str:
    text = str(raw or "").strip().upper()
    text = re.sub(r"\(.*?\)", "", text).strip()  # VARCHAR(255) -> VARCHAR
    text = text.split()[0] if text else ""
    if text in _SQL_TYPES:
        return text
    return _TYPE_ALIASES.get(text, "TEXT")


def _norm_filename(raw) -> str:
    """A safe relative path, or '' — mirrors `blueprint._norm_filename`."""
    name = str(raw or "").strip().strip("'\"").replace("\\", "/").lstrip("/")
    name = name.split("#", 1)[0].split("?", 1)[0].strip()
    if not name or ".." in name or not _FILENAME_RE.match(name):
        return ""
    return name


# Words ending in these are not plurals, so the trailing "s" must survive:
# status -> status, not "statu". Same for address/analysis/news.
_NOT_PLURAL_ENDINGS = ("ss", "us", "is", "os", "ies_", "news")


def _singular(table: str) -> str:
    """products -> product, categories -> category. Good enough for a label."""
    low = (table or "").lower()
    if len(low) <= 3:
        return low
    if low.endswith("ies"):
        return low[:-3] + "y"
    if low.endswith(("sses", "shes", "ches", "xes")):
        return low[:-2]
    if low.endswith(_NOT_PLURAL_ENDINGS):
        return low
    if low.endswith("s"):
        return low[:-1]
    return low


def _split_columns(blob: str) -> list[str]:
    """Split a column list on commas that are not inside parentheses.

    `DECIMAL(10,2)` and `VARCHAR(255)` must not be split in the middle.
    """
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in blob or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        out.append("".join(current))
    return [c.strip() for c in out if c.strip()]


# Table-level clauses that are not columns.
_NON_COLUMN_RE = re.compile(
    r"^\s*(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT)\b", re.IGNORECASE
)


def _parse_columns(blob: str, added_in: int = 1) -> tuple[Field, ...]:
    fields: list[Field] = []
    seen: set[str] = set()
    for chunk in _split_columns(blob):
        if _NON_COLUMN_RE.match(chunk):
            continue
        parts = chunk.split()
        if not parts:
            continue
        name = _ident(parts[0])
        if not name or name.lower() in seen:
            continue
        rest = " ".join(parts[1:]).upper()
        raw_type = parts[1] if len(parts) > 1 else "TEXT"
        fields.append(
            Field(
                name=name,
                type=_norm_type(raw_type),
                pk="PRIMARY KEY" in rest,
                required="NOT NULL" in rest or "PRIMARY KEY" in rest,
                added_in=added_in,
            )
        )
        seen.add(name.lower())
        if len(fields) >= MAX_FIELDS:
            break
    return tuple(fields)


def parse_schema_line(line: str, added_in: int = 1) -> Entity | None:
    """Turn one free-text `data_schema` string into a structured Entity.

    This is the conversion the whole phase turns on: the blueprint hands over
    `"users(email TEXT PRIMARY KEY, password_hash TEXT) — seed a demo user"`,
    which cannot be diffed, and this produces fields that can. Trailing prose
    after the closing paren is ignored.
    """
    match = _SCHEMA_RE.match(str(line or ""))
    if not match:
        return None
    table = _ident(match.group("table"))
    if not table:
        return None
    fields = _parse_columns(match.group("cols"), added_in)
    if not fields:
        return None
    return Entity(name=_singular(table), table=table, fields=fields)


def _fields_from_data(raw_fields, added_in: int = 1) -> list[Field]:
    """Validated `Field`s from a model's JSON list.

    Accepts both shapes the model reaches for: `"title TEXT"` and
    `{"name": "title", "type": "TEXT", "required": true}`. Unknown types collapse
    to TEXT via `_norm_type`, unusable names are dropped rather than guessed at,
    and the list is capped — the same discipline as every other parser here,
    because this is model output that ends up in a `CREATE TABLE`.
    """
    out: list[Field] = []
    seen: set[str] = set()
    for item in raw_fields or []:
        if isinstance(item, str):
            bits = item.split()
            name = _ident(bits[0] if bits else "")
            raw_type = bits[1] if len(bits) > 1 else "TEXT"
            rest = " ".join(bits[1:]).upper()
            pk = "PRIMARY KEY" in rest
            required = pk or "NOT NULL" in rest
        elif isinstance(item, dict):
            name = _ident(item.get("name"))
            raw_type = str(item.get("type") or "TEXT")
            pk = bool(item.get("pk") or item.get("primary_key"))
            required = pk or bool(item.get("required") or item.get("not_null"))
        else:
            continue
        ftype = _norm_type(raw_type)
        if not name or name.lower() in seen:
            continue
        # `IMAGE`/`FILE` normalise to TEXT — they are not SQLite types — which
        # would throw away the only signal that this column holds an uploaded
        # file, and with it the Phase 4b upload wiring. Everything downstream
        # (`Field.is_upload`, `crud.upload_helper_source`, `fix_form_enctype`)
        # keys off the NAME, so carry the signal there: `cover` -> `cover_path`.
        if raw_type.strip().upper() in UPLOAD_TYPES and not name.lower().endswith(
            ("_path", "_image", "_file")
        ):
            name = f"{name}_path"[:40]
            if name.lower() in seen:
                continue
        seen.add(name.lower())
        out.append(
            Field(name=name, type=ftype, pk=pk, required=required, added_in=added_in)
        )
        if len(out) >= MAX_FIELDS:
            break
    return out


def entities_from_data(data: dict | None) -> tuple[Entity, ...]:
    """The schema call's JSON, validated into diffable entities (Phase C1).

    The schema used to arrive as free text inside the blueprint's own answer
    (`"users(email TEXT PRIMARY KEY, …)"`) and had to be reverse-engineered by
    `parse_schema_line` afterwards. Asking for it FIRST, on its own, in a shape
    that is already structured, removes that round-trip — and, more importantly,
    means the layout can be planned against a schema that already exists rather
    than invented in the same breath as the pages that read it.

    Pure and total: anything unparseable is dropped, never guessed at, and a
    failed call (``None``) yields ``()`` so the caller falls back to the old
    free-text path.
    """
    data = data if isinstance(data, dict) else {}
    out: list[Entity] = []
    seen: set[str] = set()
    for item in data.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = _ident(item.get("name") or item.get("entity"))
        table = _ident(item.get("table")) or (f"{name}s" if name else "")
        if not table:
            continue
        fields = _fields_from_data(item.get("fields"))
        if not fields or table.lower() in seen:
            continue
        # Every table needs a key the CRUD helpers can address a row by. The
        # model omits it perhaps a third of the time (it is "obvious"), and a
        # products table with no id makes edit/delete unwriteable.
        if not any(f.pk for f in fields):
            fields.insert(0, Field(name="id", type="INTEGER", pk=True, required=True))
        seen.add(table.lower())
        out.append(
            Entity(name=name or _singular(table), table=table, fields=tuple(fields))
        )
        if len(out) >= MAX_ENTITIES:
            break
    return tuple(out)


def _create_table_blocks(text: str) -> list[tuple[str, str]]:
    """`(table, column blob)` for each CREATE TABLE, matching parens properly.

    A regex cannot do this: `price DECIMAL(10,2)` means the statement's closing
    paren is not the first one, so a non-greedy `\\(.*?\\)` truncates the column
    list. Scan forward counting depth instead.
    """
    out: list[tuple[str, str]] = []
    for match in _CREATE_TABLE_HEAD_RE.finditer(text or ""):
        depth = 1
        start = match.end()
        i = start
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if depth == 0:
            out.append((match.group("table"), text[start : i - 1]))
    return out


def _searchable_sql(text: str) -> list[str]:
    """Where SQL may legitimately live in a source file.

    For a module that parses, that is its string literals and nothing else — a
    `# CREATE TABLE ...` comment creates no table, and the scaffold ships
    exactly such a comment as an example. Only when the file does NOT parse do
    we fall back to scanning it raw, because a half-written file is still worth
    reading. `sql_strings` returns [] for both cases, so parseability has to be
    checked here rather than inferred from an empty result.
    """
    try:
        ast.parse(text or "")
    except SyntaxError:
        return [text or ""]
    return sql_strings(text)


def entities_from_sql(sources: dict[str, str], strings_reader=None) -> list[Entity]:
    """Entities recovered from real `CREATE TABLE` statements on disk.

    Used only as a fallback when the blueprint declared no schema — the
    blueprint's contract is the intent and stays authoritative when present.

    Scans **string literals only** (via `pyimports.sql_strings`), for the same
    reason `missing_tables` does: the scaffold ships a *commented* CREATE TABLE
    example, and counting a comment would invent a table that does not exist.
    A source that doesn't parse falls back to its raw text, since a half-written
    file is exactly when this information is still worth having.
    """
    # `_searchable_sql` reads Python string literals via `ast`. A JS project
    # passes `crud_node.js_strings` instead — same rule (literals only, never a
    # comment), different language.
    read_strings = strings_reader or _searchable_sql
    out: list[Entity] = []
    seen: set[str] = set()
    for text in sources.values():
        for literal in read_strings(text):
            for table_raw, cols in _create_table_blocks(literal):
                table = _ident(table_raw)
                if not table or table.lower() in seen:
                    continue
                fields = _parse_columns(cols)
                if not fields:
                    continue
                seen.add(table.lower())
                out.append(Entity(name=_singular(table), table=table, fields=fields))
                if len(out) >= MAX_ENTITIES:
                    return out
    return out


# `@app.route("/posts")` … `render_template("posts.html")` inside the view.
_ROUTE_RE = re.compile(
    r"@app\.route\(\s*[\"'](?P<path>/[^\"']*)[\"'](?P<rest>[^)]*)\)\s*\n"
    r"(?:\s*@[^\n]+\n)*"
    r"\s*def\s+(?P<view>\w+)\s*\([^)]*\)\s*:(?P<body>(?:\n(?:[ \t]+[^\n]*|\s*))*)",
)
_RENDER_RE = re.compile(r"render_template\(\s*[\"'](?P<tpl>[\w./-]+)[\"']")
_METHODS_RE = re.compile(r"methods\s*=\s*\[(?P<m>[^\]]*)\]")


def routes_from_source(source: str) -> list[tuple[str, str, str, str]]:
    """`(method, path, view_name, template)` for each Flask route in a module.

    Read off the file the build actually produced, so `pages` records the real
    route→template mapping rather than one guessed from a filename. Regex rather
    than `ast` on purpose: it must survive a file that doesn't fully parse,
    which is exactly when this information is most worth having.
    """
    out: list[tuple[str, str, str, str]] = []
    for match in _ROUTE_RE.finditer(source or ""):
        path = match.group("path")
        body = match.group("body") or ""
        tpl = _RENDER_RE.search(body)
        template = tpl.group("tpl") if tpl else ""
        methods_match = _METHODS_RE.search(match.group("rest") or "")
        methods = (
            [
                m.strip().strip("\"'").upper()
                for m in methods_match.group("m").split(",")
            ]
            if methods_match
            else ["GET"]
        )
        for method in methods or ["GET"]:
            if method:
                out.append((method, path, match.group("view"), template))
    return out


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


@dataclass
class SpecDelta:
    """What one amendment turn changes — ONLY the changes, never the whole spec.

    Produced by one temperature-0 extraction call (`core._extract_delta`). It
    deliberately does not say which existing files to edit: that question — "what
    else does this break?" — is the one a 7B model answers worst, so it is
    computed deterministically from the spec by `app/agent/impact.py`.
    """

    summary: str = ""
    add_entities: tuple[Entity, ...] = ()
    add_fields: tuple[tuple[str, Field], ...] = ()  # (entity name, field)
    add_endpoints: tuple[SpecEndpoint, ...] = ()
    add_pages: tuple[Page, ...] = ()
    new_files: tuple[tuple[str, str], ...] = ()  # (filename, instruction)

    def is_empty(self) -> bool:
        return not (
            self.add_entities
            or self.add_fields
            or self.add_endpoints
            or self.add_pages
            or self.new_files
        )

    def touched_entities(self) -> set[str]:
        """Entity names this delta creates or alters."""
        return {e.name for e in self.add_entities} | {n for n, _ in self.add_fields}


def delta_from_data(data: dict | None, spec: "ProjectSpec") -> SpecDelta:
    """Turn the extraction call's JSON into a validated SpecDelta.

    Same discipline as `blueprint_from_data`: every value is normalised, unknown
    types collapse to TEXT, paths must be safe and relative, and anything that
    cannot be made sense of is dropped rather than guessed at. ``data`` may be
    None (the call failed) — that yields an empty delta and the caller falls
    back to ordinary routing.

    A field named on an entity the spec doesn't have becomes a NEW entity rather
    than being silently discarded; a field the entity already has is dropped,
    because re-adding it would produce a duplicate column.
    """
    data = data if isinstance(data, dict) else {}

    add_entities: list[Entity] = []
    add_fields: list[tuple[str, Field]] = []
    for item in data.get("entities") or []:
        if not isinstance(item, dict):
            continue
        raw_name = _ident(item.get("name") or item.get("entity") or item.get("table"))
        if not raw_name:
            continue
        parsed = _fields_from_data(item.get("add_fields") or item.get("fields"))
        if not parsed:
            continue

        existing = spec.entity(raw_name)
        if existing is None:
            table = _ident(item.get("table")) or (raw_name + "s")
            add_entities.append(
                Entity(name=raw_name, table=table, fields=tuple(parsed))
            )
        else:
            for f in parsed:
                if existing.field(f.name) is None:
                    add_fields.append((existing.name, f))

    add_endpoints: list[SpecEndpoint] = []
    for item in data.get("endpoints") or []:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "GET").strip().upper()
        path = str(item.get("path") or item.get("route") or "").strip()
        if not path.startswith("/") or method not in (
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ):
            continue
        if any(e.method == method and e.path == path for e in spec.endpoints):
            continue  # already there
        add_endpoints.append(
            SpecEndpoint(
                method=method,
                path=path[:120],
                request=" ".join(str(item.get("request") or "").split())[:120],
                response=" ".join(str(item.get("response") or "").split())[:120],
                handler="app.py",
                template=_norm_filename(item.get("template")),
                entity=_ident(item.get("entity")),
            )
        )
        if len(add_endpoints) >= MAX_ENDPOINTS:
            break

    add_pages: list[Page] = []
    for item in data.get("pages") or []:
        if not isinstance(item, dict):
            continue
        template = _norm_filename(item.get("template"))
        route = str(item.get("route") or "")[:120]
        if not template and not route:
            continue
        if any(p.template == template and template for p in spec.pages):
            continue
        add_pages.append(
            Page(
                route=route,
                template=template,
                nav_label=str(item.get("nav_label") or "")[:40],
                purpose=" ".join(str(item.get("purpose") or "").split())[:100],
                reads=tuple(_ident(r) for r in (item.get("reads") or []) if _ident(r))[
                    :5
                ],
            )
        )
        if len(add_pages) >= MAX_PAGES:
            break

    new_files: list[tuple[str, str]] = []
    seen_files: set[str] = set()
    for item in data.get("new_files") or []:
        if isinstance(item, str):
            fname, instruction = _norm_filename(item), ""
        elif isinstance(item, dict):
            fname = _norm_filename(item.get("filename") or item.get("file"))
            instruction = " ".join(str(item.get("instruction") or "").split())[:400]
        else:
            continue
        if not fname or fname.lower() in seen_files:
            continue
        seen_files.add(fname.lower())
        new_files.append((fname, instruction))
        if len(new_files) >= MAX_FILES:
            break

    # A page or endpoint naming a template that doesn't exist yet implies the
    # file, even when the model forgot to list it — the same "declared it, then
    # omitted the file" failure `blueprint._ensure_backend` exists to catch.
    for page in add_pages:
        if page.template and page.template.lower() not in seen_files:
            seen_files.add(page.template.lower())
            new_files.append(
                (
                    page.template,
                    page.purpose
                    or f"The page served at {page.route or page.template}.",
                )
            )

    return SpecDelta(
        summary=" ".join(str(data.get("summary") or "").split())[:200],
        add_entities=tuple(add_entities),
        add_fields=tuple(add_fields),
        add_endpoints=tuple(add_endpoints),
        add_pages=tuple(add_pages),
        new_files=tuple(new_files),
    )


@dataclass
class ProjectSpec:
    """The living contract for a project, persisted across turns."""

    name: str = ""
    summary: str = ""
    language: str = ""
    backend: str = ""
    revision: int = 1
    spec_version: int = SPEC_VERSION
    entities: tuple[Entity, ...] = ()
    endpoints: tuple[SpecEndpoint, ...] = ()
    pages: tuple[Page, ...] = ()
    features: tuple[SpecFeature, ...] = ()
    files: dict[str, FileRecord] = field(default_factory=dict)  # path -> record
    history: tuple[HistoryEntry, ...] = ()

    def __post_init__(self) -> None:
        # `files` was `path -> role` before D2, and both shapes still arrive:
        # `_load_files` accepts a pre-D2 `project.json`, and plenty of callers
        # (and tests) build a spec in code with the bare role. Normalising at the
        # one point of construction means every reader can rely on the record
        # type without each one having to re-check — the alternative is an
        # `isinstance` at every use site, and the first one anybody forgets is a
        # crash inside `save()`, which is best-effort and would swallow it.
        if any(not isinstance(v, FileRecord) for v in self.files.values()):
            self.files = {
                name: (
                    value
                    if isinstance(value, FileRecord)
                    else FileRecord(role=str(value or _role_for(name))[:20])
                )
                for name, value in self.files.items()
            }

    # -- lookup ----------------------------------------------------------

    def entity(self, name: str) -> Entity | None:
        low = (name or "").lower()
        return next(
            (
                e
                for e in self.entities
                if e.name.lower() == low or e.table.lower() == low
            ),
            None,
        )

    def is_empty(self) -> bool:
        return not (self.entities or self.endpoints or self.pages)

    # -- schema ----------------------------------------------------------

    def ddl(self, dialect: Dialect = SQLITE) -> list[str]:
        """`CREATE TABLE IF NOT EXISTS` per entity, ready to execute."""
        return [e.to_ddl(dialect) for e in self.entities]

    def migrations(self, since: int = 0, dialect: Dialect = SQLITE) -> list[str]:
        """Column-addition calls for every field added after ``since``.

        This is why fields carry `added_in`. Adding a field to an entity in turn
        3 must not mean dropping the table — it means one more idempotent
        `ALTER TABLE ADD COLUMN`, which runs on startup against the rows already
        stored. The scaffold ships the primitive on both stacks:
        `db.ensure_column` (sqlite, a PRAGMA read then an ALTER) and
        `db.ensureColumn` (PostgreSQL, `ADD COLUMN IF NOT EXISTS`).
        """
        out: list[str] = []
        for entity in self.entities:
            for f in entity.fields:
                if f.added_in > since and not f.pk:
                    out.append(dialect.migration_call(entity.table, f.name, f.type))
        return out

    # -- prompt threading -------------------------------------------------

    def to_context_block(self) -> str:
        """A compact, factual statement of what already exists.

        The load-bearing method of the phase: it replaces "let the model re-read
        the chat history and re-infer the schema" with a contract it can just
        read. Budgeted hard (`CONTEXT_BUDGET_CHARS`) because it rides in the same
        prompt as the plan manifest and sibling context inside `llm_num_ctx` —
        `purpose` prose is dropped first, then whole sections from the bottom up,
        so the schema (the part a migration depends on) is the last thing to go.
        """
        header = f"## This project already exists — revision {self.revision}"
        if self.summary:
            header += f"\n{self.summary}"

        sections: list[str] = []
        if self.entities:
            sections.append(
                "### Data — these tables exist; use these EXACT names\n"
                + "\n".join(f"- {e.summary()}" for e in self.entities)
            )
        if self.endpoints:
            sections.append(
                "### Routes that already exist — do not redefine or rename them\n"
                + "\n".join(f"- {e.to_line()}" for e in self.endpoints)
            )
        if self.pages:
            sections.append(
                "### Pages that already exist\n"
                + "\n".join(f"- {p.to_line()}" for p in self.pages)
            )

        block = "\n\n".join([header] + sections)
        while len(block) > CONTEXT_BUDGET_CHARS and sections:
            sections.pop()  # drop from the bottom: pages, then routes
            block = "\n\n".join([header] + sections)
        return block[:CONTEXT_BUDGET_CHARS]

    # -- amendment --------------------------------------------------------

    def merge_delta(self, delta: SpecDelta, request: str = "") -> list[str]:
        """Apply an amendment, bump the revision, and record it in history.

        Returns the files the spec itself knows are affected — the handlers and
        templates named by the changed endpoints/pages, plus `db.py`/`models.py`/
        `seed.py` when the schema moved. Phase 3's `impact.py` owns the full
        rule set (which display templates read a changed entity, and so on);
        this is the part derivable from the spec alone.
        """
        self.revision += 1
        rev = self.revision
        impacted: set[str] = set()
        added: list[str] = []

        for entity in delta.add_entities:
            if self.entity(entity.name) is None and len(self.entities) < MAX_ENTITIES:
                stamped = Entity(
                    name=entity.name,
                    table=entity.table,
                    fields=tuple(
                        Field(f.name, f.type, f.pk, f.required, rev)
                        for f in entity.fields
                    ),
                )
                self.entities = self.entities + (stamped,)
                added.append(f"entity {entity.name}")

        for entity_name, new_field in delta.add_fields:
            existing = self.entity(entity_name)
            if existing is None or existing.field(new_field.name) is not None:
                continue
            stamped = Field(
                new_field.name, new_field.type, new_field.pk, new_field.required, rev
            )
            replaced = Entity(
                name=existing.name,
                table=existing.table,
                fields=existing.fields + (stamped,),
            )
            self.entities = tuple(
                replaced if e.name == existing.name else e for e in self.entities
            )
            added.append(f"{existing.name}.{new_field.name}")

        for endpoint in delta.add_endpoints:
            if any(
                e.method == endpoint.method and e.path == endpoint.path
                for e in self.endpoints
            ):
                continue
            if len(self.endpoints) >= MAX_ENDPOINTS:
                break
            self.endpoints = self.endpoints + (
                SpecEndpoint(
                    endpoint.method,
                    endpoint.path,
                    endpoint.request,
                    endpoint.response,
                    endpoint.handler or "app.py",
                    endpoint.template,
                    endpoint.entity,
                    rev,
                ),
            )
            added.append(f"{endpoint.method} {endpoint.path}")
            impacted.add(endpoint.handler or "app.py")
            if endpoint.template:
                impacted.add(endpoint.template)

        for page in delta.add_pages:
            if any(
                p.route == page.route and p.template == page.template
                for p in self.pages
            ):
                continue
            if len(self.pages) >= MAX_PAGES:
                break
            self.pages = self.pages + (
                Page(
                    page.route,
                    page.template,
                    page.nav_label,
                    page.purpose,
                    page.reads,
                    rev,
                ),
            )
            added.append(page.route or page.template)
            if page.template:
                impacted.add(page.template)
            impacted.add("templates/base.html")  # the nav lives there

        if delta.add_entities or delta.add_fields:
            impacted.update({"db.py", "models.py", "seed.py"})

        self.history = (
            self.history
            + (HistoryEntry(revision=rev, request=request[:200], added=tuple(added)),)
        )[-MAX_HISTORY:]
        if delta.summary:
            self.summary = (
                (self.summary + "; " + delta.summary)[:300]
                if self.summary
                else delta.summary[:300]
            )
        return sorted(impacted)

    def reconcile_with_disk(self, root: Path | str) -> list[str]:
        """Fold what is really on disk back into the spec (D3). Additive.

        Only `_run_blueprint` and `_amend_project` ever wrote the spec, so an
        ordinary `_file_op_flow` edit that added a route left memory describing a
        project that no longer existed — and the *next* amendment then planned
        against the stale contract. This closes that: after any turn that wrote
        files, whatever the files now say is folded back in.

        **Additive on purpose.** New routes, pages and files are recorded;
        nothing is removed. A route that has disappeared is not necessarily gone
        — `impact.vanished_routes` treats a missing route as a REGRESSION to
        restore, and deleting it from the spec here would destroy the very
        evidence that check runs on. Removal stays a deliberate act of the
        amendment flow.

        Returns the human-readable additions, empty when nothing changed.
        """
        try:
            fresh = ProjectSpec.from_disk(root)
        except Exception:
            logger.warning("could not reconcile the spec with %s", root, exc_info=True)
            return []
        if fresh is None:
            return []

        added: list[str] = []
        known = {(e.method, e.path) for e in self.endpoints}
        for endpoint in fresh.endpoints:
            if (endpoint.method, endpoint.path) in known:
                continue
            if len(self.endpoints) >= MAX_ENDPOINTS:
                break
            known.add((endpoint.method, endpoint.path))
            self.endpoints += (
                SpecEndpoint(
                    method=endpoint.method,
                    path=endpoint.path,
                    handler=endpoint.handler,
                    template=endpoint.template,
                    entity=endpoint.entity,
                    added_in=self.revision,
                ),
            )
            added.append(f"{endpoint.method} {endpoint.path}")

        seen_pages = {p.template for p in self.pages if p.template}
        for page in fresh.pages:
            if not page.template or page.template in seen_pages:
                continue
            if len(self.pages) >= MAX_PAGES:
                break
            seen_pages.add(page.template)
            self.pages += (
                Page(
                    route=page.route,
                    template=page.template,
                    nav_label=page.nav_label,
                    reads=page.reads,
                    added_in=self.revision,
                ),
            )
            added.append(page.template)

        # Entities only when we had none: a real `CREATE TABLE` is good evidence,
        # but the declared schema carries `added_in` stamps that drive migrations
        # and re-deriving it from SQL would flatten them all to revision 1.
        if not self.entities and fresh.entities:
            self.entities = fresh.entities
            added.extend(e.table for e in fresh.entities)

        for name, record in fresh.files.items():
            current = self.files.get(name)
            if current is None:
                if len(self.files) >= MAX_FILES:
                    break
                self.files[name] = FileRecord(
                    role=record.role,
                    defines=record.defines,
                    reads=record.reads,
                    revision=self.revision,
                )
                added.append(name)
            elif record.defines and record.defines != current.defines:
                # The file is known but now defines something else — that is the
                # drift this method exists for, so refresh it in place.
                self.files[name] = FileRecord(
                    role=current.role or record.role,
                    purpose=current.purpose,
                    defines=record.defines,
                    reads=record.reads or current.reads,
                    revision=current.revision,
                )
        return added

    def to_readme(self) -> str:
        """The project's README, rendered from what it actually contains.

        The scaffold ships a generic one; this replaces it with the real entity
        and route list, so the file describes THIS project rather than the
        template it started as. Regenerated whenever the spec changes, which is
        the only way a README stays true after an amendment.
        """
        name = self.name or "This project"
        out = [f"# {name}", ""]
        if self.summary:
            out += [self.summary, ""]

        out += [
            "A Flask web application. One process serves the pages, the styles, the",
            "uploaded files and the API — no separate frontend server, no build step.",
            "",
            "## Run it",
            "",
            "```bash",
            "python -m venv .venv",
            r".venv\Scripts\activate        # Windows",
            "source .venv/bin/activate     # macOS / Linux",
            "",
            "pip install -r requirements.txt",
            "python seed.py                # a few rows of demo data",
            "python app.py",
            "```",
            "",
            "Then open <http://127.0.0.1:5000>.",
            "",
        ]

        if self.pages:
            out += ["## Pages", "", "| Route | Template |", "| --- | --- |"]
            out += [
                f"| `{p.route or '—'}` | `{p.template or '—'}` |" for p in self.pages
            ]
            out.append("")

        if self.endpoints:
            out += [
                "## Routes",
                "",
                "| Method | Path | Works with |",
                "| --- | --- | --- |",
            ]
            out += [
                f"| `{e.method}` | `{e.path}` | {e.entity or '—'} |"
                for e in self.endpoints
            ]
            out.append("")

        if self.entities:
            out += ["## Data", ""]
            for entity in self.entities:
                out.append(f"**`{entity.table}`**")
                out.append("")
                out.append("| Column | Type | Notes |")
                out.append("| --- | --- | --- |")
                for f in entity.fields:
                    notes = []
                    if f.pk:
                        notes.append("primary key")
                    if f.required and not f.pk:
                        notes.append("required")
                    if f.added_in > 1:
                        notes.append(f"added in revision {f.added_in}")
                    out.append(f"| `{f.name}` | {f.type} | {', '.join(notes) or '—'} |")
                out.append("")
            out += [
                "Schema changes are additive: a new field becomes an `ensure_column`",
                "call in `db.py` that runs on every start, so existing rows are kept",
                "rather than recreated.",
                "",
            ]

        out += [
            "## Deploy it",
            "",
            "A standard WSGI app (`app:app`), so anything that reads a Procfile runs it:",
            "",
            "```",
            "web: gunicorn app:app --bind 0.0.0.0:$PORT",
            "```",
            "",
            "Both halves of that bind matter: the host supplies `$PORT`, and gunicorn's",
            "default of `127.0.0.1` would accept only connections from inside the",
            "container — the deploy comes up healthy and is still unreachable.",
            "",
            "- **Render** — build `pip install -r requirements.txt`, start `gunicorn app:app --bind 0.0.0.0:$PORT`.",
            "- **Railway / Fly.io** — detected from the Procfile.",
            "- **PythonAnywhere** — point the WSGI config at `app` in `app.py`.",
            "",
            "Set `SECRET_KEY` from the environment rather than shipping the generated",
            "one, and note that SQLite lives on the instance disk: a host with an",
            "ephemeral filesystem resets it on redeploy.",
            "",
            f"<sub>{README_MARKER} — revision {self.revision}.</sub>",
            "",
        ]
        return "\n".join(out)

    # -- persistence ------------------------------------------------------

    @staticmethod
    def path_for(root: Path | str) -> Path:
        return Path(root) / SPEC_DIRNAME / SPEC_FILENAME

    def to_dict(self) -> dict:
        return {
            "spec_version": self.spec_version,
            "revision": self.revision,
            "name": self.name,
            "summary": self.summary,
            "stack": {"language": self.language, "backend": self.backend},
            "entities": [asdict(e) for e in self.entities],
            "endpoints": [asdict(e) for e in self.endpoints],
            "pages": [asdict(p) for p in self.pages],
            "features": [asdict(f) for f in self.features],
            "files": {name: asdict(rec) for name, rec in self.files.items()},
            "history": [asdict(h) for h in self.history],
        }

    def save(self, root: Path | str) -> bool:
        """Write `<root>/.coder/project.json` atomically. Never raises.

        Direct write, deliberately NOT `executor.execute("write_file", …)` — see
        the module docstring: the spec is agent state, and routing it through the
        tool would prompt the approval gate every turn and evict the user's undo
        history. tmp + `os.replace` so a crash mid-write cannot leave a partial
        file that `load()` would then reject.
        """
        target = self.path_for(root)
        tmp = target.with_suffix(".json.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, target)
            return True
        except Exception:
            logger.warning("could not save project spec to %s", target, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    @classmethod
    def load(cls, root: Path | str) -> "ProjectSpec | None":
        """Read the spec back, or None if it is absent, corrupt or unreadable.

        Never raises: a garbled `project.json` must degrade to "no memory",
        which is exactly today's behaviour, not to a broken turn.
        """
        path = cls.path_for(root)
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("could not read project spec at %s", path, exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls.from_dict(data)
        except Exception:
            logger.warning("project spec at %s is malformed", path, exc_info=True)
            return None

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectSpec":
        stack = data.get("stack") if isinstance(data.get("stack"), dict) else {}
        return cls(
            name=str(data.get("name") or "")[:80],
            summary=str(data.get("summary") or "")[:300],
            language=str(stack.get("language") or "")[:20],
            backend=str(stack.get("backend") or "")[:20],
            revision=max(1, int(data.get("revision") or 1)),
            spec_version=int(data.get("spec_version") or SPEC_VERSION),
            entities=_load_entities(data.get("entities")),
            endpoints=_load_endpoints(data.get("endpoints")),
            pages=_load_pages(data.get("pages")),
            features=_load_features(data.get("features")),
            files=_load_files(data.get("files")),
            history=_load_history(data.get("history")),
        )

    # -- construction from a build ----------------------------------------

    @classmethod
    def from_blueprint(
        cls, bp: Blueprint, root: Path | str, name: str = ""
    ) -> "ProjectSpec":
        """Distil a finished build into the contract that outlives the turn.

        The blueprint states the *intent*; ``root`` is read to record what was
        actually produced. Entities come from the contract's free-text
        `data_schema` (parsed into diffable fields), falling back to real
        `CREATE TABLE` statements on disk when the blueprint declared none. Page
        routes are read off `app.py`'s real `@app.route` → `render_template`
        pairs, so the mapping is what shipped rather than one guessed from a
        filename.
        """
        root = Path(root)
        stack: Stack = bp.stack

        # Phase C1: the schema was decided structurally, before the layout, so
        # take it as-is. Parsing it back out of `data_schema` prose would be a
        # lossless-to-lossy-to-lossless round trip that can only lose.
        entities: list[Entity] = list(bp.entities[:MAX_ENTITIES])
        seen_tables: set[str] = {e.table.lower() for e in entities}
        if not entities:
            for line in bp.contract.data_schema:
                parsed = parse_schema_line(line)
                if parsed and parsed.table.lower() not in seen_tables:
                    seen_tables.add(parsed.table.lower())
                    entities.append(parsed)
                if len(entities) >= MAX_ENTITIES:
                    break

        py_sources = _read_python(root)
        if not entities:
            entities = entities_from_sql(py_sources)

        # Phase N0: which files are "the backend" and how a route is spelled in
        # them is the stack's answer, not `.py` + `@app.route`. Imported inside
        # the function because `stacks.flask_adapter` imports this module — a
        # top-level import would close the cycle. On Flask this resolves to
        # exactly the two lines it replaced.
        from app.agent.stacks import get_adapter, key_for_stack

        adapter = get_adapter(
            key_for_stack(
                stack.language if stack else "", stack.backend if stack else ""
            )
        )
        entry_stem = Path(adapter.entry_file).stem
        backend_sources = (
            py_sources
            if adapter.language == "python"
            else _read_sources(root, adapter.source_globs)
        )
        handler_file = (
            adapter.entry_file if (root / adapter.entry_file).is_file() else ""
        )

        # Real route -> template mapping, when the server file exists.
        route_map: dict[str, tuple[str, str]] = {}  # template -> (method, path)
        routes = adapter.routes_from_source(backend_sources.get(entry_stem, ""))
        for method, path, _view, template in routes:
            if template and template not in route_map:
                route_map[template] = (method, path)

        # Endpoints the contract DECLARED, kept only when the build really has
        # them. The context block says "routes that already exist — do not
        # redefine"; listing one that was never written turns that line into an
        # instruction not to build the missing route. Measured live: the
        # blueprint declared `POST /api/login`, the coverage check reported it
        # unwired on the same turn, and the spec claimed it existed.
        real_routes = {(m, p) for m, p, _v, _t in routes}
        backend_text = "\n".join(backend_sources.values())
        endpoints: list[SpecEndpoint] = []
        for ep in bp.contract.endpoints[:MAX_ENDPOINTS]:
            defined = (
                (ep.method, ep.path) in real_routes
                or (
                    # Conservative second chance: a route registered in a way the
                    # parser doesn't recognise still names its path as a literal.
                    bool(backend_text)
                    and f'"{ep.path}"' in backend_text
                )
                or (bool(backend_text) and f"'{ep.path}'" in backend_text)
            )
            if not defined and backend_text:
                continue
            endpoints.append(
                SpecEndpoint(
                    method=ep.method,
                    path=ep.path,
                    request=ep.request,
                    response=ep.response,
                    handler=handler_file,
                    template=_norm_filename(ep.template),
                    # Declared by the layout call (Phase C2) when it said so;
                    # `_guess_entity`'s substring match on the path is now the
                    # fallback rather than the only answer.
                    entity=ep.entity or _guess_entity(ep.path, entities),
                )
            )
        # Routes the build really defined but the contract never mentioned.
        known = {(e.method, e.path) for e in endpoints}
        for method, path, _view, template in routes:
            if (method, path) not in known and len(endpoints) < MAX_ENDPOINTS:
                known.add((method, path))
                endpoints.append(
                    SpecEndpoint(
                        method=method,
                        path=path,
                        handler=handler_file or adapter.entry_file,
                        template=template,
                        entity=_guess_entity(path, entities),
                    )
                )

        pages: list[Page] = []
        seen_templates: set[str] = set()
        files: dict[str, FileRecord] = {}
        for pf in bp.files[:MAX_FILES]:
            fname = _norm_filename(pf.filename)
            if not fname:
                continue
            files[fname] = FileRecord(
                role=pf.role or _role_for(fname),
                purpose=" ".join((pf.instruction or "").split())[:120],
                reads=tuple(pf.reads)[:5],
            )
            # Phase N0: `.ejs` is a page on the Node stack. Without the stack's
            # own extension here a Node build records ZERO pages, and a spec
            # with no pages silently disarms the functional probe's "every page
            # renders" step and `_resolve_target_from_spec`.
            if not fname.lower().endswith((".html", ".htm", adapter.template_ext)):
                continue
            if is_layout_template(root / fname, fname):
                continue  # the shell every page extends is not itself a page
            if len(pages) >= MAX_PAGES:
                continue
            template_key = fname.split(f"{adapter.template_dir}/", 1)[-1]
            method_path = route_map.get(fname) or route_map.get(template_key)
            stem = Path(fname).stem
            seen_templates.add(fname)
            pages.append(
                Page(
                    route=method_path[1] if method_path else _route_for(stem),
                    template=fname,
                    nav_label=_nav_label(stem),
                    purpose=" ".join((pf.instruction or "").split())[:100],
                    # Declared by the layout call (Phase C2) if it declared any;
                    # inferring it from instruction prose is the fallback, and
                    # was "routinely empty on the very listing page that
                    # matters" — the reason `smoke.py` probes every page.
                    reads=(
                        tuple(pf.reads)[:3]
                        or tuple(
                            e.name
                            for e in entities
                            if e.name in (pf.instruction or "").lower()
                        )[:3]
                    ),
                )
            )

        # Pages the build really serves that the blueprint never listed. The
        # scaffold's own `templates/index.html` is the standing example: `GET /`
        # renders it, but it was copied in rather than planned, so without this
        # the home page is absent from the project's own memory.
        for method, path, _view, template in routes:
            if method != "GET" or not template or len(pages) >= MAX_PAGES:
                continue
            resolved = _resolve_template(
                root, template, adapter.template_dir, adapter.template_ext
            )
            if not resolved or resolved in seen_templates:
                continue
            if is_layout_template(root / resolved, resolved):
                continue
            seen_templates.add(resolved)
            stem = Path(resolved).stem
            pages.append(
                Page(
                    route=path,
                    template=resolved,
                    nav_label=_nav_label(stem),
                )
            )

        for rel in sorted(scaffolded_files(root)):
            files.setdefault(rel, FileRecord(role=_role_for(rel)))

        return cls(
            name=name or root.name,
            summary=bp.summary,
            language=stack.language if stack else "",
            backend=stack.backend if stack else "",
            revision=1,
            entities=tuple(entities),
            endpoints=tuple(endpoints),
            pages=tuple(pages),
            features=tuple(
                SpecFeature(f.name, f.tier, tuple(f.files))
                for f in bp.features[:MAX_FEATURES]
            ),
            files=files,
        )

    @classmethod
    def from_disk(cls, root: Path | str) -> "ProjectSpec | None":
        """Recover the contract of a project Coder did NOT build.

        `from_blueprint` has been the only way a spec comes into existence, so
        memory was a privilege of projects built in this session. A repo cloned
        from git, one built before `ProjectSpec` existed, or one whose
        `.coder/project.json` was deleted all route as though the project were
        unknown — no amendment path, no impact analysis, no migrations — even
        though everything needed to read the contract off the files is already
        here: `entities_from_sql` recovers tables from real `CREATE TABLE`s,
        `routes_from_source` reads real `@app.route` → `render_template` pairs,
        and `is_layout_template` knows the shell from a page. This wires them to
        the filesystem instead of to a blueprint.

        **Only what can be SEEN is recorded** — routes really defined, tables
        really created, pages a route really renders — for the same reason
        `from_blueprint` reads `root` rather than trusting the plan: the context
        block says *"these already exist — do not redefine them"*, so a page
        listed here that does not exist reads as an instruction not to build it.

        Returns None unless the project defines at least one route, so an
        ordinary Python folder never acquires an invented contract. Routes
        registered on a Blueprint (`@bp.route`) are not recognised by
        `_ROUTE_RE`, and such a project simply declines to be adopted rather
        than being adopted wrongly.
        """
        root = Path(root)
        # Phase N4: try Python/Flask first — unchanged, so an existing Flask
        # repo adopts exactly as it did — then Node. Whichever yields real
        # routes wins; if neither does, this still declines, so an ordinary
        # folder never acquires an invented contract.
        py_sources = _read_python(root)
        sources, ext, read_strings, language = py_sources, ".py", None, "python"
        read_routes = routes_from_source
        template_dir, template_ext = "templates", ".html"

        if not _has_routes(py_sources, routes_from_source):
            from app.agent.crud_node import js_strings
            from app.agent.stacks import get_adapter

            node = get_adapter("node")
            js_sources = _read_sources(root, node.source_globs)
            if not _has_routes(js_sources, node.routes_from_source):
                return None
            sources, ext, read_strings, language = (
                js_sources,
                ".js",
                js_strings,
                "node",
            )
            read_routes = node.routes_from_source
            template_dir, template_ext = node.template_dir, node.template_ext

        if not sources:
            return None

        # (method, path) -> handler file, keeping the first definition seen.
        handlers: dict[tuple[str, str], str] = {}
        routes: list[tuple[str, str, str, str]] = []
        for stem in sorted(sources):
            for method, path, view, template in read_routes(sources[stem]):
                key = (method, path)
                if key in handlers:
                    continue
                handlers[key] = f"{stem}{ext}"
                routes.append((method, path, view, template))
        if not routes:
            return None

        entities = entities_from_sql(sources, read_strings)

        endpoints: list[SpecEndpoint] = []
        pages: list[Page] = []
        seen_templates: set[str] = set()
        for method, path, _view, template in routes:
            resolved = (
                _resolve_template(root, template, template_dir, template_ext)
                if template
                else ""
            )
            if len(endpoints) < MAX_ENDPOINTS:
                endpoints.append(
                    SpecEndpoint(
                        method=method,
                        path=path[:120],
                        handler=handlers[(method, path)],
                        template=resolved,
                        entity=_guess_entity(path, entities),
                    )
                )
            # A page is a GET whose template is really on disk and is not the
            # shell every other page extends.
            if method != "GET" or not resolved or len(pages) >= MAX_PAGES:
                continue
            if resolved in seen_templates or not (root / resolved).is_file():
                continue
            if is_layout_template(root / resolved, resolved):
                continue
            seen_templates.add(resolved)
            pages.append(
                Page(
                    route=path[:120],
                    template=resolved,
                    nav_label=_nav_label(Path(resolved).stem),
                    reads=_template_reads(root / resolved, entities),
                )
            )

        # D2: record what each file DEFINES, so an edit can be routed to it —
        # handlers get the routes read out of them, pages the entities their
        # markup really mentions.
        defines_by_file: dict[str, list[str]] = {}
        for (method, path), handler in handlers.items():
            defines_by_file.setdefault(handler, []).append(f"{method} {path}")
        page_reads = {p.template: p.reads for p in pages}
        files: dict[str, FileRecord] = {
            rel: FileRecord(
                role=_role_for(rel),
                defines=tuple(sorted(defines_by_file.get(rel, [])))[:12],
                reads=tuple(page_reads.get(rel, ()))[:5],
            )
            for rel in _disk_files(root)
        }
        if language == "node":
            backend = "express"
        else:
            backend = "flask" if any(uses_flask(t) for t in sources.values()) else ""
        return cls(
            name=root.name,
            summary=(
                f"Existing project read from disk: {len(entities)} table(s), "
                f"{len(endpoints)} route(s), {len(pages)} page(s)."
            ),
            language=language,
            backend=backend,
            revision=1,
            entities=tuple(entities),
            endpoints=tuple(endpoints),
            pages=tuple(pages),
            files=files,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_layout_template(path: Path, filename: str = "") -> bool:
    """Is this the shell other pages extend, rather than a page of its own?

    `base.html` has no route and no nav entry — recording it as a page gave it
    the invented route `/base` and the nav label "Base" on a live build, which
    an amendment turn would then try to link to. Detected by name and, more
    reliably, by shape: it defines `{% block %}`s without extending anything.
    """
    name = Path(filename or path).name.lower()
    if name in ("base.html", "layout.html", "layout.ejs"):
        return True
    if name.startswith("_"):
        return True  # a partial / macro file, on either stack
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return "{% block" in text and "{% extends" not in text


def _resolve_template(
    root: Path, template: str, subdir: str = "templates", ext: str = ""
) -> str:
    """`render_template("index.html")` -> the repo-relative path it refers to.

    ``subdir``/``ext`` come from the stack. Express names a view WITHOUT its
    extension (`res.render("products")` -> `views/products.ejs`), so the
    extension has to be supplied rather than assumed to be part of the name.
    Falls back to the conventional path when nothing matches on disk, which is
    what lets a page be recorded before the file is written.
    """
    name = _norm_filename(template)
    if not name:
        return ""
    names = [name]
    if ext and not name.lower().endswith(ext):
        names.append(f"{name}{ext}")
    for candidate in [f"{subdir}/{n}" for n in names] + names:
        if (root / candidate).is_file():
            return candidate
    return f"{subdir}/{names[-1]}"


def _nav_label(stem: str) -> str:
    """A human nav label for a template stem. `index` is "Home", not "Index"."""
    low = (stem or "").lower()
    if low in ("index", "home"):
        return "Home"
    return low.replace("_", " ").replace("-", " ").title()


def _route_for(stem: str) -> str:
    """Fallback route for a template with no matching @app.route on disk."""
    low = (stem or "").lower()
    return "/" if low in ("index", "home") else f"/{low.replace('_', '-')}"


def _guess_entity(path: str, entities: list[Entity]) -> str:
    low = (path or "").lower()
    for e in entities:
        if e.table.lower() in low or e.name.lower() in low:
            return e.name
    return ""


def _role_for(filename: str) -> str:
    low = (filename or "").lower()
    if low.endswith((".html", ".htm")):
        return "page"
    if low.endswith((".css", ".js")):
        return "asset"
    if low.endswith(".py"):
        return "backend"
    return "config"


def _has_routes(sources: dict[str, str], reader) -> bool:
    """Does any of these modules really define a route? Best-effort.

    The gate that decides which stack `from_disk` adopts as, and the reason it
    can decline entirely: a folder of ordinary Python or JavaScript defines no
    routes and must never acquire an invented contract.
    """
    for text in (sources or {}).values():
        try:
            if reader(text):
                return True
        except Exception:
            continue
    return False


def _read_sources(root: Path, globs: tuple[str, ...]) -> dict[str, str]:
    """Top-level sources matching ``globs``, keyed by stem. Best-effort.

    The stack-agnostic form of `_read_python`: the Node adapter's globs pick up
    `server.js` / `models.js` where Flask's pick up `app.py` / `models.py`.
    """
    out: dict[str, str] = {}
    for glob in globs:
        try:
            for path in sorted(root.glob(glob)):
                try:
                    out[path.stem] = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
        except Exception:
            logger.debug("could not list %s in %s", glob, root)
    return out


def _read_python(root: Path) -> dict[str, str]:
    """Top-level `.py` sources, keyed by module name. Best-effort."""
    out: dict[str, str] = {}
    try:
        for path in sorted(root.glob("*.py")):
            try:
                out[path.stem] = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    except Exception:
        logger.debug("could not list python files in %s", root)
    return out


def _template_reads(path: Path, entities: list[Entity]) -> tuple[str, ...]:
    """Which entities a template actually mentions.

    `from_blueprint` infers `reads` from the blueprint's prose, which is why it
    is "routinely empty on the very listing page that matters" (see `smoke.py`).
    A template on disk can simply be read: `{% for product in products %}` names
    both the entity and its table. Best-effort — unreadable file means no claim.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        return ()
    return tuple(e.name for e in entities if e.name in text or e.table.lower() in text)[
        :3
    ]


# Where a web project's real files live. Deliberately shallow: top-level modules
# plus the two trees the fixed layout defines, so a vendored dependency or a
# virtualenv inside the project cannot flood `files` (which is capped anyway,
# and rides in a prompt).
_DISK_GLOBS = (
    "*.py",
    "templates/**/*.html",
    "templates/**/*.htm",
    "static/**/*.css",
    "static/**/*.js",
    # The Node layout (Phase N4). `node_modules/` is not reachable by any of
    # these, which is the point — a vendored dependency tree would flood
    # `files` and it rides in a prompt.
    "*.js",
    "views/**/*.ejs",
    "public/**/*.css",
    "public/**/*.js",
)


def _disk_files(root: Path) -> list[str]:
    """Project files worth recording, as sorted posix-relative paths."""
    out: list[str] = []
    seen: set[str] = set()
    for pattern in _DISK_GLOBS:
        try:
            matches = sorted(root.glob(pattern))
        except Exception:
            continue
        for path in matches:
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            # Dot-directories are skipped everywhere else (the RAG indexer,
            # project_memory) and `.coder/` in particular must never be listed
            # as project source.
            if rel in seen or any(part.startswith(".") for part in Path(rel).parts):
                continue
            seen.add(rel)
            out.append(rel)
            if len(out) >= MAX_FILES:
                return sorted(out)
    return sorted(out)


def scaffolded_files(root: Path) -> list[str]:
    """Project files worth recording in `files` beyond the blueprint's list."""
    out: list[str] = []
    for rel in ("app.py", "db.py", "models.py", "seed.py", "templates/base.html"):
        if (root / rel).is_file():
            out.append(rel)
    return out


def _load_entities(raw) -> tuple[Entity, ...]:
    out: list[Entity] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        table = _ident(item.get("table") or item.get("name"))
        if not table:
            continue
        fields: list[Field] = []
        for f in item.get("fields") or []:
            if not isinstance(f, dict):
                continue
            fname = _ident(f.get("name"))
            if not fname:
                continue
            fields.append(
                Field(
                    name=fname,
                    type=_norm_type(f.get("type")),
                    pk=bool(f.get("pk")),
                    required=bool(f.get("required")),
                    added_in=max(1, int(f.get("added_in") or 1)),
                )
            )
            if len(fields) >= MAX_FIELDS:
                break
        if not fields:
            continue
        out.append(
            Entity(
                name=_ident(item.get("name")) or _singular(table),
                table=table,
                fields=tuple(fields),
            )
        )
        if len(out) >= MAX_ENTITIES:
            break
    return tuple(out)


def _load_endpoints(raw) -> tuple[SpecEndpoint, ...]:
    out: list[SpecEndpoint] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "GET").strip().upper()
        path = str(item.get("path") or "").strip()
        if not path.startswith("/") or method not in (
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ):
            continue
        out.append(
            SpecEndpoint(
                method=method,
                path=path[:120],
                request=str(item.get("request") or "")[:120],
                response=str(item.get("response") or "")[:120],
                handler=_norm_filename(item.get("handler")),
                template=_norm_filename(item.get("template")),
                entity=_ident(item.get("entity")),
                added_in=max(1, int(item.get("added_in") or 1)),
            )
        )
        if len(out) >= MAX_ENDPOINTS:
            break
    return tuple(out)


def _load_pages(raw) -> tuple[Page, ...]:
    out: list[Page] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        template = _norm_filename(item.get("template"))
        route = str(item.get("route") or "")[:120]
        if not template and not route:
            continue
        out.append(
            Page(
                route=route,
                template=template,
                nav_label=str(item.get("nav_label") or "")[:40],
                purpose=str(item.get("purpose") or "")[:100],
                reads=tuple(_ident(r) for r in (item.get("reads") or []) if _ident(r))[
                    :5
                ],
                added_in=max(1, int(item.get("added_in") or 1)),
            )
        )
        if len(out) >= MAX_PAGES:
            break
    return tuple(out)


def _load_features(raw) -> tuple[SpecFeature, ...]:
    out: list[SpecFeature] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:80]
        if not name:
            continue
        out.append(
            SpecFeature(
                name=name,
                tier=str(item.get("tier") or "core")[:12],
                files=tuple(
                    _norm_filename(f)
                    for f in (item.get("files") or [])
                    if _norm_filename(f)
                )[:8],
                added_in=max(1, int(item.get("added_in") or 1)),
            )
        )
        if len(out) >= MAX_FEATURES:
            break
    return tuple(out)


def _load_files(raw) -> dict[str, FileRecord]:
    """Read `files` back, accepting BOTH shapes.

    A pre-D2 `project.json` stored `"app.py": "backend"`; the current one stores
    a record. Both load — an old spec simply arrives with role-only records,
    which is exactly what it knew.
    """
    out: dict[str, FileRecord] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        name = _norm_filename(key)
        if not name:
            continue
        if isinstance(value, dict):
            out[name] = FileRecord(
                role=str(value.get("role") or _role_for(name))[:20],
                purpose=" ".join(str(value.get("purpose") or "").split())[:120],
                defines=tuple(
                    " ".join(str(d).split())[:60] for d in (value.get("defines") or [])
                )[:12],
                reads=tuple(_ident(r) for r in (value.get("reads") or []) if _ident(r))[
                    :5
                ],
                revision=max(1, int(value.get("revision") or 1)),
            )
        else:
            out[name] = FileRecord(role=str(value or _role_for(name))[:20])
        if len(out) >= MAX_FILES:
            break
    return out


def _load_history(raw) -> tuple[HistoryEntry, ...]:
    out: list[HistoryEntry] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        out.append(
            HistoryEntry(
                revision=max(1, int(item.get("revision") or 1)),
                request=str(item.get("request") or "")[:200],
                added=tuple(str(a)[:80] for a in (item.get("added") or []))[:12],
            )
        )
        if len(out) >= MAX_HISTORY:
            break
    return tuple(out)
