# Phase 2 — ProjectSpec: what shipped and what the live run corrected

Companion to `docs/phase0-baseline.md` and `docs/phase1-notes.md`. Recorded **2026-07-31**.

## What shipped

| Piece | Where |
|---|---|
| Data model (`Field`/`Entity`/`SpecEndpoint`/`Page`/`SpecFeature`/`HistoryEntry`) | `app/agent/projectspec.py` |
| `load` / `save` (atomic, direct write), `from_blueprint`, `to_context_block` | same |
| `entity()`, `ddl()`, `migrations(since=…)`, `merge_delta()` + `SpecDelta` | same |
| Free-text `data_schema` → structured fields (`parse_schema_line`) | same |
| Save after a build; reload into `self._spec` every turn; `get_spec()` accessor | `core.py` |
| `/spec` | `app/cli/commands.py` |
| 53 tests | `tests/test_projectspec.py` |

**Done when — met.** A live `build me an e-commerce site for selling books` leaves a
`.coder/project.json` that accurately describes what was built, and `/spec` renders it.

## The conversion the phase turns on

`ApiContract.data_schema` is a tuple of free-text strings. `parse_schema_line` turns
`"products(id INTEGER PRIMARY KEY, title TEXT NOT NULL, price REAL)"` into fields with
types normalised to SQLite storage classes, each stamped `added_in`. That stamp is the
whole point:

- `ddl()` emits `CREATE TABLE` using only revision-1 fields.
- `migrations(since=n)` emits `ensure_column(conn, "products", "image_path", "TEXT")`
  for everything added later.

So adding a field in turn 3 is an idempotent `ALTER TABLE ADD COLUMN` against the rows
already stored, not a table drop. A test runs the real `ensure_column` primitive against
in-memory sqlite3 **with a row already in the table** and asserts the row survives —
"emits SQL-shaped text" and "emits SQL that works" are different claims.

## What the live run corrected

The first live build wrote a spec that was *present* but not *accurate*. Reading the JSON
against the project on disk found three defects, all now fixed and pinned with tests.

1. **`templates/base.html` was recorded as a page**, with the invented route `/base` and
   nav label "Base". It is the shell every page extends. An amendment turn would have
   tried to add a nav link to a route that does not exist. Fixed by `is_layout_template`,
   which detects it by name *and* by shape (`{% block %}` with no `{% extends %}`).

2. **`POST /api/login` was recorded as an existing route, and it had never been built** —
   the coverage check reported it unwired on the same turn, so the two disagreed and
   nothing surfaced it. This is the worst of the three, because `to_context_block()` says
   *"Routes that already exist — do not redefine or rename them"*: listing an unbuilt
   route is an instruction not to build it. A declared endpoint is now kept only when the
   route parser found it or its path appears as a literal in a `.py` file. With no backend
   on disk at all, the contract survives unchanged (there is nothing to check against).

3. **The home page was missing.** `GET /` renders the scaffold's `templates/index.html`,
   which was copied in rather than planned, so it never appeared in `bp.files` and never
   became a page. Pages are now also derived from real `GET` routes that render a template.

After the fixes, on the same prompt:

```
routes really in app.py : [('GET','/'), ('GET','/login'), ('GET','/products'), ('POST','/login')]
routes in spec          : [('GET','/'), ('GET','/login'), ('GET','/products'), ('POST','/login')]
real routes missing from spec: none
pages: /login, /products, /  (all three templates exist on disk)
entities: user(users: email, password_hash), product(products: id, title, author, price)
context block: 608 chars against a 1200 budget
```

Three more bugs were caught by the unit tests rather than the build: `_singular("status")`
returned `"statu"`; the `CREATE TABLE` regex anchored to end-of-string, so it missed
statements mid-file and truncated a column list at `DECIMAL(10,2)` (now a paren-balanced
scan); and a fallback made the scaffold's *commented* `CREATE TABLE` example count as a
real table.

## Design notes worth keeping

- **`from_blueprint` reads the filesystem on purpose.** The blueprint states intent; the
  "done when" asks for a description of what was *built*. Taking `root` (which the plan's
  signature already does) is what lets the spec record real route→template pairs, recover
  entities from real `CREATE TABLE` statements when the blueprint declared none, and
  refuse to claim an endpoint that was never written.
- **`save()` bypasses the executor deliberately** — see the module docstring. The spec is
  agent state; the approval gate and `.coder_backups/` are for user code.
- **`merge_delta` is implemented but not yet exercised by a real amendment.** It bumps the
  revision, stamps new fields with it, records history, and returns the files the spec
  alone knows are affected. Phase 3's `impact.py` owns the full rule set (which display
  templates read a changed entity, and so on).

## Carried forward, unchanged from Phase 1

The generated app still has runtime gaps that Phase 2 does not address by itself — a
route calling a `models.` helper that was never written, and `init_db()` shipping without
a `CREATE TABLE`. The spec is now the missing *input* for fixing both deterministically:
`spec.ddl()` can write the schema block and `spec.entities` can drive CRUD generation.
That is Phase 3 (deterministic `db.py` migration block) and Phase 4a (`crud.py`).
