# Phase 7 — evals that mirror the demo

Companion to `phase0-baseline.md` … `phase6-notes.md`. Recorded **2026-08-01**.
The last phase of `docs/fullstack-web-plan.md`.

## What shipped

| Piece | Where |
|---|---|
| Multi-turn `EvalTask` (`prompts`) + `run_task` looping one workdir/one agent | `evals/harness.py` |
| `spec_has_entity`, `spec_has_endpoint`, `db_has_column`, `app_serves`, `earlier_pages_still_work`, `post_persists` | `evals/checks.py` |
| `WEBAPP_TASKS` — the demo, turn for turn | `evals/tasks.py` |
| `--webapp` | `evals/run.py` |
| 22 tests | `tests/test_webapp_evals.py` |

## The harness change is the load-bearing part

`EvalTask` gained `prompts: list[str]` alongside `prompt`, and `run_task` now runs a
whole **conversation** against ONE workdir with ONE agent, checking only after the last
turn. `prompt` still works, so all ~14 existing tasks are untouched — there is a test for
that.

The shared agent is not an optimisation. A fresh agent per turn would reload the spec from
disk between turns and mask exactly the in-memory staleness this suite exists to catch.

A turn that raises stops the conversation, because every later turn would then be measuring
the wrong thing. `ctx.answers` carries every turn's answer, so a check can look at what
turn 2 said rather than only the last thing printed.

## The checks, and why each is worth its runtime

- **`spec_has_entity` / `spec_has_endpoint`** — does the project *remember* what it built?
  This is the Phase 2 capability, asserted rather than assumed.
- **`db_has_column`** — asks the **database**, not the source. A `CREATE TABLE` in a file
  nobody executes proves nothing; this is what separates "the schema changed" from "the
  schema was described". Phase 1 and Phase 4 both shipped builds that would have passed a
  source-level check and failed this one.
- **`app_serves`** — starts the real app and requires each route to answer.
- **`post_persists`** — POST, then require the value to come back. A handler that answers
  302 and never writes passes every other check in the file and fails this one.
- **`earlier_pages_still_work`** — the headline. Not "did turn 3 work" but **"did turn 3
  break turn 1"**. During Phase 3 an amendment deleted turn 1's `/products` route while
  reporting success: the file compiled, the new route worked, and nothing else could see
  it. That is the failure this number tracks.

## The suite

```
web_turn1_build   build me an e-commerce site for selling books
web_turn2_amend   + add an admin page where I can add a product with a picture
web_turn3_cart    + add a shopping cart
web_turn4_search  + now let customers search products by title
```

Each task is the whole conversation up to its turn, so turn 3's task really does build,
amend, and then amend again. Every task after the first asserts `earlier_pages_still_work`.

Run it with `python -m evals.run --webapp`. It is deliberately its own suite: ten live
builds, each of which starts the generated app. `--webapp` also turns the smoke test off,
because the checks do the running and the two would fight for port 5000.

## How to read the number

Per `CLAUDE.md`, the planner runs at temperature 0.2, so **a single run proves nothing
directional**. Run the suite ~5× before treating a change as a fix or a regression, and
compare against a stashed baseline. `--keep DIR` is the fastest diagnosis: the wrong
content sitting inside the wrong file names the bug immediately.

Expect this suite to score lower than `--blueprint` does, and that is the point. It asks
harder questions — "does the database have the column", "does the value come back", "does
turn 1 still serve" — and the earlier suites were passing while builds were visibly broken.
A suite that scores 100% on a broken app is worse than one that scores 50% honestly.

## The first real number: 0/2

Turns 1 and 2 were run live and scored against their kept artifacts:

```
[FAIL] web_turn1_build  (1 turn)
       - ok:   file app.py exists
       - ok:   file templates/base.html exists
       - ok:   spec has entity product
       - FAIL: app did not start: SyntaxError: invalid character '—' (U+2014)
[FAIL] web_turn2_amend  (2 turns)
       - ok:   spec has POST …/
       - FAIL: products has: author, id, price, title
       - ok:   earlier pages: all 1 served (/)
```

**0/2, and that is the suite earning its keep on its first run.** Every earlier suite
would have scored `web_turn1_build` a PASS: the files exist, the spec has the entity, a
socket answers. Only `app_serves` noticed that the app does not start at all.

The cause is worth recording. `app.py` line 1 was
`Web Turn1 Build — Flask application entry point.` — the scaffold's module docstring **with
its opening `"""` clipped off** by a surgical edit. The em-dash only makes the error
dramatic; any text in that position is a `SyntaxError`. The file was written, the syntax
repair did not recover it, and it shipped.

**`earlier_pages_still_work` passed on turn 2** — the headline check, on the one task that
could exercise it. Turn 1's `/` still served after the amendment.

### Fixed as a result

The deterministic passes that hand-edit Python — `restore_index_route`,
`restore_page_routes`, both migration blocks — run OUTSIDE `_verify_and_repair`, so nothing
would have noticed if one of them produced source that does not parse. They now go through
`_write_python_if_valid`, which compiles before writing and declines otherwise. Same
discipline as the intent check: a pass may leave a file unimproved, but must never leave
one broken.

### Not fixed, deliberately

`db_has_column("products", "image_path")` failed because the build named the column
something else (earlier runs produced `image`, `image_url`, `picture_url`). The check is
strict on purpose for now — it says exactly what the table does have — but it is asserting
a name the model was never told to use. Either the amend prompt should fix the column name
for an image field, or the check should accept a family of names. Worth deciding before
using this number to compare runs.

## Still open

The generated shop has no product listing page (see `phase5-notes.md`), so
`post_persists`-style checks on `/` will fail for a real reason until the blueprint prompt
learns that a storefront request implies a listing. That is the first thing this suite
should be pointed at.

The `--webapp` suite has been run once, partially (turns 1–2). Per the temperature-0.2
rule it needs ~5 runs before any of these numbers are directional.
