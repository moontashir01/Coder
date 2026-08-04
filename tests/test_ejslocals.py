"""Names an EJS view uses that its route never passes.

Found by running a generated app, which is the only way it could be found: EJS
compiles to `with (locals)`, so a free identifier is a ReferenceError at render
time and every byte-reading check is green. On the OpenBazaar build all five
listing pages answered 500 on `empty_state is not defined` — the prompt block
lists `table(rows, columns, empty)` and `empty_state(message)` side by side, and
the model passed the second one's NAME as the first one's argument.
"""

from pathlib import Path

import pytest

from app.agent import core as core_module
from app.agent.ejslocals import free_identifiers, render_locals, repair_view_locals
from app.agent.stacks.flask_adapter import FLASK
from app.agent.stacks.node_adapter import NODE

SERVER_JS = """\
app.get("/orders", async (req, res) => {
  const orders = await models.listOrders();
  res.render("orders", { orders });
});

app.get("/orders/new", (req, res) => {
  res.render("new_order");
});

app.get("/orders/:id", async (req, res) => {
  res.render("order_detail", { order: await models.getOrder(req.params.id), total: 1 });
});
"""

BROKEN_VIEW = (
    '<%- ui.page_header("Orders", "/orders/new", "Add Order") %>\n'
    '<%- ui.table(orders, ["id", "final_amount"], empty_state) %>\n'
)


# ---------------------------------------------------------------------------
# What the routes hand each view
# ---------------------------------------------------------------------------


def test_render_locals_are_read_per_view():
    assert render_locals(SERVER_JS) == {
        "orders": {"orders"},
        "new_order": set(),
        "order_detail": {"order", "total"},
    }


def test_locals_are_unioned_across_routes_rendering_the_same_view():
    """Either route may supply the name, so the view may legitimately use it."""
    source = (
        'app.get("/a", (req, res) => res.render("page", { one }));\n'
        'app.get("/b", (req, res) => res.render("page", { two }));\n'
    )
    assert render_locals(source)["page"] == {"one", "two"}


# ---------------------------------------------------------------------------
# Finding the free name
# ---------------------------------------------------------------------------


def test_the_undefined_name_is_found():
    assert free_identifiers(BROKEN_VIEW, {"orders"}) == ["empty_state"]


def test_names_the_route_passes_are_not_flagged():
    assert "orders" not in free_identifiers(BROKEN_VIEW, {"orders"})


def test_the_scaffold_s_own_bindings_are_not_flagged():
    view = "<%- ui.badge(projectName) %>\n"
    assert free_identifiers(view, set()) == []


def test_a_name_the_template_declares_itself_is_not_flagged():
    view = "<% for (const row of rows) { %><%= row.id %><% } %>\n"
    assert free_identifiers(view, {"rows"}) == []


def test_an_object_key_is_not_a_reference():
    view = "<%- ui.card({ title: 'x' }.title) %>\n"
    assert "title" not in free_identifiers(view, set())


def test_a_typeof_guarded_name_is_not_flagged():
    """`typeof x` is the one expression that does not throw on an undeclared
    name, and it is how the scaffold's own layout.ejs handles an optional
    local — flagging it would make this check's first report a false alarm
    about Coder's own file."""
    view = '<%- ui.flash_messages(typeof messages !== "undefined" ? messages : []) %>\n'
    assert free_identifiers(view, set()) == []


def test_the_shipped_layout_is_clean(tmp_path):
    """The check runs against every generated project, so a false positive here
    would fire on every single build."""
    NODE.scaffold(tmp_path, "sanity")
    entry = (tmp_path / "server.js").read_text(encoding="utf-8")
    provided = render_locals(entry)
    for view in sorted((tmp_path / "views").glob("*.ejs")):
        text = view.read_text(encoding="utf-8")
        _out, fixes, problems = repair_view_locals(text, provided.get(view.stem, set()))
        assert (fixes, problems) == ([], []), view.name


# ---------------------------------------------------------------------------
# Repairing only what is unambiguous
# ---------------------------------------------------------------------------


