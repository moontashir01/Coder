"""What must survive a repair pass rewriting the project's entry file.

Every test here comes from one measured build: the OpenBazaar PRD on the Node
stack (2026-08-04). `_wire_missing_endpoints` was asked to add ONE route, and
its single edit came back having deleted `GET /orders/new`, `POST /orders/new`,
the 404 handler, `db.initDb()`, `app.listen()` and `module.exports`. Every
existing guard passed: `node --check` accepts a file of handler registrations,
`restore_entry_route` anchors on the very lines that had been deleted so it
declined, and the smoke test was skipped for want of `node_modules`. The build
reported "verified OK" on an app that could not start.

So the two properties pinned here are:

  * the entry file can still START the app (`restore_boot_block`), and
  * a route the same turn wrote is still there (`route_blocks` /
    `reinstate_routes`),

plus — and this is the half that a unit test alone would miss — that `core`
actually CALLS them, in the order that makes them work.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import core as core_module
from app.agent.impact import reinstate_routes as flask_reinstate
from app.agent.impact import route_blocks as flask_route_blocks
from app.agent.scaffold import restore_run_block
from app.agent.stacks.flask_adapter import FLASK
from app.agent.stacks.node_adapter import NODE

ADAPTERS = [FLASK, NODE]


# The shape the scaffold ships, trimmed to what these repairs read.
GOOD_SERVER_JS = """\
"use strict";
const express = require("express");
const db = require("./db");
const models = require("./models");

const app = express();
const PORT = process.env.PORT || 3000;

app.get("/", (req, res) => {
  res.render("index", { title: "Home" });
});

app.get("/orders", async (req, res) => {
  const orders = await models.listOrders();
  res.render("orders", { orders });
});

app.get("/orders/new", (req, res) => {
  res.render("new_order");
});

app.post("/orders/new", async (req, res) => {
  await models.createOrder(req.body.item_id, req.body.buyer_id);
  res.redirect("/orders");
});

app.use((req, res) => {
  res.status(404).render("index", { title: "Not found" });
});

db.initDb().then(() => {
  app.listen(PORT, () => console.log("up"));
});

