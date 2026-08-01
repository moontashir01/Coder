# Phase 0 baseline — full-stack web specialization

Recorded **2026-07-31**, on the dev machine (Windows 11, Python 3.12.10, `.venv`).
This is the "before" number for `docs/fullstack-web-plan.md`. Phases 1–7 get measured
against it. Do not re-baseline without re-running both suites the same way.

## What Phase 0 changed

| # | Change | Where |
|---|---|---|
| 1 | Flask **3.1.3** installed into `.venv` (+ `blinker`, `itsdangerous`, `Werkzeug`, pinned into `requirements.txt`). **No gunicorn** — it imports `fcntl` and does not run on Windows; it belongs only in the *generated* app's requirements, for the Linux host. | `requirements.txt` |
| 2 | `expand_requirements` → `True`, `blueprint_smoke_test` → `True` | `config/settings.py` |
| 3 | `_no_blueprint` autouse fixture defaults both flags **off** in tests | `conftest.py` |
| 4 | Per-suite flag pinning so the golden suite still measures plain routing | `evals/run.py` |

Nothing new was built. `runtime_probe.detect_stack()` now returns `_flask()` on its own —
verified, no code change was needed.

### Note on (4), which the plan did not anticipate

`settings.expand_requirements` is now a **global** default, and `should_blueprint()` matches
several `GOLDEN_TASKS` prompts verbatim (`"Create an index.html file for a simple landing
page"`). Left alone, the default eval suite would have silently stopped measuring plain
routing, and its 14/14 baseline would no longer be comparable to anything. `evals/run.py`
now sets `settings.expand_requirements = blueprint`: `--blueprint` measures the blueprint
path, the default suite measures routing. One knob each.

## The numbers

### `pytest tests/`

| Run | Result | Wall clock |
|---|---|---|
| Ollama **up** | 627 passed, exit 0 | 589s (9:49) |
| Ollama **stopped** | 627 passed, exit 0 | 545s (9:05) |

**The suite is genuinely offline.** It was re-run with the Ollama server killed and passed
identically — which is the property step 3 of the plan exists to protect, and a stronger
check than the clock.

**The plan's "~6 minutes" tripwire is stale, not a regression.** `--durations=25` shows a flat
~7.6s floor on *every* test that constructs an `AgentCore`, in files with nothing to do with
the blueprint (`test_context_budget`, `test_evals`, `test_file_flow`, `test_buildspec`).
Measured directly: `import app.agent.core` = 1.77s, `AgentCore()` = ~2.3s each. ~70 heavy
tests × ~7.6s ≈ the whole runtime. The flip cannot contribute — the autouse fixture forces
both flags `False` for every test under the rootdir, so test-time behaviour is identical to
before. `CLAUDE.md` has been corrected from ~6 min to ~9 min.

### `python -m evals.run --blueprint`

**4/4 passed (100%)**, 345s.

```
[PASS] bp_login_fullstack   form, password field, backend app.py, backend reads password, 3 files
[PASS] bp_signup_reset      form, backend app.py, 9 files
[PASS] bp_todo_app          backend app.py, fetch( in static/todo.js, 5 files
[PASS] bp_contact_form      form, backend app.py, action= in template, 4 files
```

The Flask stack is visibly in effect: builds now emit `app.py`, `templates/*.html`,
`static/*` and bind **:5000**, instead of the old `http.server` shape.

## The finding that matters most

The suite scores **4/4, but only 3 of the 4 generated apps actually run.** Smoke-testing each
kept artifact directly:

```
[OK  ] bp_contact_form      app.py started; GET / -> 404 on :5000
[OK  ] bp_login_fullstack   app.py started; GET / -> 404 on :5000
[FAIL] bp_signup_reset      app.py crashed on startup (NameError: name 'app' is not defined)
[OK  ] bp_todo_app          app.py started; GET / -> 404 on :5000
```

`bp_signup_reset` is the one build that split itself across `app.py` / `routes.py` /
`models.py`. Its `routes.py` uses `@app.route`, `sqlite3` and `DATABASE` without importing
any of them, and `app.py` does `import routes` at line 5 — so the process dies at import.
It passed every check anyway, because `has_backend_server()` and `min_files_written()` read
files; they never run them.

Two things follow, and both are already in the plan:

- **Gap 2 is real and measurable.** The 7B model gets hand-written Flask wiring wrong roughly
  1 in 4 builds, and it gets it wrong in the *boilerplate*, not the domain logic. That is
  precisely what Phase 1's deterministic scaffold and its fixed canonical layout remove.
- **Gap 3 is real and measurable.** A 100% eval score coexisting with a crashing app is the
  "it starts is not it works" problem stated numerically. Note the passing three answer
  `404` on `/` — they serve, but nothing is at the root. Phase 5's functional probe and
  Phase 7's `db_has_column` / `post_persists` / `earlier_pages_still_work` checks are what
  turn this into a number that can move.

Also worth recording for later: `blueprint_smoke_test` demonstrably fired during the eval run
(the artifacts contain `todos.db`, `messages.db` and `__pycache__/*.pyc`, which only exist if
`app.py` was executed), and `bp_signup_reset` still shipped broken — so the existing
`max_smoke_repairs=1` regeneration pass did not rescue it. Worth a look when Phase 5 extends
that loop.

## How to reproduce

```
.venv\Scripts\python.exe -m pytest tests/ -q --durations=25
.venv\Scripts\python.exe -m evals.run --blueprint --keep <dir>
```

The planner runs at temperature 0.2, so per `CLAUDE.md` a single eval run proves nothing
directional — the 4/4 above is one run. Re-run ~5× before treating any later change as a
regression or a fix.
