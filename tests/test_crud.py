"""Deterministic data-layer codegen (app/agent/crud.py) — Phase 4a/4b/4d.

Fully offline. The generated SQL is EXECUTED against a real in-memory sqlite3,
because "emits plausible-looking SQL" and "emits SQL that works" are different
claims and only one of them is worth making.
"""

import sqlite3

import pytest

from app.agent.crud import (
    ALLOWED_UPLOAD_EXTENSIONS,
    api_context,
    apply_table_block,
    entity_helpers,
    has_uploads,
    models_source,
    plaintext_password_writes,
    seed_source,
    table_block,
    upload_helper_source,
)
from app.agent.projectspec import Entity, Field, ProjectSpec


def _shop() -> ProjectSpec:
    return ProjectSpec(
        name="bookshop",
        entities=(
            Entity(
                "product",
                "products",
                (
                    Field("id", "INTEGER", pk=True, required=True),
                    Field("title", "TEXT", required=True),
                    Field("author", "TEXT"),
                    Field("price", "REAL"),
                ),
            ),
            Entity(
                "user",
                "users",
                (
                    Field("email", "TEXT", pk=True, required=True),
                    Field("password_hash", "TEXT"),
                ),
            ),
        ),
    )


def _run(spec: ProjectSpec):
    """Create the tables, exec the generated modules, return (conn, namespace)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for statement in spec.ddl():
        conn.execute(statement)

    namespace: dict = {"get_db": lambda: conn}
    source = models_source(spec).replace("from db import get_db\n", "")
    source = source.replace(
        "from werkzeug.security import generate_password_hash  # noqa: F401\n", ""
    )
    # Never really close the shared in-memory connection.
    exec(
        compile(source.replace("conn.close()", "pass"), "models.py", "exec"), namespace
    )
    return conn, namespace


# ---------------------------------------------------------------------------
# 4a — models.py
# ---------------------------------------------------------------------------


def test_generated_models_compile():
    compile(models_source(_shop()), "models.py", "exec")


def test_the_full_crud_cycle_runs_against_real_sqlite():
    """The claim is not "this looks like SQL" — it is "sqlite accepts it"."""
    spec = _shop()
    conn, ns = _run(spec)
    try:
        new_id = ns["create_product"]("Dune", "Herbert", 9.99)
        assert new_id == 1

        rows = ns["list_products"]()
        assert [r["title"] for r in rows] == ["Dune"]

        one = ns["get_product"](new_id)
        assert one["author"] == "Herbert" and one["price"] == 9.99

        ns["update_product"](new_id, "Dune Messiah", "Herbert", 12.50)
        assert ns["get_product"](new_id)["title"] == "Dune Messiah"

        ns["delete_product"](new_id)
        assert ns["list_products"]() == []
        assert ns["get_product"](new_id) is None
    finally:
        conn.close()


def test_an_entity_with_a_text_primary_key_still_works():
    """`users` keys on email, so nothing may assume an autoincrement id."""
    spec = _shop()
    conn, ns = _run(spec)
    try:
        ns["create_user"]("a@b.c", "hashed")
        assert ns["get_user"]("a@b.c")["password_hash"] == "hashed"
        assert [u["email"] for u in ns["list_users"]()] == ["a@b.c"]
    finally:
        conn.close()


def test_an_autoincrement_key_is_never_asked_for_as_an_argument():
    source = entity_helpers(_shop().entities[0])
    assert "def create_product(title, author, price):" in source
    assert "def create_product(id" not in source


def test_a_natural_email_lookup_is_provided():
    """The thing a login route actually needs, when email is NOT the key."""
    spec = ProjectSpec(
        entities=(
            Entity(
                "user",
                "users",
                (
                    Field("id", "INTEGER", pk=True),
                    Field("email", "TEXT"),
                    Field("password_hash", "TEXT"),
                ),
            ),
        )
    )
    source = models_source(spec)
    assert "def get_user_by_email(email):" in source
    compile(source, "models.py", "exec")


def test_a_text_primary_key_is_not_repeated_in_update():
    """`users` keys on email, so `update_user(email, email, …)` would be a
    SyntaxError — the key identifies the row, it is never also a set column."""
    source = models_source(_shop())
    assert "def update_user(email, password_hash):" in source
    compile(source, "models.py", "exec")


def test_every_value_is_bound_not_interpolated():
    """SQL injection is impossible by construction, not by a check."""
    source = models_source(_shop())
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith('"') and (
            "INSERT" in line or "SELECT" in line or "UPDATE" in line
        ):
            assert "%s" not in line and "{" not in line and " + " not in line


def test_injection_through_a_value_is_inert():
    spec = _shop()
    conn, ns = _run(spec)
    try:
        ns["create_product"]("'); DROP TABLE products; --", "x", 1.0)
        assert len(ns["list_products"]()) == 1  # the table is still there
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
    finally:
        conn.close()


def test_column_lists_match_the_table_by_construction():
    """Both are printed from the same Entity, so they cannot drift — this is the
    `models.get_all_posts` / `add_post` mismatch that broke live builds."""
    spec = _shop()
    source = models_source(spec)
    for field in spec.entities[0].fields:
        assert field.name in source


def test_a_spec_with_no_entities_yields_an_importable_stub():
    source = models_source(ProjectSpec())
    compile(source, "models.py", "exec")
    assert "get_db" in source


# ---------------------------------------------------------------------------
# 4d — seed.py
# ---------------------------------------------------------------------------


def test_generated_seed_compiles_and_fills_every_table():
    spec = _shop()
    source = seed_source(spec)
    compile(source, "seed.py", "exec")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        for statement in spec.ddl():
            conn.execute(statement)
        ns = {
            "db": type("db", (), {"get_db": staticmethod(lambda: conn)}),
            "generate_password_hash": lambda p: "hashed:" + p,
        }
        body = source.replace("conn.close()", "pass").replace("import db\n", "")
        body = body.replace(
            "from werkzeug.security import generate_password_hash\n", ""
        )
        exec(compile(body, "seed.py", "exec"), ns)
        ns["seed"]()

        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    finally:
        conn.close()


def test_seeded_passwords_are_hashed_not_plaintext():
    source = seed_source(_shop())
    assert "generate_password_hash(" in source
    assert '"demo-password"' in source
    assert plaintext_password_writes(source) == []


def test_seed_for_an_empty_spec_is_still_valid():
    compile(seed_source(ProjectSpec()), "seed.py", "exec")


# ---------------------------------------------------------------------------
# db.py — the CREATE TABLE half
# ---------------------------------------------------------------------------

_DB_PY = """import sqlite3


