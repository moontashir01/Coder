"""Deterministic data layer — generate `db.py`, `models.py` and `seed.py` from the spec.

Phase 4a/4d of `docs/fullstack-web-plan.md`, and the direct answer to the two
failures still open after Phase 3, both measured on live builds:

  * `init_db()` shipped with no `CREATE TABLE`, so every route touching the table
    500'd with "no such table: products" (`docs/phase1-notes.md`).
  * `app.py` called `models.get_all_posts(...)` while `models.py` defined only
    `add_post`, so the route died with `AttributeError` (`docs/phase1-notes.md`).

Both are files that are **100% derivable from `spec.entities`**. There is nothing
for a model to decide: the table is the fields, the query is the table, the seed
row is the field types. So they stop being generated. `ProjectSpec` (Phase 2)
made the entity list structured, which is what makes this possible at all — and
the LLM is left to write only the routes and the page bodies, which is where its
judgement is actually worth something.

Two properties fall out for free rather than being checked for:

  * **SQL injection is impossible by construction.** Every value is bound as a
    `?` parameter. Identifiers (table and column names) come from
    `projectspec._ident`, which admits only `[A-Za-z_][A-Za-z0-9_]*`, so they
    cannot carry SQL either.
  * **Column lists always match the table**, because both are printed from the
    same `Entity`.

Pure and offline (design rule 2): strings in, strings out, no filesystem and no
LLM, so the generated SQL can be unit-tested by executing it against a real
in-memory sqlite3.
"""

from __future__ import annotations

import re

from app.agent.projectspec import Entity, Field, ProjectSpec
from app.agent.pyimports import searchable_sql

# Extensions an upload is allowed to have. An allowlist, not a denylist: the
# question "is this dangerous?" has no reliable answer, but "is this an image?"
# does.
ALLOWED_UPLOAD_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")

# Field names that mean "this holds a secret" — used by the plaintext-password
# check, which is deliberately a check on the CODE rather than a line in a prompt.
_SECRET_NAME_RE = re.compile(r"(password|passwd|secret|token|api_?key)", re.IGNORECASE)
# A hashed value is fine; the raw request field is not.
_HASH_CALL_RE = re.compile(
    r"(generate_password_hash|hashpw|pbkdf2|bcrypt|scrypt|argon|sha256|hashlib)",
    re.IGNORECASE,
)


def _pk(entity: Entity) -> Field | None:
    return next((f for f in entity.fields if f.pk), None)


def _writable(entity: Entity) -> list[Field]:
    """Fields a caller supplies — everything except an autoincrement key."""
    return [f for f in entity.fields if not (f.pk and f.type == "INTEGER")]


def _sample(field: Field, index: int) -> str:
    """A plausible demo value, as a Python literal.

    Seeded rows exist so no page is ever empty on first load — an empty list in
    a demo reads as broken even when it is correct.
    """
    name = field.name.lower()
    if field.is_upload():
        return '""'  # no file on disk yet; the template falls back
    if field.type == "INTEGER":
        return str(index)
    if field.type == "REAL":
        return f"{9.99 + index:.2f}"
    if "email" in name:
        return f'"demo{index}@example.com"'
    if "url" in name or "link" in name:
        return f'"https://example.com/{index}"'
    if _SECRET_NAME_RE.search(name):
        # Never a real password, and never plaintext — see plaintext_password_writes.
        return 'generate_password_hash("demo-password")'
    if "date" in name or "time" in name:
        return '"2026-01-01"'
    if "desc" in name or "body" in name or "content" in name or "text" in name:
        return f'"Demo {name} number {index}."'
    return f'"Demo {field.name} {index}"'


# ---------------------------------------------------------------------------
# models.py
# ---------------------------------------------------------------------------


