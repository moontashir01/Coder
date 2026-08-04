# Continue here — session handoff

**Written:** 2026-08-04. Read this first, then `docs/node-stack-plan.md`.

## The Node stack has now been RUN, and running it found six things

A generated Express app was built from a real 12.5 KB PRD, installed, seeded and
served against a live PostgreSQL 18, and every page was requested. That is the
first time the Node stack has been exercised end to end, and it is where all six
of the following came from — **none of them was visible to any check that reads
bytes**, and every one of them shipped inside a build whose answer said
"verified OK":

1. `server.js` truncated at the last handler — no `initDb`, no `listen`, no 404
   handler. The app could not start. `node --check` passes on a file of route
   registrations, and `restore_entry_route` anchors on the lines that had been
   deleted, so it declined without saying anything.
2. Two routes deleted by `_wire_missing_endpoints`, which had been asked to ADD
   one. The loss was reported and not acted on.
3. `db.setup()` against a `db.js` exporting `initDb` — the cross-module check
   was Python-only and returned `[]`.
4. Every listing page 500 on `empty_state is not defined`: EJS compiles to
   `with (locals)`, so a free identifier is a render-time ReferenceError.
5. Every create form a `ReferenceError` before the database: a canonical TEXT
   primary key had no default, so `id` was the insert helper's first argument.
6. One empty optional form field killed the process — PostgreSQL refuses `""`
   for an integer, and Express 4 does not forward a rejected `async` handler.

**The durable lesson is the one this file already states, arriving through a
sixth door: a check that cannot run reads exactly like a check that passed.**
Three of the six were *guarded* — by `restore_entry_route`, by the coverage
report, by `_check_cross_module_calls` — and each guard declined silently on the
one input that mattered. When you add a check, test what it does on the input it
is meant to catch, not only on the input it is meant to pass.

All six are fixed, with regression tests in `tests/test_entry_repair.py`,
`tests/test_jsdeps.py`, `tests/test_ejslocals.py` and additions to
`tests/test_crud_node.py`. Each new repair has a test asserting `core` CALLS it,
and in the order that makes it work — the boot block before the routes (a
restored route needs the 404 handler to be placed relative to), the ordering
pass after both restores (they insert at the wrong end), and the view check last
(it reads `res.render` out of the finished entry file).

**Still unmeasured, and must not be reported otherwise:** the app was proven on a
throwaway cluster, so nothing here says anything about a particular machine's
server; and the `--stack node --npm-install` EVAL SUITE has still never been run,
so the Node *score* remains unmeasured exactly as it was.

## Then the repairs themselves produced four more, and the lesson repeated

Running the *fixed* builds found four defects in the fixes, and every one was
the same shape a third time:

7. `reinstate_routes` re-inserted a block sliced from a file whose routes were
   nested in a callback, one closing brace too many, and the build shipped a
   `server.js` that would not parse. **`NodeAdapter.write_source_if_valid` was
   not running `node --check`** — the Flask gate compiles, this one only asked
   "is this HTML in a .js file?". It runs the real check now and reverts.
8. `order_routes` returned `[]` both for "the order is fine" and for "I cannot
   read this file's shape", so a real collision was silent. It returns
   `(text, moved, problems)` now, and a decline is stated.
9. The route record was filled only on a BUILD turn, leaving the amendment path
   — where the user literally asks for routes to be preserved — unprotected.
10. `_extract_filename` created a file named `app.get` from a sentence about
    `app.get(...)`. The blocklist could never have held it.

**Two rules worth carrying forward.** First: whenever the Flask path has a
guarantee, ask out loud whether the Node path has it too — six of the sixteen
defects this session were exactly that gap, and each was invisible because the
Node side returned a neutral value rather than failing. Second: **a repair pass
needs the same net as generation.** These passes write files without going
through `_verify_and_repair`, so the write gate is the only thing standing
between a bad slice and the user.

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