def get_db():
    return sqlite3.connect("app.db")


def init_db():
    conn = get_db()
    try:
        conn.commit()
    finally:
        conn.close()
"""


def test_tables_are_inserted_into_init_db_and_execute():
    updated, changed = apply_table_block(_DB_PY, _shop())

    assert changed is True
    assert updated.index("CREATE TABLE") < updated.index("conn.commit()")
    compile(updated, "db.py", "exec")

    conn = sqlite3.connect(":memory:")
    try:
        ns = {"sqlite3": sqlite3, "get_db": lambda: conn}
        exec(compile(updated.replace("conn.close()", "pass"), "db.py", "exec"), ns)
        ns["get_db"] = lambda: conn
        ns["init_db"]()
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"products", "users"} <= names
    finally:
        conn.close()


def test_applying_tables_twice_is_a_no_op():
    once, _ = apply_table_block(_DB_PY, _shop())
    twice, changed = apply_table_block(once, _shop())
    assert changed is False and twice == once


@pytest.mark.parametrize("source", ["", "def nothing():\n    pass\n"])
def test_unrecognisable_db_py_is_declined(source):
    assert apply_table_block(source, _shop()) == (source, False)


def test_no_entities_means_no_change():
    assert apply_table_block(_DB_PY, ProjectSpec()) == (_DB_PY, False)


def test_a_commented_example_does_not_count_as_an_existing_table():
    """Measured live: the scaffold's own commented `CREATE TABLE ... products`
    example made the idempotency check skip the real products table, so every
    product route 500'd with "no such table" while users worked fine."""
    with_comment = _DB_PY.replace(
        "def init_db():",
        "def init_db():\n    # e.g. CREATE TABLE IF NOT EXISTS products (id INTEGER)",
    )
    updated, changed = apply_table_block(with_comment, _shop())

    assert changed is True
    created = [
        line
        for line in updated.splitlines()
        if "CREATE TABLE" in line and "#" not in line
    ]
    assert any("products" in line for line in created)
    assert any("users" in line for line in created)


