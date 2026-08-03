"""The Node stack: Express + EJS + PostgreSQL.

Phases N1/N2 of `docs/node-stack-plan.md`. This adapter is deliberately
**shallower than the Flask one**, and says so out loud rather than pretending
otherwise — see `gaps`, which `/stack` prints verbatim.

What works here today (N1-N4):

  * a scaffold that runs before the model has written a line (`scaffolds/node/`),
    carrying the same component sheet and the same theme file as Flask, so all
    of Phase W1's design-system work transfers unchanged;
  * the deterministic data layer — `db.js`'s tables, `models.js`, `seed.js` and
    `passwords.js` written from the SAME `Entity` objects `crud.py` uses, with
    the dialect differences confined to `projectspec.POSTGRES`;
  * migrations derived from the spec rather than from the model, and *reported*
    when they cannot be placed — a migration the caller believes ran when it did
    not is worse than one that never claimed to;
  * routes read back off `server.js`, so the spec records what was really built,
    and read off a Node repo Coder did NOT build (`ProjectSpec.from_disk`);
  * `.ejs` structural checking and path-based link validation, with the same
    exactly-one-candidate near-miss rule W2 uses on `url_for`;
  * the layout invariant — a view that ships its own `<html>`/`<nav>` renders
    two navbars, and that is detected and repaired exactly as on Flask.

What is still Flask-only is in `gaps` below, which `/stack` prints verbatim. The
list is not decoration: an adapter that returned a Flask-shaped answer for a
Node project would be worse than no Node stack at all, because the failure would
be silent and on turn 2.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.agent import scaffold as _scaffold
from app.agent.crud_node import (
    adds_column,
    api_context,
    apply_block,
    creates_table,
    models_source,
    needs_password_helper,
    password_helper_source,
    seed_source,
    table_block,
)
from app.agent.projectspec import POSTGRES
from app.agent.verify import (
    check_text,
    fix_link_targets,
    form_method_mismatches_by_path,
    unresolved_links,
)
from config.settings import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.agent.projectspec import ProjectSpec

logger = logging.getLogger(__name__)

# `app.get("/products", handler)` / `router.post('/x', a, b)`. A regex, not
# tree-sitter: N4 owns the parser upgrade, and the shape the scaffold and the
# model actually write is this one. Deliberately narrow — a route it cannot see
# is a route left unvalidated, which is the safe direction; a route it invents
# would send the repair passes after a handler that does not exist.
_ROUTE_RE = re.compile(
    r"""\b(?:app|router)\s*\.\s*(?P<method>get|post|put|patch|delete|all)\s*\(\s*"""
    r"""(?P<q>["'`])(?P<path>[^"'`]+)(?P=q)""",
    re.IGNORECASE,
)
# The view a handler renders, looked for in the text following the route call.
_RENDER_RE = re.compile(r"""\bres\s*\.\s*render\s*\(\s*["'`](?P<tpl>[^"'`]+)["'`]""")

# The scaffold's home route, and where to put it back if generation deletes it.
_INDEX_ROUTE_RE = re.compile(
    r"""\b(?:app|router)\s*\.\s*get\s*\(\s*["'`]/["'`]""", re.IGNORECASE
)
_ANY_ROUTE_RE = re.compile(r"""\b(?:app|router)\s*\.\s*(?:get|post)\s*\(""")
_LISTEN_RE = re.compile(r"""\bapp\s*\.\s*listen\s*\(""")
_INIT_DB_RE = re.compile(r"""\bdb\s*\.\s*initDb\s*\(""")
# Express matches middleware in registration order, so the 404 catch-all and the
# error handler are TERMINAL: a route registered after them is never reached.
# Restoring the home page below the 404 handler would leave the site 404ing on
# its front page anyway — the exact failure being repaired, now invisible.
#
# Matched on the handler's own parameter list, NOT on nearby text: scanning a
# window after each `app.use(` for "404" flagged `app.use(express.static(...))`
# as terminal because the *comment* introducing the real 404 handler fell inside
# the window, and the route was then inserted above the static mount. An inline
# `(req, res)` / `(err, req, res, next)` is what makes a `use` terminal; a
# middleware mount (`app.use(express.static(…))`, `app.use(expressLayouts)`)
# starts with an identifier and is not.
_TERMINAL_USE_RE = re.compile(
    r"""\bapp\s*\.\s*use\s*\(\s*(?:async\s+)?(?:function\s*)?\(\s*_?(?:err|req)\b"""
)

# Probe verdicts that mean "we could not find out", never "the environment is at
# fault". `CODER_BAD_DBJS` is the important one: a `db.js` that will not load is
# a defect in the generated code, and gating the smoke test on it would suppress
# the only check that reports it.
_CANNOT_TELL = frozenset({"CODER_NO_URL", "CODER_BAD_DBJS"})

_INDEX_ROUTE_SNIPPET = """
app.get("/", (req, res) => {
  // Home page. Restored by Coder: generation had removed it.
  res.render("index", { title: "Home" });
});

"""


def _insertion_point(text: str) -> int | None:
    """Where a restored route must go: above everything terminal, or nowhere.

    Express matches middleware in registration order, so the 404 catch-all, the
    error handler and the server start are all boundaries a route must stay
    ABOVE. Anchoring on `app.listen(` alone is not enough and was measured
    wrong against this project's own scaffold: the 404 handler sits above the
    listen call, so the "restored" home route landed below it and the site went
    on 404ing its own front page — the repair reporting success while changing
    nothing that mattered.

    Returns the start of the line containing the earliest boundary, or None
    when there is no boundary to place the route relative to (declining beats
    appending to the end of a file whose shape we could not read).
    """
    bounds: list[int] = []
    for pattern in (_TERMINAL_USE_RE, _INIT_DB_RE, _LISTEN_RE):
        match = pattern.search(text)
        if match:
            bounds.append(match.start())
    if not bounds:
        return None
    return text.rfind("\n", 0, min(bounds)) + 1


class NodeAdapter:
    """Node / Express / EJS / PostgreSQL — opt-in via `/stack node`."""

    key = "node"
    label = "Node · Express · EJS · PostgreSQL"
    display_name = "Express"
    scaffold_summary = (
        "server.js, db.js, models.js, ui.js, views/, public/, package.json, Procfile"
    )
    start_hint = "npm install && node server.js"
    seed_hint = "node seed.js"
    language = "node"
    # `runtime_probe._node()` reports "stdlib" when the network is off, which
    # collides with the *Python* stdlib stack — which is why `key_for_stack`
    # checks `language` first and this tuple is only a secondary signal.
    backends = ("express", "node")
    entry_file = "server.js"
    template_dir = "views"
    template_ext = ".ejs"
    layout_file = "layout.ejs"
    static_dir = "public"
    theme_file = "public/css/theme.css"
    home_template = "views/index.ejs"
    db_module = "db.js"
    source_globs = ("*.js", "*.mjs")
    page_note = (
        "A FRAGMENT that layout.ejs wraps — no <html>, no <head>, no <nav> of "
        "its own."
    )
    home_edit_note = (
        "Replace the whole view — it is a fragment, so there is no layout to "
        "preserve inside it."
    )

    guarantees = (
        "runnable Express + EJS skeleton before any generation",
        "the same component sheet and theme file as Flask (W1 transfers whole)",
        "deterministic data layer (db.js / models.js / seed.js from the schema), "
        "with $1 parameters and RETURNING id",
        "schema migrations from the spec, never from the model",
        "every entity gets a list page and a create form, derived not prompted",
        "routes read back off server.js, so the spec records what was built",
        "views that ship their own <html>/<nav> are detected and rewritten",
        "the browser layer (W4-W7) — it speaks HTTP and DOM, not Python",
        ".ejs structural checking: an unterminated <% takes the page down at "
        "render time, and nothing else in the pipeline can see it",
        "link validation against the routes really defined in server.js — a "
        "near miss is repointed, anything else is reported",
        "a Node repo Coder did not build is adopted: routes off server.js, "
        "tables off db.js, so turn 2 can amend it",
        "an EJS template graph off disk, so an amendment knows which view "
        "displays an entity without being told (W8's shape)",
        "readiness is PROVEN, not assumed: node, node_modules, the port and a "
        "real SELECT 1 through the project's own connection string, so a "
        "database that does not exist is named instead of surfacing as a "
        "mysterious failed start",
    )
    gaps = (
        "no missing-import repair: Flask adds an import a module uses but never "
        "binds, which is its only AUTO-REPAIRING correctness check. It is built "
        "on stdlib `ast`; a JS equivalent is its own piece of work and would be "
        "weaker, so an undefined name here surfaces at runtime",
        "the IMPORT dependency graph stays Python-only: `symbols.py` extracts JS "
        "symbols but does not resolve JS imports, so a change's blast radius "
        "comes from the template graph and the spec's own `reads`, not from "
        "which module imports which",
        "routes are read with a regex, not a parser: a route written in a shape "
        "it does not recognise is left unvalidated rather than reported wrongly",
        "no template-scoped editing — an edit to a view rewrites the whole file, "
        "where Flask confines it to the `{% block %}` it belongs in (W3)",
        "`npm install` needs the network once, which sqlite never does — the "
        "build itself stays offline, but the project cannot RUN until it has "
        "been fetched, and Coder will not fetch it for you",
    )

    # -- scaffold ---------------------------------------------------------

    def scaffold_dir(self) -> Path:
        return _scaffold.scaffold_dir(self.key)

    def scaffold(self, root: Path, name: str | None = None) -> list[str]:
        return _scaffold.copy_scaffold(self.scaffold_dir(), root, name)

    def scaffold_files(self) -> set[str]:
        return _scaffold.scaffold_tree_files(self.scaffold_dir())

    def frozen_files(self) -> set[str]:
        """What generation must not rewrite — the Flask `_FROZEN` list, mapped.

        Same rule, same reasons: pure boilerplate where a rewrite is all risk
        and no benefit, plus the component library and the theme, which a model
        handed either one rewrites out from under every page that depends on it.
        `ui.js` is frozen for exactly the reason `_macros.html` is.
        """
        return {
            "package.json",
            "Procfile",
            ".gitignore",
            "public/uploads/.gitkeep",
            "ui.js",
            "public/css/theme.css",
        }

    def is_frozen(self, filename: str) -> bool:
        name = (filename or "").replace("\\", "/").strip()
        while name.startswith("./"):
            name = name[2:]
        return name.lstrip("/") in self.frozen_files()

    def write_theme(self, root: Path, css: str) -> bool:
        return _scaffold.write_theme(root, css, self.theme_file)

    def theme_exists(self, root: Path) -> bool:
        return (Path(root) / self.theme_file).is_file()

    # -- data layer -------------------------------------------------------

    def write_data_layer(self, root: Path, spec: "ProjectSpec") -> tuple[set[str], str]:
        """Write `db.js`'s tables, `models.js` and `seed.js` from the entities.

        Phase N3, and the exact mirror of the Flask adapter: these files contain
        no decisions, so they are generated from `spec.entities` rather than
        prompted for. `crud_node` emits from the SAME `Entity` objects `crud`
        does — everything that differs is in `projectspec.POSTGRES` — so the two
        stacks cannot end up with different schemas from one spec.

        Returns ``(files it now owns, the API description for the prompt)``. The
        second half is not optional: taking the data layer away from the model is
        only safe if the model is TOLD what replaced it.
        """
        root = Path(root)
        if not spec.entities:
            return set(), ""
        owned: set[str] = set()

        db_path = root / self.db_module
        if db_path.is_file():
            try:
                source = db_path.read_text(encoding="utf-8", errors="replace")
                missing = [
                    e for e in spec.entities if not creates_table(source, e.table)
                ]
                if missing:
                    from app.agent.projectspec import ProjectSpec as _Spec

                    block = table_block(_Spec(entities=tuple(missing)))
                    updated, changed = apply_block(source, block)
                    if changed and self.write_source_if_valid(db_path, updated):
                        owned.add(self.db_module)
            except Exception:
                logger.warning("could not write the schema into db.js", exc_info=True)

        writers = [("models.js", models_source(spec)), ("seed.js", seed_source(spec))]
        if needs_password_helper(spec):
            # Node has no `werkzeug.security`, and adding bcrypt would mean a
            # native build on a machine whose point is that it works offline.
            writers.append(("passwords.js", password_helper_source()))
        for rel, text in writers:
            path = root / rel
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
                owned.add(rel)
            except Exception:
                logger.warning("could not write %s", rel, exc_info=True)
        return owned, api_context(spec)

    def migration_note(self, root: Path, spec: "ProjectSpec", since: int) -> str:
        """Put the new `ensureColumn` calls into `db.js`. Reported if it can't.

        PostgreSQL's `ALTER TABLE … ADD COLUMN IF NOT EXISTS` is DDL against a
        live server, not SQLite's runtime PRAGMA check — so unlike Flask, a
        migration here can fail at runtime even after being written correctly.
        That is exactly why the write is deterministic and the failure path
        prints the statements: a migration the caller believes ran when it did
        not is worse than one that never claimed to.
        """
        db_path = Path(root) / self.db_module
        if not db_path.is_file():
            return ""
        calls = spec.migrations(since=since, dialect=POSTGRES)
        if not calls:
            return ""
        try:
            source = db_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

        # Idempotent per column, and read off string literals / decommented
        # source: db.js ships a *commented* `ensureColumn(client, "widgets", …)`
        # example, and counting that as a real migration is the `_creates_table`
        # trap one stack over.
        pending = [
            (entity.table, f.name, f.type)
            for entity in spec.entities
            for f in entity.fields
            if f.added_in > since
            and not f.pk
            and not adds_column(source, entity.table, f.name)
        ]
        if not pending:
            return ""
        block = "\n".join(
            f"    {POSTGRES.migration_call(table, column, kind)};"
            for table, column, kind in pending
        )
        updated, changed = apply_block(source, block)
        if not changed or not self.write_source_if_valid(db_path, updated):
            return (
                "may not meet: could not place the schema migration in db.js — "
                "add it by hand inside `initDb()`: " + "; ".join(calls)
            )
        return (
            f"Wrote {len(pending)} schema migration(s) into `db.js` from the "
            "project spec — existing rows are kept, not recreated."
        )

    # -- running it -------------------------------------------------------

    def readiness(self, root: Path) -> str:
        """Why the generated app cannot start here, or "" when it should.

        Phase N5's gate. Node + Postgres has three ways to be un-runnable where
        Flask has one, and the caller uses this to SKIP the smoke test rather
        than fail it — otherwise `_smoke_repair_instruction` sends the model to
        rewrite correct code because a package was never installed or a database
        was down.

        **A skipped check must never read as a passing one**: the caller reports
        this string, it never silently swallows it.

        Ordered cheapest-first, and each step is a precondition of the next: the
        `SELECT 1` costs a subprocess, so it runs only once `node`,
        `node_modules` and something on the port have all been established. The
        earlier checks are not redundant with it — they are what let its failure
        mean one specific thing.
        """
        import shutil

        if not shutil.which("node"):
            return "Node.js is not on PATH — install it to run the generated app"
        if not (Path(root) / "node_modules").is_dir():
            return (
                "`node_modules` is missing — run `npm install` in the project "
                "(it needs the network once)"
            )
        if not self._postgres_listening():
            host, port = self._db_endpoint()
            return (
                f"nothing is listening on {host}:{port} — start PostgreSQL and "
                "create the project's database (see db.js)"
            )
        return self.database_reason(root)

    def database_reason(self, root: Path) -> str:
        """The `SELECT 1` half of N5: can the app really reach its database?

        A socket to 5432 proves a server is listening. It does not prove that
        *this project's* database exists or that its credentials work — and both
        of those fail at `initDb()`, which the generated app treats as fatal, so
        without this the whole build reports "the smoke test failed" for a reason
        that is not in the code at all.

        Run through **`node` and the project's own `pg`**, not a Python driver:
        `node_modules` has already been established above, `pg` is the project's
        own dependency, and this needs nothing installed on the Python side — the
        same reasoning that makes `crypto.scrypt` the password helper. It reads
        the connection string out of the project's own `db.js` for the load-bearing
        reason: a probe that guessed its own URL could pass while the app fails,
        or fail while the app works, and either way it would be measuring
        something other than the thing that has to work.

        **Uncertainty resolves to "" — run the check.** A probe that cannot say
        anything (no `node`, no URL to try, a `db.js` that will not load, a
        crash, a timeout) must not gate the smoke test: skipping is only correct
        when we KNOW the environment is at fault, and the smoke test is the real
        measurement. In particular a `db.js` that fails to load is a *code*
        defect, and reporting it here would skip the very check that exists to
        catch it.
        """
        payload = self._probe_database(root)
        if payload is None or payload.get("ok"):
            return ""

        code = str(payload.get("code") or "")
        database = str(payload.get("database") or "")
        host = str(payload.get("host") or "localhost")
        port = payload.get("port") or 5432
        message = str(payload.get("message") or "").strip()

        if code == "CODER_NO_PG":
            return (
                "the `pg` package is not installed in the project — run "
                "`npm install` (it needs the network once)"
            )
        if code == "3D000":  # invalid_catalog_name
            named = f' "{database}"' if database else ""
            fix = f"`createdb {database}`" if database else "`createdb <name>`"
            return (
                f"PostgreSQL is running, but the database{named} does not exist "
                f"— create it once with {fix}"
            )
        if code in ("28P01", "28000"):  # invalid_password / invalid_authorization
            return (
                f"PostgreSQL at {host}:{port} rejected the credentials in "
                "DATABASE_URL — fix them, or set DATABASE_URL to a role that "
                "can reach this project's database"
            )
        if code in ("ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "ETIMEDOUT"):
            return (
                f"cannot reach PostgreSQL at {host}:{port} ({code}) — start it, "
                "or point DATABASE_URL at a server that is running"
            )
        detail = f" ({code})" if code else ""
        return (
            f"the project cannot reach its database{detail} — {message}"
            if message
            else f"the project cannot reach its database{detail}"
        )

    # The probe itself. Kept as a `node -e` string rather than a file written
    # into the project: readiness is a READ, and dropping a script into someone's
    # repo to answer a question about it is the side effect `ProjectSpec.from_disk`
    # refuses to have. `require` resolves from cwd under `-e`, which is why the
    # subprocess runs with `cwd=root` — that is what reaches the project's own
    # `node_modules` and its own `db.js`.
    _PROBE_SCRIPT = """
"use strict";
function say(o) { console.log("CODER_PROBE " + JSON.stringify(o)); }
let url = process.env.DATABASE_URL || "";
// An ABSENT db.js is fine -- an adopted repo may configure the URL another way,
// so fall through to the environment. A db.js that is PRESENT and throws is a
// CODE defect, and must not be reported as an environment one: that would skip
// the smoke test which exists to catch it. The two are told apart rather than
// collapsed, so the rule holds even when DATABASE_URL is set.
let present = true;
try { require.resolve("./db"); } catch (e) { present = false; }
if (present) {
  try {
    url = require("./db").DATABASE_URL || url;
  } catch (e) {
    say({ ok: false, code: "CODER_BAD_DBJS", message: String(e.message || "").split("\\n")[0] });
    process.exit(2);
  }
}
if (!url) { say({ ok: false, code: "CODER_NO_URL" }); process.exit(2); }
let where = { host: "localhost", port: 5432, database: "" };
try {
  const u = new URL(url);
  where = {
    host: u.hostname || "localhost",
    port: Number(u.port || 5432),
    database: decodeURIComponent((u.pathname || "").replace(/^\\//, "")),
  };
} catch (e) {}
let pg;
try { pg = require("pg"); }
catch (e) { say(Object.assign({ ok: false, code: "CODER_NO_PG" }, where)); process.exit(2); }
const client = new pg.Client({
  connectionString: url,
  connectionTimeoutMillis: TIMEOUT_MS,
});
client
  .connect()
  .then(function () { return client.query("SELECT 1"); })
  .then(function () { say(Object.assign({ ok: true }, where)); process.exit(0); })
  .catch(function (e) {
    say(Object.assign(
      { ok: false, code: String(e.code || ""), message: String(e.message || "").split("\\n")[0] },
      where
    ));
    process.exit(1);
  });
"""

    def _probe_database(self, root: Path) -> dict | None:
        """Run the probe and return its payload, or None when it said nothing.

        None is "we could not find out", and every caller reads it as such.
        """
        import json
        import subprocess

        timeout = max(1.0, float(getattr(settings, "db_probe_timeout", 6.0)))
        script = self._PROBE_SCRIPT.replace("TIMEOUT_MS", str(int(timeout * 1000)))
        try:
            proc = subprocess.run(
                ["node", "-e", script],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout + 3.0,
            )
        except Exception:
            # A probe that cannot run tells us nothing about the database.
            logger.debug("database probe did not run", exc_info=True)
            return None

        for line in (proc.stdout or "").splitlines():
            if line.startswith("CODER_PROBE "):
                try:
                    payload = json.loads(line[len("CODER_PROBE ") :])
                except ValueError:
                    return None
                if not isinstance(payload, dict):
                    return None
                if payload.get("code") in _CANNOT_TELL:
                    # Either there is no connection string to try, or `db.js`
                    # itself will not load — and that second one is a CODE
                    # defect. Reporting either as an environment problem would
                    # skip the smoke test that exists to catch them.
                    return None
                return payload
        return None

    @staticmethod
    def _db_endpoint() -> tuple[str, int]:
        """`(host, port)` from DATABASE_URL, else the local default."""
        import os
        from urllib.parse import urlsplit

        url = os.environ.get("DATABASE_URL", "")
        if url:
            try:
                parts = urlsplit(url)
                return parts.hostname or "localhost", int(parts.port or 5432)
            except (ValueError, TypeError):
                logger.debug("could not parse DATABASE_URL", exc_info=True)
        return "localhost", 5432

    def _postgres_listening(self) -> bool:
        import socket

        host, port = self._db_endpoint()
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    # Phase N6, and N5's probe reused wholesale — same transport (`node -e` with
    # the project's own `pg`), same connection string (the project's own db.js),
    # same "say nothing rather than guess" failure mode. Reading the schema with
    # a *different* connection than the one the app uses would be measuring a
    # different database, which is the trap `database_reason` already avoids.
    _SCHEMA_SCRIPT = """
"use strict";
function say(o) { console.log("CODER_SCHEMA " + JSON.stringify(o)); }
let url = process.env.DATABASE_URL || "";
let present = true;
try { require.resolve("./db"); } catch (e) { present = false; }
if (present) { try { url = require("./db").DATABASE_URL || url; } catch (e) {} }
if (!url) { process.exit(2); }
let pg;
try { pg = require("pg"); } catch (e) { process.exit(2); }
const client = new pg.Client({
  connectionString: url,
  connectionTimeoutMillis: TIMEOUT_MS,
});
client
  .connect()
  .then(function () {
    return client.query(
      "SELECT table_name, column_name FROM information_schema.columns " +
      "WHERE table_schema = 'public'"
    );
  })
  .then(function (res) {
    const out = {};
    for (const row of res.rows) {
      (out[row.table_name] = out[row.table_name] || []).push(row.column_name);
    }
    say(out);
    process.exit(0);
  })
  .catch(function (e) { process.exit(1); });
"""

    def table_columns(self, root: Path) -> dict[str, set[str]] | None:
        """What PostgreSQL REALLY has: `{table: {column, ...}}`, or None.

        The Flask adapter answers this from the sqlite file; there is no file
        here, so it is a query — run the same way `database_reason` runs its
        `SELECT 1`, for the same reason.

        None means *could not read* and is never the same answer as "no tables":
        reporting an unreachable database as an empty schema would turn an
        environment problem into "the build created no tables", which is the
        exact misattribution this whole gate exists to prevent.
        """
        import json
        import subprocess

        timeout = max(1.0, float(getattr(settings, "db_probe_timeout", 6.0)))
        script = self._SCHEMA_SCRIPT.replace("TIMEOUT_MS", str(int(timeout * 1000)))
        try:
            proc = subprocess.run(
                ["node", "-e", script],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout + 3.0,
            )
        except Exception:
            logger.debug("schema read did not run", exc_info=True)
            return None

        for line in (proc.stdout or "").splitlines():
            if line.startswith("CODER_SCHEMA "):
                try:
                    payload = json.loads(line[len("CODER_SCHEMA ") :])
                except ValueError:
                    return None
                if not isinstance(payload, dict):
                    return None
                return {str(t): set(map(str, c or ())) for t, c in payload.items()}
        return None

    def run_command(self, entry: str | Path) -> list[str]:
        return ["node", Path(entry).name]

    def seed_command(self) -> list[str] | None:
        """`node seed.js`, run once after a build.

        The same deliberate exception to "never execute generated code" the
        Flask stack makes, and it holds for the same reason and only for that
        reason: since phase N3 `seed.js` and `db.js`'s schema are written by
        `crud_node.py`, not by the model. `_seed_demo_data` only runs this when
        the data layer really was generated this turn.
        """
        return ["node", "seed.js"]

    # -- reading what was written ----------------------------------------

    def routes_from_source(self, source: str) -> list[tuple[str, str, str, str]]:
        """`(method, path, handler_name, view)` for each Express route.

        The handler "name" is synthesized from the path (Express handlers are
        usually anonymous arrow functions), which is enough for the spec's
        route→view mapping. The view is the first `res.render("x")` that follows
        the route call and precedes the next one — the same "stop at the next
        decorator, not the next def" lesson `templatedeps.view_bodies` learned.
        """
        text = source or ""
        matches = list(_ROUTE_RE.finditer(text))
        out: list[tuple[str, str, str, str]] = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end() : end]
            rendered = _RENDER_RE.search(body)
            view = rendered.group("tpl") if rendered else ""
            path = match.group("path")
            method = match.group("method").upper()
            handler = "index" if path == "/" else re.sub(r"\W+", "_", path).strip("_")
            out.append((method, path, handler or "index", view))
        return out

    def check_links(self, text: str, routes) -> tuple[str, list, list]:
        """Phase N4: an EJS view names a route by its PATH, not by a view name.

        Same defect class as W2's `url_for` check and the same rules — near miss
        repointed only when exactly ONE route matches, everything else reported
        — but the lookup is a path, so it has to allow for `:id` segments.

        Returns ``(text, fixes, problems)``.
        """
        fixed, fixes = fix_link_targets(text, routes)
        problems = [
            f"link to {link} — no route serves it"
            for link in unresolved_links(fixed, routes)
        ]
        return fixed, fixes, problems + form_method_mismatches_by_path(fixed, routes)

    def source_is_valid(self, filename: str, source: str) -> bool:
        """The tooling-free content guard only — there is no in-process JS parser.

        `verify.check_file` still runs `node --check` on the ordinary write
        path, so a syntax error a deterministic pass introduced is caught there
        rather than here. What this DOES catch is the failure that guard exists
        for: prose or an HTML document written into a `.js` file.
        """
        suffix = Path(filename).suffix.lower()
        ok, _error = check_text(source, suffix, str(filename))
        return ok

    def write_source_if_valid(self, path: Path, source: str) -> bool:
        path = Path(path)
        if not self.source_is_valid(path.name, source):
            logger.warning("declined to write invalid source to %s", path.name)
            return False
        try:
            path.write_text(source, encoding="utf-8", newline="\n")
            return True
        except Exception:
            logger.warning("could not write %s", path.name, exc_info=True)
            return False

    def restore_entry_route(self, source: str) -> tuple[str, bool]:
        """Put the `/` route back into server.js when generation removed it.

        The same measured failure as Flask's `restore_index_route`: a 7B asked
        to add routes answers with a block that replaces the one it was supposed
        to add to, and the finished site 404s on its own front page.

        Conservative in the same way — it declines rather than guesses when the
        file is not a recognisable Express app, when `/` is still routed, or
        when there is nothing to anchor the insertion to.
        """
        text = source or ""
        if not _ANY_ROUTE_RE.search(text):
            return source, False  # not a route file — nothing to reason about
        if _INDEX_ROUTE_RE.search(text):
            return source, False  # still there
        cut = _insertion_point(text)
        if cut is None:
            return source, False  # can't place it safely
        return text[:cut].rstrip("\n") + "\n" + _INDEX_ROUTE_SNIPPET + text[cut:], True

    def restore_routes(self, source: str, missing) -> tuple[str, list[str]]:
        """Re-add GET routes an amendment deleted, as Express handlers.

        `impact.restore_page_routes`' rule, one stack over: a page route's whole
        body is a `res.render`, so it can be restored exactly. A POST handler's
        body is domain logic and inventing it would be generation, not repair —
        those are reported by the caller instead.

        Placed above the terminal handlers for `_insertion_point`'s reason: a
        route below the 404 catch-all is never reached, so "restoring" it there
        would report success and change nothing.
        """
        text = source or ""
        cut = _insertion_point(text)
        if cut is None:
            return source, []
        live = {(m, p) for m, p, _v, _t in self.routes_from_source(text)}

        blocks: list[str] = []
        restored: list[str] = []
        for endpoint in missing:
            if endpoint.method != "GET" or not endpoint.template:
                continue
            if (endpoint.method, endpoint.path) in live:
                continue
            # Express names a view WITHOUT its extension.
            view = Path(endpoint.template).name
            if view.endswith(self.template_ext):
                view = view[: -len(self.template_ext)]
            blocks.append(
                f'\napp.get("{endpoint.path}", (req, res) => {{\n'
                f"  // Restored by Coder — this page existed before the last change.\n"
                f'  res.render("{view}");\n'
                f"}});\n"
            )
            restored.append(endpoint.path)
        if not blocks:
            return source, []
        return text[:cut].rstrip("\n") + "\n" + "".join(blocks) + "\n" + text[cut:], (
            restored
        )

    def orphan_templates(self, root: Path) -> list[str]:
        """Views that are full `<html>` documents instead of layout fragments.

        No marker to look for, unlike Jinja's `{% extends %}`: a correctly
        shaped EJS view carries nothing at all, because `express-ejs-layouts`
        wraps it. So any view containing `<html>` is an orphan. `layout.ejs` and
        partials (`_*.ejs`) are excluded — the layout is *supposed* to be a
        document.
        """
        return _scaffold.documents_without_layout(
            root,
            self.template_dir,
            self.template_ext,
            skip=("layout.ejs",),
            skip_prefixes=("_",),
        )

    def convert_template(self, source: str) -> tuple[str, bool]:
        """Rewrite a full `<html>` view as the fragment the layout wraps.

        Deterministic: lift the `<body>` contents and drop the chrome
        `layout.ejs` already renders. Declines — leaving the file for the caller
        to report — whenever nothing would survive.
        """
        title, inner = _scaffold.document_inner(source or "")
        if not inner:
            return source, False
        head = (
            "<%# Converted by Coder: this view was a full HTML document, which "
            "renders a second navbar. layout.ejs owns the document. %>\n"
        )
        # The title is a layout concern; carry it as a comment rather than
        # dropping it silently, so the route can pass it as a local.
        if title:
            head += (
                f"<%# Page title was: {title} — pass it as `title` from the route. %>\n"
            )
        return head + "\n" + inner + "\n", True

    def build_template_graph(self, root: Path):
        """The project's EJS edges (Phase N4) — same graph, different parser."""
        from app.agent.templatedeps import build_graph, parse_ejs_template

        return build_graph(
            root,
            self.entry_file,
            template_dir=self.template_dir,
            template_ext=self.template_ext,
            parser=parse_ejs_template,
            routes_reader=self.routes_from_source,
        )

    def template_edit_region(self, filename: str, text: str):
        """None — EJS has no block structure to scope an edit to (phase N4).

        None is the existing "use the whole-file path" answer, so this is not a
        new behaviour, only the absence of Phase W3's improvement.
        """
        return None

    # -- prompt blocks ----------------------------------------------------

    def ui_context(self) -> str:
        """The prompt block naming the components generation is expected to use.

        Same rule `crud.api_context()` taught, same component NAMES as the Flask
        block: shipping a component sheet without telling the model it exists
        does not stop it writing its own `.product-card`, it just means the site
        has two design systems.
        """
        return (
            "## Page components — ALREADY WRITTEN, use them\n"
            "`public/css/style.css` defines the site's components and `ui.js` "
            "defines the markup for them. Do NOT write new CSS, and do NOT "
            "hand-write a table, a form field or an empty state.\n"
            "- `ui` is available in every view already (server.js sets "
            "`app.locals.ui`). Call the helpers with `<%- ... %>` — they return "
            "HTML, so `<%= %>` would print the tags as text.\n"
            "- **Helpers** (call as `<%- ui.name(...) %>`):\n"
            "  - `page_header(title, action_url, action_label, subtitle)` — the "
            'page\'s `<h1>` plus an optional button, e.g. "Add product".\n'
            "  - `table(rows, columns, empty)` — a list of rows; `columns` are "
            "real column names, in display order. Use this for EVERY listing "
            "page.\n"
            "  - `card(title, body, href, image, meta)` — one item in a "
            '`<div class="grid">`.\n'
            "  - `field(name, label, type, value, required, placeholder, hint)` "
            "— one labelled form control; `type='textarea'` renders a textarea, "
            "`type='file'` an upload.\n"
            "  - `badge(text, kind)` — a small status label.\n"
            "  - `empty_state(message, action_url, action_label)` — shown "
            "instead of an empty list.\n"
            "  - `flash_messages(messages)` — only if a page needs them "
            "somewhere other than the top; layout.ejs already renders them.\n"
            "- Every helper escapes what it is given, so a value from the "
            "database is safe to pass straight in.\n"
            "- **Classes** for anything the helpers don't cover: `.grid` "
            "(auto-fitting card grid), `.stack` (vertical rhythm), `.cluster` "
            "(inline row), `.card`, `.table-wrap` + `.table`, `.field`, "
            "`.form-narrow`, `.button` / `.button-secondary` / `.button-danger` "
            "/ `.button-small`, `.alert-success` / `-error` / `-warning` / "
            "`-info`, `.empty`, `.badge`, `.breadcrumb`, `.pagination`, "
            "`.sidebar-layout`, `.page-header`, `.hero`, `.lede`, "
            "`.visually-hidden`.\n"
            "- A wide table MUST sit inside `.table-wrap` or the page scrolls "
            "sideways on a phone — `ui.table()` already does this for you.\n"
            "- Colours and fonts are the CSS variables in "
            "`public/css/theme.css` (`var(--color-accent)`, "
            "`var(--font-heading)`, …). Never write a hex code or a font family "
            "into a view or a new stylesheet: a value that isn't a variable is "
            "the one thing a restyle cannot reach."
        )

    def scaffold_context(self, written) -> str:
        """What the scaffold already provides, so generation adds rather than reinvents."""
        if not written:
            return ""
        return (
            "## Project skeleton — ALREADY CREATED, do not rewrite it\n"
            "A working Express app is already on disk and already runs. You are "
            "adding this project's own features to it, not building it from "
            "scratch:\n"
            "- **ADD to these files; never delete what is already in them.** In "
            "particular `server.js` already defines the `/` route — keep it and "
            "add your new ones alongside it, ABOVE the 404 handler at the "
            "bottom (a route added below it is never reached). The view it "
            "renders, `views/index.ejs`, is only a PLACEHOLDER: the home page "
            "itself still has to be written.\n"
            "- `server.js` holds routes ONLY. The Express app, the view engine, "
            "the layout, the static mount and the `db.initDb()` call already "
            "exist. Handlers are `async` and `await` the model helpers.\n"
            "- `db.js` owns the connection pool and the schema. Use "
            "`const { getPool } = require('./db')`; add `CREATE TABLE IF NOT "
            "EXISTS` statements inside `initDb()`, and use `await "
            "ensureColumn(client, table, column, decl)` for a field added "
            "later. Never create a Pool anywhere else.\n"
            "- `models.js` owns every query, one exported function per "
            "operation, always with `$1, $2, …` parameters — never a template "
            "literal with a value in it. Routes call these helpers; routes "
            "never write SQL. PostgreSQL has no `lastrowid`, so an insert that "
            "needs the new id ends with `RETURNING id`.\n"
            "- `views/layout.ejs` owns the navigation and the page shell. EVERY "
            "view is a FRAGMENT that it wraps — never write `<html>`, `<head>` "
            "or a `<nav>` in a view, and never copy the nav into one; add links "
            'to the `<nav class="site-nav">` in layout.ejs instead.\n'
            "- `public/css/style.css` (components) and `public/css/theme.css` "
            "(colour and font variables) are the ONLY stylesheets, both linked "
            "by layout.ejs. Never add a third one, and never write a hex code "
            "or a font family into a view — use `var(--color-accent)`, "
            "`var(--font-heading)` and the other variables. See the components "
            "section below.\n"
            "- `seed.js` holds demo rows so no page is ever empty.\n"
            "- `package.json`, `Procfile`, `ui.js` and `.gitignore` are done. "
            "Leave them alone."
        )


NODE = NodeAdapter()
