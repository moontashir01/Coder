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
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from app.agent import crud_node as _crud_node
from app.agent import scaffold as _scaffold
from app.agent.crud_node import (
    adds_column,
    api_context,
    apply_block,
    creates_table,
    js_strings,
    js_without_comments,
    models_source,
    needs_password_helper,
    password_helper_source,
    seed_source,
    table_block,
)
from app.agent.ejslocals import render_locals, repair_view_locals
from app.agent.jsdeps import fix_db_bootstrap
from app.agent.jsimports import (
    add_missing_requires,
    middleware_gaps,
    plaintext_password_writes,
)
from app.agent.projectspec import POSTGRES
from app.agent.verify import (
    check_file,
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

# `CREATE DATABASE` cannot take a bound parameter, so the name is interpolated —
# and therefore has to be an identifier this project generated, never anything
# that arrived from outside. `scaffold.project_slug` emits `[a-z0-9-]`; this is
# the same guard `projectspec._ident` applies to a table name, for the same
# reason. Anything else is refused rather than quoted and hoped for.
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")


def _last_error_line(stderr: str | None) -> str:
    """One useful line out of a failed command's stderr.

    `apprunner._loudest_line`'s rule: prefer a line that names an error, fall
    back to the last line, and never return the whole log — this is printed to
    someone who asked for a URL, not a build transcript.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if "error" in line.lower():
            return line[:200]
    return lines[-1][:200] if lines else "no output"


_INDEX_ROUTE_SNIPPET = """
app.get("/", (req, res) => {
  // Home page. Restored by Coder: generation had removed it.
  res.render("index", { title: "Home" });
});

"""

_EXPORTS_RE = re.compile(r"\bmodule\s*\.\s*exports\b")

# The scaffold's terminal section, which is what makes server.js an app rather
# than a list of handlers. Restored verbatim when a rewrite drops it — see
# `NodeAdapter.restore_boot_block` for why it has to be checked at all.
_NOT_FOUND_SNIPPET = """
// Anything that matched no route above. Restored by Coder.
app.use((req, res) => {
  res.status(404).render("index", {
    title: "Not found",
    notFound: req.originalUrl,
  });
});

// Any error a route throws lands here. Logged in full, shown short.
app.use((err, req, res, _next) => {
  console.error(err);
  res.status(500).send("Internal Server Error");
});
"""

# Written against `process.env.PORT` rather than the scaffold's `PORT` const,
# and the `db` half is only used when the file really does require ./db: this
# runs on a file a rewrite has already damaged, so it must not assume that any
# particular binding above it survived.
_LISTEN_SNIPPET = """
// Create the database schema and apply any new columns, then serve. Restored
// by Coder: a rewrite had removed it, so `node server.js` exited silently
// without ever listening.
db.initDb()
  .then(() => {
    app.listen(process.env.PORT || 3000, () => {
      console.log(`listening on http://127.0.0.1:${process.env.PORT || 3000}`);
    });
  })
  .catch((err) => {
    console.error("Could not initialise the database:", err.message);
    console.error(
      "Start PostgreSQL and create the database, or set DATABASE_URL. See db.js."
    );
    process.exit(1);
  });
"""

_LISTEN_SNIPPET_NO_DB = """
// Restored by Coder: a rewrite had removed the listen call, so `node server.js`
// exited silently without ever serving.
app.listen(process.env.PORT || 3000, () => {
  console.log(`listening on http://127.0.0.1:${process.env.PORT || 3000}`);
});
"""

_DB_REQUIRE_RE = re.compile(r"""require\(\s*["'`]\./db["'`]\s*\)""")

_EXPORTS_SNIPPET = """
module.exports = app;
"""


# Strings/templates, for the bracket-balance guard below. Comments are gone
# by then (`js_without_comments`), so a naive literal regex is safe here.
_JS_LITERAL_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")


def _brackets_balance(block: str) -> bool:
    """Do `{}`, `()` and `[]` pair up in this slice, ignoring strings/comments?

    Not a parser and not trying to be — `write_source_if_valid` runs the real
    `node --check`. This is the cheap guard that stops an unbalanced slice from
    being RECORDED in the first place, so a later reinstatement cannot even
    propose it.
    """
    # Comments first (that walk knows a `//` inside a URL from a real comment),
    # then blank the literals it left intact — a `{` inside a string is text.
    text = _JS_LITERAL_RE.sub(
        lambda m: " " * len(m.group(0)), js_without_comments(block or "")
    )
    depth = {"{": 0, "(": 0, "[": 0}
    closing = {"}": "{", ")": "(", "]": "["}
    for char in text:
        if char in depth:
            depth[char] += 1
        elif char in closing:
            depth[closing[char]] -= 1
            if depth[closing[char]] < 0:
                return False
    return not any(depth.values())


def _shadows(param_path: str, literal_path: str) -> bool:
    """Would Express match `param_path` for a request meant for `literal_path`?

    True only for the case that actually bites: same segment count, every
    literal segment equal, and at least one `:param` standing where the other
    path has a fixed word — `/items/:id` against `/items/new`.
    """
    a = (param_path or "").strip("/").split("/")
    b = (literal_path or "").strip("/").split("/")
    if len(a) != len(b) or any(seg.startswith(":") for seg in b):
        return False
    saw_param = False
    for seg_a, seg_b in zip(a, b):
        if seg_a.startswith(":"):
            saw_param = True
        elif seg_a != seg_b:
            return False
    return saw_param


# `<%- include("../layout", { … }) %>` — a view wrapping itself in the shell
# that already wraps it.
_LAYOUT_INCLUDE_RE = re.compile(
    r"""<%[-=]?\s*include\s*\(\s*["'][^"']*layout[^"']*["'][^%]*%>\s*"""
)

# `res.render("<view>"` — the name only, quotes excluded.
_RENDER_NAME_RE = re.compile(
    r"""res\s*\.\s*render\s*\(\s*["'](?P<view>[^"']+)["']"""
)


def _function_block(text: str, name: str) -> str:
    """`async function name(…) { … }` out of ``text``, doc comment included."""
    match = re.search(
        r"(?:^/\*\*(?:(?!\*/).)*\*/\s*)?^(?:async\s+)?function\s+"
        + re.escape(name)
        + r"\s*\(",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    brace = text.find("{", match.end())
    if brace == -1:
        return ""
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    return ""


def _names_route(message: str, method: str, path: str) -> bool:
    """Does ``message`` name this route?

    The path has to appear as a whole segment path — `/items` must not match
    `/items/new`, or "fix /items" would edit two handlers and pick one. A method
    word narrows it when the message uses one, which is how `POST /items/new`
    and `GET /items/new` are told apart.
    """
    low = message.lower()
    if path.lower() not in low:
        return False
    # `/items` inside `/items/new` is a prefix, not a mention: require the
    # character after the match to end the path.
    at = low.index(path.lower())
    after = low[at + len(path) :][:1]
    if after and (after.isalnum() or after in "/-_"):
        return False
    spoken = re.findall(r"\b(get|post|put|patch|delete)\b", low)
    if spoken and method.lower() not in spoken:
        return False
    return True


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
    # How this stack spells "the id goes here" in a route path.
    route_param = ":id"
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
        "the startup block is an invariant: a rewrite that ends server.js at "
        "the last handler is repaired, because `node --check` accepts such a "
        "file and the app then exits without ever listening",
        "routes are restored from the source they had earlier in the turn, so a "
        "POST full of domain logic comes back as written rather than being "
        "reported as lost",
        "a parameterised route that shadows a literal sibling is moved below it "
        "— Express matches in registration order, so /items/:id above "
        "/items/new swallows the create form",
        "undefined names at runtime are found the way Flask finds them "
        "(`jsimports.py`, tree-sitter): a `require` that was never written, a "
        "`req.session` with no session middleware mounted, and a raw form "
        "password on its way into storage — none of which `node --check` sees",
        "the JavaScript INSIDE an .ejs view is syntax-checked, not just the "
        "markup around it: `<%- users.forEach(u => { %>` parses as balanced "
        "markup and throws at render time",
        "calls between the project's own modules are checked (jsdeps.py): "
        "server.js calling something db.js does not export is a startup crash "
        "no syntax check can see",
        "views are checked against what their routes actually pass: EJS "
        "compiles to `with (locals)`, so a free identifier is a render-time "
        "ReferenceError and a 500 on a page this build wrote",
    )
    gaps = (
        "missing-require repair only binds Node builtins and this project's own "
        "modules: an undefined npm package (`bcrypt`) is REPORTED, never "
        "required, because requiring something absent from node_modules turns "
        "one broken route into an app that will not boot",
        "no duplicate-definition check: `pyimports.duplicate_definitions` is "
        "`ast`-based, and a regex guess would report the same function twice "
        "for every if/else branch",
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
        # An entity the delta INTRODUCED has no table yet, and `ALTER TABLE` on
        # a table that does not exist is a hard failure inside `initDb()` —
        # which the generated app treats as fatal, so the whole site stops
        # booting. Measured on turn 2 of the OpenBazaar build: the delta added a
        # `seller` entity, db.js got two `ensureColumn(client, "sellers", …)`
        # calls and nothing that creates `sellers`, and every page went down
        # with `relation "sellers" does not exist`. A new table is a CREATE, not
        # an ALTER; `creates_table` reads string literals for the `_creates_table`
        # trap's reason.
        created: list[str] = []
        for entity in spec.entities:
            if creates_table(source, entity.table):
                continue
            if not any(f.added_in > since for f in entity.fields):
                continue  # not this revision's business
            statement = entity.to_ddl(POSTGRES)
            indented = "\n".join("      " + line for line in statement.splitlines())
            created.append(f"    await client.query(`\n{indented}\n    `);")

        pending = [
            (entity.table, f.name, f.type)
            for entity in spec.entities
            for f in entity.fields
            if f.added_in > since
            and not f.pk
            and not adds_column(source, entity.table, f.name)
            # …and never a column of a table this same block is creating: the
            # CREATE already has every one of them.
            and creates_table(source, entity.table)
        ]
        if not pending and not created:
            return ""
        block = "\n".join(
            created
            + [
                f"    {POSTGRES.migration_call(table, column, kind)};"
                for table, column, kind in pending
            ]
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
            # Named with the download, because this is the one blocker `/run`'s
            # auto-setup cannot clear for you: installing a runtime needs an
            # installer, and often an administrator.
            return (
                "Node.js is not installed — get it from https://nodejs.org, "
                "then run this again"
            )
        if not (Path(root) / "node_modules").is_dir():
            return (
                "`node_modules` is missing — run `npm install` in the project "
                "(it needs the network once)"
            )
        if not self._postgres_listening():
            host, port = self._db_endpoint()
            return (
                f"PostgreSQL is not running on {host}:{port} — start it (on "
                "Windows: Services → postgresql), then run this again. The "
                "project's database is created for you once it answers"
            )
        return self.database_reason(root)

    def autosetup(self, root: Path, log=None) -> list[str]:
        """Do the setup this project needs, and return what was really done.

        `readiness` names why the app cannot start. Three of the reasons it names
        are commands — `npm install`, `createdb`, the seed — and typing them in
        another terminal, in the right order, is the entire difference between a
        URL and a wall of instructions for someone who does not use a terminal.
        This performs exactly those three.

        Five rules, and each one is a failure this would otherwise cause:

        * **It only ever does what `readiness` would have TOLD you to do.**
          Nothing here is a repair of the generated code, and nothing is done
          speculatively — `node_modules` is installed because it is absent, the
          database is created because PostgreSQL said `3D000`.
        * **A refused login is never answered by creating a database.** Only
          `3D000` (invalid_catalog_name) reaches `_create_database`; `28P01` is a
          password this cannot guess, and creating something under a working
          *other* credential would hide that.
        * **Uncertainty does nothing.** `_probe_database` returning None means we
          could not find out — the same "cannot tell" that must never replace the
          smoke test — so no database is created on a guess.
        * **What could not be done is not reported as done.** Every line returned
          describes a step that finished; a failed `npm install` returns its
          error, and `readiness` then names the same blocker it named before.
        * **It never installs Node, starts a service, or edits `.env`.** Those
          need an installer or an administrator. They stay `readiness`'s job.

        ``log`` is called with progress lines for the steps that take real time
        (an `npm install` on a cold cache is ~30s of silence otherwise).
        """
        import shutil

        root = Path(root)
        done: list[str] = []

        def say(line: str) -> None:
            if log is not None:
                try:
                    log(line)
                except Exception:  # a progress line must never break setup
                    logger.debug("autosetup log hook raised", exc_info=True)

        # Nothing below can be fixed without an installer, so do not start.
        if not shutil.which("node"):
            return done
        # Not a scaffolded Node project: no manifest, nothing to install. An
        # adopted repo with its own tooling is not ours to run npm in.
        if not (root / "package.json").is_file():
            return done

        if not (root / "node_modules").is_dir():
            say("installing dependencies (npm install) — this needs the network, once")
            ok, note = self._npm_install(root)
            done.append(note)
            if not ok:
                return done

        # A database cannot be created through a server that is not answering,
        # and starting one is not something this may do.
        if not self._postgres_listening():
            return done

        payload = self._probe_database(root)
        if payload is None or payload.get("ok"):
            return done
        if str(payload.get("code") or "") != "3D000":
            return done

        name = str(payload.get("database") or "")
        if not _DB_NAME_RE.match(name):
            return done

        say(f"creating the database {name}")
        ok, note = self._create_database(root, name)
        done.append(note)
        if not ok:
            return done

        # `seed.js` calls `initDb()` first, so this one command both creates the
        # tables in the brand-new database and puts demo rows in them. Only after
        # a database we just created: re-seeding one that already existed would
        # write rows into someone's data.
        seeded = self._seed(root, say)
        if seeded:
            done.append(seeded)
        return done

    def _npm_install(self, root: Path) -> tuple[bool, str]:
        """`npm install` in ``root``. Returns ``(ok, one line about it)``."""
        import shutil
        import subprocess

        npm = shutil.which("npm")
        if npm is None:
            return False, "npm is not on PATH — install Node.js, which includes it"

        timeout = max(30.0, float(getattr(settings, "npm_install_timeout", 300.0)))
        try:
            proc = subprocess.run(
                [npm, "install"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, (
                f"npm install did not finish within {timeout:.0f}s — run it "
                "yourself in the project folder and check the network"
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("npm install did not run", exc_info=True)
            return False, f"npm install could not be started: {e}"

        if proc.returncode != 0:
            return False, f"npm install failed: {_last_error_line(proc.stderr)}"
        if not (root / "node_modules").is_dir():
            # Exit code 0 with nothing installed: report the state on disk, not
            # the exit code, since the next step depends on the directory.
            return False, "npm install reported success but node_modules is absent"
        return True, "installed the project's dependencies (npm install)"

    # `CREATE DATABASE` has to run against a database that already exists, so
    # this connects to the maintenance database `postgres` on the SAME server,
    # with the SAME credentials, taken from the project's own `db.js` — the rule
    # `_probe_database` follows and for its reason: a connection string we
    # guessed could succeed where the app fails, or fail where the app works.
    _CREATE_DB_SCRIPT = """
"use strict";
function say(o) { console.log("CODER_SETUP " + JSON.stringify(o)); }
let url = process.env.DATABASE_URL || "";
try { url = require("./db").DATABASE_URL || url; } catch (e) {}
if (!url) { say({ ok: false, message: "no connection string" }); process.exit(2); }
let admin;
try {
  const u = new URL(url);
  u.pathname = "/postgres";
  admin = u.toString();
} catch (e) { say({ ok: false, message: "unreadable connection string" }); process.exit(2); }
let pg;
try { pg = require("pg"); }
catch (e) { say({ ok: false, message: "the pg package is not installed" }); process.exit(2); }
const client = new pg.Client({
  connectionString: admin,
  connectionTimeoutMillis: TIMEOUT_MS,
});
client
  .connect()
  .then(function () { return client.query('CREATE DATABASE "DBNAME"'); })
  .then(function () { say({ ok: true }); process.exit(0); })
  .catch(function (e) {
    // 42P04 = duplicate_database. Something else created it in the meantime,
    // which is the state we wanted; that is a success, not an error.
    if (String(e.code || "") === "42P04") { say({ ok: true, existed: true }); process.exit(0); }
    say({ ok: false, code: String(e.code || ""), message: String(e.message || "").split("\\n")[0] });
    process.exit(1);
  });
"""

    def _create_database(self, root: Path, name: str) -> tuple[bool, str]:
        """`CREATE DATABASE <name>` through the project's own `pg`.

        ``name`` must already have passed `_DB_NAME_RE` — it is interpolated into
        SQL, because `CREATE DATABASE` takes no bound parameters.
        """
        import json
        import subprocess

        if not _DB_NAME_RE.match(name):  # pragma: no cover - caller checks first
            return False, f"refusing to create a database named {name!r}"

        timeout = max(1.0, float(getattr(settings, "db_probe_timeout", 6.0)))
        script = self._CREATE_DB_SCRIPT.replace(
            "TIMEOUT_MS", str(int(timeout * 1000))
        ).replace("DBNAME", name)
        try:
            proc = subprocess.run(
                ["node", "-e", script],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout + 3.0,
            )
        except Exception:
            logger.debug("create-database did not run", exc_info=True)
            return False, f"could not create the database {name}"

        payload: dict | None = None
        for line in (proc.stdout or "").splitlines():
            if line.startswith("CODER_SETUP "):
                try:
                    parsed = json.loads(line[len("CODER_SETUP ") :])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    payload = parsed
                break

        if payload is None:
            return False, f"could not create the database {name}"
        if payload.get("ok"):
            if payload.get("existed"):
                return True, f"the database {name} already existed"
            return True, f"created the database {name}"

        message = str(payload.get("message") or "").strip()
        detail = f" — {message}" if message else ""
        return False, f"could not create the database {name}{detail}"

    def _seed(self, root: Path, say) -> str:
        """Run `node seed.js`, which calls `initDb()` first. Best-effort.

        Returns a line when it did something, "" when it did not. A seed that
        fails is not a reason to withhold the URL: the app creates its own tables
        on startup, so the site works — it is just empty, and `readiness` has
        nothing to say about that.
        """
        import subprocess

        command = self.seed_command()
        if not command or not (root / "seed.js").is_file():
            return ""
        say("creating the tables and demo data")
        try:
            proc = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(
                    5.0, float(getattr(settings, "smoke_test_timeout", 8.0)) * 4
                ),
            )
        except Exception:
            logger.debug("seed did not run", exc_info=True)
            return ""
        if proc.returncode != 0:
            return f"the demo data could not be loaded: {_last_error_line(proc.stderr)}"
        return "created the tables and loaded demo data"

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

    _SQL_SCRIPT = """
"use strict";
function say(o) { console.log("CODER_SQL " + JSON.stringify(o)); }
let url = process.env.DATABASE_URL || "";
try { url = require("./db").DATABASE_URL || url; } catch (e) {}
if (!url) { say({ ok: false, code: "CODER_NO_URL" }); process.exit(2); }
let pg;
try { pg = require("pg"); }
catch (e) { say({ ok: false, code: "CODER_NO_PG" }); process.exit(2); }
const payload = JSON.parse(process.env.CODER_SQL_PAYLOAD || "{}");
const client = new pg.Client({ connectionString: url, connectionTimeoutMillis: TIMEOUT_MS });
client
  .connect()
  .then(function () { return client.query(payload.sql, payload.params || []); })
  .then(function (r) { say({ ok: true, rows: r.rows || [], count: r.rowCount }); process.exit(0); })
  .catch(function (e) {
    say({ ok: false, code: String(e.code || ""), message: String(e.message || "").split("\\n")[0] });
    process.exit(1);
  });
"""

    def run_sql(self, root: Path, sql: str, params: list | None = None):
        """Run one parameterised statement against the project's own database.

        The transport is N5's: `node -e` with the project's `pg` and the URL out
        of its own `db.js`, so this measures the database the app really uses
        rather than one guessed from settings. Values are BOUND — the caller
        passes `$1`-style placeholders — and the only thing interpolated into
        `sql` by callers is an identifier they took from the spec, which
        `projectspec._ident` has already validated.

        Returns the rows as dicts, or **None for every uncertainty**: no node, no
        `pg`, no URL, a timeout, a crash, unparseable output. `smoke.py` reads
        None as "could not check" and says so — `database_reason`'s rule, and it
        matters more here, because a behaviour probe that mistook an unreachable
        database for a broken rule would send the repair loop at correct code.
        """
        import json
        import subprocess

        timeout = max(1.0, float(getattr(settings, "db_probe_timeout", 6.0)))
        script = self._SQL_SCRIPT.replace("TIMEOUT_MS", str(int(timeout * 1000)))
        env = dict(os.environ)
        env["CODER_SQL_PAYLOAD"] = json.dumps({"sql": sql, "params": params or []})
        try:
            proc = subprocess.run(
                ["node", "-e", script],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout + 3.0,
                env=env,
            )
        except Exception:
            logger.debug("run_sql: could not start node", exc_info=True)
            return None
        for line in (proc.stdout or "").splitlines():
            if not line.startswith("CODER_SQL "):
                continue
            try:
                payload = json.loads(line[len("CODER_SQL ") :])
            except Exception:
                return None
            if not payload.get("ok"):
                logger.debug("run_sql refused: %s", payload.get("code"))
                return None
            rows = payload.get("rows")
            return rows if isinstance(rows, list) else []
        return None

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
        """Write only if the result still PARSES — `node --check`, not a guess.

        This is the gate every deterministic pass leans on, and until it ran
        `node --check` it was the Flask guarantee's shadow rather than its
        equal: `_write_python_if_valid` compiles the Python it is about to
        write, so a hand-written repair that breaks `app.py` is refused, while
        here only a content guard ran ("is this HTML in a .js file?") and a
        repair that broke `server.js` shipped.

        Measured: `reinstate_routes` put back four handlers captured from a
        version of the file where they were nested inside a callback, the
        re-inserted text carried one closing brace too many, and the finished
        build died with `SyntaxError: Unexpected token '}'`. Everything
        downstream was green, because nothing between that pass and the user
        ever parsed the file.

        The check needs a path, so it writes first and RESTORES the previous
        contents when the result does not parse — the same net `_intent_repair`
        casts, one stack over. With `node` absent it degrades to the old content
        guard rather than blocking the write, because a check that cannot run
        must not become a refusal to work.
        """
        path = Path(path)
        if not self.source_is_valid(path.name, source):
            logger.warning("declined to write invalid source to %s", path.name)
            return False
        # …and for the ENTRY FILE, that it still serves what it served. Every
        # deterministic pass writes through here, and several of them rewrite
        # the file wholesale — so a pass that was asked to add one route can
        # take ten out, and the only evidence is a 404 much later. Measured on
        # the OpenBazaar build more than once, most expensively at the end:
        # `POST /register` and `POST /login` vanished from a turn whose job was
        # to fix a third handler. None of these passes has any business
        # REMOVING a route, so losing one means the rewrite went wrong.
        if path.name == self.entry_file:
            try:
                current = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                current = ""
            if current.strip():
                had = {(m, p_) for m, p_, _v, _t in self.routes_from_source(current)}
                now = {(m, p_) for m, p_, _v, _t in self.routes_from_source(source)}
                lost = had - now
                if lost:
                    logger.warning(
                        "declined a write to %s that would lose %d route(s): %s",
                        path.name,
                        len(lost),
                        ", ".join(sorted(f"{m} {p_}" for m, p_ in lost))[:200],
                    )
                    return False
        try:
            before = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.is_file()
                else None
            )
            path.write_text(source, encoding="utf-8", newline="\n")
        except Exception:
            logger.warning("could not write %s", path.name, exc_info=True)
            return False

        if shutil.which("node") is None:
            return True  # cannot tell; the ordinary write path still checks it
        ok, error = check_file(path)
        if ok:
            return True
        logger.warning("reverted %s — the rewrite broke it: %s", path.name, error)
        try:
            if before is None:
                path.unlink()
            else:
                path.write_text(before, encoding="utf-8", newline="\n")
        except Exception:
            logger.warning("could not revert %s", path.name, exc_info=True)
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

    def restore_boot_block(self, source: str) -> tuple[str, bool]:
        """Put back the tail that makes server.js an app rather than a list of
        handlers: the 404 handler, the error handler, `db.initDb()` and
        `app.listen()`.

        Measured on the OpenBazaar PRD build: `_wire_missing_endpoints` was
        asked to add ONE route, and its rewrite ended the file at the last
        handler — no `initDb`, no `listen`, no exports. Every guard passed.
        `node --check` passes, because a file of handler registrations is
        perfectly valid JavaScript; `restore_entry_route` declined, because
        `_insertion_point` anchors on the very lines that had been deleted; and
        the smoke test was skipped for want of `node_modules`. So the build
        reported "verified OK" on an app that cannot start at all.

        That is why this is checked separately from the routes: without
        `app.listen`, nothing else about the file matters, and it is the one
        defect the existing invariants were structurally unable to see. Restored
        piece by piece, so a file that kept its 404 handler but lost `listen`
        gets only what is missing.
        """
        text = source or ""
        if not _ANY_ROUTE_RE.search(text):
            return source, False  # not a route file — nothing to reason about
        additions: list[str] = []
        if not _TERMINAL_USE_RE.search(text):
            additions.append(_NOT_FOUND_SNIPPET)
        if not _LISTEN_RE.search(text):
            additions.append(
                _LISTEN_SNIPPET
                if _DB_REQUIRE_RE.search(text)
                else _LISTEN_SNIPPET_NO_DB
            )
        if not _EXPORTS_RE.search(text):
            additions.append(_EXPORTS_SNIPPET)
        if not additions:
            return source, False
        return text.rstrip("\n") + "\n" + "".join(additions), True

    def sql_literals(self, source: str) -> list[str]:
        """Where SQL may legitimately live in a `.js` file: string literals.

        `crud_node.js_strings` separates literals from comments in one walk,
        which is the only order that can tell `"postgres://host/db"` apart from
        a `//` comment. The Python default falls back to the whole raw file for
        anything `ast` cannot parse — i.e. every JavaScript file — and then
        reads the prose in the comments as SQL.
        """
        return js_strings(source)

    def render_locals(self, entry_source: str) -> dict[str, set[str]]:
        """Per view stem, the names its routes pass to `res.render`."""
        return render_locals(entry_source)

    def repair_view_locals(
        self, text: str, provided: set[str]
    ) -> tuple[str, list[str], list[str]]:
        """Blank undefined `ui.*()` arguments; report every other free name.

        EJS compiles to `with (locals)`, so a name the route did not pass is a
        ReferenceError at render time. Measured: every listing page of the
        OpenBazaar build answered 500 on `empty_state is not defined`, and no
        static check could see it.
        """
        return repair_view_locals(text, provided)

    def repair_module_calls(self, source: str, root: Path) -> tuple[str, list[str]]:
        """Point the startup call back at the function `db.js` really exports.

        The measured failure: generation rewrote server.js's tail as
        `db.setup().then(…)`, and `db.js` exports `initDb`. `node --check`
        accepts it, every route is found, `app.listen(` is present so the
        boot-block invariant holds — and the app dies on startup with
        `db.setup is not a function`.
        """
        db_file = Path(root) / "db.js"
        if not db_file.is_file():
            return source, []
        try:
            db_source = db_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return source, []
        return fix_db_bootstrap(source, db_source)

    def repair_runtime_names(
        self, filename: str, source: str, root: Path
    ) -> tuple[str, list[str], list[str]]:
        """The Flask import/password checks, for JavaScript (`jsimports.py`).

        This was the widest of the Node stack's gaps and it was invisible in the
        worst way: `node --check` passes a file full of undefined names, so the
        build reported `verified OK` and the app threw at runtime. Measured on a
        live build — `server.js` called `bcrypt.compareSync(...)` with `bcrypt`
        never required, assigned `req.session.userId` with no session middleware
        mounted, and stored the raw `password_hash` form field while the
        project's own generated `passwords.js` sat unused. Three defects, one
        file, nothing in the pipeline able to see any of them.

        Only requires from the allowlist are WRITTEN (a Node builtin, or a
        sibling module that really exports the name). A package this project may
        not have installed is reported, because `require("bcrypt")` against an
        absent package turns one broken route into an app that will not boot.
        """
        if Path(filename).suffix.lower() not in (".js", ".mjs", ".cjs"):
            return source, [], []

        fixed, added, unresolved = add_missing_requires(source, Path(root))

        reports: list[str] = []
        if unresolved:
            reports.append(
                "uses undefined name(s) at runtime — "
                + ", ".join(unresolved[:6])
                + " (require them, or remove the code that uses them)"
            )
        try:
            leaks = plaintext_password_writes(fixed, set(unresolved))
        except Exception:
            logger.debug("password check failed for %s", filename, exc_info=True)
            leaks = []
        if leaks:
            reports.append(
                "stores a password without hashing it — "
                + "; ".join(leaks[:3])
                + " (use hashPassword from ./passwords)"
            )
        try:
            reports.extend(middleware_gaps(fixed))
        except Exception:
            logger.debug("middleware check failed for %s", filename, exc_info=True)
        return fixed, added, reports

    def shadowed_routes(self, source: str) -> list[tuple[str, str, str]]:
        """`(method, param_path, literal_path)` for every route pair where the
        parameterised one is registered FIRST and therefore wins.

        Read straight off `routes_from_source`, which is the robust
        order-preserving reader, rather than off `route_blocks` — the point is
        that this question must still be answerable on a file whose shape
        `route_blocks` declines to slice. That distinction is not academic: on a
        measured build the routes were nested inside a callback, the block
        slicer correctly gave up, and `order_routes` then reported *nothing* —
        so `/bids/new` went on being served by `/bids/:id` and answered 500 with
        no complaint anywhere. Silence is the failure mode this whole file
        exists to remove.
        """
        seen: list[tuple[str, str]] = [
            (method.upper(), path)
            for method, path, _v, _t in self.routes_from_source(source or "")
        ]
        out: list[tuple[str, str, str]] = []
        for index, (method, path) in enumerate(seen):
            if ":" not in path:
                continue
            for later_method, later_path in seen[index + 1 :]:
                if later_method == method and _shadows(path, later_path):
                    out.append((method, path, later_path))
        return out

    def order_routes(self, source: str) -> tuple[str, list[str], list[str]]:
        """Move `/items/new` above `/items/:id` when a rewrite put it below.

        Express matches middleware in registration order, so a parameterised
        route registered first swallows every literal sibling: `GET /items/:id`
        above `GET /items/new` turns the create form into a lookup for an item
        whose id is the string "new" — a 404 or a 500 on a page the same build
        wrote, with nothing anywhere reporting a defect.

        Order is not something a route-adding pass can be trusted to preserve:
        `reinstate_routes` and `restore_routes` both insert at the bottom of the
        route section, which is exactly the wrong end. So it is asserted on the
        finished file instead.

        Only reorders blocks that COLLIDE — same method, same prefix, one
        literal and one parameterised. Everything else keeps the order it was
        written in, because route order carries meaning this cannot see.
        """
        text = source or ""
        collisions = self.shadowed_routes(text)
        if not collisions:
            return source, [], []
        shadowing = {(method, path) for method, path, _lit in collisions}

        spans = self._route_spans(text)
        if len(spans) < 2 or not shadowing <= {(m, p) for m, p, _s, _e in spans}:
            # The collision is real but this file's shape cannot be sliced —
            # routes nested inside a callback, most often. SAY SO. Returning
            # `[]` here is indistinguishable from "nothing was wrong", and the
            # page the parameterised route is swallowing goes on 500ing.
            return (
                source,
                [],
                [
                    f" {method} {literal} is registered BELOW "
                    f"{method} {param}, which matches it first — Express matches in "
                    "registration order, so that page is unreachable. Move it above "
                    "by hand (its routes are nested, so this could not be done "
                    "safely here)."
                    for method, param, literal in collisions
                ],
            )

        # The route SECTION is everything above the terminal handlers, and only
        # that. Slicing from the first route to the last one is what this used
        # to do, and when an earlier pass had left a route below the 404
        # handler that slice swallowed the startup block with it: rebuilding
        # the span from route text alone then deleted `db.initDb()` and
        # `app.listen`, and the app defined its handlers and exited in silence.
        # Measured on the OpenBazaar build — the repair that moved one route
        # took the whole server down.
        boundary = _insertion_point(text)
        if boundary is not None:
            below = [s for s in spans if s[2] >= boundary]
            if below:
                # A route registered after the 404 handler never matches
                # anything anyway, so lifting it is a repair in its own right.
                lifted = "".join(
                    text[s[2] : s[3]].strip("\n") + "\n\n" for s in below
                )
                for s in sorted(below, key=lambda s: s[2], reverse=True):
                    text = text[: s[2]] + text[s[3] :]
                at = _insertion_point(text)
                if at is None:
                    return source, [], []
                text = text[:at] + lifted + text[at:]
                spans = self._route_spans(text)
                boundary = _insertion_point(text)
            spans = [s for s in spans if boundary is None or s[3] <= boundary]
        if len(spans) < 2:
            return (text, sorted(f"{m} {p}" for m, p in shadowing), []) if text != source else (source, [], [])

        start, end = spans[0][2], spans[-1][3]
        # A stable sort by one bit: a route that shadows a literal sibling goes
        # to the BOTTOM of the route section, everything else keeps the order it
        # was written in. Deliberately not a full sort — route order carries
        # meaning this cannot see, so it moves only what is provably wrong.
        ordered = sorted(spans, key=lambda s: (s[0], s[1]) in shadowing)
        if ordered == spans:
            return (text, sorted(f"{m} {p}" for m, p in shadowing), []) if text != source else (source, [], [])
        body = "\n\n".join(text[s[2] : s[3]].strip("\n") for s in ordered)
        moved = sorted(f"{m} {p}" for m, p in shadowing)
        return text[:start] + body + "\n" + text[end:], moved, []

    def _route_spans(self, text: str) -> list[tuple[str, str, int, int]]:
        """`(method, path, start, end)` per route, in the order they appear."""
        blocks = self.route_blocks(text)
        spans: list[tuple[str, str, int, int]] = []
        at = 0
        for (method, path), block in sorted(
            blocks.items(), key=lambda kv: text.find(kv[1])
        ):
            found = text.find(block, at)
            if found < 0:
                return []  # a block we cannot locate — decline rather than guess
            spans.append((method, path, found, found + len(block)))
            at = found
        return spans

    def route_blocks(self, source: str) -> dict[tuple[str, str], str]:
        """`(METHOD, path) -> the exact source of that route's handler.

        Not a parse: the block runs from the start of the line the `app.get(`
        call sits on (plus any comment lines directly above it, which is where
        the handler's own explanation lives) to the start of the next route, or
        to the first terminal handler — never past it, or the last route would
        swallow the 404 handler and `app.listen`.

        **Declines outright when the routes are not at the top level.** If the
        terminal boundary sits ABOVE the first route, the model has wrapped the
        routes inside something — measured: `db.initDb().then(() => { …routes…
        })`, which puts `db.initDb(` on line 1 of the region. The slices are
        then meaningless, and the last one runs to end-of-file and swallows the
        wrapper's own closing `});`. Reinstating that block later wrote one
        brace too many and the finished build would not parse. A shape this
        cannot read must produce no blocks rather than wrong ones.
        """
        text = source or ""
        matches = list(_ROUTE_RE.finditer(text))
        if not matches:
            return {}
        floor = _insertion_point(text)
        if floor is not None and floor <= matches[0].start():
            return {}
        limit = floor if floor is not None else len(text)
        out: dict[tuple[str, str], str] = {}
        for i, match in enumerate(matches):
            start = text.rfind("\n", 0, match.start()) + 1
            # Walk back over comment lines that belong to this handler.
            while start > 0:
                prev_start = text.rfind("\n", 0, start - 1) + 1
                line = text[prev_start : start - 1].strip()
                if not line.startswith("//"):
                    break
                start = prev_start
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if end > limit >= start:
                end = limit
            if end <= start:
                continue
            block = text[start:end].strip("\n") + "\n"
            # A block whose brackets do not balance is a slice through the
            # middle of something, not a handler. Re-inserting it elsewhere can
            # only produce a file that will not parse.
            if not _brackets_balance(block):
                continue
            key = (match.group("method").upper(), match.group("path"))
            out.setdefault(key, block)
        return out

    def reinstate_routes(
        self, source: str, blocks: dict[tuple[str, str], str]
    ) -> tuple[str, list[str]]:
        """Put back, verbatim, routes that were in this file earlier in the turn.

        `restore_routes` can only rebuild a GET whose whole body is a
        `res.render`, because anything else would be generation. This is the
        other half and the commoner one: the handler's real source is still in
        hand from minutes ago, so a POST with domain logic in it comes back
        exactly as written rather than being reported as lost.
        """
        text = source or ""
        cut = _insertion_point(text)
        if cut is None or not blocks:
            return source, []
        live = {(m, p) for m, p, _v, _t in self.routes_from_source(text)}
        chunks, restored = [], []
        for (method, path), block in blocks.items():
            if (method, path) in live:
                continue
            chunks.append("\n" + block)
            restored.append(f"{method} {path}")
        if not chunks:
            return source, []
        return (
            text[:cut].rstrip("\n") + "\n" + "".join(chunks) + "\n" + text[cut:],
            sorted(restored),
        )

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

    def strip_layout_include(self, source: str) -> tuple[str, bool]:
        """Remove a view's own `include("layout")` — the layout already wraps it.

        `express-ejs-layouts` renders every `res.render()` inside
        `views/layout.ejs`, so a view that includes the layout as well renders
        the shell inside itself: at best two navigations, and in practice the
        page 500s because the layout expects a `body` the include does not
        provide. `blueprint_layout` states the rule and a 7B writes it anyway —
        measured on `views/seller_listings.ejs`, whose first line was
        `<%- include("../layout", { title: "Create Listing" }) %>`.

        Only an include of the LAYOUT is removed; a partial (`_filters`) is a
        real include and is left alone.
        """
        text = source or ""
        out = _LAYOUT_INCLUDE_RE.sub("", text)
        return (out.lstrip("\n") if out != text else text), out != text

    def repair_model_calls(self, source: str, root: Path) -> tuple[str, list[str]]:
        """`models.getItemById(id)` -> `models.getItem(id)`.

        The data layer is GENERATED, so the name of every query helper is known
        exactly; a route calling something else is a `TypeError: … is not a
        function`, i.e. a 500 on that page. The one shape worth repairing is the
        one the model reaches for constantly and that means precisely one thing
        — a `ById` suffix on a getter that exists without it. Measured on the
        OpenBazaar build: five detail routes, five 500s, five names one suffix
        away from the helper sitting in `models.js`.

        Everything else is left alone and reported by `unresolved_local_calls`:
        `listAuctions` names a query nobody wrote, and inventing one is
        generation, not repair.
        """
        text = source or ""
        models = Path(root) / "models.js"
        if not models.is_file():
            return text, []
        try:
            exported = set(
                re.findall(
                    r"function\s+([A-Za-z_$][\w$]*)",
                    models.read_text(encoding="utf-8", errors="replace"),
                )
            )
        except Exception:
            return text, []
        if not exported:
            return text, []

        fixes: list[str] = []
        for name in sorted(set(re.findall(r"models\.([A-Za-z_$][\w$]*)\s*\(", text))):
            if name in exported:
                continue
            # Two shapes, each meaning exactly one thing: a `ById` suffix on a
            # getter that exists without it, and `getThings` where the generated
            # list helper is `listThings`. Measured: `/items` answered
            # `models.getItems is not a function` with `listItems` defined four
            # lines away.
            if name.endswith("ById"):
                target = name[: -len("ById")]
            elif name.startswith("get"):
                target = "list" + name[len("get") :]
            else:
                continue
            if target not in exported:
                continue
            text = re.sub(
                r"models\.%s\s*\(" % re.escape(name), f"models.{target}(", text
            )
            fixes.append(f"{name} -> {target}")
        return text, fixes

    def _recover_called_helpers(self, root: Path, source: str) -> str:
        """Splice back a helper the entry file calls and this file has lost.

        Read out of `.coder_backups` — the newest snapshot of `models.js` that
        still defines the name — so the restored function is the one that
        worked, character for character, rather than a fresh guess at what it
        did. Returns "" when there is nothing to do.
        """
        entry = root / self.entry_file
        if not entry.is_file():
            return ""
        try:
            calls = set(
                re.findall(r"models\.([A-Za-z_$][\w$]*)\s*\(", entry.read_text(
                    encoding="utf-8", errors="replace"
                ))
            )
        except Exception:
            return ""
        missing = sorted(
            name
            for name in calls
            if not re.search(r"function\s+" + re.escape(name) + r"\s*\(", source)
        )
        if not missing:
            return ""

        backups = sorted(
            (root / ".coder_backups").glob("*models.js"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        ) if (root / ".coder_backups").is_dir() else []
        out = source
        for name in missing:
            for backup in backups:
                try:
                    text = backup.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                block = _function_block(text, name)
                if not block:
                    continue
                out = _crud_node._insert_before_exports(out, block)
                break
        return out if out != source else ""

    def call_arity_mismatches(self, root: Path) -> list[str]:
        """`models.createUser(a, b)` against a helper that takes eight columns.

        The data layer is generated, so each helper's parameter list is known
        exactly. A call with the wrong count still resolves, still parses and
        still routes — it fails at request time, inserting nulls into NOT NULL
        columns. Measured on the finished OpenBazaar build: `POST /register`,
        the one page a new user has to get through, answered 500 for this and
        nothing in the pipeline said why.

        Report only, and deliberately conservative: a call is flagged only when
        it passes FEWER arguments than the helper declares (a trailing optional
        is a real pattern, an omitted required column is not), and a helper
        whose parameter list cannot be read is skipped.
        """
        models = Path(root) / "models.js"
        entry = Path(root) / self.entry_file
        if not (models.is_file() and entry.is_file()):
            return []
        try:
            defs = dict(
                re.findall(
                    r"function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
                    models.read_text(encoding="utf-8", errors="replace"),
                )
            )
            source = entry.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        out: list[str] = []
        for name, args in re.findall(
            r"models\.([A-Za-z_$][\w$]*)\s*\(([^()]*)\)", source
        ):
            if name not in defs:
                continue  # `unresolved_local_calls` owns that one
            expected = [a for a in (p.strip() for p in defs[name].split(",")) if a]
            passed = [a for a in (p.strip() for p in args.split(",")) if a]
            if len(passed) < len(expected):
                out.append(
                    f"{self.entry_file} calls models.{name} with "
                    f"{len(passed)} argument(s); it takes {len(expected)} "
                    f"({', '.join(expected)})"
                )
        return sorted(set(out))

    def normalize_render_names(
        self, source: str, root: Path
    ) -> tuple[str, list[str]]:
        """`res.render("views/login.ejs")` -> `res.render("login")`.

        Express resolves a view name against the configured views directory and
        appends the engine's extension, so a value carrying either is a lookup
        failure — `Failed to lookup view "views/login.ejs"`, a 500 on a page
        whose file is right there. `blueprint_layout` states the rule and the
        route-restoring stub obeys it; a route the MODEL wrote is where it goes
        wrong, and measured it did, on the login page of the OpenBazaar build.

        Only rewrites a name that resolves to a view that really exists, so a
        subdirectory this stack does not use, or a name that is already right,
        is never touched.
        """
        text = source or ""
        views = Path(root) / self.template_dir
        fixes: list[str] = []

        def replace(match):
            raw = match.group("view")
            stem = Path(raw).name
            if stem.endswith(self.template_ext):
                stem = stem[: -len(self.template_ext)]
            if not (views / f"{stem}{self.template_ext}").is_file():
                # The build wrote `views/item_detail.ejs` and the route renders
                # "item": Express reports `Failed to lookup view`, which is a
                # 500 on every detail page of the site. Repointed only when
                # EXACTLY ONE view could be meant — `references._name_key`'s
                # rule, for its reason: sending a route to the wrong page is
                # worse than the error it replaces.
                # `<name>_detail` is what `derive_pages_from_entities` calls a
                # detail page, so it is tried first and by name. Only if there
                # is no such file does a prefix match get a look, and then only
                # when exactly one view could be meant — `new_item` and
                # `item_detail` both look like "item", and guessing between
                # them would send a route to the wrong page.
                detail = f"{stem}_detail"
                if (views / f"{detail}{self.template_ext}").is_file():
                    stem = detail
                else:
                    # Both directions, because the model errs both ways: it
                    # renders "item" for `item_detail.ejs`, and "items_list" for
                    # `items.ejs`. Either way EXACTLY ONE view may match, or the
                    # name is left alone and reported.
                    candidates = sorted(
                        path.stem
                        for path in views.glob(f"*{self.template_ext}")
                        if path.stem.startswith(f"{stem}_")
                        or stem.startswith(f"{path.stem}_")
                    )
                    if len(candidates) != 1:
                        return match.group(0)
                    stem = candidates[0]
            if stem == raw:
                return match.group(0)
            fixes.append(f"{raw} -> {stem}")
            return match.group(0).replace(raw, stem)

        out = _RENDER_NAME_RE.sub(replace, text)
        return out, fixes

    def restore_data_layer_api(self, root: Path, spec: "ProjectSpec") -> list[str]:
        """Refill query helpers an edit to `models.js` deleted.

        `db.js` is never handed to the model; `models.js` still is, and turn 2
        of the OpenBazaar build shows the price — one added column, a rewrite a
        third shorter, and every page 500ing on `models.listUsers is not a
        function`. Deterministic and additive: see `crud_node.restore_model_api`.
        """
        path = Path(root) / "models.js"
        if not path.is_file():
            return []
        try:
            current = path.read_text(encoding="utf-8", errors="replace")
            repaired, restored = _crud_node.restore_model_api(current, spec)
            # A helper the spec does not imply — one a later turn was asked for
            # by name, like `listAuctions` — is invisible to the generator, so
            # once an edit drops it nothing above can put it back. The entry
            # file still calls it, and a previous version of this file still
            # defines it: that is enough to restore it exactly, with no model
            # and no guessing.
            recovered = self._recover_called_helpers(Path(root), repaired)
            if recovered:
                repaired, extra = _crud_node.restore_model_api(recovered, spec)
                restored = sorted(set(restored + extra + ["(from a backup)"]))
        except Exception:
            return []
        if not restored or repaired == current:
            return []
        # The same rule every repair here follows: a fix that will not parse is
        # not a fix. `models.js` is required by the entry file, so a broken one
        # takes the whole app down rather than one page.
        if not self.write_source_if_valid(path, repaired):
            return []
        return restored

    def wire_view_route(
        self, source: str, path: str, stem: str, locals_js: str = ""
    ) -> tuple[str, bool]:
        """Add `app.get(path)` rendering `stem`, above the terminal handlers.

        A view nothing renders is a page that was written, styled, checked and
        linked to from the site's own navigation, and then served a 404 —
        measured on the OpenBazaar build for `/orders` and `/auctions`, both of
        them in the nav of every page. Nothing else could repair it:
        `check_links` reports the dead link, `_wire_missing_endpoints` only
        knows the routes the SPEC declared, and coverage only knows the files
        the plan named.

        Deterministic, and it declines rather than guesses: the caller has
        already established that exactly one view carries this stem and that
        nothing routes to it. ``locals_js`` is the data expression, chosen by
        the caller from the generated model helpers; without one the page still
        renders, because a name no route passes is `add_render_locals`' job.
        """
        text = source or ""
        if not path.startswith("/") or not stem:
            return text, False
        at = _insertion_point(text)
        if at is None:
            return text, False
        title = stem.replace("_", " ").replace("-", " ").strip().title()
        extra = f", {locals_js}" if locals_js else ""
        block = (
            f'app.get("{path}", async (req, res) => {{\n'
            f'  res.render("{stem}", {{ title: "{title}"{extra} }});\n'
            "});\n\n"
        ).replace("\n", chr(10))
        return text[:at] + block + text[at:], True

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

    def route_edit_region(self, filename: str, text: str, user_message: str):
        """The ONE route block an edit names, as a spliceable region, or None.

        The Phase W3 idea moved from templates to the entry file, and for a
        sharper reason. A 7B asked to fix one handler answers with that handler —
        and `_apply_search_replace` then matches its SEARCH against the whole
        file, so the reply lands as a replacement for everything it resembles.
        Measured on the OpenBazaar build: one "fix POST /items/new" turn came
        back having deleted **27 routes**, and the route-loss guard had to revert
        the whole edit, which means the requested fix did not land either.

        Confining the edit to the block makes both impossible at once: the model
        sees one handler, its SEARCH is matched against that handler, and
        `BlockRegion.splice` copies every other byte of the file through
        untouched. A rewrite of one route cannot lose another.

        Declines — returning None for today's whole-file path — whenever the
        answer is not certain: not the entry file, no routes parsed, the message
        names no route, or it names more than one. Two candidates mean the
        request was ambiguous, and editing the wrong handler is worse than
        editing the file.
        """
        if Path(filename).name != self.entry_file:
            return None
        spans = self._route_spans(text or "")
        if not spans:
            return None
        message = " ".join((user_message or "").split())
        if not message:
            return None

        wanted = [
            (method, path, start, end)
            for method, path, start, end in spans
            if _names_route(message, method, path)
        ]
        if len(wanted) != 1:
            return None
        method, path, start, end = wanted[0]
        body = text[start:end]
        if not body.strip():
            return None
        return _scaffold.BlockRegion(
            name=f"{method} {path}",
            start=start,
            end=end,
            body=body,
            siblings=tuple(f"{m} {p}" for m, p, _s, _e in spans if p != path)[:8],
            kind="route",
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

    def schema_types(self) -> str:
        """The column types the SCHEMA call may use on this stack.

        PostgreSQL has a real boolean and a real timestamp, so this stack says
        so. `TIMESTAMP` is emitted as `TIMESTAMPTZ` and `TEXT` primary keys are
        generated by the database (`gen_random_uuid()`), which is what lets a
        PRD's `UUID PRIMARY KEY` survive as something the app can actually
        insert into.
        """
        return (
            "## Column types — use only these\n"
            "`INTEGER`, `TEXT`, `REAL`, `BLOB`, `NUMERIC`, `BOOLEAN`, "
            "`TIMESTAMP`.\n\n"
            "Storage is PostgreSQL. A yes/no is `BOOLEAN` and a point in time "
            "is `TIMESTAMP` (written as `TIMESTAMPTZ`) — do NOT flatten either "
            "into `INTEGER` or `TEXT`, or the app cannot compare them. Money is "
            "`NUMERIC`. The primary key of every table is either "
            '`{"name": "id", "type": "INTEGER", "pk": true}` '
            '(autoincrement) or `{"name": "id", "type": "TEXT", '
            '"pk": true}` (a generated UUID); use TEXT when the document asks '
            "for UUID keys. Either way the database fills it in."
        )

    def blueprint_layout(self) -> str:
        """The filenames the PLANNING call must use on this stack.

        Until this existed the planning prompt named the Flask layout and
        nothing else, so an Express build was planned as `app.py` +
        `templates/*.html`. `derive_pages_from_entities` then wrote the real
        `views/*.ejs` beside those, and the two sets disagreed — the model's own
        pages were planned at paths this stack never renders.
        """
        return (
            "## File layout — use these names and no others\n"
            "| File | Holds |\n"
            "| --- | --- |\n"
            "| `server.js` | routes only — one `app.get`/`app.post` per URL, no "
            "SQL |\n"
            "| `db.js` | the `pg` pool, `initDb()`, `ensureColumn()` |\n"
            "| `models.js` | one query helper per operation, `$1, $2` parameters "
            "only |\n"
            "| `seed.js` | a few demo rows per table |\n"
            "| `views/layout.ejs` | the nav and page shell — the ONLY place nav "
            "exists |\n"
            '| `views/index.ejs` | the home page — `"action": "edit"`, it '
            "already exists |\n"
            "| `views/<page>.ejs` | one per page, a FRAGMENT the layout wraps |\n"
            "| `public/css/style.css` | the one stylesheet |\n"
            "| `public/js/app.js` | optional enhancement only |\n\n"
            "Rules that follow from it:\n"
            "- A view is a fragment: never `<html>`, `<head>` or its own `<nav>`. "
            "`express-ejs-layouts` wraps every `res.render()` in "
            "`views/layout.ejs`.\n"
            '- Express renders a view by its STEM: `res.render("items")` for '
            "`views/items.ejs`. A link names a PATH (`/items/new`), never a view "
            "name.\n"
            '- Prefer a real `<form method="post" action="/route">` over '
            "`fetch()`. It works with JavaScript disabled, which is what makes "
            '"the button does nothing" impossible rather than merely unlikely.\n'
            "- Routes call helpers in `models.js`; they never write SQL inline. "
            "Handlers are `async` and `await` those helpers.\n"
            "- Do NOT plan `package.json`, `Procfile`, `ui.js` or `.gitignore` — "
            "they are already written for you.\n"
            "- **Plan the home page.** `views/index.ejs` exists but holds "
            'placeholder text, so give it `"action": "edit"` and an instruction '
            "describing what this site's front door shows and which pages it "
            "links to. A build that leaves it alone ships a site whose first "
            "page says it was scaffolded."
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
