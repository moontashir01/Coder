# Full-Stack Web Specialization — implementation plan

**Goal.** Turn Coder from "generates static pages" into "builds a runnable, deployable
full-stack web application, incrementally, across many turns."

**Acceptance test (the demo).** In a live session:

```
coder> build me an e-commerce site for selling books
coder> add an admin page where I can add a product with a picture
coder> add a shopping cart
coder> now let customers search products by title
```

…and at the end, `python app.py` starts a working site: the storefront lists products
from SQLite, the admin form uploads an image that persists to disk and shows on the
storefront, the cart works, search works, and **every page written in turn 1 still works
after turn 4**.

This document is the build order. It is written to be implemented phase by phase.

---

## 0. Where Coder actually is today (read this first)

Do **not** start from scratch. Roughly 60% of this already exists and is tested.

| Already built | Where | State |
|---|---|---|
| Requirements Blueprint — infers implied features, file list, API contract from one short request | `app/agent/blueprint.py` | Works. Flag `expand_requirements` **OFF** by default |
| Stack detection — picks a backend that actually runs on this machine | `app/agent/runtime_probe.py` | Works. Defaults to Python **stdlib** |
| Coverage check — creates planned files the model forgot, reports unwired endpoints | `core.py::_verify_blueprint_coverage` | Works, on by default |
| Smoke test — actually **starts** the generated backend and probes it over HTTP | `app/agent/smoke.py` | Works. Flag `blueprint_smoke_test` **OFF** |
| Dead-reference / nav / link repair | `core.py::_repair_dead_references`, `_repair_nav_consistency`, `_repair_page_links` | Works, on by default |
| Syntax + intent verification per file | `app/agent/verify.py`, `app/agent/intent.py` | Works, on by default |
| Eval harness with blueprint tasks | `evals/` (`python -m evals.run --blueprint`) | 4/4 live on qwen2.5-coder:7b |

### The three gaps that lose the demo

**Gap 1 — no memory of the project between turns.** This is the biggest one.
`core.py:2829` does `self._blueprint = None` at the top of every `chat()`. The blueprint —
the endpoints, the schema, the feature list — exists for exactly one turn and is then
thrown away. Worse, `should_blueprint()` (`blueprint.py:44`) *deliberately excludes*
incremental verbs (`add`, `update`, `edit`, `fix`) and `_EDIT_INTO_RE` rejects
"add X to Y". So turn 2 of the demo — "add an admin page" — never sees turn 1's contract
at all.

What Coder has instead is not enough:
- `app/memory/conversation.py` — a sliding window of raw chat text. The model has to
  re-read prose and re-infer the schema. A 7B model will not do this reliably.
- `app/memory/project_memory.py` — a filesystem scan producing language counts and a
  module list. It knows `app.py` exists; it does not know `app.py` defines
  `POST /admin/products` reading `title, price, image`.

**Gap 2 — the default stack is not deployable.** `runtime_probe.STDLIB_STACK` emits
`http.server` + `BaseHTTPRequestHandler`. It runs offline, which was the right call at the
time, but it is ~80 lines of routing boilerplate the 7B model must get right by hand, it
has no templating, no session handling, and no file-upload parsing. Nobody deploys it.

**Gap 3 — "it starts" is not "it works."** The smoke test proves the process survives and
answers a socket. It does not prove that submitting the product form stores a row, or that
the uploaded image comes back on the next page load.

Phases 1–7 below close exactly these three gaps.

---

## 1. The stack decision

### Recommendation: **Flask + Jinja2 + SQLite, one process, server-rendered**

```
Python 3 · Flask · Jinja2 templates · stdlib sqlite3 · vanilla CSS/JS
```

Do not add SQLAlchemy, React, a build step, or a separate frontend dev server.

### Why this one

**It is one process.** Flask serves the HTML pages, the static CSS/JS, the uploaded
images, and the JSON API. One thing to start, one thing to deploy, no CORS, no
`npm run dev` in a second terminal. For a live demo this matters more than anything else.

**Server-rendered forms kill the "dead button" problem structurally.** A plain
`<form method="post" action="/admin/products">` posting to a Flask route works with
**zero JavaScript**. The single failure Coder exists to fix — the button that does nothing
— stops being a wiring problem and becomes an HTML attribute the scaffold writes
deterministically.

**Jinja template inheritance kills the navbar bug structurally.** `base.html` defines the
nav once; every page does `{% extends "base.html" %}`. The "every page has a different
navbar" class of bug that `_repair_nav_consistency` exists to patch becomes impossible.
That repair pass keeps working as a safety net, but it will have nothing to do.

