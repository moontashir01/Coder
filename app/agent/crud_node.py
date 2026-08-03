"""Deterministic data layer for the Node stack — `db.js`, `models.js`, `seed.js`.

Phase N3 of `docs/node-stack-plan.md`, and the exact mirror of `crud.py`. Same
argument, one stack over: these three files contain no decisions — the table IS
the fields, the query IS the table, the demo row IS the field types — and a 7B
asked to write them produces, measurably, an `initDb()` with no `CREATE TABLE`
and a `server.js` calling helpers `models.js` never exported.

**Both stacks emit from the same `Entity` objects.** That is the property worth
protecting: `crud.py` and this module are two spellings of one schema, not two
schemas. Everything that differs between them lives in `projectspec.Dialect`
(placeholder, autoincrement key, type map, migration call), so a change to the
entity model reaches both, and neither can quietly drift.

The three properties `crud.py` gets by construction hold here for the same
reasons:

  * **SQL injection is impossible.** Every value is bound as `$1, $2, …`.
    Identifiers come from `projectspec._ident`, which admits only
    `[A-Za-z_][A-Za-z0-9_]*`, so they cannot carry SQL either.
  * **Column lists cannot drift from the tables**, because both are printed from
    the same `Entity`.
  * **`api_context()` is not optional.** Taking `models.js` away from the model
    is only safe if the model is TOLD what replaced it — otherwise it invents an
    API and the app dies on the first request with "models.getAllProducts is not
    a function".

Two things PostgreSQL forces that sqlite did not:

  * **`RETURNING id`.** There is no `lastrowid`, so an insert that needs to know
    the row it created must ask for it in the statement.
  * **Async everywhere.** `pg` is promise-based, so every helper is `async` and
    every caller must `await`. A missing `await` yields a Promise where a row was
    expected and renders `[object Promise]` — which is why `api_context` states
    it outright rather than hoping.

Pure and offline (design rule 2): strings in, strings out, no filesystem and no
LLM, so the generated SQL can be executed against a real PostgreSQL in tests.
"""

from __future__ import annotations

import re

from app.agent.crud import _SECRET_NAME_RE, ALLOWED_UPLOAD_EXTENSIONS
from app.agent.projectspec import POSTGRES, Entity, Field, ProjectSpec

# NB there is deliberately no string-literal regex here: separating literals
# from comments in JavaScript cannot be done in two regex passes in either
# order. See `_scan`, which does it in one walk.


def _camel(name: str) -> str:
    """`add_product` / `products` -> `addProduct` / `products`.

    JavaScript's convention, and not cosmetic: a 7B writing a call site reaches
    for camelCase by habit, so a snake_case export is a call that fails at
    runtime on a page that was otherwise correct.
    """
    parts = [p for p in re.split(r"[_\s-]+", str(name or "")) if p]
    if not parts:
        return ""
    return parts[0].lower() + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _pascal(name: str) -> str:
    camel = _camel(name)
    return camel[:1].upper() + camel[1:]


def _pk(entity: Entity) -> Field | None:
    return next((f for f in entity.fields if f.pk), None)


def _writable(entity: Entity) -> list[Field]:
    """Fields a caller supplies — everything except an autoincrement key."""
    return [f for f in entity.fields if not (f.pk and f.type == "INTEGER")]


def _sample(field: Field, index: int) -> str:
    """A plausible demo value, as a JavaScript literal.

    Seeded rows exist so no page is ever empty on first load — an empty list in
    a demo reads as broken even when it is correct. Mirrors `crud._sample` value
    for value, so the two stacks' demo data look like the same product.
    """
    name = field.name.lower()
    if field.is_upload():
        return '""'  # no file on disk yet; the view falls back
    if field.type == "INTEGER":
        return str(index)
    if field.type == "REAL":
        return f"{9.99 + index:.2f}"
    if "email" in name:
        return f'"demo{index}@example.com"'
    if "url" in name or "link" in name:
        return f'"https://example.com/{index}"'
    if _SECRET_NAME_RE.search(name):
        # Never a real password, and never plaintext.
        return 'await hashPassword("demo-password")'
    if "date" in name or "time" in name:
        return '"2026-01-01"'
    if "desc" in name or "body" in name or "content" in name or "text" in name:
        return f'"Demo {name} number {index}."'
    return f'"Demo {field.name} {index}"'


