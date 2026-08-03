# Web Quality — making the generated site *look* right and *behave* right

Successor to `docs/always-fullstack-plan.md`. That plan made every website request produce a
schema-first, full-stack Flask app that **runs**. This one is about the two things "runs"
does not cover:

1. **How it looks.** The scaffold ships 184 lines of CSS; everything beyond a card and a
   button is improvised per page by a 7B model, differently each time.
2. **Whether the page actually works in a browser.** Nothing in Coder has ever rendered a
   page. `smoke.py` fetches bytes with `urllib`, so CSS layout is never evaluated, JS is
   never executed, and a button wired to nothing looks identical to one that works.

**The honest framing.** Claude Code's web output is good for three reasons: a frontier model
writes the markup, it *looks at* what it built with a browser, and it edits surgically. The
first is unreachable offline and this plan does not pretend otherwise. The other two are
engineering, and they are what this plan buys — plus one more lever the project has already
proven twice (`scaffold.py`, `crud.py`): **deterministic beats generated.** The presentation
layer is the last place a blank page is still handed to the model.

---

## Acceptance test (the demo, extended)

Same live session as `docs/fullstack-web-plan.md`, with four new questions asked at the end:

```
coder> build me an e-commerce site for selling books
coder> add an admin page where I can add a product with a picture
coder> add a shopping cart
coder> now let customers search products by title
```

At the end, on top of everything that plan already promised:

- **No page scrolls sideways on a 390 px phone**, and every page uses the same components —
  the products table on turn 1 and the cart table on turn 3 are visibly the same table.
- **The browser console is clean on every page** — no uncaught exception, no 404 for an
  asset the markup references.
- **Every button and form does something**: clicking it changes the URL, the DOM, or raises
  a flash message. No dead buttons.
- **No page 500s on a `BuildError`** because a template called `url_for('product_list')`
  and the route is named `products`.

---

## Where it stands today

| Concern | Today | Verdict |
| --- | --- | --- |
| Component styles | `scaffolds/flask/static/css/style.css`, 184 lines — header, nav, card, grid, form, button | starter sheet; tables/alerts/empty states improvised per page |
| Theme / palette | `buildspec._STYLE_PRESETS` picks real hex + fonts, chroma-validated | good, but delivered to the model as **prose it may ignore** |
| Reusable markup | none | every page re-writes the same table and the same form |
| Renders the page | nothing, ever | `smoke.py` is `urllib`; CSS and JS are never exercised |
| JS executes | never | `verify.py` runs `node --check` — syntax only |
| `url_for` resolves | not checked | `references.py` deliberately **skips** `url_for` to avoid corrupting templates |
| Form method matches route | not checked | a form POSTing to a GET-only route is a 405 nothing can see |
| Template edits | whole-file SEARCH/REPLACE | routinely deletes `{% extends %}`; `_restore_scaffold_invariants` repairs it after the fact |
| Browser testing | `skills/web-testing/SKILL.md` names `browser_*` MCP tools | aspirational — `mcp_servers.json` ships `{"servers": []}` |

### Four failures reachable today

- **`{{ url_for('product_list') }}` against a route named `products`** → Jinja `BuildError`
  → 500 on that page. The functional probe reports the 500 but nothing prevents it, and
  nothing tells the model the endpoint *name* was the problem.
- **A `<form method="post">` posting to a route declared `@app.route("/x")`** → 405. Every
  file-level check passes; `functional_probe` posts to routes from the spec, not from the
  form, so it can miss it.
- **A page whose table is 900 px wide inside a 390 px viewport** → the demo is opened on a
  phone and the site is unusable. No check in the codebase can observe layout.
- **`<button onclick="addToCart(1)">` with no `addToCart` defined** → `node --check` passes
  (it's valid syntax in a `.js` that never defines it, or it's inline in HTML), the page
  renders, the button is dead. This is `weaknesses.md` #2's last open item verbatim.

---

## Phase W1 — A design system, so the model never writes CSS from scratch — **DONE (2026-08-02)**

**Depends on:** nothing. **Size:** medium. **LLM calls added:** zero.

Shipped as written. `style.css` grew from 184 to ~480 lines (tables, alerts, empty states,
badges, breadcrumbs, pagination, sidebar layout, focus rings, reduced-motion),
`templates/_macros.html` and `static/css/theme.css` are new scaffold files and both are
`_FROZEN`, `buildspec.resolve_theme` / `theme_css` / `theme_tokens` resolve a style word to
real tokens, `scaffold.write_theme` writes them, and `scaffold.ui_context()` is threaded into
`_run_blueprint`'s `extra` beside the contract, scaffold and data-layer blocks.
`tests/test_scaffold_ui.py` = 41 tests.

**Three design changes made during implementation, each because the plan's version was
wrong:**

- **The dark scheme moved out of `style.css` into `theme.css`.** The plan put
  `prefers-color-scheme` in the component sheet. That cannot work: `theme.css` is linked
  *after* it, so a plain `:root` block there beats a `@media` block here and the site is
  stranded in light colours with no visible cause. `theme.css` owns every colour token for
  both schemes so the two cannot disagree. Pinned by
  `test_dark_scheme_lives_in_theme_not_in_the_component_sheet`.
