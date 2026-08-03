"""The stack seam: the adapter contract, and the rules that protect Flask.

Phases N0-N2 of `docs/node-stack-plan.md`. Two kinds of test here, and the
first kind matters more:

  * **The seam did not change Flask.** N0's exit criterion is the whole existing
    suite passing unmodified, which the rest of `tests/` covers; what this file
    adds is the handful of properties that would let a *future* edit break Flask
    quietly — `get_adapter` answering for junk input, the spec outranking the
    setting, `resolve_key` not swallowing `stdlib`.
  * **The Node adapter is honest about what it does not do.** An adapter that
    returned a Flask-shaped answer for a Node project would be worse than no
    Node stack at all, because the failure would be silent and on turn 2.

All offline: a scaffold copy, some string parsing, and `tmp_path`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent.stacks import (
    DEFAULT_KEY,
    describe_stacks,
    get_adapter,
    key_for_stack,
    probe_prefer,
    resolve_key,
    stack_keys,
)
from app.agent.stacks.flask_adapter import FLASK
from app.agent.stacks.node_adapter import NODE
from config.settings import settings


class _Spec:
    """The two fields `resolve_key` reads off a ProjectSpec."""

    def __init__(self, language="", backend=""):
        self.language = language
        self.backend = backend


ADAPTERS = [FLASK, NODE]


# ---------------------------------------------------------------------------
# get_adapter is TOTAL — a spec written before the seam existed must still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["", None, "auto", "nope", "FLASK ", "stdlib", "fastapi"]
)
def test_unknown_keys_fall_back_to_flask(key):
    """A KeyError here would turn a missing field in an old project.json into a
    dead turn. Every unrecognised input lands on the default instead."""
    assert get_adapter(key).key == DEFAULT_KEY


def test_known_keys_resolve(monkeypatch):
    assert get_adapter("flask") is FLASK
    assert get_adapter("node") is NODE
    assert get_adapter("NODE") is NODE  # case is not a typo


def test_default_is_listed_first():
    """Flask stays the default and the head of the menu: it is the stack with
    the deeper guarantees, so an accident lands on the better-verified path."""
    assert stack_keys()[0] == DEFAULT_KEY


# ---------------------------------------------------------------------------
# N1's load-bearing rule: the SPEC decides, not the session setting
# ---------------------------------------------------------------------------


def test_the_spec_beats_the_setting():
    """Without this, opening a Node project with web_stack left at "flask"
    sends the amendment path to write Python `ensure_column` calls into a db.py
    that does not exist — silently, on turn 2."""
    assert resolve_key(_Spec("node", "express"), "flask") == "node"
    assert resolve_key(_Spec("python", "flask"), "node") == "flask"


def test_the_setting_decides_only_when_the_project_has_no_memory():
    assert resolve_key(None, "node") == "node"
    assert resolve_key(_Spec("", ""), "node") == "node"


def test_language_is_checked_before_backend():
    """`runtime_probe._node()` reports backend="stdlib" with the network off,
    which collides with the PYTHON stdlib stack. Only the language tells them
    apart, so it has to win."""
    assert key_for_stack("node", "stdlib") == "node"
    assert key_for_stack("python", "stdlib") == "flask"


def test_resolve_key_does_not_swallow_the_python_stacks():
    """`resolve_key` answers "which adapter", `probe_prefer` answers "which
    stack to probe for". Conflating them turns a stdlib or fastapi build into a
    Flask one — silently, because the Flask adapter would then decline to
    scaffold and everything downstream would look normal."""
    assert resolve_key(None, "stdlib") == "flask"  # the right ADAPTER
    assert probe_prefer(None, "stdlib") == "stdlib"  # the right STACK
    assert probe_prefer(None, "fastapi") == "fastapi"
    assert probe_prefer(None, "auto") == "auto"


def test_probe_prefer_maps_express_to_the_name_detect_stack_takes():
    """A Node build persists backend="express"; `detect_stack` only understands
    "node". Passing the raw value would fall through to auto and silently
    re-probe."""
    assert probe_prefer(_Spec("node", "express"), "flask") == "node"


# ---------------------------------------------------------------------------
# The contract, both implementations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_every_adapter_answers_the_whole_protocol(adapter):
    for name in (
        "key",
        "label",
        "display_name",
        "language",
        "backends",
        "entry_file",
        "template_dir",
        "template_ext",
        "layout_file",
        "static_dir",
        "theme_file",
        "home_template",
        "db_module",
        "source_globs",
        "guarantees",
        "gaps",
    ):
        assert getattr(adapter, name) not in (None, ""), name
    for name in (
        "scaffold",
        "scaffold_files",
        "frozen_files",
        "is_frozen",
        "write_theme",
        "theme_exists",
        "write_data_layer",
        "migration_note",
        "readiness",
        "run_command",
        "seed_command",
        "write_source_if_valid",
        "routes_from_source",
        "check_links",
        "source_is_valid",
        "restore_entry_route",
        "orphan_templates",
        "convert_template",
        "build_template_graph",
        "template_edit_region",
        "ui_context",
        "scaffold_context",
    ):
        assert callable(getattr(adapter, name)), name


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_the_scaffold_tree_exists_and_matches_what_is_written(adapter, tmp_path):
    """Same drift rule the Flask scaffold already has: `scaffold_files()` is
    read by the build plan to know what already exists, so a tree it does not
    match tells generation the wrong thing."""
    written = adapter.scaffold(tmp_path, "Demo Shop")
    assert written, f"{adapter.key} scaffold copied nothing"
    assert adapter.scaffold_files() == set(written)
    for rel in written:
        assert (tmp_path / rel).is_file()


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_the_scaffold_writes_the_entry_layout_and_theme(adapter, tmp_path):
    """The three files every later phase assumes are there."""
    adapter.scaffold(tmp_path, "Demo Shop")
    assert (tmp_path / adapter.entry_file).is_file()
    assert (tmp_path / adapter.template_dir / adapter.layout_file).is_file()
    assert (tmp_path / adapter.home_template).is_file()
    assert adapter.theme_exists(tmp_path)


@pytest.mark.parametrize(
    "adapter,port", [(FLASK, 5000), (NODE, 3000)], ids=lambda a: getattr(a, "key", a)
)
def test_the_scaffold_s_own_port_is_the_first_one_probed(adapter, port, tmp_path):
    """`detect_ports` falls back to a common list, so a port it cannot read off
    the source still "works" — by probing 8000 and 5000 first and attaching to
    whatever else happens to be listening there."""
    from app.agent.smoke import detect_ports

    adapter.scaffold(tmp_path, "Demo Shop")
    source = (tmp_path / adapter.entry_file).read_text(encoding="utf-8")
    assert detect_ports(source)[0] == port


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_frozen_files_are_really_in_the_scaffold(adapter):
    """A frozen name that no scaffold writes drops a planned file for nothing."""
    assert adapter.frozen_files() <= adapter.scaffold_files()


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_placeholders_are_fully_substituted(adapter, tmp_path):
    adapter.scaffold(tmp_path, "Demo Shop")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for placeholder in (
                "{{PROJECT_NAME}}",
                "{{SECRET_KEY}}",
                "{{PROJECT_SLUG}}",
            ):
                assert placeholder not in text, f"{placeholder} left in {path.name}"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_rerunning_the_scaffold_never_overwrites(adapter, tmp_path):
    """An amendment turn calls this again; it must be a no-op, or turn 2
    silently reverts turn 1."""
    adapter.scaffold(tmp_path, "Demo Shop")
    entry = tmp_path / adapter.entry_file
    entry.write_text("// mine\n", encoding="utf-8")
    assert adapter.scaffold(tmp_path, "Demo Shop") == []
    assert entry.read_text(encoding="utf-8") == "// mine\n"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_the_theme_is_the_only_scaffold_file_write_theme_touches(adapter, tmp_path):
    adapter.scaffold(tmp_path, "Demo Shop")
    assert adapter.write_theme(tmp_path, ":root { --x: 1; }") is True
    assert (tmp_path / adapter.theme_file).read_text(encoding="utf-8") == (
        ":root { --x: 1; }"
    )
    # An empty theme is never written — an unstyled turn must not blank a
    # hand-tuned theme.
    assert adapter.write_theme(tmp_path, "   ") is False


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_ui_context_names_the_same_components_on_both_stacks(adapter):
    """`ui_context()` has to say the same thing on both stacks, or the two sites
    stop being one product. The Node helpers carry the Flask macro names for
    exactly this reason."""
    block = adapter.ui_context()
    for macro in (
        "page_header",
        "table",
        "card",
        "field",
        "badge",
        "empty_state",
        "flash_messages",
    ):
        assert macro in block, macro
    for cls in (".table-wrap", ".grid", ".card", ".empty", ".badge"):
        assert cls in block, cls
    assert adapter.theme_file in block


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_scaffold_context_is_empty_when_nothing_was_scaffolded(adapter):
    """Naming files that are not on disk is the `api_context` failure mode."""
    assert adapter.scaffold_context([]) == ""
    assert adapter.entry_file in adapter.scaffold_context(["x"])


# ---------------------------------------------------------------------------
# Flask: the delegations still return what they always did
# ---------------------------------------------------------------------------


def test_flask_run_and_seed_use_the_running_interpreter():
    """`sys.executable`, not `python`: the generated project's dependency is
    installed in the venv Coder runs from, and a bare `python` on PATH is
    routinely a different interpreter."""
    import sys

    assert FLASK.run_command("app.py") == [sys.executable, "app.py"]
    assert FLASK.seed_command() == [sys.executable, "seed.py"]


def test_flask_parses_its_own_routes():
    source = (
        '@app.route("/products")\n'
        "def products():\n"
        '    return render_template("products.html")\n'
    )
    assert FLASK.routes_from_source(source) == [
        ("GET", "/products", "products", "products.html")
    ]


def test_flask_declines_to_write_python_that_does_not_parse(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert FLASK.write_source_if_valid(target, "y = 2\n") is True
    assert FLASK.write_source_if_valid(target, "def broken(:\n") is False
    assert target.read_text(encoding="utf-8") == "y = 2\n"


def test_flask_readiness_never_gates_a_check_that_always_ran():
    """sqlite has no daemon and Flask needs no install step; `Stack.runnable`
    already answers the only question this stack has."""
    assert FLASK.readiness(Path(".")) == ""


# ---------------------------------------------------------------------------
# Node: honest about the phases that are not built yet
# ---------------------------------------------------------------------------


def test_node_writes_no_data_layer_for_a_spec_with_no_schema():
    """A spec with no entities describes no tables, so there is nothing to
    generate — and `api_context` must stay empty with it. Naming helpers that
    were never written is `api_context`'s own failure mode, inverted."""
    from app.agent.projectspec import ProjectSpec

    owned, api = NODE.write_data_layer(Path("."), ProjectSpec(name="x"))
    assert owned == set()
    assert api == ""


