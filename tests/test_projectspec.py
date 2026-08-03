"""ProjectSpec — persistent project memory (app/agent/projectspec.py), Phase 2.

Fully offline. The DDL tests execute against a real in-memory sqlite3, because
"emits SQL-shaped text" and "emits SQL that works" are different claims and only
one of them is worth making.
"""

import io
import json
import sqlite3

import pytest
from rich.console import Console

import app.cli.commands as commands_mod
from app.agent.blueprint import (
    TIER_CORE,
    TIER_REQUESTED,
    ApiContract,
    Blueprint,
    Endpoint,
    Feature,
    PlannedFile,
)
from app.agent.projectspec import (
    CONTEXT_BUDGET_CHARS,
    Entity,
    Field,
    Page,
    ProjectSpec,
    SpecDelta,
    SpecEndpoint,
    entities_from_sql,
    parse_schema_line,
    routes_from_source,
)
from app.agent.runtime_probe import Stack
from app.cli.commands import handle_command

FLASK_STACK = Stack(language="python", backend="flask", note="Flask is installed")


# ---------------------------------------------------------------------------
# parse_schema_line — free text becomes something diffable
# ---------------------------------------------------------------------------


def test_parses_the_blueprints_own_schema_format():
    """This exact string shape comes from app/resources/prompts/blueprint.md."""
    entity = parse_schema_line(
        "users(email TEXT PRIMARY KEY, password_hash TEXT NOT NULL) — seed a demo user"
    )

    assert entity is not None
    assert entity.table == "users"
    assert entity.name == "user"  # singularised for a readable label
    assert [f.name for f in entity.fields] == ["email", "password_hash"]
    email = entity.field("email")
    assert email.pk is True and email.required is True and email.type == "TEXT"
    assert entity.field("password_hash").required is True


def test_types_are_normalised_to_sqlite_storage_classes():
    entity = parse_schema_line(
        "products(id INT PRIMARY KEY, title VARCHAR(255), price DECIMAL(10,2), "
        "live BOOLEAN, added DATETIME)"
    )
    types = {f.name: f.type for f in entity.fields}
    assert types == {
        "id": "INTEGER",
        "title": "TEXT",
        "price": "REAL",
        "live": "INTEGER",
        "added": "TEXT",
    }


def test_parenthesised_types_are_not_split_on_their_comma():
    """`DECIMAL(10,2)` must stay one column, not become two."""
    entity = parse_schema_line("t(a DECIMAL(10,2), b TEXT)")
    assert [f.name for f in entity.fields] == ["a", "b"]


def test_table_level_constraints_are_not_columns():
    entity = parse_schema_line(
        "orders(id INTEGER PRIMARY KEY, user_id INTEGER, "
        "FOREIGN KEY (user_id) REFERENCES users(id))"
    )
    assert [f.name for f in entity.fields] == ["id", "user_id"]


@pytest.mark.parametrize(
    "line", ["", "not a schema at all", "users", "users()", "(a TEXT)"]
)
def test_unparseable_schema_lines_return_none(line):
    assert parse_schema_line(line) is None


@pytest.mark.parametrize(
    "table,expected",
    [("products", "product"), ("categories", "category"), ("status", "status")],
)
def test_singularisation(table, expected):
    entity = parse_schema_line(f"{table}(id INTEGER PRIMARY KEY)")
    assert entity.name == expected


# ---------------------------------------------------------------------------
# ddl() / migrations() — the reason fields are structured at all
# ---------------------------------------------------------------------------


def _spec_with_product():
    return ProjectSpec(
        name="bookshop",
        entities=(
            Entity(
                name="product",
                table="products",
                fields=(
                    Field("id", "INTEGER", pk=True, required=True),
                    Field("title", "TEXT", required=True),
                    Field("price", "REAL"),
                ),
            ),
        ),
    )


def test_ddl_executes_against_real_sqlite():
    """'Looks like SQL' is not the claim being made — 'sqlite accepts it' is."""
    spec = _spec_with_product()
    conn = sqlite3.connect(":memory:")
    try:
        for statement in spec.ddl():
            conn.execute(statement)
        conn.execute(
            "INSERT INTO products (title, price) VALUES (?, ?)", ("Dune", 9.99)
        )
        row = conn.execute("SELECT title, price FROM products").fetchone()
        assert row == ("Dune", 9.99)
    finally:
        conn.close()


def test_ddl_is_idempotent():
    spec = _spec_with_product()
    conn = sqlite3.connect(":memory:")
    try:
        for _ in range(2):
            for statement in spec.ddl():
                conn.execute(statement)
    finally:
        conn.close()


def test_migrations_only_cover_fields_added_after_the_given_revision():
    spec = _spec_with_product()
    spec.entities = (
        Entity(
            name="product",
            table="products",
            fields=spec.entities[0].fields + (Field("image_path", "TEXT", added_in=2),),
        ),
    )

    assert spec.migrations(since=2) == []  # nothing newer than rev 2
    later = spec.migrations(since=1)
    assert later == ['ensure_column(conn, "products", "image_path", "TEXT")']


def test_a_field_added_later_is_a_migration_not_part_of_create_table():
    """Otherwise adding a field in turn 3 would mean dropping turn 1's data."""
    spec = _spec_with_product()
    spec.entities = (
        Entity(
            name="product",
            table="products",
            fields=spec.entities[0].fields + (Field("image_path", "TEXT", added_in=2),),
        ),
    )
    assert "image_path" not in spec.ddl()[0]
    assert any("image_path" in m for m in spec.migrations(since=1))


def test_migrations_never_try_to_add_a_primary_key():
    """SQLite cannot ALTER TABLE ADD COLUMN a PRIMARY KEY."""
    spec = ProjectSpec(
        entities=(
            Entity("thing", "things", (Field("id", "INTEGER", pk=True, added_in=3),)),
        )
    )
    assert spec.migrations(since=1) == []