- **A generated theme emits `:root` only — no dark block.** A palette chosen for "soft
  pastel" has no correct mechanical inverse, and deriving one by rule produces colours nobody
  picked. The *default* theme, which nobody chose, keeps its dark scheme.
- **Role assignment reads the palette's weight before its lightness.** Taking the lightest
  colour as the background looks right until a dark palette arrives: the `dark mode` preset
  is mostly dark, so its lightest entry is the **text**, and the first implementation turned
  "build me a dark mode dashboard" into a light theme with a blue surface. `theme_tokens` now
  decides which *end* of the palette is the page first. Caught by
  `test_a_dark_palette_declares_a_dark_color_scheme`, which is why that test exists.

**And one addition the plan didn't call for:** `_ensure_contrast` moves the accent along its
own lightness axis until it clears WCAG AA against the page, preserving the hue. Without it
the pastel preset's accent landed at 3.5:1 as link text. `test_every_preset_is_legible` now
asserts text-on-page, accent-on-page and label-on-accent all clear 4.5:1 for **all 17
presets** — a preset that ships an unreadable pairing would now ship it site-wide, so the
curation needs a check rather than trust.

*Original plan below.*

**W1a — grow the stylesheet into a component sheet.** Keep the existing token block and
system-font stacks (offline is load-bearing, see `strip_external_assets`). Add: `.table`,
alert variants (`.alert-success` / `-error` / `-warning`), `.empty` state, `.badge`,
`.media` card with an aspect-ratio image box, `.sidebar-layout`, `.breadcrumb`,
`.pagination`, visible `:focus-visible` rings, `prefers-color-scheme: dark`, and
`prefers-reduced-motion`.

**W1b — themes as data, not as prose.** `_STYLE_PRESETS` already resolves "soft pastel" to
five real hex values and a font pairing, and `palette_matches_style` already rejects the
model's own palette when it contradicts the request. Today all of that reaches the model as
text in a context block and is obeyed at the model's discretion. Instead: write the resolved
tokens **into `static/css/theme.css`** during `_run_blueprint`, next to the scaffold copy,
and have `base.html` link it after `style.css`. The preset then applies whether or not the
model cooperates. `BuildSpec.to_context_block()` keeps stating the palette — the model still
needs to know which variables exist — but it is now a description of a fact rather than an
instruction.

**W1c — a Jinja macro library.** `templates/_macros.html` with `card()`, `field()`,
`table(rows, columns)`, `flash_messages()`, `empty_state()`. Page generation collapses from
forty lines of hand-written markup to `{% import "_macros.html" as ui %}` plus a few calls:
much less surface for the model to get wrong, and cross-page consistency *by construction*
rather than by `_repair_nav_consistency` after the fact.

**W1d — `scaffold.ui_context()`.** The rule `crud.api_context()` taught: taking work away
from the model is only safe if you tell it what replaced it, or it invents
`class="product-card"` and styles it inline. Emit a compact block listing the available
classes and macro signatures, threaded into `_run_blueprint`'s `extra` beside
`contract_block` / `scaffold_block` / `data_api` (core.py:3178).

**Load-bearing rules:**
- **Placeholder substitution stays exact-literal.** `{{PROJECT_NAME}}` shares Jinja's
  delimiters and `{{ url_for(...) }}` must survive untouched — the macro file is full of
  both, so this is now much easier to break than it was.
- **Dotfiles need the `_DOTFILES` dance** (`gitignore` → `.gitignore`); the `package-data`
  recursive glob does not reliably ship dotfiles.
- **The scaffold still never overwrites.** A re-run on turn 3 must not revert a theme the
  user hand-edited on turn 2.
- **`theme.css` is `_FROZEN`, `style.css` is not.** The theme is derived data; the component
  sheet is something a build may legitimately extend.

**Tests:** the CSS is a static asset — assert `scaffold_files()` includes it, that a
scaffolded project's `base.html` links both sheets in order, that `ui_context()` names every
macro `_macros.html` actually defines (a drifting context block is the `api_context` failure
mode), and that theme writing is idempotent.

---

## Phase W2 — Endpoint and form-method validation (deterministic) — **DONE (2026-08-02)**

**Depends on:** nothing. **Size:** small. **LLM calls added:** zero.

Shipped as written: `verify.endpoints_referenced` / `unresolved_endpoints` /
`fix_endpoint_names` / `form_method_mismatches`, wired as `core._check_endpoints` in
`_verify_and_repair`'s stage-0 block beside `strip_external_assets` and `fix_form_enctype`.
`tests/test_endpoints.py` = 20 tests.

One correction the implementation forced, recorded because the plan stated it wrongly: the
plan's own example, `product_list` → `products`, is **not** a near miss and must not be
rewritten. The key rule inherited from `references._name_key` drops punctuation and one
trailing plural, so `product`/`products` and `addproduct`/`add_product` collapse together
while `product_list` and `products` do not — and that is right, because they are different
handler names and silently sending a form to the wrong one is worse than the 500 it replaces.
`test_a_genuinely_different_name_is_never_guessed` pins it. The plan text above is corrected.

*Original plan below.*

`projectspec.routes_from_source` already parses `@app.route` decorators out of `app.py`.
Add to `verify.py`:

- **`unresolved_endpoints(template_text, endpoints)`** — every `url_for('name')` whose name
  is not a defined view function. Repair by the **near-miss** pattern `references.py` already
  uses (`find_similar_file` / `rewrite_reference` / `_redirect_near_miss_references`), with
  its key rule: punctuation dropped and one trailing plural collapsed. So `product` →
  `products` and `addproduct` → `add_product` are naming slips and get rewritten, while
  `product_list` → `products` does **not** match — those are different names, and that is
  correct. No near miss = **report, never invent** — synthesizing a route is generation, and
  the coverage check owns that.
- **`form_method_mismatches(template_text, routes)`** — a `<form method="post" action="{{
  url_for('x') }}">` where `x` is declared without `methods=["POST"]`. Report with both
  halves named, so the repair prompt says something actionable.

Runs as a **stage-0 deterministic fix** in `_verify_and_repair` (core.py:1964-1969), beside
`strip_external_assets` and `fix_form_enctype`, for `.html` files only.

**Load-bearing rules:**
- **`base.html` links home with a literal `/` on purpose** — a `BuildError` in the layout
  fires on every page, so the scaffold trades one 404 for that. This pass must never
  "helpfully" convert a literal path into a `url_for`. It only ever validates existing
  `url_for` calls.
- **Only rewrite when exactly one candidate matches**, the same strictness
  `_resolve_target_from_spec` uses. Two candidates means guessing, and a wrong endpoint
  silently sends a form to the wrong handler — worse than the 500 it replaced.
- **The route list must come from the file on disk**, not from the spec: the spec is additive
  (`reconcile_with_disk` never deletes) and could name a route this turn's edit removed.

---

## Phase W3 — Jinja block-aware edits (prevention instead of repair) — **DONE (2026-08-02)**

**Depends on:** W1 (macros make block bodies small). **Size:** medium.

Shipped as written. `scaffold.template_edit_region` / `BlockRegion.splice` are the pure half,
`_surgical_edit(region=…)` sends only the block body and splices the result back, and
`_file_op_flow` falls back to the whole-file path when the block-confined attempt matches
nothing. `tests/test_jinja_blocks.py` = 19 tests.

Two decisions the implementation had to make that the plan left open:

- **The fallback is the whole-file SEARCH/REPLACE path, not the rewrite.** The plan said
  "fall back to today's whole-file path" and today's path is *two* stages. Falling straight
  to a regeneration would have made every title edit cost a full file rewrite — so a block
  attempt that matches nothing re-runs `_surgical_edit` with no region, and only then does the
  rewrite happen. Cost: one extra `_llm_edit` call in the case that used to cost a whole-file
  generation. `test_a_block_edit_that_does_not_match_falls_back_to_the_whole_file` pins it,
  and a non-template edit still spends exactly one call.
- **`{% block title %}` is never edited alone.** It joins nav/head/scripts/styles on a
  never-alone list: a one-line block gives SEARCH nothing to match, so confining an edit to it
  would fail every time and burn the extra call. A request about the title reaches the title
  through the fallback, which is what the fallback is for.

`templates_without_inheritance` was **not** reused as the detector, contrary to the plan's
third load-bearing rule — it scans a directory for templates that *lack* `{% extends %}`, and
this pass needs the opposite question asked about one string in memory. Reusing it would have
meant a directory walk per edit to answer a question about the text already in hand. The
shared thing is the `{% extends %}` test, and it is one regex.

*Original plan below.*

`_surgical_edit` runs SEARCH/REPLACE across the whole file. On a child template, the edit
almost always belongs to one `{% block content %}` — and measured behaviour is that the 7B
replaces the block it was asked to add to, which is exactly why
`_restore_scaffold_invariants` / `convert_to_child_template` exist. Repairing that after the
fact is strictly worse than not causing it.

Add a template-aware branch to `_file_op_flow`: when the target is a `.html` under
`templates/` that carries `{% extends %}`, extract the named block, send **only** the block
body for editing, and splice the result back. `{% extends %}`, `{% block title %}` and the
file's other blocks are then untouchable by construction.

**Load-bearing rules:**
- **Fall back to today's whole-file path** whenever the block can't be located unambiguously
  (nested blocks, a block opened in one file and closed in another). The fallback is the
  existing, tested behaviour — never a new failure mode.
- **A template without `{% extends %}` is not a child template**; leave it to
  `convert_to_child_template`, which owns that conversion.
- `scaffold.templates_without_inheritance` already identifies these — reuse it rather than
  writing a second detector.

---

## Phase W4 — The browser layer (`app/agent/browser.py`) — **DONE (2026-08-02), driver unverified**

**Depends on:** nothing. **Size:** medium. **This is the foundation for W5–W7 and W10.**

Shipped: `available()` / `install_hint()` / `browser_session()` → `Session.probe` /
`.screenshot` / `.click`, plus `probe_pages()` and the default `LAYOUT_SCRIPT`. Settings
`browser_checks` (**off**), `browser_timeout`, `browser_widths` (`[1280, 390]`),
`browser_max_pages`. `tests/test_browser.py` = 21 offline tests + 2 gated on a real browser.

Three shape decisions worth knowing:

- **`PageProbe` carries facts, never verdicts.** No thresholds live here — W5/W6 own those as
  pure functions over a probe, which is what lets them test with no browser in the loop.
- **The API surface is a `Session`, not a per-page function.** A launch costs ~0.5s, and
  W5/W6/W7 all want many pages inside one browser and inside the smoke test's single process
  window. `probe_pages()` is the convenience wrapper.
- **The localhost jail runs before the launch, not after.** This module renders pages *and
  executes their JavaScript*; pointed at an arbitrary URL it would be a general-purpose web
  client running untrusted script. `test_a_refused_url_never_launches_a_browser` pins the
  ordering, not just the refusal.

**Honest status: the two driver tests have never run.** Playwright is not installed here, so
`test_probe_sees_what_bytes_cannot` and `test_screenshot_returns_png_bytes` skip. They start a
real `http.server` on localhost serving one deliberately broken page (2000px div, a 404'd
`<script src>`, a `ReferenceError`) and assert the probe sees all three — but per this
codebase's own rule, *a probe only ever tested against a fake has not been tested*. Installing
Chromium is the one network trip in this plan and is the user's call, so it was not done
unilaterally. **Nothing calls `browser.py` yet** — it is inert library code until W5/W6 wire
it into `_smoke_test_backend`.

*Original plan below.*

A thin, best-effort wrapper over headless Chromium via Playwright:

```
open_page(url, width) -> PageProbe      # DOM metrics + console + network, one navigation
screenshot(url, width) -> bytes         # PNG
interact(url, actions) -> list[Result]  # click / fill / submit, then re-observe
```

**The offline tension, stated plainly.** Playwright's Chromium download needs the network
**once**; after that it is fully offline. No pure-Python renderer does modern CSS layout, so
there is no offline-native alternative — this is a real, deliberate exception to the project's
core rule, and it must be handled the way Phase A handled a missing Flask: a **loud, separate
install hint**, never a silent skip that looks like a pass. Gate it on
`settings.browser_checks` (default **off** until installed) and report
`browser checks skipped — install with: python -m playwright install chromium` in the answer.

**Where it runs.** Inside the smoke test's existing process window
(`_smoke_test_backend` → `run_smoke_test`), **not** as a second server launch. Two servers
fight over port 5000 and over `app.db` — the reason `--webapp` already turns
`blueprint_smoke_test` off. One start, one teardown, all probes in between.

**Load-bearing rules:**
- **Every failure is non-fatal**, exactly like `vision.py`: no Playwright, no browser
  binary, a navigation timeout, a crash — all return `None` and the turn proceeds as if the
  stage did not exist.
- **Localhost only, hard timeout**, matching `smoke.py`'s existing constraints.
- **Reuse `smoke._kill_tree`** and register teardown the same way `AppRunner` does; a leaked
  Chromium is worse than a leaked Flask because nothing prints its port.

**Tests:** the driver gets a couple of tests gated on `importorskip("playwright")`, like the
git tool tests. Everything W5–W7 build on top is a **pure function over a `PageProbe`**, unit
tested offline against recorded fixtures — no browser in the default suite.

---

## Phase W5 — Deterministic layout audit — **DONE (2026-08-02), driver unverified**

**Depends on:** W4. **Size:** small. **LLM calls added:** zero.

Shipped in `app/agent/pageaudit.py` beside W6 (they share one navigation, so splitting them
across modules would mean probing every page twice): `horizontal_overflow`, `empty_content`,
`low_contrast`, `unsized_images`, all pure functions over a `PageProbe`, plus `AUDIT_SCRIPT`
which measures computed contrast in the page. `tests/test_pageaudit.py` = 65 tests covering
W5 and W6 together.

**Three checks came back different, each because "a false failure is worse than no check"
bit:**

- **"Clipped or off-viewport elements" is not a check, and could not be one.** The plan lists
  it separately from horizontal overflow. But W1 shipped `.table-wrap` — a horizontal scroll
  container — as *the* answer to a wide table, so an element extending past the viewport's
  right edge is the design system working. The check would have failed every page that used
  the component correctly. Overflow is judged on `document.scrollWidth` only, and the
  offending elements are reported as the **culprit** inside that finding, which is what makes
  it actionable. `test_a_wide_table_inside_a_scroll_container_is_not_a_defect` pins it.
- **Contrast needed the large-text allowance to exist at all.** WCAG AA is 3:1 for text ≥24px
  (or ≥18.66px bold), and a flat 4.5 reports every heading rendered in a lighter tint — a
  compliant page failing the check that exists to find non-compliant ones. The threshold lives
  in Python over a probe that reports the ratio, the font size and the weight, per W4's rule
  that no verdict lives in JavaScript.
- **"Images with no intrinsic dimensions" is a warning, not a failure.** An image that has not
  decoded yet measures identically to one that never will, and that is not evidence enough to
  rewrite a template. `Finding.severity` exists for exactly this: warnings are reported in the
  answer and never reach the repair loop.

**And "empty `<main>`" got much stricter than the plan implies:** nothing is reported unless
there is no text, no media *and* no element children. An empty listing table is a page that
rendered — calling it empty would send the repair loop after the seed data.