module.exports = app;
"""

# What the model actually returned: the file ends at the last handler it kept.
TRUNCATED_SERVER_JS = GOOD_SERVER_JS[: GOOD_SERVER_JS.index('app.get("/orders/new"')]

GOOD_APP_PY = """\
from flask import Flask, render_template, request

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/orders/new", methods=["GET", "POST"])
def new_order():
    if request.method == "POST":
        models.create_order(request.form["item_id"])
    return render_template("new_order.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
"""

TRUNCATED_APP_PY = GOOD_APP_PY[: GOOD_APP_PY.index('@app.route("/orders/new"')]


# ---------------------------------------------------------------------------
# The boot block — without it nothing else about the file matters
# ---------------------------------------------------------------------------


def test_node_restores_a_truncated_boot_block():
    restored, changed = NODE.restore_boot_block(TRUNCATED_SERVER_JS)
    assert changed
    assert "app.listen(" in restored
    assert "db.initDb()" in restored
    assert "module.exports" in restored


def test_node_boot_block_restores_only_what_is_missing():
    """A file that kept its 404 handler must not get a second one."""
    without_listen = GOOD_SERVER_JS.replace(
        'db.initDb().then(() => {\n  app.listen(PORT, () => console.log("up"));\n});\n',
        "",
    )
    restored, changed = NODE.restore_boot_block(without_listen)
    assert changed
    assert restored.count("res.status(404)") == 1
    assert restored.count("app.listen(") == 1


def test_node_boot_block_does_not_reference_db_when_the_file_does_not():
    """The file is already damaged, so the repair may not assume `db` survived."""
    no_db = TRUNCATED_SERVER_JS.replace('const db = require("./db");\n', "")
    restored, changed = NODE.restore_boot_block(no_db)
    assert changed
    assert "app.listen(" in restored
    assert "db.initDb()" not in restored


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_boot_block_is_idempotent(adapter):
    """A build that never lost it is untouched, and costs nothing."""
    source = GOOD_SERVER_JS if adapter is NODE else GOOD_APP_PY
    assert adapter.restore_boot_block(source) == (source, False)


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_boot_block_declines_on_a_file_that_is_not_an_entry_file(adapter):
    assert adapter.restore_boot_block("const x = 1;\n") == ("const x = 1;\n", False)
    assert adapter.restore_boot_block("") == ("", False)


def test_flask_restores_a_truncated_run_block():
    restored, changed = restore_run_block(TRUNCATED_APP_PY)
    assert changed
    assert '__name__ == "__main__"' in restored
    assert "app.run(" in restored
    compile(restored, "app.py", "exec")  # a repair may never break the file


def test_flask_run_block_declines_without_an_app_to_run():
    """Restoring `app.run()` where there is no `app` trades a dead file for a
    NameError, which is worse: it looks like the repair worked."""
    source = '@app.route("/")\ndef index():\n    return "hi"\n'
    assert restore_run_block(source) == (source, False)


# ---------------------------------------------------------------------------
# Routes — restored from the source they had, never regenerated
# ---------------------------------------------------------------------------


def test_node_route_blocks_capture_each_handler_verbatim():
    blocks = NODE.route_blocks(GOOD_SERVER_JS)
    assert ("POST", "/orders/new") in blocks
    body = blocks[("POST", "/orders/new")]
    assert "models.createOrder(req.body.item_id, req.body.buyer_id)" in body
    # Never past the terminal handlers, or the last route swallows the server.
    assert "app.listen" not in body
    assert "res.status(404)" not in body


def test_node_reinstates_a_deleted_post_handler_with_its_logic():
    """The case `restore_routes` structurally cannot cover: a POST body is
    domain logic, so it can only come back from the source it had."""
    blocks = NODE.route_blocks(GOOD_SERVER_JS)
    damaged = GOOD_SERVER_JS.replace(
        GOOD_SERVER_JS[
            GOOD_SERVER_JS.index('app.get("/orders/new"') : GOOD_SERVER_JS.index(
                "app.use((req, res)"
            )
        ],
        "",
    )
    restored, names = NODE.reinstate_routes(damaged, blocks)
    assert names == ["GET /orders/new", "POST /orders/new"]
    assert "models.createOrder(req.body.item_id, req.body.buyer_id)" in restored
    # Above the 404 handler, or the route is registered and never reached.
    assert restored.index('app.post("/orders/new"') < restored.index("res.status(404)")


def test_node_reinstatement_is_a_no_op_when_nothing_was_lost():
    blocks = NODE.route_blocks(GOOD_SERVER_JS)
    assert NODE.reinstate_routes(GOOD_SERVER_JS, blocks) == (GOOD_SERVER_JS, [])


def test_flask_reinstates_a_deleted_route_with_its_body():
    blocks = flask_route_blocks(GOOD_APP_PY)
    assert ("POST", "/orders/new") in blocks
    restored, names = flask_reinstate(TRUNCATED_APP_PY, blocks)
    assert "POST /orders/new" in names
    assert 'models.create_order(request.form["item_id"])' in restored
    compile(restored, "app.py", "exec")


def test_flask_reinstatement_does_not_define_a_view_twice():
    """A view name still in the file means the handler was RENAMED onto another
    path. Re-adding it would shadow the live definition."""
    blocks = flask_route_blocks(GOOD_APP_PY)
    renamed = GOOD_APP_PY.replace(
        '@app.route("/orders/new"', '@app.route("/orders/add"'
    )
    restored, names = flask_reinstate(renamed, blocks)
    assert names == []
    assert restored == renamed


# ---------------------------------------------------------------------------
# The call sites. A repair nothing calls reads exactly like one that passed.
# ---------------------------------------------------------------------------


def _core_source() -> str:
    return Path(core_module.__file__).with_suffix(".py").read_text(encoding="utf-8")


def test_core_calls_both_repairs_after_the_endpoint_wiring_edit():
    """`_wire_missing_endpoints` rewrites the entry file wholesale, and is the
    pass that was measured deleting routes out of it."""
    source = _core_source()
    body = source[source.index("async def _wire_missing_endpoints") :]
    body = body[: body.index("\n    async def ", 10)]
    assert "_restore_boot_block_note" in body
    assert "_reinstate_entry_routes" in body


def test_core_calls_both_repairs_at_the_end_of_the_turn():
    """The invariant belongs wherever the file stops being rewritten — the smoke
    repair runs after everything else and rewrites it again."""
    source = _core_source()
    tail = source[source.index("_restore_entry_route_note()") :][:600]
    assert "_reinstate_entry_routes" in tail


def test_the_boot_block_is_restored_before_the_routes_are():
    """Order is load-bearing: a restored route is placed ABOVE the 404 handler,
    so with no boot block there is no anchor and the route restore declines."""
    source = _core_source()
    for start in re.finditer(r"_restore_boot_block_note\(", source):
        window = source[start.start() : start.start() + 900]
        if "_reinstate_entry_routes" in window:
            break
    else:  # pragma: no cover — the assertion below reports it
        pytest.fail("no call site restores the boot block before the routes")
    assert window.index("_restore_boot_block_note") < window.index(
        "_reinstate_entry_routes"
    )


# ---------------------------------------------------------------------------
# Route ORDER — Express matches in registration order, so it is a correctness
# property, not a style one
# ---------------------------------------------------------------------------


SHADOWED_SERVER_JS = """\
"use strict";
const express = require("express");
const db = require("./db");
const app = express();

app.get("/items", (req, res) => {
  res.render("items");
});

app.get("/items/:id", (req, res) => {
  res.render("item_detail");
});

app.get("/items/new", (req, res) => {
  res.render("new_item");
});

app.post("/items/new", (req, res) => {
  res.redirect("/items");
});

app.use((req, res) => {
  res.status(404).send("nope");
});

db.initDb().then(() => app.listen(3000));
"""


def test_a_parameterised_route_is_moved_below_the_page_it_swallows():
    ordered, moved, problems = NODE.order_routes(SHADOWED_SERVER_JS)
    assert moved == ["GET /items/:id"] and problems == []
    assert ordered.index('app.get("/items/new"') < ordered.index('app.get("/items/:id"')
    # …and still above the 404 handler, or it is never reached at all.
    assert ordered.index('app.get("/items/:id"') < ordered.index("res.status(404)")


def test_route_ordering_is_idempotent():
    ordered, _m, _p = NODE.order_routes(SHADOWED_SERVER_JS)
    assert NODE.order_routes(ordered) == (ordered, [], [])


def test_route_ordering_leaves_a_file_with_no_collision_alone():
    """It moves only what is provably wrong: route order carries meaning this
    cannot see."""
    source = SHADOWED_SERVER_JS.replace('app.get("/items/:id"', 'app.get("/orders/:id"')
    assert NODE.order_routes(source) == (source, [], [])


def test_flask_declines_to_reorder_and_says_why():
    """Werkzeug ranks rules by specificity, so this is a real no-op rather than
    an unimplemented one."""
    assert FLASK.order_routes(GOOD_APP_PY) == (GOOD_APP_PY, [], [])


def test_core_orders_the_routes_after_restoring_them():
    """Both restores insert at the BOTTOM of the route section, which is the
    wrong end for `/items/:id` — so ordering has to run last."""
    source = _core_source()
    tail = source[source.index("_restore_entry_route_note()") :][:900]
    assert "_order_entry_routes" in tail
    assert tail.index("_reinstate_entry_routes") < tail.index("_order_entry_routes")


# ---------------------------------------------------------------------------
# A deterministic pass may leave a file unimproved. It may never leave it BROKEN
# ---------------------------------------------------------------------------


# What a 7B actually wrote: the routes nested inside the startup callback, so
# the terminal boundary (`db.initDb(`) is ABOVE them and the naive slice for the
# last route runs to end-of-file, swallowing the wrapper's own `});`.
WRAPPED_SERVER_JS = """\
"use strict";
const express = require("express");
const db = require("./db");
const app = express();

db.initDb().then(() => {
  app.get("/bids", async (req, res) => {
    res.render("bids");
  });

  app.get("/bids/:id", async (req, res) => {
    res.render("bid_detail");
  });

  app.listen(3000);
});
"""


def test_a_slice_that_does_not_balance_is_never_recorded():
    """Re-inserting it elsewhere can only produce a file that will not parse —
    measured: one closing brace too many, and the finished build died with
    `SyntaxError: Unexpected token '}'`."""
    blocks = NODE.route_blocks(WRAPPED_SERVER_JS)
    for key, block in blocks.items():
        assert block.count("{") == block.count("}"), key
        assert block.count("(") == block.count(")"), key


def test_reinstating_into_a_wrapped_file_still_parses(tmp_path):
    blocks = NODE.route_blocks(WRAPPED_SERVER_JS)
    damaged = WRAPPED_SERVER_JS.replace(
        '  app.get("/bids", async (req, res) => {\n    res.render("bids");\n  });\n\n',
        "",
    )
    restored, _names = NODE.reinstate_routes(damaged, blocks)
    path = tmp_path / "server.js"
    path.write_text(damaged, encoding="utf-8")
    # The write gate is the real net: it runs `node --check` and reverts.
    assert NODE.write_source_if_valid(path, restored) is True


def test_the_node_write_gate_reverts_a_rewrite_that_breaks_the_file(tmp_path):
    """`_write_python_if_valid` compiles the Python it is about to write, so a
    hand-written repair that breaks `app.py` is refused. Until this ran
    `node --check`, the Node side only asked "is this HTML in a .js file?" — so
    a deterministic pass that broke `server.js` shipped, and everything
    downstream was green because nothing between it and the user parsed."""
    path = tmp_path / "server.js"
    path.write_text("const a = 1;\n", encoding="utf-8")

    assert NODE.write_source_if_valid(path, "const b = 2;\n") is True
    assert path.read_text(encoding="utf-8").strip() == "const b = 2;"

    assert NODE.write_source_if_valid(path, "const c = 3;\n});\n") is False
    assert path.read_text(encoding="utf-8").strip() == "const b = 2;"  # reverted


def test_the_write_gate_still_writes_a_brand_new_file(tmp_path):
    path = tmp_path / "fresh.js"
    assert NODE.write_source_if_valid(path, "const a = 1;\n") is True
    assert path.is_file()


# Routes nested inside a callback: the slicer must decline, and the DECLINE
# must be audible. Returning `[]` here is indistinguishable from "the order is
# fine", and on a measured build `/bids/new` went on being served by
# `/bids/:id` — 500 on every request, with the build reporting nothing.
NESTED_SHADOWED_JS = """\
"use strict";
const express = require("express");
const db = require("./db");
const app = express();

app.get("/bids/:id", async (req, res) => {
  res.render("bid_detail");
});

db.initDb().then(() => {
  app.get("/bids/new", async (req, res) => {
    res.render("new_bid");
  });

  app.listen(3000);
});
"""


def test_a_collision_it_cannot_repair_is_reported_not_swallowed():
    source, moved, problems = NODE.order_routes(NESTED_SHADOWED_JS)
    assert source == NESTED_SHADOWED_JS  # nothing was rewritten
    assert moved == []
    assert len(problems) == 1
    assert "/bids/new" in problems[0] and "/bids/:id" in problems[0]


def test_the_collision_reader_does_not_depend_on_the_block_slicer():
    """That is the whole point: the question must still be answerable on a file
    whose shape `route_blocks` refuses to slice."""
    assert NODE.route_blocks(NESTED_SHADOWED_JS) == {} or True  # either is fine
    assert NODE.shadowed_routes(NESTED_SHADOWED_JS) == [
        ("GET", "/bids/:id", "/bids/new")
    ]


def test_a_correctly_ordered_file_reports_no_collision():
    good = NESTED_SHADOWED_JS.replace(
        'app.get("/bids/:id", async (req, res) => {\n  res.render("bid_detail");\n});\n\n',
        "",
    )
    assert NODE.shadowed_routes(good) == []


# ---------------------------------------------------------------------------
# A code token in a sentence is not a file to write
# ---------------------------------------------------------------------------


def test_a_method_call_is_not_taken_for_a_filename(tmp_path, monkeypatch):
    """Measured: "move the `app.get("/bids/:id")` route below…" created a file
    literally named `app.get` in the project root. The blocklist that had been
    grown from an earlier junk `e.g` file could never have caught it — every
    code token of that shape (`res.render`, `req.body`, `db.initDb`) is a
    candidate, so the test has to be a positive one."""
    from app.agent.core import _extract_filename

    monkeypatch.chdir(tmp_path)
    assert _extract_filename('move the app.get("/bids/:id") route below it') is None
    assert _extract_filename("update db.initDb() so it works") is None
    assert _extract_filename("call models.listItems from the route") is None


def test_a_real_filename_in_the_same_sentence_still_wins(tmp_path, monkeypatch):
    from app.agent.core import _extract_filename

    monkeypatch.chdir(tmp_path)
    assert _extract_filename("In server.js, move the app.get(...) route") == "server.js"
    assert (
        _extract_filename('fix res.render("x") in views/items.ejs') == "views/items.ejs"
    )
    assert _extract_filename("edit README.md please") == "README.md"


def test_an_unusual_extension_that_exists_on_disk_is_accepted(tmp_path, monkeypatch):
    """The allowlist cannot know every extension a project uses, so a file that
    is really there settles it."""
    from app.agent.core import _extract_filename

    monkeypatch.chdir(tmp_path)
    (tmp_path / "build.gradle").write_text("x", encoding="utf-8")
    assert _extract_filename("edit build.gradle") == "build.gradle"


# ---------------------------------------------------------------------------
# The amendment path had no protection at all
# ---------------------------------------------------------------------------


class _Spec:
    """The two fields `_wanted_entry_routes` reads off a ProjectSpec."""

    def __init__(self, paths):
        self.endpoints = [SimpleNamespace(path=p) for p in paths]
        self.pages = []


def _agent_with(recorded, spec, blueprint=None):
    agent = core_module.AgentCore.__new__(core_module.AgentCore)
    agent._entry_routes = recorded
    agent._spec = spec
    agent._blueprint = blueprint
    return agent


RECORDED = {("GET", "/bids"): "a\n", ("GET", "/bids/:id"): "b\n"}


def test_a_build_turn_protects_every_recorded_route():
    """Everything after generation is repair, and repair only adds."""
    agent = _agent_with(RECORDED, _Spec(["/bids"]), blueprint=object())
    assert agent._wanted_entry_routes() == RECORDED


def test_an_amendment_protects_only_what_the_spec_still_declares():
    """Measured: a follow-up asked to MOVE `GET /bids/:id` below `/bids/new`
    deleted it instead, and the detail page 404'd from then on — the record was
    only ever populated on a build turn, so nothing could put it back."""
    agent = _agent_with(RECORDED, _Spec(["/bids", "/bids/:id"]))
    assert set(agent._wanted_entry_routes()) == set(RECORDED)


def test_an_amendment_does_not_fight_a_deletion_the_user_asked_for():
    """A turn that says "drop the /bids page" removes it from the spec too."""
    agent = _agent_with(RECORDED, _Spec(["/bids"]))
    assert set(agent._wanted_entry_routes()) == {("GET", "/bids")}


def test_no_spec_on_disk_behaves_as_it_did_before_this_existed():
    agent = _agent_with(RECORDED, None)
    assert agent._wanted_entry_routes() == RECORDED


def test_the_record_is_filled_at_the_top_of_every_turn():
    """Not only inside `_run_blueprint` — that is what left the amendment path
    unprotected."""
    source = _core_source()
    chat = source[source.index("    async def chat(") :]
    chat = chat[: chat.index("\n    async def ", 10)]
    assert "_remember_entry_routes" in chat
