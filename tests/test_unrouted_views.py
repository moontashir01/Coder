"""A page the build wrote that nothing serves, and a delta aimed at the wrong stack.

Both were measured on the OpenBazaar build and both are silent: the page is
generated, verified, converted to a layout child, linked from the navigation of
every page — and then answers 404, because nothing in the pipeline asks "is
there a route that renders this file?". The second is worse, because the file is
written somewhere the stack never looks at all.

Offline: no LLM, no browser, no database — the passes are deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.core import AgentCore
from app.agent.projectspec import Entity, Field, Page, ProjectSpec, SpecDelta, SpecEndpoint
from config.settings import settings

pytestmark = pytest.mark.asyncio


ENTRY = '''"use strict";
const express = require("express");
const models = require("./models");
const app = express();

app.get("/items", async (req, res) => {
  res.render("items", { title: "Items", items: await models.listItems() });
});

app.use((req, res) => res.status(404).send("nope"));

app.listen(3000);
'''

LAYOUT = '<nav><a href="/items">Items</a> <a href="/orders">Orders</a></nav>'


def _project(tmp_path: Path) -> Path:
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "layout.ejs").write_text(LAYOUT, encoding="utf-8")
    (tmp_path / "views" / "items.ejs").write_text("<h1>Items</h1>", encoding="utf-8")
    (tmp_path / "views" / "orders.ejs").write_text("<h1>Orders</h1>", encoding="utf-8")
    (tmp_path / "server.js").write_text(ENTRY, encoding="utf-8")
    (tmp_path / "models.js").write_text(
        "async function listOrders() { return []; }\nmodule.exports = { listOrders };\n",
        encoding="utf-8",
    )
    return tmp_path


def _spec() -> ProjectSpec:
    return ProjectSpec(
        name="Market",
        entities=(
            Entity(
                name="order",
                table="orders",
                fields=(Field(name="id", type="TEXT", pk=True),),
            ),
        ),
    )


async def _agent(tmp_path: Path, monkeypatch) -> AgentCore:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "web_stack", "node")
    monkeypatch.setattr(settings, "sandbox_root", tmp_path)
    agent = AgentCore(session_id="pytest_unrouted")
    agent._project_path = str(tmp_path)
    agent._spec = _spec()
    agent._select_stack(agent._spec)
    return agent


async def test_a_linked_page_with_no_route_gets_one(tmp_path, monkeypatch):
    root = _project(tmp_path)
    agent = await _agent(root, monkeypatch)
    try:
        note = await agent._route_unrouted_views(root)
    finally:
        agent.close()

    source = (root / "server.js").read_text(encoding="utf-8")
    assert "/orders" in note
    assert 'app.get("/orders"' in source
    assert 'res.render("orders"' in source
    # The data comes from the generated helper, so the page shows rows.
    assert "models.listOrders()" in source
    # …and above the 404 handler, or Express never reaches it.
    assert source.index('app.get("/orders"') < source.index("res.status(404)")


async def test_a_page_nothing_links_to_is_left_alone(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / "views" / "scratch.ejs").write_text("<h1>Scratch</h1>", encoding="utf-8")
    agent = await _agent(root, monkeypatch)
    try:
        await agent._route_unrouted_views(root)
    finally:
        agent.close()
    assert '"/scratch"' not in (root / "server.js").read_text(encoding="utf-8")


async def test_a_page_that_already_has_a_route_is_untouched(tmp_path, monkeypatch):
    root = _project(tmp_path)
    before = (root / "server.js").read_text(encoding="utf-8")
    agent = await _agent(root, monkeypatch)
    try:
        await agent._route_unrouted_views(root)
    finally:
        agent.close()
    after = (root / "server.js").read_text(encoding="utf-8")
    assert after.count('app.get("/items"') == 1
    assert before.split("app.get")[0] == after.split("app.get")[0]


async def test_a_delta_is_retargeted_onto_this_stacks_layout(tmp_path, monkeypatch):
    """Every example in `prompts/amend.md` is written in Flask paths, so a Node
    project was amended with `templates/login.html` — a file Express never
    renders — and `/login` went on answering 404 while the turn reported the
    page as created."""
    agent = await _agent(_project(tmp_path), monkeypatch)
    try:
        delta = SpecDelta(
            summary="add login",
            new_files=(("templates/login.html", "a login form"),),
            add_pages=(Page(route="/login", template="templates/login.html"),),
            add_endpoints=(
                SpecEndpoint(method="POST", path="/login", template="templates/login.html"),
            ),
        )
        out = agent._retarget_delta_paths(delta)
    finally:
        agent.close()

    assert out.new_files == (("views/login.ejs", "a login form"),)
    assert out.add_pages[0].template == "views/login.ejs"
    assert out.add_endpoints[0].template == "views/login.ejs"


async def test_a_path_already_right_for_the_stack_is_not_rewritten(tmp_path, monkeypatch):
    agent = await _agent(_project(tmp_path), monkeypatch)
    try:
        delta = SpecDelta(summary="x", new_files=(("views/login.ejs", "form"),))
        assert agent._retarget_delta_paths(delta).new_files == (
            ("views/login.ejs", "form"),
        )
        # …and a file that is not a template keeps its own name.
        other = SpecDelta(summary="x", new_files=(("public/js/bid.js", "js"),))
        assert agent._retarget_delta_paths(other).new_files == (
            ("public/js/bid.js", "js"),
        )
    finally:
        agent.close()


# ---------------------------------------------------------------------------
# A render call and a query call that name something almost right
# ---------------------------------------------------------------------------


async def test_a_render_naming_a_path_is_pointed_at_the_view(tmp_path):
    """Express resolves a view by STEM against the views directory, so
    `res.render("views/login.ejs")` is `Failed to lookup view` — a 500 on a page
    whose file is right there."""
    from app.agent.stacks import get_adapter

    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "login.ejs").write_text("<h1>hi</h1>", encoding="utf-8")
    out, fixes = get_adapter("node").normalize_render_names(
        'res.render("views/login.ejs");', tmp_path
    )
    assert fixes == ["views/login.ejs -> login"]
    assert 'res.render("login")' in out


async def test_a_detail_view_is_found_by_its_full_name(tmp_path):
    """The build writes `item_detail.ejs`; the route renders "item"."""
    from app.agent.stacks import get_adapter

    (tmp_path / "views").mkdir()
    for name in ("item_detail.ejs", "items.ejs", "new_item.ejs"):
        (tmp_path / "views" / name).write_text("<h1>x</h1>", encoding="utf-8")
    out, fixes = get_adapter("node").normalize_render_names(
        'res.render("item", { item });', tmp_path
    )
    assert fixes == ["item -> item_detail"]
    assert 'res.render("item_detail"' in out


async def test_an_ambiguous_name_is_left_alone(tmp_path):
    """Two views could be meant and neither is the `_detail` one, so nothing is
    rewritten: sending a route to the wrong page is worse than the error it
    replaces. (`<name>_detail` IS decided, because that is the name
    `derive_pages_from_entities` gives a detail page.)"""
    from app.agent.stacks import get_adapter

    (tmp_path / "views").mkdir()
    for name in ("item_edit.ejs", "item_admin.ejs"):
        (tmp_path / "views" / name).write_text("<h1>x</h1>", encoding="utf-8")
    source = 'res.render("item");'
    out, fixes = get_adapter("node").normalize_render_names(source, tmp_path)
    assert (out, fixes) == (source, [])


async def test_a_getter_with_a_ById_suffix_is_pointed_at_the_real_helper(tmp_path):
    """The data layer is generated, so `getItem` is the name that exists;
    `models.getItemById` is a `is not a function` 500 on the detail page."""
    from app.agent.stacks import get_adapter

    (tmp_path / "models.js").write_text(
        "async function getItem(id) { return null; }\nmodule.exports = { getItem };\n",
        encoding="utf-8",
    )
    out, fixes = get_adapter("node").repair_model_calls(
        "const item = await models.getItemById(id);", tmp_path
    )
    assert fixes == ["getItemById -> getItem"]
    assert "models.getItem(id)" in out


async def test_a_query_nobody_wrote_is_left_for_the_report(tmp_path):
    """`listAuctions` names a helper that does not exist under any name.
    Inventing one is generation, not repair."""
    from app.agent.stacks import get_adapter

    (tmp_path / "models.js").write_text(
        "async function listItems() { return []; }\nmodule.exports = { listItems };\n",
        encoding="utf-8",
    )
    source = "const rows = await models.listAuctions();"
    out, fixes = get_adapter("node").repair_model_calls(source, tmp_path)
    assert (out, fixes) == (source, [])


async def test_a_new_entity_gets_a_create_table_not_an_alter(tmp_path):
    """An amendment that introduces an entity has no table yet, and `ALTER TABLE`
    on a table that does not exist fails inside `initDb()` — which the generated
    app treats as fatal. Measured: `relation "sellers" does not exist`, and the
    whole site stopped booting."""
    from app.agent.projectspec import Entity, Field, ProjectSpec
    from app.agent.stacks import get_adapter

    (tmp_path / "db.js").write_text(
        'async function initDb() {\n  const client = await getPool().connect();\n'
        "  try {\n  } finally {\n    client.release();\n  }\n}\n",
        encoding="utf-8",
    )
    spec = ProjectSpec(
        name="M",
        revision=2,
        entities=(
            Entity(
                name="seller",
                table="sellers",
                fields=(
                    Field(name="id", type="TEXT", pk=True, added_in=2),
                    Field(name="email", type="TEXT", added_in=2),
                ),
            ),
        ),
    )
    get_adapter("node").migration_note(tmp_path, spec, since=1)
    written = (tmp_path / "db.js").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS sellers" in written
    assert 'ensureColumn(client, "sellers"' not in written


async def test_the_prompts_own_example_is_not_built(tmp_path, monkeypatch):
    """A 7B copies the illustration when the request is one it finds hard, and
    downstream a copied example is indistinguishable from a real answer.
    Measured on turn 3 of the OpenBazaar build: a request for a create-listing
    page produced `views/admin_products.ejs` and a `/admin/products` route for a
    product catalogue nobody had mentioned."""
    from app.agent.core import _drop_example_echoes

    delta = SpecDelta(
        summary="add a listing form",
        new_files=(
            ("templates/admin_products.html", "example"),
            ("views/new_listing.ejs", "the real one"),
        ),
        add_pages=(
            Page(route="/admin/products", template="templates/admin_products.html"),
            Page(route="/listings/new", template="views/new_listing.ejs"),
        ),
    )
    out = _drop_example_echoes(delta)
    assert out.new_files == (("views/new_listing.ejs", "the real one"),)
    assert [p.route for p in out.add_pages] == ["/listings/new"]


async def test_reordering_routes_never_deletes_the_startup_block(tmp_path):
    """`order_routes` sliced from the first route to the last one and rebuilt
    that span from route text alone. With a route left below the 404 handler by
    an earlier pass — which happens, and is itself a defect — the slice
    swallowed `db.initDb()` and `app.listen`, and rebuilding dropped them. The
    app then defined every handler and exited in silence, which is the one
    failure worse than a broken page."""
    from app.agent.stacks import get_adapter

    source = (
        'app.get("/orders/:id", (req, res) => {\n  res.render("order_detail");\n});\n\n'
        'app.get("/orders/new", (req, res) => {\n  res.render("new_order");\n});\n\n'
        'app.use((req, res) => {\n  res.status(404).send("no");\n});\n\n'
        "db.initDb().then(() => {\n  app.listen(3000);\n});\n\n"
        'app.get("/extra", (req, res) => {\n  res.render("extra");\n});\n'
    )
    out, moved, problems = get_adapter("node").order_routes(source)

    assert "app.listen(3000)" in out, "the startup block was deleted"
    assert "res.status(404)" in out
    assert moved == ["GET /orders/:id"]
    # The literal now registers first, and every route still registers above the
    # 404 handler — including the one that was stranded below it.
    assert out.index('app.get("/orders/new"') < out.index('app.get("/orders/:id"')
    assert out.index('app.get("/extra"') < out.index("res.status(404)")


async def test_a_render_name_with_an_extra_suffix_finds_its_view(tmp_path):
    """The model errs in both directions: "item" for `item_detail.ejs`, and
    "items_list" for `items.ejs`. Both were `Failed to lookup view`, i.e. a 500
    on a listing page whose file is right there."""
    from app.agent.stacks import get_adapter

    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "items.ejs").write_text("<h1>x</h1>", encoding="utf-8")
    out, fixes = get_adapter("node").normalize_render_names(
        'res.render("items_list", { items });', tmp_path
    )
    assert fixes == ["items_list -> items"]
    assert 'res.render("items"' in out


async def test_a_call_that_omits_generated_columns_is_reported(tmp_path):
    """`models.createUser(username, password)` against a helper that takes eight
    columns resolves, parses and routes — and inserts nulls into NOT NULL
    columns, so `POST /register` answers 500. Measured on the finished
    OpenBazaar build, where nothing in the pipeline could say why."""
    from app.agent.stacks import get_adapter

    (tmp_path / "models.js").write_text(
        "async function createUser(fullName, email, phone, passwordHash) {\n"
        "  return 1;\n}\nmodule.exports = { createUser };\n",
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "await models.createUser(username, password);\n", encoding="utf-8"
    )
    reported = get_adapter("node").call_arity_mismatches(tmp_path)
    assert len(reported) == 1
    assert "2 argument(s); it takes 4" in reported[0]


async def test_a_call_that_matches_is_not_reported(tmp_path):
    from app.agent.stacks import get_adapter

    (tmp_path / "models.js").write_text(
        "async function listItems() {\n  return [];\n}\nmodule.exports = { listItems };\n",
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "const rows = await models.listItems();\n", encoding="utf-8"
    )
    assert get_adapter("node").call_arity_mismatches(tmp_path) == []


# ---------------------------------------------------------------------------
# Route-scoped editing: one handler is shown, one handler can change
# ---------------------------------------------------------------------------


ROUTES = (
    'app.get("/items", async (req, res) => {\n  res.render("items");\n});\n\n'
    'app.get("/items/new", async (req, res) => {\n  res.render("new_item");\n});\n\n'
    'app.post("/items/new", async (req, res) => {\n  await models.createItem(a);\n});\n\n'
    'app.get("/orders", async (req, res) => {\n  res.render("orders");\n});\n\n'
    "app.use((req, res) => res.status(404).send('x'));\n\n"
    "db.initDb().then(() => {\n  app.listen(3000);\n});\n"
)


async def test_a_named_route_becomes_the_only_editable_region():
    """A 7B asked to fix one handler answers with that handler, and the
    whole-file matcher lands it as a replacement for everything it resembles —
    measured at 27 routes deleted in one turn. Inside the block that is
    impossible: `splice` copies every other byte through."""
    from app.agent.stacks import get_adapter

    region = get_adapter("node").route_edit_region(
        "server.js", ROUTES, "Fix the POST /items/new handler in server.js."
    )
    assert region is not None
    assert region.name == "POST /items/new"
    assert region.kind == "route"
    assert "models.createItem" in region.body
    assert "/orders" not in region.body  # the rest of the file is out of reach

    # …and splicing a replacement back cannot touch another route.
    spliced = region.splice(ROUTES, 'app.post("/items/new", H);\n')
    assert 'app.get("/orders"' in spliced
    assert "app.listen(3000)" in spliced
    assert "models.createItem" not in spliced


async def test_a_prefix_is_not_a_mention():
    """`/items` inside `/items/new` would otherwise make "fix /items" ambiguous
    — and picking one of two handlers is how an edit lands in the wrong place."""
    from app.agent.stacks import get_adapter

    region = get_adapter("node").route_edit_region("server.js", ROUTES, "fix /items")
    assert region is not None and region.name == "GET /items"


async def test_two_candidates_decline_to_a_whole_file_edit():
    """`/items/new` has a GET and a POST; with no method word the request is
    ambiguous, and None is today's tested path."""
    from app.agent.stacks import get_adapter

    assert (
        get_adapter("node").route_edit_region(
            "server.js", ROUTES, "fix the /items/new page"
        )
        is None
    )


async def test_a_message_naming_no_route_declines():
    from app.agent.stacks import get_adapter

    assert (
        get_adapter("node").route_edit_region("server.js", ROUTES, "tidy the app")
        is None
    )


async def test_only_the_entry_file_gets_a_route_region():
    from app.agent.stacks import get_adapter

    assert (
        get_adapter("node").route_edit_region("models.js", ROUTES, "fix GET /orders")
        is None
    )
