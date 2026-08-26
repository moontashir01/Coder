"""Click → the exact template source behind it (`app/agent/pointer.py`).

Entirely offline: no browser, no server, no model. That is the point of the
split — the mapping is the part that has to be right, and it is pure text in,
target-or-refusal out.

The rule under test everywhere: **exactly one candidate, or decline**. Editing
the wrong element is silent and reads as the model ignoring the request; a
refusal costs one sentence and a second click.
"""

from types import SimpleNamespace

import pytest

from app.agent.pointer import (
    Decline,
    Element,
    _route_matches,
    locate_in_template,
    resolve_element,
    template_for_path,
)
from app.agent.stacks import get_adapter

PAGE = """{% extends "base.html" %}
{% block content %}
<h1 class="page-title">Our Products</h1>
<p id="intro">Everything we sell.</p>
<div class="grid">
  {% for p in products %}
  <article class="card">
    <h3>{{ p.name }}</h3>
    <span class="price">{{ p.price }}</span>
  </article>
  {% endfor %}
</div>
<img src="/static/logo.png" id="logo">
{% endblock %}
"""

LAYOUT = """<!doctype html>
<html><body>
<nav class="site-nav"><a href="/">Home</a><a href="/products">Products</a></nav>
{% block content %}{% endblock %}
</body></html>
"""


def _graph(routes):
    return SimpleNamespace(routes=tuple(routes), parents=lambda t: [])


# ---------------------------------------------------------------------------
# URL → template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route,url,expected",
    [
        ("/products", "/products", True),
        ("/products", "/products/", True),
        ("/products/<int:id>", "/products/12", True),
        ("/products/:id", "/products/12", True),
        ("/products/<int:id>", "/products", False),
        ("/products", "/orders", False),
        ("/", "/", True),
    ],
)
def test_route_matching_handles_both_stacks_parameters(route, url, expected):
    assert _route_matches(route, url) is expected


def test_a_url_resolves_to_the_template_that_rendered_it():
    graph = _graph([("GET", "/products", "products", "templates/products.html")])
    assert template_for_path(graph, "/products") == "templates/products.html"


def test_two_routes_agreeing_on_the_template_are_one_answer():
    """`/products/:id` also matches `/products/new` — the union rule."""
    graph = _graph(
        [
            ("GET", "/products/<int:id>", "detail", "templates/product.html"),
            ("GET", "/products/new", "new", "templates/product.html"),
        ]
    )
    assert template_for_path(graph, "/products/new") == "templates/product.html"


def test_two_routes_disagreeing_is_a_refusal():
    graph = _graph(
        [
            ("GET", "/products/<int:id>", "detail", "templates/detail.html"),
            ("GET", "/products/new", "new", "templates/new.html"),
        ]
    )
    out = template_for_path(graph, "/products/new")
    assert isinstance(out, Decline)
    assert "more than one template" in out.reason


def test_an_unrouted_url_is_a_refusal_naming_the_url():
    out = template_for_path(_graph([]), "/nowhere")
    assert isinstance(out, Decline)
    assert "/nowhere" in out.reason


# ---------------------------------------------------------------------------
# Element → span
# ---------------------------------------------------------------------------


def test_an_id_pins_the_element():
    found = locate_in_template(PAGE, Element(tag="p", element_id="intro"))
    search, how, line = found
    assert search == '<p id="intro">Everything we sell.</p>'
    assert how == "id"
    assert line == 4


def test_a_unique_class_pins_the_element():
    found = locate_in_template(PAGE, Element(tag="h1", classes=("page-title",)))
    search, how, _line = found
    assert search == '<h1 class="page-title">Our Products</h1>'
    assert how == "class"


def test_visible_text_pins_the_element_when_nothing_else_can():
    found = locate_in_template(PAGE, Element(tag="h1", text="Our Products"))
    search, how, _line = found
    assert "Our Products" in search
    assert how == "text"


def test_the_span_carries_template_expressions_through_verbatim():
    """The SEARCH half must be the REAL source, not the masked copy."""
    found = locate_in_template(PAGE, Element(tag="span", classes=("price",)))
    search, _how, _line = found
    assert search == '<span class="price">{{ p.price }}</span>'


