# Always Full-Stack — schema-first builds with a working project memory

Successor to `docs/fullstack-web-plan.md`. That plan made Coder *able* to build a
Flask/Jinja/SQLite site. This one makes it do so **for every website request**, derive the
**layout from the schema** instead of alongside it, and keep a project memory good enough
that turn N can update any file turn 1 wrote.

Three concrete promises, in the order they must be earned:

1. **Any website request produces a full-stack app on the fixed stack** — not sometimes,
   and never a silent downgrade to static HTML or to `http.server`.
2. **The schema is decided first, and the pages are derived from it** — so "the layout
   matches the data" is true by construction, not by prompt.
3. **The project's files are remembered**, so "add reviews to the products page" resolves
   to real files with real routes and real columns.

---

## Where it stands today

The pieces exist and are good; the wiring is what fails the promise.

| Promise | Today | Verdict |
| --- | --- | --- |
| Always Flask | `detect_stack()` auto-probes; Flask wins only because Coder's own `requirements.txt` pins it | accidental |
| Always full-stack | `should_blueprint()` is a verb×noun regex with four vetoes | misses most phrasings |
| Schema first | one blueprint call emits files *and* free-text `data_schema` together | reversed |
| Layout matches schema | `_guess_entity()` substring-matches paths; `Page.reads` is inferred from prose and "routinely empty" | guessed |
| Project memory | `ProjectSpec` → `.coder/project.json`, `impact.py` | real, but only for projects Coder blueprinted |
| Memory covers files | `files: dict[path, role]` — four role words | too thin to route an edit |

Three failures follow directly, and each is reachable today:

- **"Build me a recipe organizer"** → `organizer` is not in `_BLUEPRINT_NOUN_RE`
  ([blueprint.py:54-62](../app/agent/blueprint.py#L54-L62)) → no blueprint → static HTML
  with no server and no database.
- **A project cloned from git, or built before `ProjectSpec` existed** → `ProjectSpec.load`
  returns None → no memory, no amendment path, no impact analysis. `entities_from_sql()`
  and `routes_from_source()` could recover all of it and are never called for this.
- **A plain `_file_op_flow` edit that adds a route** → the spec is not updated (only
  `_run_blueprint` and `_amend_project` write it), so the memory silently drifts from disk
  and the *next* amendment plans against a stale contract.

---

## Phase A — Force the stack, and fail loudly if it is absent — **DONE (2026-08-02)**

Small, and everything else assumes it. Shipped as written, with one design change made
during implementation: the "not installed" warning is a **new `Stack.install_hint` field**
rather than part of `note`. `note` is the generation instruction, and
`prompts/blueprint.md` tells the model not to use a framework that isn't available — so a
warning inside `note` would have made the model write a stdlib app, causing the very
downgrade the phase exists to report. `install_hint` is user-facing only; the model never
sees it. Two follow-ons fell out of the same reasoning: the smoke test is skipped when
`stack.runnable` is False (otherwise the repair loop rewrites correct code to fix a missing
package), and two prompt strings that claimed the stack was "what runs on this machine"
were reworded, since that is no longer necessarily true.

`requirements.txt` was left alone — it is a UTF-16 `pip freeze` snapshot with no structure
to document anything in. Flask is declared in `pyproject.toml` instead, which is the real
manifest. Full suite: **914 passed**.

- Add `settings.web_stack: str = "flask"` (`auto|flask|fastapi|stdlib|none`) and thread it
  as `prefer=` into both `detect_stack()` call sites —
  [core.py:205](../app/agent/core.py#L205) and [core.py:2432](../app/agent/core.py#L2432).
  The `prefer` parameter already exists ([runtime_probe.py:65-104](../app/agent/runtime_probe.py#L65-L104))
  and is currently used by nothing but tests.
- **Change the fall-through.** `prefer="flask"` with Flask missing currently drops to the
  `auto` branch and quietly returns the stdlib stack. That is the silent-downgrade failure
  class this codebase fights everywhere else. Instead return
  `Stack(language="python", backend="flask", runnable=False, note="Flask is not installed…")`
  and have `_run_blueprint` lead its answer with `pip install flask` rather than building a
  different kind of app than the user was told they'd get.
- Move `Flask` out of Coder's own `requirements.txt` accident into an explicit, documented
  runtime dependency of *generated projects*.

**Test:** extend `tests/test_blueprint.py`'s `detect_stack` cases with prefer-flask-present,
prefer-flask-absent, and assert the absent case is `runnable=False` rather than stdlib.

---

## Phase B — Every website request gets the full-stack path

`should_blueprint()` is narrow *by design* — the module docstring calls the narrow gate one
of three leashes. Widening it is a deliberate reversal for web requests only, so it needs
its own leash.

**Two-tier gate**, cheapest first:

1. **Tier 1 — the existing regex.** Keep it verbatim. When it fires it is right and free.
2. **Tier 2 — one temperature-0 micro-call**, reached only when tier 1 *misses* and the
   message is not a question, a split/refactor, or an amendment: *"Is this asking to build
   a web application? Answer YES or NO."* One token. Gated by
   `settings.web_intent_fallback` (default on). This is what catches "recipe organizer",
   "something to track my expenses", "a place my club can post events" — the open-ended
   phrasings a noun list can never enumerate.

Two vetoes need adjusting, both in [blueprint.py:76-88](../app/agent/blueprint.py#L76-L88):

- `_SINGLE_FILE_ONLY_RE` stays for `"a css file"` / `"a new js file"`, but must stop
  vetoing when the request also names a site/app/page. "Make a new html file for the about
  page" of an existing project is an amendment, not a static one-off.
- `_EDIT_INTO_RE` currently matches `add|include … to|into|in`, so **"build a shop with
  reviews included in it"** is vetoed as an edit. Require that the edit verb *lead* the
  message, rather than appear anywhere in it.

**Precedence is unchanged and load-bearing:** `should_amend` still runs first
([core.py:3785](../app/agent/core.py#L3785)). Widening the build gate must never make turn 2
rebuild turn 1's project.

**Escape hatch.** A full build is many LLM calls and minutes on a 7B; someone who wants one
static page must still get one. Keep an explicit opt-out (`"just html"`, `"static only"`,
`"no backend"` → `prefer="none"`), and say in the answer which path was taken.

---

## Phase C — Schema first, layout derived from it

The heart of the request, and the only phase that changes the pipeline's shape.

Today one call produces files and schema simultaneously, and the schema arrives as free
text (`"users(email TEXT PRIMARY KEY, …)"`) that `parse_schema_line` has to reverse-engineer
afterwards. Split it in two:

**C1 — the schema call** (`app/resources/prompts/schema.md`, temp 0). Request in,
*structured entities only* out:

```json
{"summary": "...",
 "entities": [{"name": "product", "table": "products", "purpose": "one item for sale",
               "fields": [{"name": "id", "type": "INTEGER", "pk": true},
                          {"name": "title", "type": "TEXT", "required": true},
                          {"name": "image_path", "type": "IMAGE"}]}]}
```

Parsed by a new pure `projectspec.entities_from_data()`, reusing the existing `_ident` /
`_norm_type` / `MAX_*` validation. No prose, no `parse_schema_line` round-trip. Coder's
`IMAGE`/`FILE` upload types ([projectspec.py:108-114](../app/agent/projectspec.py#L108-L114))
become things the model states directly rather than things inferred from a column name.

**C2 — the layout call**, given the entities as a rendered table. `prompts/blueprint.md`
gains the rule that every page declares the entities it `reads` and every form declares the
entity it writes — so the fields the existing prompt already asks for get grounded in a
schema the model was handed instead of one it is inventing in the same breath.

**C3 — deterministic completion** (`blueprint.derive_pages_from_entities()`, pure, no LLM).
Every declared entity must end up with a list page, a create form, and the routes behind
them. If the layout call omitted any, synthesize them from the entity. This is the phase's
actual guarantee: the same "deterministic beats generated" rule that already produced
`scaffold.py` and `crud.py`, extended to routes and pages. A prompt rule is a hope; this is
a postcondition.

**C4 — the spec records declared relationships, not guessed ones.**
`ProjectSpec.from_blueprint` takes `Page.reads` and `SpecEndpoint.entity` from C2's
declarations, and `_guess_entity`'s substring match
([projectspec.py:1271-1276](../app/agent/projectspec.py#L1271-L1276)) drops to a fallback.
This also fixes Phase 5's known false-failure source — `smoke.py` had to check *every* page
precisely because `reads` was unreliable.

**Cost:** one extra LLM call per greenfield build. Cheap against a build turn, and it
removes the free-text→structured conversion that entities have to survive today.

---

## Phase D — A memory that covers the files, not just the contract

Four gaps, in value order.

**D1 — Adopt projects Coder did not build** — **DONE (2026-08-02)**. Landed as planned, plus
three decisions the plan didn't anticipate. (a) It **declines** unless a real route is
defined, so an ordinary Python folder never acquires an invented contract; routes registered
on a Blueprint (`@bp.route`) aren't recognised by `_ROUTE_RE`, and such a project declines
rather than being adopted wrongly. (b) It **saves nothing** — writing `.coder/project.json`
into someone's repo because they asked a question about it is an unrequested side effect, and
the first amendment persists it anyway; it is recomputed per turn rather than cached, since a
cache goes stale exactly when a turn writes a route without amending (the drift D3 closes).
(c) **`_write_readme` now only overwrites a README Coder wrote** (`README_MARKER`, also added
to the scaffold's copy) — adoption is what made an existing repo able to reach the amendment
path on turn 1, where regenerating a hand-written README would have destroyed the user's work.
A pre-existing test asserted the scaffold-README replacement against an invented stand-in
rather than the shipped file; it now uses the real one. Full suite: **929 passed**.

Original plan text: pure reuse of
machinery that already exists and is never called for this: `entities_from_sql()` reads real
`CREATE TABLE`s, `routes_from_source()` reads real `@app.route` → `render_template` pairs,
`is_layout_template()` excludes `base.html`, `scaffolded_files()` finds the rest. Call it
from `load_project()` and from `chat()` when `ProjectSpec.load` returns None. Highest
value-per-line in the plan: it turns "memory only for projects built in this session" into
"memory for any Flask project on disk".

**D2 — A real file index.** Widen `files: dict[str, str]` to a `FileRecord` carrying `role`,
`purpose`, `defines` (routes, view functions, template blocks), `reads` (entities), and
`revision`. The on-disk format already tolerates this — `_load_files` reads
`value.get("role") if isinstance(value, dict)`
([projectspec.py:1444](../app/agent/projectspec.py#L1444)) — so old `project.json` files
keep loading. Populate `defines` from `routes_from_source` plus the symbol index
(`app/rag/symbols.py`), which already stores per-file definitions and is currently unused by
the spec.

**D3 — Reconcile after every write turn.** New `_sync_spec_after_writes(trace)` at the
`chat()` seam, next to `_repair_dead_references`, so *any* path that writes files updates the
spec — not only `_run_blueprint` and `_amend_project`. Best-effort, same discipline as the
rest of the spec: a reconcile failure never costs a turn whose files were written. Without
this the memory drifts the moment someone edits a file the ordinary way.

**D4 — Spec context everywhere, and target resolution from it.**
- Thread `spec.to_context_block()` into `_file_op_flow` / `_build_messages` whenever a spec
  exists, not only on the amendment path. Then a request that misses `_AMEND_VERB_RE`
  ("the nav should have a contact link") still reaches the model with the project's real
  routes and tables attached.
- Add `_resolve_target_from_spec(msg)`: "the products page" → `templates/products.html` via
  the `pages` table's `nav_label` / `route` / `template`. Today `_extract_filename` is a
  filename regex that falls back to "the last file I wrote", which is why a follow-up naming
  a page by its *label* rather than its filename lands on the wrong file.

---

## Phase E — Prove it

Extend the `--webapp` eval suite; the checks that matter already exist (`db_has_column`,
`post_persists`, `earlier_pages_still_work`).

- One task per request shape: blog, shop, booking, **and one deliberately outside
  `_BLUEPRINT_NOUN_RE`** ("something to organize my recipes") — that task is Phase B's
  regression test.
- Per task assert: a Flask app exists (not static HTML), `db_has_column` for **every**
  declared entity, every entity has a reachable list page, `post_persists`.
- Then turn 2 adds a field and turn 3 adds a page, asserting `earlier_pages_still_work`.

Per CLAUDE.md's eval lesson: the planner runs at temperature 0.2, so **a single run proves
nothing** — re-run a suspect task ~5× against a stashed baseline before calling anything a
fix or a regression.

---

## Order, and what each phase costs

| Phase | Depends on | Rough size | Buys |
| --- | --- | --- | --- |
| A — force the stack | — | small | the stack stops being an accident |
| D1 — adopt from disk | — | small | memory for any project, not just fresh builds |
| C — schema first | A | large | layout that provably matches the data |
| B — widen the gate | A, C | medium | *any* website request, not a noun list |
| D2–D4 — file memory | C, D1 | medium | "update any file" actually resolves |
| E — evals | all | medium | proof, and a regression net |

A and D1 are independent and cheap — do them first for immediate movement. **C before B**
is deliberate: widening the gate first would route far more requests into a pipeline whose
schema handling is still the weak link, making every new failure a build failure.

## Risks worth stating up front

- **Every page request becomes a full build.** Minutes and many LLM calls on a 7B where a
  static page took one. The tier-2 classifier and the explicit static escape hatch are the
  mitigations; if builds still feel heavy, the next lever is caching the schema call per
  session, not narrowing the gate again.
- **The tier-2 classifier is a new false-positive surface.** "Explain how a login page
  works" must not build one. `_QUESTION_RE` still runs ahead of it, and the classifier
  prompt should be shown the question veto, not asked to re-derive it.
- **Widening the build gate can shadow the amendment gate.** `should_amend` runs first today
  and must keep running first; Phase B needs a test pinning that turn 2 of a two-turn
  conversation still amends.
- **Forcing Flask makes Coder's offline principle load-bearing on one package.** Phase A's
  loud failure is the whole mitigation — a downgrade the user is told about is fine, a
  silent one is the bug.