# ---------------------------------------------------------------------------
# api_context — telling the model what replaced the file it can't see
# ---------------------------------------------------------------------------


def test_api_context_lists_the_exact_callable_names():
    """Measured live: with the data layer taken away but not described, app.py
    opened `from models import get_user_by_email, get_all_products, User,
    Product` — four invented names — and died at import."""
    block = api_context(_shop())

    assert "models.list_products()" in block
    assert "models.create_product(title, author, price)" in block
    assert "models.get_product(id)" in block
    assert "models.create_user(email, password_hash)" in block
    assert "do not import classes" in block  # no User/Product to import


def test_api_context_signatures_match_the_generated_functions():
    """The description and the code must not drift — the whole point."""
    spec = _shop()
    source = models_source(spec)
    for line in api_context(spec).splitlines():
        if not line.startswith("- `models."):
            continue
        name = line.split("models.", 1)[1].split("(", 1)[0]
        assert f"def {name}(" in source, name


def test_api_context_is_empty_without_entities():
    assert api_context(ProjectSpec()) == ""


# ---------------------------------------------------------------------------
# 4b — uploads
# ---------------------------------------------------------------------------


def _upload_spec():
    return ProjectSpec(
        entities=(
            Entity(
                "product",
                "products",
                (
                    Field("id", "INTEGER", pk=True),
                    Field("title", "TEXT"),
                    Field("image_path", "TEXT"),
                ),
            ),
        )
    )


def test_uploads_are_detected_from_the_field():
    assert has_uploads(_upload_spec()) is True
    assert has_uploads(_shop()) is False


def test_the_upload_helper_allowlists_extensions_and_compiles():
    source = upload_helper_source()
    compile(
        "from pathlib import Path\nUPLOAD_DIR = Path('.')\n" + source, "app.py", "exec"
    )
    for ext in ALLOWED_UPLOAD_EXTENSIONS:
        assert f'"{ext}"' in source
    assert "secure_filename" in source
    assert "while target.exists():" in source  # collision-safe


def test_the_upload_helper_rejects_traversal_and_bad_extensions(tmp_path):
    ns = {}
    preamble = (
        "from pathlib import Path\n"
        "from werkzeug.utils import secure_filename\n"
        f"UPLOAD_DIR = Path(r'{tmp_path}')\n"
    )
    exec(compile(preamble + upload_helper_source(), "app.py", "exec"), ns)
    save_upload = ns["save_upload"]

    class _File:
        def __init__(self, filename):
            self.filename = filename
            self.saved_to = None

        def save(self, target):
            self.saved_to = target
            open(target, "wb").close()

    assert save_upload(None) == ""
    assert save_upload(_File("")) == ""
    assert save_upload(_File("evil.exe")) == ""  # not on the allowlist
    assert save_upload(_File("notes")) == ""  # no extension

    escaped = _File("../../etc/passwd.png")
    name = save_upload(escaped)
    assert name and ".." not in name
    assert escaped.saved_to.parent == tmp_path  # jailed to the upload dir

    first = save_upload(_File("photo.png"))
    second = save_upload(_File("photo.png"))
    assert first != second  # collision-safe


# ---------------------------------------------------------------------------
# 4c — the one part of auth that must never be a prompt instruction
# ---------------------------------------------------------------------------


def test_a_raw_password_on_its_way_to_storage_is_flagged():
    source = (
        "@app.route('/register', methods=['POST'])\n"
        "def register():\n"
        "    password = request.form['password']\n"
        "    models.create_user(email, password)\n"
    )
    assert plaintext_password_writes(source)


def test_read_then_hash_is_not_flagged():
    """A prompt instruction is advice; this is a check on the code — but it must
    not cry wolf on the correct pattern."""
    source = (
        "from werkzeug.security import generate_password_hash\n"
        "def register():\n"
        "    password = request.form['password']\n"
        "    models.create_user(email, generate_password_hash(password))\n"
    )
    assert plaintext_password_writes(source) == []


def test_unrelated_code_is_not_flagged():
    assert plaintext_password_writes("title = request.form['title']\n") == []
    assert plaintext_password_writes("") == []