def test_node_reports_a_migration_it_cannot_apply(tmp_path):
    """Reported, never swallowed, and never guessed at: a half-edited schema
    file is worse than none, and a migration the caller believes ran when it
    did not is worse still."""
    from app.agent.projectspec import Entity, Field, ProjectSpec

    (tmp_path / "db.js").write_text("// db\n", encoding="utf-8")
    spec = ProjectSpec(
        name="x",
        revision=2,
        entities=(
            Entity(
                name="product",
                table="products",
                fields=(
                    Field(name="id", type="INTEGER", pk=True),
                    Field(name="colour", type="TEXT", added_in=2),
                ),
            ),
        ),
    )
    note = NODE.migration_note(tmp_path, spec, since=1)
    assert note.startswith("may not meet:")
    assert "colour" in note
    # Nothing was written.
    assert (tmp_path / "db.js").read_text(encoding="utf-8") == "// db\n"


def test_node_seeds_and_runs_with_node():
    """Running generated code is a deliberate exception that holds only because
    `crud_node.py` — not the model — writes `seed.js`. Since phase N3 it does,
    so the exception applies here exactly as it does on Flask."""
    assert NODE.seed_command() == ["node", "seed.js"]
    assert NODE.run_command("server.js") == ["node", "server.js"]


def test_node_parses_express_routes():
    source = (
        'app.get("/", (req, res) => { res.render("index"); });\n'
        'app.post("/products/new", async (req, res) => {\n'
        "  await models.createProduct(req.body.title);\n"
        '  res.redirect("/products");\n'
        "});\n"
        "app.get('/products', async (req, res) => {\n"
        "  const rows = await models.listProducts();\n"
        "  res.render('products', { rows });\n"
        "});\n"
    )
    assert NODE.routes_from_source(source) == [
        ("GET", "/", "index", "index"),
        ("POST", "/products/new", "products_new", ""),
        ("GET", "/products", "products", "products"),
    ]