def test_ensure_column_migration_actually_works_against_sqlite():
    """Exercise the real primitive the scaffold ships, on a table with data."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO products (title) VALUES ('Dune')")

        def ensure_column(c, table, column, decl):
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        for _ in range(2):  # idempotent
            ensure_column(conn, "products", "image_path", "TEXT")

        row = conn.execute("SELECT title, image_path FROM products").fetchone()
        assert row == ("Dune", None)  # the existing row survived
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_round_trip_save_and_load(tmp_path):
    spec = _spec_with_product()
    spec.endpoints = (SpecEndpoint("POST", "/admin/products", "{title,price}"),)
    spec.pages = (
        Page("/", "templates/index.html", "Home", "storefront", ("product",)),
    )
    spec.summary = "Online bookstore"
    spec.language, spec.backend = "python", "flask"

    assert spec.save(tmp_path) is True
    loaded = ProjectSpec.load(tmp_path)

    assert loaded is not None
    assert loaded.name == spec.name
    assert loaded.summary == "Online bookstore"
    assert loaded.backend == "flask"
    assert [e.table for e in loaded.entities] == ["products"]
    assert [f.name for f in loaded.entities[0].fields] == ["id", "title", "price"]
    assert loaded.entities[0].field("id").pk is True
    assert [(e.method, e.path) for e in loaded.endpoints] == [
        ("POST", "/admin/products")
    ]
    assert loaded.pages[0].nav_label == "Home"


def test_spec_lands_in_the_dot_coder_directory(tmp_path):
    """Inside the project (diffable, travels with the folder) and dot-prefixed,
    so the RAG indexer and project_memory already skip it."""
    _spec_with_product().save(tmp_path)
    assert (tmp_path / ".coder" / "project.json").is_file()


def test_load_returns_none_when_absent(tmp_path):
    assert ProjectSpec.load(tmp_path) is None


@pytest.mark.parametrize(
    "content", ["{not json", "", "[]", '"a string"', '{"entities": "not a list"}']
)
def test_corrupt_spec_returns_none_never_raises(tmp_path, content):
    """A garbled spec must degrade to 'no memory' — today's behaviour — not to a
    broken turn."""
    path = ProjectSpec.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    spec = ProjectSpec.load(tmp_path)
    assert spec is None or spec.entities == ()


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    spec = _spec_with_product()
    spec.save(tmp_path)
    leftovers = list((tmp_path / ".coder").glob("*.tmp"))
    assert leftovers == []
    # And the file that landed is complete, parseable JSON.
    data = json.loads(ProjectSpec.path_for(tmp_path).read_text(encoding="utf-8"))
    assert data["spec_version"] == 1 and data["revision"] == 1


def test_save_returns_false_rather_than_raising_when_it_cannot_write(tmp_path):
    blocker = tmp_path / ".coder"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    assert _spec_with_product().save(tmp_path) is False


def test_hostile_values_are_rejected_on_load(tmp_path):
    """The spec is read back and fed to a model, so it gets the same validation
    discipline as blueprint._norm_filename / _clean_endpoints."""
    path = ProjectSpec.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "revision": 1,
                "entities": [
                    {"table": "ok_table", "fields": [{"name": "a", "type": "TEXT"}]},
                    {"table": "drop table x;--", "fields": [{"name": "b"}]},
                ],
                "endpoints": [
                    {"method": "POST", "path": "/fine"},
                    {"method": "STEAL", "path": "/bad"},
                    {"method": "GET", "path": "not-absolute"},
                ],
                "pages": [{"template": "../../etc/passwd"}],
            }
        ),
        encoding="utf-8",
    )

    spec = ProjectSpec.load(tmp_path)

    assert [e.table for e in spec.entities] == ["ok_table"]
    assert [e.path for e in spec.endpoints] == ["/fine"]
    assert all(".." not in p.template for p in spec.pages)


# ---------------------------------------------------------------------------
# to_context_block — the method that replaces "re-read the chat history"
# ---------------------------------------------------------------------------


def test_context_block_states_the_contract():
    spec = _spec_with_product()
    spec.endpoints = (SpecEndpoint("POST", "/admin/products", "{title,price}"),)
    spec.pages = (Page("/", "templates/index.html", "Home"),)

    block = spec.to_context_block()

    assert "products(" in block and "title TEXT" in block
    assert "POST /admin/products" in block
    assert "revision 1" in block


def test_context_block_stays_inside_its_budget():
    """It rides in the same prompt as the plan manifest and sibling context."""
    spec = ProjectSpec(
        name="big",
        summary="x" * 300,
        entities=tuple(
            Entity(
                f"e{i}",
                f"table{i}",
                tuple(Field(f"field_{j}", "TEXT") for j in range(20)),
            )
            for i in range(10)
        ),
        endpoints=tuple(
            SpecEndpoint("POST", f"/route/{i}", "{a,b,c}") for i in range(20)
        ),
        pages=tuple(Page(f"/p{i}", f"templates/p{i}.html", f"P{i}") for i in range(20)),
    )
    assert len(spec.to_context_block()) <= CONTEXT_BUDGET_CHARS


def test_context_block_drops_pages_before_schema():
    """Schema is what a migration depends on, so it is the last thing to go."""
    spec = ProjectSpec(
        entities=(
            Entity(
                "product", "products", tuple(Field(f"f{i}", "TEXT") for i in range(20))
            ),
        ),
        pages=tuple(
            Page(f"/p{i}", f"templates/page{i}.html", f"Page {i}") for i in range(30)
        ),
    )
    block = spec.to_context_block()
    assert "products(" in block


def test_empty_spec_still_produces_a_header():
    assert "revision 1" in ProjectSpec().to_context_block()


# ---------------------------------------------------------------------------
# from_blueprint
# ---------------------------------------------------------------------------


def _bookshop_blueprint():
    return Blueprint(
        summary="Online bookstore with admin product management",
        features=(
            Feature("Product catalog", TIER_REQUESTED, ("templates/index.html",)),
            Feature("Backend", TIER_CORE, ("app.py",)),
        ),
        files=(
            PlannedFile("app.py", "create", "routes", "backend"),
            PlannedFile("templates/index.html", "create", "storefront product grid"),
            PlannedFile("templates/admin.html", "create", "admin form"),
        ),
        contract=ApiContract(
            endpoints=(
                Endpoint("GET", "/products", "", "200 list"),
                Endpoint("POST", "/admin/products", "{title, price}", "302"),
            ),
            data_schema=(
                "products(id INTEGER PRIMARY KEY, title TEXT NOT NULL, price REAL)",
            ),
        ),
        stack=FLASK_STACK,
    )


def test_from_blueprint_maps_the_contract_to_structured_entities(tmp_path):
    spec = ProjectSpec.from_blueprint(_bookshop_blueprint(), tmp_path, "bookshop")

    assert spec.name == "bookshop"
    assert spec.backend == "flask"
    assert spec.revision == 1
    product = spec.entity("product")
    assert product is not None and product.table == "products"
    assert [f.name for f in product.fields] == ["id", "title", "price"]
    assert [(e.method, e.path) for e in spec.endpoints] == [
        ("GET", "/products"),
        ("POST", "/admin/products"),
    ]
    assert {p.template for p in spec.pages} == {
        "templates/index.html",
        "templates/admin.html",
    }
    assert spec.entity("product") is spec.entity("products")  # name or table


def test_base_html_is_not_recorded_as_a_page(tmp_path):
    """Live build recorded `templates/base.html` with the invented route `/base`
    and nav label "Base". It is the shell every page extends; an amendment turn
    would have tried to add a nav link to it."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "base.html").write_text(
        "<html><body>{% block content %}{% endblock %}</body></html>", encoding="utf-8"
    )
    bp = Blueprint(
        files=(
            PlannedFile("templates/base.html", "create", "the shell"),
            PlannedFile("templates/index.html", "create", "home"),
        ),
        contract=ApiContract(endpoints=(Endpoint("GET", "/"),)),
        stack=FLASK_STACK,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path)

    assert [p.template for p in spec.pages] == ["templates/index.html"]
    assert not any(p.route == "/base" for p in spec.pages)


