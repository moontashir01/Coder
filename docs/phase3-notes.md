# Phase 3 — the amendment flow: what shipped and what three live runs corrected

Companion to `phase0-baseline.md`, `phase1-notes.md`, `phase2-notes.md`.
Recorded **2026-08-01**.

## What shipped

| Piece | Where |
|---|---|
| `should_amend()` — the mirror of `should_blueprint()` | `app/agent/blueprint.py` |
| Delta extraction prompt | `app/resources/prompts/amend.md` |
| `delta_from_data()` — validated `SpecDelta` | `app/agent/projectspec.py` |
| Impact analysis, migration writing, regression detection | `app/agent/impact.py` |
| `_extract_delta`, `_amend_project`, `_apply_migrations`, `_check_amendment_regressions` | `core.py` |
| `chat()` seam ahead of the blueprint gate | `core.py` |
| 53 tests | `tests/test_impact.py`, `tests/test_amend.py` |

**Done when — met.** Turn 2 works, and turn 1's files are updated rather than
orphaned. On the final live run, all four pages the project has (three from turn 1,
one from turn 2) serve 200 **after** the amendment.

## The shape, and why

The model is asked for **only the delta** and is explicitly told *not* to name which
existing files to edit. "What else does this break?" is the question a 7B model answers
worst — it lists `app.py` and stops. `impact.py` derives it from the spec by rule, with no
LLM call: a new field on `product` means `db.py`, `models.py`, `seed.py`, every template
whose `reads` include the entity, every form template that writes it, and `app.py`. Each
carries a *specific reason*, threaded into that file's instruction.

`db.py` is impacted but never handed to the model: its migration is written from
`spec.migrations(since=revision)`, because a migration is exactly derivable from
`added_in` and letting a 7B model write `ALTER TABLE` against live data is risk with no
upside.

## Three live two-turn runs, three corrections

Every one of these was found by running the demo, not by reading the code.

**1. Merged reasons meant only the first got done.** I merged multiple reasons for one
file into a single edit, on the theory that editing a file twice would make the second
pass fight the first. `app.py` received three instructions at once — read the image off
the request, define `POST /admin/products`, add the view function — and the model did
**only the first**. The route was silently never written; the coverage check had to report
it. The theory was wrong: `_file_op_flow` re-reads the file on each call, so sequential
surgical edits compose. Now one reason = one edit, with a file's edits kept adjacent.

**2. The amendment deleted a route from turn 1.** With the above fixed, `POST
/admin/products` landed — and the same edit *replaced* turn 1's `/products` route, so a
page that worked before the change 404'd after it. Nothing could see it: the file
compiles, the new route works, the turn reports success. This is the exact regression the
plan exists to prevent, and Phase 2 is what made it detectable — the spec knows which
routes existed **and at which revision**:

- `vanished_routes()` — spec routes from an *earlier* revision that `app.py` no longer
  defines. Routes added *this* turn are excluded; an unwritten new route is the coverage
  check's business, not a regression.
- `restore_page_routes()` — a deleted GET page route is restored **exactly**, because its
  body is just `return render_template(...)`. A deleted POST handler is **reported, not
  invented**: its body is domain logic, and synthesizing it would be generation.

Final run: `Restored /products — the change had removed page route(s) that existed
before it.` → 4/4 pages serve 200.

**3. The upload form had no `enctype`** (Phase 4b's check, pulled forward). The admin form
the amendment created was `<form method='post'>` with `<input type='file'>` and no
`enctype="multipart/form-data"`. The browser then posts only the filename,
`request.files[...]` raises, and the upload silently never happens — invisible to every
other check, because the HTML is valid and the page renders. The plan calls this "the
single most likely way the live demo embarrasses you". `verify.fix_form_enctype` is
deterministic and purely additive. **This is scope pulled forward from Phase 4b**, done
because Phase 3's own demo turn ("add a product with a picture") does not work without it.

A Phase 1 scaffold bug also surfaced: `db.py`'s commented example used
`ensure_column(conn, "products", …)`, and turn 1's model copied it into real code,
creating a phantom `image_path` column beside the spec's `image`. The example now uses a
deliberately unrelated table.

## Step 5 — the failure the plan warned about

`chat()` gates BOTH the coverage check and the smoke test on `self._blueprint is not
None`, and clears it every turn. An amendment that didn't set it would be the only kind of
turn never verified and never run — invisibly, because the turn still reports success.
`_amend_project` therefore ends with `self._blueprint = _blueprint_from_spec(spec)`, the
plan's preferred option, and `test_an_amendment_turn_still_gets_the_smoke_test`
monkeypatches both passes and asserts each was called.

## Still open

**`POST /admin/products` returns 500** under the probe. Part of that is the probe's own
limitation — it posts urlencoded data with no file, so `request.files["image"]` raises
regardless. Phase 5's functional probe is specified to synthesize a real 1×1 PNG for
`IMAGE` fields, which is exactly what is needed to tell a probe artifact from a genuine
break. Until then, treat the write path as unverified rather than working.

**"Started" still is not "works."** The smoke test reports the first route that answers.
On one run it reported `GET /login -> 200` while a different page was broken. Phase 5.
