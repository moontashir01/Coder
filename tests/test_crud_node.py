"""The Node data layer: same entities, PostgreSQL spelling (Phase N3).

`docs/node-stack-plan.md`. `tests/test_crud.py` proves the Flask data layer by
executing the generated SQL against a real in-memory sqlite3; the equivalent
here needs a live PostgreSQL, so this file has three tiers and each says which
it is:

  * **Pure generation** — always runs, no server, no node. The bulk of it.
  * **`node --check`** — gated on node being on PATH. Proves the emitted
    JavaScript parses, which no string assertion can.
  * **Real PostgreSQL** — gated on `psycopg` AND a reachable server, and
    **skipped loudly** rather than silently passing. This is the tier that
    would catch a type PostgreSQL rejects or an `ON CONFLICT` it will not take.

The property worth protecting above all: `crud.py` and `crud_node.py` emit from
the SAME `Entity` objects, so the two stacks cannot end up describing different
schemas from one spec.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
import textwrap

import pytest

from app.agent import crud, crud_node
from app.agent.projectspec import POSTGRES, SQLITE, Entity, Field, ProjectSpec

PRODUCT = Entity(
    name="product",
    table="products",
    fields=(
        Field(name="id", type="INTEGER", pk=True),
        Field(name="title", type="TEXT", required=True),
        Field(name="price", type="REAL"),
        Field(name="cover_path", type="TEXT"),
    ),
)
USER = Entity(
    name="user",
    table="users",
    fields=(
        Field(name="id", type="INTEGER", pk=True),
        Field(name="email", type="TEXT", required=True),
        Field(name="password_hash", type="TEXT"),
    ),
)
# A TEXT primary key: the shape that made `update_user(email, email, …)` a
# SyntaxError on the Flask side, and would be a duplicate parameter here.
SETTING = Entity(
    name="setting",
    table="settings",
    fields=(
        Field(name="key", type="TEXT", pk=True),
        Field(name="value", type="TEXT"),
    ),
)


def _spec(*entities) -> ProjectSpec:
    return ProjectSpec(
        name="Widget Shop", entities=entities or (PRODUCT, USER, SETTING)
    )


# ---------------------------------------------------------------------------
# The dialect: one entity model, two spellings
# ---------------------------------------------------------------------------


def test_sqlite_ddl_is_unchanged_by_the_dialect_split():
    """Phase N3 added dialects; the Flask output must be byte-identical, since
    every existing caller relies on the default."""
    ddl = _spec(PRODUCT).ddl()[0]
    assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in ddl
    assert "price REAL" in ddl
    assert _spec(PRODUCT).ddl(SQLITE)[0] == ddl


def test_postgres_ddl_uses_serial_and_numeric():
    ddl = _spec(PRODUCT).ddl(POSTGRES)[0]
    assert "id SERIAL PRIMARY KEY" in ddl
    assert "AUTOINCREMENT" not in ddl  # not a PostgreSQL keyword
    # REAL exists in PostgreSQL but is a 4-byte float, which is wrong for the
    # price REAL overwhelmingly means in a generated schema.
    assert "price NUMERIC" in ddl
    assert "title TEXT NOT NULL" in ddl


def test_a_text_primary_key_gets_no_serial():
    ddl = _spec(SETTING).ddl(POSTGRES)[0]
    assert "key TEXT PRIMARY KEY" in ddl


def test_the_two_dialects_describe_the_same_tables_and_columns():
    """The whole point of one entity model. Names must match exactly — a column
    that exists on one stack and not the other is a spec that lies."""
    for entity in (PRODUCT, USER, SETTING):
        lite = _spec(entity).ddl(SQLITE)[0]
        pg = _spec(entity).ddl(POSTGRES)[0]
        for f in entity.fields:
            assert f"{f.name} " in lite and f"{f.name} " in pg
        assert lite.splitlines()[0] == pg.splitlines()[0]  # same CREATE TABLE line


def test_migrations_speak_each_stack_s_own_primitive():
    spec = ProjectSpec(
        name="x",
        revision=2,
        entities=(
            Entity(
                name="product",
                table="products",
                fields=PRODUCT.fields
                + (Field(name="colour", type="TEXT", added_in=2),),
            ),
        ),
    )
    assert spec.migrations(since=1) == [
        'ensure_column(conn, "products", "colour", "TEXT")'
    ]
    assert spec.migrations(since=1, dialect=POSTGRES) == [
        'await ensureColumn(client, "products", "colour", "TEXT")'
    ]


def test_a_migration_never_re_adds_a_primary_key():
    """A PK arrives with the table; ALTERing one in is a different operation."""
    spec = ProjectSpec(
        entities=(
            Entity(
                name="x",
                table="xs",
                fields=(Field(name="id", type="INTEGER", pk=True, added_in=3),),
            ),
        )
    )
    assert spec.migrations(since=1, dialect=POSTGRES) == []


# ---------------------------------------------------------------------------
# models.js
# ---------------------------------------------------------------------------


def test_every_value_is_a_bound_parameter():
    """SQL injection impossible BY CONSTRUCTION, not by inspection: no query may
    build its text from a value."""
    source = crud_node.models_source(_spec())
    for line in source.splitlines():
        if "query(" in line or "FROM" in line or "INSERT" in line:
            assert "${" not in line, line  # no template interpolation
            assert not (" + " in line and '"' in line and "SELECT" in line), line
    assert "$1" in source


def test_placeholders_are_numbered_from_one_with_no_gaps():
    import re

    source = crud_node.models_source(_spec())
    for statement in re.findall(r'"((?:SELECT|INSERT|UPDATE|DELETE)[^"]*)"', source):
        marks = sorted({int(m) for m in re.findall(r"\$(\d+)", statement)})
        assert marks == list(range(1, len(marks) + 1)), statement


def test_every_query_binds_exactly_as_many_values_as_it_marks():
    """The classic `pg` bug: `$1, $2, $3` with two values in the array. It does
    not raise at import, it raises on the request — so nothing that reads the
    file catches it, and the page 500s the first time anyone opens it."""
    import re

    source = crud_node.models_source(_spec())
    calls = re.findall(
        r'query\(\s*\n\s*"(?P<sql>[^"]+)",\s*\n\s*\[(?P<args>[^\]]*)\]', source
    )
    assert calls, "no parameterised queries found — the regex or the emitter moved"
    for sql, args in calls:
        marks = {int(m) for m in re.findall(r"\$(\d+)", sql)}
        values = [a.strip() for a in args.split(",") if a.strip()]
        assert len(marks) == len(values), (sql, args)


def test_an_insert_asks_for_the_id_it_creates():
    """PostgreSQL has no `lastrowid`. Without RETURNING, a handler that redirects
    to the new row has nothing to redirect to."""
    source = crud_node.models_source(_spec(PRODUCT))
    assert (
        "INSERT INTO products (title, price, cover_path) VALUES ($1, $2, $3) " in source
    )
    assert "RETURNING id" in source
    assert "return rows[0].id;" in source


def test_an_update_never_takes_the_key_twice():
    """A TEXT primary key is also a writable column; naming it in both the SET
    list and the parameter list is a duplicate-parameter error."""
    source = crud_node.models_source(_spec(SETTING))
    assert "async function updateSetting(key, value)" in source
    assert "UPDATE settings SET value = $1 WHERE key = $2" in source


def test_helpers_are_async_and_exported():
    source = crud_node.models_source(_spec())
    for name in ("listProducts", "createProduct", "getUserByEmail", "deleteSetting"):
        assert f"async function {name}(" in source
        assert f"  {name},\n" in source, f"{name} is defined but never exported"


def test_names_are_camelcase_not_snake_case():
    """A 7B writing a call site reaches for camelCase by habit, so a snake_case
    export is a runtime failure on a page that was otherwise correct."""
    source = crud_node.models_source(_spec(USER))
    assert "getUserByEmail" in source
    assert "get_user_by_email" not in source


def test_api_context_names_exactly_what_models_js_exports():
    """`crud.api_context`'s rule: taking the data layer away from the model is
    only safe if the model is told what replaced it. A block that drifts names a
    function that does not exist, and the request dies on it."""
    spec = _spec()
    source = crud_node.models_source(spec)
    block = crud_node.api_context(spec)
    exported = [n for e in spec.entities for n in crud_node._exports(e)]
    for name in exported:
        assert f"models.{name}(" in block, f"{name} is exported but not advertised"
        assert f"async function {name}(" in source
    # ...and nothing advertised that isn't real.
    import re

    for advertised in re.findall(r"`models\.(\w+)\(", block):
        assert advertised in exported, f"{advertised} is advertised but not exported"


def test_api_context_states_the_await_rule():
    """A forgotten `await` renders `[object Promise]` on a page that otherwise
    looks right — invisible to every check that reads bytes."""
    block = crud_node.api_context(_spec())
    assert "async" in block and "await" in block


def test_api_context_is_empty_without_entities():
    assert crud_node.api_context(ProjectSpec(name="x")) == ""


def test_both_stacks_expose_the_same_operations():
    """Two spellings of one data layer. An operation on one stack and not the
    other means a page that works on Flask and 404s on Node."""
    for entity in (PRODUCT, USER, SETTING):
        spec = _spec(entity)
        py = {s.split("(")[0] for s in crud._signatures(entity)}
        js = {s.split("(")[0] for s in crud_node._signatures(entity)}
        assert len(py) == len(js), (entity.table, py, js)
        assert spec is not None


# ---------------------------------------------------------------------------
# seed.js
# ---------------------------------------------------------------------------


def test_seed_is_safe_to_run_twice():
    """Every INSERT, not just most of them — one bare insert is a duplicate row
    per run, and the demo's product list grows every time anyone reseeds."""
    source = crud_node.seed_source(_spec(PRODUCT))
    inserts = [ln for ln in source.splitlines() if "INSERT INTO" in ln]
    assert len(inserts) == 3
    assert all("ON CONFLICT DO NOTHING" in ln for ln in inserts)


