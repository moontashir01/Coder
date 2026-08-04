"""Calls between a Node project's own modules that nothing defines.

The gap this closes was silent by construction: `_check_cross_module_calls`
returned `[]` for anything but Python, honestly documented — and a check that
returns nothing reads exactly like a check that passed. The OpenBazaar PRD build
(2026-08-04) shipped a `server.js` whose startup was

    db.setup().then(() => { app.listen(PORT, …); });

against a `db.js` exporting `initDb`. `node --check` accepted it, every route
parsed, `app.listen(` was present so the boot-block invariant held, and the
answer reported a clean build of an app that exits on startup.
"""

from pathlib import Path

import pytest

from app.agent import core as core_module
from app.agent.jsdeps import (
    exported_names,
    fix_db_bootstrap,
    unresolved_local_calls,
)
from app.agent.stacks.flask_adapter import FLASK
from app.agent.stacks.node_adapter import NODE

DB_JS = """\
"use strict";
const { Pool } = require("pg");
async function initDb() {}
async function close() {}
module.exports = { getPool, initDb, ensureColumn, close, DATABASE_URL };
"""

SERVER_JS = """\
"use strict";
const express = require("express");
const db = require("./db");
const models = require("./models");

const app = express();

app.get("/items", async (req, res) => {
  res.render("items", { items: await models.listItems() });
});

db.setup().then(() => {
  app.listen(3000);
});
"""

MODELS_JS = "module.exports = { listItems };\n"


# ---------------------------------------------------------------------------
# Reading exports
# ---------------------------------------------------------------------------


def test_exports_are_read_from_the_scaffold_s_own_shape():
    assert exported_names(DB_JS) == {
        "getPool",
        "initDb",
        "ensureColumn",
        "close",
        "DATABASE_URL",
    }


def test_property_assignment_exports_are_read_too():
    source = "exports.one = 1;\nmodule.exports.two = function () {};\n"
    assert exported_names(source) == {"one", "two"}


def test_a_module_with_no_exports_reads_as_unknown_not_as_empty():
    """`set()` means "could not tell", and the caller must then stay quiet —
    a confident wrong complaint about a working build is worse than a miss."""
    assert exported_names("const x = 1;\n") == set()
    assert unresolved_local_calls(SERVER_JS, {"db": "const x = 1;\n"}) == []


# ---------------------------------------------------------------------------
# Finding the dangling call
# ---------------------------------------------------------------------------


def test_a_call_the_module_never_exports_is_reported():
    assert unresolved_local_calls(SERVER_JS, {"db": DB_JS, "models": MODELS_JS}) == [
        "db.setup"
    ]


def test_calls_that_do_resolve_are_not_reported():
    assert "models.listItems" not in unresolved_local_calls(
        SERVER_JS, {"db": DB_JS, "models": MODELS_JS}
    )


def test_a_package_from_node_modules_is_never_checked():
    """`express.static` is somebody else's API and unreadable from here."""
    source = 'const express = require("express");\napp.use(express.static("public"));\n'
    assert unresolved_local_calls(source, {"db": DB_JS}) == []


def test_a_local_module_that_is_not_ours_is_skipped():
    source = 'const helper = require("./helper");\nhelper.doThing();\n'
    assert unresolved_local_calls(source, {"db": DB_JS}) == []


# ---------------------------------------------------------------------------
# The one repair — a scaffold invariant, not a general fixer
# ---------------------------------------------------------------------------


def test_the_startup_call_is_repointed_at_the_real_function():
    fixed, fixes = fix_db_bootstrap(SERVER_JS, DB_JS)
    assert fixes == ["db.setup() -> db.initDb()"]
    assert "db.initDb()" in fixed and "db.setup()" not in fixed


def test_the_repair_is_idempotent():
    fixed, _ = fix_db_bootstrap(SERVER_JS, DB_JS)
    assert fix_db_bootstrap(fixed, DB_JS) == (fixed, [])


def test_the_repair_declines_when_db_js_exports_no_initdb():
    """Rewriting to a name that is also absent trades one dead app for another
    that looks repaired."""
    assert fix_db_bootstrap(SERVER_JS, "module.exports = { getPool };\n") == (
        SERVER_JS,
        [],
    )


def test_the_repair_leaves_calls_that_take_arguments_alone():
    """Only the no-argument startup call is known. A call with arguments is
    doing something this cannot reconstruct."""
    source = SERVER_JS.replace("db.setup()", "db.setup({ retries: 3 })")
    assert fix_db_bootstrap(source, DB_JS) == (source, [])


# ---------------------------------------------------------------------------
# The call sites
# ---------------------------------------------------------------------------


def test_the_cross_module_check_no_longer_gives_up_on_javascript():
    source = Path(core_module.__file__).with_suffix(".py").read_text(encoding="utf-8")
    body = source[source.index("def _check_cross_module_calls") :]
    body = body[: body.index("\n    async def ", 10)]
    assert "js_unresolved_local_calls" in body
    assert 'if self._adapter.language != "python":\n            return []' not in body


def test_core_repairs_the_startup_call_at_the_end_of_the_turn():
    source = Path(core_module.__file__).with_suffix(".py").read_text(encoding="utf-8")
    tail = source[source.index("_restore_entry_route_note()") :][:1200]
    assert "_repair_entry_module_calls" in tail


@pytest.mark.parametrize("adapter", [FLASK, NODE])
def test_every_adapter_answers_the_repair_call(adapter, tmp_path):
    """The protocol is only useful if both sides implement it; Flask's honest
    no-op still has to be callable."""
    assert adapter.repair_module_calls("print('hi')\n", tmp_path) == (
        "print('hi')\n",
        [],
    )


# ---------------------------------------------------------------------------
# Where SQL may live differs per language, and getting it wrong INVENTS defects
# ---------------------------------------------------------------------------


JS_WITH_PROSE = """\
/**
 * Every query is printed from the same definition as the tables in db.js,
 * so a column cannot drift from a table it was taken from.
 */
const q = "SELECT * FROM items";
"""


def test_the_node_extractor_reads_literals_not_comments():
    """`searchable_sql` falls back to the whole raw file when `ast.parse`
    fails — which every `.js` file does — so the Python default read the prose
    above and reported tables called `a` and `the` on a real build."""
    from app.agent.pyimports import missing_tables

    sources = {"models": JS_WITH_PROSE}
    # The Python default invents two tables out of the sentence above.
    assert {"a", "the"} <= set(missing_tables(sources))
    assert missing_tables(sources, NODE.sql_literals) == ["items"]


def test_a_real_missing_table_is_still_reported_on_node():
    sources = {
        "models": 'const q = "SELECT * FROM orders";\n',
        "db": 'await client.query("CREATE TABLE IF NOT EXISTS items (id SERIAL)");\n',
    }
    assert missing_tables_node(sources) == ["orders"]


def missing_tables_node(sources):
    from app.agent.pyimports import missing_tables

    return missing_tables(sources, NODE.sql_literals)


def test_flask_keeps_the_python_answer():
    from app.agent.pyimports import missing_tables, searchable_sql

    sources = {"app": 'q = "SELECT * FROM posts"\n'}
    assert FLASK.sql_literals(sources["app"]) == searchable_sql(sources["app"])
    assert missing_tables(sources, FLASK.sql_literals) == ["posts"]