def test_a_void_element_needs_no_closing_tag():
    found = locate_in_template(PAGE, Element(tag="img", element_id="logo"))
    search, _how, _line = found
    assert search == '<img src="/static/logo.png" id="logo">'


def test_a_repeated_class_with_nothing_to_tell_them_apart_declines():
    source = '<div class="card">one</div>\n<div class="card">two</div>\n'
    out = locate_in_template(source, Element(tag="div", classes=("card",)))
    assert isinstance(out, Decline)
    assert "unique" in out.reason


def test_a_click_on_the_document_shell_declines():
    """`<body>` is not something anybody means to replace."""
    out = locate_in_template(PAGE, Element(tag="body"))
    assert isinstance(out, Decline)


def test_an_unbalanced_template_declines_rather_than_guessing_the_end():
    source = '<div class="card">never closed\n'
    out = locate_in_template(source, Element(tag="div", classes=("card",)))
    assert isinstance(out, Decline)


# ---------------------------------------------------------------------------
# The whole mapping, on disk
# ---------------------------------------------------------------------------


def _project(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "products.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "templates" / "base.html").write_text(LAYOUT, encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from flask import Flask, render_template\n"
        "app = Flask(__name__)\n\n"
        '@app.route("/products")\n'
        "def products():\n"
        '    return render_template("products.html")\n',
        encoding="utf-8",
    )
    return tmp_path


def test_a_click_resolves_to_file_span_and_block(tmp_path):
    root = _project(tmp_path)
    target = resolve_element(
        root,
        get_adapter("flask"),
        Element(tag="p", element_id="intro", url_path="/products"),
    )
    assert not isinstance(target, Decline)
    assert target.path == "templates/products.html"
    assert target.search == '<p id="intro">Everything we sell.</p>'
    assert target.region == "content"  # W3 can scope the edit to the block
    assert target.how == "id"


def test_a_click_on_the_nav_resolves_to_the_LAYOUT_not_the_page(tmp_path):
    """A child template inherits its shell; the nav lives in base.html."""
    root = _project(tmp_path)
    target = resolve_element(
        root,
        get_adapter("flask"),
        Element(tag="nav", classes=("site-nav",), url_path="/products"),
    )
    assert not isinstance(target, Decline)
    assert target.path == "templates/base.html"
    assert "site-nav" in target.search
    assert "layout" in target.how


def test_an_unresolvable_click_declines_without_naming_a_file(tmp_path):
    root = _project(tmp_path)
    out = resolve_element(
        root,
        get_adapter("flask"),
        Element(tag="section", classes=("nope",), url_path="/products"),
    )
    assert isinstance(out, Decline)
    assert "Nothing was changed" in out.reason


def test_a_project_with_no_routes_declines(tmp_path):
    (tmp_path / "templates").mkdir()
    out = resolve_element(
        tmp_path, get_adapter("flask"), Element(tag="h1", url_path="/products")
    )
    assert isinstance(out, Decline)


# ---------------------------------------------------------------------------
# The other stack
# ---------------------------------------------------------------------------


EJS_VIEW = """<h1 class="page-title">Our Items</h1>
<ul>
  <% items.forEach(function (i) { %>
  <li class="item"><%= i.name %></li>
  <% }); %>
</ul>
"""


def test_the_same_mapping_works_on_an_ejs_view(tmp_path):
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "items.ejs").write_text(EJS_VIEW, encoding="utf-8")
    (tmp_path / "server.js").write_text(
        'const express = require("express");\n'
        "const app = express();\n"
        'app.get("/items", (req, res) => { res.render("items"); });\n',
        encoding="utf-8",
    )
    target = resolve_element(
        tmp_path,
        get_adapter("node"),
        Element(tag="h1", classes=("page-title",), url_path="/items"),
    )
    assert not isinstance(target, Decline)
    assert target.path == "views/items.ejs"
    assert target.search == '<h1 class="page-title">Our Items</h1>'
    assert target.region == ""  # EJS has no blocks to scope to


