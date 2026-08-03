"""The template-aware dependency index (Phase W8, docs/web-quality-plan.md).

The gap being closed: `symbols.py` resolved imports for Python only, so the
graph stopped at `app.py`, and everything downstream that needed "which template
shows a product" had to fall back on `Page.reads` — which is inferred from
blueprint prose and, as CLAUDE.md records, is *routinely empty on the very
listing page that matters*.

Most of what follows asserts the module does NOT produce an edge. That is the
load-bearing half: `impact.py` turns every edge into an instruction to rewrite a
file, so a wrong edge is a 7B model let loose on a file that was fine.
"""

from pathlib import Path

import pytest

from app.agent.core import AgentCore
from app.agent.impact import impacted_files
from app.agent.projectspec import Entity, Field, Page, ProjectSpec, SpecDelta
from app.agent.stacks.flask_adapter import FLASK
from app.agent.stacks.node_adapter import NODE
from app.agent.templatedeps import (
    TemplateGraph,
    build_graph,
    is_layout,
    parse_ejs_template,
    parse_template,
    view_bodies,
)
from app.rag.symbols import SymbolIndex, extract_symbols

BASE = """<!doctype html>
<html><head><title>{% block title %}Shop{% endblock %}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
</head><body>
<nav><a href="/">Home</a> <a href="{{ url_for('products') }}">Products</a></nav>
<main>{% block content %}{% endblock %}</main>
<script src="{{ url_for('static', filename='js/app.js') }}"></script>
</body></html>
"""

PRODUCTS = """{% extends "base.html" %}
{% import "_macros.html" as ui %}
{% block content %}
<h1>Products</h1>
{% for product in products %}
  <div class="card">{{ product.title }} — {{ product.price }}</div>
{% endfor %}
{% endblock %}
"""

ADD_PRODUCT = """{% extends "base.html" %}
{% block content %}
<form method="post" action="{{ url_for('add_product') }}" enctype="multipart/form-data">
  <input name="title"><input type="file" name="cover">
  <button type="submit">Save</button>
</form>
{% endblock %}
"""

