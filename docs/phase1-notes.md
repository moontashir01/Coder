# Phase 1 — the web-app scaffold: what shipped and what it exposed

Companion to `docs/phase0-baseline.md`. Recorded **2026-07-31**.

## What shipped

| Piece | Where |
|---|---|
| Runnable Flask skeleton, 13 files | `app/resources/scaffolds/flask/` |
| `is_web_app` / `scaffold_flask` / `scaffold_files` / `is_frozen` / `scaffold_context` | `app/agent/scaffold.py` |
| Scaffold-before-generate, frozen-file drop, truncation reporting | `core.py::_run_blueprint` |
| `scaffolds_dir`; `blueprint_max_files` 12 → 24 | `config/settings.py` |
| Canonical layout taught to the planner | `app/resources/prompts/blueprint.md`, `runtime_probe._flask()` |
| Offline typography (system stacks) | `app/agent/buildspec.py` |
| External-asset stripping | `app/agent/verify.py`, `core.py::_strip_offline_dead_assets` |
| 47 tests | `tests/test_scaffold.py` |

**The phase's own "done when" is met and directly verified:** a freshly scaffolded project
starts with `python app.py` and serves `/` with **200**, with template inheritance
rendering, before a single generated line exists.

## Two deviations from the plan, both deliberate

**1. Only 4 files are dropped from the build plan, not every file the scaffold owns.**
The plan says "drop any planned file the scaffold already owns." Taken literally that
drops `templates/index.html` and `app.py` — leaving the placeholder home page as the
finished site. Only pure boilerplate is frozen (`requirements.txt`, `Procfile`,
`.gitignore`, `.gitkeep`); everything else stays planned and is **edited** onto the
skeleton, because `_file_op_flow` routes an existing file to `_surgical_edit`. So the
domain layer still lands, on top of something that already runs.

**2. `Procfile` binds explicitly.** The plan's `web: gunicorn app:app` leaves gunicorn on
its default `127.0.0.1`, which on Render/Railway/Fly is reachable only from inside the
container — the deploy goes green and the site is still down. Shipped as
`web: gunicorn app:app --bind 0.0.0.0:$PORT`.

## What three live `build me a blog` runs exposed

Everything below was found by running the thing, not by reading it.

### Fixed

- **Jinja read as a file path.** The scaffold put `{{ url_for('static', filename=...) }}`
  into every page, and `references.py` treated it as a relative path: it reported an
  existing stylesheet as missing, and `find_broken_page_links` would have rewritten
  `href="{{ url_for('posts') }}"` into `"{{ url_for('posts') }}.html"` — corrupting a
  working template. Guarded by `references.is_template_expression`.
- **A one-route deletion that killed every page.** `base.html` used `url_for('index')`.
  When generation deleted that route, the resulting `BuildError` fired on *every* page,
  because base.html renders on all of them. Now a literal `/`: worst case is one 404.
- **Generation deletes the scaffold's `/` route — 2 runs out of 2.** The surgical edit
  replaces the block it was asked to add to. `scaffold.restore_index_route` puts it back
  deterministically and says so in the answer. It declines rather than guesses when the
  file isn't a recognisable Flask app, when `/` is still routed, or when
  `render_template` isn't imported — synthesizing a route that raises `NameError` would
  be worse than the 404 it replaces.

Run 3, with the guards in: `GET / -> 200` (runs 1 and 2 both gave 404).

### Fixed in the follow-up pass (builds 4–7)

- **Missing imports.** `app/agent/pyimports.py` parses with stdlib `ast`, finds names
  loaded but never bound anywhere in the module, and adds the import — from an
  **allowlist only**, so an unknown name is reported rather than guessed at, and
  `import models` is added only when `models.py` really exists. Binding is collected
  flat across the whole module (over-approximating scope), so the error direction is
  always "do nothing". The result is re-parsed before being returned, so the pass can
  never hand back a file it broke. Live: `added 3 missing import(s)` on every subsequent
  build, and `undefined_names` is empty afterwards.
- **Template inheritance.** `scaffold.convert_to_child_template` lifts `<body>` into
  `{% block content %}`, carries `<title>` into `{% block title %}`, and drops the
  `<header>`/`<nav>`/`<footer>` that `base.html` already renders — leaving those would
  render two navbars, worse than the drift the layout prevents. Live: **3 of 3 templates
  extend `base.html`** on builds 5–7, up from 0 of 2.

### Two false positives I shipped and then caught

Both were found by running builds, not by reading the code. Worth recording, because
both are the exact failure mode the design claimed to avoid.

- **`__file__` reported as undefined.** It is genuinely not in `dir(builtins)` (unlike
  `__name__`), so the checker flagged `BASE_DIR = Path(__file__).resolve()` — a line the
  *scaffold itself ships*. Fixed with an explicit `_MODULE_DUNDERS` set.
- **`models.add_post` reported as non-existent when it existed.** The cross-module check
  ran per-file, during `app.py`'s write, when `models.py` on disk was still the scaffold
  stub — the very next file in the same build defined the function. Moved to
  `_check_cross_module_calls`, which runs once at the end of the turn when every file is
  final. A check that cries wolf is worse than no check.
- (A third, caught by tests rather than a build: `missing_tables` first scanned raw
  source, so `from flask import Flask` matched its `FROM <table>` pattern and it reported
  `flask` as a missing table. It now extracts string literals via `ast` — which also
  means the scaffold's *commented* `CREATE TABLE` example correctly does not count as
  creating anything.)

### Open — and this is precisely what Phase 2 is for

The blog still does not fully work. Functionally probed (GET every page, POST a real
form), build 7 gives:

```
GET /              -> 200
GET /posts         -> 500
GET /posts/new     -> 200
POST /posts/new    -> 500
```

Two root causes, both now **reported honestly** rather than shipped silently:

1. **`app.py` calls `models.get_all_posts`; `models.py` never defines it.** Both files
   compile, the import resolves, and the route dies with `AttributeError`.
   `unresolved_local_calls` reports it exactly.
2. **No `CREATE TABLE` is ever emitted.** `init_db()` keeps the scaffold's commented
   example while `app.py` runs `SELECT * FROM posts` → `no such table: posts`.
   `missing_tables` reports it. On one build a surgical edit also *duplicated* `db.py`'s
   entire tail, leaving two `init_db()` where the second (table-less) one wins —
   `duplicate_definitions` reports that.

Neither can be repaired deterministically today, and deliberately so: writing
`get_all_posts` means inventing a query, and creating the table means inventing its
columns. That is generation, not repair. **A structured entity list is the missing
input** — which is exactly `ProjectSpec.entities` (Phase 2) feeding `spec.ddl()` /
`spec.migrations()` (Phase 3) and `crud.py` (Phase 4a). The plan already says schema
changes must not be generated; these three checks are what make the absence visible in
the meantime.

**"Started" still is not "works."** Build 6 is the cautionary case: the smoke test probed
`GET /posts/new`, got 200, and reported success — while `/posts` and every POST returned
500. Any HTTP status counts as alive. That is Gap 3 verbatim, and Phase 5's functional
probe is the fix. Until it exists, do not read a green smoke line as a working app.

## Testing note carried over

Time the suite with `ollama serve` **stopped**. Measured on identical code and tests:
171s with a 7B model resident vs 46s without — a warm model looks exactly like a
performance regression. See the gotcha in `CLAUDE.md`.
