# Phase 5 — prove it works, don't just prove it starts

Companion to `phase0-baseline.md` … `phase4-notes.md`. Recorded **2026-08-01**.
**Closes Gap 3.**

## What shipped

| Piece | Where |
|---|---|
| `functional_probe(spec, port)` — GET pages, POST endpoints, assert the value returns | `app/agent/smoke.py` |
| `ProbeCheck` + `SmokeResult.checks` / `.failures()` / honest `note()` | same |
| `_png_1x1()` — a real PNG from stdlib `zlib`+`struct`, no Pillow | same |
| `_encode_multipart()` — so the upload branch is genuinely taken | same |
| `server_error()` — lift the exception out of a 5xx for the repair prompt | same |
| Functional failures feed the `max_smoke_repairs` loop | `core._smoke_repair_instruction` |
| 31 tests, against real Flask subprocesses | `tests/test_functional_probe.py` |

`spec=None` reproduces the old liveness behaviour exactly, so every existing caller is
unaffected — there is a test for that.

## The gap it closes

Every phase before this one could report a passing smoke test on a broken app, because
**any HTTP status counted as alive**. Recorded instances:

- Phase 1: `Smoke test: app.py started; GET /posts/new -> 200` while `/posts` and every
  POST returned 500.
- Phase 3: `GET /login -> 200` reported on a run where a different page was broken.
- Phase 4: `GET /api/login -> 404` counted as up.

The probe now asks three things, and step 3 is the whole point:

1. Every page in the spec renders — 2xx **and** a non-empty body.
2. Every write endpoint accepts a real submission, with a genuine 1×1 PNG posted as
   multipart for an `IMAGE` field so the upload path is actually exercised. Under 500
   passes; a 5xx is the app breaking.
3. **The posted value comes back.** Only this one can fail on a build whose INSERT
   silently does nothing.

The headline test is `_SILENT_APP`: identical to the working fixture except its POST
handler never writes. It starts, answers, returns 302 — passes every liveness check — and
the probe catches it, because `product comes back after POST` fails while
`POST /products` passes.

## Three live corrections

**1. A dropped connection reported as "no response".** The dev server occasionally resets
a connection mid-probe (WinError 10054 on a POST that answers 500 when repeated alone).
`_request` now retries once. Reporting "no response" when the truth is a named exception
throws away exactly the detail the repair loop needs.

**2. The exception behind a 500 was being discarded.** `server_error()` lifts it out of
Werkzeug's debug page, turning `POST /x failed` into
`POST /x -> 500 NameError: name 'Product' is not defined`. Generic error pages return `""`
rather than the noise "HTTP 500 — 500 Internal Server Error".

**3. A false failure in the probe itself — the most important of the three.** The probe
originally checked only pages whose `reads` names the entity. On a live build it reported
`FAIL product visible on /add_product`, and the database showed the row had **persisted
perfectly well**: `reads` is inferred from the blueprint's prose and was empty on the
storefront `/`, while the *form* page `/add_product` was tagged. It now checks every page
and reports which one showed the value.

That one is worth dwelling on. A false failure here is **worse than no check at all**,
because the repair loop wired up in this same phase would have taken the report and sent
the model off to rewrite code that already worked. A probe that cries wolf actively
damages the build. There is now a regression test with the exact live topology — listing
page untagged, form page tagged.

## The final live result, and why it is a pass for the phase

```
may not meet: Smoke test: 3/4 functional check(s) passed
  ok   GET /add_product — 200
  ok   GET / — 200
  ok   POST /add_product — 302
  FAIL product comes back after POST — the posted value is not shown on any page
```

Verified against the database: the row **did** persist (`CoderProbe7f3a name`), and
`templates/index.html` is still the scaffold placeholder with no template loop. The build
produced a form page and never produced a listing page — so you can add a book and there is
nowhere to see it.

That is a real, user-visible failure, correctly reported for the first time. Before this
phase the same build announced `app.py started; GET / -> 200` and looked finished.

## Still open

**The build has no product listing page.** The blueprint planned a form and left `/` as the
placeholder. The probe reports it; nothing yet fixes it. The natural home is the blueprint
prompt (a storefront request implies a listing page) or Phase 7's eval assertions.

**The repair loop is wired but unproven live.** `_smoke_repair_instruction` now feeds
functional failures back, and `max_smoke_repairs` defaults to 1. Whether a 7B model
actually fixes "the posted value is not shown on any page" from that prompt has not been
measured — worth checking before relying on it in a demo.
