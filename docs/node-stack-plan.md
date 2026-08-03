# Adding a second stack: Node + Express + PostgreSQL

**Goal.** Coder asks which stack to build on, then builds either the existing
Flask/Jinja2/sqlite3 stack or a new Node/Express/PostgreSQL one — with the Flask
path's *behaviour* provably unchanged.

**Status:** **N0–N4 are implemented.** N5 shipped early in cut-down form (see
deviation 3 below); N6 is still plan only.
**Predecessor:** `docs/web-quality-plan.md` (W1–W10).

> ### What shipped, and what that leaves
>
> **N0 — the seam.** `app/agent/stacks/` holds the `StackAdapter` protocol,
> `flask_adapter.py` (delegation only — no rewritten logic) and
> `node_adapter.py`. Every `core.py` site listed in §0 now routes through
> `self._adapter`, chosen once per turn. The existing suite passes unmodified.
>
> **N1 — choosing and remembering.** `/stack` lists both stacks with their
> guarantees *and their gaps*, and `/stack node` switches the session default.
> `stacks.resolve_key` puts the spec ahead of the setting, so an amendment to a
> Node project can no longer be handed Flask's machinery.
>
> **N2 — the scaffold.** `app/resources/scaffolds/node/` is a runnable Express +
> EJS + PostgreSQL skeleton: verified to serve `/` with 200 through the real
> layout, serve its static files, and render a custom 404, before any LLM call.
> `style.css` and `theme.css` are byte-identical copies of the Flask sheets.
>
> **N3 — the data layer.** `app/agent/crud_node.py` writes `db.js`'s tables,
> `models.js`, `seed.js` and (when the schema holds a secret) `passwords.js`,
> from the same `Entity` objects `crud.py` uses. The dialect differences live in
> `projectspec.Dialect` (`SQLITE` / `POSTGRES`), so one schema cannot produce two
> different databases. `derive_pages_from_entities` and `derive_home_page` now
> run for any stack that ships a scaffold, so a Node build gets a list page, a
> create form and their routes per entity — identical routes to Flask's, verified
> side by side.
>
> **N4 — verification.** Four checks, all of which the Node stack was previously
> missing and `gaps` previously admitted to:
>
> 1. **`.ejs` structural checking** (`verify.strip_ejs` + `_check_ejs_text`,
>    wired into `check_text`/`is_verifiable`). The JavaScript comes out first —
>    `<% if (a) { %>` is not an element — then the markup underneath is balanced.
>    An unterminated `<%` is the characteristic EJS error and it takes the page
>    down at render time; nothing else could see it, because the file is
>    valid-ish HTML, `node --check` does not read it and the browser never gets
>    that far. It catches the exact `<%# … #%>` bug that broke the scaffold's
>    home page during N2. A view may legitimately be a fragment *or* a whole
>    document, and both pass for the right reason.
> 2. **Link validation for a path-routed stack** (`verify.local_links`,
>    `unresolved_links`, `fix_link_targets`, `form_method_mismatches_by_path`),
>    exposed as `adapter.check_links(text, routes)` on **both** adapters with
>    `core._check_endpoints` dispatching through it. Flask keeps its `url_for`
>    name-checking unchanged; Node checks paths and allows for `:id` segments.
>    W2's rules transfer whole: an unambiguous near miss is repointed, everything
>    else is reported. **A form is judged against the UNION of every route that
>    matches it** — `/products/:id` also matches `/products/new`, and taking the
>    first match reported a real `POST /products/new` handler as a 405.
> 3. **An EJS template graph** (`templatedeps.parse_ejs_template`;
>    `build_graph` gained `template_dir` / `template_ext` / `parser` /
>    `routes_reader`, all defaulting to the Flask layout, so every existing
>    caller and test is unchanged). Same graph shape, different parser. The
>    load-bearing rule survives the port: identifiers come from EJS expressions
>    with the strings stripped first, so `layout.ejs` — whose nav says "Products"
>    and whose href is `/products` — does **not** read every entity.
> 4. **Spec adoption for a Node repo Coder did not build.**
>    `ProjectSpec.from_disk` tries Python/Flask first (unchanged, so an existing
>    Flask repo adopts exactly as it did), then Node: routes off `server.js`,
>    tables off `db.js` via `crud_node.js_strings`, `/products` →
>    `views/products.ejs` with `reads=('product',)`. It still declines when there
>    is no route, so a plain JS folder never acquires an invented contract.
>
> **What N4 does NOT close** — and `gaps` says so, because `/stack` prints it:
> there is no `pyimports.add_missing_imports` equivalent (Flask's only
> *auto-repairing* correctness check, and a JS version is its own piece of work
> and would be weaker); the **import** dependency graph stays Python-only;
> routes are read with a regex rather than tree-sitter, which leaves an
> unrecognised route shape unvalidated rather than reported wrongly; and there is
> no template-scoped editing, so an edit to a view is a whole-file edit.
>
> **Four deliberate deviations from the plan text below**, each because the
> plan's shape did not survive contact:
>
> 1. **`views/_macros.ejs` is `ui.js`.** EJS has no macro construct —
>    `<%- include('_macros') %>` renders a partial, it does not export
>    callables. Plain functions returning escaped HTML give the identical call
>    site (`<%- ui.table(rows, columns) %>`) and keep every macro NAME, which is
>    what `ui_context()` and the drift tests actually depend on.
> 2. **`restore_invariants` / `write_migrations` are split up.** The
>    orchestration is async and writes through `executor.execute` (approval
>    gate, backup, `/undo`), so only the decisions moved behind the protocol.
> 3. **A cut-down N5 shipped early.** `adapter.readiness(root)` checks node,
>    `node_modules` and a socket to Postgres, and the smoke test is *skipped and
>    reported* rather than failed when one is missing. Without it, `npm install`
>    never having run would send the repair loop to rewrite correct code. The
>    `SELECT 1` half is still N5's.
> 4. **`passwords.js` is written too.** `crud.py` leans on `werkzeug.security`,
>    which ships with Flask; Node has no equivalent, and `bcrypt` means a native
>    build on a machine whose point is that it works offline. Node's own
>    `crypto.scrypt` needs nothing installed, so the helper is generated rather
>    than left to a prompt — the same reason `plaintext_password_writes` is a
>    check on the code and not a line of advice.
>
> **The `_creates_table` trap needed a JS answer.** `db.js` ships a *commented*
> `CREATE TABLE ... widgets` example, exactly as `db.py` does, and the Flask side
> already lost a live build to counting one. `pyimports.searchable_sql` uses
> stdlib `ast`; there is no Python-side JS parser, so `crud_node.js_strings`
> strips comments and reads string literals, erring toward reporting *fewer*
> literals — writing `CREATE TABLE IF NOT EXISTS` twice is a no-op, skipping one
> is a dead app. The plan asked for a tree-sitter query here; that is worth doing
> when N4 brings tree-sitter in for routes, and the trap is closed either way.
>
> **What N3 does NOT close:** the generated SQL has not been executed against a
> real PostgreSQL on this machine. `tests/test_crud_node.py` has that tier, gated
> on `psycopg` plus `CODER_TEST_DATABASE_URL`, and it **skips loudly** rather
> than passing quietly. What has been verified here: every file parses
> (`node --check`), every query's placeholder count matches its parameter array,
> and the password helper round-trips. Flask remains the default.