**File upload is three lines.** This is literally the faculty's example ("add products with
pictures which will be stored in backend"):

```python
f = request.files["image"]
name = secure_filename(f.filename)
f.save(Path(app.static_folder) / "uploads" / name)
```

Doing the same with `http.server` means hand-parsing a multipart body. Doing it in Express
means `npm install multer`.

**Sessions and auth are built in.** `flask.session` is a signed cookie — login/logout in
~15 lines, no JWT plumbing.

**The model knows Flask cold.** `qwen2.5-coder:7b` has seen enormous amounts of Flask.
Measured against stdlib `BaseHTTPRequestHandler`, its first-attempt correctness is not
close. Picking the framework the small model writes best is a real engineering lever, not
a cosmetic choice.

**It stays Python.** `verify.py` compiles `.py` in-process with `compile()` — no external
binary, no `node --check`, no `tsc`. Every existing verification and repair path keeps
working unchanged.

**It deploys.** `requirements.txt` + `gunicorn app:app` → Render, Railway, Fly,
PythonAnywhere. The `Procfile` is one line and Phase 1 writes it from the template. (No
Dockerfile in the scaffold — those hosts read the Procfile directly, and an unused
Dockerfile is one more file that can drift out of sync with the app.)

**`runtime_probe` already prefers it.** `detect_stack()` at `runtime_probe.py:98` returns
`_flask()` the moment `find_spec("flask")` resolves. Installing Flask into the venv is
literally the whole change — see Phase 0.

### Why not the alternatives

