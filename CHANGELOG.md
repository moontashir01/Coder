# Changelog

All notable changes to Coder are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **A second stack: Node + Express + EJS + PostgreSQL** (`docs/node-stack-plan.md`,
  phases N0–N2). `/stack` shows which stack a build targets and switches it;
  `/stack node` scaffolds a runnable Express app — routes, a connection pool,
  the same component stylesheet and theme file the Flask stack ships, and a
  layout every view is wrapped by — before a single line is generated. The
  choice is remembered per project in `.coder/project.json`, and **a project's
  own stack always beats the session default**, so opening a Node project on
  turn 2 can no longer hand it Flask's Python migrations.
  The data layer is generated on Node too, not prompted for: the tables,
  `models.js`, `seed.js` and the password hashing come from the project's own
  schema, with `$1` parameters and `RETURNING id`, and every entity gets a list
  page and a create form. Both stacks emit from the same schema, so one project
  description cannot produce two different databases.
  `/stack` prints each stack's gaps as well as its guarantees, because they are
  still not equals: Flask keeps `url_for` validation, import repair and
  block-scoped template editing that Node does not have yet. Flask stays the
  default. When Node, `npm install` or PostgreSQL is missing the smoke test is
  **skipped and reported**, never quietly passed.
- **Generated sites now come with a design system, and Coder can look at them**
  (`docs/web-quality-plan.md`). Every build ships a real component stylesheet and
  a Jinja macro library, and the style you ask for is *written* into
  `static/css/theme.css` rather than described to the model and hoped for — so
  the table on page one and the table on page three are the same table. A
  misnamed `url_for` is repaired before it can 500 a page, and an edit to a page
  now goes into that page's `{% block content %}` instead of across the whole
  file, so it can no longer delete the layout it inherits from.
  With a headless browser installed (`pip install playwright && python -m
  playwright install chromium`, then `BROWSER_CHECKS=true`) Coder also *renders*
  what it built: it reports pages that scroll sideways on a phone, JavaScript
  errors, assets that 404, text below the AA contrast line, and buttons wired to
  nothing — then fixes the file that owns the problem and re-measures, undoing
  the fix if the page got worse. `CHECK_VISUAL=true` adds a screenshot critique
  from the vision model on top. All of it is off by default and reported as
  skipped when it cannot run, so a machine without a browser behaves exactly as
  before. `BEST_OF_N=2` generates a page or module twice and keeps whichever
  candidate the checks prefer, at roughly double the generation time.
- **Project memory that can route an edit** — the spec now records what each
  file *defines* (its routes and handlers) and which data it shows, not just
  "this is a page". So "update the products page" finds `templates/products.html`
  by name instead of falling back to whatever file was written last, and the
  project's tables and routes are now in front of the model on ordinary turns
  too, not only when a request happens to look like an amendment. Memory also
  keeps up with edits made outside the amendment flow: a route added by a plain
  file edit is recorded, so the next change plans against what is really there.
  Existing `project.json` files keep working unchanged.
- **Any website request now gets a full-stack build** — the build gate was a
  keyword regex, so "build me a recipe organizer" or "I need somewhere to track
  my expenses" fell through to a static HTML page with no server and no
  database. When the keywords miss, Coder now asks the model one yes/no question
  instead of guessing from a word list. Ordinary turns are unaffected (the
  question is only asked for plausible candidates, and anything but a clear yes
  leaves routing alone). Two long-standing misfires are fixed with it: "build a
  shop and add reviews to it" was read as an edit rather than a build, and
  "build me a website with a css file for the styling" was read as a
  single-file request. Say "just html", "no backend" or "static only" to get a
  frontend-only build; `WEB_INTENT_FALLBACK=false` restores the regex-only gate.
- **Schema-first builds** — a build request now decides what the app *stores*
  before it decides what it looks like, in its own short call, and the pages are
  planned around that schema instead of invented alongside it. Every table then
  gets a page that lists it, a form that adds to it, and the routes behind both
  — deterministically, so a four-table request can't come back with pages for
  two. Uploaded-image columns keep working (`IMAGE` becomes a `_path` column, so
  the upload wiring still fires). `SCHEMA_FIRST=false` restores the old
  behaviour, where the schema arrived as free text inside the build plan.