def test_node_restores_a_deleted_home_route():
    """The measured Flask failure, one stack over: a 7B's edit replaces the
    block it was told to add to, and the site 404s on its own front page."""
    source = (
        'const app = express();\napp.get("/products", (req, res) => {});\n'
        "db.initDb().then(() => {\n  app.listen(PORT);\n});\n"
    )
    restored, changed = NODE.restore_entry_route(source)
    assert changed
    assert 'app.get("/", ' in restored
    # ...and it lands ABOVE the server start, not after it.
    assert restored.index('app.get("/", ') < restored.index("db.initDb()")
    # Idempotent: a file that still routes `/` is left alone.
    assert NODE.restore_entry_route(restored)[1] is False


def test_the_restored_route_lands_where_express_will_reach_it(tmp_path):
    """Against the REAL scaffold, not a sketch of it.

    Express matches middleware in registration order, so the 404 catch-all and
    the error handler are terminal. Anchoring on `app.listen(` alone put the
    "restored" home route below the 404 handler — the site went on 404ing its
    own front page while the repair reported success. Measured, not theorised.
    """
    NODE.scaffold(tmp_path, "Demo Shop")
    server = tmp_path / "server.js"
    text = server.read_text(encoding="utf-8")
    broken = text.replace(
        'app.get("/", (req, res) => {', 'app.get("/x", (req, res) => {'
    )
    assert broken != text, "the scaffold's home route moved; update this test"

    restored, changed = NODE.restore_entry_route(broken)
    assert changed
    at = restored.index('app.get("/", (req')
    assert at > restored.index("express.static"), "above the static mount"
    assert at < restored.index("res.status(404)"), "below the 404 catch-all"
    assert at < restored.index("app.use((err"), "below the error handler"
    assert at < restored.index("app.listen("), "below the server start"


def test_a_middleware_mount_is_not_mistaken_for_a_terminal_handler():
    """`app.use(express.static(…))` is a mount, not a catch-all. Reading nearby
    text instead of the handler's own parameter list flagged it as terminal
    because the comment introducing the real 404 handler was close enough."""
    source = (
        "app.use(express.static('public'));\n"
        "// the 404 handler is described down here, 404 and all\n"
        'app.get("/x", h);\n'
        "app.listen(3000);\n"
    )
    restored, changed = NODE.restore_entry_route(source)
    assert changed
    assert restored.index('app.get("/", ') > restored.index("express.static")


@pytest.mark.parametrize(
    "source",
    [
        "",  # not a route file
        "const x = 1;\n",  # ditto
        'app.get("/x", h);\n',  # nothing to anchor the insertion to
    ],
)
def test_node_declines_to_restore_when_it_cannot_place_the_route(source):
    """A route appended after the 404 handler is never reached, which is worse
    than the 404 it replaces."""
    assert NODE.restore_entry_route(source) == (source, False)