def test_seed_never_stores_a_plaintext_password():
    """`crud._sample`'s rule. A demo password in the clear is still a password
    in the clear, and it is the row every reader copies."""
    source = crud_node.seed_source(_spec(USER))
    assert 'await hashPassword("demo-password")' in source
    assert '"demo-password"]' not in source
    assert crud_node.plaintext_password_writes(source) == []


def test_seed_reports_failure_rather_than_exiting_clean():
    source = crud_node.seed_source(_spec())
    assert "process.exit(1)" in source
    assert "Seeding failed" in source


def test_seed_handles_a_spec_with_nothing_to_insert():
    source = crud_node.seed_source(ProjectSpec(name="x"))
    assert "async function seed()" in source
    assert "INSERT" not in source


# ---------------------------------------------------------------------------
# db.js — placement and idempotency
# ---------------------------------------------------------------------------

_DB_JS = textwrap.dedent("""\
    "use strict";
    const { Pool } = require("pg");
    let pool = null;
    function getPool() { return pool; }
    async function ensureColumn(client, table, column, decl) {}
    async function initDb() {
      const client = await getPool().connect();
      try {
        // Shape:
        //     await client.query(`CREATE TABLE IF NOT EXISTS widgets (id SERIAL)`);
        //     await ensureColumn(client, "widgets", "colour", "TEXT");
      } finally {
        client.release();
      }
    }
    module.exports = { getPool, initDb, ensureColumn };
    """)


