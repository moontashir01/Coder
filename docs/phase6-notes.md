# Phase 6 — demo surface

Companion to `phase0-baseline.md` … `phase5-notes.md`. Recorded **2026-08-01**.

## What shipped

| Piece | Where |
|---|---|
| `AppRunner` — one long-lived app process, owned by the session | `app/agent/apprunner.py` |
| `/run [restart\|stop\|status]` | `app/cli/commands.py` |
| `/spec` (shipped in Phase 2) | same |
| `/plan` extended with the amendment preview | same, `core.preview_amendment` |
| `README.md` rendered from the spec | `ProjectSpec.to_readme`, `core._write_readme` |
| 21 tests | `tests/test_apprunner.py` |

## `/run`

The smoke test starts the app, probes it and kills it within seconds —
deliberately, because it executes generated code. That is the wrong shape for a
demo: the person watching wants a URL they can open, that keeps working while
the next turn amends the project.

Verified live against a real generated project:

```
App running at http://127.0.0.1:5000
opened http://127.0.0.1:5000 -> 200, 1327 bytes
App: running at http://127.0.0.1:5000 (…/p5shop3)
Stopped.
```

Three decisions worth recording:

- **One process, never a pool.** Two copies of the same app fight over the port
  and over `app.db`, and "which one am I looking at?" is not a question anyone
  should face mid-demo. A second `/run` reports the existing URL instead of
  launching a rival.
- **Process-tree kill plus an `atexit` hook.** Reuses `smoke.py`'s `_kill_tree`
  (Windows `taskkill /T`, POSIX process-group kill) so a crashed REPL cannot
  orphan something holding :5000. Not hypothetical — orphaned `llama-server`
  processes from killed parents cost this project a build
  (`docs/phase4-notes.md`).
- **An app that starts but never answers is reported, not claimed.** Saying
  "running at http://…" about a URL that returns nothing is the kind of small
  lie that wastes a demo.

There is a test that `stop()` genuinely frees the port, because an orphan on
:5000 is precisely the failure that would surface at the worst moment.

## `/plan` — the amendment preview

Kept rather than cut (the plan's trim guidance says "keep `/spec`, cut `/plan`"),
for one reason: it is the only place the impact rules are visible on their own.
When a spec is loaded and the request reads as an amendment, `/plan` shows the
delta, the new files, and a table of **existing** files that will be updated with
the reason for each — before anything changes:

```
Amendment to revision 1 — add product images
Adds
  + product.image_path (TEXT)
New files
  + templates/admin.html
┌──────────────────────┬──────────────────────────────────────────────┐
│ File                 │ Why                                          │
├──────────────────────┼──────────────────────────────────────────────┤
│ models.py            │ include image_path in the column lists …     │
│ templates/index.html │ show image_path for each product             │
└──────────────────────┴──────────────────────────────────────────────┘
Nothing has been changed — run the request to apply it.
```

It costs the same single delta-extraction call the real amendment would, and
falls back to the ordinary planner when there is no spec or the request is not an
amendment.

## The README

The scaffold ships a generic README. Leaving it means the file describes the
*template* rather than the project, and by turn 3 a README documenting turn 1 is
worse than none. `ProjectSpec.to_readme()` renders the real thing — pages,
routes, columns, `added in revision N` on later fields, and the deploy line with
the `--bind 0.0.0.0:$PORT` that gunicorn's default would otherwise get wrong. It
is rewritten whenever the spec is saved, on builds and amendments alike.

Rendered from a real project's spec:

```
# P5Shop3
Build an e-commerce site for selling books with a page to add products …
## Pages     /add_product -> templates/add_product.html, / -> templates/index.html
## Routes    GET|POST /add_product (product), GET /
## Data      products: name TEXT (primary key), description, price REAL, image_url
```

## One thing that caught me out

The live demo-surface run printed the **scaffold's** README, not the generated
one — because that project was built during Phase 5, before `_write_readme`
existed. Nothing was wrong; the project simply predated the feature. Worth
recording because it is an easy way to misread a live check: an old artifact
cannot demonstrate new behaviour. The rendering was verified against that same
project's real spec, and two tests now cover the integration point, including
that a README which cannot be written never costs a built turn.

## Still open

Unchanged from Phase 5: the generated shop has no product listing page, so the
functional probe's "the posted value is not shown on any page" is a true report.
`/run` will happily serve that app — the demo surface shows what exists, it does
not improve it.
