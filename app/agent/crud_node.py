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
    """Fields a caller supplies — everything the DATABASE does not fill in.

    Asked of the dialect rather than tested as `pk and type == "INTEGER"`, which
    is what it used to be. On PostgreSQL a TEXT primary key is generated too
    (`gen_random_uuid()`), and treating it as writable put the id first in every
    insert helper — so the route generated beside it read `const id = await
    models.createUser(id, …)` and threw a ReferenceError before it ever reached
    the database. Measured on the OpenBazaar PRD build, whose five tables all
    have UUID keys, i.e. every create form on the site.
    """
    return [f for f in entity.fields if not POSTGRES.generates_pk(f.type, f.pk)]


def _js_string(value: str) -> str:
    """``value`` as a double-quoted JavaScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _ref_table(field: Field) -> str:
    """The table ``field.references`` points at: `orders(id)` -> `orders`."""
    return field.references.split("(", 1)[0].strip()


def _sample(field: Field, index: int) -> str:
    """A plausible demo value, as a JavaScript literal.

    Seeded rows exist so no page is ever empty on first load — an empty list in
    a demo reads as broken even when it is correct. Mirrors `crud._sample` value
    for value, so the two stacks' demo data look like the same product.
    """
    name = field.name.lower()
    if field.check:
        # The column's own CHECK names the only values it may hold, so anything
        # else is not a "plausible" demo value — it is a row PostgreSQL refuses.
        # Measured on the OpenBazaar build: `condition_rating` was seeded as
        # "Demo condition_rating 1" and the FIRST insert took the whole seed
        # down with `violates check constraint`, so every page of a build whose
        # schema has any enumeration at all came up empty.
        values = list(field.check)
        return _js_string(values[(index - 1) % len(values)])
    if field.is_upload():
        return '""'  # no file on disk yet; the view falls back
    if field.type == "INTEGER":
        return str(index)
    if field.type in ("REAL", "NUMERIC"):
        # NUMERIC is NOT a fall-through to the string default. SQLite has type
        # AFFINITY, so `INSERT INTO users (cod_reliability_score) VALUES
        # ('Demo cod_reliability_score 1')` is accepted there and the bug is
        # invisible; PostgreSQL maps the column to NUMERIC and refuses outright
        # with `invalid input syntax for type numeric`, taking the whole seed
        # with it. Measured on the first real-PostgreSQL run of a generated
        # project (2026-08-04) — the exact class of defect that only a live
        # database can show, which is why that run was worth doing.
        return f"{9.99 + index:.2f}"
    if field.type == "BOOLEAN":
        # A real PostgreSQL boolean, so the string default would be rejected the
        # same way NUMERIC's was. Kept in step with `crud._sample`, which emits
        # `1` because SQLite spells this column INTEGER.
        return "true"
    if field.type == "TIMESTAMP":
        # TIMESTAMPTZ on PostgreSQL: a bare "2026-01-01" parses, but a full
        # ISO instant is what every comparison in a generated app comes to.
        return '"2026-01-01T00:00:00Z"'
    if field.type == "BLOB":
        return "null"  # BYTEA on PostgreSQL; a text literal is not a valid one
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


# Columns for which an empty string is not a value. An unfilled optional form
# field arrives as `""`, and PostgreSQL refuses it outright:
#
#     invalid input syntax for type integer: ""
#
# SQLite's type AFFINITY accepts it and stores the empty string in an INTEGER
# column, so the Flask stack has the same defect and merely hides it — the
# `_sample` lesson again, and `crud._bind` is its other half.
_NOT_BLANKABLE = frozenset({"TEXT", "BLOB"})


def _bind(field: Field) -> str:
    """The argument expression an INSERT/UPDATE binds for ``field``."""
    arg = _camel(field.name)
    return arg if field.type in _NOT_BLANKABLE else f"nullIfBlank({arg})"


def entity_helpers(entity: Entity) -> str:
    """`list / get / create / update / delete` for one entity, as async functions."""
    pk = _pk(entity)
    table, name = entity.table, entity.name
    writable = _writable(entity)
    cols = ", ".join(f.name for f in writable)
    marks = POSTGRES.placeholders(len(writable))
    args = ", ".join(_camel(f.name) for f in writable)
    binds = ", ".join(_bind(f) for f in writable)

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
            f"    [{binds}]\n"
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
        update_binds = ", ".join(_bind(f) for f in updatable)
        pk_arg = _camel(pk.name)
        parts.append(
            f"/** Overwrite one {name}. */\n"
            f"async function update{_pascal(name)}({pk_arg}, {update_args}) {{\n"
            f"  await getPool().query(\n"
            f'    "UPDATE {table} SET {assignments} WHERE {pk.name} = {key_mark}",\n'
            f"    [{update_binds}, {pk_arg}]\n"
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
    header += (
        "\n/**\n"
        ' * An unfilled optional form field arrives as `""`, and PostgreSQL\n'
        " * refuses it for anything that is not text:\n"
        " *\n"
        ' *     invalid input syntax for type integer: ""\n'
        " *\n"
        " * The column means `NULL` there, so say so. Applied to every non-text\n"
        " * bind below, never to a TEXT one — an empty string IS a valid value\n"
        " * for a text column, and turning it into NULL would lose it.\n"
        " */\n"
        "function nullIfBlank(value) {\n"
        '  return value === "" || value === undefined ? null : value;\n'
        "}\n"
    )

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


def restore_model_api(source: str, spec: ProjectSpec) -> tuple[str, list[str]]:
    """Put back generated query helpers that an edit to `models.js` dropped.

    `models.js` is GENERATED — its column lists are printed from the same entity
    definitions as the tables in `db.js` — but unlike `db.js` it is still handed
    to the model, because a later turn legitimately adds a query no schema
    implies. Turn 2 of the OpenBazaar build is what that costs: asked to add one
    column to the users queries, the model rewrote the file a third shorter, and
    every page of the site answered `TypeError: models.listUsers is not a
    function`.

    Additive and deterministic. A helper the file still defines is left exactly
    as it is, improvements included; only a name the spec says must exist and
    the file no longer defines is re-emitted, with `module.exports` extended to
    match. It cannot revert an edit — it can only refill a hole.
    """
    text = source or ""
    if not text.strip():
        return text, []

    restored: list[str] = []
    for entity in spec.entities:
        if not entity.fields:
            continue
        wanted = _exports(entity)
        if all(_defines(text, name) for name in wanted):
            continue
        # The helpers are written per entity as ONE block, so an entity missing
        # any of them gets that whole block back. Re-emitting half of it would
        # leave two definitions of the survivors, and in JavaScript the later
        # one silently wins — `duplicate_definitions`' whole complaint.
        for name in wanted:
            text = _drop_function(text, name)
        text = _insert_before_exports(text, entity_helpers(entity))
        restored += wanted

    # …and whatever the file defines but forgot to export. A helper that is
    # written and not exported is `models.listAuctions is not a function` — the
    # same 500 as one that was deleted, from the opposite cause, and the turn
    # that added it reports success either way. Measured: the two helpers a
    # repair turn was asked for landed in the file and never reached
    # `module.exports`.
    unexported = [
        name
        for name in re.findall(r"function\s+([A-Za-z_$][\w$]*)", text)
        if _QUERY_NAME_RE.match(name) and not _is_exported(text, name)
    ]
    # …and anything the export block NAMES that the file does not define. That
    # one is not a 500 on a page — `module.exports` is evaluated when the module
    # loads, so `images is not defined` there takes the entire app down before
    # it listens. Measured on turn 5 of the OpenBazaar build.
    phantom = [
        name for name in _exported_names(text) if not _defines(text, name)
    ]
    if not restored and not unexported and not phantom:
        return source or "", []
    return _rebuild_exports(text, spec), sorted(set(restored + unexported + phantom))


# `listItems`, `getUserByEmail`, `createOrder` — the shape a query helper has.
# Deliberately not "everything the file defines": `nullIfBlank` is a private
# detail and exporting it would be noise, not a repair.
_QUERY_NAME_RE = re.compile(r"^(list|get|create|update|delete|find|count|search)[A-Z]")


def _exported_names(text: str) -> list[str]:
    """Every identifier `module.exports = { … }` lists."""
    match = _EXPORTS_RE.search(text)
    if not match:
        return []
    end = text.find("}", match.end())
    body = text[match.end() : end] if end != -1 else ""
    return _IDENT_IN_EXPORTS_RE.findall(body)


def _is_exported(text: str, name: str) -> bool:
    return name in _exported_names(text)


_EXPORTS_RE = re.compile(r"^module\.exports\s*=\s*\{", re.MULTILINE)
_IDENT_IN_EXPORTS_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _defines(text: str, name: str) -> bool:
    return re.search(r"function\s+" + re.escape(name) + r"\s*\(", text) is not None


def _drop_function(text: str, name: str) -> str:
    """Remove `function name(…) { … }` and the doc comment above it."""
    match = re.search(
        r"(?:^/\*\*(?:(?!\*/).)*\*/\s*)?^(?:async\s+)?function\s+"
        + re.escape(name)
        + r"\s*\(",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return text
    brace = text.find("{", match.end())
    if brace == -1:
        return text
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[: match.start()].rstrip() + "\n\n" + text[index + 1 :].lstrip()
    return text


def _insert_before_exports(text: str, block: str) -> str:
    """Put ``block`` above `module.exports`, or at the end of the file."""
    match = _EXPORTS_RE.search(text)
    at = match.start() if match else len(text)
    return text[:at].rstrip() + "\n\n" + block.strip() + "\n\n" + text[at:]


def _rebuild_exports(text: str, spec: ProjectSpec) -> str:
    """Rewrite `module.exports` so it names every helper the file defines."""
    names: list[str] = []
    for entity in spec.entities:
        if entity.fields:
            names.extend(n for n in _exports(entity) if _defines(text, n))
    match = _EXPORTS_RE.search(text)
    if match:
        end = text.find("}", match.end())
        existing = text[match.end() : end] if end != -1 else ""
        # A name the model added and exported stays exported — this pass refills
        # holes, it does not take anything away.
        for extra in _IDENT_IN_EXPORTS_RE.findall(existing):
            if extra not in names and _defines(text, extra):
                names.append(extra)
        for extra in re.findall(r"function\s+([A-Za-z_$][\w$]*)", text):
            if extra not in names and _QUERY_NAME_RE.match(extra):
                names.append(extra)
        block = "module.exports = {\n" + "".join(f"  {n},\n" for n in names) + "};\n"
        tail = text[end + 1 :] if end != -1 else ""
        return text[: match.start()] + block + tail.lstrip()
    return (
        text.rstrip()
        + "\n\nmodule.exports = {\n"
        + "".join(f"  {n},\n" for n in names)
        + "};\n"
    )


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
        "/**",
        " * The id of a parent row for a foreign key to point at, or null when",
        " * the parent table was never seeded. It cycles, so a handful of child",
        " * rows spread over whatever parents exist.",
        " */",
        "function pickId(ids, index) {",
        "  return ids.length > 0 ? ids[(index - 1) % ids.length] : null;",
        "}",
        "",
        "/** Insert demo rows. Safe to run repeatedly. */",
        "async function seed() {",
        "  const client = await db.getPool().connect();",
        "  try {",
    ]

    wrote_any = False
    # table -> the JS array holding the ids this script really inserted. A
    # foreign key has to point at a row that EXISTS, and the only ids that
    # exist are the ones the parent insert returned — `gen_random_uuid()`
    # mints them, so they cannot be known here. Measured on the OpenBazaar
    # build, where every child row was seeded with the string
    # "Demo seller_id 1" and the first `items` insert took the whole seed down.
    ids_var: dict[str, str] = {}
    for entity in spec.entities:
        writable = _writable(entity)
        if not writable:
            continue
        pk = _pk(entity)
        # A REQUIRED parent whose rows this script never inserts. Seeding the
        # child anyway is a guaranteed foreign-key violation, and one failed
        # insert aborts every later one — so the entity is skipped WITH ITS
        # REASON in the file rather than emitted to fail at runtime.
        blocked = sorted(
            {
                _ref_table(f)
                for f in writable
                if f.references
                and f.required
                and _ref_table(f) != entity.table
                and _ref_table(f) not in ids_var
            }
        )
        if blocked:
            lines.append(
                f"    // {entity.name}: not seeded - it requires a row in "
                f"{', '.join(blocked)}, which this script does not insert."
            )
            continue
        wrote_any = True
        cols = ", ".join(f.name for f in writable)
        marks = POSTGRES.placeholders(len(writable))
        var = f"{_camel(entity.table)}Ids"
        lines.append(f"    // {entity.name}")
        if pk is not None:
            lines.append(f"    const {var} = [];")
        for i in range(1, rows + 1):
            values = ", ".join(
                (
                    f"pickId({ids_var[_ref_table(f)]}, {i})"
                    if f.references and _ref_table(f) in ids_var
                    # A self-reference (`categories.parent_id`) and a forward one
                    # both resolve to nothing yet. NULL is the only value that is
                    # certainly valid, and every such column is nullable — a
                    # required one was skipped above.
                    else "null" if f.references else _sample(f, i)
                )
                for f in writable
            )
            insert = (
                f'"INSERT INTO {entity.table} ({cols}) VALUES ({marks}) '
                "ON CONFLICT DO NOTHING"
                + (f" RETURNING {pk.name}" if pk is not None else "")
                + '"'
            )
            if pk is None:
                lines.append(
                    "    await client.query(\n"
                    f"      {insert},\n"
                    f"      [{values}]\n"
                    "    );"
                )
                continue
            lines.append(
                "    {\n"
                "      const inserted = await client.query(\n"
                f"        {insert},\n"
                f"        [{values}]\n"
                "      );\n"
                f"      if (inserted.rows[0]) {var}.push(inserted.rows[0].{pk.name});\n"
                "    }"
            )
        if pk is not None:
            # `ON CONFLICT DO NOTHING` returns NO row for one that is already
            # there, so on the second run of a seed the array is empty and every
            # child insert binds null into a NOT NULL foreign key. The rows do
            # exist — they just were not inserted by THIS run — so read them
            # back. This is what keeps the script safe to run twice, which is
            # the whole point of `ON CONFLICT DO NOTHING`.
            lines.append(
                f"    if ({var}.length === 0) {{\n"
                f"      const existing = await client.query(\n"
                f'        "SELECT {pk.name} FROM {entity.table} LIMIT {rows}"\n'
                "      );\n"
                f"      for (const row of existing.rows) {var}.push(row.{pk.name});\n"
                "    }"
            )
            ids_var[entity.table] = var
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