def test_the_commented_example_does_not_count_as_a_real_table():
    """The `_creates_table` trap, one stack over. Scanning raw text made the
    Flask scaffold's *commented* `CREATE TABLE ... products` count as real, so
    the real table was never created and every route 500'd."""
    assert crud_node.creates_table(_DB_JS, "widgets") is False
    assert crud_node.adds_column(_DB_JS, "widgets", "colour") is False


def test_a_url_literal_is_not_shredded_by_comment_stripping():
    """`"postgres://host/db"` is three characters of a URL, not a comment.

    Stripping comments before finding literals truncates it at the `//`, leaving
    an unterminated quote that pairs with an unrelated one further down and
    yields a "literal" spanning real code — measured on this project's own
    db.js. Finding literals first has the mirror problem. `_scan` does both in
    one walk, which is the only order that can tell the two apart.
    """
    source = (
        'const DATABASE_URL = "postgres://postgres:postgres@localhost:5432/shop";\n'
        "// a comment mentioning CREATE TABLE IF NOT EXISTS widgets\n"
        'const q = "CREATE TABLE IF NOT EXISTS products (id SERIAL)";\n'
    )
    literals = crud_node.js_strings(source)
    assert "postgres://postgres:postgres@localhost:5432/shop" in literals
    assert crud_node.creates_table(source, "widgets") is False
    assert crud_node.creates_table(source, "products") is True