def entity_helpers(entity: Entity) -> str:
    """`list_ / get_ / create_ / update_ / delete_` for one entity."""
    pk = _pk(entity)
    table, name = entity.table, entity.name
    writable = _writable(entity)
    cols = ", ".join(f.name for f in writable)
    marks = ", ".join("?" for _ in writable)
    args = ", ".join(f.name for f in writable)

    order = f" ORDER BY {pk.name} DESC" if pk else ""
    parts = [
        f"def list_{table}():\n"
        f'    """Every {name}, newest first."""\n'
        f"    conn = get_db()\n"
        f"    try:\n"
        f'        rows = conn.execute("SELECT * FROM {table}{order}").fetchall()\n'
        f"        return [dict(row) for row in rows]\n"
        f"    finally:\n"
        f"        conn.close()\n"
    ]

    if pk:
        parts.append(
            f"def get_{name}({pk.name}):\n"
            f'    """One {name} by {pk.name}, or None."""\n'
            f"    conn = get_db()\n"
            f"    try:\n"
            f"        row = conn.execute(\n"
            f'            "SELECT * FROM {table} WHERE {pk.name} = ?", ({pk.name},)\n'
            f"        ).fetchone()\n"
            f"        return dict(row) if row else None\n"
            f"    finally:\n"
            f"        conn.close()\n"
        )

    if writable:
        parts.append(
            f"def create_{name}({args}):\n"
            f'    """Insert one {name}; returns its new id."""\n'
            f"    conn = get_db()\n"
            f"    try:\n"
            f"        cur = conn.execute(\n"
            f'            "INSERT INTO {table} ({cols}) VALUES ({marks})",\n'
            f"            ({args}{',' if len(writable) == 1 else ''}),\n"
            f"        )\n"
            f"        conn.commit()\n"
            f"        return cur.lastrowid\n"
            f"    finally:\n"
            f"        conn.close()\n"
        )

    # The key identifies the row, so it is never also one of the columns being
    # set — otherwise a TEXT primary key (users keyed on email) generates
    # `def update_user(email, email, ...)`, which is a SyntaxError.
    updatable = [f for f in writable if not f.pk]
    if pk and updatable:
        assignments = ", ".join(f"{f.name} = ?" for f in updatable)
        update_args = ", ".join(f.name for f in updatable)
        parts.append(
            f"def update_{name}({pk.name}, {update_args}):\n"
            f'    """Overwrite one {name}."""\n'
            f"    conn = get_db()\n"
            f"    try:\n"
            f"        conn.execute(\n"
            f'            "UPDATE {table} SET {assignments} WHERE {pk.name} = ?",\n'
            f"            ({update_args}, {pk.name}),\n"
            f"        )\n"
            f"        conn.commit()\n"
            f"    finally:\n"
            f"        conn.close()\n"
        )
        parts.append(
            f"def delete_{name}({pk.name}):\n"
            f'    """Remove one {name}."""\n'
            f"    conn = get_db()\n"
            f"    try:\n"
            f'        conn.execute("DELETE FROM {table} WHERE {pk.name} = ?", ({pk.name},))\n'
            f"        conn.commit()\n"
            f"    finally:\n"
            f"        conn.close()\n"
        )

    # A lookup by the natural key people actually log in with.
    email = next((f for f in entity.fields if "email" in f.name.lower()), None)
    if email and (not pk or pk.name != email.name):
        parts.append(
            f"def get_{name}_by_{email.name}({email.name}):\n"
            f'    """One {name} looked up by {email.name}, or None."""\n'
            f"    conn = get_db()\n"
            f"    try:\n"
            f"        row = conn.execute(\n"
            f'            "SELECT * FROM {table} WHERE {email.name} = ?", ({email.name},)\n'
            f"        ).fetchone()\n"
            f"        return dict(row) if row else None\n"
            f"    finally:\n"
            f"        conn.close()\n"
        )

    return "\n\n".join(parts)


def api_context(spec: ProjectSpec) -> str:
    """The EXACT functions `models.py` provides, for the generation prompt.

    Without this the model invents an API against the file it can no longer see
    being written. Measured live: `app.py` opened with
    `from models import get_user_by_email, get_all_products, User, Product` —
    four names, none of which exist — and the app died at import with an
    ImportError before serving a single page. Taking the data layer away from
    the model is only safe if the model is told what replaced it.
    """
    if not spec.entities:
        return ""
    lines: list[str] = []
    for entity in spec.entities:
        for signature in _signatures(entity):
            lines.append(f"- `models.{signature}`")
    return (
        "## The data layer is ALREADY WRITTEN — call it, do not redefine it\n"
        "`models.py` and the tables in `db.py` are generated from this project's "
        "schema. Use `import models` and call EXACTLY these functions. Do not "
        "invent other names, do not import classes (there are none — every "
        "helper returns plain dicts), and do not write SQL in `app.py`:\n"
        + "\n".join(lines)
        + "\n`db.init_db()` already creates every table at startup."
    )


