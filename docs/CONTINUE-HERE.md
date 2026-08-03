# Continue here — session handoff

**Written:** 2026-08-04. Read this first, then `docs/node-stack-plan.md`.

## The previous handoff's work is done and committed

The tree that this file used to warn about — several days of W1–W10 and N0–N4
work living **only** in the working tree — is committed. There is nothing
unpushed-and-irreplaceable sitting here now.

- Author is `moontashir01 <moontashir.azim@northsouth.edu>` and correct.
- **Do NOT add a `Co-Authored-By: Claude` trailer** — the repo owner is the
  single author. The existing commits have none; keep it that way.

## Where things stand

| Phase | State |
|---|---|
| N0 — adapter seam | **Done.** `app/agent/stacks/` |
| N1 — `/stack`, spec-driven dispatch | **Done.** |
| N2 — Node scaffold | **Done.** `app/resources/scaffolds/node/` |
| N3 — data layer + PostgreSQL | **Done.** `app/agent/crud_node.py` |
| N4 — verification for Node | **Done, tested and documented.** |
| N5 — readiness gate | **Done.** `SELECT 1` via `NodeAdapter.database_reason` |
| N6 — tests and evals | **Done.** `evals.run --stack node` |

**`docs/node-stack-plan.md` is fully implemented.** What is left below are the
stated gaps and one piece of unfinished business, not unbuilt phases.

## What N4 shipped

All four are implemented **and pinned by tests** (they were not, before):

1. **`.ejs` structural check** — `verify.strip_ejs` + `_check_ejs_text`, wired
   into `check_text` and `is_verifiable`. Catches an unterminated `<%`, a stray
   `%>` and unbalanced HTML underneath. `tests/test_verify.py`, including the
   real scaffold views.
2. **Link validation for path-routed stacks** — `adapter.check_links(text,
   routes)` on **both** adapters, dispatched from `core._check_endpoints`. Flask
   keeps `url_for` name-checking; Node checks paths and allows for `:id`.
   `tests/test_stacks.py`, including the union-across-matching-routes rule.
3. **EJS template graph** — `templatedeps.parse_ejs_template`; `build_graph`
   gained keyword args that all default to the Flask layout.
   `tests/test_templatedeps.py`, including that a no-keyword call is unchanged.
4. **Node spec adoption** — `ProjectSpec.from_disk` tries Python/Flask first,
   then Node. `tests/test_projectspec.py`.

`NodeAdapter.guarantees` / `.gaps` were rewritten to match, and
`test_the_gaps_are_gaps_the_code_really_has` now checks each claim against the
code — the list had gone stale claiming N4's three landed features were missing,
and `/stack` prints it verbatim.

## What N5 shipped

`NodeAdapter.database_reason` runs a real `SELECT 1` through **`node` and the
project's own `pg`**, using the connection string out of the project's own
`db.js`. So "the database does not exist" is now named, with the `createdb`
command, instead of arriving as a failed smoke test. `/run` consults it too.

Three rules to keep if you touch it:

- **Uncertainty resolves to `""` — run the check.** A probe that could not
  complete must never replace the smoke test.
- **A `db.js` that will not load is a CODE defect**, so it reports "cannot tell",
  not "your environment is broken". The probe tells an *absent* db.js apart from
  a *broken* one deliberately; collapsing them made the rule hold only by
  accident, and it broke as soon as `DATABASE_URL` was set.
- **`node --check` the probe script.** A syntax error there is invisible — the
  probe returns nothing, that reads as "cannot tell", and readiness reports a
  clean environment forever. There is a test for exactly this.

## What N6 shipped

`python -m evals.run --webapp --stack node` runs the same tasks against a Node
build. The Phase E checks generalised for free (they read the project's own
spec); how they REACHED the app did not, so `CheckContext.adapter` now supplies
the entry file, the template dir, the route parser and the database reader.

- **The adapter comes from the built project's `.coder/project.json`, never from
  `settings.web_stack`** — `resolve_key`'s precedence, and a test pins it.