# ---------------------------------------------------------------------------
# models.js
# ---------------------------------------------------------------------------


def entity_helpers(entity: Entity) -> str:
    """`list / get / create / update / delete` for one entity, as async functions."""
    pk = _pk(entity)
    table, name = entity.table, entity.name
    writable = _writable(entity)
    cols = ", ".join(f.name for f in writable)
    marks = POSTGRES.placeholders(len(writable))
    args = ", ".join(_camel(f.name) for f in writable)

    order = f" ORDER BY {pk.name} DESC" if pk else ""
    parts = [
        f"/** Every {name}, newest first. */\n"
        f"async function list{_pascal(table)}() {{\n"
        f'  const {{ rows }} = await getPool().query("SELECT * FROM {table}{order}");\n'
        f"  return rows;\n"
        f"}}\n"
    ]

    if pk:
        arg = _camel(pk.name)
        parts.append(
            f"/** One {name} by {pk.name}, or null. */\n"
            f"async function get{_pascal(name)}({arg}) {{\n"
            f"  const {{ rows }} = await getPool().query(\n"
            f'    "SELECT * FROM {table} WHERE {pk.name} = $1",\n'
            f"    [{arg}]\n"
            f"  );\n"
            f"  return rows[0] || null;\n"
            f"}}\n"
        )

    if writable:
        # RETURNING id, not lastrowid: PostgreSQL has no out-of-band way to
        # report the key it just generated.
        returning = f" RETURNING {pk.name}" if pk else ""
        result = f"  return rows[0].{pk.name};\n" if pk else "  return null;\n"
        parts.append(
            f"/** Insert one {name}; returns its new "
            f"{pk.name if pk else 'id'}. */\n"
            f"async function create{_pascal(name)}({args}) {{\n"
            f"  const {{ rows }} = await getPool().query(\n"
            f'    "INSERT INTO {table} ({cols}) VALUES ({marks}){returning}",\n'
            f"    [{args}]\n"
            f"  );\n" + result + "}\n"
        )

    # The key identifies the row, so it is never also one of the columns being
    # set — otherwise a TEXT primary key (users keyed on email) generates
    # `updateUser(email, email, …)`, which is a duplicate-parameter error.
    updatable = [f for f in writable if not f.pk]
    if pk and updatable:
        assignments = ", ".join(
            f"{f.name} = {POSTGRES.placeholder(i)}"
            for i, f in enumerate(updatable, start=1)
        )
        key_mark = POSTGRES.placeholder(len(updatable) + 1)
        update_args = ", ".join(_camel(f.name) for f in updatable)
        pk_arg = _camel(pk.name)
        parts.append(
            f"/** Overwrite one {name}. */\n"
            f"async function update{_pascal(name)}({pk_arg}, {update_args}) {{\n"
            f"  await getPool().query(\n"
            f'    "UPDATE {table} SET {assignments} WHERE {pk.name} = {key_mark}",\n'
            f"    [{update_args}, {pk_arg}]\n"
            f"  );\n"
            f"}}\n"
        )
        parts.append(
            f"/** Remove one {name}. */\n"
            f"async function delete{_pascal(name)}({pk_arg}) {{\n"
            f"  await getPool().query(\n"
            f'    "DELETE FROM {table} WHERE {pk.name} = $1",\n'
            f"    [{pk_arg}]\n"
            f"  );\n"
            f"}}\n"
        )

    # A lookup by the natural key people actually log in with.
    email = next((f for f in entity.fields if "email" in f.name.lower()), None)
    if email and (not pk or pk.name != email.name):
        arg = _camel(email.name)
        parts.append(
            f"/** One {name} looked up by {email.name}, or null. */\n"
            f"async function get{_pascal(name)}By{_pascal(email.name)}({arg}) {{\n"
            f"  const {{ rows }} = await getPool().query(\n"
            f'    "SELECT * FROM {table} WHERE {email.name} = $1",\n'
            f"    [{arg}]\n"
            f"  );\n"
            f"  return rows[0] || null;\n"
            f"}}\n"
        )

    return "\n".join(parts)