def test_the_shipped_db_js_reads_correctly(tmp_path):
    """The file this actually runs against, not a sketch of it.

    Scaffolded rather than read from the template tree: the connection string is
    a `{{DATABASE_URL}}` placeholder there, filled from
    `settings.postgres_server` at copy time, so the template holds no URL to
    find. What matters is that the file a project RECEIVES parses this way.
    """
    from app.agent.stacks.node_adapter import NODE

    NODE.scaffold(tmp_path, "shop")
    source = (tmp_path / "db.js").read_text(encoding="utf-8")
    assert any("postgres://" in lit for lit in crud_node.js_strings(source))
    assert "{{" not in source  # every placeholder was substituted
    assert crud_node.creates_table(source, "widgets") is False
    assert crud_node.adds_column(source, "widgets", "colour") is False


def test_a_real_create_table_is_found():
    source = _DB_JS.replace(
        "  } finally {",
        "        await client.query(`CREATE TABLE IF NOT EXISTS products (id SERIAL)`);\n"
        "  } finally {",
    )
    assert crud_node.creates_table(source, "products") is True
    assert crud_node.creates_table(source, "orders") is False


def test_a_column_already_in_the_table_needs_no_migration():
    source = _DB_JS.replace(
        "  } finally {",
        "        await client.query(`CREATE TABLE IF NOT EXISTS products "
        "(id SERIAL, colour TEXT)`);\n  } finally {",
    )
    assert crud_node.adds_column(source, "products", "colour") is True
    assert crud_node.adds_column(source, "products", "size") is False


def test_a_real_ensure_column_call_is_found():
    source = _DB_JS.replace(
        "  } finally {",
        '        await ensureColumn(client, "products", "colour", "TEXT");\n'
        "  } finally {",
    )
    assert crud_node.adds_column(source, "products", "colour") is True


def test_the_table_block_lands_inside_init_db():
    updated, changed = crud_node.apply_block(
        _DB_JS, crud_node.table_block(_spec(PRODUCT))
    )
    assert changed
    assert updated.index("CREATE TABLE IF NOT EXISTS products") < updated.index(
        "} finally {"
    )
    assert updated.index("async function initDb") < updated.index(
        "CREATE TABLE IF NOT EXISTS products"
    )


def test_it_declines_rather_than_guessing_on_an_unrecognisable_db_js():
    """A half-edited schema file is worse than none."""
    assert crud_node.apply_block("const x = 1;\n", "  // block") == (
        "const x = 1;\n",
        False,
    )
    assert crud_node.apply_block(_DB_JS, "") == (_DB_JS, False)