APP_PY = """
from flask import Flask, render_template, request, redirect, url_for
import models

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/products")
def products():
    return render_template("products.html", products=models.get_all_products())


@app.route("/products/new", methods=["GET", "POST"])
def add_product():
    return render_template("add_product.html")
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "base.html").write_text(BASE, encoding="utf-8")
    (tmp_path / "templates" / "products.html").write_text(PRODUCTS, encoding="utf-8")
    (tmp_path / "templates" / "add_product.html").write_text(
        ADD_PRODUCT, encoding="utf-8"
    )
    (tmp_path / "templates" / "index.html").write_text(
        '{% extends "base.html" %}\n{% block content %}<p>Hi</p>{% endblock %}',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(APP_PY, encoding="utf-8")
    return tmp_path


# --- parsing one template ---------------------------------------------------


def test_a_child_template_declares_its_layout_and_partials():
    info = parse_template(PRODUCTS, "templates/products.html")
    assert info.extends == "base.html"
    assert info.includes == ("_macros.html",)
    assert info.blocks == ("content",)


def test_a_template_declares_the_endpoints_it_links_to():
    info = parse_template(BASE, "templates/base.html")
    assert "products" in info.endpoints
    assert info.assets == ("css/style.css", "js/app.js")


def test_only_a_form_action_counts_as_writing():
    """`url_for` in a nav link and `url_for` in a form action are different
    facts: one is a link, the other is where a write goes."""
    info = parse_template(ADD_PRODUCT, "templates/add_product.html")
    assert info.form_endpoints == ("add_product",)
    assert parse_template(BASE).form_endpoints == ()


def test_the_entity_hint_comes_from_the_loop_not_the_prose():
    info = parse_template(PRODUCTS)
    assert info.mentions("product") and info.mentions("products")


def test_a_nav_link_does_not_make_the_layout_a_reader_of_products():
    """THE false edge this module is built to avoid.

    `base.html` says `Products` in a link and `url_for('products')` in an href.
    Counting either would make the site layout a reader of every entity, and an
    amendment would then rewrite base.html to "show price for each product".
    """
    info = parse_template(BASE)
    assert not info.mentions("product")
    assert "products" not in info.identifiers  # it was inside a string literal


def test_jinja_keywords_and_filters_are_never_entity_hints():
    info = parse_template(
        "{% for x in items %}{{ x.name|title|default('n/a') }}{% endfor %}"
    )
    for word in ("for", "in", "endfor", "title", "default"):
        assert word not in info.identifiers


def test_unreadable_input_yields_no_edges():
    assert parse_template("").extends == ""
    assert parse_template("not a template at all").identifiers == ()


@pytest.mark.parametrize(
    "path,text,expected",
    [
        ("templates/base.html", BASE, True),
        ("templates/layout.html", "{% block content %}{% endblock %}", True),
        ("templates/products.html", PRODUCTS, False),
        # Shape, not just name: blocks with no `extends` is a layout.
        ("templates/shell.html", "{% block body %}x{% endblock %}", True),
    ],
)
def test_layout_detection(path, text, expected):
    assert is_layout(path, parse_template(text, path)) is expected


# --- the graph --------------------------------------------------------------


def test_the_graph_reads_the_project(project):
    graph = build_graph(project)
    assert set(graph.templates) == {
        "templates/base.html",
        "templates/products.html",
        "templates/add_product.html",
        "templates/index.html",
    }
    assert graph.routes


def test_children_and_parents_resolve_the_layout(project):
    graph = build_graph(project)
    assert graph.parents("templates/products.html") == ["templates/base.html"]
    assert "templates/products.html" in graph.children("templates/base.html")


def test_route_and_template_find_each_other(project):
    graph = build_graph(project)
    assert graph.template_for_route("/products") == "templates/products.html"
    assert graph.route_for_template("templates/products.html") == "/products"
    assert graph.view_for_template("templates/products.html") == "products"


def test_the_listing_page_is_found_without_page_reads(project):
    """The whole point: `Page.reads` said nothing, and the template still knows."""
    graph = build_graph(project)
    assert graph.templates_reading("product") == ["templates/products.html"]
    assert "templates/base.html" not in graph.templates_reading("product")


def test_a_page_whose_variable_is_generic_is_found_through_its_view(project):
    """`{{ item.title }}` names no entity — the view that renders it does."""
    (project / "templates" / "shop.html").write_text(
        '{% extends "base.html" %}{% block content %}'
        "{% for item in rows %}{{ item.title }}{% endfor %}{% endblock %}",
        encoding="utf-8",
    )
    (project / "app.py").write_text(
        APP_PY + '\n\n@app.route("/shop")\ndef shop():\n'
        '    return render_template("shop.html", rows=models.get_all_products())\n',
        encoding="utf-8",
    )
    graph = build_graph(project)
    assert "templates/shop.html" in graph.templates_reading("product")


def test_forms_writing_names_the_form_page(project):
    graph = build_graph(project)
    assert graph.forms_writing("add_product") == ["templates/add_product.html"]
    assert graph.forms_writing() == []


def test_a_project_with_no_templates_is_an_empty_graph(tmp_path):
    graph = build_graph(tmp_path)
    assert graph.templates == {} and graph.routes == ()
    assert graph.templates_reading("product") == []
    assert graph.children("templates/base.html") == []


def test_an_unreadable_template_contributes_nothing(project, monkeypatch):
    """No edges, never a wrong edge — `reconcile_with_disk`'s rule."""
    bad = project / "templates" / "broken.html"
    bad.write_bytes(b"\xff\xfe\x00binary")
    graph = build_graph(project)
    assert "templates/products.html" in graph.templates  # the rest still parsed