def _exports(entity: Entity) -> list[str]:
    """The names `entity_helpers` defines, in the same order."""
    pk = _pk(entity)
    writable = _writable(entity)
    updatable = [f for f in writable if not f.pk]
    out = [f"list{_pascal(entity.table)}"]
    if pk:
        out.append(f"get{_pascal(entity.name)}")
    if writable:
        out.append(f"create{_pascal(entity.name)}")
    if pk and updatable:
        out.append(f"update{_pascal(entity.name)}")
        out.append(f"delete{_pascal(entity.name)}")
    email = next((f for f in entity.fields if "email" in f.name.lower()), None)
    if email and (not pk or pk.name != email.name):
        out.append(f"get{_pascal(entity.name)}By{_pascal(email.name)}")
    return out


def _signatures(entity: Entity) -> list[str]:
    """The call signatures, for `api_context`. Same order as `_exports`."""
    pk = _pk(entity)
    writable = _writable(entity)
    updatable = [f for f in writable if not f.pk]
    args = ", ".join(_camel(f.name) for f in writable)
    out = [f"list{_pascal(entity.table)}() -> Promise<row[]>"]
    if pk:
        out.append(
            f"get{_pascal(entity.name)}({_camel(pk.name)}) -> Promise<row | null>"
        )
    if writable:
        out.append(
            f"create{_pascal(entity.name)}({args}) -> Promise<new "
            f"{pk.name if pk else 'id'}>"
        )
    if pk and updatable:
        update_args = ", ".join(_camel(f.name) for f in updatable)
        out.append(f"update{_pascal(entity.name)}({_camel(pk.name)}, {update_args})")
        out.append(f"delete{_pascal(entity.name)}({_camel(pk.name)})")
    email = next((f for f in entity.fields if "email" in f.name.lower()), None)
    if email and (not pk or pk.name != email.name):
        out.append(
            f"get{_pascal(entity.name)}By{_pascal(email.name)}"
            f"({_camel(email.name)}) -> Promise<row | null>"
        )
    return out


def api_context(spec: ProjectSpec) -> str:
    """The EXACT functions `models.js` provides, for the generation prompt.

    `crud.api_context`'s rule, and its measured failure: without this the model
    invents an API against a file it can no longer see being written, and the
    app dies on the first request. The async note is not padding — a forgotten
    `await` renders `[object Promise]` on a page that otherwise looks right.
    """
    if not spec.entities:
        return ""
    lines: list[str] = []
    for entity in spec.entities:
        for signature in _signatures(entity):
            lines.append(f"- `models.{signature}`")
    return (
        "## The data layer is ALREADY WRITTEN — call it, do not redefine it\n"
        "`models.js` and the tables in `db.js` are generated from this project's "
        "schema. Use `const models = require('./models')` and call EXACTLY these "
        "functions. Do not invent other names, do not define classes (there are "
        "none — every helper resolves to plain row objects), and do not write "
        "SQL in `server.js`:\n"
        + "\n".join(lines)
        + "\n**Every helper is `async`.** Route handlers must be "
        "`async (req, res) => { ... }` and must `await` every call — a missing "
        "`await` renders `[object Promise]` instead of the data.\n"
        "`db.initDb()` already creates every table at startup."
    )