- `adapter.table_columns(root)` is the new seam: sqlite `PRAGMA` on Flask,
  `information_schema` over the project's own `pg` on Node. **`None` means "could
  not read" and is never collapsed into "no tables".**
- **`npm install` is the honest gap.** A generated Node project cannot run until
  the network has been used once, so the app-running checks FAIL naming the
  command rather than skipping (W10's rule). `--npm-install` lets the RUNNER do
  it. **A `--stack node` score without that flag measures the files, not the
  running app**, and the run says so up front.

## What is LEFT

1. **The Node eval suite has never been run end to end.** Everything is wired and
   unit-tested offline, but `--stack node --npm-install` needs the network, a
   live PostgreSQL and Ollama all at once, and that has not happened on this
   machine. Until it does, treat the Node score as unmeasured — not as zero, and
   certainly not as passing.
2. **A real PostgreSQL has still never run the generated SQL.**
   `tests/test_crud_node.py` has that tier, gated on `psycopg` +
   `CODER_TEST_DATABASE_URL`. It **skips loudly** — do not make it pass quietly.
3. **Node's remaining gaps are real**, listed in `NodeAdapter.gaps` and printed
   by `/stack`: no import repair, no template-scoped editing, an import
   dependency graph that stays Python-only, and routes read by regex rather than
   by a parser. Flask stays the default because of them.
4. **Two more stage-0 passes are still `.html`-only, and should probably take
   the adapter's `template_ext` the way `_check_endpoints` now does.** Found
   while wiring N4's link check; deliberately NOT changed here, because they are
   separate features and were not part of N4's scope. Both fail silently on a
   Node build, which is the failure mode this codebase cares most about:
   - **`core._fix_upload_form`** (`core.py`, the `(".html", ".htm")` gate) — a
     `<form>` with `<input type="file">` and no `enctype="multipart/form-data"`
     posts only the filename. Nothing about that is Jinja-specific, so an `.ejs`
     upload form currently never gets the fix.
   - **`core._strip_offline_dead_assets`** (same gate plus the CSS types) — a
     generated Node site can still ship a Google Fonts `<link>`, which offline
     costs a DNS timeout per page and then renders in the wrong font. CLAUDE.md's
     "Generated sites are kept offline too" is therefore only true of Flask today.

   `_repair_page_links` and `_repair_nav_consistency` are also `.html`-only and
   that is **fine** — the first rewrites links only when the target exists as a
   sibling file (a real route in a server-rendered app must not be touched), and
   on Node the nav lives in `layout.ejs` and views carry none, so the second has
   nothing to reconcile. Leave both alone.

## Things worth not re-learning

- **`black app/ tests/` reformats files unrelated to your change.** Format only
  the files you touched, or check `git diff` after. (Doing exactly that this
  session touched nothing extra.)
- **The suite takes ~15 min and buffers all output**, so a background run shows
  0 bytes until it finishes. That is normal, not a hang.
- **Time the suite with `ollama serve` stopped.** A resident 7B starves the
  machine and makes the suite look ~3.7x slower — like a regression in whatever
  you just changed.
- **`tests/test_functional_probe.py::test_a_working_app_passes_every_check` is
  flaky under load** — it starts a real Flask server on :5000 with a 20 s
  timeout. It passes in isolation. Do not "fix" it.
- **`_macros.html` fails `check_file`** (tags open in one macro branch and close
  in another). Pre-existing, and inert: the file is `_FROZEN`, so `check_file`
  never runs on it in practice.
- **A stale `gaps` list is worse than no list.** It is printed verbatim by
  `/stack`, so it tells someone choosing a stack the opposite of the truth. When
  you close a gap, close it in that tuple in the same change.
- **`templatedeps._resolve` matches a bare stem only as a last resort.** Express
  writes `res.render("products")` where Jinja writes the full filename. The
  fallback is guarded on the name having no dot at all and is reached only after
  every other match returns "", so it can turn a `""` into a hit and never one
  answer into a different one. Do not widen it.