def test_view_bodies_splits_at_top_level_defs():
    bodies = view_bodies(APP_PY)
    assert "get_all_products" in bodies["products"]
    assert "get_all_products" not in bodies["index"]


# --- what impact.py does with it -------------------------------------------


def _spec_and_delta():
    entity = Entity(
        name="product",
        table="products",
        fields=(Field(name="title", type="TEXT"), Field(name="price", type="REAL")),
    )
    spec = ProjectSpec(
        entities=(entity,),
        # `reads` deliberately EMPTY — the measured real-world case.
        pages=(Page(route="/products", template="templates/products.html"),),
    )
    delta = SpecDelta(
        add_fields=(("product", Field(name="stock", type="INTEGER", added_in=2)),)
    )
    return spec, delta


def test_without_the_graph_the_listing_page_is_missed(project):
    """The before picture, pinned so the improvement is not imaginary."""
    spec, delta = _spec_and_delta()
    existing = {"app.py", "db.py", "models.py", "templates/products.html"}
    edits = impacted_files(spec, delta, existing)
    assert not [e for e in edits if e.filename == "templates/products.html"]


def test_with_the_graph_the_listing_page_is_updated(project):
    spec, delta = _spec_and_delta()
    existing = {"app.py", "db.py", "models.py", "templates/products.html"}
    edits = impacted_files(spec, delta, existing, graph=build_graph(project))
    hits = [e for e in edits if e.filename == "templates/products.html"]
    assert len(hits) == 1 and "stock" in hits[0].reason


def test_the_graph_never_proposes_editing_a_file_that_is_not_there(project):
    spec, delta = _spec_and_delta()
    edits = impacted_files(spec, delta, {"app.py"}, graph=build_graph(project))
    assert all(e.filename == "app.py" for e in edits)


def test_the_layout_is_never_proposed_for_a_field_change(project):
    spec, delta = _spec_and_delta()
    existing = {"app.py", "templates/products.html", "templates/base.html"}
    edits = impacted_files(spec, delta, existing, graph=build_graph(project))
    assert not [e for e in edits if e.filename == "templates/base.html"]


def test_a_broken_graph_costs_only_its_own_edges(project):
    """Best-effort, like everything else derived from disk."""

    class Exploding:
        def templates_reading(self, *names):
            raise RuntimeError("boom")

        def forms_writing(self, *names):
            raise RuntimeError("boom")

    spec, delta = _spec_and_delta()
    edits = impacted_files(spec, delta, {"app.py", "db.py"}, graph=Exploding())
    assert [e.filename for e in edits]  # the spec-derived edits still happened


# --- the symbol index ------------------------------------------------------


def test_a_template_is_indexed_as_edges_not_symbols(project):
    """`find_symbol("content")` returning every page would be worse than
    useless, so a template contributes no symbols at all."""
    result = extract_symbols(project / "templates" / "products.html")
    assert result.symbols == []
    assert result.template_deps == ["base.html", "_macros.html"]


def test_the_dependency_graph_reaches_into_the_templates(project):
    index = SymbolIndex(db_path=":memory:")
    for rel in (
        "app.py",
        "templates/base.html",
        "templates/products.html",
        "templates/add_product.html",
    ):
        index.index_file(project / rel, project_root=project)

    base = str(project / "templates" / "base.html")
    products = str(project / "templates" / "products.html")
    # A page depends on its layout...
    assert base in index.dependencies(products)
    # ...the layout knows its children...
    assert products in index.dependents(base)
    # ...and the route knows the page it serves.
    assert products in index.dependencies(str(project / "app.py"))
    index.close()


def test_a_dynamic_render_template_call_yields_no_edge(tmp_path):
    (tmp_path / "app.py").write_text(
        "from flask import render_template\n"
        "def show(name):\n"
        "    return render_template(name + '.html')\n",
        encoding="utf-8",
    )
    assert extract_symbols(tmp_path / "app.py").template_deps == []