def models_source(spec: ProjectSpec) -> str:
    """The whole of `models.js`, derived from the spec's entities."""
    header = (
        f"/**\n * Database queries for {spec.name or 'this project'} — "
        "written by Coder from the project spec.\n"
        " *\n"
        " * One function per operation. Every value is bound as a $1/$2 parameter\n"
        " * and never concatenated into the SQL, so user input can never become\n"
        " * SQL. The column lists are printed from the same entity definition as\n"
        " * the tables in db.js, so the two cannot drift apart.\n"
        " */\n\n"
        '"use strict";\n\n'
        'const { getPool } = require("./db");\n'
    )
    needs_hash = any(
        _SECRET_NAME_RE.search(f.name) for e in spec.entities for f in e.fields
    )
    if needs_hash:
        header += 'const { hashPassword } = require("./passwords");\n'

    bodies = [entity_helpers(e) for e in spec.entities if e.fields]
    names: list[str] = []
    for entity in spec.entities:
        if entity.fields:
            names.extend(_exports(entity))
    if not bodies:
        return header + "\nmodule.exports = {};\n"
    exports = "module.exports = {\n" + "".join(f"  {n},\n" for n in names) + "};\n"
    return header + "\n" + "\n\n".join(bodies) + "\n\n" + exports


# ---------------------------------------------------------------------------
# passwords.js — the one place a secret is hashed
# ---------------------------------------------------------------------------


def needs_password_helper(spec: ProjectSpec) -> bool:
    return any(_SECRET_NAME_RE.search(f.name) for e in spec.entities for f in e.fields)


def password_helper_source() -> str:
    """`hashPassword` / `verifyPassword` on Node's own crypto — no dependency.

    `crud.py` leans on `werkzeug.security`, which ships with Flask. Node has no
    equivalent in the standard install, and adding `bcrypt` would mean a native
    build on a machine whose whole selling point is that it works offline. Node's
    built-in `scrypt` is a real password KDF and needs nothing installed.

    Written by us rather than by the model for the reason `plaintext_password_writes`
    exists: this is the one thing that must not be left to advice.
    """
    return (
        "/**\n"
        " * Password hashing — written by Coder, not generated.\n"
        " *\n"
        " * scrypt from Node's own crypto: a real password KDF, no dependency to\n"
        " * install, and it works offline. Never store a request password\n"
        " * directly; hash it here and compare with verifyPassword.\n"
        " */\n\n"
        '"use strict";\n\n'
        'const crypto = require("crypto");\n\n'
        "const KEYLEN = 64;\n\n"
        "/** Hash a plaintext password. Returns `salt:hash`, safe to store. */\n"
        "async function hashPassword(plain) {\n"
        '  const salt = crypto.randomBytes(16).toString("hex");\n'
        "  const derived = await new Promise((resolve, reject) => {\n"
        "    crypto.scrypt(String(plain), salt, KEYLEN, (err, key) =>\n"
        "      err ? reject(err) : resolve(key)\n"
        "    );\n"
        "  });\n"
        '  return `${salt}:${derived.toString("hex")}`;\n'
        "}\n\n"
        "/** Check a plaintext password against a stored `salt:hash`. */\n"
        "async function verifyPassword(plain, stored) {\n"
        '  const [salt, hash] = String(stored || "").split(":");\n'
        "  if (!salt || !hash) {\n"
        "    return false;\n"
        "  }\n"
        "  const derived = await new Promise((resolve, reject) => {\n"
        "    crypto.scrypt(String(plain), salt, KEYLEN, (err, key) =>\n"
        "      err ? reject(err) : resolve(key)\n"
        "    );\n"
        "  });\n"
        "  // Constant-time: a plain === comparison leaks the hash a byte at a\n"
        "  // time to anyone who can measure the response.\n"
        '  const expected = Buffer.from(hash, "hex");\n'
        "  return (\n"
        "    expected.length === derived.length &&\n"
        "    crypto.timingSafeEqual(expected, derived)\n"
        "  );\n"
        "}\n\n"
        "module.exports = { hashPassword, verifyPassword };\n"
    )


