"""Flask endpoint + form-method validation (Phase W2, docs/web-quality-plan.md).

Fully offline and deterministic — no LLM, no browser, no server.

The failure being closed: `{{ url_for('product') }}` against a view named
`products` is a Jinja BuildError, i.e. a 500 on that page, from a file that
parses, renders in isolation and passes every check that existed. Nothing looked
at it — `references.py` deliberately skips `url_for` (it cannot tell a route from
a file path), the syntax check only balances tags, and the functional probe sees
the 500 without ever being able to name the endpoint as the cause.
"""

from types import SimpleNamespace

import pytest

from app.agent.core import AgentCore
from app.agent.verify import (
    endpoints_referenced,
    fix_endpoint_names,
    form_method_mismatches,
    unresolved_endpoints,
)


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


APP_PY = """
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/products")
def products():
    return render_template("products.html")


@app.route("/products/new", methods=["GET", "POST"])
def add_product():
    return render_template("add_product.html")


@app.route("/checkout")
def checkout():
    return render_template("checkout.html")
"""

KNOWN = {"index", "products", "add_product", "checkout"}


# --- finding what a template asks for ---------------------------------------


def test_endpoints_referenced_collects_and_dedupes():
    html = (
        "<a href=\"{{ url_for('products') }}\">Shop</a>"
        "<a href=\"{{ url_for('products') }}\">Again</a>"
        "<a href='{{ url_for(\"checkout\") }}'>Pay</a>"
    )
    assert endpoints_referenced(html) == ["products", "checkout"]


def test_static_is_never_reported_missing():
    """Flask registers `static` itself; it is never defined in app.py."""
    html = "<link href=\"{{ url_for('static', filename='css/style.css') }}\">"
    assert unresolved_endpoints(html, KNOWN) == []


def test_unresolved_endpoints_reports_the_unknown_one():
    html = "<a href=\"{{ url_for('basket') }}\">Basket</a>"
    assert unresolved_endpoints(html, KNOWN) == ["basket"]


# --- repairing a near miss --------------------------------------------------


@pytest.mark.parametrize("written", ["product", "Products", "productS"])
def test_a_singular_or_miscased_name_is_repointed(written):
    html = f"<a href=\"{{{{ url_for('{written}') }}}}\">Shop</a>"
    fixed, fixes = fix_endpoint_names(html, KNOWN)
    assert fixes == [(written, "products")]
    assert "url_for('products')" in fixed


def test_an_underscore_slip_is_repointed():
    html = '<form action="{{ url_for(\'addproduct\') }}" method="post"></form>'
    fixed, fixes = fix_endpoint_names(html, KNOWN)
    assert fixes == [("addproduct", "add_product")]
    assert "url_for('add_product')" in fixed


def test_quote_style_is_preserved():
    html = "<a href='{{ url_for(\"product\") }}'>Shop</a>"
    fixed, _ = fix_endpoint_names(html, KNOWN)
    assert 'url_for("products")' in fixed


def test_a_genuinely_different_name_is_never_guessed():
    """The rule that keeps this pass safe.

    `edit_product` and `add_product` are different handlers, and silently
    sending a form to the wrong one is worse than the 500 it would replace. It
    is reported instead.
    """
    html = '<form action="{{ url_for(\'edit_product\') }}" method="post"></form>'
    fixed, fixes = fix_endpoint_names(html, KNOWN)
    assert fixes == []
    assert fixed == html
    assert unresolved_endpoints(fixed, KNOWN) == ["edit_product"]


def test_two_candidates_means_no_rewrite():
    """Same strictness as `_resolve_target_from_spec`: ambiguity is not a fix."""
    html = "<a href=\"{{ url_for('item') }}\">x</a>"
    fixed, fixes = fix_endpoint_names(html, {"items", "item_", "other"})
    assert fixes == []
    assert fixed == html


def test_a_clean_template_is_untouched():
    html = "<a href=\"{{ url_for('products') }}\">Shop</a>"
    fixed, fixes = fix_endpoint_names(html, KNOWN)
    assert (fixed, fixes) == (html, [])