def test_an_unresolvable_template_name_records_no_target(tmp_path):
    (tmp_path / "app.py").write_text(
        "from flask import render_template\n"
        "def show():\n"
        "    return render_template('nope.html')\n",
        encoding="utf-8",
    )
    index = SymbolIndex(db_path=":memory:")
    index.index_file(tmp_path / "app.py", project_root=tmp_path)
    assert index.dependencies(str(tmp_path / "app.py")) == []
    index.close()


# --- picking an edit target -------------------------------------------------


async def test_the_edit_target_can_come_from_the_templates(project, monkeypatch):
    """ "put the price on the product listing" names no page, no route and no
    file — but exactly one template displays a product."""
    monkeypatch.chdir(project)
    a = AgentCore(session_id="pytest_w8_target")
    a._spec = ProjectSpec(
        entities=(Entity(name="product", table="products"),),
        pages=(Page(route="/products", template=""),),
    )
    assert (
        a._resolve_target_from_spec("put the price on the product listing")
        == "templates/products.html"
    )


async def test_two_matching_templates_still_decline(project, monkeypatch):
    monkeypatch.chdir(project)
    (project / "templates" / "featured.html").write_text(
        '{% extends "base.html" %}{% block content %}'
        "{% for product in products %}{{ product.title }}{% endfor %}{% endblock %}",
        encoding="utf-8",
    )
    a = AgentCore(session_id="pytest_w8_ambiguous")
    a._spec = ProjectSpec(entities=(Entity(name="product", table="products"),))
    assert a._resolve_target_from_spec("show the price on the product page") is None


async def test_a_page_that_matches_by_name_still_wins(project, monkeypatch):
    """The graph is a FALLBACK. A message that names the page keeps resolving
    the way it did before W8."""
    monkeypatch.chdir(project)
    a = AgentCore(session_id="pytest_w8_precedence")
    a._spec = ProjectSpec(
        entities=(Entity(name="product", table="products"),),
        pages=(
            Page(
                route="/products",
                template="templates/add_product.html",
                nav_label="Add product",
            ),
        ),
    )
    assert (
        a._resolve_target_from_spec("update the add product page")
        == "templates/add_product.html"
    )


# ===========================================================================
# The same graph off an EJS project (Phase N4, docs/node-stack-plan.md)
# ===========================================================================
#
# Same graph SHAPE, different parser. The rules that matter are the ones above
# restated: identifiers come from EJS expressions with the strings stripped
# first, and the layout is never a reader of anything. Get that wrong and an
# amendment rewrites the site shell to "show price for each product".

LAYOUT_EJS = """<!doctype html>
<html><head><title><%= projectName %></title>
<link rel="stylesheet" href="/css/style.css">
</head><body>
<nav class="site-nav"><a href="/">Home</a> <a href="/products">Products</a></nav>
<main><%- ui.flash_messages(messages) %><%- body %></main>
<script src="/js/app.js"></script>
</body></html>
"""

PRODUCTS_EJS = """<%- include("_filters") %>
<h1>Products</h1>
<% products.forEach(function (product) { %>
  <a href="/products/<%= product.id %>"><%= product.title %></a> — <%= product.price %>
<% }); %>
"""

ADD_PRODUCT_EJS = """<form method="post" action="/products/new" enctype="multipart/form-data">
  <%- ui.field("title", "Title") %>
  <button type="submit">Save</button>
</form>
"""

