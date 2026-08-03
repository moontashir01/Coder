# Continue here — session handoff

**Written:** 2026-08-03. Read this first, then `docs/node-stack-plan.md`.

## ⚠️ Nothing is committed or pushed yet

All of the work below lives in the **working tree only**. `main` tracks
`origin/main` and has **zero unpushed commits** — so a `git checkout` or a
`git clean` would destroy several days of work. Commit before anything else.

- Author is already correct: `moontashir01 <moontashir.azim@northsouth.edu>`.
- **Do NOT add a `Co-Authored-By: Claude` trailer** — the repo owner asked to be
  the single author. A ready-to-use commit message is at the bottom of this file.
- Scope note: the tree contains **two** bodies of work — the W1–W10 web-quality
  phase (which was already uncommitted before this session) and the N0–N4 Node
  stack. They share `core.py`, `projectspec.py`, `scaffold.py` and
  `blueprint.py`, so they cannot be split into two commits without building an
  intermediate tree that has never been run. One commit is the honest choice.

## Where things stand

| Phase | State |
|---|---|
| N0 — adapter seam | **Done.** `app/agent/stacks/` |
| N1 — `/stack`, spec-driven dispatch | **Done.** |
| N2 — Node scaffold | **Done.** `app/resources/scaffolds/node/` |
| N3 — data layer + PostgreSQL | **Done.** `app/agent/crud_node.py` |
| N4 — verification for Node | **Code done, NOT tested or documented** |
| N5 — readiness gate | Partly done early (see below) |
| N6 — tests and evals | Not started |

Last **full** suite run (pre-N4 tree): **1393 passed, 2 skipped** (the 2 skips
are `playwright` being absent — pre-existing). A later run reached ~98% clean
before it was stopped to make the N4 edits.

## N4 — what is already written

All four are implemented and manually verified, but **have no tests yet**.

1. **`.ejs` syntax check** — `verify.strip_ejs` + `_check_ejs_text`, wired into
   `check_text` and `is_verifiable`. Catches an unterminated `<%`, a stray `%>`
   and unbalanced HTML underneath. Verified against the real scaffold views, and
   it catches the exact `<%# … #%>` bug that broke the home page during N2.
2. **Link validation for path-routed stacks** — `verify.local_links`,
   `unresolved_links`, `fix_link_targets`, `form_method_mismatches_by_path`.
   Exposed as `adapter.check_links(text, routes)` on **both** adapters;
   `core._check_endpoints` now dispatches through it. Flask keeps `url_for`
   name-checking; Node checks paths, allowing for `:id` segments.
3. **EJS template graph** — `templatedeps.parse_ejs_template`, `build_graph`
   gained keyword args (`template_dir`, `template_ext`, `parser`,
   `routes_reader`) that all default to the Flask layout, and
   `adapter.build_template_graph(root)`. Verified that `layout.ejs` does **not**
   read every entity, which is the rule the whole entity-hint design exists for.
4. **Node spec adoption** — `ProjectSpec.from_disk` tries Python/Flask first
   (unchanged), then Node. Verified: adopts `node`/`express`, recovers tables
   from `db.js`, routes from `server.js`, and maps `/products` →
   `views/products.ejs` with `reads=('product',)`.

## N4 — what is LEFT to do

1. **Write the tests.** Nothing above is pinned. Suggested homes:
   - `tests/test_verify.py` — `strip_ejs` / `.ejs` check: unterminated `<%`,
     stray `%>`, `<%%` escape, unclosed `<div>`, a valid fragment, a valid
     layout, and the real scaffold views.
   - `tests/test_stacks.py` — `check_links` on both adapters; the `:id` match;
     the near-miss repair; **the union-across-matching-routes rule** (a
     `POST /products/new` must not be reported as 405 just because
     `GET /products/:id` also matches — this was a real bug found and fixed).
   - `tests/test_templatedeps.py` — `parse_ejs_template`, and that `build_graph`
     with no keyword args behaves exactly as before.
   - `tests/test_projectspec.py` — Node adoption, and that a plain JS folder
     with no routes still returns `None`.