1. **A generated Node project HAS now been built and run end to end** (2026-08-04,
   PostgreSQL 18.4 + `npm install` + `node server.js`), and it found two defects
   that only a live database could show. Both are fixed, both have regression
   tests in `tests/test_crud_node.py`:
   - `_sample` let **`NUMERIC` fall through to the string default**, so
     `seed.js` died with `invalid input syntax for type numeric: "Demo
     cod_reliability_score 1"`. SQLite has type AFFINITY and accepts that
     string, so **the Flask seed was wrong in the same way and looked fine** —
     the two `_sample`s must stay in step. `BLOB` had the same hole (`BYTEA`).
   - The scaffold's `server.js` **never required `models`**, so every data page
     answered `{"error":"models is not defined"}`. Flask survives the identical
     omission only because `_repair_missing_imports` adds it, and that pass is
     Python-only — see gap 3. A scaffold on this stack has to be right unaided.

   The `--stack node --npm-install` EVAL SUITE is still unrun; what happened was
   a single real build, not the scored suite. Treat the Node *score* as
   unmeasured.
2. **A real PostgreSQL has now run the generated SQL** — `initDb()` created all
   five tables and the app served `/`. `tests/test_crud_node.py`'s live tier is
   still gated on `psycopg` + `CODER_TEST_DATABASE_URL` and still **skips
   loudly** here (psycopg is not installed); do not make it pass quietly. The
   run above went through **node and the project's own `pg`**, which is the same
   path `NodeAdapter.database_reason` uses and a different one from that tier.
3. **Node's remaining gaps are real**, listed in `NodeAdapter.gaps` and printed
   by `/stack`: no import repair, no template-scoped editing, an import
   dependency graph that stays Python-only, and routes read by regex rather than
   by a parser. **`settings.web_stack` was switched to `"node"` on 2026-08-04 by
   the repo owner, and none of these gaps closed when it was** — the default now
   points at the shallower stack deliberately, so read `NodeAdapter.gaps` before
   treating a Node build's output as being held to Flask's guarantees.
   `stacks.DEFAULT_KEY` is still `"flask"`, and that is a different question (an
   empty/unknown key, including every spec written before the seam existed);
   changing it would reinterpret existing Flask projects as Express ones.
4. **`_repair_page_links` and `_repair_nav_consistency` are still `.html`-only,
   and that is FINE** — do not "fix" them to match the others. The first
   rewrites a link only when the target exists as a sibling file, because a real
   route in a server-rendered app must not be touched; on Node every path IS a
   route. The second reconciles navs across pages, and on Node the nav lives in
   `layout.ejs` while views carry none, so it has nothing to reconcile.

   (The other two — `_fix_upload_form` and `_strip_offline_dead_assets` — WERE
   `.html`-only and are now fixed; see below.)

## The last two `.html`-only passes, now closed

`core._fix_upload_form` and `core._strip_offline_dead_assets` both took the
stack's `template_ext`, the way `_check_endpoints` did in N4. Before that, a
`.ejs` upload form could not be repaired by anything (`fix_form_enctype` has
exactly one caller), and a Node build's only defence against a CDN `<link>` was
a prompt-level hint the model is free to ignore.

**The important part was not the gate.** Widening it alone would have started
CORRUPTING files: `<% … %>` and `{% if a > b %}` can contain a `>`, every regex
in `verify.py` scans attributes with `[^>]*`, and both of these passes write
their match back — so `<form action="<%= u %>">` would have been rewritten as
`<form action="<%= u % enctype="…">`. `verify.mask_template_tags` blanks those
expressions to **equal-length** spaces so spans still line up, and
`strip_external_assets` now cuts by span instead of re-running `re.sub` on raw
text. A file with no template tags comes back byte-for-byte.

This also fixed the Jinja half — `{% if a > b %}` inside a tag was being
corrupted on Flask before anyone noticed.

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