**Honest status, same as W4's:** the pure functions are tested exhaustively against hand-built
probes, and `AUDIT_SCRIPT`/`CONTROLS_SCRIPT` are checked with `node --check` (a syntax error
there fails *silently* and in the worst direction — `Session.probe` swallows an `evaluate`
failure, the key is simply absent, and every check then reports a clean page). But no browser
is installed here, so **the scripts have never run against a real DOM.** That is the one
untested seam.

*Original plan below.*

The reliable half of "look at it", and the part that catches the failures that actually
embarrass a demo. All of it is JS evaluated in the page, no model involved:

- **Horizontal overflow at 390 px** — `document.body.scrollWidth > window.innerWidth`. The
  single most common responsive bug, and a one-line check.
- **Clipped or off-viewport elements** — any element whose bounding box extends past the
  viewport's right edge.
- **Contrast below 4.5:1** on text nodes, from *computed* colours (which is why this needs a
  browser and cannot be done by reading the CSS).
- **Empty `<main>`** — the page rendered, and there is nothing in it.
- **Images with no intrinsic dimensions** — the layout-shift source.

Failures feed the existing repair loop through the same door as
`_smoke_repair_instruction`, naming the page, the selector and the measurement.

**Load-bearing rule:** **a false failure here is worse than no check** — this is exactly the
lesson `functional_probe` step 3 learned, where probing only entities named in `reads`
produced a false failure for a row that had persisted, and the repair loop was sent to
rewrite working code. Each check reports a **measurement**, not a judgement, and anything
ambiguous passes.

---

## Phase W6 — Runtime JS and the dead-button probe — **DONE (2026-08-02), driver unverified**

**Depends on:** W4. **Size:** medium. **Closes `weaknesses.md` #2's last open item.**

Shipped in `pageaudit.py`: `console_findings` / `network_findings` (page-level, pure),
`CONTROLS_SCRIPT` + `triage_controls` (which controls are safe to click, and why the others
were not) and `click_findings` (what a click proved). `audit_site` drives it — one session,
one navigation per page per width, then one per control. Wired through
`run_smoke_test(on_serving=…)` so it runs inside the server window the smoke test already
opened, exactly as the plan requires.

**The plan says "click each `<button>` / submit each `<form>`". The forms half is not
implemented, deliberately, and this is the biggest deviation in the phase:**

- Native validation **blocks** a submit with an empty required field, so the page does not
  change — and "nothing changed" would be a false failure on a form that is perfectly correct.
- A POST that *did* go through inserts a second row behind the checks that assert against the
  seeded data, which is the same objection the plan itself raises about Delete buttons.
- `functional_probe` already posts to every write endpoint with real values, a real 1×1 PNG
  and a real multipart body, and requires the value to come back. What the browser adds that
  HTTP cannot is **the control** — so the control is what it probes.

A GET form with no empty required field **is** submitted: it cannot mutate anything and it is
the demo's own turn 4 ("let customers search products by title"). Every skip is reported —
`SiteAudit.controls_skipped` carries the reason and `note()` prints it, per the rule that a
check which did not run is never reported as one that passed.

**Two more corrections:**

- **`Session.click` had to report `innerHTML` length as well as `innerText`.** A handler that
  only toggles a class moves neither the URL nor the rendered text, and the plan's "URL, DOM,
  or a flash message" test would have called a working tab switch dead. Both lengths are
  reported now; a click counts as working if *any* signal moved.
- **`ProbeCheck` gained an `owner` field.** Browser checks ride in the smoke result's check
  list — which is how they reach the answer for free — but `_smoke_repair_instruction` feeds
  every failure to a rewrite of **app.py**. Without the split, "the products table scrolls
  sideways at 390px" would have sent the model to rewrite the server file for a CSS problem
  and then report it fixed. Browser findings are repaired by `_repair_browser_findings`
  against the page's own template (resolved through the ProjectSpec, and only when that file
  really exists), bounded by `settings.max_browser_repairs` (1). Contrast and network findings
  are **reported, never repaired**: the palette lives in the frozen, deterministically written
  `theme.css`, and a reference to a missing file is `_repair_dead_references`' job — it
  creates the file rather than rewriting the page that asked for it.

*Original plan below.*

- **Console and network.** Uncaught exceptions, `console.error`, and failed requests on every
  page. The failed-request half catches a dangling `<script src>` **at runtime** — the
  runtime complement to `references.py`'s static scan, and it also catches the case where the
  file exists but 404s because the Flask static route doesn't serve it.
- **Every button and form does something.** Click each `<button>` / submit each `<form>`,
  then assert *something* changed: URL, DOM, or a flash message. `functional_probe` proves
  the server persists a POST; this proves the control that sends it exists and is wired.

`server_error()`'s existing trick — lift the exception out of a 5xx so the repair prompt says
`NameError: name 'Product' is not defined` instead of "POST failed" — applies verbatim to a
JS stack trace. That specificity is what makes the repair land.

**Load-bearing rules:**
- **Destructive actions are skipped.** A "Delete" button that works will empty the seeded
  data the later probes assert against. Skip by accessible name (`delete`/`remove`) and by
  `method="post"` on a route the spec marks destructive — and report the skip, per this
  codebase's rule that a check which didn't run is never reported as one that passed.