2. **Update `NodeAdapter.guarantees` / `.gaps`** in
   `app/agent/stacks/node_adapter.py`. They are now **stale and wrong**: they
   still claim there is no route validation, no `.ejs` syntax check and no spec
   adoption. `/stack` prints these verbatim, so leaving them is worse than
   having no list. Move those three to `guarantees` and leave as gaps:
   - no `pyimports.add_missing_imports` equivalent (the only *auto-repairing*
     correctness check; a JS version is its own week and would be weaker);
   - the dependency graph stays Python-only (`symbols.py` extracts JS symbols
     but does not resolve JS imports);
   - route parsing is a regex, not tree-sitter (the plan wants tree-sitter; the
     regex is honest and narrow, and N4's cut-list ranks this low);
   - no template-scoped editing (W3's equivalent) — `template_edit_region`
     returns `None`, which is the existing whole-file path, not a new failure.
3. **Docs:** `docs/node-stack-plan.md` status header → N0–N4; the CLAUDE.md
   "Two stacks behind one seam" section; `CHANGELOG.md`.
4. **Run the full suite**: `.venv/Scripts/python.exe -m pytest tests/ -q`
   (~15 min; stop `ollama serve` first or it looks like a regression).
5. **Commit and push.**

## Things worth not re-learning

- **`black app/ tests/` reformats files unrelated to your change.** It happened
  once this session and ~14 files had to be reverted. Format only the files you
  touched, or check `git diff` after.
- **The suite takes ~15 min and buffers all output**, so a background run shows
  0 bytes until it finishes. That is normal, not a hang.
- **`tests/test_functional_probe.py::test_a_working_app_passes_every_check` is
  flaky under load** — it starts a real Flask server on :5000 with a 20 s
  timeout. It passes in isolation. Do not "fix" it.
- **`_macros.html` fails `check_file`** (tags open in one macro branch and close
  in another). Pre-existing, and inert: the file is `_FROZEN`, so `check_file`
  never runs on it in practice.
- **A real PostgreSQL has never run the generated SQL.**
  `tests/test_crud_node.py` has that tier, gated on `psycopg` +
  `CODER_TEST_DATABASE_URL`. It **skips loudly** — do not make it pass quietly.
- N5 shipped early in cut-down form: `adapter.readiness(root)` checks node,
  `node_modules` and a socket to Postgres, and the smoke test is *skipped and
  reported*. The `SELECT 1` half is still outstanding.

## Ready-to-use commit message

Save to a file and use `git commit -F <file>`. **No Claude trailer.**

```
feat: web-quality layer (W1-W10) and a second stack, Node + Express + PostgreSQL

Two bodies of work that share too many files to separate into commits without
shipping an intermediate tree nobody ever ran.

## Web quality (docs/web-quality-plan.md, W1-W10)

Generated sites now come with a design system, and Coder can look at what it
built. Every build ships a component stylesheet and a Jinja macro library, and
the style you ask for is WRITTEN into static/css/theme.css rather than described
to the model and hoped for -- so the table on page one and the table on page
three are the same table.

- W1  static/css/style.css + templates/_macros.html, every rule in custom
      properties; theme.css resolved from the request and linked last.
- W2  A misnamed url_for is a Jinja BuildError -- a 500 on a page that parses,
      renders in isolation and passes every other check. Near misses are
      repointed, anything else is reported.
- W3  An edit to a child template is confined to its {% block %}, so a 7B's
      SEARCH/REPLACE can no longer delete the {% extends %} it was editing under.
- W4-W7 With a headless browser installed, the pages are rendered: horizontal
      overflow at 390px, console errors, 404ing assets, sub-AA contrast, buttons
      wired to nothing. Findings are repaired in the file that owns them and
      re-measured, and the pass is reverted if the page got worse. Off by
      default, and reported as skipped when it cannot run.
- W8  A real template dependency graph off disk.
- W9  Best-of-N on high-value files, scored by the deterministic checks above.
      Default 1 (off).

## A second stack (docs/node-stack-plan.md, N0-N4)

- N0  app/agent/stacks/: a StackAdapter protocol, a Flask adapter that is pure
      delegation, and a Node one. Every `workdir / "app.py"`, `glob("*.py")` and
      `[sys.executable, "seed.py"]` in core.py now goes through self._adapter.
      The existing suite passes unmodified, which was the point of the phase.
- N1  /stack shows and switches the stack, printing each one's gaps as well as
      its guarantees. A project's own stack (.coder/project.json) always beats
      the session default -- otherwise opening a Node project on turn 2 writes
      Python ensure_column calls into a db.py that does not exist.
- N2  app/resources/scaffolds/node/: a runnable Express + EJS + PostgreSQL
      skeleton that serves / with a 200 through its layout, serves its static
      files and renders its own 404 before any LLM call. style.css and theme.css
      are byte-identical copies, so the design system transfers whole.
- N3  crud_node.py writes db.js's tables, models.js, seed.js and passwords.js
      from the same Entity objects crud.py uses; the differences live in
      projectspec.Dialect, so one schema cannot produce two databases. $1
      parameters, RETURNING id, SERIAL keys, scrypt hashing. Every entity gets a
      list page and a create form, derived not prompted.
- N4  .ejs syntax checking, link validation with near-miss repair, an EJS
      template graph, and spec adoption for a Node repo Coder did not build.

Flask stays the default: it keeps import repair and block-scoped template
editing that Node does not have yet, and /stack says so.

Node + PostgreSQL has three ways to be un-runnable where Flask had one, so the
smoke test is skipped and REPORTED when node, node_modules or the database is
missing, rather than sending the repair loop after code that is already correct.

Verified: 1393 offline tests on the pre-N4 tree; the Express scaffold booted and
was probed for real; every generated .js parses under `node --check`; every
query's placeholder count matches its parameter array. The generated SQL has NOT
been run against a live PostgreSQL -- tests/test_crud_node.py has that tier,
gated on CODER_TEST_DATABASE_URL, and it skips loudly.
```