def test_declared_but_unbuilt_endpoints_are_not_claimed_to_exist(tmp_path):
    """The context block says "routes that already exist — do not redefine".
    Listing one that was never written tells the model not to build it. Measured
    live: the blueprint declared POST /api/login and the coverage check reported
    it unwired on the same turn."""
    (tmp_path / "app.py").write_text(
        "from flask import Flask, render_template\n\n"
        "app = Flask(__name__)\n\n\n"
        '@app.route("/login", methods=["GET", "POST"])\n'
        "def login():\n"
        '    return render_template("login.html")\n',
        encoding="utf-8",
    )
    bp = Blueprint(
        files=(PlannedFile("templates/login.html"),),
        contract=ApiContract(
            endpoints=(
                Endpoint("POST", "/api/login", "{email, password}"),
                Endpoint("POST", "/login"),
            )
        ),
        stack=FLASK_STACK,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path)

    paths = {e.path for e in spec.endpoints}
    assert "/api/login" not in paths  # declared, never built
    assert "/login" in paths


def test_declared_endpoints_survive_when_there_is_no_backend_to_check(tmp_path):
    """With no .py on disk there is nothing to verify against, so the contract
    is all we have — keep it rather than silently emptying the spec."""
    bp = Blueprint(
        files=(PlannedFile("templates/login.html"),),
        contract=ApiContract(endpoints=(Endpoint("POST", "/api/login"),)),
        stack=FLASK_STACK,
    )
    spec = ProjectSpec.from_blueprint(bp, tmp_path)
    assert [e.path for e in spec.endpoints] == ["/api/login"]