SERVER_JS = """
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


@pytest.fixture
def node_project(tmp_path):
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "layout.ejs").write_text(LAYOUT_EJS, encoding="utf-8")
    (tmp_path / "views" / "products.ejs").write_text(PRODUCTS_EJS, encoding="utf-8")
    (tmp_path / "views" / "add_product.ejs").write_text(
        ADD_PRODUCT_EJS, encoding="utf-8"
    )
    (tmp_path / "views" / "_filters.ejs").write_text("<p>filters</p>", encoding="utf-8")
    (tmp_path / "views" / "index.ejs").write_text("<h1>Hi</h1>", encoding="utf-8")
    (tmp_path / "server.js").write_text(SERVER_JS, encoding="utf-8")
    return tmp_path


# --- parsing one EJS view ---------------------------------------------------


def test_an_ejs_view_declares_its_partials_and_its_links():
    info = parse_ejs_template(PRODUCTS_EJS, "views/products.ejs")
    assert info.includes == ("_filters",)
    # On a path-routed stack the "endpoint" IS the path.
    assert "/products/<%= product.id %>" in info.endpoints


def test_an_ejs_view_has_no_extends_because_the_layout_is_implicit():
    """`express-ejs-layouts` wraps every render, so a correctly shaped view
    carries no marker at all — unlike Jinja's `{% extends %}`."""
    info = parse_ejs_template(PRODUCTS_EJS, "views/products.ejs")
    assert info.extends == "" and info.blocks == ()


def test_only_a_form_action_counts_as_writing_on_ejs_too():
    assert parse_ejs_template(ADD_PRODUCT_EJS).form_endpoints == ("/products/new",)
    assert parse_ejs_template(LAYOUT_EJS).form_endpoints == ()


def test_the_ejs_entity_hint_comes_from_the_expression_not_the_prose():
    info = parse_ejs_template(PRODUCTS_EJS)
    assert info.mentions("product") and info.mentions("products")


def test_a_nav_link_does_not_make_layout_ejs_a_reader_of_products():
    """THE false edge, one stack over.

    `layout.ejs` says "Products" in a link and `/products` in an href. Counting
    either would make the site shell a reader of every entity, and an amendment
    would rewrite the navigation to "show price for each product".
    """
    info = parse_ejs_template(LAYOUT_EJS, "views/layout.ejs")
    assert not info.mentions("product")
    assert "products" not in info.identifiers  # it was inside a string literal


def test_the_real_scaffold_layout_reads_no_entity():
    """Not a synthetic layout — the file the Node scaffold actually ships, which
    is the one every generated project gets."""
    from app.agent.scaffold import scaffold_dir

    source = (scaffold_dir("node") / "views" / "layout.ejs").read_text(encoding="utf-8")
    info = parse_ejs_template(source, "views/layout.ejs")
    for entity in ("product", "order", "user", "post"):
        assert not info.mentions(entity), entity


def test_javascript_builtins_are_never_entity_hints():
    info = parse_ejs_template(
        "<% rows.forEach(function (row) { %><%= JSON.stringify(row) %><% }); %>"
    )
    for word in ("forEach", "function", "JSON", "stringify", "row", "rows"):
        assert word not in info.identifiers, word


def test_unreadable_ejs_yields_no_edges():
    assert parse_ejs_template("").identifiers == ()
    assert parse_ejs_template("just some prose").identifiers == ()


# --- the graph, built with the Node adapter's arguments ---------------------


def test_build_graph_with_no_keywords_is_exactly_the_flask_behaviour(project):
    """The back-compat guarantee. Every keyword defaults to the Flask layout, so
    the existing callers and every existing test are unchanged — N4 added
    arguments, it did not change a default."""
    default = build_graph(project)
    spelled_out = build_graph(project, "app.py", template_dir="templates")
    assert set(default.templates) == set(spelled_out.templates)
    assert default.routes == spelled_out.routes
    assert default.views == spelled_out.views
    # The Flask shape, asserted directly rather than only against itself.
    assert "templates/products.html" in default.templates
    assert default.views and "products" in default.views


def test_passing_a_parser_is_what_turns_the_python_view_bodies_off(project):
    """`views` is read only when `parser is None`. `view_bodies` is Python-shaped
    (`def name():` up to the next decorator), so running it over another
    language's entry file would produce garbage edges, not missing ones."""
    assert build_graph(project).views
    assert build_graph(project, parser=parse_template).views == {}