def test_node_finds_views_that_are_full_documents(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    (views / "layout.ejs").write_text("<html><body><%- body %></body></html>", "utf-8")
    (views / "_partial.ejs").write_text("<html>partial</html>", encoding="utf-8")
    (views / "good.ejs").write_text("<section>fine</section>", encoding="utf-8")
    (views / "bad.ejs").write_text(
        "<html><head><title>T</title></head><body><nav>x</nav>"
        "<section>real</section></body></html>",
        encoding="utf-8",
    )
    assert NODE.orphan_templates(tmp_path) == ["views/bad.ejs"]


def test_node_converts_a_document_view_into_a_fragment():
    source = (
        "<html><head><title>Products</title></head><body>"
        "<header><nav>everywhere</nav></header>"
        "<section>the real page</section>"
        "<footer>bye</footer></body></html>"
    )
    converted, ok = NODE.convert_template(source)
    assert ok
    assert "the real page" in converted
    # The chrome layout.ejs already renders is gone — leaving it renders TWO
    # navbars, which is worse than the drift the layout prevents.
    assert "<nav>" not in converted and "<html" not in converted.lower()
    assert "Products" in converted  # the title is carried, not silently dropped


def test_node_declines_a_conversion_that_would_empty_the_file():
    """Better a wrong-shaped page than an empty one."""
    assert NODE.convert_template(
        "<html><body><nav>only chrome</nav></body></html>"
    ) == (
        "<html><body><nav>only chrome</nav></body></html>",
        False,
    )
    assert NODE.convert_template("<section>already a fragment</section>")[1] is False


def test_node_does_not_scope_edits_to_a_block():
    """Phase W3's block editing is Jinja-shaped. None is the existing
    whole-file answer, not a new failure mode."""
    assert (
        NODE.template_edit_region("views/products.ejs", "<section>x</section>") is None
    )


def test_node_readiness_names_each_missing_piece(tmp_path, monkeypatch):
    """Three ways to be un-runnable where Flask has one, and none of them is a
    defect in the generated code — so the caller SKIPS the smoke test and says
    so, rather than sending the repair loop after correct code."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert "Node.js" in NODE.readiness(tmp_path)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")
    assert "npm install" in NODE.readiness(tmp_path)

    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)
    assert "PostgreSQL" in NODE.readiness(tmp_path)

    monkeypatch.setattr(NODE, "_postgres_listening", lambda: True)
    monkeypatch.setattr(NODE, "database_reason", lambda root: "")
    assert NODE.readiness(tmp_path) == ""


# ---------------------------------------------------------------------------
# Phase N5 — proving the database, not just the port
# ---------------------------------------------------------------------------
#
# A socket to 5432 proves a server is listening. It does not prove that THIS
# project's database exists or that its credentials work — and both of those
# fail inside `initDb()`, which the generated app treats as fatal. Without this
# half, a build reports "the smoke test failed" for a reason that is not in the
# code at all, and the repair loop is sent to rewrite something correct.

# A stand-in for the `pg` package. The probe requires the PROJECT's pg, so a
# fake one is enough to drive every branch without a database anywhere.
_PG_OK = """
class Client {
  constructor(cfg) { this.cfg = cfg; }
  connect() { return Promise.resolve(); }
  query() { return Promise.resolve({ rows: [{ "?column?": 1 }] }); }
  end() { return Promise.resolve(); }
}
module.exports = { Client };
"""


def _pg_failing(code, message):
    return f"""
class Client {{
  constructor(cfg) {{ this.cfg = cfg; }}
  connect() {{
    const e = new Error({message!r});
    e.code = {code!r};
    return Promise.reject(e);
  }}
  query() {{ return Promise.resolve(); }}
  end() {{ return Promise.resolve(); }}
}}
module.exports = {{ Client }};
"""


@pytest.fixture
def node_project(tmp_path):
    """A project root with `node_modules` and a db.js, ready to take a fake pg."""

    def build(pg_source=_PG_OK, db_source=None, database="demo_shop"):
        if db_source is None and database:
            db_source = (
                "module.exports = { DATABASE_URL: "
                f'"postgres://postgres:postgres@localhost:5432/{database}" }};\n'
            )
        modules = tmp_path / "node_modules"
        modules.mkdir(exist_ok=True)
        if pg_source is not None:
            pg = modules / "pg"
            pg.mkdir(exist_ok=True)
            (pg / "package.json").write_text(
                '{"name":"pg","version":"8.0.0","main":"index.js"}', encoding="utf-8"
            )
            (pg / "index.js").write_text(pg_source, encoding="utf-8")
        if db_source:
            (tmp_path / "db.js").write_text(db_source, encoding="utf-8")
        return tmp_path

    return build


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


@needs_node
@pytest.mark.parametrize("name", ["_PROBE_SCRIPT", "_SCHEMA_SCRIPT"])
def test_the_node_scripts_are_valid_javascript(tmp_path, name):
    """Not ceremony — `pageaudit.py` learned this the expensive way.

    A syntax error in either script fails in the WORST direction: the runner
    swallows it and returns None, which both callers read as "could not find
    out". Readiness would then report a clean environment forever, and the
    schema check would report an unreadable database. The check would be gone
    and nothing would say so.
    """
    script = tmp_path / f"{name.lower()}.js"
    script.write_text(
        getattr(NODE, name).replace("TIMEOUT_MS", "5000"), encoding="utf-8"
    )
    proc = subprocess.run(
        ["node", "--check", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


@needs_node
def test_a_reachable_database_blocks_nothing(node_project):
    assert NODE.database_reason(node_project()) == ""


@needs_node
def test_a_missing_database_is_named_with_the_command_that_creates_it(node_project):
    """`3D000` is invalid_catalog_name. This is THE case N5 exists for: the
    server is up, the port answers, and the app still cannot start."""
    root = node_project(
        _pg_failing("3D000", 'database "demo_shop" does not exist'),
        database="demo_shop",
    )
    reason = NODE.database_reason(root)
    assert "demo_shop" in reason
    assert "createdb demo_shop" in reason


@needs_node
@pytest.mark.parametrize("code", ["28P01", "28000"])
def test_rejected_credentials_are_reported_as_credentials(node_project, code):
    root = node_project(_pg_failing(code, "password authentication failed"))
    assert "credentials" in NODE.database_reason(root)


@needs_node
def test_an_unreachable_server_names_the_endpoint(node_project):
    root = node_project(_pg_failing("ECONNREFUSED", "connect ECONNREFUSED"))
    reason = NODE.database_reason(root)
    assert "localhost:5432" in reason and "ECONNREFUSED" in reason


@needs_node
def test_a_missing_pg_package_asks_for_npm_install(node_project):
    """`node_modules` exists but the dependency is not in it — a half-finished
    install, which is not the same failure as never having run one."""
    root = node_project(pg_source=None)
    assert "npm install" in NODE.database_reason(root)


@needs_node
def test_an_unrecognised_failure_is_reported_rather_than_swallowed(node_project):
    """An unknown SQLSTATE must still reach the user. Silence here would be a
    skipped check reading as a passing one."""
    root = node_project(_pg_failing("53300", "too many clients already"))
    reason = NODE.database_reason(root)
    assert reason and "too many clients already" in reason


# --- the rule that protects the smoke test ---------------------------------


@needs_node
def test_a_db_js_that_will_not_load_is_never_an_environment_problem(
    node_project, monkeypatch
):
    """THE load-bearing rule of this phase.

    A `db.js` that throws is a defect in the generated CODE. Reporting it here
    would skip the smoke test — the only check that can report it — and the turn
    would end saying the environment was at fault. It must read as "cannot tell"
    even when DATABASE_URL is set, which is why the probe tells an absent db.js
    apart from a broken one instead of collapsing both into one try/except.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:5432/fromenv")
    root = node_project(db_source="this is not ( valid javascript\n")
    assert NODE._probe_database(root) is None
    assert NODE.database_reason(root) == ""


@needs_node
def test_an_absent_db_js_still_probes_the_environment_url(node_project, monkeypatch):
    """Absent is not broken: an adopted repo may configure the URL elsewhere, so
    the probe falls through to DATABASE_URL rather than giving up."""
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:5432/fromenv")
    root = node_project(
        _pg_failing("3D000", "no such database"), db_source="", database=""
    )
    assert "fromenv" in NODE.database_reason(root)


@needs_node
def test_no_connection_string_anywhere_reports_nothing(node_project, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    root = node_project(db_source="", database="")
    assert NODE.database_reason(root) == ""


def test_a_probe_that_cannot_run_never_gates_the_smoke_test(tmp_path, monkeypatch):
    """Every uncertainty resolves to "run the check". Skipping is only correct
    when we KNOW the environment is at fault; the smoke test is the real
    measurement and a probe we could not complete must not replace it."""
    monkeypatch.setattr(NODE, "_probe_database", lambda root: None)
    assert NODE.database_reason(tmp_path) == ""


def test_a_probe_timeout_is_not_a_verdict(tmp_path, monkeypatch):
    """A hung server must not turn into "your database is fine" OR into a
    fabricated fault — `_probe_database` returns None and the check runs."""

    def explode(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="node", timeout=1)

    monkeypatch.setattr(subprocess, "run", explode)
    assert NODE._probe_database(tmp_path) is None


def test_the_probe_runs_in_the_project_and_is_bounded(tmp_path, monkeypatch):
    """It must run with cwd=root — that is what reaches the project's own
    node_modules and its own db.js — and it must carry a timeout, or one
    unreachable host stalls the whole turn."""
    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    def record(cmd, **kw):
        seen.update(kw)
        seen["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(subprocess, "run", record)
    NODE._probe_database(tmp_path)

    assert seen["cmd"][0] == "node" and seen["cmd"][1] == "-e"
    assert seen["cwd"] == str(tmp_path)
    assert seen["timeout"] > 0
    assert "TIMEOUT_MS" not in seen["cmd"][2]  # the placeholder was substituted


def test_readiness_reaches_the_database_only_after_the_cheap_checks(
    tmp_path, monkeypatch
):
    """The `SELECT 1` costs a subprocess, so the earlier steps are not merely
    redundant with it — they are what let its failure mean one specific thing."""
    calls = []
    monkeypatch.setattr(NODE, "database_reason", lambda root: calls.append(root) or "")

    monkeypatch.setattr("shutil.which", lambda name: None)
    NODE.readiness(tmp_path)
    assert calls == [], "no node, so the probe must not be spawned"

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")
    NODE.readiness(tmp_path)
    assert calls == [], "no node_modules, so the probe must not be spawned"

    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)
    NODE.readiness(tmp_path)
    assert calls == [], "nothing listening, so the probe must not be spawned"

    monkeypatch.setattr(NODE, "_postgres_listening", lambda: True)
    NODE.readiness(tmp_path)
    assert calls == [tmp_path]


def test_flask_never_pays_for_the_database_probe(tmp_path):
    """sqlite has no daemon, so this whole phase must be invisible on Flask."""
    assert FLASK.readiness(tmp_path) == ""
    assert not hasattr(FLASK, "database_reason")


# ---------------------------------------------------------------------------
# Phase N6 — reading the schema, whichever database holds it
# ---------------------------------------------------------------------------
#
# `evals/checks.py` asserts "every table the project DECLARES exists in the
# database". That question is the same on both stacks; only the answer's source
# differs, so it goes through the seam rather than through an `if` in the evals.

_PG_SCHEMA = """
class Client {
  constructor(cfg) { this.cfg = cfg; }
  connect() { return Promise.resolve(); }
  query(sql) {
    if (!/information_schema/.test(sql)) {
      return Promise.reject(new Error("unexpected query: " + sql));
    }
    return Promise.resolve({ rows: [
      { table_name: "recipes", column_name: "id" },
      { table_name: "recipes", column_name: "title" },
      { table_name: "ingredients", column_name: "id" }
    ]});
  }
  end() { return Promise.resolve(); }
}
module.exports = { Client };
"""


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_reading_the_schema_is_part_of_the_contract(adapter):
    assert callable(adapter.table_columns)


def test_flask_reads_the_sqlite_file(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    assert FLASK.table_columns(tmp_path) == {"recipes": {"id", "title"}}


def test_no_sqlite_file_is_could_not_read_not_no_tables(tmp_path):
    """None and {} are different answers. Reporting an unreadable database as an
    empty schema would turn an environment problem into "the build created no
    tables" — the misattribution this whole gate exists to prevent."""
    assert FLASK.table_columns(tmp_path) is None


@needs_node
def test_node_reads_the_schema_out_of_postgres(node_project):
    root = node_project(_PG_SCHEMA)
    assert NODE.table_columns(root) == {
        "recipes": {"id", "title"},
        "ingredients": {"id"},
    }


@needs_node
def test_a_database_node_cannot_reach_reads_as_could_not_read(node_project):
    root = node_project(pg_source=None)
    assert NODE.table_columns(root) is None


# ---------------------------------------------------------------------------
# Link validation (Phase N4) — one defect class, two vocabularies
# ---------------------------------------------------------------------------
#
# W2's rule, restated for a stack whose views name a route by its PATH rather
# than by a view name. The rules are identical either way: repoint only an
# unambiguous near miss, report everything else. Sending a link to the WRONG
# page is worse than the 404 it replaces.

# `GET /products/new` is deliberately absent: `/products/:id` also matches that
# path, which is the shape that produced a real false 405 (see the union test).
NODE_ROUTES = [
    ("GET", "/", "index", "index"),
    ("GET", "/products", "products", "products"),
    ("GET", "/products/:id", "products_id", "product"),
    ("POST", "/products/new", "products_new", ""),
]
FLASK_ROUTES = [
    ("GET", "/products", "products", "templates/products.html"),
    ("POST", "/products/new", "add_product", ""),
]


def test_node_repoints_a_near_miss_of_a_real_route():
    """`references._name_key`'s rule, inherited whole: punctuation dropped and
    one trailing plural collapsed, so `/product` -> `/products` is a slip."""
    text, fixes, problems = NODE.check_links('<a href="/product">Shop</a>', NODE_ROUTES)
    assert '<a href="/products">Shop</a>' == text
    assert fixes == [("/product", "/products")]
    assert problems == []


def test_node_leaves_a_link_to_a_different_page_alone():
    """`/edit_product` and `/add_product` are two handlers, not one misspelling.
    Reported, never repointed — the repair would be a wrong destination."""
    routes = [("GET", "/add_product", "add_product", "")]
    text, fixes, problems = NODE.check_links('<a href="/edit_product">e</a>', routes)
    assert fixes == [] and text == '<a href="/edit_product">e</a>'
    assert problems == ["link to /edit_product — no route serves it"]


def test_node_resolves_a_link_through_a_parameterised_segment():
    """`/products/5` is served by `/products/:id`. Without this every link to a
    detail page reads as broken — a false-failure flood, not a finding."""
    assert NODE.check_links('<a href="/products/5">One</a>', NODE_ROUTES) == (
        '<a href="/products/5">One</a>',
        [],
        [],
    )


def test_a_form_is_judged_against_every_route_that_matches_it():
    """THE union rule, and a real bug found and fixed while building this.

    `/products/:id` also matches `/products/new`, so taking the FIRST matching
    route reported a genuine `POST /products/new` handler as a 405 — a false
    failure on correct code. The server tries each route in turn, so the request
    405s only when NONE of them accepts the method.
    """
    assert NODE.check_links(
        '<form method="post" action="/products/new"></form>', NODE_ROUTES
    ) == ('<form method="post" action="/products/new"></form>', [], [])


def test_a_genuine_405_is_still_reported():
    """The union rule must not swallow the defect it is guarding around."""
    _text, _fixes, problems = NODE.check_links(
        '<form method="post" action="/products"></form>', NODE_ROUTES
    )
    assert problems == [
        "may not meet: the form posting POST to /products will 405 — "
        "that route only accepts GET"
    ]


def test_a_form_with_no_action_is_never_judged():
    """Which route it posts to cannot be known from the view, and a false
    failure here sends the repair loop at working code."""
    assert NODE.check_links('<form method="post"></form>', NODE_ROUTES)[2] == []


@pytest.mark.parametrize(
    "markup",
    [
        '<link rel="stylesheet" href="/css/style.css">',  # the static mount
        '<a href="https://example.com/products">out</a>',  # external
        '<a href="//cdn.example.com/x">cdn</a>',  # protocol-relative
        '<a href="#top">top</a>',  # an anchor
        '<a href="mailto:x@y.z">mail</a>',
        '<a href="products">relative</a>',  # not a route reference
        '<a href="/products/<%= p.id %>">built at render time</a>',
    ],
)
def test_what_is_not_a_route_reference_is_never_reported(markup):
    """Everything that is not a link to one of this app's own pages is dropped
    rather than guessed at — an off-disk false alarm is `references.py`'s
    lesson, and it applies to routes too."""
    text, fixes, problems = NODE.check_links(markup, NODE_ROUTES)
    assert (text, fixes, problems) == (markup, [], [])


def test_no_routes_at_all_reports_nothing():
    """An empty route list means the parser could not read the server file.
    Reporting every link on the page as broken would be the flood, not a find."""
    assert NODE.check_links('<a href="/anything">x</a>', []) == (
        '<a href="/anything">x</a>',
        [],
        [],
    )


def test_flask_still_checks_url_for_names_and_only_those():
    """N4 changed the dispatch, not Flask's behaviour: a Jinja page names a
    route by its VIEW, and W2's checks are untouched."""
    text, fixes, problems = FLASK.check_links(
        "<a href=\"{{ url_for('product') }}\">x</a>", FLASK_ROUTES
    )
    assert fixes == [("product", "products")] and "url_for('products')" in text
    assert problems == []

    _t, _f, problems = FLASK.check_links(
        "<a href=\"{{ url_for('basket') }}\">x</a>", FLASK_ROUTES
    )
    assert problems == ["url_for('basket') has no such route"]


def test_flask_does_not_judge_a_raw_path():
    """A Jinja page's routes are reached through `url_for`; a literal path is
    `_repair_page_links`' business, not this pass's. Pinned so the Node path
    check cannot leak into the Flask adapter later."""
    assert FLASK.check_links('<a href="/nope">x</a>', FLASK_ROUTES) == (
        '<a href="/nope">x</a>',
        [],
        [],
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.key)
def test_check_links_is_part_of_the_contract(adapter):
    """`core._check_endpoints` dispatches through this on every stack, so an
    adapter that lacks it takes the whole verify pass down."""
    assert callable(adapter.check_links)
    assert adapter.check_links("", []) == ("", [], [])


async def test_the_link_check_really_reaches_an_ejs_view(tmp_path, monkeypatch):
    """A check that never runs reads exactly like a passing one.

    `_check_endpoints` filtered on `.html`/`.htm`, so every `.ejs` view returned
    "" before `check_links` was ever called — the Node half of W2 was written,
    tested at the adapter, and dead at the call site.
    """
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    (tmp_path / "server.js").write_text(
        'app.get("/products", (req, res) => { res.render("products"); });\n',
        encoding="utf-8",
    )
    view = tmp_path / "views" / "products.ejs"
    view.parent.mkdir()
    view.write_text('<a href="/product">Shop</a>\n', encoding="utf-8")

    agent = AgentCore(session_id="pytest_n4_reaches_ejs")
    agent._stack_key = NODE.key  # what `_select_stack` pins from the spec

    note = await agent._check_endpoints(view, "views/products.ejs")

    assert "repointed" in note and "/product -> /products" in note
    assert '<a href="/products">Shop</a>' in view.read_text(encoding="utf-8")


async def test_the_upload_fix_really_reaches_an_ejs_view(tmp_path, monkeypatch):
    """The same reachability hole as the link check, in the pass whose whole job
    is to stop an upload silently doing nothing.

    Node really does generate upload forms (`crud_node.has_uploads`,
    `ui.field(type='file')`), and `fix_form_enctype` has exactly one caller — so
    while that caller filtered on `.html`, a `.ejs` upload form could never be
    repaired by anything.
    """
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", True)  # keep the other pass inert
    view = tmp_path / "views" / "add_product.ejs"
    view.parent.mkdir()
    view.write_text(
        '<form action="<%= url %>"><input type="file" name="cover"></form>\n',
        encoding="utf-8",
    )

    agent = AgentCore(session_id="pytest_n6_enctype_ejs")
    agent._stack_key = NODE.key

    note = await agent._fix_upload_form(view, "views/add_product.ejs")

    assert "enctype" in note
    body = view.read_text(encoding="utf-8")
    assert 'enctype="multipart/form-data"' in body
    assert "<%= url %>" in body  # and the expression survived


async def test_the_offline_asset_strip_really_reaches_an_ejs_view(
    tmp_path, monkeypatch
):
    """Until this gate took `template_ext`, the prompt-level guard in
    `buildspec.to_context_block` was the ONLY thing keeping a Node build offline
    — a hint the model is free to ignore, with no deterministic backstop."""
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", False)
    view = tmp_path / "views" / "index.ejs"
    view.parent.mkdir()
    view.write_text(
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=X">\n'
        "<h1><%= projectName %></h1>\n",
        encoding="utf-8",
    )

    agent = AgentCore(session_id="pytest_n6_offline_ejs")
    agent._stack_key = NODE.key

    note = await agent._strip_offline_dead_assets(view, "views/index.ejs")

    assert note
    body = view.read_text(encoding="utf-8")
    assert "fonts.googleapis" not in body
    assert "<%= projectName %>" in body


async def test_neither_pass_touches_a_javascript_file(tmp_path, monkeypatch):
    """Widening the gate must not widen it to everything: `.js` carries neither
    a form nor a stylesheet link, and rewriting one would be a new failure."""
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", False)
    script = tmp_path / "app.js"
    original = 'const u = "https://fonts.googleapis.com/css";\n'
    script.write_text(original, encoding="utf-8")

    agent = AgentCore(session_id="pytest_n6_js_untouched")
    agent._stack_key = NODE.key

    assert await agent._fix_upload_form(script, "app.js") == ""
    assert await agent._strip_offline_dead_assets(script, "app.js") == ""
    assert script.read_text(encoding="utf-8") == original


async def test_the_link_check_still_ignores_a_file_that_is_not_a_template(
    tmp_path, monkeypatch
):
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    (tmp_path / "server.js").write_text(
        'app.get("/products", (req, res) => {});\n', encoding="utf-8"
    )
    script = tmp_path / "app.js"
    script.write_text('const u = "/product";\n', encoding="utf-8")

    agent = AgentCore(session_id="pytest_n4_ignores_js")
    agent._stack_key = NODE.key

    assert await agent._check_endpoints(script, "app.js") == ""
    assert script.read_text(encoding="utf-8") == 'const u = "/product";\n'


# ---------------------------------------------------------------------------
# Amendments on a Node project use Node's own machinery
# ---------------------------------------------------------------------------


def _node_spec(**kw):
    from app.agent.projectspec import ProjectSpec, SpecEndpoint

    return ProjectSpec(
        name="x",
        language="node",
        backend="express",
        revision=2,
        endpoints=(
            SpecEndpoint(
                method="GET",
                path="/products",
                template="views/products.ejs",
                added_in=1,
            ),
        ),
        **kw,
    )


def test_a_live_node_route_is_not_reported_as_vanished():
    """`vanished_routes` read server.js with the `@app.route` regex, found
    nothing, and reported EVERY route the project ever had as gone — a flood of
    false failures on every Node amendment. A false failure is worse than no
    check."""
    from app.agent.impact import vanished_routes

    server = 'app.get("/products", (req, res) => res.render("products"));\n'
    assert vanished_routes(_node_spec(), server) == []
    # ...and a route that really did vanish is still caught.
    assert [e.path for e in vanished_routes(_node_spec(), "app.listen(3000);")] == [
        "/products"
    ]


def test_impact_names_the_files_this_stack_actually_has():
    """An amendment to a Node project proposed editing db.py / models.py /
    app.py, none of which exist there — so `present()` dropped them all and turn
    2 silently edited no backend file at all."""
    from app.agent.impact import _layout

    assert _layout(_node_spec()) == (
        "db.js",
        "models.js",
        "seed.js",
        "server.js",
        "views/layout.ejs",
    )
    # A spec that names no stack is a pre-N0 project.json — still Flask.
    from app.agent.projectspec import ProjectSpec

    assert _layout(ProjectSpec(name="x"))[0] == "db.py"


def test_a_restored_node_route_is_reachable_and_renders_its_view():
    from app.agent.impact import vanished_routes

    source = (
        'app.get("/x", h);\n'
        "app.use((req, res) => res.status(404).send());\n"
        "app.listen(3000);\n"
    )
    missing = vanished_routes(_node_spec(), source)
    restored_source, restored = NODE.restore_routes(source, missing)
    assert restored == ["/products"]
    # Express names a view WITHOUT its extension.
    assert 'res.render("products")' in restored_source
    assert ".ejs" not in restored_source
    assert restored_source.index('app.get("/products"') < restored_source.index(
        "res.status(404)"
    )


def test_a_post_handler_is_never_invented():
    """Its body is domain logic; restoring it would be generation, not repair."""
    from app.agent.projectspec import SpecEndpoint

    source = 'app.get("/x", h);\napp.listen(3000);\n'
    missing = [SpecEndpoint(method="POST", path="/products", template="views/p.ejs")]
    assert NODE.restore_routes(source, missing) == (source, [])


def test_a_node_page_makes_a_build_a_web_build():
    """`is_web_app` gates the scaffold copy. With `.ejs` unrecognised, a Node
    build whose plan is all views and no declared endpoint got no skeleton —
    the one thing that must happen before generation."""
    from app.agent.blueprint import ApiContract, Blueprint, PlannedFile
    from app.agent.runtime_probe import detect_stack
    from app.agent.scaffold import is_web_app

    bp = Blueprint(
        summary="s",
        files=(PlannedFile(filename="views/products.ejs", action="create"),),
        contract=ApiContract(),
        stack=detect_stack(prefer="node"),
    )
    assert is_web_app(bp) is True


def test_ejs_views_are_scanned_for_dead_references():
    from app.agent.references import REF_SCANNED_EXTS

    assert ".ejs" in REF_SCANNED_EXTS


# ---------------------------------------------------------------------------
# The menu tells the truth
# ---------------------------------------------------------------------------


def test_the_menu_states_each_stack_s_gaps():
    """Flask has import repair and block-scoped template editing that Node does
    not. A menu listing two stacks as equals is how a demo gets built on the
    weaker one by accident."""
    rows = {row["key"]: row for row in describe_stacks()}
    assert rows["flask"]["guarantees"]
    assert rows["node"]["guarantees"]
    assert rows["node"]["gaps"], "the Node stack must state what it cannot do"
    assert not rows["flask"]["gaps"]


def test_the_gaps_are_gaps_the_code_really_has(tmp_path):
    """`/stack` prints these verbatim, so a stale list is worse than no list —
    it tells someone choosing a stack the opposite of the truth.

    Each capability below is checked against the code, then against the claim.
    N4 landed three of these and the list went on denying all three, which is
    what this test exists to stop happening again quietly.
    """
    claims = " ".join(NODE.gaps).lower()

    # Landed in N4 — these must NOT read as missing any more.
    fixed, _f, _p = NODE.check_links(
        '<a href="/product">x</a>', [("GET", "/products", "products", "")]
    )
    assert fixed != '<a href="/product">x</a>', "link validation really repairs"
    assert "no route validation" not in claims
    assert "no link validation" not in claims

    from app.agent.verify import is_verifiable

    assert is_verifiable("views/x.ejs"), ".ejs really is checked"
    assert "no .ejs syntax check" not in claims

    from app.agent.projectspec import ProjectSpec

    (tmp_path / "server.js").write_text(
        'app.get("/products", (req, res) => { res.render("products"); });\n',
        encoding="utf-8",
    )
    assert ProjectSpec.from_disk(tmp_path) is not None, "a Node repo really adopts"
    assert "gets no memory" not in claims

    # Landed in N5 — readiness really runs a SELECT 1 now.
    assert callable(getattr(NODE, "database_reason", None))
    assert "SELECT 1" in " ".join(NODE.guarantees)
    assert "nothing runs `select 1`" not in claims
    assert "not proven" not in claims

    # Still genuinely missing — these must stay stated.
    assert NODE.template_edit_region("views/x.ejs", "<p>x</p>") is None
    assert "template-scoped editing" in claims
    assert not hasattr(NODE, "add_missing_imports")
    assert "missing-import repair" in claims
