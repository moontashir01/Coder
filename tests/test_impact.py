"""Impact analysis (app/agent/impact.py) — Phase 3, fully offline, no LLM.

These are the rules that answer "what else does this break?", which is the
question the model is deliberately never asked.
"""

import sqlite3

import pytest

from app.agent.impact import (
    APP_FILE,
    BASE_TEMPLATE,
    DB_FILE,
    MODELS_FILE,
    SEED_FILE,
    apply_migration_block,
    describe,
    impacted_files,
    migration_block,
    restore_page_routes,
    vanished_routes,
)
from app.agent.projectspec import (
    Entity,
    Field,
    Page,
    ProjectSpec,
    SpecDelta,
    SpecEndpoint,
)

# The canonical layout the scaffold guarantees.
PROJECT_FILES = {
    APP_FILE,
    DB_FILE,
    MODELS_FILE,
    SEED_FILE,
    BASE_TEMPLATE,
    "templates/index.html",
    "templates/admin.html",
    "static/css/style.css",
}


def _bookshop() -> ProjectSpec:
    return ProjectSpec(
        name="bookshop",
        revision=1,
        entities=(
            Entity(
                "product",
                "products",
                (
                    Field("id", "INTEGER", pk=True, required=True),
                    Field("title", "TEXT", required=True),
                    Field("price", "REAL"),
                ),
            ),
        ),
        endpoints=(
            SpecEndpoint("GET", "/", template="templates/index.html"),
            SpecEndpoint(
                "POST",
                "/admin/products",
                request="{title, price}",
                template="templates/admin.html",
                entity="product",
            ),
        ),
        pages=(
            Page("/", "templates/index.html", "Home", "storefront", ("product",)),
            Page("/admin", "templates/admin.html", "Admin", "add a product"),
        ),
    )


# ---------------------------------------------------------------------------
# new field on an existing entity — the demo's turn 2
# ---------------------------------------------------------------------------


def test_a_new_field_reaches_every_file_that_touches_the_entity():
    """The money shot: "I added images, and it went back and updated db.py,
    models.py, the storefront template and the seed script.\" """
    spec = _bookshop()
    delta = SpecDelta(add_fields=(("product", Field("image_path", "IMAGE")),))

    edits = impacted_files(spec, delta, PROJECT_FILES)
    touched = {e.filename for e in edits}

    assert DB_FILE in touched  # migration
    assert MODELS_FILE in touched  # column lists
    assert SEED_FILE in touched  # demo rows
    assert "templates/index.html" in touched  # reads product -> display it
    assert "templates/admin.html" in touched  # writes product -> form input
    assert APP_FILE in touched  # read the field off the request


def test_each_edit_carries_a_specific_reason():
    """The reason is threaded into that file's instruction, so the model is told
    precisely what to change — not handed the whole request again."""
    spec = _bookshop()
    delta = SpecDelta(add_fields=(("product", Field("image_path", "IMAGE")),))

    reasons = {e.filename: e.reason for e in impacted_files(spec, delta, PROJECT_FILES)}

    assert "image_path" in reasons[MODELS_FILE]
    assert "products" in reasons[MODELS_FILE]
    assert "form input" in reasons["templates/admin.html"]
    assert "image_path" in reasons["templates/index.html"]


def test_a_template_that_does_not_read_the_entity_is_left_alone():
    spec = _bookshop()
    spec.pages = spec.pages + (Page("/about", "templates/about.html", "About"),)
    files = PROJECT_FILES | {"templates/about.html"}

    edits = impacted_files(
        spec, SpecDelta(add_fields=(("product", Field("x", "TEXT")),)), files
    )

    assert "templates/about.html" not in {e.filename for e in edits}


def test_files_that_do_not_exist_are_never_proposed_for_editing():
    """A file that isn't there is a NEW file and belongs to the create path."""
    spec = _bookshop()
    delta = SpecDelta(add_fields=(("product", Field("image_path", "IMAGE")),))

    edits = impacted_files(spec, delta, {APP_FILE})  # only app.py on disk

    assert {e.filename for e in edits} == {APP_FILE}


def test_each_reason_is_its_own_edit_not_one_merged_instruction():
    """Measured live: app.py was handed three reasons at once and the model did
    only the first, so `POST /admin/products` was silently never written. One
    surgical edit gets one thing done."""
    spec = _bookshop()
    delta = SpecDelta(
        add_fields=(("product", Field("image_path", "IMAGE")),),
        add_entities=(Entity("review", "reviews", (Field("body", "TEXT"),)),),
    )

    edits = impacted_files(spec, delta, PROJECT_FILES)
    models_reasons = [e.reason for e in edits if e.filename == MODELS_FILE]

    assert len(models_reasons) == 2  # one for the field, one for the new entity
    assert any("image_path" in r for r in models_reasons)
    assert any("reviews" in r for r in models_reasons)


