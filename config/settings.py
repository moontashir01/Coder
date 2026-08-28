import os
from pathlib import Path

from pydantic_settings import BaseSettings

# Install base: the directory that ships with Coder's bundled resources.
# Bundled prompts/skills/default-MCP-config now live INSIDE the `app` package
# (app/resources/, Step 13 / D1) so they install as package data and a
# `pipx`/wheel install ships them — no reliance on the repo layout. Resolved
# once, independent of cwd, so a globally-installed `coder` finds its own
# resources from any project folder. Order of precedence:
#   1. $CODER_HOME (explicit override — expects <home>/app/resources/…)
#   2. the source tree / site-packages — this file lives at <base>/config/settings.py,
#      so <base>/app/resources holds the data in both editable and wheel installs.
# NB: runtime STATE paths below (chroma/sqlite/symbols/backups/history) stay
# relative so they land in the *current* project folder, per-project.
_BASE = Path(os.environ.get("CODER_HOME") or Path(__file__).resolve().parent.parent)
_RESOURCES = _BASE / "app" / "resources"


class Settings(BaseSettings):
    # Model config
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5-coder:7b"
    embedding_model: str = "nomic-embed-text"
    # 7B is meaningfully slower than the old 3B default on the same hardware;
    # keep a generous per-request timeout so longer generations aren't cut off.
    llm_request_timeout_seconds: int = 120

    # Vision pipeline (app/agent/vision.py): an @-referenced IMAGE is handed to
    # this model, which describes it in structured text; that text then feeds
    # the normal code-generation flow. The user never talks to it directly and
    # the coding model never sees an image. Only one 7B model fits in 8 GB of
    # VRAM, so Ollama swaps models between the vision call and generation —
    # a few seconds, and the reason vision_num_ctx stays small (the description
    # is short). vision_enabled=False is the kill switch: image refs are then
    # skipped exactly like an unreadable file. Override via .env
    # (VISION_MODEL=llava:7b) without touching code.
    # NB the Ollama tag has no hyphen in "qwen2.5vl" — `qwen2.5-vl:7b` does not
    # resolve and every vision call would degrade to text-only.
    vision_model: str = "qwen2.5vl:7b"
    vision_enabled: bool = True
    vision_num_ctx: int = 4096
    image_extensions: list[str] = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]
    # Refuse to base64 an image bigger than this (encoding inflates it ~33%);
    # beyond it something is wrong and the ref is skipped with a warning.
    max_image_bytes: int = 20_000_000
    # Downscale an image so its LONGEST edge is at most this many pixels before
    # sending it to the VL model. A byte cap does NOT bound resolution, and
    # Qwen2.5-VL tokenizes at ~native resolution while Ollama SILENTLY truncates
    # the prompt to vision_num_ctx — so a tall/high-res screenshot loses its
    # lower half with no error and the model describes only what survived. 1536px
    # keeps the image comfortably under the 4096-token window with room for the
    # prompt and the description. 0 disables downscaling. See app/agent/vision.py.
    max_image_dimension: int = 1536

    # Bundled-resource paths — anchored to the package's resources dir (see
    # _RESOURCES above) so they resolve identically from any working directory
    # and ship as package data in a wheel/pipx install.
    skills_dir: Path = _RESOURCES / "skills"
    prompts_dir: Path = _RESOURCES / "prompts"
    mcp_config: Path = _RESOURCES / "mcp_servers.json"
    # Runnable project skeletons copied verbatim before any generation
    # (app/agent/scaffold.py, docs/fullstack-web-plan.md Phase 1). Same rule as
    # the paths above: resolved from the package, NEVER from cwd, so a
    # pipx/wheel install ships them. pyproject's package-data glob
    # (resources/**/*) already picks this tree up — no packaging change needed.
    scaffolds_dir: Path = _RESOURCES / "scaffolds"

    # Per-project paths — relative on purpose (resolved against cwd) so state
    # is created in whatever project folder `coder` is launched from.
    project_root: Path = Path(".")
    projects_dir: Path = Path("projects")
    models_dir: Path = Path("models")
    chroma_persist_dir: Path = Path(".chroma_db")
    sqlite_path: Path = Path(".coder.db")
    symbols_path: Path = Path(".symbols.db")
    # Persistent embedding cache (Step 2 / P2): SHA-256(text) -> vector, so
    # embeddings survive restarts. Relative, per-project (resolved against cwd).
    embed_cache_dir: Path = Path(".coder_embed_cache")
    # LRU bound on the on-disk embedding cache (number of cached vectors).
    max_embed_cache_entries: int = 10000
    # Indexer caps (Step 3 / P4, C4): skip files bigger than this when
    # indexing; read_file truncates its output at the same ceiling.
    max_index_file_bytes: int = 1_000_000
    max_read_file_bytes: int = 1_000_000
    # Tool-output caps (app/tools/filesystem.py): the tool loop pastes every
    # result into the conversation history for the rest of the turn, and the
    # old arrangement — return EVERYTHING, then `_truncate_context` keeps the
    # first 2000 chars — kept whatever the sorted walk found first, which is
    # the alphabetically earliest folder, not the relevant hit. search_files
    # now RANKS matches (definition lines and filename hits first) and returns
    # the top `max_search_matches`, NAMING how many were dropped so the model
    # narrows the pattern instead of guessing; recursive list_directory skips
    # vendored/hidden dirs (the indexer's own rule) and both listings cap at
    # `max_list_entries` with the remainder counted, never silent.
    max_search_matches: int = 8
    max_list_entries: int = 200

    # Agent config
    # qwen2.5-coder:7b handles a larger context window than the old 3B default;
    # raised from 4096 accordingly.
    max_context_tokens: int = 8192
    # Ollama's own context window (num_ctx). It defaults to 4096 server-side and
    # is NOT derived from max_context_tokens — leaving it unset meant we budgeted
    # 8192 prompt tokens into a 4096 window and Ollama silently dropped the
    # overflow, evicting exactly the sibling-file context that keeps a multi-page
    # build consistent. Must exceed max_context_tokens (the PROMPT budget) with
    # room left for the generated file. Costs KV-cache memory per request; lower
    # it if a machine is tight on RAM/VRAM.
    llm_num_ctx: int = 16384
    max_tool_retries: int = 3
    max_tool_failures: int = 2  # §11: give up a tool after this many failures
    # M4: how many tool-call rounds the native tool loop may take before it
    # stops. Raised from the old hard-coded 8 so genuinely multi-part work has
    # room to finish every step.
    max_tool_steps: int = 12
    # The model's own per-turn task list (app/agent/todos.py): an `update_todos`
    # builtin plus the list restated after every tool round, so a long tool-loop
    # turn does not forget its remaining steps. Zero extra LLM calls; False
    # skips registering the tool and every prompt reads as before.
    todo_tool: bool = True
    # M1: split a compound request ("do A, then B, and C") into ordered
    # sub-tasks and route each one, instead of only handling the first. When the
    # cheap regex splitter sees a single task but the request still reads as
    # multi-part (natural language, no explicit "then"/"also"), the LLM planner
    # decomposes it. max_plan_tasks caps how many sub-tasks one turn will run.
    decompose_multitask: bool = True
    max_plan_tasks: int = 10
    # Per-project instructions the USER writes (`<project>/.coder/INSTRUCTIONS.md`,
    # app/agent/instructions.py): conventions that must hold on every turn in this
    # project and are derivable neither from the code (so the ProjectSpec cannot
    # hold them) nor from keywords (so a skill is the wrong shape). Loaded once by
    # load_project and stated in the prompt. The cap matters: this text is
    # prepended to EVERY prompt in the project, so an unbounded file would evict
    # the sibling/RAG context the answer depends on — and a truncation is stated
    # in the block rather than silently applied. Set false to ignore the file.
    project_instructions: bool = True
    max_instructions_chars: int = 4000
    # Surgical editing (app/agent/patch.py). How much of an existing file is
    # shown to the SEARCH/REPLACE editor. This was 6000 and is NOT a context
    # limit — llm_num_ctx is 16384 tokens, roughly 65k characters — so the only
    # thing the old number bought was a guaranteed miss on every edit whose
    # target sat past it, and a guaranteed miss falls through to the whole-file
    # rewrite, which is the behaviour this whole path exists to avoid.
    max_edit_context_chars: int = 24000
    # The largest file a whole-file REWRITE may regenerate. Above it the rewrite
    # is REFUSED rather than run against a truncated copy of the file: the old
    # code pasted `full_existing[:4000]` under the words "return the COMPLETE
    # updated file", so any larger file came back cut off and was written that
    # way. A rewrite that cannot see the whole file must not run.
    max_rewrite_chars: int = 24000
    # Reject a write that keeps less than this fraction of a file's bytes unless
    # the request asked for a deletion — a truncation parses perfectly, so no
    # other stage can see it. Files under min_chars are exempt (a three-line
    # file legitimately becomes a one-line file).
    shrink_guard: bool = True
    shrink_guard_floor: float = 0.6
    shrink_guard_min_chars: int = 400
    # `/point` — click the running page, edit the source behind it
    # (app/agent/pointer.py). How long the picker window waits for a click
    # before giving up; it is a person choosing, so this is generous.
    point_timeout: int = 180
    # Verify-and-repair: how many LLM repair passes to run when a just-written
    # file fails its syntax/structure check.
    max_repair_attempts: int = 2
    # Intent check (app/agent/intent.py): after a file passes its SYNTAX check,
    # spend one more LLM call asking "does this file do what the user asked
    # for?" — the only point in the write path that sees the request and the
    # result together. Complaints are filtered deterministically before any
    # rewrite, and a rewrite that breaks the syntax check is reverted, so the
    # worst case is a wasted call. Costs one call per file written (two if it
    # repairs); set false to restore the syntax-only behaviour.
    check_intent: bool = True
    max_intent_repairs: int = 1
    # Cross-file reference repair: after a turn that wrote files, find local
    # references (<script src>, <link href>, CSS @import/url(), JS relative
    # imports) pointing at files that don't exist and create the missing TEXT
    # files so the build actually resolves (weaknesses.md #2/#3). Missing binary
    # assets (images/fonts) are reported, never fabricated. max_reference_repairs
    # caps how many files one turn will auto-create.
    check_references: bool = True
    max_reference_repairs: int = 10
    # TOTAL characters of already-written sibling files threaded into the next
    # step of a multi-file build. Previously capped per file, so the prompt grew
    # linearly with the page count and pushed the earliest pages out of the
    # context window. The shared nav block is lifted out and stated once within
    # this same budget.
    max_sibling_context_chars: int = 6000
    # Multi-file builds: before planning the file list, spend ONE extra LLM call
    # distilling the requirements every file shares (the navigation the request
    # dictated, concrete fonts/colours for the style words it used) so each
    # per-file call stops re-interpreting them — see app/agent/buildspec.py. The
    # call only happens when the request plausibly states something shared, and
    # what it returns is filtered against the user's own words.
    extract_build_spec: bool = True
    # Requirements Blueprint (app/agent/blueprint.py, docs/requirements-blueprint.md):
    # for a greenfield BUILD request ("build me a login page"), spend ONE extra
    # LLM call up front to infer the WHOLE build — the implied features (tiered
    # requested/core/optional), the full file list incl. a backend, and an API
    # contract that makes the files line up — then hand that to _multi_file_flow.
    # ON by default since the full-stack-web work (docs/fullstack-web-plan.md
    # Phase 0): its eval suite holds 4/4 on qwen2.5-coder:7b, and every later
    # phase (scaffold, ProjectSpec, amendments) reads the blueprint. It still
    # deliberately *infers* work the user didn't spell out — the opposite of the
    # rest of the pipeline — so set it False to get the literal-request behaviour
    # back. Only fires when should_blueprint() matches (build verb + app/artifact
    # noun, not a question/edit/split).
    # NB tests default it OFF again (conftest.py::_no_blueprint): a bare
    # "create an index.html" fixture matches should_blueprint(), and the stage
    # would reach a real Ollama. Tests that want it opt back in explicitly.
    expand_requirements: bool = True
    # Which backend stack a build targets (docs/always-fullstack-plan.md Phase A).
    # "flask" | "node" | "fastapi" | "stdlib" | "none" force that stack; "auto"
    # restores the old probe-and-pick behaviour. Leaving it on "auto" made the
    # full-stack promise depend on a framework happening to be importable, which
    # is why it is a forced name and not a probe.
    # A forced stack that ISN'T installed is reported (Stack.runnable=False +
    # install_hint), never swapped for another one — see runtime_probe.py.
    # **This is a SESSION DEFAULT, and it loses to a project that remembers its
    # own stack** (docs/node-stack-plan.md N1, `stacks.resolve_key`). Reading it
    # first would send an amendment to a Node project down the Flask path, which
    # writes Python `ensure_column` calls into a db.py that does not exist.
    # `/stack` shows and switches it, and says so when a project overrides it.
    #
    # Default "node" (Express + EJS + PostgreSQL) since 2026-08-04, by request.
    # **It is the SHALLOWER of the two stacks and that has not changed** — the
    # real, current gaps are in `NodeAdapter.gaps` and `/stack` prints them
    # verbatim: no import repair, no template-scoped editing, an import
    # dependency graph that stays Python-only, and routes read by regex rather
    # than by a parser. Two environment facts come with it, both of which the
    # Flask default did not have: a generated project cannot RUN until
    # `npm install` has used the network once (Coder will not do that for you),
    # and it needs a live PostgreSQL whose database exists — `NodeAdapter.
    # database_reason` names that with the `createdb` command instead of letting
    # it arrive as a failed smoke test. Set `WEB_STACK=flask` in `.env`, or run
    # `/stack flask`, to go back.
    web_stack: str = "node"
    # Phase C: spend ONE extra temperature-0 call deciding what the app STORES
    # before planning what it looks like, so the layout is derived from a schema
    # rather than invented alongside it (app/resources/prompts/schema.md). Every
    # entity then gets a list page, a create form and their routes
    # deterministically (blueprint.derive_pages_from_entities). Off = the schema
    # arrives as free text inside the blueprint's own answer, exactly as before.
    schema_first: bool = True
    # A `@`-referenced prose document (`.md`/`.txt`/`.rst`/…) on a BUILD request
    # is a requirements document, not a file to edit: "build the site described
    # in @PRD.md". Before this, the blueprint stages saw only the one-line
    # request — the schema call, the layout call and every per-file generation
    # were planned from "build the website described in PRD.md" and the document
    # itself was read, then dropped. This is the budget for how much of it is
    # quoted into those calls; what does not fit is reported as truncated, never
    # silently cut. 0 disables the whole feature (the pre-Phase-R behaviour).
    max_spec_doc_chars: int = 16000
    # ...and the tighter budget for threading it into EVERY per-file generation,
    # where it sits on top of the contract, the scaffold block, the UI block, the
    # plan manifest and the siblings. An overflowing prompt evicts the siblings,
    # which is `_sibling_context`'s "every page has a different navbar" bug
    # arriving by a new road — and the contract those pages need was already
    # derived from this document by the planning stages.
    max_spec_doc_context_chars: int = 4000
    # Phase B: when should_blueprint()'s verb×noun regex MISSES, ask the model
    # the one thing a noun list cannot know — "is this a request to build a web
    # app?" — so "a recipe organizer" or "somewhere to track my expenses" stops
    # shipping static HTML with no server. One temperature-0, one-word call, and
    # only for messages `may_be_web_build` already judged genuine candidates, so
    # an ordinary turn costs nothing. Off = the regex is the whole gate.
    web_intent_fallback: bool = True
    # Build the 'optional' tier too (OAuth, 2FA, email delivery, …). Default off:
    # optional features are reported, not silently built.
    blueprint_optional_tier: bool = False
    # Hard cap on how many files one blueprint build will create — bounds the
    # 7B model's fan-out (cost/latency + coherence, weaknesses.md #6/#8).
    # Raised 12 -> 24 in Phase 1: the scaffold now owns ~13 boilerplate files, so
    # the GENERATED set is smaller, but a full e-commerce plan still clears 12
    # easily and was being silently truncated — `_run_blueprint` and
    # `_verify_blueprint_coverage` applied the SAME slice, so the coverage check
    # could not see the files the cap had already dropped. The slice now reports
    # what it drops (`may not meet:`), per the "never claim a pass you didn't
    # get" rule: a cap that reports is a budget, a cap that hides is a bug.
    blueprint_max_files: int = 24
    # After coverage, make ONE edit to the entry file adding the routes the
    # build's own contract and its own pages need but that generation did not
    # write. The blueprint plans 11 routes and the model's single surgical edit
    # to `app.py` lands 6 of them, so the pages the same build wrote 500 on
    # `url_for('new_category')` — measured, every live build. Coverage already
    # computes exactly what is missing and REPORTED it; this acts on that list.
    # One attempt, never a loop: repeatedly rewriting the file the whole app
    # depends on is how a working build gets churned into a broken one. The
    # edit is reverted if it breaks the file, and whatever is still missing
    # afterwards is reported rather than claimed. False = report only, which is
    # exactly the behaviour before this existed.
    wire_missing_endpoints: bool = True
    # After a blueprint build, verify the WHOLE request shipped (weaknesses.md #3):
    # every planned file exists (create the missing ones) and every declared
    # endpoint is defined in a backend file (reported if not). Inert unless a
    # blueprint actually ran this turn, so it costs nothing on ordinary turns.
    check_blueprint_coverage: bool = True
    # Phase 3: after a blueprint build, actually START the generated backend and
    # probe it — the only check that proves the server RUNS, not just parses
    # (weaknesses.md #2's last-open item). Runs with a hard timeout and kills the
    # process tree afterwards. On a startup crash it can feed the traceback back
    # for up to max_smoke_repairs regeneration passes.
    # ON by default since docs/fullstack-web-plan.md Phase 0: "it starts" is the
    # cheapest honest signal that a generated backend works, and the later phases
    # build a functional probe on top of it. It still EXECUTES model-generated
    # code — set False to disable. Inert unless a blueprint ran this turn.
    # NB tests default it OFF again (conftest.py::_no_blueprint) so no test ever
    # spawns a subprocess server.
    blueprint_smoke_test: bool = True
    smoke_test_timeout: float = 8.0
    max_smoke_repairs: int = 1
    # Phase N5 (docs/node-stack-plan.md): seconds the `SELECT 1` readiness probe
    # may take on the Node stack. It runs `node -e` against the project's own
    # `pg` and its own DATABASE_URL, so it costs a subprocess — and it is the
    # difference between "something is listening on 5432" and "this app can
    # actually reach its database". Bounded because an unreachable host can hang
    # far longer than a refused connection; on timeout the probe reports nothing
    # and the smoke test RUNS, because a check we could not complete must never
    # gate the real measurement. Flask never reaches this (sqlite has no daemon).
    db_probe_timeout: float = 6.0
    # This machine's PostgreSQL server, WITHOUT a database name — the scaffold
    # appends the project's own slug. Every generated project on the Node stack
    # ships with it as the default connection string, so the credentials are
    # stated once here rather than retyped into each project. Override per
    # machine in `.env` (POSTGRES_SERVER=…) or per deployment with the
    # DATABASE_URL environment variable, which the generated `db.js` reads
    # first. The old hard-coded `postgres://postgres:postgres@localhost:5432`
    # was nobody's real server, so a first run failed on authentication before
    # it could fail on anything worth reading.
    postgres_server: str = "postgres://postgres:admin@localhost:5432"
    # `/run` does the setup a generated project needs before it can start,
    # instead of printing the commands and leaving them to the person watching.
    # On Flask there is nothing to do; on Node it is `npm install`, `CREATE
    # DATABASE` and the seed — three commands that have to be typed in another
    # terminal, in the right order, before a single page can be opened.
    #
    # This is the second deliberate exception to the offline rule, after
    # Playwright's Chromium download, and it is the only one that ships ON. The
    # reason they differ: a missing browser costs an optional CHECK, while
    # missing `node_modules` means the app cannot run at all, so the network is
    # not an enhancement here — it is the difference between a URL and a wall of
    # instructions. Nothing about the BUILD reaches the network either way.
    #
    # What it will never do: install Node, start a PostgreSQL service, or guess
    # a password. Those need an installer or an administrator, so they stay
    # reported (`readiness`) rather than attempted. And it only creates a
    # database when PostgreSQL said `3D000` — a refused login is never answered
    # by creating something.
    #
    # `AUTO_SETUP=false` in `.env` restores the previous behaviour exactly:
    # `/run` names what is missing and changes nothing.
    auto_setup: bool = True
    # `npm install` on a cold cache fetches express, ejs and pg. Bounded because
    # a hung registry connection would otherwise hold the REPL open with no
    # output; on timeout the step is REPORTED as failed, never as done.
    npm_install_timeout: float = 300.0
    # Phase W4 (docs/web-quality-plan.md): render generated pages in a real
    # headless browser, so layout, CSS and JS can be observed at all — every
    # check before this one reads bytes (`smoke.py` is urllib), which is why a
    # table 900px wide inside a 390px viewport and a button wired to nothing
    # were both invisible.
    # OFF by default, and this default is not timidity: Playwright's Chromium
    # download needs the network ONCE, which is a real exception to the offline
    # rule. It is handled the way Phase A handles a missing Flask — a loud,
    # separate install hint (`browser.install_hint()`), never a silent skip that
    # reads as a pass. Someone who never installs it gets exactly the old
    # behaviour. Turn on with BROWSER_CHECKS=true once
    # `python -m playwright install chromium` has run.
    browser_checks: bool = False
    # Seconds for one navigation, and how long to keep collecting console/network
    # events after load. Kept well under smoke_test_timeout: these probes run
    # INSIDE the smoke test's process window (two servers would fight over :5000).
    browser_timeout: float = 8.0
    # Viewport widths every page is observed at. The narrow one is the point —
    # horizontal overflow at phone width is the single most common responsive
    # failure and cannot be seen at desktop width.
    browser_widths: list[int] = [1280, 390]
    # Cap on pages visited per turn, so an N-page site cannot turn one build into
    # dozens of navigations. Like blueprint_max_files, what it drops is reported
    # rather than silently truncated.
    browser_max_pages: int = 12
    # Phase W6: the dead-button probe reloads the page once per control, so N
    # pages x M buttons is the real fan-out. Same rule as above — what the cap
    # drops is reported, never hidden.
    browser_max_controls: int = 8
    # Phase W5/W6: how many FILES one turn may rewrite in response to what the
    # browser measured (a template that scrolls sideways, a button wired to
    # nothing). Separate from max_smoke_repairs because the target is different:
    # a layout defect is fixed in the template or the stylesheet, never in the
    # server file the smoke repair edits. 0 = report the findings, change
    # nothing.
    max_browser_repairs: int = 1
    # Phase W7: screenshot the rendered pages and ask the VISION model whether
    # they look broken. OFF by default and the least reliable stage in the
    # pipeline — a 7B VL judging a 7B's markup — so it is built to be reverted:
    # a rewrite that regresses W5's measurements is undone automatically.
    # It needs browser_checks as well (there is no screenshot without a browser),
    # costs one vision call per page per width, and swaps the loaded Ollama model.
    # NB tests default it OFF (conftest.py::_no_blueprint): it fires inside the
    # smoke stage and would reach a real Ollama.
    check_visual: bool = False
    # How many pages get screenshotted (× browser_widths vision calls each), and
    # how many files one turn may rewrite on the strength of what the model saw.
    visual_max_pages: int = 2
    max_visual_repairs: int = 1
    # Phase W9 (docs/web-quality-plan.md): generate a high-value file (a page
    # template, a Python module) more than once and keep whichever candidate
    # scores best on the DETERMINISTIC checks — parses, no dead CDN asset, every
    # url_for resolves, extends the layout, no duplicate definition. Only sound
    # because W2/W5/W6 made those checks objective; before them this would have
    # been a coin flip dressed up as a measurement.
    # DEFAULT 1 = off, and that default is the point: every request already
    # costs minutes on a 7B, so N=2 means roughly double the generation time for
    # the files it applies to. Ties go to the first candidate, so N>1 can never
    # change a build without measurably improving it.
    best_of_n: int = 1
    # Sampling temperature for the EXTRA candidates. The first is generated
    # exactly as it always was; identical samples would make the whole thing a
    # waste of a call, so the rest are drawn hotter.
    best_of_temperature: float = 0.4
    # Roles per model. Planning, schema extraction and critique are *reasoning*
    # calls, not codegen, and a general instruct model may well beat
    # qwen2.5-coder at them — this makes that question measurable instead of
    # assumed. Empty = use llm_model, i.e. exactly today's behaviour, and the
    # role LLM is then the SAME OBJECT as the general one so `/model` and test
    # patching keep working.
    planner_model: str = ""
    judge_model: str = ""
    retrieval_top_k: int = 5
    conversation_buffer_size: int = 20
    # U6: when history overflows max_context_tokens, summarize the dropped
    # oldest turns into a compact note instead of silently forgetting them.
    summarize_history: bool = True
    # T0: record what each turn DID — route, tools, files written, who asked —
    # into `turn_events`, which is what `/export` renders. Off restores the
    # pre-T0 behaviour exactly (the conversation is still stored); the test
    # suite defaults it off so a scripted turn does not append to the repo's
    # real history, and tests/test_turnlog.py opts back in.
    record_turns: bool = True
    # T1 — two front-ends on one machine (app/agent/sessions.py).
    # `cross_process_lock` is the advisory `<project>/.coder/coder.lock` that
    # stops a REPL and a `--bot-only` daemon interleaving writes into the same
    # project; off leaves only the in-process `asyncio.Lock`, which is all a
    # single process ever needed. `turn_lock_timeout` is how long to WAIT for
    # the other front-end's turn — long, because a build turn is minutes.
    # `turn_lock_stale_after` is the PID-REUSE backstop and nothing else: a
    # lock whose holder is dead is reclaimed immediately regardless of age, and
    # this only governs a lock whose pid is alive but implausibly old.
    cross_process_lock: bool = True
    turn_lock_timeout: float = 900.0
    turn_lock_stale_after: float = 3600.0
    # Close a project's agent (and its file watcher) after this long untouched.
    # A long-running bot would otherwise hold one watcher, one Chroma
    # collection and one AgentCore per project it ever saw. 0 disables.
    session_idle_timeout: float = 1800.0
    # T2 — the Telegram front-end (app/bot/). OFF by default: it is the one
    # dependency that talks to the network, and it carries the conversation
    # (including file contents quoted in a reply) off the machine. Nothing
    # about GENERATION reaches the network either way — the model, the index
    # and every file operation stay local.
    telegram_enabled: bool = False
    # Never hardcode this and never print it: set TELEGRAM_TOKEN in .env.
    telegram_token: str = ""
    # Numeric Telegram user ids, never @usernames — a username is reassignable,
    # so an allowlist of names is an impersonation surface. EMPTY MEANS NOBODY:
    # an unconfigured bot refuses everyone, including its owner.
    telegram_allowed_users: list[int] = []
    telegram_poll_timeout: float = 30.0
    # How often the streaming message is edited. Telegram rate-limits edits to
    # one message, and a stream produces hundreds of tokens a second.
    telegram_edit_interval: float = 1.5
    # An approval nobody answers is a DENY, not a stall — but the question has
    # to stay open long enough to reach a phone.
    telegram_approval_timeout: float = 120.0
    telegram_max_concurrent_turns: int = 2
    telegram_rate_burst: int = 5
    telegram_rate_seconds: float = 60.0
    telegram_max_tool_lines: int = 20
    # T3 — pairing and audit. A code is minted on the machine (`/bot pair`),
    # stored HASHED, single-use, and short-lived: it is a live grant until it is
    # redeemed. The audit log is per project (relative paths resolve against the
    # project root) and lives in `.coder/`, which the indexer already skips.
    telegram_pairing_ttl: float = 300.0
    bot_audit_log: Path = Path(".coder/bot_audit.log")

    # Safety
    # Safe writes (Tier 3 #8): mutating file tools back up the previous
    # content here first; undo_write restores the most recent backup.
    backups_dir: Path = Path(".coder_backups")
    max_write_backups: int = 20
    # Permission gating (Tier 3 #8): the Executor refuses any tool whose
    # ToolDefinition.permissions intersects this list. Tags in use:
    # fs:read, fs:write, fs:delete, shell, git:read, git:write, mcp.
    denied_permissions: list[str] = []

    # Path jail (Step 5 / S2): file tools reject paths that resolve outside
    # sandbox_root. None disables the jail (tests / library use); main.py sets
    # it to cwd at startup and load_project narrows it to the project dir.
    # allow_outside_root (or the --allow-outside-root flag) turns it off.
    sandbox_root: Path | None = None
    allow_outside_root: bool = False

    # Human-in-the-loop approval (Step 6 / S3, S6): the Executor consults an
    # approval hook before running any tool whose permissions intersect
    # approval_gated_permissions. auto_approve (--yolo) skips the gate;
    # safe_mode (--safe) denies safe_deny_permissions when there is no
    # interactive approver (e.g. a non-TTY run). No hook + not safe = allow,
    # so tests and piped/eval runs never block.
    auto_approve: bool = False
    safe_mode: bool = False
    approval_gated_permissions: list[str] = ["fs:write", "fs:delete", "shell"]
    safe_deny_permissions: list[str] = ["shell", "fs:delete"]

    # Shell hardening (Step 7 / S1, S4). command_allowlist, when non-empty,
    # restricts run_command to those invoked binaries (denylist stays a
    # backstop). Network-reaching commands are refused unless allow_network
    # (--allow-network); network_commands lists the gated binaries.
    command_allowlist: list[str] = []
    allow_network: bool = False
    network_commands: list[str] = [
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "rsync",
    ]
    allowed_commands: list[str] = [
        "python",
        "pip",
        "npm",
        "node",
        "git",
        "ls",
        "cat",
        "echo",
        "mkdir",
        "touch",
        "cp",
        "mv",
    ]
    blocked_commands: list[str] = [
        "rm -rf /",
        "sudo rm",
        "format",
        "mkfs",
        "dd if=/dev/zero",
    ]
    command_timeout_seconds: int = 30

    model_config = {"env_file": ".env"}


settings = Settings()
