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
        "source_is_valid",
        "restore_entry_route",
        "orphan_templates",
        "convert_template",
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


def test_node_writes_no_data_layer_and_claims_none():
    """`crud.py` is sqlite3 to its core. Emitting it into a .js file would give
    a data layer that is confidently wrong, and `api_context` would then name
    helpers that do not exist — api_context's own failure mode, inverted."""
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
    assert NODE.readiness(tmp_path) == ""


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
    """Flask has endpoint validation, deterministic migrations and import
    repair that Node does not. A menu listing two stacks as equals is how a
    demo gets built on the weaker one by accident."""
    rows = {row["key"]: row for row in describe_stacks()}
    assert rows["flask"]["guarantees"]
    assert rows["node"]["guarantees"]
    assert rows["node"]["gaps"], "the Node stack must state what it cannot do"
    assert not rows["flask"]["gaps"]