# ---------------------------------------------------------------------------
# seed.js
# ---------------------------------------------------------------------------


def seed_source(spec: ProjectSpec, rows: int = 3) -> str:
    """`seed.js` with a few demo rows per entity.

    An empty page in a demo reads as broken even when it is working perfectly,
    so every table starts with something in it. `ON CONFLICT DO NOTHING` is
    PostgreSQL's `INSERT OR IGNORE`: it keeps the script safe to run twice.
    """
    lines = [
        "/**",
        f" * Demo data for {spec.name or 'this project'} — "
        "written by Coder from the project spec.",
        " *",
        " * Run it with:  node seed.js",
        " *",
        " * `ON CONFLICT DO NOTHING` keeps it safe to run more than once.",
        " */",
        "",
        '"use strict";',
        "",
        'const db = require("./db");',
    ]
    if needs_password_helper(spec):
        lines.append('const { hashPassword } = require("./passwords");')
    lines += [
        "",
        "/** Insert demo rows. Safe to run repeatedly. */",
        "async function seed() {",
        "  const client = await db.getPool().connect();",
        "  try {",
    ]

    wrote_any = False
    for entity in spec.entities:
        writable = _writable(entity)
        if not writable:
            continue
        wrote_any = True
        cols = ", ".join(f.name for f in writable)
        marks = POSTGRES.placeholders(len(writable))
        lines.append(f"    // {entity.name}")
        for i in range(1, rows + 1):
            values = ", ".join(_sample(f, i) for f in writable)
            lines.append(
                "    await client.query(\n"
                f'      "INSERT INTO {entity.table} ({cols}) VALUES ({marks}) '
                'ON CONFLICT DO NOTHING",\n'
                f"      [{values}]\n"
                "    );"
            )
    if not wrote_any:
        lines.append("    // No entities with insertable columns.")

    lines += [
        "  } finally {",
        "    client.release();",
        "  }",
        "}",
        "",
        "if (require.main === module) {",
        "  db.initDb()",
        "    .then(seed)",
        "    .then(async () => {",
        '      console.log("Seeded the database.");',
        "      await db.close();",
        "    })",
        "    .catch(async (err) => {",
        "      // Reported, never swallowed: a seed that failed silently is a",
        "      // demo whose pages are empty for a reason nobody can see.",
        '      console.error("Seeding failed:", err.message);',
        "      await db.close();",
        "      process.exit(1);",
        "    });",
        "}",
        "",
        "module.exports = { seed };",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# db.js — the CREATE TABLE half
# ---------------------------------------------------------------------------

# `async function initDb(` — the function the scaffold ships and this fills in.
_INIT_DB_RE = re.compile(r"^\s*async function initDb\s*\([^)]*\)\s*\{", re.MULTILINE)
# Where the tables go: immediately before the `} finally {` that releases the
# client. Anchoring on the release rather than on a comment means a db.js the
# model has edited is still recognisable.
_FINALLY_RE = re.compile(r"^(\s*)\}\s*finally\s*\{", re.MULTILINE)


def table_block(spec: ProjectSpec) -> str:
    """`await client.query(...)` lines creating every table the spec declares."""
    out: list[str] = []
    for statement in spec.ddl(POSTGRES):
        indented = "\n".join("      " + line for line in statement.splitlines())
        out.append(f"    await client.query(`\n{indented}\n    `);")
    return "\n".join(out)


def migration_block(spec: ProjectSpec, since: int = 0) -> str:
    """`await ensureColumn(...)` lines for every field added after ``since``."""
    calls = spec.migrations(since=since, dialect=POSTGRES)
    return "\n".join(f"    {call};" for call in calls)


def apply_block(source: str, block: str) -> tuple[str, bool]:
    """Insert ``block`` into `initDb()`, before the `} finally {`.

    Idempotency is the CALLER's job (see `creates_table` / `adds_column`) —
    this only places text. Declines rather than guessing when `initDb()` is not
    recognisable: a half-edited schema file is worse than none, and the caller
    reports the decline instead of pretending it applied.
    """
    text = source or ""
    if not block.strip():
        return source, False
    init = _INIT_DB_RE.search(text)
    if not init:
        return source, False
    closing = _FINALLY_RE.search(text, init.end())
    if not closing:
        return source, False
    at = closing.start()
    return text[:at] + block + "\n" + text[at:], True


def _scan(source: str) -> tuple[list[str], str]:
    """One pass over JavaScript: ``(string literals, source minus comments)``.

    The `_creates_table` trap, ported. Scanning raw text made the *commented*
    `CREATE TABLE ... widgets` example in the Flask scaffold count as a real
    table, so on a live build the real one was never created and every route
    500'd with "no such table". `db.js` ships the same commented example, so the
    same trap was here waiting.

    `pyimports.searchable_sql` answers this with stdlib `ast`; Python has no JS
    parser, and regex cannot do it in two passes. Stripping comments FIRST
    truncates `"postgres://host/db"` at the `//`, leaving an unterminated quote
    that then pairs with an unrelated one further down the file and yields a
    "literal" spanning real code — measured on this project's own `db.js`.
    Finding literals first and stripping comments after has the mirror problem.

    So do both at once. A single left-to-right walk knows whether a `//` is a
    comment or three characters of a URL, because it knows whether it is inside
    a string — which is the whole distinction. Not a full JS lexer: a regex
    literal containing a quote (`/["]/`) would confuse it. That case does not
    appear in generated data-layer code, and the failure direction is to see
    FEWER literals — writing `CREATE TABLE IF NOT EXISTS` twice is a no-op,
    skipping one is a dead app.
    """
    text = source or ""
    literals: list[str] = []
    kept: list[str] = []  # everything but the comments, literals included
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            quote, start, i = ch, i + 1, i + 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
            literals.append(text[start:i])
            kept.append(text[start - 1 : min(i + 1, n)])
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            kept.append(ch)
            i += 1
    return literals, "".join(kept)


def js_strings(source: str) -> list[str]:
    """Every JavaScript string/template literal in ``source``. See `_scan`."""
    return _scan(source)[0]


def js_without_comments(source: str) -> str:
    """``source`` with every comment removed and every literal intact.

    What a check on a CALL needs (`ensureColumn(client, "x", "y", …)`): the call
    is code, its arguments are literals, and only the comments must go.
    """
    return _scan(source)[1]


def creates_table(source: str, table: str) -> bool:
    """Does this module already create ``table``? Guards idempotency.

    Reads string literals only — never raw text. See `js_strings`.
    """
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?"
        + re.escape(table)
        + r"\b",
        re.IGNORECASE,
    )
    return any(pattern.search(literal) for literal in js_strings(source or ""))


