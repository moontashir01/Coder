# Coder — From "build the layout" to "build the whole thing" (Requirements Blueprint)

**The ask.** Today, "build me a login page" produces a `login.html` (plus a stylesheet and
a script if it happens to reference them). What it does *not* produce is everything a login page
actually **implies**: the form's fields and validation, a *forgot-password* page and flow, a
*sign-up* link and page, and — the big one — a **backend**, so that pressing the button *does
something* instead of nothing. The user wants Coder to **reason about what the request needs**,
plan the full set of features and files (frontend **and** backend and the wiring between them),
and build them — for **any** request, not a hardcoded list of "login" special-cases.

This is a real capability gap, not a prompt tweak. This doc explains why the current architecture
can't do it, then specifies a **Requirements Blueprint** stage that closes it, grounded in the
existing code seams. It's written to slot in beside the existing design docs
([weaknesses.md](weaknesses.md), [multi-task-handling.md](multi-task-handling.md),
[professional-agent-roadmap.md](professional-agent-roadmap.md)) and it deliberately reuses the
machinery those describe rather than rebuilding it.

---

## 1. What happens today — walk the login request through the code

`AgentCore.chat()` ([core.py:2515](../app/agent/core.py#L2515)) processes "build me a login page":

1. `_split_compound()` sees **one** task (no "and/then") → not a compound.
2. `wants_multifile()` ([core.py:226](../app/agent/core.py#L226)) is **false** — no
   "separate/split/extract", no explicit "N files", no `a.css and b.js` list.
3. `classify()` → `code_generation`; `_looks_multipart()` → false. No decomposition.
4. `_route_one()` ([core.py:2048](../app/agent/core.py#L2048)): `_wants_file_op()` matches
   (verb "build" + noun "page") → **`_file_op_flow`** → **one** `FILENAME:` generation →
   `login.html` on disk.
5. `_repair_dead_references()` creates a `styles.css` / `login.js` **only if** `login.html`
   happened to link them.
6. `_intent_repair()` checks `login.html` against the literal words "build me a login page" — it
   verifies a password field exists (it live-caught exactly that once), but it will **never**
   demand a backend the user didn't mention.

**Result:** a static page. No `/login` endpoint, no user store, no forgot-password page, no
sign-up page, no submit handler that reaches a server. The button is decorative. Every stage did
its job correctly — the pipeline is simply built to deliver *what was literally said*, one file at
a time.

---

## 2. Why the architecture can't do this today (it's by design)

The gap is not a bug; it's the **governing philosophy** of the whole harness: *do what was asked,
invent nothing.* That philosophy is everywhere, and it exists because the engine is a **7B local
model** that runs wild the moment you let it improvise (weaknesses.md #1):

- **`buildspec.py` is explicitly anti-hallucination.** `_clean_nav()`
  ([buildspec.py:331](../app/agent/buildspec.py#L331)) **drops any nav label the user didn't
  literally type**; `_clean_behaviors()` drops any behaviour about a page nobody mentioned. Its
  own docstring: *"Never invent requirements."* So the one stage that produces a *shared spec*
  is structurally incapable of adding "forgot password".
- **`_intent_repair` judges against the user's exact message** — a stronger check than syntax, but
  it measures *"did you build what they said"*, not *"did you build what this kind of thing needs"*.
- **The router picks one lane and runs one unit of work.** `_file_op_flow` writes exactly one
  file; `wants_multifile` is narrow on purpose (multi-task-handling.md, root cause). Nothing
  *expands* a terse request into its implied parts. Decomposition (`_split_compound`,
  `Planner.decompose`) splits requests that are *already* compound — it does not turn a single
  request into more than it says.

So the new capability is, precisely, the **opposite** of the existing conservatism, in one narrow
place. The central design problem of this feature is: **add principled inference without unleashing
the 7B model's tendency to invent.** Everything below is shaped by that constraint.

---

## 3. The design — a "Requirements Blueprint" stage

Add **one** new stage, upstream of the router, that runs **only** for greenfield build requests.
It takes the short request and produces a **Blueprint**: the implied features (tiered), the full
file list (frontend + backend + data + glue), an **interface contract** that makes those files
line up, and a **stack** chosen to actually run on *this* offline machine. The Blueprint then feeds
the **existing** multi-file machinery — it does not replace it.

```
chat(msg)
  ├─ should_blueprint(msg)?  ──no──►  (unchanged: split / multifile / route_one)
  │        │ yes
  │        ▼
  │   blueprint = expand_requirements(msg, detect_stack())   # ONE temp-0 LLM call, cached
  │        │                                                 # tiers features, lists files+contract+stack
  │        ▼
  │   (optional) preview blueprint → user approves          # weakness #4: preview == execution
  │        ▼
  │   _run_blueprint(blueprint)  ──►  _multi_file_flow(preplanned_ops=…, extra_context=contract)
  │                                     └─ reuses: sibling threading, shared-asset note,
  │                                        BuildSpec (style/nav), verify+intent repair,
  │                                        dead-ref / nav / link repair
  └─ _verify_blueprint_coverage(blueprint, trace)           # weakness #3: whole-request check
```

### 3a. Where it sits — the seam in `chat()`

At the top of `chat()`, right after `_update_skills_context` / `memory.add_human`
([core.py:2534](../app/agent/core.py#L2534)) and **before** the `_split_compound` ladder:

```python
if settings.expand_requirements and should_blueprint(clean_message):
    blueprint = await self._expand_requirements(clean_message, at_refs)
    if blueprint and blueprint.is_actionable():
        answer, trace = await self._run_blueprint(blueprint, at_refs)
        # ... then fall through to the SAME dead-ref/nav/link repair block, plus
        #     the new _verify_blueprint_coverage below.
```

`should_blueprint()` is a new, **narrow** gate — a build verb (`build/create/make/scaffold/
generate/design/implement`) plus an artifact noun (`page/app/site/website/form/dashboard/API/
feature/system/…`), and it must **exclude** edits, questions, and "split/refactor" wording. It's the
same shape as `_wants_image_build()` ([core.py:157](../app/agent/core.py#L157)) — one regex,
consulted in one place, so it can't change how any existing request routes. A screenshot build
(`build this @shot.png`) is a build request too and should flow through here once described.

### 3b. What the Blueprint contains

A new `app/agent/blueprint.py` with a frozen dataclass, mirroring `BuildSpec`'s shape so it threads
into prompts the same way (`to_context_block()`):

```python
@dataclass(frozen=True)
class Blueprint:
    summary: str                       # "A login page with email/password auth + password reset"
    features: tuple[Feature, ...]      # each: (name, tier, files_it_needs)
    files: tuple[FileOp, ...]          # SAME FileOp shape _plan_file_ops emits — reused verbatim
    contract: ApiContract              # endpoints, form field names, data schema (see 3d)
    stack: Stack                       # chosen runtime (see 4)
    build_spec: BuildSpec              # style/nav — delegated to the EXISTING buildspec.py
```

**Features are tiered** — this is the anti-hallucination lever (§2):

| Tier | Meaning | Built by default? |
|------|---------|-------------------|
| `requested` | Literally in the message ("login page") | **Yes** |
| `core` | So implied that omitting it makes the thing non-functional — a login *needs* a submit target, a users store, an error state | **Yes** |
| `optional` | A competent engineer *might* add it — "remember me", OAuth, 2FA, email delivery, rate-limiting | **No — listed, not built** |

Default build set = `requested ∪ core`. `optional` features are **reported** ("I can also add: OAuth
login, email-based reset — say the word"), never silently built. This keeps scope honest: "login
page" yields a working form + validation + a real submit + a forgot-password page/link + a user
store, but not a silent sprawl into OAuth. `settings.blueprint_optional_tier` can flip
optional-into-built for users who want maximalism.

### 3c. It reuses the plan-consumption path, not a new one

`Blueprint.files` is a list of the **same `FileOp`** that `_parse_file_plan` already produces
([core.py:1872](../app/agent/core.py#L1872) `_plan_file_ops`). So the integration is minimal: give
`_multi_file_flow` an optional `preplanned_ops` parameter; when present it **skips the
`_plan_file_ops` call** and uses the blueprint's ops, then runs the identical downstream loop —
`_sibling_context`, `_shared_asset_note`, per-file `_file_op_flow`, `_verify_and_repair`
(`_syntax_repair` + `_intent_repair`), and the `chat()`-seam repairs (dead-ref, nav, link). The
blueprint's `contract.to_context_block()` is folded into `plan_extra`/`extra_context` exactly where
`spec_block` is today ([core.py:2000](../app/agent/core.py#L2000)). **We are adding a smarter plan
*producer*, not a new plan *consumer*.**

### 3d. The interface contract — what makes frontend and backend actually line up

More files means more chances for the 7B model to disagree with itself (weaknesses.md #6 — context
threading loses coherence at scale). The fix that already works for *style* (BuildSpec's canonical
nav/palette) we extend to *behaviour and API*. The `ApiContract` is a compact, canonical statement
injected into **every** per-file generation:

- **Endpoints:** `POST /api/login  (body: {email, password}) -> 200 {ok, redirect} | 401 {error}`
- **Form ↔ route binding:** the login form has `id="login-form"`, fields `name="email"`,
  `name="password"`, and submits to **exactly** `/api/login`; the server reads **exactly** those
  field names.
- **Data schema:** `users(email TEXT PK, password_hash TEXT)`, seeded with one demo user
  (`demo@example.com` / `demo1234`) so the flow is testable offline out of the box.
- **Redirect/errors:** success → `dashboard.html`; failure → inline `#login-error` text.

This is the single most important part of the feature. Without it you get a beautiful form that
POSTs to `/login` and a server that only serves `/auth` — two files that each "work" and together
do nothing. The contract is to the API what `BuildSpec.nav` is to navigation: stated once,
concretized once, copied verbatim everywhere.

---

## 4. The backend problem — being honest about "offline"

This is the hard part and the place a naive plan would over-promise. "What happens after I press the
button" means a running server, and Coder's whole identity is **offline** with a **network gate on
by default** (`allow_network=False`, [settings.py:179](../config/settings.py#L179)) — so it usually
**cannot `pip install flask` or `npm install express`**. A blueprint that emits a Flask app on a
machine with no Flask and no network produces files that don't run — which is worse than today,
because now the report *claims* a working backend.

So the blueprint must **choose a stack that actually runs on this machine**, detected at plan time by
a new `app/agent/runtime_probe.py`:

- `detect_stack()` checks, in order of preference and **grounded in reality**:
  1. **Python stdlib** (`http.server` / `wsgiref` + stdlib `sqlite3` + `json`) — **always available,
     zero install, runs fully offline.** This is the **default backend**.
  2. **Flask / FastAPI** — only if **already importable in the venv** (`importlib.util.find_spec`).
  3. **Node / Express** — only if `node` is on PATH **and** `express` resolves (or `allow_network`
     is on so `npm install` is permitted).
- The chosen `Stack` is threaded into the blueprint and every backend-file prompt, so the generated
  server uses libraries that **exist here**.

**Recommendation: default to the stdlib stack.** A ~40-line stdlib server with a sqlite (or even
in-memory/JSON) user store gives a genuinely working "press the button → it authenticates → you get
redirected or an error" on *any* machine, with no install and no network — which is exactly the
offline promise. Escalate to Flask/FastAPI **only** when the probe proves they're present. This
turns "offline" from a limitation into the feature's grounding principle.

> Phased scope for the backend (see §7): Phase 1 ships **wired frontend + stdlib mock backend that
> runs**; Phase 2 adds **stack detection + framework backends when present**; Phase 3 adds the
> **runtime smoke test** (§5) that actually starts the server and hits the endpoint.

---

## 5. Closing the loop — verify the *whole request*, not each file

weaknesses.md #3 is still open: *"Nothing checks that the whole request was satisfied."* The
blueprint is what finally makes that check possible, because now there's an explicit spec of what
"the whole thing" is. Add `_verify_blueprint_coverage(blueprint, trace)` at the `chat()` seam,
beside `_repair_dead_references` ([core.py:2578](../app/agent/core.py#L2578)), gated by
`settings.check_blueprint_coverage`:

1. **Every planned file exists** (the plan promised N files; are they all on disk?).
2. **Every `core`/`requested` feature has its marker** — the forgot-password page exists and is
   linked from login; the sign-up page exists; the error element is present.
3. **The wiring resolves** (deterministic, no LLM): the form's `action`/`fetch()` target **matches a
   route the backend actually defines**, and that route **reads the form's field names**. A form
   POSTing to `/api/login` while the server routes `/login` is the characteristic full-stack break —
   catch it here and repair it (repoint one to the other, the same way `_repair_page_links` repoints
   near-miss hrefs).
4. **(Phase 3) Runtime smoke test** — start the backend via `run_command` (local, so it passes the
   network gate), `curl`/`urllib` the endpoint with the seeded credentials, assert a 200 and the
   redirect. This is the only check that proves the JS/route *executes*, which the intent check
   (a *reading* of the file) can't (weaknesses.md #2, still-open part). Gated, best-effort, and it
   feeds failures back into the existing repair loop.

Misses feed back through `_file_op_flow` / `_intent_repair` — the repair machinery already exists;
the blueprint just gives it a **checklist of what "done" means** instead of a per-file syntax pass.

---

## 6. Preview = execution (fixes weakness #4 for free)

Because the blueprint is computed **once, at temperature 0, and cached for the turn**, it also fixes
the "the plan you preview is not the plan that runs" problem (weaknesses.md #4). Wire it into the
existing preview surface:

- Extend `get_plan()` ([core.py:2620](../app/agent/core.py#L2620)) / add `blueprint_preview()` and a
  `/blueprint <request>` REPL command that shows **features (by tier) + files + chosen stack**
  without building.
- The REPL "Plan" panel (M6, multi-task-handling.md) renders the blueprint before building; when
  interactive and `settings.blueprint_confirm` is on, the user approves or edits the file list first
  (`[b]uild / [e]dit / [c]ancel`). **The approved blueprint is the exact one that runs** — no
  re-decomposition, no temperature-0.3 drift.

This is also the honesty valve: the user *sees* that a backend and three extra pages are about to be
created and can veto scope before a single file is written.

---

## 7. Implementation plan (phased, by leverage)

> **Status (2026-07-23): Phases 0–1 + the deterministic half of Phase 2 landed, flag OFF.** Shipped
> `app/agent/blueprint.py` (gate + tiered dataclasses + tolerant `blueprint_from_data` +
> interface-contract `to_context_block`), `app/agent/runtime_probe.py` (`detect_stack`, stdlib
> default), `app/resources/prompts/blueprint.md`, the `expand_requirements` / `blueprint_optional_tier`
> / `blueprint_max_files` / `check_blueprint_coverage` settings, and the `chat()` seam +
> `_expand_requirements` / `_run_blueprint` / `_multi_file_flow(preplanned_ops=…)`. **Phase 2
> coverage** (`_verify_blueprint_coverage` + `_unwired_endpoints`): after a blueprint build, it
> creates any planned file still missing (threading the contract) and reports endpoints left unwired
> as `may not meet: …` — inert unless a blueprint ran. `tests/test_blueprint.py` (41 tests, offline)
> covers the gate, probe, parsing/tiers, the seam, and coverage. **Eval measuring stick (§8) landed:**
> `evals/checks.py` gained coherence checks (`has_backend_server`, `any_file_matches`, `route_wired`,
> `backend_defines_route`/`frontend_calls_route`, `backend_reads_fields`) that see the failures a
> file-exists eval can't (weaknesses.md #7); `evals/tasks.py` has a 4-task `BLUEPRINT_TASKS` suite;
> `python -m evals.run --blueprint` runs it with the flag on. `tests/test_evals.py` unit-tests the
> checks + a scripted end-to-end run.
>
> **Live-validated (2026-07-23) on `qwen2.5-coder:7b`: `python -m evals.run --blueprint` = 4/4 (100%).**
> Getting there fixed three real things the live run exposed: (1) `_expand_requirements` ran at temp
> 0.2 and flip-flopped between an actionable and a thin blueprint → gave it a dedicated **temp-0
> JSON-mode `_llm_blueprint`** (determinism + reliable parse of the nested schema); (2) a down Ollama
> silently scored 0/N → added an **Ollama preflight** to `evals/run.py` that fails loud; (3) the 7B
> model routinely *declares* a backend (an endpoint or a "Backend Server" feature) but omits the
> server file from `files`, dropping the build to layout-only → added a deterministic
> **`_ensure_backend` net** that synthesizes the server file from the declared contract when the
> blueprint's own output signals a backend. Generated apps are real: a login build ships `login.html`
> (email/password form) + `server.py` (a `do_POST` that reads those fields, checks a sqlite `users`
> table, returns JSON).
>
> **Phase 3 shipped (2026-07-23): runtime smoke test.** `app/agent/smoke.py`
> (`run_smoke_test`/`detect_ports`) + `AgentCore._smoke_test_backend`/`_pick_backend_entry` actually
> START the generated server, probe it over localhost HTTP, and kill the process tree — the only check
> that proves the backend RUNS, not just parses (weaknesses.md #2's last-open item). On a startup crash
> it feeds the traceback back for up to `max_smoke_repairs` regeneration passes. Opt-in
> (`settings.blueprint_smoke_test`, default off — it executes generated code), hard-timeout, localhost
> only. `tests/test_smoke.py` (15 tests, real subprocesses, offline). Live-verified: all three earlier
> generated backends (login/todo/contact) start and serve HTTP; end-to-end the build answer carries
> `Smoke test: server.py started; GET /api/login -> 501 on :8000`. Still to do: deterministic
> form-to-route auto-repair; broaden the blueprint eval suite; then flip the default on.

### Phase 0 — Prove the seam is safe (no behaviour change)
- Add `should_blueprint()` + `settings.expand_requirements` (**default `False`** initially).
- Add `app/agent/blueprint.py` with the dataclasses and a stub `_expand_requirements` that returns
  `None`. Wire the `chat()` seam so that with the flag off, **every existing test still passes
  unchanged** (this is the guardrail — the whole codebase's contract is "the pipeline behaves
  exactly as before when the new stage is empty", exactly how `extract_build_spec` was added).
- `tests/test_blueprint.py` (offline, `ScriptedLLM`): assert the gate fires on "build me a login
  page" and does **not** fire on "explain this file", "split styles into a css file", "fix the nav".

### Phase 1 — Blueprint → build (the core value) 🔴
- `app/resources/prompts/blueprint.md`: the extraction prompt. Asks for `{summary, features:[{name,
  tier, files}], files:[{filename, action, instruction}], contract:{endpoints, form_bindings,
  data_schema}}`. Tier rules spelled out; **output only JSON** (same discipline as
  `SPEC_INSTRUCTIONS` / `_MULTIFILE_PLAN_INSTRUCTIONS`).
- `_expand_requirements()`: one temp-0 `json_mode` call; parse tolerantly (reuse `_extract_json`);
  build the `Blueprint`; **delegate style/nav to the existing `_extract_build_spec`** so we don't
  duplicate buildspec.
- `runtime_probe.detect_stack()` → default **stdlib** stack.
- `_run_blueprint()` → `_multi_file_flow(preplanned_ops=blueprint.files, extra_context=
  blueprint.contract.to_context_block())`. Add the `preplanned_ops` param to `_multi_file_flow`.
- Deliverable: "build me a login page" now yields `login.html`, `styles.css`, `login.js`
  (validation + fetch to `/api/login`), `forgot-password.html`, `signup.html`, a stdlib `server.py`
  with `/api/login` + `/api/reset` + a seeded sqlite user store, and a `README` with run steps —
  all cross-consistent via the contract.

### Phase 2 — Coverage verification + stack detection 🔴
- `_verify_blueprint_coverage()` (checks 1–3 of §5) at the `chat()` seam; `settings.
  check_blueprint_coverage`. Deterministic form↔route wiring repair.
- `runtime_probe`: real detection of Flask/FastAPI/Node; contract carries the chosen stack into
  backend prompts.
- Flip `expand_requirements` default → **`True`** once evals (below) hold.

### Phase 3 — Runtime smoke test + preview/approval 🟡
- Check 4 of §5 (start server, hit endpoint), gated + best-effort.
- `/blueprint` command, REPL preview panel, `blueprint_confirm` approval gate.

### Settings to add (`config/settings.py`, same pattern as `check_intent`/`extract_build_spec`)
```
expand_requirements: bool = False    # Phase 0 default; → True after Phase 2
blueprint_optional_tier: bool = False  # build 'optional' features too
blueprint_confirm: bool = True       # interactive approval before building
blueprint_max_files: int = 12        # cap the fan-out (cost/coherence, weaknesses #6/#8)
check_blueprint_coverage: bool = True
blueprint_backend: str = "auto"      # auto | stdlib | flask | fastapi | node | none
```

---

## 8. Testing & evals — teach the harness to see this

The eval harness can't currently see any of these failures (weaknesses.md #7 — it checks
`file_exists` + substrings, which a broken build passes). Extend `evals/checks.py` and add tasks to
`evals/tasks.py`:

- `route_defined(route)` / `form_posts_to(form_file, route)` — the form's target is a real route.
- `route_reads_fields(server_file, [fields])` — the server reads the form's field names.
- `feature_present(blueprint, tier)` — every core/requested feature shipped.
- `backend_runs(server_file)` — (Phase 3) it starts and answers the seeded login.
- Golden tasks: "build me a login page", "make a contact form", "create a todo app" — each asserting
  the **backend file + wiring**, not just the HTML. These become the measuring stick before flipping
  the default on, per the roadmap's "do the evals first" rule.

Keep tests **offline**: `ScriptedLLM` returns a canned blueprint JSON; `detect_stack()` is
monkeypatched to a fixed stack; no real Ollama (mirror the `conftest.py` `check_intent`-OFF
discipline so the coverage check doesn't silently hit a live model in the suite).

---

## 9. Risks and the honest ceiling

- **The 7B model is the ceiling (weaknesses.md #1).** Asking it to *design and build a coherent
  full-stack app* is a bigger ask than the single-file generations it already fumbles. Expect the
  blueprint step itself to sometimes miss a file or mis-wire a route — which is *why* Phase 2's
  coverage check and the interface contract are not optional polish but load-bearing. For serious
  full-stack builds, the `/model` escape to 14B/32B will matter more here than anywhere else; keep
  the **default scope conservative** (frontend-complete + wired stdlib backend) so it stays reliable
  offline.
- **Cost/latency (weaknesses.md #8).** This multiplies LLM calls: blueprint + N files (now more
  files) + verify + coverage. Mitigations already in the design: gate hard to greenfield builds
  (ordinary requests pay nothing), one cached temp-0 blueprint call, `blueprint_max_files` cap, and
  reuse of every existing flow.
- **Scope creep / inventing (§2).** The tier system + the narrow `should_blueprint` gate + the
  preview/approval step are the three guards. If any one is weakened, the 7B model's tendency to
  improvise returns. Do not let `_expand_requirements` add features the tiering can't justify.
- **False "it works".** A generated backend that imports a missing library is worse than no backend.
  The runtime probe (never emit a stack that isn't installed) plus the Phase 3 smoke test are the
  answer; until Phase 3 lands, the coverage report must say *"backend wired, not runtime-tested"*
  rather than "verified OK" — same honesty rule as `may not meet:` in the intent check.

---

## If you do only three

1. **The interface contract (§3d).** Without it, "frontend + backend" is two files that don't talk.
   It's the whole difference between a working app and a pile of related files, and it's a direct
   extension of the BuildSpec pattern that already works.
2. **The stdlib-default backend + runtime probe (§4).** This is what makes "what happens after I
   press the button" real *and* keeps the offline promise. Never emit a stack the machine can't run.
3. **Blueprint-coverage verification (§5).** This is the first thing in Coder that checks the
   *whole request* was satisfied (weaknesses.md #3), and the blueprint is what makes it definable.
   It turns "Created 6 files — verified OK" into "the login button actually logs in."

None of these beat the model ceiling (#1) — like the rest of the harness, they make Coder *honest
about it*: infer the full build, ground it in what runs offline, and prove the whole thing was
delivered.

---

*Grounded in a read of `app/agent/core.py`, `planner.py`, `buildspec.py`, `intent.py`,
`verify.py`, `references.py`, and `config/settings.py`, plus the design docs. Line references may
drift as the code evolves.*