# ---------------------------------------------------------------------------
# Uploads and secrets
# ---------------------------------------------------------------------------


def test_upload_helper_allowlists_and_jails():
    source = crud_node.upload_helper_source()
    assert "ALLOWED_UPLOAD_EXTENSIONS" in source
    # basename() is what stops `../../etc/passwd` escaping the upload dir.
    assert "path.basename(" in source
    assert '"png"' in source and '"exe"' not in source


def test_plaintext_password_writes_flags_the_raw_request_field():
    assert crud_node.plaintext_password_writes(
        "const password = req.body.password;\nawait models.createUser(email, password);"
    )
    # ...and stays silent when the module hashes anywhere.
    assert (
        crud_node.plaintext_password_writes(
            "const password = req.body.password;\n"
            "const hash = await hashPassword(password);"
        )
        == []
    )


def test_password_helper_is_constant_time_and_salted():
    source = crud_node.password_helper_source()
    assert "scrypt" in source
    assert "randomBytes" in source  # a per-password salt
    # A plain === leaks the hash a byte at a time to anyone timing the response.
    assert "timingSafeEqual" in source


# ---------------------------------------------------------------------------
# Tier 2: it actually parses (needs node on PATH)
# ---------------------------------------------------------------------------

node_bin = shutil.which("node")