def adds_column(source: str, table: str, column: str) -> bool:
    """Is this column already added, either in the table or by an ensureColumn?

    Checked against the literals for the same reason as `creates_table`: the
    scaffold's commented `ensureColumn(client, "widgets", "colour", …)` example
    must not count.
    """
    text = source or ""
    call = re.compile(
        r"ensureColumn\s*\(\s*[A-Za-z_$][\w$]*\s*,\s*[\"'`]"
        + re.escape(table)
        + r"[\"'`]\s*,\s*[\"'`]"
        + re.escape(column)
        + r"[\"'`]"
    )
    # The call itself is code and its arguments are literals, so this reads the
    # source with only the COMMENTS removed — otherwise the scaffold's own
    # commented `ensureColumn(client, "widgets", …)` example counts as real.
    if call.search(js_without_comments(text)):
        return True
    # A column present in the CREATE TABLE needs no migration.
    create = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?"
        + re.escape(table)
        + r"\b(?P<cols>.*?)\)",
        re.IGNORECASE | re.DOTALL,
    )
    for literal in js_strings(text):
        match = create.search(literal)
        if match and re.search(r"\b" + re.escape(column) + r"\b", match.group("cols")):
            return True
    return False


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def has_uploads(spec: ProjectSpec) -> bool:
    return any(f.is_upload() for e in spec.entities for f in e.fields)