# ---------------------------------------------------------------------------
# The pinned edit
#
# The whole reason the mapping is worth having: the SEARCH half comes from the
# file, so the model writes only the replacement and the edit cannot miss.
# ---------------------------------------------------------------------------


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(m.content) for m in messages))
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


async def test_a_pointed_edit_changes_only_the_clicked_fragment(tmp_path, monkeypatch):
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    root = _project(tmp_path)
    page = root / "templates" / "products.html"
    before = page.read_text(encoding="utf-8")

    agent = AgentCore(session_id="pytest_point")
    agent._llm_edit = ScriptedLLM(['<p id="intro">Everything, half price.</p>'])
    answer, trace = await agent.edit_pointed_element(
        Element(tag="p", element_id="intro", url_path="/products"),
        "say everything is half price",
        root,
    )

    after = page.read_text(encoding="utf-8")
    assert "Everything, half price." in after
    # Every other line survives byte-for-byte — the guarantee the pinned span
    # gives, and the one a whole-file rewrite cannot.
    assert (
        after.replace(
            '<p id="intro">Everything, half price.</p>',
            '<p id="intro">Everything we sell.</p>',
        )
        == before
    )
    assert "Edited" in answer
    assert "templates/products.html" in answer or "products.html" in answer
    assert trace and trace[0]["result"]["success"] is True


async def test_the_model_is_never_asked_for_a_search_block(tmp_path, monkeypatch):
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    root = _project(tmp_path)
    agent = AgentCore(session_id="pytest_point_prompt")
    agent._llm_edit = ScriptedLLM(['<p id="intro">New copy.</p>'])
    await agent.edit_pointed_element(
        Element(tag="p", element_id="intro", url_path="/products"), "new copy", root
    )
    prompt = agent._llm_edit.prompts[0]
    assert "FRAGMENT" in prompt
    assert "Output the SEARCH/REPLACE block(s) now" not in prompt
    # The fragment is what it was shown; the rest of the page is not.
    assert '<p id="intro">Everything we sell.</p>' in prompt
    assert "Our Products" not in prompt


async def test_an_answer_in_the_other_format_is_still_understood(tmp_path, monkeypatch):
    """A 7B wraps the answer in the format it saw. Recover it, don't refuse it."""
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    root = _project(tmp_path)
    agent = AgentCore(session_id="pytest_point_wrapped")
    agent._llm_edit = ScriptedLLM(
        [
            "<<<<<<< SEARCH\n"
            '<p id="intro">Everything we sell.</p>\n'
            "=======\n"
            '<p id="intro">Wrapped answer.</p>\n'
            ">>>>>>> REPLACE"
        ]
    )
    await agent.edit_pointed_element(
        Element(tag="p", element_id="intro", url_path="/products"), "change it", root
    )
    assert "Wrapped answer." in (root / "templates" / "products.html").read_text(
        "utf-8"
    )


async def test_a_stale_click_refuses_rather_than_editing_the_wrong_lines(
    tmp_path, monkeypatch
):
    from app.agent import pointer
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    root = _project(tmp_path)
    page = root / "templates" / "products.html"

    # The mapping resolves against text that is no longer in the file.
    monkeypatch.setattr(
        pointer,
        "resolve_element",
        lambda *_a, **_k: pointer.PointerTarget(
            "templates/products.html", "<p>gone since the click</p>", "id"
        ),
    )
    agent = AgentCore(session_id="pytest_point_stale")
    agent._llm_edit = ScriptedLLM(["should never be called"])
    before = page.read_text(encoding="utf-8")
    answer, trace = await agent.edit_pointed_element(
        Element(tag="p", url_path="/products"), "change it", root
    )
    assert page.read_text(encoding="utf-8") == before
    assert "changed since that click" in answer
    assert trace == []
    assert agent._llm_edit.calls == 0


async def test_a_declined_click_writes_nothing(tmp_path, monkeypatch):
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    root = _project(tmp_path)
    before = (root / "templates" / "products.html").read_text(encoding="utf-8")
    agent = AgentCore(session_id="pytest_point_decline")
    agent._llm_edit = ScriptedLLM(["never"])
    answer, trace = await agent.edit_pointed_element(
        Element(tag="section", classes=("nope",), url_path="/products"), "x", root
    )
    assert (root / "templates" / "products.html").read_text("utf-8") == before
    assert trace == []
    assert agent._llm_edit.calls == 0
    assert "Nothing was changed" in answer