def test_an_identical_reason_is_never_queued_twice():
    spec = _bookshop()
    spec.pages = spec.pages + (
        Page("/also", "templates/index.html", "Also", "", ("product",)),
    )
    edits = impacted_files(
        spec, SpecDelta(add_fields=(("product", Field("x", "TEXT")),)), PROJECT_FILES
    )
    index_edits = [e for e in edits if e.filename == "templates/index.html"]
    assert len(index_edits) == len({e.reason for e in index_edits})


def test_edits_for_one_file_are_kept_adjacent():
    """They must run back to back against the version each previous one wrote."""
    spec = _bookshop()
    delta = SpecDelta(
        add_fields=(("product", Field("image_path", "IMAGE")),),
        add_pages=(Page("/cart", "templates/cart.html", "Cart"),),
    )
    names = [e.filename for e in impacted_files(spec, delta, PROJECT_FILES)]
    for name in set(names):
        first, last = names.index(name), len(names) - 1 - names[::-1].index(name)
        assert names[first : last + 1].count(name) == last - first + 1


# ---------------------------------------------------------------------------
# new entity / endpoint / page
# ---------------------------------------------------------------------------


def test_a_new_entity_touches_the_data_layer():
    edits = impacted_files(
        _bookshop(),
        SpecDelta(add_entities=(Entity("cart", "carts", (Field("qty", "INTEGER"),)),)),
        PROJECT_FILES,
    )
    assert {DB_FILE, MODELS_FILE, SEED_FILE} <= {e.filename for e in edits}


def test_a_new_endpoint_touches_app_py_and_its_form_template():
    edits = impacted_files(
        _bookshop(),
        SpecDelta(
            add_endpoints=(
                SpecEndpoint("POST", "/cart/add", template="templates/index.html"),
            )
        ),
        PROJECT_FILES,
    )
    reasons = {e.filename: e.reason for e in edits}
    assert "/cart/add" in reasons[APP_FILE]
    assert "point the form at" in reasons["templates/index.html"]


def test_a_new_page_always_touches_base_html():
    """The nav lives in base.html and only there — this is the rule that stops
    pages drifting apart."""
    edits = impacted_files(
        _bookshop(),
        SpecDelta(add_pages=(Page("/cart", "templates/cart.html", "Cart"),)),
        PROJECT_FILES,
    )
    reasons = {e.filename: e.reason for e in edits}
    assert "nav link" in reasons[BASE_TEMPLATE] and "Cart" in reasons[BASE_TEMPLATE]
    assert APP_FILE in reasons


def test_an_empty_delta_impacts_nothing():
    assert impacted_files(_bookshop(), SpecDelta(), PROJECT_FILES) == []


def test_the_edit_set_is_capped():
    """An amendment claiming twenty files is a runaway, not a change."""
    spec = _bookshop()
    spec.pages = tuple(
        Page(f"/p{i}", f"templates/p{i}.html", f"P{i}", "", ("product",))
        for i in range(40)
    )
    files = PROJECT_FILES | {f"templates/p{i}.html" for i in range(40)}

    edits = impacted_files(
        spec, SpecDelta(add_fields=(("product", Field("x", "TEXT")),)), files
    )
    assert len(edits) <= 12


def test_describe_names_the_files_and_the_why():
    edits = impacted_files(
        _bookshop(),
        SpecDelta(add_fields=(("product", Field("image_path", "IMAGE")),)),
        PROJECT_FILES,
    )
    text = describe(edits)
    assert "models.py" in text and "image_path" in text
    assert describe([]) == ""


# ---------------------------------------------------------------------------
# The deterministic half of db.py
# ---------------------------------------------------------------------------

_DB_PY = '''"""SQLite for the shop."""

import sqlite3


def get_db():
    return sqlite3.connect("app.db")


def ensure_column(conn, table, column, decl):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db():
    conn = get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, title TEXT)")
        conn.commit()
    finally:
        conn.close()
'''


def _spec_with_new_field():
    spec = _bookshop()
    spec.merge_delta(SpecDelta(add_fields=(("product", Field("image_path", "TEXT")),)))
    return spec


def test_migration_block_comes_from_the_spec_not_the_model():
    spec = _spec_with_new_field()
    block = migration_block(spec, since=1)
    assert 'ensure_column(conn, "products", "image_path", "TEXT")' in block
    assert migration_block(_bookshop(), since=1) == ""


def test_migration_is_inserted_before_commit_and_still_runs():
    spec = _spec_with_new_field()
    updated, changed = apply_migration_block(_DB_PY, migration_block(spec, since=1))

    assert changed is True
    assert "image_path" in updated
    assert updated.index("image_path") < updated.index("conn.commit()")

    # And the rewritten module actually works against real sqlite3.
    namespace: dict = {}
    exec(compile(updated, "db.py", "exec"), namespace)
    conn = sqlite3.connect(":memory:")
    try:
        namespace["get_db"] = lambda: conn
        # Re-exec init_db with the patched get_db in scope.
        exec(compile(updated, "db.py", "exec"), namespace)
        namespace["get_db"] = lambda: conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO products (title) VALUES ('Dune')")
        namespace["ensure_column"](conn, "products", "image_path", "TEXT")
        row = conn.execute("SELECT title, image_path FROM products").fetchone()
        assert row == ("Dune", None)  # the existing row survived
    finally:
        conn.close()