def test_pages_include_a_route_the_blueprint_never_planned(tmp_path):
    """`GET /` renders the scaffold's index.html, which was copied in rather
    than planned — without this the home page is missing from the spec."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text(
        '{% extends "base.html" %}', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "from flask import Flask, render_template\n\n"
        "app = Flask(__name__)\n\n\n"
        '@app.route("/")\n'
        "def index():\n"
        '    return render_template("index.html")\n',
        encoding="utf-8",
    )
    bp = Blueprint(
        files=(PlannedFile("app.py"), PlannedFile("templates/products.html")),
        contract=ApiContract(endpoints=(Endpoint("GET", "/products"),)),
        stack=FLASK_STACK,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path)

    home = next((p for p in spec.pages if p.route == "/"), None)
    assert home is not None
    assert home.template == "templates/index.html"


def test_from_blueprint_reads_real_routes_off_app_py(tmp_path):
    """The spec should describe what was BUILT, so the route→template mapping is
    read from the generated app.py rather than guessed from a filename."""
    (tmp_path / "app.py").write_text(
        "from flask import Flask, render_template\n\n"
        "app = Flask(__name__)\n\n\n"
        '@app.route("/")\n'
        "def index():\n"
        '    return render_template("index.html")\n\n\n'
        '@app.route("/admin/products", methods=["GET", "POST"])\n'
        "def admin_products():\n"
        '    return render_template("admin.html")\n',
        encoding="utf-8",
    )
    bp = _bookshop_blueprint()

    spec = ProjectSpec.from_blueprint(bp, tmp_path, "bookshop")

    admin = next(p for p in spec.pages if p.template == "templates/admin.html")
    assert admin.route == "/admin/products"  # from app.py, not from the filename
    assert any(e.path == "/" for e in spec.endpoints)  # route the contract missed


def test_from_blueprint_falls_back_to_create_table_on_disk(tmp_path):
    """No declared schema, but the build really made a table — record it."""
    (tmp_path / "db.py").write_text(
        "def init_db():\n"
        "    conn.execute('''CREATE TABLE IF NOT EXISTS posts (\n"
        "        id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "        title TEXT NOT NULL,\n"
        "        body TEXT)''')\n",
        encoding="utf-8",
    )
    bp = Blueprint(
        files=(PlannedFile("app.py"), PlannedFile("templates/index.html")),
        contract=ApiContract(endpoints=(Endpoint("GET", "/posts"),)),
        stack=FLASK_STACK,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path)

    post = spec.entity("post")
    assert post is not None and post.table == "posts"
    assert [f.name for f in post.fields] == ["id", "title", "body"]


def test_entities_from_sql_ignores_commented_examples():
    """The scaffold ships a COMMENTED CREATE TABLE. It creates nothing, so
    counting it would invent a table the database does not have."""
    commented = (
        "def init_db():\n"
        "    # conn.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER)''')\n"
        "    pass\n"
    )
    assert entities_from_sql({"db": commented}) == []


def test_entities_from_sql_finds_statements_mid_file():
    """The statement is rarely the last thing in the file."""
    source = (
        "def init_db():\n"
        "    conn.execute('''CREATE TABLE IF NOT EXISTS posts (\n"
        "        id INTEGER PRIMARY KEY, title TEXT)''')\n"
        "    conn.commit()\n"
        "    conn.close()\n"
    )
    entities = entities_from_sql({"db": source})
    assert [e.table for e in entities] == ["posts"]


def test_entities_from_sql_handles_parenthesised_types():
    """`DECIMAL(10,2)` means the closing paren is not the first one."""
    source = (
        "conn.execute('''CREATE TABLE items (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    price DECIMAL(10,2),\n"
        "    name VARCHAR(255))''')\n"
    )
    entities = entities_from_sql({"db": source})
    assert [f.name for f in entities[0].fields] == ["id", "price", "name"]
    assert entities[0].field("price").type == "REAL"


# ---------------------------------------------------------------------------
# routes_from_source
# ---------------------------------------------------------------------------


def test_routes_and_templates_are_read_together():
    source = (
        '@app.route("/")\n'
        "def index():\n"
        '    return render_template("index.html")\n\n'
        '@app.route("/posts/new", methods=["GET", "POST"])\n'
        "def new_post():\n"
        "    if request.method == 'POST':\n"
        "        return redirect('/posts')\n"
        '    return render_template("new_post.html")\n'
    )
    routes = routes_from_source(source)

    assert ("GET", "/", "index", "index.html") in routes
    assert ("GET", "/posts/new", "new_post", "new_post.html") in routes
    assert ("POST", "/posts/new", "new_post", "new_post.html") in routes


def test_routes_from_empty_or_route_free_source():
    assert routes_from_source("") == []
    assert routes_from_source("x = 1\n") == []


# ---------------------------------------------------------------------------
# merge_delta
# ---------------------------------------------------------------------------


def test_merge_delta_bumps_the_revision_and_stamps_new_fields():
    spec = _spec_with_product()
    delta = SpecDelta(
        summary="add product images",
        add_fields=(("product", Field("image_path", "TEXT")),),
    )

    impacted = spec.merge_delta(delta, request="add a picture to products")

    assert spec.revision == 2
    added = spec.entity("product").field("image_path")
    assert added is not None and added.added_in == 2
    # …which is exactly what makes the migration fall out for free.
    assert spec.migrations(since=1) == [
        'ensure_column(conn, "products", "image_path", "TEXT")'
    ]
    assert {"db.py", "models.py", "seed.py"} <= set(impacted)


def test_merge_delta_records_history():
    spec = _spec_with_product()
    spec.merge_delta(
        SpecDelta(add_endpoints=(SpecEndpoint("POST", "/cart"),)), request="add a cart"
    )
    assert len(spec.history) == 1
    assert spec.history[0].revision == 2
    assert spec.history[0].request == "add a cart"
    assert "POST /cart" in spec.history[0].added


def test_merge_delta_is_idempotent_for_things_that_already_exist():
    spec = _spec_with_product()
    delta = SpecDelta(add_fields=(("product", Field("title", "TEXT")),))
    spec.merge_delta(delta)
    assert len([f for f in spec.entity("product").fields if f.name == "title"]) == 1


def test_merge_delta_survives_a_round_trip(tmp_path):
    spec = _spec_with_product()
    spec.merge_delta(
        SpecDelta(add_fields=(("product", Field("image_path", "TEXT")),)),
        request="add images",
    )
    spec.save(tmp_path)

    loaded = ProjectSpec.load(tmp_path)

    assert loaded.revision == 2
    assert loaded.entity("product").field("image_path").added_in == 2
    assert loaded.history[0].request == "add images"


# ---------------------------------------------------------------------------
# /spec — the visible proof that the agent remembers
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, spec):
        self._spec = spec

    def get_spec(self):
        return self._spec


class _FakeRepl:
    def __init__(self, spec):
        self.agent = _FakeAgent(spec)


@pytest.fixture
def captured_console(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(
        commands_mod, "console", Console(file=buf, force_terminal=False, width=200)
    )
    return buf


async def test_spec_command_shows_the_contract(captured_console):
    spec = _spec_with_product()
    spec.name = "bookshop"
    spec.language, spec.backend = "python", "flask"
    spec.endpoints = (SpecEndpoint("POST", "/admin/products", entity="product"),)
    spec.pages = (Page("/", "templates/index.html", "Home"),)

    handled = await handle_command("/spec", _FakeRepl(spec))

    out = captured_console.getvalue()
    assert handled is True
    assert "bookshop" in out and "revision 1" in out
    assert "products" in out and "title" in out
    assert "/admin/products" in out
    assert "templates/index.html" in out


async def test_spec_command_when_there_is_nothing_remembered(captured_console):
    handled = await handle_command("/spec", _FakeRepl(None))
    assert handled is True
    assert "No project spec yet" in captured_console.getvalue()


async def test_spec_command_shows_the_revision_a_field_arrived_in(captured_console):
    """The demo beat: proof that turn 2's column is recorded as turn 2's."""
    spec = _spec_with_product()
    spec.merge_delta(
        SpecDelta(add_fields=(("product", Field("image_path", "TEXT")),)),
        request="add a picture",
    )

    await handle_command("/spec", _FakeRepl(spec))

    out = captured_console.getvalue()
    assert "image_path" in out and "rev 2" in out
    assert "add a picture" in out  # history line


def test_new_page_marks_base_html_impacted():
    """The nav lives in base.html, so a new page always touches it."""
    spec = _spec_with_product()
    impacted = spec.merge_delta(
        SpecDelta(add_pages=(Page("/cart", "templates/cart.html", "Cart"),))
    )
    assert "templates/base.html" in impacted
    assert "templates/cart.html" in impacted


# ---------------------------------------------------------------------------
# from_disk — adopting a project Coder did not build (D1)
# ---------------------------------------------------------------------------


_ADOPT_APP = """
from flask import Flask, render_template, request, redirect

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/products")
def products():
    return render_template("products.html", products=get_all())