- **Project memory for projects Coder didn't build** — an existing Flask project
  (cloned from git, built before project memory existed, or with its
  `.coder/project.json` deleted) now has its contract read off the files on the
  first turn: tables from real `CREATE TABLE`s, routes from real `@app.route`
  decorators, pages from the templates those routes render. So "add a reviews
  column to products" works on a repo Coder has never seen, instead of being
  treated as an unknown folder. It declines rather than guessing when the
  project defines no routes, records only what is actually there, and writes
  nothing — the first amendment is what persists the spec.
  `README.md` is no longer overwritten unless Coder wrote it.
- **Forced backend stack (`WEB_STACK`, default `flask`)** — builds now target
  Flask + Jinja2 + sqlite3 by decision rather than by probe. Previously
  `detect_stack()` picked the richest importable framework, so "Coder builds
  full-stack web apps" quietly depended on Flask happening to be installed.
  `WEB_STACK=auto` restores the old probing; `stdlib`/`fastapi`/`none` force
  those instead. **A forced stack that isn't installed is reported, never
  swapped** — the build still produces a Flask project, the answer leads with
  `pip install flask`, and the smoke test is skipped rather than blaming the
  generated code for a missing package. Flask is now an explicit dependency in
  `pyproject.toml` (it is the generated apps' runtime, not a Coder import).
- **Screenshot-to-code** — `@`-reference an image (`build a website like this
  @mockup.png`) and a local vision model (`qwen2.5vl:7b`) describes it into
  structured text that feeds the normal code generation. No new command or
  model switch, and no image ever reaches the coding model. Needs
  `ollama pull qwen2.5vl:7b`; if the model is missing or the call fails, the
  request falls back to text-only instead of failing. `VISION_ENABLED=false`
  turns it off, `VISION_MODEL=` swaps the model. Oversized screenshots are
  downscaled (long edge capped at `MAX_IMAGE_DIMENSION`, 1536px) before they
  reach the model, so a high-res image isn't silently truncated to the vision
  context window and half-described; the resize is best-effort (falls back to
  the original bytes) and needs Pillow.
- **Streaming file generation** — creating/rewriting a single file now streams
  the model's tokens live (previously only plain answers streamed).
- **`/model` command** — show or switch the Ollama model at runtime (rebuilds
  the agent + planner LLMs); larger models like `qwen2.5-coder:14b`/`:32b` work.
- **`coder init` / `coder config`** — write a `.env` template with first-run
  steps, and show or set individual settings.
- **Auto-load the current directory** as the project on startup (`--no-index`
  opts out).
- **`coder --update`** — pull the latest source and reinstall in place
  (`--dry-run` prints the actions first). Non-git installs are pointed at
  `pipx upgrade coder`.
- **`pipx` install support** — prompts, skills, and the default MCP config now
  ship as package data under `app/resources/`, so a non-editable install works.
- **Live auto-reindex** — a debounced watchdog observer keeps RAG/symbol
  retrieval fresh when files change on disk.
- **Multi-language symbol index** — `find_symbol` / `find_references` now cover
  JS/TS/JSX/TSX/Go/Rust/Java/C/C++ via tree-sitter (Python stays on stdlib `ast`).
- **Security profile & flags** — project-root path jail for file tools, an
  interactive approval gate for writes/deletes/shell (`--yolo`, `--safe`),
  and a shell allowlist + network gate (`--allow-network`,
  `--allow-outside-root`).
- **Prompt-injection framing** — retrieved file/tool content is fenced as
  untrusted data the model must not treat as instructions.

### Changed
- **Conversation summarization** — when history overflows the token budget, the
  dropped oldest turns are summarized into the prompt instead of being silently
  forgotten (`summarize_history`, on by default).
- **Faster loads** — incremental, content-hash indexing skips unchanged files
  and a persistent on-disk embedding cache survives restarts; the indexer
  honors `.gitignore` and size/binary caps.
- **Per-project backups** — safe-write snapshots and `/undo` are scoped to the
  active project.
- **No import-time side effects** — the ChromaDB client, symbol index,
  retriever, and tool registry are built lazily; importing the package no longer
  writes state to disk.
- Best-effort failures now log via module loggers instead of being swallowed.

## [0.1.0]

- Initial offline AI coding assistant: local Ollama LLM + embeddings, ChromaDB
  RAG, SQLite memory, tree-sitter chunking, MCP servers, skills, and a Rich REPL.