- **Bound the fan-out.** N pages × M buttons is a lot of navigations; cap it the way
  `blueprint_max_files` caps its own fan-out, and **report what was dropped** rather than
  truncating silently.

---

## Phase W7 — Vision critique on the rendered page — **DONE (2026-08-02)**

**Depends on:** W4, W5. **Size:** medium. **Do it last of the browser trio** — it is the
least reliable part, and W5 must be catching the objective failures first so this one is only
ever asked about taste.

Shipped as `app/agent/visualcheck.py` (pure: `VISUAL_CHECKLIST`, `parse_visual_verdict`,
`filter_visual_complaints`, `build_visual_repair_prompt`) plus `core._visual_review`, gated by
`settings.check_visual` (**off**), `visual_max_pages` (2) and `max_visual_repairs` (1).
Screenshots are captured by `audit_site(screenshot_pages=…)` inside the smoke window — the only
moment a live server exists — and `vision.ask_about_image` sends bytes, so nothing is written
to disk. `tests/test_visualcheck.py` = 35 tests.

**Four decisions worth knowing:**

- **The prompt asks for the literal `MISSING:` marker** so `intent.parse_verdict` is reused
  *verbatim*. The wording is slightly odd for a defect list; a second parser would be a second
  thing that can read noise as a defect, and "unparseable = PASS" is the property that protects
  a page that is fine.
- **A complaint must name a visible SYMPTOM** (`_RENDER_SYMPTOM_RE`) and must not ask for new
  content (`_INVENTION_RE`) or hedge (`_SUGGESTION_RE`). "The page could be more modern" and
  "add a testimonials section" die deterministically, with no second call.
- **Screenshots are the viewport, not the full page.** A 4000px-tall PNG is downscaled to
  illegibility by `vision._prepare_image`'s 1536px long-edge cap before the VL model sees it,
  and what a person judges first is the fold.
- **The revert is generalised, and it also covers W5/W6.** `_guarded_repair` snapshots the files
  a pass is about to rewrite, re-measures afterwards, and restores them byte-for-byte if the
  error count went up. Because the restored files are identical to the ones that produced the
  earlier audit, that audit's numbers are true again — no third server start. The plan asked
  for this on W7 only; applying it to the browser repair too was free and obviously right.

*Original plan below.*

`qwen2.5vl:7b` already ships and `vision.py` already knows how to send an image. Screenshot
each page at 1280 and 390, and ask a **checklist**, not "critique this" — an open prompt on a
7B VL produces "the page has a clean and modern feel", which is unactionable.

Reuse `_intent_repair`'s shape exactly, because it is the same problem (a 7B judging a 7B):
`parse_verdict` with **unparseable = PASS**, `filter_complaints` to drop hedged suggestions,
and **revert any rewrite that breaks `check_file` or regresses W5's measurements**. That last
clause is new and necessary: a visual "fix" that introduces horizontal overflow must be
undone automatically, or this stage is a net negative.

**Load-bearing rules:**
- **Never let it invent content.** The same tension `buildspec._clean_nav` resolves — a
  critique that says "add a testimonials section" is a feature request, not a defect. Filter
  complaints to the ones about *rendering* of content that exists.
- **Gated `settings.check_visual`, default off in tests** — same trap as `check_intent` and
  `schema_first`: it fires on file-writing turns and would reach a real Ollama, silently
  ending the suite's offline guarantee. `conftest.py` must default it off.

---

## Phase W8 — Template-aware dependency index — **DONE (2026-08-02)**

**Depends on:** nothing (pairs with W2). **Size:** medium.

Shipped as `app/agent/templatedeps.py` (`parse_template` / `build_graph` / `TemplateGraph`),
consumed by `impact.impacted_files(…, graph=…)` and `core._resolve_target_from_spec`, plus
Jinja edges in the real index: `symbols.extract_symbols` now reads `.html` and
`render_template("x.html")`, so `dependencies("templates/products.html")` returns its layout
and `dependents("templates/base.html")` returns its children. `tests/test_templatedeps.py` =
32 tests.

**The entity hint is where all the difficulty was.** The obvious version — does the template
mention the word `products`? — makes `base.html` a reader of every entity in the project,
because its nav says "Products", and an amendment would then rewrite the site layout to "show
price for each product". So identifiers are taken **only from Jinja expressions, with string
literals stripped first**: `{% for p in products %}` counts, `<a href="/products">Products</a>`
does not, and `{{ url_for('products') }}` contributes only `url_for`. Layout templates are
excluded outright. `test_a_nav_link_does_not_make_the_layout_a_reader_of_products` is the test
that exists because the first implementation got this wrong.

**Two corrections the implementation forced:**

- **Matching needs word SEGMENTS, not just the whole identifier.** The signal arrives in two
  shapes — a template writes `{% for product in products %}`, while the view behind a
  generic-looking page writes `models.get_all_products()`. Keying only the whole name finds the
  first and misses the second, which is precisely the page `Page.reads` also missed.
- **`view_bodies` stops at the next decorator, not the next `def`.** `@app.route("/products")`
  sits between two functions, so sweeping it into the previous body made `index` look like a
  page that displays products.