@pytest.mark.skipif(node_bin is None, reason="node is not on PATH")
@pytest.mark.parametrize(
    "name,render",
    [
        ("models.js", lambda s: crud_node.models_source(s)),
        ("seed.js", lambda s: crud_node.seed_source(s)),
        ("passwords.js", lambda s: crud_node.password_helper_source()),
        ("upload.js", lambda s: crud_node.upload_helper_source()),
    ],
)
def test_generated_javascript_parses(name, render, tmp_path):
    """No string assertion can prove this. A generated file that does not parse
    takes the whole app down at require() time."""
    path = tmp_path / name
    path.write_text(render(_spec()), encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [node_bin, "--check", str(path)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Tier 3: it runs against a real PostgreSQL
# ---------------------------------------------------------------------------
# Mirrors `test_crud.py`'s use of real in-memory sqlite3. Gated on BOTH a driver
# and a reachable server, and skipped LOUDLY — a suite that silently passes
# without ever touching a database has verified nothing about the SQL.


def _pg_connection():
    psycopg = pytest.importorskip("psycopg", reason="psycopg is not installed")
    dsn = os.environ.get("CODER_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "set CODER_TEST_DATABASE_URL to run the generated SQL against a real "
            "PostgreSQL (this suite otherwise never executes it)"
        )
    try:
        return psycopg.connect(dsn, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL at CODER_TEST_DATABASE_URL is unreachable: {exc}")


def test_generated_ddl_executes_on_real_postgres():
    conn = _pg_connection()
    try:
        with conn.cursor() as cur:
            for statement in _spec().ddl(POSTGRES):
                cur.execute(statement)
            # And the migration primitive PostgreSQL actually has.
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS colour TEXT")
            cur.execute(
                "INSERT INTO products (title, price, cover_path) "
                "VALUES (%s, %s, %s) RETURNING id",
                ("Demo", 9.99, ""),
            )
            assert cur.fetchone()[0] >= 1
        conn.rollback()
    finally:
        conn.close()


def test_a_missing_database_really_reports_sqlstate_3d000():
    """Phase N5 keys its clearest message off this exact code.

    The offline readiness tests drive a FAKE `pg` that raises `3D000`, so they
    prove how the adapter HANDLES the code — not that PostgreSQL really emits it
    when the database is absent. That assumption is the one thing only a live
    server can settle, and getting it wrong would leave the "create it with
    `createdb x`" message permanently unreachable while every test passed.

    Asked through **node-pg**, because that is the library the production probe
    uses (`NodeAdapter.database_reason` runs `node -e` against the project's own
    `pg` and reads `err.code`). The first version of this asked psycopg instead
    and failed the first time it was ever run: psycopg 3.3.4 populates neither
    `.sqlstate` nor `.diag.sqlstate` for a failure raised while CONNECTING, so
    the assertion could never have held — the proxy was wrong, not the product.
    Measured 2026-08-04 against PostgreSQL 18.4: node-pg reports
    `code=3D000`.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    dsn = os.environ.get("CODER_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "set CODER_TEST_DATABASE_URL to confirm the SQLSTATE Phase N5 keys "
            "its 'database does not exist' message off"
        )
    project = os.environ.get("CODER_TEST_PG_PROJECT_DIR")
    if not project or not (Path(project) / "node_modules" / "pg").is_dir():
        pytest.skip(
            "set CODER_TEST_PG_PROJECT_DIR to a project whose node_modules has "
            "`pg` — the production probe reads node-pg's err.code, and psycopg "
            "cannot answer this question (it exposes no sqlstate on connect)"
        )
    absent = re.sub(
        r"/[^/?]*(\?|$)", "/coder_definitely_not_a_database\1", dsn, count=1
    )
    script = (
        "const { Client } = require('pg');"
        f"const c = new Client({{ connectionString: {absent!r} }});"
        "c.connect().then(() => { console.log('CONNECTED'); process.exit(0); })"
        ".catch(e => { console.log(String(e.code)); process.exit(0); });"
    ).replace("'", '"', 0)
    result = subprocess.run(
        ["node", "-e", script],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = (result.stdout or "").strip()
    if output == "CONNECTED":  # pragma: no cover - only if it really exists
        pytest.skip("coder_definitely_not_a_database exists on this server")
    assert output == "3D000", (
        f"expected invalid_catalog_name from node-pg, got {output!r}; "
        f"stderr: {result.stderr}"
    )


def test_psycopg_cannot_answer_the_sqlstate_question(monkeypatch):
    """Pins WHY the test above goes through node — so nobody 'simplifies' it
    back to psycopg and gets a failure that looks like a product defect."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg is not installed")
    dsn = os.environ.get("CODER_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set CODER_TEST_DATABASE_URL")
    absent = re.sub(
        r"/[^/?]*(\?|$)", "/coder_definitely_not_a_database\1", dsn, count=1
    )
    try:
        psycopg.connect(absent, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        assert getattr(exc, "sqlstate", None) is None
        # It still NAMES the database, which is all this driver can prove here.
        assert "coder_definitely_not_a_database" in str(exc)
    else:  # pragma: no cover
        pytest.skip("coder_definitely_not_a_database exists on this server")


# ---------------------------------------------------------------------------
# Defects found by the first real-PostgreSQL run of a generated project
# (2026-08-04). Both were invisible on SQLite.
# ---------------------------------------------------------------------------


def test_numeric_and_blob_samples_are_not_strings():
    """`invalid input syntax for type numeric: "Demo cod_reliability_score 1"`.

    SQLite has type AFFINITY and accepts a string into a NUMERIC column, so the
    seed looked fine on Flask for as long as nobody ran the Node one against a
    real database. PostgreSQL refuses outright and takes the whole seed with it.
    """
    from app.agent.crud import _sample as py_sample
    from app.agent.crud_node import _sample as js_sample
    from app.agent.projectspec import Field

    numeric = Field(name="cod_reliability_score", type="NUMERIC")
    assert float(js_sample(numeric, 1)) > 0
    assert float(py_sample(numeric, 1)) > 0

    blob = Field(name="payload", type="BLOB")
    assert js_sample(blob, 1) == "null"  # BYTEA; a text literal is not one
    assert py_sample(blob, 1) == "None"


def test_scaffold_server_requires_models():
    """`{"error":"models is not defined"}` on every data page of a live build.

    The scaffold ships `models.js`, its own header says "every query lives in
    models.js", and generation writes routes that call `models.x()` — but
    `server.js` never required it. Flask survives the same omission only because
    `_repair_missing_imports` adds it, and that pass is Python-only; Node's lack
    of import repair is a stated gap in `NodeAdapter.gaps`, so the scaffold has
    to be right on its own.
    """
    from pathlib import Path

    from config.settings import settings

    scaffold = Path(settings.scaffolds_dir) / "node"
    server = (scaffold / "server.js").read_text(encoding="utf-8")
    assert 'require("./models")' in server
    assert (scaffold / "models.js").is_file()  # ...and the require can't throw


# ---------------------------------------------------------------------------
# An empty optional form field is NULL, not ""
# ---------------------------------------------------------------------------


def test_a_non_text_bind_is_nulled_when_the_field_is_left_blank():
    """Measured against a live PostgreSQL: posting the create form with an
    empty optional number field returned `invalid input syntax for type
    integer: ""` and, because Express 4 does not catch a rejected async
    handler, took the whole server down with it."""
    entity = Entity(
        name="category",
        table="categories",
        fields=(
            Field("id", "INTEGER", pk=True),
            Field("parent_id", "INTEGER"),
            Field("name", "TEXT", required=True),
        ),
    )
    source = crud_node.entity_helpers(entity)

    assert "[nullIfBlank(parentId), name]" in source
    # A TEXT column keeps "" — it is a valid value there, and nulling it loses
    # the difference between "left blank" and "cleared".
    assert "nullIfBlank(name)" not in source


def test_models_js_defines_the_helper_it_uses(tmp_path):
    spec = ProjectSpec(
        name="shop",
        entities=(
            Entity(
                name="item",
                table="items",
                fields=(Field("id", "INTEGER", pk=True), Field("qty", "INTEGER")),
            ),
        ),
    )
    source = crud_node.models_source(spec)

    assert "function nullIfBlank(value)" in source
    assert "nullIfBlank(qty)" in source


def test_the_scaffold_turns_a_rejected_async_handler_into_a_500(tmp_path):
    """Express 4 leaves the rejection unhandled and Node 15+ exits the process,
    so one bad form post is a total outage rather than one failed request."""
    from app.agent.stacks.node_adapter import NODE

    NODE.scaffold(tmp_path, "shop")
    source = (tmp_path / "server.js").read_text(encoding="utf-8")

    assert ".catch(next)" in source
    # Wrapped BEFORE any route is defined, or the routes below are not covered.
    assert source.index(".catch(next)") < source.index('app.get("/"')


# ---------------------------------------------------------------------------
# The seed must satisfy the schema it was generated beside
# ---------------------------------------------------------------------------


def _constrained_spec() -> ProjectSpec:
    """Two tables in the shape a real PRD produces: an enumeration and an FK."""
    users = Entity(
        name="user",
        table="users",
        fields=(
            Field(name="id", type="TEXT", pk=True),
            Field(name="email", type="TEXT", required=True, unique=True),
        ),
    )
    items = Entity(
        name="item",
        table="items",
        fields=(
            Field(name="id", type="TEXT", pk=True),
            Field(name="seller_id", type="TEXT", required=True, references="users(id)"),
            Field(
                name="status",
                type="TEXT",
                required=True,
                check=("DRAFT", "ACTIVE", "SOLD"),
            ),
        ),
    )
    return ProjectSpec(name="Market", entities=(users, items))


def test_a_seeded_value_is_one_the_check_constraint_allows():
    """`condition_rating` was seeded as "Demo condition_rating 1" against a
    column whose CHECK names five values, and PostgreSQL refused the row — which
    aborted the whole seed, so every page of the OpenBazaar build came up empty
    on a schema that was otherwise correct."""
    source = crud_node.seed_source(_constrained_spec())
    assert '"DRAFT"' in source
    assert "Demo status" not in source


def test_a_foreign_key_is_seeded_with_an_id_that_exists():
    """`gen_random_uuid()` mints the parent id, so it cannot be known when the
    file is written: the child insert has to use the id the parent insert
    RETURNED. Before this the value was the string "Demo seller_id 1"."""
    source = crud_node.seed_source(_constrained_spec())
    assert "RETURNING id" in source
    assert "pickId(usersIds, 1)" in source
    assert '"Demo seller_id' not in source


def test_a_second_run_still_has_parent_ids():
    """`ON CONFLICT DO NOTHING` returns NO row for one already stored, so a
    re-run would bind null into a NOT NULL foreign key unless the ids are read
    back."""
    source = crud_node.seed_source(_constrained_spec())
    assert "if (usersIds.length === 0)" in source
    assert "SELECT id FROM users LIMIT" in source


def test_a_child_of_an_unseeded_parent_is_skipped_with_its_reason():
    """Emitting the insert anyway is a guaranteed foreign-key violation, and one
    failed insert takes every later one with it."""
    orphan = Entity(
        name="bid",
        table="bids",
        fields=(
            Field(name="id", type="TEXT", pk=True),
            Field(
                name="auction_id", type="TEXT", required=True, references="auctions(id)"
            ),
        ),
    )
    source = crud_node.seed_source(ProjectSpec(name="M", entities=(orphan,)))
    assert "not seeded" in source and "auctions" in source
    assert "INSERT INTO bids" not in source


# ---------------------------------------------------------------------------
# ...and an edit to models.js must not be able to delete the API
# ---------------------------------------------------------------------------


def test_a_helper_an_edit_deleted_is_put_back():
    """Turn 2 of the OpenBazaar build: asked to add one column to the users
    queries, the model returned a models.js a third shorter, and every page
    answered `TypeError: models.listUsers is not a function`."""
    spec = _constrained_spec()
    full = crud_node.models_source(spec)
    # The shape of the real failure: one entity's helpers are simply gone.
    truncated = full.split("async function listItems")[0] + "module.exports = {\n};\n"
    assert "listItems" not in truncated

    repaired, restored = crud_node.restore_model_api(truncated, spec)

    assert "listItems" in restored
    assert "async function listItems" in repaired
    assert "listUsers" in repaired  # what survived is untouched
    assert repaired.count("async function listUsers") == 1


def test_it_refills_holes_and_never_takes_anything_away():
    spec = _constrained_spec()
    source = crud_node.models_source(spec) .replace(
        "module.exports = {", "async function findLatest() {\n  return [];\n}\n\nmodule.exports = {\n  findLatest,"
    )
    repaired, restored = crud_node.restore_model_api(source, spec)
    assert restored == []  # nothing was missing
    assert repaired == source


def test_a_file_that_still_has_everything_is_returned_byte_for_byte():
    spec = _constrained_spec()
    source = crud_node.models_source(spec)
    repaired, restored = crud_node.restore_model_api(source, spec)
    assert (repaired, restored) == (source, [])


def test_a_helper_the_file_defines_but_never_exported_is_exported():
    """A helper that is written and not exported is
    `models.listAuctions is not a function` — the same 500 as one that was
    deleted, from the opposite cause, and the turn that added it reports success
    either way. Measured: the two helpers a repair turn was asked for landed in
    the file and never reached `module.exports`."""
    spec = _constrained_spec()
    source = crud_node.models_source(spec).replace(
        "module.exports = {",
        "async function listAuctions() {\n  return [];\n}\n\nmodule.exports = {",
    )
    repaired, restored = crud_node.restore_model_api(source, spec)
    assert "listAuctions" in restored
    exports = repaired.split("module.exports")[1]
    assert "listAuctions" in exports


def test_a_private_helper_is_not_exported():
    """`nullIfBlank` is a detail of the file, not part of its API."""
    spec = _constrained_spec()
    source = crud_node.models_source(spec)
    repaired, restored = crud_node.restore_model_api(source, spec)
    assert restored == []
    assert repaired == source