def test_applying_the_same_migration_twice_is_a_no_op():
    spec = _spec_with_new_field()
    block = migration_block(spec, since=1)
    once, _ = apply_migration_block(_DB_PY, block)
    twice, changed = apply_migration_block(once, block)

    assert changed is False
    assert twice.count("image_path") == once.count("image_path")


@pytest.mark.parametrize(
    "source",
    [
        "",
        "def nothing():\n    pass\n",  # no init_db
        "def init_db():\n    pass\n",  # init_db with no conn.commit()
    ],
)
def test_unrecognisable_db_py_is_declined_not_half_edited(source):
    """A half-applied edit to the file that owns the schema is worse than none —
    the caller reports the migration instead."""
    spec = _spec_with_new_field()
    updated, changed = apply_migration_block(source, migration_block(spec, since=1))
    assert changed is False
    assert updated == source


def test_no_block_means_no_change():
    assert apply_migration_block(_DB_PY, "") == (_DB_PY, False)


# ---------------------------------------------------------------------------
# Regressions an amendment causes — "turn 1's pages still work"
# ---------------------------------------------------------------------------

_AMENDED_APP = """from flask import Flask, render_template

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin/products", methods=["GET", "POST"])
def admin_products():
    return render_template("admin.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
"""


def _amended_spec():
    """A spec at revision 2 whose /products route came from revision 1."""
    spec = _bookshop()
    spec.revision = 2
    spec.endpoints = (
        SpecEndpoint("GET", "/", template="templates/index.html", added_in=1),
        SpecEndpoint(
            "GET", "/products", template="templates/products.html", added_in=1
        ),
        SpecEndpoint("POST", "/checkout", entity="product", added_in=1),
        SpecEndpoint("POST", "/admin/products", entity="product", added_in=2),
    )
    return spec


def test_a_route_deleted_by_the_amendment_is_detected():
    """Measured live: turn 2's edit replaced turn 1's /products route, so a page
    that worked before the change 404'd after it."""
    gone = {(e.method, e.path) for e in vanished_routes(_amended_spec(), _AMENDED_APP)}
    assert ("GET", "/products") in gone
    assert ("POST", "/checkout") in gone
    assert ("GET", "/") not in gone  # still there


def test_a_route_added_this_turn_is_not_a_regression():
    """It isn't written yet — that's the coverage check's business, not this."""
    spec = _amended_spec()
    spec.endpoints = spec.endpoints + (
        SpecEndpoint("GET", "/cart", template="templates/cart.html", added_in=2),
    )
    gone = {e.path for e in vanished_routes(spec, _AMENDED_APP)}
    assert "/cart" not in gone


def test_a_deleted_page_route_is_restored_exactly():
    """A page route's whole body is `return render_template(...)`, so it can be
    put back verbatim rather than regenerated."""
    missing = vanished_routes(_amended_spec(), _AMENDED_APP)
    updated, restored = restore_page_routes(_AMENDED_APP, missing)

    assert restored == ["/products"]
    assert '@app.route("/products")' in updated
    assert 'render_template("products.html")' in updated
    assert updated.index('@app.route("/products")') < updated.index("if __name__")
    compile(updated, "app.py", "exec")


def test_a_deleted_post_handler_is_reported_not_invented():
    """Its body is domain logic; synthesizing it would be generation."""
    missing = vanished_routes(_amended_spec(), _AMENDED_APP)
    _, restored = restore_page_routes(_AMENDED_APP, missing)
    assert "/checkout" not in restored


def test_restoring_is_idempotent():
    missing = vanished_routes(_amended_spec(), _AMENDED_APP)
    once, _ = restore_page_routes(_AMENDED_APP, missing)
    twice, restored = restore_page_routes(once, missing)
    assert restored == [] and twice == once


def test_a_deterministic_pass_never_writes_python_that_does_not_compile(tmp_path):
    """The hand-editing passes run OUTSIDE `_verify_and_repair`, so nothing else
    would notice broken output. Measured: a live build shipped an `app.py` whose
    module docstring had lost its opening quotes, and every file-level check
    passed while the app would not start."""
    from app.agent.core import AgentCore

    agent = AgentCore(session_id="pytest_valid_py")
    target = tmp_path / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert agent._write_python_if_valid(target, "y = 2\n") is True
    assert target.read_text(encoding="utf-8") == "y = 2\n"

    assert agent._write_python_if_valid(target, "def broken(:\n") is False
    assert target.read_text(encoding="utf-8") == "y = 2\n"  # untouched


def test_nothing_to_restore_leaves_the_file_untouched():
    spec = _amended_spec()
    spec.endpoints = (SpecEndpoint("GET", "/", template="templates/index.html"),)
    assert vanished_routes(spec, _AMENDED_APP) == []
    assert restore_page_routes(_AMENDED_APP, []) == (_AMENDED_APP, [])