---

## 0. The honest framing, first

You cannot add a second stack "without changing the Flask stack." The Flask
logic is not behind an interface — it is **inlined into `core.py`** at a dozen
call sites (`workdir / "app.py"` at 2017/2220/3165/3589, `workdir.glob("*.py")`
at 2372, `[sys.executable, "seed.py"]` at 3343, the `db.py` migration writer at
3363–3398, `templates/index.html` at 4039) plus six modules that are Flask/Python
to their core (`scaffold.py`, `crud.py`, `pyimports.py`, `templatedeps.py`, and
parts of `projectspec.py` / `impact.py`).

So the promise this plan can actually keep is the stronger, testable one:

> **Flask code changes. Flask behaviour does not.**
> Every existing test passes, unmodified, at every phase boundary.

That is the same guarantee the blueprint phase shipped under ("flag OFF, proven
inert, suite passed"), and it is enforceable. "We won't touch it" is not.

## 0.1 Two named stacks, not two axes

Do **not** make database and framework independent. That yields four
combinations (flask+sqlite, flask+pg, node+sqlite, node+pg), four scaffolds and
a test matrix nobody maintains. Ship exactly two named stacks:

| `web_stack` | Language | Framework | Templates | Database |
|---|---|---|---|---|
| `flask` (default, today) | Python | Flask | Jinja2 | sqlite3 |
| `node` (new) | Node | Express | EJS | PostgreSQL |

If Postgres-under-Flask is ever wanted, it is a separate decision made later,
not a free consequence of this work.

## 0.2 What already exists (do not rebuild it)

- **`runtime_probe.Stack`** — `language` / `backend` / `runnable` / `note` /
  `install_hint`, and `detect_stack(prefer=…)` already accepts `"node"`.
- **`ProjectSpec.language` / `.backend`** — already persisted to
  `.coder/project.json` (`projectspec.py:1206`), already reloaded (1271), already
  populated from the `Stack` (1449). **Nothing dispatches on them.** This is the
  single most important existing asset: cross-turn stack memory is already
  written to disk.
- **Stack-agnostic already (~12,000 of 16,093 lines in `app/agent/`)**: the whole
  quality layer — `browser.py`, `pageaudit.py`, `visualcheck.py` — drives real
  Chromium over HTTP and does not know what served the page. Same for `smoke.py`'s
  functional probe (599 lines, **one** Flask reference), `intent.py`,
  `buildspec.py`, `references.py`, `blueprint.py`, the router, the tool loop.
  `symbols.py` already extracts symbols for JS/TS via tree-sitter.

---

## Phase N0 — The adapter seam (no new stack)

**Nothing new is buildable at the end of this phase. That is the point.**

Introduce `app/agent/stacks/__init__.py` with a `StackAdapter` protocol, and
`app/agent/stacks/flask_adapter.py` implementing it by **delegating to today's
functions**. No logic is rewritten; call sites move behind the protocol.

```python
class StackAdapter(Protocol):
    key: str                 # "flask" | "node"
    language: str            # "python" | "node"
    entry_file: str          # "app.py" | "server.js"
    template_dir: str        # "templates" | "views"
    template_ext: str        # ".html" | ".ejs"
    source_globs: tuple[str, ...]

    def scaffold(self, root: Path, name: str) -> list[str]: ...
    def frozen_files(self) -> set[str]: ...
    def write_data_layer(self, root: Path, spec) -> tuple[set[str], str]: ...
    def seed_command(self) -> list[str]: ...
    def run_command(self, entry: Path) -> list[str]: ...
    def write_migrations(self, root: Path, spec, since: int) -> str: ...
    def restore_invariants(self, root: Path) -> str: ...
    def routes_from_source(self, source: str) -> list[tuple]: ...
    def ui_context(self) -> str: ...
    def prompt_note(self) -> str: ...
```

**Rules:**
- `get_adapter(key)` returns the Flask adapter for **any unknown key**, including
  `""`. A spec written before this phase has no adapter key and must keep working.
- Every `core.py` site listed in §0 routes through `self._adapter`.
- `self._adapter` is chosen once per turn from `spec.backend or settings.web_stack`.

**Exit criteria:** the entire existing suite passes unmodified. Not "passes with
updated expectations" — *unmodified*. If a test needs changing, the seam is wrong.

**Cost: ~1 week.**

---

## Phase N1 — Choosing the stack, and remembering it

**`/stack` REPL command:**

```
coder> /stack
Current: flask (Flask + Jinja2 + SQLite)
  1. flask   Python · Flask · Jinja2 · SQLite      [ready]
  2. node    Node · Express · EJS · PostgreSQL     [needs: npm install, postgres]
coder> /stack node
```

**A first build with no stack chosen asks once**, then never again for that
project — the choice is written to `.coder/project.json` by the machinery that
already persists it.

**The load-bearing rule: an amendment reads the stack from the spec, never from
`settings.web_stack`.** Without this, opening a Node project with `web_stack`
left at `flask` sends `_amend_project` to write Python `ensure_column` calls into
a `db.py` that does not exist — a silent, total failure on turn 2. This is the
single most likely way the feature "falls apart," and `spec.backend` already
holds the answer.

**Precedence:** `spec.backend` (project memory) → `settings.web_stack` (session
default) → `flask`.

**Cost: ~2 days.**

---

## Phase N2 — The Node scaffold (data, not code)

`app/resources/scaffolds/node/`, mirroring `scaffolds/flask/` file for file, so
the design system carries over unchanged:

```
server.js          routes only, no SQL
db.js              getPool(), initDb(), ensureColumn()
models.js          one query helper per operation, $1 params only
seed.js            a few demo rows per table
views/layout.ejs   the shell + nav  (base.html's role)
views/index.ejs
views/_macros.ejs  table / card / field / badge / empty_state / flash
public/css/style.css   ← COPY of the Flask sheet, unchanged
public/css/theme.css   ← written by write_theme, unchanged
public/js/app.js
package.json
Procfile
gitignore          (dot restored on write — see CLAUDE.md packaging note)
```

**`style.css` and `theme.css` are copied verbatim from the Flask scaffold.** They
are framework-agnostic CSS written entirely in custom properties, and
`resolve_theme` → `theme_tokens` → `theme_css` → `write_theme` needs no change at
all. **All of W1's design-system work transfers for free.** Do not fork these
files — if they drift, the two stacks stop looking like one product.

**`views/_macros.ejs` must expose the same macro names as `_macros.html`**, so
`ui_context()` says the same thing on both stacks and the drift tests
(`test_scaffold_ui.py`) generalise instead of being duplicated.

**Cost: ~4 days** (most of it making `server.js` genuinely runnable before any
LLM call, which is the whole point of the scaffold).

---

## Phase N3 — The Node data layer + PostgreSQL

`app/agent/crud_node.py`, the mirror of `crud.py`: `entity_helpers`,
`models_source`, `seed_source`, `table_block`, `api_context`, `upload_helper_source`.

Both must emit from the **same `Entity` objects**, so `projectspec` gains
SQL-dialect awareness:

| Concern | sqlite3 | PostgreSQL |
|---|---|---|
| placeholder | `?` | `$1, $2, …` |
| autoincrement PK | `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| add column | `ensure_column()` via PRAGMA | `ALTER TABLE … ADD COLUMN IF NOT EXISTS` |
| insert returning id | `cursor.lastrowid` | `RETURNING id` |
| types | TEXT/INTEGER/REAL | TEXT/INTEGER/NUMERIC/TIMESTAMPTZ |

**Rules inherited from `crud.py`, non-negotiable:**
- Values bound as parameters; identifiers only ever from `projectspec._ident`
  (`[A-Za-z_][A-Za-z0-9_]*`). SQL injection stays impossible **by construction**.
- Column lists printed from the same `Entity` as the DDL, so they cannot drift.
- `api_context()` is not optional. Taking `models.js` away from the model is only
  safe if the model is told what replaced it.
- Idempotency checks read **string literals**, never raw text — the `_creates_table`
  trap, which on Python needed `pyimports.searchable_sql`. The JS equivalent needs
  a tree-sitter query, not a regex.

**PostgreSQL is genuinely harder than sqlite here, and `migrations(since=n)` is
where it shows.** `ensure_column` is a runtime PRAGMA check; `ALTER TABLE … IF NOT
EXISTS` is DDL against a live server that may reject it. Migration failures must
be *reported*, never swallowed.

**Cost: ~1.5 weeks.**

---

## Phase N4 — Verification for Node

This phase is cheaper than it looks, because most of it already works.

**Free (no change):** `browser.py`, `pageaudit.py`, `visualcheck.py`, and
`smoke.py`'s three functional-probe steps. They speak HTTP and DOM.

**One-line changes:**
- `smoke._runtime_command` (`smoke.py:129`) — `[sys.executable, f]` → adapter's
  `run_command()`, i.e. `["node", "server.js"]`.
- `_seed_demo_data` (`core.py:3343`) — `["node", "seed.js"]`. Same deliberate
  exception to "never execute generated code" applies, for the same reason:
  `seed.js` is written by `crud_node.py`, not by the model.
- `apprunner.py` — same `run_command()`.

**Real work:**
- **`verify.check_file`** — `.js` already has `node --check` plus the content
  guard. `.ejs` needs a tag-balance check like `.html` has.
- **W2 endpoint validation** — `routes_from_source` currently parses `@app.route`
  via Python `ast`. Node needs `app.get("/x", …)` via tree-sitter. The near-miss
  repair rule (`references._name_key`, exactly-one-candidate) transfers unchanged.
- **`templatedeps.py`** — EJS `include()` / `res.render()` instead of Jinja
  `extends` / `render_template`. Same graph shape, different parser.

**Known permanent losses on the Node stack — state them in the answer, do not
paper over them:**
1. **`pyimports.add_missing_imports` has no equivalent.** It is the only
   *auto-repairing* correctness check, built on stdlib `ast`. A JS version via
   tree-sitter is possible but is its own week and will be weaker.
2. **The dependency graph stays Python-only.** `symbols.py` extracts JS *symbols*
   but does not resolve JS *imports*, so `dependencies`/`dependents` return
   nothing and impact analysis falls back to `Page.reads` + `templatedeps`.

**Cost: ~1.5 weeks.**

---

## Phase N5 — The readiness gate (the part that protects everything else)

**This is the phase that stops the feature from falling apart, and it has no
Flask precedent — sqlite has no daemon and needs no install.**

Node + Postgres introduces **three** ways to be un-runnable where Flask had one:

| Failure | Detect with | Report |
|---|---|---|
| Node absent | `shutil.which("node")` | "install Node.js" |
| `node_modules` absent | `(root/"node_modules").is_dir()` | "run `npm install`" |
| Postgres not listening / no DB | socket connect + `SELECT 1` | "start PostgreSQL, create `<db>`" |

`Stack.runnable` today means "is the framework importable." It must become
"**can the generated app actually start and reach its database.**"

**Rules, all inherited from existing decisions in this codebase:**
- **A skipped check must never read as a passing one** (`browser.py`'s rule). If
  Postgres is down, the smoke test, functional probe, seed and browser audit are
  all *reported as not run*, never reported clean.
- **`install_hint` stays separate from `note`** (`runtime_probe.py`'s rule). The
  model is told to write Express + `pg`; the *user* is told to run `npm install`.
  Fold them together and the model reads the warning and writes something else.
- **The smoke test is skipped when `runnable` is False** — exactly as it is for
  absent Flask today. Otherwise `_smoke_repair_instruction` sends the model to
  rewrite correct code because a database was down.

**`npm install` needs the network, and `allow_network` ships False.** This is a
real, deliberate exception, handled precisely like Playwright's Chromium in W4:
a one-time gated install, a loud separate hint, and default-off. It is **not** a
silent skip. Note `runtime_probe._node()` already degrades `backend` to `"stdlib"`
when the network is off — decide explicitly whether the Node stack requires
Express (and therefore a one-time install) or targets Node's built-in `http`.
**Recommendation: require Express.** A vendored-nothing Node stack reproduces the
stdlib stack nobody wanted.

**Cost: ~4 days.**

---

## Phase N6 — Tests and evals

- `tests/test_stacks.py` — the adapter contract, both implementations.
- `tests/test_crud_node.py` — generated SQL executed against **real** Postgres,
  mirroring `test_crud.py`'s use of real in-memory sqlite3. This needs a live
  server, which is why it must be `pytest.importorskip`-gated and skipped loudly
  in CI rather than silently passing.
- Parametrise the existing web evals over both stacks. The Phase E checks
  (`is_full_stack_app`, `every_entity_has_a_table`, `entities_are_usable`) name no
  table and no route — **they read the project's own spec, so they generalise for
  free.** That is the highest-value existing asset in this whole plan.
- **The `conftest.py` trap, fourth occurrence.** Any new setting that reaches an
  LLM call must default OFF in tests (`check_intent`, `schema_first`,
  `check_visual` all had to). Assume the same for anything added here.

**Cost: ~1 week.**

---

## Totals

| Phase | Work | Cost |
|---|---|---|
| N0 | Adapter seam, zero behaviour change | 1 week |
| N1 | `/stack`, spec-driven dispatch | 2 days |
| N2 | Node scaffold (data) | 4 days |
| N3 | `crud_node.py` + Postgres dialect | 1.5 weeks |
| N4 | Verification for Node | 1.5 weeks |
| N5 | Readiness gate | 4 days |
| N6 | Tests and evals | 1 week |
| | **Total** | **~6–7 weeks** |

**Cut-list, in the order to cut:** N4's EJS `templatedeps` (falls back to
`Page.reads`) → W2 endpoint validation for Node (report-only) → uploads on Node →
`best_of_n` scoring for Node. Cutting all four still leaves a Node stack that
scaffolds, generates, runs, seeds, and passes the browser audit — roughly **4
weeks** for a demonstrably working second stack with fewer guarantees.

**Never cut:** N0 (without the seam this is a fork, not a feature), N1's
spec-driven dispatch (mixed-stack amendments are silent corruption), N5's
readiness gate (an unverified build reported as verified is the one failure this
codebase exists to prevent).

---

## The risks, stated plainly

1. **Depth dilution.** Flask has W1 components, W2 endpoint validation, W3 Jinja
   block editing, migrations, impact analysis and `add_missing_imports`. Node on
   day one has none of the last three. If faculty picks Node, the demo is *worse*
   than the Flask one. Mitigation: `/stack` reports each stack's guarantee tier
   honestly, and Flask stays the default.
2. **The offline promise narrows.** Postgres is local, so nothing leaves the
   machine and "offline" survives. `npm install` does not. Be precise about this
   distinction in any writeup — they are different claims.
3. **Two stacks, one 7B.** `qwen2.5-coder:7b` writes Express about as well as
   Flask (JavaScript is its best-represented language), so this is the *one*
   second stack where the model is not the limiting factor. Go or Java would be.
4. **The test matrix roughly doubles**, and `test_crud_node.py` cannot run without
   a live Postgres. Budget for a CI story, or accept a loudly-skipped suite.
5. **Regression risk is concentrated entirely in N0.** After the seam lands and
   the suite passes unmodified, every later phase is additive. Do N0 slowly.