def _signatures(entity: Entity) -> list[str]:
    """The call signatures `entity_helpers` emits, in the same order."""
    pk = _pk(entity)
    writable = _writable(entity)
    updatable = [f for f in writable if not f.pk]
    args = ", ".join(f.name for f in writable)
    out = [f"list_{entity.table}() -> list[dict]"]
    if pk:
        out.append(f"get_{entity.name}({pk.name}) -> dict | None")
    if writable:
        out.append(f"create_{entity.name}({args}) -> new id")
    if pk and updatable:
        update_args = ", ".join(f.name for f in updatable)
        out.append(f"update_{entity.name}({pk.name}, {update_args})")
        out.append(f"delete_{entity.name}({pk.name})")
    email = next((f for f in entity.fields if "email" in f.name.lower()), None)
    if email and (not pk or pk.name != email.name):
        out.append(f"get_{entity.name}_by_{email.name}({email.name}) -> dict | None")
    return out


def models_source(spec: ProjectSpec) -> str:
    """The whole of `models.py`, derived from the spec's entities."""
    header = (
        f'"""Database queries for {spec.name or "this project"} — '
        "written by Coder from the project spec.\n\n"
        "One function per operation. Every value is bound as a `?` parameter and\n"
        "never formatted into the SQL, so user input can never become SQL. The\n"
        "column lists are printed from the same entity definition as the tables in\n"
        "db.py, so the two cannot drift apart.\n"
        '"""\n\n'
        "from db import get_db\n"
    )
    if any(f.is_upload() for e in spec.entities for f in e.fields) or any(
        _SECRET_NAME_RE.search(f.name) for e in spec.entities for f in e.fields
    ):
        header += "from werkzeug.security import generate_password_hash  # noqa: F401\n"
    bodies = [entity_helpers(e) for e in spec.entities if e.fields]
    return header + "\n\n" + "\n\n".join(bodies) if bodies else header


# ---------------------------------------------------------------------------
# seed.py
# ---------------------------------------------------------------------------


def seed_source(spec: ProjectSpec, rows: int = 3) -> str:
    """`seed.py` with a few demo rows per entity.

    An empty page in a demo reads as broken even when it is working perfectly,
    so every table starts with something in it.
    """
    needs_hash = any(
        _SECRET_NAME_RE.search(f.name) for e in spec.entities for f in e.fields
    )
    lines = [
        f'"""Demo data for {spec.name or "this project"} — '
        "written by Coder from the project spec.",
        "",
        "Run it with:  python seed.py",
        "",
        "`INSERT OR IGNORE` keeps it safe to run more than once.",
        '"""',
        "",
        "import db",
    ]
    if needs_hash:
        lines.append("from werkzeug.security import generate_password_hash")
    lines += [
        "",
        "",
        "def seed():",
        '    """Insert demo rows. Safe to run repeatedly."""',
    ]
    if not spec.entities:
        lines += [
            "    pass",
            "",
            "",
            'if __name__ == "__main__":',
            "    db.init_db()",
            "    seed()",
            '    print("Seeded the database.")',
            "",
        ]
        return "\n".join(lines)

    lines += ["    conn = db.get_db()", "    try:"]
    for entity in spec.entities:
        writable = _writable(entity)
        if not writable:
            continue
        cols = ", ".join(f.name for f in writable)
        marks = ", ".join("?" for _ in writable)
        lines.append(f"        # {entity.name}")
        for i in range(1, rows + 1):
            values = ", ".join(_sample(f, i) for f in writable)
            lines.append(
                f"        conn.execute(\n"
                f'            "INSERT OR IGNORE INTO {entity.table} ({cols}) '
                f'VALUES ({marks})",\n'
                f"            ({values}{',' if len(writable) == 1 else ''}),\n"
                f"        )"
            )
    lines += [
        "        conn.commit()",
        "    finally:",
        "        conn.close()",
        "",
        "",
        'if __name__ == "__main__":',
        "    db.init_db()",
        "    seed()",
        '    print("Seeded the database.")',
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# db.py — the CREATE TABLE half
# ---------------------------------------------------------------------------

_INIT_DB_RE = re.compile(r"^def init_db\(\).*?:\n", re.MULTILINE)
_COMMIT_RE = re.compile(r"^(\s*)conn\.commit\(\)", re.MULTILINE)


def table_block(spec: ProjectSpec) -> str:
    """`conn.execute(...)` lines creating every table the spec declares."""
    out: list[str] = []
    for statement in spec.ddl():
        indented = "\n".join("            " + line for line in statement.splitlines())
        out.append(
            f'        conn.execute(\n            """\n{indented}\n            """\n        )'
        )
    return "\n".join(out)


def apply_table_block(source: str, spec: ProjectSpec) -> tuple[str, bool]:
    """Put the CREATE TABLE statements into `init_db()`, before `conn.commit()`.

    Idempotent per table, and declines rather than guessing when `init_db()`
    isn't recognisable — a half-edited schema file is worse than none.
    """
    text = source or ""
    missing = [e for e in spec.entities if not _creates_table(text, e.table)]
    if not missing:
        return source, False

    init_match = _INIT_DB_RE.search(text)
    if not init_match:
        return source, False
    commit = _COMMIT_RE.search(text, init_match.end())
    if not commit:
        return source, False

    block = table_block(ProjectSpec(entities=tuple(missing)))
    if not block:
        return source, False
    at = commit.start()
    return text[:at] + block + "\n\n" + text[at:], True


def _creates_table(source: str, table: str) -> bool:
    """Does this module already create `table`? Guards idempotency.

    Scans string literals only. Scanning raw text made the scaffold's own
    *commented* example (`# CREATE TABLE IF NOT EXISTS products (…)`) count as a
    real table, so on a live build the products table was never created while
    users was — and every product route 500'd with "no such table".
    """
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?"
        + re.escape(table)
        + r"\b",
        re.IGNORECASE,
    )
    return any(pattern.search(literal) for literal in searchable_sql(source or ""))