@app.route("/products/new", methods=["GET", "POST"])
def new_product():
    if request.method == "POST":
        add(request.form["title"])
        return redirect("/products")
    return render_template("new_product.html")
"""

_ADOPT_DB = '''
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    price REAL
)
"""
'''


def _write_adopted_project(root):
    """A hand-written Flask project: no .coder/, no blueprint, nothing of ours."""
    (root / "app.py").write_text(_ADOPT_APP, encoding="utf-8")
    (root / "db.py").write_text(_ADOPT_DB, encoding="utf-8")
    (root / "templates").mkdir()
    (root / "templates" / "base.html").write_text(
        "<html><body>{% block content %}{% endblock %}</body></html>", encoding="utf-8"
    )
    (root / "templates" / "index.html").write_text(
        '{% extends "base.html" %}{% block content %}Home{% endblock %}',
        encoding="utf-8",
    )
    (root / "templates" / "products.html").write_text(
        '{% extends "base.html" %}{% block content %}'
        "{% for product in products %}{{ product.title }}{% endfor %}{% endblock %}",
        encoding="utf-8",
    )
    (root / "templates" / "new_product.html").write_text(
        '{% extends "base.html" %}{% block content %}<form method="post">'
        '<input name="title"></form>{% endblock %}',
        encoding="utf-8",
    )
    (root / "static").mkdir()
    (root / "static" / "css").mkdir()
    (root / "static" / "css" / "style.css").write_text("body{}", encoding="utf-8")
    return root


def test_from_disk_recovers_the_contract_of_a_project_we_did_not_build(tmp_path):
    """The D1 promise: memory for any Flask project, not only fresh builds."""
    spec = ProjectSpec.from_disk(_write_adopted_project(tmp_path))

    assert spec is not None
    assert (spec.language, spec.backend) == ("python", "flask")
    # Tables come from the real CREATE TABLE, not from a declared schema.
    assert [e.table for e in spec.entities] == ["products"]
    assert [f.name for f in spec.entities[0].fields] == ["id", "title", "price"]
    # Routes come from real @app.route decorators, with their handler file.
    assert ("GET", "/products") in {(e.method, e.path) for e in spec.endpoints}
    assert ("POST", "/products/new") in {(e.method, e.path) for e in spec.endpoints}
    assert {e.handler for e in spec.endpoints} == {"app.py"}
    # Pages come from GET routes that really render a template on disk.
    assert {p.template for p in spec.pages} == {
        "templates/index.html",
        "templates/products.html",
        "templates/new_product.html",
    }
    assert "app.py" in spec.files and "static/css/style.css" in spec.files


def test_from_disk_excludes_the_layout_template_from_pages(tmp_path):
    """base.html is the shell every page extends, not a page with a route."""
    spec = ProjectSpec.from_disk(_write_adopted_project(tmp_path))
    assert "templates/base.html" not in {p.template for p in spec.pages}


def test_from_disk_reads_which_entities_a_page_uses(tmp_path):
    """`reads` is inferred from prose in from_blueprint and routinely empty; a
    template on disk simply says `{% for product in products %}`."""
    spec = ProjectSpec.from_disk(_write_adopted_project(tmp_path))
    products = next(p for p in spec.pages if p.template == "templates/products.html")
    assert "product" in products.reads
    home = next(p for p in spec.pages if p.template == "templates/index.html")
    assert home.reads == ()


def test_from_disk_declines_a_project_with_no_routes(tmp_path):
    """An ordinary Python folder must not acquire an invented contract."""
    (tmp_path / "utils.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "db.py").write_text(_ADOPT_DB, encoding="utf-8")  # tables, no web
    assert ProjectSpec.from_disk(tmp_path) is None


def test_from_disk_declines_an_empty_directory(tmp_path):
    assert ProjectSpec.from_disk(tmp_path) is None


def test_from_disk_does_not_record_a_page_whose_template_is_missing(tmp_path):
    """The context block says "these already exist — do not redefine them", so a
    page listed here that isn't there reads as an instruction not to build it."""
    (tmp_path / "app.py").write_text(
        "from flask import Flask, render_template\n"
        "app = Flask(__name__)\n"
        '@app.route("/ghost")\n'
        "def ghost():\n"
        '    return render_template("ghost.html")\n',
        encoding="utf-8",
    )
    spec = ProjectSpec.from_disk(tmp_path)

    assert spec is not None
    assert [e.path for e in spec.endpoints] == ["/ghost"]  # the route is real
    assert spec.pages == ()  # the page is not


def test_from_disk_skips_dot_directories(tmp_path):
    """`.coder/` must never be listed as project source — the RAG indexer and
    project_memory skip it for the same reason."""
    _write_adopted_project(tmp_path)
    (tmp_path / ".coder").mkdir()
    (tmp_path / ".coder" / "notes.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("y = 2", encoding="utf-8")

    spec = ProjectSpec.from_disk(tmp_path)
    assert not [f for f in spec.files if f.startswith(".")]


def test_from_disk_writes_nothing(tmp_path):
    """Adoption is a read. Writing .coder/project.json into someone's repo
    because they asked a question about it is a side effect they didn't ask for;
    the first amendment persists it instead."""
    _write_adopted_project(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())

    ProjectSpec.from_disk(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / ".coder").exists()


# ---------------------------------------------------------------------------
# from_disk on a Node repo Coder did not build (Phase N4)
# ---------------------------------------------------------------------------
#
# Same promise, second stack: without it a Node project that Coder did not build
# has no amendment path, no impact analysis and no migrations. Python/Flask is
# still tried FIRST and is completely unchanged, so an existing Flask repo
# adopts exactly as it did before.

_ADOPT_SERVER_JS = """
const express = require("express");
const app = express();

app.get("/", (req, res) => {
  res.render("index", { title: "Home" });
});

app.get("/products", async (req, res) => {
  res.render("products", { products: await models.listProducts() });
});

app.post("/products/new", async (req, res) => {
  await models.createProduct(req.body.title);
  res.redirect("/products");
});

app.listen(5000);
"""

_ADOPT_DB_JS = """
// Example — this is a COMMENT, not a table:
//   CREATE TABLE IF NOT EXISTS widgets (id SERIAL PRIMARY KEY, name TEXT)
const SCHEMA = `
CREATE TABLE IF NOT EXISTS products (
  id SERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  price NUMERIC
)`;
"""


def _write_adopted_node_project(root):
    """A hand-written Express project: no .coder/, no blueprint, nothing of ours."""
    (root / "server.js").write_text(_ADOPT_SERVER_JS, encoding="utf-8")
    (root / "db.js").write_text(_ADOPT_DB_JS, encoding="utf-8")
    (root / "views").mkdir()
    (root / "views" / "layout.ejs").write_text(
        "<html><body><nav><a href='/'>Home</a></nav><%- body %></body></html>",
        encoding="utf-8",
    )
    (root / "views" / "index.ejs").write_text("<h1>Home</h1>", encoding="utf-8")
    (root / "views" / "products.ejs").write_text(
        "<% products.forEach(function (product) { %>" "<%= product.title %><% }); %>",
        encoding="utf-8",
    )
    (root / "public").mkdir()
    (root / "public" / "css").mkdir()
    (root / "public" / "css" / "style.css").write_text("body{}", encoding="utf-8")
    return root


def test_from_disk_adopts_an_express_project(tmp_path):
    """The gap N4 closes: before this, a Node repo Coder did not build got no
    memory at all, because adoption read Python only."""
    spec = ProjectSpec.from_disk(_write_adopted_node_project(tmp_path))

    assert spec is not None
    assert (spec.language, spec.backend) == ("node", "express")
    assert {(e.method, e.path) for e in spec.endpoints} == {
        ("GET", "/"),
        ("GET", "/products"),
        ("POST", "/products/new"),
    }
    assert {e.handler for e in spec.endpoints} == {"server.js"}


def test_from_disk_recovers_postgres_tables_from_db_js(tmp_path):
    """`entities_from_sql` reads the same SQL either way; what differs is how the
    string literals are found, which on JS is `crud_node.js_strings`."""
    spec = ProjectSpec.from_disk(_write_adopted_node_project(tmp_path))
    assert [e.table for e in spec.entities] == ["products"]
    assert [f.name for f in spec.entities[0].fields] == ["id", "title", "price"]


def test_a_commented_create_table_is_not_a_table_on_the_node_side_either(tmp_path):
    """The `_creates_table` trap, one stack over. `db.js` ships a *commented*
    `CREATE TABLE ... widgets` example exactly as `db.py` does, and counting one
    as real already cost the Flask side a live build."""
    spec = ProjectSpec.from_disk(_write_adopted_node_project(tmp_path))
    assert "widgets" not in {e.table for e in spec.entities}


def test_from_disk_maps_an_express_route_to_the_view_it_renders(tmp_path):
    """`res.render("products")` names the view without its extension, so the
    `views/` + `.ejs` layout is what turns it back into a file on disk."""
    spec = ProjectSpec.from_disk(_write_adopted_node_project(tmp_path))
    products = next(p for p in spec.pages if p.route == "/products")
    assert products.template == "views/products.ejs"
    assert products.reads == ("product",)


def test_from_disk_excludes_the_ejs_layout_from_pages(tmp_path):
    """layout.ejs is the shell every view is wrapped in, not a page with a route
    — and no route renders it, so it must not appear as one."""
    spec = ProjectSpec.from_disk(_write_adopted_node_project(tmp_path))
    assert "views/layout.ejs" not in {p.template for p in spec.pages}


def test_from_disk_declines_a_plain_javascript_folder(tmp_path):
    """The rule that keeps adoption honest, restated: no route means no web
    project, so an ordinary JS folder never acquires an invented contract."""
    (tmp_path / "utils.js").write_text(
        "function add(a, b) { return a + b; }\nmodule.exports = { add };\n",
        encoding="utf-8",
    )
    (tmp_path / "db.js").write_text(_ADOPT_DB_JS, encoding="utf-8")  # tables, no web
    assert ProjectSpec.from_disk(tmp_path) is None


def test_node_modules_is_never_recorded_as_project_source(tmp_path):
    """The `.coder/` rule with a different name: a few thousand vendored files
    would drown the file record that exists to route an edit."""
    _write_adopted_node_project(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")

    spec = ProjectSpec.from_disk(tmp_path)
    assert not [f for f in spec.files if f.startswith("node_modules")]
    assert "server.js" in spec.files and "views/products.ejs" in spec.files


def test_python_is_tried_before_node(tmp_path):
    """A Flask repo that happens to ship a stray .js file must still adopt as
    Flask — the Python path is unchanged, and only reached-past when it finds no
    routes at all."""
    _write_adopted_project(tmp_path)
    (tmp_path / "server.js").write_text(_ADOPT_SERVER_JS, encoding="utf-8")

    spec = ProjectSpec.from_disk(tmp_path)
    assert (spec.language, spec.backend) == ("python", "flask")
    assert {e.handler for e in spec.endpoints} == {"app.py"}


def test_an_adopted_node_spec_routes_its_amendment_to_the_node_adapter(tmp_path):
    """The point of recording the stack at all. `resolve_key` reads it back, so
    an amendment cannot be handed Python `ensure_column` calls for a `db.py`
    that does not exist."""
    from app.agent.stacks import resolve_key

    spec = ProjectSpec.from_disk(_write_adopted_node_project(tmp_path))
    assert resolve_key(spec, "flask") == "node"


# ---------------------------------------------------------------------------
# The seam in AgentCore: a saved spec wins, adoption fills the gap (D1)
# ---------------------------------------------------------------------------


def test_load_or_adopt_prefers_the_saved_spec(tmp_path):
    """A real .coder/project.json carries revisions and history that a disk scan
    cannot reconstruct, so it must never be shadowed by adoption."""
    from app.agent.core import AgentCore

    _write_adopted_project(tmp_path)
    saved = _spec_with_product()
    saved.summary = "the saved one"
    assert saved.save(tmp_path)

    spec = AgentCore._load_or_adopt_spec(tmp_path)
    assert spec.summary == "the saved one"


def test_load_or_adopt_falls_back_to_disk(tmp_path):
    from app.agent.core import AgentCore

    _write_adopted_project(tmp_path)
    spec = AgentCore._load_or_adopt_spec(tmp_path)

    assert spec is not None
    assert [e.table for e in spec.entities] == ["products"]


def test_load_or_adopt_returns_none_for_an_ordinary_folder(tmp_path):
    from app.agent.core import AgentCore

    (tmp_path / "notes.txt").write_text("hello")
    assert AgentCore._load_or_adopt_spec(tmp_path) is None


def test_readme_is_not_clobbered_when_a_human_wrote_it(tmp_path):
    """D1 lets an existing repo reach the amendment path on turn 1, which is the
    first time _write_readme can meet a README that isn't ours."""
    from app.agent.core import AgentCore

    handwritten = "# My Project\n\nYears of notes live here.\n"
    (tmp_path / "README.md").write_text(handwritten, encoding="utf-8")

    AgentCore._write_readme(tmp_path, _spec_with_product())

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == handwritten


def test_readme_is_regenerated_when_coder_wrote_it(tmp_path):
    from app.agent.core import AgentCore

    spec = _spec_with_product()
    (tmp_path / "README.md").write_text(spec.to_readme(), encoding="utf-8")

    spec.summary = "now with carts"
    AgentCore._write_readme(tmp_path, spec)

    assert "now with carts" in (tmp_path / "README.md").read_text(encoding="utf-8")


def test_readme_is_written_when_there_is_none(tmp_path):
    from app.agent.core import AgentCore

    AgentCore._write_readme(tmp_path, _spec_with_product())
    assert (tmp_path / "README.md").is_file()


def test_the_scaffolds_readme_is_recognised_as_ours(tmp_path):
    """Otherwise a scaffolded project's generic README would survive every
    amendment — the exact file Phase 6 exists to replace."""
    from app.agent.projectspec import README_MARKER
    from app.agent.scaffold import flask_scaffold_dir

    shipped = (flask_scaffold_dir() / "README.md").read_text(encoding="utf-8")
    assert README_MARKER in shipped


# ---------------------------------------------------------------------------
# from_blueprint takes DECLARED relationships, not guessed ones (Phase C4)
# ---------------------------------------------------------------------------


def test_from_blueprint_takes_the_structured_schema_over_the_prose(tmp_path):
    """Phase C1 decided the schema structurally; parsing it back out of the
    prose copy would be a round trip that can only lose."""
    from app.agent.projectspec import entities_from_data

    entities = entities_from_data(
        {
            "entities": [
                {
                    "name": "product",
                    "table": "products",
                    "fields": [
                        {"name": "id", "type": "INTEGER", "pk": True},
                        {"name": "title", "type": "TEXT", "required": True},
                    ],
                }
            ]
        }
    )
    bp = Blueprint(
        summary="a shop",
        files=(PlannedFile("templates/products.html", role="frontend"),),
        contract=ApiContract(data_schema=("something_else(x TEXT)",)),
        stack=FLASK_STACK,
        entities=entities,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path, "shop")

    assert [e.table for e in spec.entities] == ["products"]
    assert spec.entities[0].field("title").required is True


def test_from_blueprint_uses_the_declared_entity_and_reads(tmp_path):
    """`_guess_entity` substring-matched the path, and `reads` was inferred from
    instruction prose — both are now the fallback, not the answer."""
    from app.agent.projectspec import entities_from_data

    entities = entities_from_data(
        {
            "entities": [
                {"name": "product", "table": "products", "fields": [{"name": "title"}]}
            ]
        }
    )
    bp = Blueprint(
        files=(
            PlannedFile(
                "templates/catalogue.html",  # name does NOT contain "product"
                role="frontend",
                instruction="the shop front",  # prose does NOT mention it either
                reads=("product",),
            ),
        ),
        contract=ApiContract(
            endpoints=(Endpoint("GET", "/catalogue", entity="product"),)
        ),
        stack=FLASK_STACK,
        entities=entities,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path, "shop")

    page = next(p for p in spec.pages if p.template == "templates/catalogue.html")
    assert page.reads == ("product",)
    # The endpoint isn't in app.py (no backend written here), so it is dropped by
    # the "declared but not built" rule — the declaration still has to survive
    # parsing, which the blueprint-level test covers.
    assert bp.contract.endpoints[0].entity == "product"


def test_from_blueprint_without_entities_behaves_as_before(tmp_path):
    """Phase C is inert when the schema call failed."""
    bp = Blueprint(
        files=(PlannedFile("templates/products.html", role="frontend"),),
        contract=ApiContract(
            data_schema=("products(id INTEGER PRIMARY KEY, title TEXT)",)
        ),
        stack=FLASK_STACK,
    )

    spec = ProjectSpec.from_blueprint(bp, tmp_path, "shop")

    assert [e.table for e in spec.entities] == ["products"]


# ---------------------------------------------------------------------------
# D2 — the file index records what a file IS, not just its role
# ---------------------------------------------------------------------------


def test_file_records_say_what_a_file_defines(tmp_path):
    from app.agent.projectspec import FileRecord

    spec = ProjectSpec.from_disk(_write_adopted_project(tmp_path))

    app_py = spec.files["app.py"]
    assert isinstance(app_py, FileRecord)
    assert app_py.role == "backend"
    assert "GET /products" in app_py.defines
    assert "POST /products/new" in app_py.defines
    # A page records the entities its markup really mentions.
    assert spec.files["templates/products.html"].reads == ("product",)


def test_a_pre_d2_project_json_still_loads(tmp_path):
    """`files` was `path -> role`. An old spec must not fail to load — it simply
    knows less, which is exactly what it knew."""
    legacy = {
        "spec_version": 1,
        "revision": 3,
        "name": "old",
        "files": {"app.py": "backend", "templates/index.html": "page"},
    }
    (tmp_path / ".coder").mkdir()
    (tmp_path / ".coder" / "project.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )

    spec = ProjectSpec.load(tmp_path)

    assert spec is not None and spec.revision == 3
    assert spec.files["app.py"].role == "backend"
    assert spec.files["app.py"].defines == ()


def test_files_given_as_plain_roles_are_normalised(tmp_path):
    """Callers (and older tests) still build a spec with the bare role; that must
    not explode inside best-effort save(), which would swallow it."""
    spec = ProjectSpec(name="x", files={"app.py": "backend"})
    assert spec.files["app.py"].role == "backend"
    assert spec.save(tmp_path) is True
    assert ProjectSpec.load(tmp_path).files["app.py"].role == "backend"


# ---------------------------------------------------------------------------
# D3 — the spec keeps up with files written outside the amendment flow
# ---------------------------------------------------------------------------


def test_reconcile_records_a_route_added_outside_an_amendment(tmp_path):
    """The drift D3 closes: only _run_blueprint and _amend_project ever wrote the
    spec, so an ordinary edit that added a route left memory describing a project
    that no longer existed."""
    _write_adopted_project(tmp_path)
    spec = ProjectSpec.from_disk(tmp_path)
    spec.revision = 4
    before = {(e.method, e.path) for e in spec.endpoints}

    # Someone edits app.py the ordinary way and adds a route + its page.
    app = tmp_path / "app.py"
    app.write_text(
        app.read_text(encoding="utf-8")
        + '\n\n@app.route("/about")\ndef about():\n    return render_template("about.html")\n',
        encoding="utf-8",
    )
    (tmp_path / "templates" / "about.html").write_text(
        '{% extends "base.html" %}{% block content %}Hi{% endblock %}', encoding="utf-8"
    )

    added = spec.reconcile_with_disk(tmp_path)

    assert "GET /about" in added
    assert ("GET", "/about") in {(e.method, e.path) for e in spec.endpoints}
    assert "templates/about.html" in {p.template for p in spec.pages}
    assert before <= {(e.method, e.path) for e in spec.endpoints}  # nothing lost
    # Stamped with the revision it appeared in, so migrations stay meaningful.
    assert next(e for e in spec.endpoints if e.path == "/about").added_in == 4


def test_reconcile_never_removes_a_vanished_route(tmp_path):
    """`impact.vanished_routes` treats a missing route as a REGRESSION to
    restore; deleting it here would destroy the evidence that check runs on."""
    _write_adopted_project(tmp_path)
    spec = ProjectSpec.from_disk(tmp_path)

    (tmp_path / "app.py").write_text(
        "from flask import Flask, render_template\n"
        "app = Flask(__name__)\n"
        '@app.route("/")\n'
        "def index():\n"
        '    return render_template("index.html")\n',
        encoding="utf-8",
    )
    spec.reconcile_with_disk(tmp_path)

    assert ("GET", "/products") in {(e.method, e.path) for e in spec.endpoints}


def test_reconcile_keeps_declared_entities_over_rediscovered_ones(tmp_path):
    """Re-deriving entities from SQL would flatten every `added_in` stamp to 1,
    and those stamps are what `migrations(since=…)` diffs on."""
    _write_adopted_project(tmp_path)
    spec = ProjectSpec.from_disk(tmp_path)
    spec.entities = (
        Entity(
            "product",
            "products",
            (Field("id", "INTEGER", pk=True), Field("title", "TEXT", added_in=3)),
        ),
    )

    spec.reconcile_with_disk(tmp_path)

    assert spec.entities[0].field("title").added_in == 3


def test_reconcile_reports_nothing_when_nothing_changed(tmp_path):
    _write_adopted_project(tmp_path)
    spec = ProjectSpec.from_disk(tmp_path)
    assert spec.reconcile_with_disk(tmp_path) == []


# ---------------------------------------------------------------------------
# D4 — the spec picks the edit target, and reaches every prompt
# ---------------------------------------------------------------------------


def _agent_on(tmp_path, monkeypatch, session):
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id=session)
    a._project_path = str(tmp_path)
    a._spec = ProjectSpec.from_disk(tmp_path)
    return a


def test_a_page_named_by_its_label_resolves_to_its_template(tmp_path, monkeypatch):
    """ "update the products page" → templates/products.html. `_extract_filename`
    is a filename regex and cannot know that; without this the request fell
    through to "whatever I wrote last"."""
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_label")

    assert a._resolve_target_from_spec("update the products page") == (
        "templates/products.html"
    )
    assert a._resolve_target_from_spec("change /products a bit") == (
        "templates/products.html"
    )


def test_target_resolution_declines_when_two_pages_match(tmp_path, monkeypatch):
    """Guessing between candidates is worse than falling through to the existing
    chain, which has its own answer."""
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_ambig")

    # Two pages are named; picking either would be a coin flip.
    assert (
        a._resolve_target_from_spec("tidy up the products page and the index page")
        is None
    )


def test_target_resolution_declines_when_nothing_matches(tmp_path, monkeypatch):
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_none")

    assert a._resolve_target_from_spec("make the styling nicer") is None
    assert a._resolve_target_from_spec("") is None


def test_target_resolution_never_returns_a_missing_file(tmp_path, monkeypatch):
    """The caller treats a missing path as "create this", which would turn a
    remembered page into a new empty one."""
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_gone")
    (tmp_path / "templates" / "products.html").unlink()

    assert a._resolve_target_from_spec("update the products page") is None


def test_target_resolution_finds_a_backend_file_by_name(tmp_path, monkeypatch):
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_file")

    # No page matches "style", so the file index answers instead.
    assert a._resolve_target_from_spec("fix the style") == "static/css/style.css"


def test_the_spec_reaches_prompts_outside_the_amendment_path(tmp_path, monkeypatch):
    """A request that misses _AMEND_VERB_RE was answered with no idea what the
    project contains."""
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_ctx")

    block = a._spec_context()

    assert "products" in block
    assert "/products" in block


def test_the_spec_block_is_not_repeated_when_the_caller_has_one(tmp_path, monkeypatch):
    """_run_blueprint and _amend_project build richer blocks of their own;
    stating the same routes twice spends llm_num_ctx to contradict nothing."""
    _write_adopted_project(tmp_path)
    a = _agent_on(tmp_path, monkeypatch, "pytest_d4_dupe")

    assert a._spec_context("## This project already exists — revision 2") == ""
    assert a._spec_context("## Build blueprint — applies to EVERY file") == ""
