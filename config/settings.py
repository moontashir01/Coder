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
    # M1: split a compound request ("do A, then B, and C") into ordered
    # sub-tasks and route each one, instead of only handling the first. When the
    # cheap regex splitter sees a single task but the request still reads as
    # multi-part (natural language, no explicit "then"/"also"), the LLM planner
    # decomposes it. max_plan_tasks caps how many sub-tasks one turn will run.
    decompose_multitask: bool = True
    max_plan_tasks: int = 10
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
    # restores the old probe-and-pick behaviour. Default "flask" because the whole
    # full-stack direction promises Flask + Jinja2 + sqlite3 — leaving it on
    # "auto" made the promise depend on Flask happening to be importable, which
    # it is here only because Coder's own environment installs it.
    # A forced stack that ISN'T installed is reported (Stack.runnable=False +
    # install_hint), never swapped for another one — see runtime_probe.py.
    # **This is a SESSION DEFAULT, and it loses to a project that remembers its
    # own stack** (docs/node-stack-plan.md N1, `stacks.resolve_key`). Reading it
    # first would send an amendment to a Node project down the Flask path, which
    # writes Python `ensure_column` calls into a db.py that does not exist.
    # `/stack` shows and switches it, and says so when a project overrides it.
    # "node" is Express + EJS + PostgreSQL and is shallower than Flask on
    # purpose — `/stack` prints each stack's gaps.
    web_stack: str = "flask"
    # Phase C: spend ONE extra temperature-0 call deciding what the app STORES
    # before planning what it looks like, so the layout is derived from a schema
    # rather than invented alongside it (app/resources/prompts/schema.md). Every
    # entity then gets a list page, a create form and their routes
    # deterministically (blueprint.derive_pages_from_entities). Off = the schema
    # arrives as free text inside the blueprint's own answer, exactly as before.
    schema_first: bool = True
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