def upload_helper_source() -> str:
    """A `saveUpload()` for server.js: allowlisted, jailed, collision-safe.

    The mirror of `crud.upload_helper_source`, and the same allowlist reasoning:
    "is this dangerous?" has no reliable answer, "is this an image?" does.

    Reads the request with Node's own `multipart` handling absent, so this takes
    an already-parsed file object (`{ originalname, buffer }`) rather than
    pretending to parse the body itself — wiring the parser is the model's job
    and is stated in the prompt.
    """
    exts = ", ".join(f'"{e}"' for e in ALLOWED_UPLOAD_EXTENSIONS)
    return (
        "/**\n"
        " * Save an uploaded image and return the filename to store in the row.\n"
        " *\n"
        ' * Returns "" when nothing usable was sent, so a form posted without a\n'
        " * file still works instead of throwing.\n"
        " */\n\n"
        '"use strict";\n\n'
        'const fs = require("fs");\n'
        'const path = require("path");\n\n'
        "const ALLOWED_UPLOAD_EXTENSIONS = new Set([" + exts + "]);\n"
        'const UPLOAD_DIR = path.join(__dirname, "public", "uploads");\n\n'
        "function saveUpload(file) {\n"
        "  if (!file || !file.originalname || !file.buffer) {\n"
        '    return "";\n'
        "  }\n"
        "  // basename() strips any directory part, so `../../etc/passwd` cannot\n"
        "  // escape the upload directory.\n"
        "  const clean = path.basename(String(file.originalname));\n"
        '  if (!clean.includes(".")) {\n'
        '    return "";\n'
        "  }\n"
        '  const ext = clean.split(".").pop().toLowerCase();\n'
        "  if (!ALLOWED_UPLOAD_EXTENSIONS.has(ext)) {\n"
        '    return "";\n'
        "  }\n"
        "  fs.mkdirSync(UPLOAD_DIR, { recursive: true });\n"
        "  const stem = clean.slice(0, clean.length - ext.length - 1);\n"
        "  let name = clean;\n"
        "  let counter = 1;\n"
        "  while (fs.existsSync(path.join(UPLOAD_DIR, name))) {\n"
        "    name = `${stem}-${counter}.${ext}`;\n"
        "    counter += 1;\n"
        "  }\n"
        "  fs.writeFileSync(path.join(UPLOAD_DIR, name), file.buffer);\n"
        "  return name;\n"
        "}\n\n"
        "module.exports = { saveUpload, ALLOWED_UPLOAD_EXTENSIONS };\n"
    )


# ---------------------------------------------------------------------------
# The one part of auth that must never be a prompt instruction
# ---------------------------------------------------------------------------

_ASSIGN_SECRET_RE = re.compile(
    r"""(?P<col>\w*(?:password|passwd|secret|token)\w*)\s*[=:]\s*"""
    r"""req\.(?:body|query|params)(?:\.\w+|\[\s*["'][^"']+["']\s*\])""",
    re.IGNORECASE,
)
_HASH_CALL_RE = re.compile(
    r"(hashPassword|scrypt|bcrypt|pbkdf2|argon|createHash|timingSafeEqual)",
    re.IGNORECASE,
)


def plaintext_password_writes(source: str) -> list[str]:
    """Lines that put a raw request password somewhere it will be stored.

    `crud.plaintext_password_writes` for JavaScript, and a check on the CODE
    rather than a line in a prompt for the same reason: a prompt instruction is
    advice, and this is the one thing that must not be left to advice. Silent
    when the module hashes anywhere, so read-then-hash is correctly left alone.
    """
    text = source or ""
    if _HASH_CALL_RE.search(text):
        return []
    return [m.group(0) for m in _ASSIGN_SECRET_RE.finditer(text)]