`templates_without_inheritance` was **not** reused as the detector (the plan's load-bearing
rule): it walks a directory looking for templates that *lack* `{% extends %}`, and this needs
the opposite question asked about one string already in memory.

*Original plan below.*

`symbols.py` resolves imports for Python only, so the dependency graph stops at `app.py`. Add
Jinja edges: template → `extends`/`include`, route → template (from `render_template` calls,
which `routes_from_source` already reads), template → `url_for` targets, template → static
assets.

Buys two things: `impact.impacted_files` stops relying on `Page.reads` — which CLAUDE.md
records as "routinely empty on the very listing page that matters" — and
`_resolve_target_from_spec` gets a stronger signal than nav-label matching for "update the
products page".

**Load-bearing rule:** stay **additive and best-effort**, like `reconcile_with_disk`. A
template the parser cannot read yields no edges, never a wrong edge — `impact.py` deletes
nothing based on absence, and this must not change that.

---

## Phase W9 — Beating the ceiling: best-of-N, and roles per model — **DONE (2026-08-02)**

**Depends on:** W2, W5, W6 (the checks are the judge — this phase is worthless before them).
**Size:** medium. **Cost:** latency, directly.

Shipped as `app/agent/candidates.py` (`is_high_value`, `score_candidate`, `pick_best`,
`describe_choice`) + `core._best_of_candidates`, with `settings.best_of_n` (**1**),
`best_of_temperature` (0.4), `planner_model` and `judge_model` (both empty).
`tests/test_candidates.py` = 32 tests.

**The scorer only uses signals an existing deterministic pass already trusts** — it parses
(`verify.check_text`), no off-machine asset, every `url_for` resolves, upload forms declare
their enctype, a page extends the layout, no duplicate top-level definition. Two exclusions
are deliberate: the `url_for` half is **skipped** rather than guessed when `app.py` is not on
disk (an unknown endpoint set makes every candidate look broken, and the choice becomes noise),
and `unresolved_local_calls` is not scored at all because `app.py` legitimately calls a
`models.py` helper the build writes two files later — scoring it per candidate would punish the
correct answer.