| Stack | Why not |
|---|---|
| **stdlib `http.server`** (today's default) | Not deployable, no templating, no upload parsing, 80 lines of boilerplate the model must nail every time |
| **FastAPI** | API-first. Serving HTML + uploads + sessions needs Jinja2Templates, `python-multipart`, `StaticFiles`, `SessionMiddleware` — more moving parts for the same result, plus a `uvicorn` line to explain |
| **Express / Node** | `npm install` needs the network (gate is off by default), `node_modules` bloat, and verification needs `node --check` on PATH |
| **Next.js / React** | Two processes, a build step, hundreds of MB of deps, and a 7B model writes app-router React badly. This loses the demo |
| **Django** | Genuinely deployable and batteries-included, but `manage.py startproject`, apps, settings.py, ORM migrations, and admin scaffolding are far too much surface for a 7B model to modify coherently turn after turn |

**One caveat, stated plainly:** Flask must be installed in the venv, so this stack is not
*quite* zero-install the way stdlib is. It is a single offline-cached wheel and it does not
break the "no network at runtime" property — the agent still never phones out. Keep
`STDLIB_STACK` as the automatic fallback for machines without Flask; `runtime_probe`
already does this.

### The canonical project layout

Every web app Coder builds uses **exactly this shape**. Fixing the layout is what lets
every prompt, every check, and every repair pass know where things are without asking.

```
<project>/
  app.py                  # Flask app + routes ONLY
  db.py                   # sqlite3 connection, init_db(), idempotent migrations
  models.py               # per-entity query helpers: list/get/create/update/delete
  requirements.txt
  seed.py                 # demo data, so the site is never empty on first load
  README.md               # how to run + how to deploy
  Procfile                # web: gunicorn app:app
  .gitignore
  static/
    css/style.css
    js/app.js
    uploads/.gitkeep      # user-uploaded images land here
  templates/
    base.html             # nav + {% block content %} — the ONLY place nav exists
    index.html
    <one per page>
```

---

## 2. The core idea: scaffold deterministically, generate only the domain

This is the single highest-leverage decision in the plan.

Today the LLM writes every byte of every file. Most of those bytes are boilerplate that is
identical in every Flask app ever written — the `Flask(__name__)` line, the sqlite
`get_db()` helper with `row_factory`, the `if __name__ == "__main__"` block, the
`{% block content %}` skeleton, `.gitignore`. Every one of those bytes is a chance for the
7B model to hallucinate.

**So stop generating them.** Phase 1 ships a real, runnable Flask skeleton as template
files under `app/resources/scaffolds/flask/`. Coder copies them verbatim — no LLM call, no
failure mode — and the model is then asked only to write the *domain* layer: the entity
fields, the routes specific to this app, and the page bodies.

The effect on the demo is large:
- The app **starts** before the model has written a single line.
- Verification failures collapse to the domain layer, which is where repair actually works.
- Turn 1 gets dramatically faster (fewer generated files).
- "Deployable" becomes true by construction — `requirements.txt`, `Procfile`, and README
  deploy steps are in the template, not left to chance.

---

## Phase 0 — Flip on what already works (½ day)

Nothing new is built here. This establishes the baseline and proves the existing machinery.

1. Install Flask into Coder's own venv, and add it to `requirements.txt`:
   ```
   .venv\Scripts\python.exe -m pip install flask
   ```
   `runtime_probe.detect_stack()` now returns `_flask()` automatically
   (`runtime_probe.py:98`). The smoke test launches with `sys.executable`
   (`smoke.py:87`), i.e. the same venv — so a generated Flask app can actually run.

   **Do not install gunicorn locally.** It does not run on Windows (it imports
   `fcntl`), so installing it here buys nothing and gives you a broken import to
   trip over. It belongs *only* in the **generated** app's `requirements.txt`, for
   the Linux host that will actually serve it. Locally you always run
   `python app.py` (the Flask dev server).

2. In `config/settings.py`, flip the defaults:
   - `expand_requirements: bool = True` (line 149)
   - `blueprint_smoke_test: bool = True` (line 167)

3. **Guard the test suite — this is not optional.** Every test that cares about these flags
   sets them explicitly by monkeypatch, but tests that merely *write a file* do not, and a
   prompt like `"Create an index.html file for a simple landing page"`
   (`evals/tasks.py::create_html_page`, and several in `tests/`) matches `should_blueprint()`.
   With the default flipped, those tests start calling `_expand_requirements` → a real
   `ChatOllama`. That is exactly the trap `conftest.py` already documents for `check_intent`
   ("28s → 611s, all still 'passing', which is exactly how a silent network dependency gets
   into a test suite"). Add the mirror fixture to `conftest.py`:

   ```python
   @pytest.fixture(autouse=True)
   def _no_blueprint(monkeypatch):
       """Default the blueprint stage OFF in tests — same reason as _no_intent_check.
       tests/test_blueprint.py and tests/test_evals.py opt back in explicitly."""
       monkeypatch.setattr(settings, "expand_requirements", False)
       monkeypatch.setattr(settings, "blueprint_smoke_test", False)
   ```

   `tests/test_blueprint.py` (lines 352, 376, 405) and `tests/test_evals.py` (line 243)
   already set the flag themselves, so they keep passing unchanged.

4. Baseline it — record the number, you will need it to prove improvement:
   ```
   python -m evals.run --blueprint
   pytest tests/ -v
   ```
   Time the suite. If it jumped from ~6 minutes, step 3 didn't take — find the test that is
   reaching Ollama before going further.

**Done when:** the blueprint path is on by default, the suite is green **and still ~6
minutes**, and you have a written-down baseline score.

---

## Phase 1 — The web-app scaffold (2 days)

**Closes Gap 2.** Deterministic, runnable Flask skeleton before any generation.

### New files

`app/resources/scaffolds/flask/` — the template tree from §1, with `{{PROJECT_NAME}}` /
`{{SECRET_KEY}}` placeholders. Resolve it through a new
`settings.scaffolds_dir = _RESOURCES / "scaffolds"` (`config/settings.py:60-62` is the
pattern) — **never** from cwd, per the "Bundled resources & packaging" rule in `CLAUDE.md`.

No `pyproject.toml` change is needed: the existing declaration is a recursive glob,
`app = ["resources/**/*", ...]` (line 67), which already picks up `resources/scaffolds/`.

`app/agent/scaffold.py`:

```python
def is_web_app(blueprint: Blueprint) -> bool: ...
    # backend stack != none AND (endpoints OR a page/template file)

def scaffold_flask(root: Path, name: str) -> list[str]: ...
    # copy the tree, substitute placeholders, return relative paths written.
    # NEVER overwrite an existing file — an amendment turn must be a no-op here.

def scaffold_files() -> set[str]: ...
    # the names the scaffold owns, so generation is told not to rewrite them
```

The scaffolded `app.py` must be genuinely runnable on its own: `/` renders `index.html`
with a "generated by Coder" placeholder, `db.init_db()` runs at startup, static files
serve. Start it by hand once and confirm before wiring anything up.

`db.py` carries the migration primitive that Phase 3 depends on — write it now:

```python
def ensure_column(conn, table: str, column: str, decl: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN, guarded by PRAGMA table_info."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
```

### Modified

- `core.py::_run_blueprint` (line 2128) — call `scaffold_flask()` **before** the per-file
  generation loop when `is_web_app(blueprint)`. The project root is
  `Path(self._project_path or Path.cwd())` — the same expression `_multi_file_flow` (line
  2031) and `_verify_blueprint_coverage` (line 2205) already use; do not invent a second
  way to resolve it. Drop any planned file the scaffold already owns, and add the
  scaffold's file list to the plan manifest as context so generated code imports
  `db`/`models` instead of reinventing them.

- **Raise `blueprint_max_files` and stop truncating silently.** `_run_blueprint:2142` does
  `blueprint.build_files(...)[: settings.blueprint_max_files]`, and
  `_verify_blueprint_coverage:2211` applies the identical slice. With the default of `12`,
  a plan for an e-commerce site is silently **cut off** — the files past position 12 are
  never built, never reported, and the coverage check that exists to catch missing files
  applies the same slice, so it cannot catch them either. The user sees a smaller app and
  no explanation. Two changes:
  - raise the default to `24` (the scaffold now owns ~10 of the boilerplate files, so the
    generated set is smaller, but an e-commerce build still clears 12 easily);
  - when the slice actually drops files, append a `may not meet:` line naming them, in
    keeping with the codebase's "never claim a pass you didn't get" rule. A cap that
    reports is a budget; a cap that hides is a bug.
- `app/resources/prompts/blueprint.md` — teach it the canonical layout: routes go in
  `app.py`, queries in `models.py`, pages in `templates/*.html` extending `base.html`.
- `runtime_probe._flask()` — extend the note with the layout and "extend base.html;
  never write a full `<html>` document in a child template."

### Keep the *generated site* offline too — this is a live demo risk

Coder the agent is fully offline. The **websites it generates are not**, and on a machine
with no network that is visible on screen.

`buildspec.py:258-260` instructs the model, in as many words, to "Load them from Google
Fonts with a `<link>` in every page's `<head>`" — because `_STYLE_PRESETS` (lines 56-137)
translates every style word into real Google Font families (`Playfair Display`, `Inter`,
`Poppins`, …). And `references.py:60` deliberately **ignores** external URLs, so no
existing pass strips or flags them. Every styled build therefore ships a hard dependency on
`fonts.googleapis.com`.

Offline, that means: the browser blocks on a dead DNS lookup for a few seconds per page,
then falls back to a default font — so the exact typography the build spec picked is the
one thing the faculty never sees. If the model *also* emits a Tailwind or Bootstrap CDN
`<script>`/`<link>` (7B models do this constantly, and nothing currently stops it), the
page renders **completely unstyled**. That single failure would do more visible damage to
the demo than any backend bug in this document.

Two changes, both small:

1. **Make the font instruction conditional on `settings.allow_network`.** With the network
   off (the default), `BuildSpec.to_prompt_block()` emits a **system font stack** instead
   of a Google Fonts link:
   ```css
   font-family: ui-sans-serif, system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
   ```
   Keep the presets' *pairing intent* (a display face for headings, a readable one for
   body) by mapping each preset to a system-stack pair rather than deleting the styling.
   The scaffold's `static/css/style.css` declares both stacks as CSS variables, so
   generated CSS references `var(--font-heading)` and the choice lives in exactly one file.
   With `--allow-network`, the current Google Fonts behaviour is unchanged.

2. **Add an external-asset guard to `verify.py`'s content checks.** When
   `allow_network` is False, a generated `.html` carrying a `<script src="http…">` or
   `<link rel="stylesheet" href="http…">` is a defect — the page cannot render as designed
   on this machine. Report it and strip it in the repair pass. This is the same shape as
   the existing wrong-language/prose content guards: deterministic, no tooling, no LLM.

**Test it the honest way:** pull the network cable (or `ipconfig /release`) and load a
generated page. If it looks identical to how it looked online, this is done.

### Tests — `tests/test_scaffold.py`

- scaffold writes every expected path into `tmp_path`
- placeholders are fully substituted (no `{{` survives)
- re-running never overwrites a modified file
- **the scaffolded app starts and serves `/`** — reuse `smoke.run_smoke_test`

**Done when:** `build me a blog` produces a project you can `python app.py` and open in a
browser, before considering a single generated line.

---

## Phase 2 — ProjectSpec: persistent project memory (3 days)

**Closes Gap 1, part 1.** The blueprint stops being per-turn and becomes the project's
living state.

### New file — `app/agent/projectspec.py`

Persisted at `<project>/.coder/project.json`. Inside the project, not in `.coder.db`, so it
survives, is inspectable, is diffable in git, and travels with the folder.

```jsonc
{
  "spec_version": 1,
  "revision": 3,                  // bumped every amendment
  "name": "bookshop",
  "summary": "Online bookstore with admin product management",
  "stack": { "language": "python", "backend": "flask" },

  "entities": [                   // STRUCTURED — this is what makes migrations work
    { "name": "product", "table": "products",
      "fields": [
        {"name": "id",         "type": "INTEGER", "pk": true},
        {"name": "title",      "type": "TEXT",    "required": true},
        {"name": "price",      "type": "REAL"},
        {"name": "image_path", "type": "TEXT",    "added_in": 2}
      ]}
  ],

  "endpoints": [
    { "method": "POST", "path": "/admin/products",
      "request": "{title, price, image}", "response": "302 -> /admin/products",
      "handler": "app.py", "template": "templates/admin_products.html",
      "entity": "product", "added_in": 2 }
  ],

  "pages": [
    { "route": "/", "template": "templates/index.html",
      "nav_label": "Home", "purpose": "storefront product grid",
      "reads": ["product"] }
  ],

  "features": [ {"name": "Product catalog", "tier": "requested", "files": [...], "added_in": 1} ],
  "files":    { "app.py": {"role": "backend", "purpose": "routes"} },
  "history":  [ {"revision": 2, "request": "add an admin page…", "added": {...}} ]
}
```

`entities` is the important addition over today's `ApiContract.data_schema`, which is a
tuple of free-text strings. Free text cannot be diffed, so it cannot produce a migration.
Structured fields can.

API:

```python
@dataclass
class ProjectSpec:
    @classmethod
    def load(cls, root: Path) -> "ProjectSpec | None": ...
    @classmethod
    def from_blueprint(cls, bp: Blueprint, root: Path, name: str) -> "ProjectSpec": ...
    def save(self, root: Path) -> None: ...          # atomic write via tmp + replace
    def to_context_block(self) -> str: ...           # compact prompt block
    def merge_delta(self, delta: SpecDelta) -> list[str]: ...  # -> impacted files
    def entity(self, name: str) -> Entity | None: ...
    def ddl(self) -> list[str]: ...                  # CREATE TABLE per entity
    def migrations(self, since: int) -> list[str]: ...  # ensure_column calls
```

`to_context_block()` is the load-bearing method. It replaces "let the model re-read chat
history" with a compact, factual statement of what exists. Budget it hard —
target ≤1200 chars, drop `history` and `purpose` strings first — it rides in the same
prompt as the plan manifest and sibling context inside `llm_num_ctx`.

Rules, matching the codebase's existing philosophy:
- Same validation discipline as `blueprint._norm_filename` / `_clean_endpoints`: safe
  relative paths only, known types only, caps on every list.
- A corrupt or unparseable `project.json` returns `None`, never raises. Coder then behaves
  exactly as it does today.
- Writing the spec is **best-effort** and never fails a turn whose files were written.
- **`save()` writes the file directly (`tmp` + `os.replace`), NOT through
  `executor.execute("write_file", …)`.** The spec is agent state, not user code. Routing it
  through the tool would put it behind the approval gate — an `[a]llow / [s]ession /
  [d]eny` prompt after every single turn, mid-demo — and would push a backup into
  `.coder_backups/` on every save, evicting the user's real undo history against
  `max_write_backups`. Direct write is correct here; the path is inside `sandbox_root`
  either way.
- `.coder/` is a dot-directory, so the RAG indexer and `project_memory._scan_project`
  already skip it (both filter `part.startswith(".")`). That is deliberate — the spec must
  not get embedded and retrieved back as if it were source. Do not "fix" the skip.

### Modified

- `core.py::_run_blueprint` — after a successful build, `ProjectSpec.from_blueprint(...)`
  and `.save()`.
- `core.py::chat` — load the spec once per turn into `self._spec` when a project is loaded.
- `app/cli/` — add `/spec`, which pretty-prints entities, endpoints, and pages. Small
  change, disproportionate demo value: it is the visible proof that the agent *remembers*.

### Tests — `tests/test_projectspec.py`

Round-trip save/load; corrupt JSON → `None`; `from_blueprint` maps a contract to entities;
`ddl()` emits valid SQL (assert with stdlib `sqlite3` in-memory); `to_context_block` stays
under budget; atomic write leaves no partial file.

**Done when:** turn 1 leaves a `.coder/project.json` that accurately describes what was
built, and `/spec` shows it.

---

## Phase 3 — The amendment flow: connecting turn N to turn 1 (4 days)

**Closes Gap 1, part 2.** This is the phase the faculty demo lives or dies on.

### The route

New gate in `blueprint.py` — the mirror image of `should_blueprint()`:

```python
def should_amend(message: str, spec_exists: bool) -> bool:
    """A change to a project we already have a spec for.

    Fires on exactly the verbs should_blueprint() rejects — add/update/change/
    remove/rename/also/now — but ONLY when a ProjectSpec exists. Without a spec
    there is nothing to amend and routing is unchanged.
    """
```

In `chat()`, ahead of the existing blueprint gate:

```python
if self._spec and should_amend(clean_message, True):
    answer, trace = await self._amend_project(clean_message, self._spec, at_refs)
```

### `core.py::_amend_project` — the flow

**Step 1 — Delta extraction (one LLM call, temperature 0, JSON mode).**
Reuse `self._llm_blueprint`. Prompt = new `app/resources/prompts/amend.md`, containing the
current spec's `to_context_block()` plus the user's message. It returns *only what changes*:

```json
{
  "summary": "add product images",
  "entities":  [{"name":"product","add_fields":[{"name":"image_path","type":"TEXT"}]}],
  "endpoints": [{"method":"POST","path":"/admin/products","request":"{title,price,image}"}],
  "pages":     [{"route":"/admin/products","template":"templates/admin_products.html","nav_label":"Admin"}],
  "new_files": [{"filename":"templates/admin_products.html","instruction":"..."}]
}
```

Note it does **not** list which existing files to edit. That is Step 2's job, and it is
deterministic — asking a 7B model "what else does this break?" is exactly the question it
answers badly.

**Step 2 — Impact analysis (`app/agent/impact.py`, no LLM).** This is the heart of "update
past files so they run smoothly with new files." Given the delta and the spec, compute the
edit set by rule:

| Delta | Files that must change | Why |
|---|---|---|
| new field on entity E | `db.py` | add `ensure_column` migration |
| | `models.py` | INSERT/UPDATE column lists |
| | every template whose `reads` includes E | display the new field |
| | every endpoint template with a form for E | new form input |
| | `seed.py` | seed the new column |
| new endpoint | `app.py` | define the route |
| | the template named in its `form_binding` | point the form at it |
| new page | `templates/base.html` | add the nav link |
| | `app.py` | add the view function |
| new entity | `db.py`, `models.py`, `seed.py` | table + helpers + demo rows |
| renamed field/route | every file referencing the old name | rename |

Each rule returns `(filename, reason)`. The reason string is threaded into that file's
edit instruction, so the model is told *precisely* what to change and why — not handed the
whole request again.

**This is the demo's money shot.** "I added images, and it went back and updated `db.py`,
`models.py`, the storefront template, and the seed script so everything still lines up" is
exactly the capability the faculty said was missing.

**Step 3 — Apply.** New files via the existing `_multi_file_flow(preplanned_ops=...)` seam;
existing files via `_surgical_edit` (surgical, so untouched code cannot be clobbered), each
carrying the spec's `to_context_block()` plus its impact reason. Schema changes are **not**
generated — `db.py`'s migration block is rewritten from `spec.migrations(since=revision)`,
deterministically.

**Step 4 — Persist.** `spec.merge_delta(delta)`, bump `revision`, append to `history`, save.

**Step 5 — Verify. Read this carefully; the obvious implementation silently skips it.**

The post-turn chain in `chat()` is *not* uniformly gated. Two of the five passes are gated
on the per-turn blueprint object:

| Pass | Gate | Runs on an amendment turn? |
|---|---|---|
| coverage check | `self._blueprint is not None` (line 2897) | **No** |
| reference repair | `settings.check_references and trace` (line 2912) | Yes |
| nav consistency | same `trace` gate | Yes |
| page links | same `trace` gate | Yes |
| smoke test | `self._blueprint is not None` (line 2955) | **No** |

And `chat()` resets `self._blueprint = None` at line 2829 on every turn. So an amendment
flow that does not set it gets **no coverage check and no smoke test** — meaning turns 2, 3
and 4 of the demo would never be verified or run, which is precisely the property being
demonstrated. The bug would be invisible: the turn still reports success, because the
checks that would have contradicted it never executed.

Fix it explicitly. Either build a `Blueprint` from the amended spec and assign
`self._blueprint` (cheapest — every downstream consumer already understands that type), or
introduce `self._webapp_turn: bool` and widen both gates to
`(self._blueprint is not None or self._webapp_turn)`. Prefer the first; it reuses
`_unwired_endpoints` and `_pick_backend_entry` with no changes.

**Add a regression test for exactly this** in `tests/test_amend.py`: run an amendment turn
with `blueprint_smoke_test=True` and a monkeypatched `_smoke_test_backend`, and assert it
was called. Without that test this silently regresses the moment anyone touches the gates.

### Tests — `tests/test_amend.py`, `tests/test_impact.py`

`should_amend` fires on "add a cart" only with a spec present; impact rules produce the
expected edit set for each delta kind; `merge_delta` bumps revision and records history;
a delta adding a field produces a valid `ALTER TABLE` (assert against real in-memory
sqlite3); a failed delta call leaves the spec untouched and falls through to today's
routing. All offline with `ScriptedLLM`.

**Done when:** turn 2 of the demo works and turn 1's files are updated, not orphaned.

---

## Phase 4 — Domain generation: CRUD, uploads, auth (3 days)

The features the faculty's e-commerce example actually names.

**4a — CRUD from entities (deterministic).** `app/agent/crud.py` generates
`models.py` helpers (`list_products`, `get_product`, `create_product`, `update_product`,
`delete_product`) straight from `spec.entities`. Parameterized SQL, no string
interpolation of values. This is pure codegen — no LLM, no hallucinated column names, SQL
injection impossible by construction. The LLM writes only routes and templates.

**4b — File/image upload.** When an entity has a field of type `IMAGE` or `FILE`:
- scaffold guarantees `static/uploads/` exists
- `app.py` gets the upload block (extension allowlist, `secure_filename`,
  `MAX_CONTENT_LENGTH`, collision-safe naming)
- the form template gets `enctype="multipart/form-data"` — add a deterministic check for
  this; a form with a file input and no enctype silently uploads nothing, and it is the
  single most likely way the live demo embarrasses you
- display templates render `{{ url_for('static', filename='uploads/' ~ item.image_path) }}`

**4c — Sessions and auth.** When the spec has a `user` entity or a `/login` endpoint,
scaffold in `flask.session` login/logout, `werkzeug.security.generate_password_hash`, and a
`@login_required` decorator. Never store a plaintext password — enforce this as a
deterministic check on generated code, not a prompt instruction.

**4d — Seed data.** `seed.py` generates 3–5 rows per entity so the storefront is never
empty on first load. An empty page in a demo reads as broken even when it is correct.

### Tests
`tests/test_crud.py` — generated `models.py` compiles and its statements execute against
in-memory sqlite3; upload path rejects `../` and disallowed extensions; a file-input form
without `enctype` is caught; no generated file contains a plaintext password write.

---

## Phase 5 — Prove it works, don't just prove it starts (2 days)

**Closes Gap 3.** Upgrade `smoke.py` from liveness to a functional probe.

New `smoke.functional_probe(spec, base_url)`:

1. `GET` every page route in `spec.pages` → assert 2xx, assert non-empty `<body>`
2. For each `POST` endpoint, synthesize a body from the entity's fields — strings for
   `TEXT`, `19.99` for `REAL`, a 1×1 PNG written with stdlib `zlib`+`struct` for `IMAGE`
   (no Pillow dependency) — post it, assert not 5xx
3. `GET` the entity's list page again → **assert the value just posted appears in the HTML**
4. Report per-check: `POST /admin/products -> 302; product visible on / ✓`

Step 3 is the whole point. It is the difference between "the server started" and "adding a
product works," and it is the sentence you want in the terminal when the faculty is
watching.

Feed any failure back through the existing `max_smoke_repairs` regeneration loop — a
traceback plus "posting to /admin/products returned 500" is a far better repair prompt than
anything static analysis produces.

Keep it gated (`blueprint_smoke_test`), localhost-only, hard-timeout, process-tree kill —
all of which `smoke.py` already does.

### Tests
`tests/test_smoke.py` — extend with a real fixture Flask app in `tmp_path`: probe passes on
a correct app, fails on one whose POST handler never persists. Offline, real subprocess.

---

## Phase 6 — Demo surface (1 day)

Small, high-visibility.

- **`/run`** — start the generated app in the background, print
  `http://127.0.0.1:5000`, keep it running across turns, restart on request. Reuse
  `smoke.py`'s process management.
- **`/spec`** — Phase 2's spec dump. Show it after each turn during the demo; it is the
  visual proof of memory.
- **`/plan`** — **already exists** (`app/cli/commands.py:112`, backed by
  `AgentCore.get_plan` / `split_tasks`). Extend it, don't add it: when a spec is loaded,
  show the amendment delta and the impact-analysis edit set before executing. Showing
  "these 4 existing files will be updated, and here's why" *before* it happens is a
  stronger demo beat than showing it afterwards.
- **README in every generated project** — run steps, deploy steps (`gunicorn`, Render,
  Railway), and the current entity/route list generated from the spec.

---

## Phase 7 — Evals that mirror the demo (1 day)

Do not walk into the demo without measuring it. Add `WEBAPP_TASKS` to `evals/tasks.py` and
a `--webapp` flag to `evals/run.py`, mirroring the existing `--blueprint` wiring.

**Multi-turn tasks — this needs a real harness change.** `EvalTask` has a single
`prompt: str` field (`evals/harness.py:19-21`) and `run_task` makes exactly one
`agent.chat(task.prompt)` call (line 81) in a fresh isolated workdir. Add
`prompts: list[str] | None = None` alongside `prompt` (keeping `prompt` working so all
~14 existing tasks are untouched), and have `run_task` loop the list against **one**
workdir with **one** `AgentCore`, running the checks only after the last turn. The shared
agent matters: a fresh one per turn would reload the spec from disk and mask exactly the
in-memory staleness bugs this suite exists to catch.

1. `build me an e-commerce site for books` → app starts, `/` returns 200, products table
   exists
2. `+ add an admin page to add a product with a picture` → `image_path` column exists,
   form has `enctype`, POST persists, image renders on `/`
3. `+ add a shopping cart` → cart routes exist, turn-1 pages **still return 200**
4. `+ let customers search by title` → search returns filtered results

New checks in `evals/checks.py`: `spec_has_entity`, `spec_has_endpoint`,
`db_has_column`, `post_persists`, `earlier_pages_still_work`.

That last one — **`earlier_pages_still_work`** — is the regression check that quantifies
the faculty's actual complaint. Track it as your headline number.

Run the full four-turn suite ~5× before demo day. Per `CLAUDE.md`, the planner runs at
temperature 0.2, so a single green run proves nothing.

---

## Order, effort, and what to cut

| Phase | Days | Cut if short on time? |
|---|---|---|
| 0 — flip flags, install Flask | 0.5 | **Never** |
| 1 — deterministic scaffold | 2 | **Never** — everything downstream assumes the layout |
| 2 — ProjectSpec | 3 | **Never** — this is the faculty's core complaint |
| 3 — amendment + impact analysis | 4 | **Never** — this is the demo |
| 4 — CRUD / upload / auth | 3 | Trim to 4a + 4b + 4d. Auth (4c) is the safest cut |
| 5 — functional probe | 2 | Trim to steps 1–2; keep step 3 if at all possible |
| 6 — `/run`, `/spec` | 1 | Keep `/spec`, cut `/plan` |
| 7 — multi-turn evals | 1 | Cut the 4th turn, never the suite |

**~16 days.** Phases 0→3 alone (9.5 days) already answer every objection the faculty
raised; 4–7 are what make it hold up live.

---

## Demo-day insurance

- Run the full four-turn eval suite the morning of, in a clean directory.
- Pre-warm Ollama (`ollama run qwen2.5-coder:7b ""`) so the first turn isn't a model load.
- Start with `--yolo` so the approval gate never blocks mid-demo.
- Have `/spec` and `/run` on screen. Memory and a live URL are the two things being judged.
- Keep `git init` + a commit after each turn — `git diff` between turns is the most
  convincing possible evidence that turn 3 went back and edited turn 1's files.
- Know the fallback: `runtime_probe` drops to `STDLIB_STACK` if Flask is missing. Verify
  `.venv\Scripts\python.exe -c "import flask; print(flask.__version__)"` on the demo
  machine. (Don't check for gunicorn — it doesn't run on Windows and isn't installed
  locally; it only ships in the generated app's `requirements.txt`.)
- **Test with the network actually off**, at least once, end to end. It is the only way to
  catch a Google Fonts or CDN link that slipped through the Phase 1 guard, and an unstyled
  page in front of the faculty is the worst-looking failure available to you.
- If a generated file comes out wrong live, that is *fine* — say so, and let the
  verify/intent/smoke repair loop fix it on screen. A visible self-repair is a feature, and
  it is one most coding agents cannot show.

---

## Design rules to hold to (these are why the existing code works)

1. **Deterministic beats generated.** Every rule that can be code — scaffold, migrations,
   CRUD, impact analysis — must be code. The LLM writes only the domain layer.
2. **Pure modules, LLM calls in `core.py`.** `blueprint.py` and `buildspec.py` make zero
   LLM calls; the caller does. Keep `projectspec.py`, `impact.py`, and `crud.py` pure so
   they unit-test fully offline.
3. **Every new stage is flag-gated and defaults to inert.** That is how the blueprint work
   shipped without destabilizing anything.
4. **Best-effort never fails a turn.** A spec that won't save, an impact rule that throws —
   log it, keep the files that were written.
5. **Tests stay offline.** `ScriptedLLM`, monkeypatched embeddings, `tmp_path`. No test may
   reach Ollama.
6. **Never claim a pass you didn't get.** The existing `may not meet:` reporting is the
   right pattern; extend it to amendments and functional probes.