# --- form method vs route ---------------------------------------------------

ROUTES = [
    ("GET", "/", "index", "index.html"),
    ("GET", "/products", "products", "products.html"),
    ("GET", "/products/new", "add_product", "add_product.html"),
    ("POST", "/products/new", "add_product", "add_product.html"),
]


def test_posting_to_a_get_only_route_is_reported():
    """A 405 nothing else can see: the HTML is valid, the page renders, the
    route exists, and the functional probe posts to spec routes, not to the
    action this form actually names."""
    html = '<form method="post" action="{{ url_for(\'products\') }}"></form>'
    (issue,) = form_method_mismatches(html, ROUTES)
    assert "products" in issue and "GET" in issue


def test_posting_to_a_route_that_accepts_post_is_fine():
    html = '<form method="post" action="{{ url_for(\'add_product\') }}"></form>'
    assert form_method_mismatches(html, ROUTES) == []


def test_a_form_with_no_action_is_not_judged():
    """It posts to its own URL, and which route that is cannot be known from
    the template alone. Guessing here would produce false failures, and a false
    failure sends the repair loop to rewrite working code."""
    html = '<form method="post"><input name="q"></form>'
    assert form_method_mismatches(html, ROUTES) == []


def test_an_unknown_endpoint_is_left_to_the_other_check():
    html = '<form method="post" action="{{ url_for(\'nope\') }}"></form>'
    assert form_method_mismatches(html, ROUTES) == []


def test_a_get_form_to_a_post_only_route_is_reported():
    routes = [("POST", "/subscribe", "subscribe", "")]
    html = "<form action=\"{{ url_for('subscribe') }}\"></form>"
    (issue,) = form_method_mismatches(html, routes)
    assert "subscribe" in issue


# --- the core seam ----------------------------------------------------------


async def test_verify_and_repair_repoints_a_near_miss(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text(APP_PY, encoding="utf-8")
    a = AgentCore(session_id="pytest_endpoints_fix")
    a._llm_edit = ScriptedLLM(["PASS"])

    page = tmp_path / "shop.html"
    page.write_text(
        "<!doctype html><html><body>"
        "<a href=\"{{ url_for('product') }}\">Shop</a>"
        "</body></html>",
        encoding="utf-8",
    )

    note, _ = await a._verify_and_repair(page, "shop.html")

    assert "repointed" in note
    assert "url_for('products')" in page.read_text(encoding="utf-8")


async def test_verify_and_repair_reports_an_unfixable_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text(APP_PY, encoding="utf-8")
    a = AgentCore(session_id="pytest_endpoints_report")
    a._llm_edit = ScriptedLLM(["PASS"])

    page = tmp_path / "cart.html"
    page.write_text(
        "<!doctype html><html><body>"
        "<a href=\"{{ url_for('view_basket') }}\">Basket</a>"
        "</body></html>",
        encoding="utf-8",
    )

    note, _ = await a._verify_and_repair(page, "cart.html")

    # Reported, never invented — writing the route is the coverage check's job.
    assert "may not meet" in note and "view_basket" in note
    assert "view_basket" in page.read_text(encoding="utf-8")


async def test_no_app_py_means_no_endpoint_check(tmp_path, monkeypatch):
    """A static build has no Flask routes; every url_for-looking string in it
    must not become a complaint."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_endpoints_static")
    a._llm_edit = ScriptedLLM(["PASS"])

    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body><h1>Hi</h1></body></html>", encoding="utf-8"
    )
    note, _ = await a._verify_and_repair(page, "index.html")
    assert "may not meet" not in note


async def test_a_css_file_is_not_checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text(APP_PY, encoding="utf-8")
    a = AgentCore(session_id="pytest_endpoints_css")
    a._llm_edit = ScriptedLLM(["PASS"])

    sheet = tmp_path / "style.css"
    sheet.write_text("body { color: red; }", encoding="utf-8")
    note, _ = await a._verify_and_repair(sheet, "style.css")
    assert "url_for" not in note