# ---------------------------------------------------------------------------
# Uploads (4b)
# ---------------------------------------------------------------------------


def has_uploads(spec: ProjectSpec) -> bool:
    return any(f.is_upload() for e in spec.entities for f in e.fields)


def upload_helper_source() -> str:
    """A `save_upload()` for app.py: allowlisted, jailed, collision-safe."""
    exts = ", ".join(f'"{e}"' for e in ALLOWED_UPLOAD_EXTENSIONS)
    return (
        "\n\nALLOWED_UPLOAD_EXTENSIONS = {" + exts + "}\n\n\n"
        "def save_upload(file_storage):\n"
        '    """Save an uploaded image and return the filename to store in the row.\n\n'
        '    Returns "" when nothing usable was sent, so a form posted without a\n'
        "    file still works instead of raising.\n"
        '    """\n'
        '    if file_storage is None or not getattr(file_storage, "filename", ""):\n'
        '        return ""\n'
        "    name = secure_filename(file_storage.filename)\n"
        '    if "." not in name:\n'
        '        return ""\n'
        '    ext = name.rsplit(".", 1)[1].lower()\n'
        "    if ext not in ALLOWED_UPLOAD_EXTENSIONS:\n"
        '        return ""\n'
        "    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)\n"
        "    target = UPLOAD_DIR / name\n"
        "    stem = target.stem\n"
        "    counter = 1\n"
        "    while target.exists():\n"
        '        target = UPLOAD_DIR / f"{stem}-{counter}.{ext}"\n'
        "        counter += 1\n"
        "    file_storage.save(target)\n"
        "    return target.name\n"
    )


# ---------------------------------------------------------------------------
# 4c — the one part of auth that must never be a prompt instruction
# ---------------------------------------------------------------------------

_ASSIGN_SECRET_RE = re.compile(
    r"""(?P<col>\w*(?:password|passwd|secret|token)\w*)\s*=\s*"""
    r"""request\.(?:form|json|args|values)(?:\.get)?[\[(]\s*["'][^"']+["']\s*[\])]""",
    re.IGNORECASE,
)


def plaintext_password_writes(source: str) -> list[str]:
    """Lines that put a raw request password somewhere it will be stored.

    A deterministic check on the CODE, deliberately not a line in a prompt: a
    prompt instruction is advice, and this is the one thing that must not be
    left to advice. Only flags an assignment that is never hashed anywhere in
    the module, so a `pw = request.form["password"]` followed by
    `generate_password_hash(pw)` is correctly left alone.
    """
    text = source or ""
    if _HASH_CALL_RE.search(text):
        return []
    return [m.group(0) for m in _ASSIGN_SECRET_RE.finditer(text)]