def test_the_graph_reads_an_ejs_project(node_project):
    graph = NODE.build_template_graph(node_project)
    assert set(graph.templates) == {
        "views/layout.ejs",
        "views/products.ejs",
        "views/add_product.ejs",
        "views/_filters.ejs",
        "views/index.ejs",
    }
    assert ("GET", "/products", "products", "products") in graph.routes


def test_the_ejs_listing_view_is_found_without_page_reads(node_project):
    """The whole point, on the stack where `Page.reads` is emptiest."""
    graph = NODE.build_template_graph(node_project)
    assert graph.templates_reading("product") == ["views/products.ejs"]
    assert "views/layout.ejs" not in graph.templates_reading("product")


def test_a_partial_is_a_parent_of_the_view_that_includes_it(node_project):
    graph = NODE.build_template_graph(node_project)
    assert graph.parents("views/products.ejs") == ["views/_filters.ejs"]
    assert graph.children("views/_filters.ejs") == ["views/products.ejs"]


def test_forms_writing_names_the_ejs_form_view(node_project):
    """The endpoint is a PATH here, not a view name."""
    graph = NODE.build_template_graph(node_project)
    assert graph.forms_writing("/products/new") == ["views/add_product.ejs"]


def test_a_node_graph_has_no_view_bodies(node_project):
    """`view_bodies` is Python-shaped (`def name():` up to the next decorator)
    and there is no equivalent for an anonymous Express arrow function yet. So
    the second entity signal is simply absent here — never a wrong one."""
    assert NODE.build_template_graph(node_project).views == {}


def test_an_express_route_resolves_to_its_view_file(node_project):
    """Express names a view WITHOUT its extension (`res.render("products")`),
    where Jinja always writes the full filename. The stem fallback is reached
    only after every name match has come back empty, so it can turn a "" into a
    hit and never one answer into a different one."""
    graph = NODE.build_template_graph(node_project)
    assert graph.template_for_route("/products") == "views/products.ejs"
    assert graph.route_for_template("views/products.ejs") == "/products"


def test_an_ambiguous_extensionless_name_still_resolves_to_nothing(node_project):
    """Two candidates means the reference was ambiguous, and a wrong edge is an
    instruction to rewrite a file that was fine."""
    (node_project / "views" / "sub").mkdir()
    (node_project / "views" / "sub" / "products.ejs").write_text("x", encoding="utf-8")
    graph = NODE.build_template_graph(node_project)
    assert graph.template_for_route("/products") == ""


def test_an_unreadable_ejs_view_costs_only_its_own_edges(node_project):
    (node_project / "views" / "broken.ejs").write_bytes(b"\xff\xfe\x00binary")
    graph = NODE.build_template_graph(node_project)
    assert "views/products.ejs" in graph.templates  # the rest still parsed


def test_a_node_project_with_no_views_is_an_empty_graph(tmp_path):
    graph = NODE.build_template_graph(tmp_path)
    assert graph.templates == {} and graph.routes == ()
    assert graph.templates_reading("product") == []


def test_each_adapter_builds_the_graph_for_its_own_layout(project, tmp_path_factory):
    """`core._template_graph` dispatches through the adapter, so pointing one
    stack's parser at the other's tree must yield nothing rather than noise.

    The two trees need separate roots — `project` and `node_project` both build
    under `tmp_path`, which is one directory per test.
    """
    other = tmp_path_factory.mktemp("node_only")
    (other / "views").mkdir()
    (other / "views" / "products.ejs").write_text(PRODUCTS_EJS, encoding="utf-8")
    (other / "server.js").write_text(SERVER_JS, encoding="utf-8")

    assert FLASK.build_template_graph(project).templates
    assert FLASK.build_template_graph(other).templates == {}
    assert NODE.build_template_graph(other).templates
    assert NODE.build_template_graph(project).templates == {}