**Three refusals in the generation loop:** nothing at N=1 (the default), nothing while
**streaming** (the user is watching candidate #1's tokens; shipping #2 would be a lie), and
nothing for a file where a defect is cheap. Ties go to the first candidate, so N>1 can never
make a build merely *different*.

**Roles are properties, not attributes.** `_llm_planner` is `_llm_blueprint` and `_llm_judge`
is `_llm_edit` until a role model is named — the same object, so `/model`, `set_model` and
every test that patches those attributes keep working. An attribute captured at construction
would silently hold the old instance.

`verify.check_file` was split into `check_text` (tooling-free, takes a string) plus the
node/tsc step, which is what lets a candidate be scored before anything is written.

**The schema call is cached per session** (`_extract_schema`), the speed lever the plan names:
the call is temperature 0 and `/plan` previews an amendment with the same message the build
then uses. A *failed* call is never cached — a transient is not an answer.

*Original plan below.*

- **Best-of-N on the few high-value files.** Generate 2 candidates at temperature 0.4 for a
  page body or `app.py`'s routes, run the deterministic checks on each, keep the winner. This
  is the only honest way to raise output quality offline without a bigger model, and it is
  only sound because W2/W5/W6 made the checks objective. Ties go to the first candidate
  (no coin flips).
- **Roles per model.** Planning, schema extraction and critique are *reasoning* calls, not
  codegen; a general instruct model may beat `qwen2.5-coder` at them. Add `planner_model` /
  `judge_model` settings in the `web_stack` style so the question becomes measurable instead
  of assumed. The `/model` machinery and `set_model` already exist.
- **Speed, to pay for it.** Cache the schema call per session (flagged in
  `always-fullstack-plan.md`'s own risk list as the next lever), and only then consider
  `OLLAMA_NUM_PARALLEL` for independent page generations.

**Load-bearing rule:** **default N=1.** Every request already costs minutes on a 7B; doubling
that must be a choice the user makes, and the setting must say what it costs.

---

## Phase W10 — Prove it — **DONE (2026-08-02)**

**Depends on:** all. **Size:** medium.

Shipped: `no_horizontal_overflow`, `no_console_errors`, `every_control_does_something`,
`nav_on_every_page`, `contrast_ok` and `style_stable_across_turns` in `evals/checks.py`, driving
`browser.py` against an `AppRunner`, plus two new `WEBAPP_TASKS` (`web_quality_build`,
`web_quality_stable`). `tests/test_quality_evals.py` = 29 offline tests.

**Three decisions:**

- **The browser pass is memoized on the `CheckContext`** (`ctx.browser`). Five checks × one
  server launch and one Chromium start each is five of both; they now share one.
- **No browser = FAIL, with the install command in the detail.** Not a skip and certainly not a
  pass: "a suite that scores 100% on a broken app is worse than one that scores 50% honestly"
  is this suite's own rule, and a check that never rendered anything has verified nothing. The
  new checks live on their **own tasks** so a machine without Chromium loses those two tasks
  rather than dragging every existing one down.
- **`style_stable_across_turns` compares pages to EACH OTHER at the end**, not to a stored
  turn-1 fingerprint. "Turn 3 must not restyle turn 1" is observable in the finished site: one
  computed body font and background across every page, no page-local `<style>` block, and every
  page still built from the shipped component classes. A page added on turn 3 that styled itself
  fails all three — and, as the plan requires, no pixel is compared, so seeded data changing is
  not noise. Fewer than two pages **fails** rather than passing vacuously.

*Original plan below.*

The eval suite measures *works* well and *looks* not at all. Add to `evals/checks.py`, driven
by W4–W6 so the checks name no selector the task author had to guess:

- `no_horizontal_overflow` at 390 px on every page in the spec.
- `no_console_errors` on every page.
- `every_control_does_something` (W6's probe, as an assertion).
- `nav_on_every_page` and `contrast_ok`.
- **`style_stable_across_turns`** — the visual sibling of `earlier_pages_still_work`, and the
  headline number for this plan: turn 3 must not restyle turn 1. Compare the computed token
  values and the component classes in use, **not** a screenshot hash — a pixel diff fails on
  seeded data changing and would be noise.

Per CLAUDE.md's standing eval lesson: the planner runs at temperature 0.2, so **a single run
proves nothing** — re-run a suspect task ~5× against a stashed baseline before calling
anything a fix or a regression.

---

## Order, and what each phase costs

| Phase | Depends on | Size | Buys |
| --- | --- | --- | --- |
| W1 — design system ✅ | — | medium | how the site *looks*, for zero LLM calls |
| W2 — endpoint validation ✅ | — | small | no `BuildError` 500s, no 405 forms |
| W4 — browser layer ✅ | — | medium | the foundation for every "look at it" check |
| W5 — layout audit ✅ | W4 | small | responsive + contrast failures become visible |
| W6 — runtime JS probe ✅ | W4 | medium | dead buttons and console errors — weaknesses #2 |
| W3 — Jinja block edits ✅ | W1 | medium | template edits stop breaking `{% extends %}` |
| W8 — template index ✅ | — | medium | impact analysis that doesn't depend on `reads` |
| W7 — vision critique ✅ | W4, W5 | medium | taste, at the reliability a 7B VL allows |
| W9 — best-of-N + roles ✅ | W2, W5, W6 | medium | quality above the single-sample ceiling |
| W10 — evals ✅ | all | medium | proof, and a regression net |

**Every phase is now implemented.** What has *not* happened is a live run: no browser is
installed on this machine, so W4–W7 and W10's checks have never rendered a real page here, and
the eval suite has not been re-measured against a baseline. The two things to do first, in
order: `pip install playwright && python -m playwright install chromium`, then
`BROWSER_CHECKS=true python -m evals.run --webapp --only web_quality_build`. Per CLAUDE.md's
standing lesson, a single run proves nothing — re-run a suspect task ~5× before calling
anything a fix or a regression.

**W1, W2 and W4 are independent — start there.** W1 is the visible win and needs no new
dependency; W2 is a morning's work that removes a whole failure class; W4 unblocks half the
plan and its install step is the one thing that might need a network trip, so discovering
that early matters.

**W5 and W6 before W7** is deliberate, and it mirrors "C before B" in the previous plan:
turning a 7B's aesthetic opinion loose before the objective checks exist means every visual
complaint lands on code that is measurably fine.

**W9 last of the implementation phases** — it uses the checks as its judge, so it is exactly
as good as W2/W5/W6 and worthless before them.

---

## Risks worth stating up front

- **Playwright breaks the offline promise, once.** This is the plan's one genuine
  compromise. Mitigation is Phase A's: `browser_checks` defaults off, the install hint is
  loud and separate from any generation instruction, and every downstream stage skips
  cleanly. A user who never installs Chromium gets exactly today's behaviour.
- **Turn latency grows again.** W5/W6 add page loads, W7 adds a vision call per page, W9
  doubles generation. Every one of them is individually gated, and the defaults must keep a
  plain build at roughly today's cost.
- **The visual critic is a new false-positive surface**, structurally identical to
  `check_intent`'s. The same four rules apply (unparseable = pass, filter complaints, revert
  a rewrite that regresses a measurement, never claim an unearned pass) — and the new fourth
  one, reverting on a W5 regression, is what keeps it from being a net loss.
- **The design system can look generic.** Every site sharing one component sheet is a real
  aesthetic cost, and the honest trade: consistent-and-plain beats improvised-and-broken on a
  7B, and W1b's themes are where the variation comes back. Revisit only if the demo feedback
  is "they all look the same" rather than "it's broken".
- **`conftest.py` now has three settings to default off** (`check_intent`, `schema_first`,
  and W7's `check_visual`). That list is a symptom — a stage that calls an LLM inside
  `_verify_and_repair` silently de-offlines the suite. Worth a single test that asserts no
  LLM-calling stage is enabled by default under pytest, rather than a fourth entry.

## What this does not fix

- **The model's taste.** W1 makes the components good; nothing here makes the 7B *compose*
  them well. That is `weaknesses.md` #1 and it stays open.
- **JS-framework work.** Everything here assumes server-rendered Jinja. An SPA is a different
  plan and is not in scope.
- **Design originality.** A site built from a fixed component sheet and five preset themes
  is, by construction, not going to surprise anyone. That is the trade this plan makes on
  purpose.