def test_the_overlay_is_valid_javascript(tmp_path):
    """A syntax error here fails SILENTLY and in the worst direction.

    The overlay is injected with `add_init_script`, which reports nothing when
    it throws — so `__coderPick` would never be called, and `/point` would
    simply sit there until it timed out with "nothing was clicked". `node
    --check` is the cheapest guard against shipping a picker that can never
    pick, and it is `pageaudit.py`'s rule applied to the one script the user
    actually interacts with. Skipped where node is absent.
    """
    import shutil
    import subprocess

    from app.agent.pointer import OVERLAY_SCRIPT

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    path = tmp_path / "overlay.js"
    path.write_text(OVERLAY_SCRIPT, encoding="utf-8")
    proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_capture_click_refuses_a_url_that_is_not_local():
    """This window renders a page and runs its JavaScript — `browser.py`'s rule,
    and it matters more here because the window is interactive."""
    from app.agent.pointer import capture_click

    out = capture_click("https://example.com/", timeout=0.1)
    assert isinstance(out, Decline)
    assert "localhost" in out.reason or "install" in out.reason.lower()


# ---------------------------------------------------------------------------
# Against a real browser and a real server
#
# The mapping above is pure and needs neither. What these two add is the half
# that cannot be reasoned about: does the overlay actually fire, does the
# payload carry the fields the mapping reads, and does a click on a link get
# swallowed instead of navigating away from the page being pointed at.
# ---------------------------------------------------------------------------


def _serve(html: str, path: str):
    """A one-page HTTP server on a free port. Returns (url, shutdown)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    body = html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib's spelling
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}{path}", server.shutdown


RENDERED = """<!doctype html>
<html><body>
<nav class="site-nav"><a href="/">Home</a><a href="/products">Products</a></nav>
<h1 class="page-title">Our Products</h1>
<p id="intro">Everything we sell.</p>
</body></html>
"""


def test_a_real_click_produces_a_payload_the_mapping_can_use(tmp_path):
    """End to end minus the human: real Chromium, real HTTP, real overlay."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from app.agent.pointer import install_picker

    root = _project(tmp_path)
    url, shutdown = _serve(RENDERED, "/products")
    picked: dict = {}
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            page = browser.new_page()
            install_picker(page, picked)
            page.goto(url, wait_until="domcontentloaded")
            page.click("#intro")
            page.wait_for_timeout(200)
            browser.close()
    finally:
        shutdown()

    assert picked, "the overlay never called __coderPick"
    element = Element.from_payload(picked)
    assert element.tag == "p"
    assert element.element_id == "intro"
    assert element.text == "Everything we sell."
    assert element.url_path == "/products"

    # And the payload really does resolve to the source behind it.
    target = resolve_element(root, get_adapter("flask"), element)
    assert not isinstance(target, Decline)
    assert target.path == "templates/products.html"
    assert target.search == '<p id="intro">Everything we sell.</p>'


def test_clicking_a_link_reports_it_instead_of_navigating(tmp_path):
    """Two failures in one, and the second was found BY this test.

    A nav link must not carry the person away from the page they are picking
    on — and the click must reach the link at all. The instruction bar first
    shipped at the top of the page with default pointer events, so Chromium
    reported it "intercepts pointer events" and the nav, the header and every
    other likely target were unclickable. `page.click` fails outright when
    that regresses, which is why this test is the guard for both.
    """
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from app.agent.pointer import install_picker

    url, shutdown = _serve(RENDERED, "/products")
    picked: dict = {}
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            page = browser.new_page()
            install_picker(page, picked)
            page.goto(url, wait_until="domcontentloaded")
            page.click("nav a[href='/']")
            page.wait_for_timeout(200)
            where = page.url
            browser.close()
    finally:
        shutdown()

    assert picked.get("tag") == "a"
    assert where.endswith("/products"), f"the click navigated to {where}"