def test_a_ui_argument_is_blanked_so_the_helper_uses_its_default():
    fixed, fixes, problems = repair_view_locals(BROKEN_VIEW, {"orders"})
    assert fixes == ["empty_state"] and problems == []
    assert 'ui.table(orders, ["id", "final_amount"], "")' in fixed


def test_anything_else_is_reported_and_left_alone():
    """Rewriting an expression whose intent is unknown is generation."""
    view = "<h1><%= subtitle %></h1>\n"
    fixed, fixes, problems = repair_view_locals(view, set())
    assert fixed == view and fixes == []
    assert problems and "subtitle" in problems[0]


def test_a_clean_view_is_untouched():
    view = '<%- ui.table(orders, ["id"]) %>\n'
    assert repair_view_locals(view, {"orders"}) == (view, [], [])


def test_the_repair_is_idempotent():
    fixed, _f, _p = repair_view_locals(BROKEN_VIEW, {"orders"})
    assert repair_view_locals(fixed, {"orders"}) == (fixed, [], [])


# ---------------------------------------------------------------------------
# Stack seam + call site
# ---------------------------------------------------------------------------


def test_flask_does_not_need_this_and_says_so():
    """An undefined name renders as empty in Jinja. This is a property of the
    template engine, not an unimplemented check."""
    assert FLASK.render_locals(SERVER_JS) == {}
    assert FLASK.repair_view_locals(BROKEN_VIEW, set()) == (BROKEN_VIEW, [], [])


def test_core_runs_the_view_check_after_every_route_pass():
    """It learns what each view is given by reading `res.render` out of the
    entry file, so a route restored later would otherwise be invisible to it."""
    source = Path(core_module.__file__).with_suffix(".py").read_text(encoding="utf-8")
    tail = source[source.index("_restore_entry_route_note()") :][:1600]
    assert "_repair_view_locals" in tail
    assert tail.index("_reinstate_entry_routes") < tail.index("_repair_view_locals")


@pytest.mark.parametrize("adapter", [FLASK, NODE])
def test_every_adapter_answers_both_calls(adapter):
    assert isinstance(adapter.render_locals(SERVER_JS), dict)
    assert len(adapter.repair_view_locals("<p>hi</p>", set())) == 3


# ---------------------------------------------------------------------------
# False alarms. The check's first live report was five complaints about five
# views that rendered perfectly — worse than saying nothing at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "view",
    [
        "<% bids.forEach(bid => { %><%= bid.id %><% }) %>",
        "<% bids.forEach((bid, i) => { %><%= bid.id %><% }) %>",
        "<% bids.forEach(function (bid) { %><%= bid.id %><% }) %>",
        "<% for (const bid of bids) { %><%= bid.id %><% } %>",
        "<% try { go() } catch (err) { %><%= err %><% } %>",
    ],
)
def test_a_callback_parameter_is_a_binding_not_a_free_name(view):
    """`rows.forEach(row => …)` — a bare parameter with no parentheses — was
    the spelling that got missed, and it is the commonest one a model writes."""
    assert free_identifiers(view, {"bids", "go"}) == []


def test_a_template_literal_in_the_render_object_does_not_truncate_it():
    """``res.render("bid_detail", { title: `Bid ${bid.id}`, bid })`` — `${...}`
    carries braces of its own, so a brace-class regex stopped early and
    reported the view's only real local as undefined."""
    source = (
        "app.get('/bids/:id', async (req, res) => {\n"
        "  const bid = await models.getBid(req.params.id);\n"
        "  res.render('bid_detail', { title: `Bid ${bid.id}`, bid });\n"
        "});\n"
    )
    assert render_locals(source)["bid_detail"] == {"title", "bid"}


def test_a_brace_inside_a_string_does_not_end_the_render_object():
    source = 'res.render("page", { label: "a { b", rows });\n'
    assert render_locals(source)["page"] == {"label", "rows"}


def test_a_render_with_no_locals_is_still_recorded():
    assert render_locals('res.render("new_order");\n') == {"new_order": set()}
