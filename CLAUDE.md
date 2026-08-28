# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Coder** — a fully **offline** AI coding assistant. It talks only to a local Ollama
(`http://localhost:11434`); nothing leaves the machine. `qwen2.5-coder:7b` is the default LLM,
`nomic-embed-text` the only embedding model. Primary interface is a CLI/REPL. ChromaDB for
vectors, SQLite for memory, LangChain for the Ollama wrappers.

## Prerequisites

Ollama running with the models pulled:
```
ollama serve
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
ollama pull qwen2.5vl:7b        # optional — only for @screenshot.png refs
```
Optional extras, both of which reach the network (once, to install) and neither of which the
build itself ever uses:
```
pip install -e ".[browser]" && python -m playwright install chromium   # W4-W7 checks + /point
pip install -e ".[telegram]"                                          # the Telegram front-end
```
All Python work uses the venv (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Unix).

## Common Commands

```bash
python main.py                          # start the REPL
python main.py --project /path/to/proj  # load + index a project on startup
python main.py --session work           # named conversation session (persists in SQLite)
pip install -e .                        # installable CLI: `coder` == `python main.py`
coder --version                         # works without Ollama (eager typer callback)

pytest tests/ -v                        # all tests (~9 min, fully offline — no Ollama needed)
                                        #   NB stop `ollama serve` first — see the timing gotcha
pytest tests/test_tools.py -v           # one file
pytest tests/test_agent.py -v -k executor   # one test by name

black app/ tests/ main.py               # format
isort app/ tests/ main.py               # import order
```

## Architecture

### Control flow — `AgentCore.chat()` routes by task type

The single most important thing to understand: `chat()` ([app/agent/core.py](app/agent/core.py))
does **not** run the tool loop for every message. It calls `Planner.classify()` first, then
routes:

```
chat(msg)
  ├─ _update_skills_context(msg)             # match + inject skills
  ├─ memory.add_human(msg)
  ├─ task_type = planner.classify(msg)       # 1 LLM call → simple_qa | explanation
  │                                          #   code_generation | file_edit | multi_step
  ├─ if wants_multifile(msg):
  │      _multi_file_flow(msg)               # plan JSON → per-file _file_op_flow
  │  elif _wants_file_op(msg) or task_type=="file_edit":
  │      _file_op_flow(msg)                  # DETERMINISTIC: gen full file → write_file
  │  elif task_type=="multi_step" and project loaded:
  │      _build_messages() → _run_tool_loop()   # native tool-calling loop
  │  else:
  │      _direct_answer()                    # one plain LLM call, NO tool protocol
  └─ memory.add_ai(answer)
```

Every successful write in `_file_op_flow` / `_surgical_edit` then runs
**`_verify_and_repair`**, which is two stages: `_syntax_repair` (does it parse / is it the right
kind of content) and then `_intent_repair` (is it what the user asked for). Both notes are joined
into the answer line: `verified OK; intent OK`.

**Stage 1 — `_syntax_repair`.** `app/agent/verify.py:check_file()` checks the file two ways — a **syntax**
check (`.py` in-process `compile()`, `.js` `node --check`, `.ts` `tsc --noEmit`, `.html`/`.htm`
tag-balance parser) **and** a tooling-free **content guard** that catches the *wrong kind* of
content the local model sometimes emits: an HTML document dumped into a `.js`/`.ts`/`.css` file,
plain prose left in a code/style file, or prose leaking before `<!doctype>` / after `</html>` in
HTML (the reproduced "text instead of code in the file" failure). Because the content guard needs no
external binary, `.js`/`.ts`/`.css`/`.scss`/`.less` are **always** verifiable now — a missing
node/tsc only skips the *syntax* half, not the language/prose guard. Unknown ext = unverifiable-ok,
never "broken". On failure it feeds the error back for a complete-file regeneration, capped at
`settings.max_repair_attempts`. Belt-and-suspenders: `_parse_file_output` also pre-trims stray prose
outside an HTML document (`_trim_html_prose`) before the first write, so the common trailing-prose
leak never reaches disk.

**Stage 2 — `_intent_repair` (`app/agent/intent.py`).** Everything above judges **form**. Nothing
judged **content**: a syntactically perfect contact form passed a request for a login form as
`verified OK`, and a `median()` that returns the *mean* compiled cleanly and shipped (both
reproduced live). The old repair prompt could not have caught either — it received the check error
and the file, and **never the user's message**. This stage is the only point in the write path that
sees the request and the result together. It runs after syntax (a file that doesn't parse is broken
whatever it says, and judging one wastes a call on a file about to be rewritten), spends ONE
temperature-0 call on `_llm_edit` asking for `PASS` or a `MISSING:` list, and regenerates the file
with the unmet points named. Because the judge is the same 7B model that wrote the file, four rules
stop it churning good files:
- **Unparseable verdict = PASS.** `parse_verdict` resolves every ambiguity toward leaving the file
  alone; so does an LLM error. Silence is the safe answer.
- **Complaints are filtered deterministically** before any rewrite (`filter_complaints`, no second
  call): hedged suggestions ("could also add a footer"), complaints about *other* files
  (`_repair_dead_references` owns those — a rewrite of this file cannot fix them), and complaints
  whose every content word is already in the file (the characteristic small-model false alarm: it
  skims, then reports what it just read as absent). That last gate can swallow a real complaint
  whose vocabulary happens to appear; the trade is deliberate.
- **A rewrite that breaks `check_file` is reverted.** Intent repair can leave a file unimproved but
  never leaves one broken — otherwise "add the missing feature" truncating the document would be a
  net loss. The repair prompt also forbids removing or renaming existing content.
- **It never claims a pass it didn't get:** unfixed requirements are reported as
  `may not meet: …` rather than hidden.
Gated by `settings.check_intent` (default on) + `max_intent_repairs` (1), and skipped for requests
too short to judge against ("fix it" names no requirement, so a judge given one invents them).
**Cost: one extra LLM call per file written** (two if it repairs) — that is the price of the check,
and `check_intent=False` restores the old syntax-only behaviour exactly. Live-validated: caught and
fixed a missing password field and the mean-instead-of-median function; 15/15 clean on
already-satisfied files (no false alarms).

> **`conftest.py` defaults `check_intent` OFF in tests** and `tests/test_intent.py` opts back in.
> The stage sits inside `_verify_and_repair`, so it fires on every file-writing test and calls
> `_llm_edit` — which most file-flow tests don't script. Left on, they reach a real `ChatOllama`
> and the suite silently stops being offline (measured 374s → 611s, still all "passing"). If you add
> a test that writes a file, you do not need to think about this; if you *want* the stage, set the
> setting in the test.

**Cross-file reference repair (closes the plan→verify loop, weaknesses.md #2/#3).** After a turn
that wrote any files, `chat()` runs `_repair_dead_references(trace)`: it scans every file written
this turn (HTML/CSS/JS via `app/agent/references.py`) for **local** references — `<script src>`,
`<link href>`, `<img src>`, CSS `@import`/`url()`, JS relative imports — that point at a file which
doesn't exist, and **creates each missing TEXT file** (`.css`/`.js`/`.ts`/`.html`/…) via
`_file_op_flow`, feeding the referencing file in as context so ids/classes/selectors line up.
Missing **binary** assets (`.png`/`.woff`/…) are **reported, never fabricated**. Before creating
anything it runs `_redirect_near_miss_references`: a reference that merely *misspells* a file that
already exists — `scripts.js` beside the plan's `script.js` — is a typo, not a dependency, so the
reference is rewritten to point at the real file (`rewrite_reference`, quoted values and CSS
`url()` only) instead of creating a duplicate asset. `find_similar_file` is deliberately strict
(same extension, same stem once punctuation and a trailing plural are collapsed), so `main.css` is
still a genuinely new file and is still generated.

Then `_repair_nav_consistency` makes every page written this turn carry the **same** navigation.
`_sibling_context` states the canonical nav in the prompt, but that's a hint the 7B model is free to
ignore — and does (page 3 renames an item, page 4 drops one), while the two link passes each look at
one page at a time and can't see the disagreement. This one is deterministic, no LLM: compare the
pages' `nav_signature`s (normalized so that *only* a different active item or `./about.html` vs
`about.html` compares equal), pick the canonical nav — best match for the build spec's labels, then
the one most pages already agree on, then the first written — and patch the outliers with
`replace_nav_block` + `set_active_link`, which carries the active marker over to each page's own
link. A page with **no** nav is left alone (never inject markup where the design may not want any).
A third pass,
`_repair_page_links`, then fixes links whose target **exists** but whose *form* can't reach it from a
static page opened over `file://` — `href="/about.html"` (root-absolute) and `href="about"`
(extensionless). It is purely deterministic (no LLM): the corrected target must already exist next to
the page, so a genuine route in a server-rendered app is never rewritten, and only the `href` value on
`<a>` tags is touched. External URLs,
`//cdn`, `data:`/`mailto:`/`#anchor`, root-absolute `/paths`, and bare npm import specifiers are all
ignored (no off-disk false alarms); targets that resolve outside the sandbox root are skipped. It's
bounded by `settings.max_reference_repairs`, gated by `settings.check_references` (default on),
best-effort, and restores `_last_write_path` so an auto-created dependency never hijacks the
follow-up edit target ("now add a footer" still edits the page, not the generated `script.js`). So a
build's `<script src="script.js">` no longer dangles when the model forgot to create `script.js` —
the pass creates it. NB this runs at the `chat()` seam, so it covers the single-file, multi-file,
subtask, AND tool-loop paths uniformly; tests that call `_file_op_flow`/`_multi_file_flow` directly
bypass it (unit-tested separately in `tests/test_references.py`).

**Why three paths:** the 3B model these paths were built for is unreliable at the JSON tool
protocol (see the "3B-era hardening" note below — the default is now `qwen2.5-coder:7b`). So:
- **Create/edit a single file → `_file_op_flow`** (the common case). `_wants_file_op()` is a
  verb+target regex ("make/create/edit … html/file/`*.ext`"); note `classify()` tags file
  *creation* as `code_generation`, so the regex — not the classifier — is what catches it. Files land
  in the loaded project, else **cwd**. A follow-up that names **no** file ("now add a footer to the
  page") targets the **last file the agent wrote** (`_last_write_fallback`; recorded in
  `_reindex_after_write`, which every successful write path hits) — skipped when the message asks
  for a new artifact (`_NEW_ARTIFACT_RE`: "a css file", "a new page") or the last write is
  gone/outside the workdir.
  - **A name the regex found is then RESOLVED against the project** (`_locate_named_file`).
    `_extract_filename` is a regex over the message: it recognises `users.ejs` and stops there, and
    nothing checked the project actually had one at its root — while a Node build keeps its views in
    `views/` and a Flask build its pages in `templates/`, so the name a person types is almost never
    the path. Measured: "fix the files inside users.ejs" resolved to `<root>/users.ejs`, which does
    not exist, so the flow read an empty string, took this for a NEW file, and wrote the model's
    "I need more information about the specific issues" reply to disk as a second, junk `users.ejs`
    beside the real `views/users.ejs`. **Both guards that should have caught it were gated on
    `filename is None`** — the spec lookup that matches by template stem, and the tool-loop
    escalation whose own comment describes this exact outcome — and a WRONG name is not a MISSING
    one. Three rules: **exactly one match or none** (two `index.ejs` mean the message was ambiguous,
    `_resolve_target_from_spec`'s rule); **dot-directories, `node_modules` and `__pycache__` are
    skipped**, load-bearing because `.coder_backups/` holds a copy of every file ever written and an
    unfiltered walk resolves `server.js` to a snapshot of itself; and **an unresolved name is only
    dropped for a request that CHANGES something** (`_wants_existing_file_change`) — a file being
    created is *supposed* not to exist yet, so "create theme.css" keeps its name.
  - **Create / new file:** ONE plain LLM call for `FILENAME: <name>\n<full contents>`, parsed by
    `_parse_file_output` (strips code fences, incl. stray/unmatched ones), written via `write_file`.
    Each call generates ONE file, but the model sometimes answers with the whole build —
    several `FILENAME:` blocks in one response — so `_parse_file_output` takes a `target` and keeps
    **only** the block for the file this call asked for (else the first), discarding the rest.
    Without that, every later block landed *inside* the first file: a `styles.css` with a script and
    an HTML document appended to it. This was live-caught by the eval suite (`multifile_three` lost
    its `index.html` that way).
  - **Edit existing file → surgical first (`_surgical_edit`).** Asks `_llm_edit` (temperature 0,
    few-shot, editor-only system prompt — NOT the persona, whose "confirm what you did" rule causes
    prose) for `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks. `_apply_search_replace` applies
    them through the shared ladder in `app/agent/patch.py` (below). One retry for *unparsed* output
    and one for *unmatched* blocks, then **fall back to a whole-file rewrite** if nothing lands.
    NB: with `qwen2.5-coder:3b` surgical edits fire
    reliably (~3/3 in practice); a non-code model like `qwen2.5vl:3b` rarely emits valid blocks and
    keeps falling back. The path is fully unit-tested regardless of model.
- **`@path` references** (`_extract_at_refs`): in any message, `@src/app.py` pins the edit *target*
  (`_resolve_ref`, prefers an existing file) and, for non-edit questions, injects the file as context
  (`_read_refs`). The `@` is stripped before the model/classifier see the text. Emails are ignored.
  An `@`-referenced **image** takes the vision path instead — see below.
- **Split/reorganize across several files → `_multi_file_flow`** — and in `chat()`, a request the
  cheap splitter leaves whole that matches `wants_multifile()` routes **straight** here, skipping
  the classify/decompose LLM calls (LLM pre-decomposition fragments a spec; this flow has its own
  per-file planner that must see the FULL text). Related: `_split_compound` treats Title-Case
  `"Label:"` items ("1. Search Bar: …") as spec headings, not new tasks — a numbered feature list
  stays one build. Caller `extra_context` (e.g. the sub-task manifest) threads into both the
  planning call and every per-file generation. (`wants_multifile()` regex:
  separate/split/extract… + plural "files" or ≥2 languages). One `_plan_file_ops` LLM call returns
  `{"files": [{filename, action, instruction}]}` (`_parse_file_plan`, tolerant), then each op runs
  through `_file_op_flow`. **Cross-file consistency:** every per-file call gets the full plan
  manifest as `extra_context`, plus `_sibling_context(written)`, so
  `<link href>`/`<script src>`/shared names line up. The manifest also carries
  `_shared_asset_note(ops)` — the ONE stylesheet / ONE script the plan chose, named exactly, so the
  pages don't link a variant spelling (`scripts.js`) that the reference repair then creates as a
  second, overlapping asset.
  - **Shared build spec (`app/agent/buildspec.py`) — runs BEFORE the plan.** `_plan_file_ops`
    decomposes per file; nothing turned the request's *cross-file* demands into one canonical
    statement, so each per-file call re-interpreted them and disagreed ("Our Story" became "About"
    on page 3; "soft pastel" became Arial and `#ff6b6b`). `_extract_build_spec` spends **one** extra
    LLM call — gated by `mentions_shared_spec()`, so an ordinary split request costs nothing and
    behaves exactly as before — and `build_spec_from_data` turns the answer into a compact block
    injected into the planner AND every per-file generation (it complements `_sibling_context`,
    which still threads the actual markup). Two halves, opposite rules:
    - *What the user asked for* (nav labels, cross-page behaviours) is **filtered against their own
      message** — a label they never typed is dropped, a behaviour about a page nobody mentioned is
      dropped. It can only restate the request, never invent one.
    - *Style* is deliberately **concretized**: style words → real Google Fonts + real hex codes, by
      the LLM when it cooperates and by `_STYLE_PRESETS` when it doesn't. Two deterministic quality
      gates reject the model's own output when it contradicts the request: `palette_matches_style`
      (measured in **chroma**, not HSL saturation — every light tint scores ~1.0 saturation, which
      would reject the very pastels it should accept) and a concrete-CSS check on the decorative
      line, both live-caught returning gold/crimson for "soft pastel" and "…creates a warm and
      inviting atmosphere" as design guidance.
    Nothing style-ish or nav-ish in the prompt → empty spec → the block is omitted entirely. **`_sibling_context` is the shared threading
  helper** (used by `_multi_file_flow` AND `_run_subtasks`): it lifts the `<nav>` (or a linking
  `<header>`) out of the first written page and states it **once** as canonical markup to copy
  verbatim, then quotes the most recent siblings against a **single total budget**
  (`max_sibling_context_chars`). It replaced `_read_refs(written, max_chars=2500)`, whose cap was
  **per file** — a six-page build shipped ~12 KB of markup that overflowed the context window and
  evicted the very pages defining the nav, and each excerpt was cut at a fixed offset that could land
  before the nav (long `<head>`) or mid-element. That was the "every page has a different navbar" bug.
- **Genuine multi-step work in a loaded project → `_run_tool_loop`** (native tool calling) — and,
  since 2026-07, **any repair request whose target can't be pinned down**. `_wants_existing_file_change()`
  (a repair verb — fix/update/refactor/rename/… — that isn't opening an interrogative) marks a
  request to change something that *already exists*. Two escalation points use it:
  `_route_one` sends such a request to the tool loop rather than the tool-free `_direct_answer`, and
  `_file_op_flow` bails to the tool loop when `target`, `_extract_filename` and
  `_last_write_fallback` **all** come back None. That last one is the important guard: without it
  `_infer_filename` fell through to its last resort `"output.txt"`, and the model — given no file to
  work from — wrote *"please provide the contents of these files"* onto disk. Creation requests are
  deliberately untouched: "make me a landing page" still infers `index.html`.
  `_FILE_OP_TARGET_RE` also covers UI nouns (`nav`/`navbar`/`header`/`footer`/`hero`/`button`/`form`/…)
  so "fix the navigation on all the pages" hits `_file_op_flow` directly; it excludes language-level
  words (`function`, `class`) so "write a python function that adds two numbers" stays a snippet.
- **Everything else (write/explain code, Q&A) → `_direct_answer`** (one call, no tools).
  This path streams: `chat()`/`_direct_answer` accept an optional `on_token` callback which,
  when set, switches to `_llm_stream.astream()` and fires per token. The REPL passes one from
  `_agent_turn` and shows tokens in a transient Rich `Live` region, then erases it and prints
  the final syntax-highlighted render (never duplicated). File/tool flows don't stream.

> **3B-era hardening — candidate for re-testing now that `qwen2.5-coder:7b` is the default.**
> The following were tuned for the 3B model's unreliability, NOT yet re-validated on 7B. Behavior is
> unchanged in this pass — these are flagged as follow-up experiments, not edits:
> - **`_wants_file_op()` regex routing** — bypasses `classify()` for file creation because 3B was
>   unreliable at the JSON tool protocol. 7B may not need this workaround; candidate to route file
>   creation back through `classify()`/the tool loop and A/B the result.
> - ~~`_normalize_action()` / `_coerce_args()` JSON-repair~~ — **resolved 2026-07** by roadmap
>   Tier 1 #2: the loop now uses native function calling and the repair machinery is deleted.
> - **`_surgical_edit` one-retry-then-whole-file-rewrite fallback** — 3B routinely dropped SEARCH
>   indentation and produced unmatched blocks. 7B may hit the exact/tolerant matchers more reliably,
>   so the rewrite fallback may fire less; re-measure the surgical-vs-rewrite ratio.
> - **Test fixtures encode 3B quirks** — `tests/test_file_flow.py` (e.g. the re-indent test at the
>   "3B copies SEARCH lines without the file's leading indent" comment) and the `ScriptedLLM`-driven
>   flows assert the fallback/repair paths still work given 3B-style malformed output. Keep these:
>   they verify the hardening survives regardless of model. Don't tighten expectations to assume 7B
>   is cleaner without first confirming against the live model.
> - **Code comments in [app/agent/core.py](app/agent/core.py)** (`_EXT_GUARD`, `_apply_block_linewise`,
>   `_file_op_flow`) still describe 3B behavior as their rationale — left intact deliberately; they
>   document *why* the guards exist, not a claim that 7B misbehaves identically.

**`app/resources/prompts/system.md` must NOT contain tool-protocol text** — the tool loop's behavioral guidance
comes from `_tool_guidance()` and the schemas from `bind_tools`. If you put tool-protocol text in
system.md it leaks into `_direct_answer`/`_file_op_flow` and the model emits fake tool-call JSON
instead of the file/answer.

### Vision pipeline — screenshot-to-code (`app/agent/vision.py`)

`coder> Build a website like this @screenshot.png`. There is no new command, flag, or model
switch: the UX is the existing `@path` syntax, and an `@`-ref whose **extension** is in
`settings.image_extensions` is routed to the vision model instead of being read as text.

The vision model (`settings.vision_model`, default `qwen2.5vl:7b` — **note the Ollama tag has no
hyphen**; `qwen2.5-vl:7b` does not resolve) is a **translator, never a participant**.
`_describe_image` base64s the file, sends it as one `HumanMessage` with `text` + `image_url`
content blocks (langchain_ollama converts a `data:` URI into Ollama's `images` field — verified
live on 1.1.0), and returns the structured description. From there everything downstream sees
**plain text**: `chat()`'s routing, `Planner.classify`, `_multi_file_flow`, `_file_op_flow`,
`_build_messages` are all unchanged and have no idea an image was involved. That encapsulation is
the design — keep image handling inside the ref-reading layer.

The extraction prompt is `app/resources/prompts/vision_describe.md` (loaded via
`settings.prompts_dir` like `system.md`, **not** hardcoded); it asks for LAYOUT / NAVIGATION /
COLOR PALETTE / TYPOGRAPHY / COMPONENTS / CONTENT / STYLE with concrete hex values, which is what
makes the description actionable for the coding model. `system.md` says nothing about vision.

Three seams in `core.py`, all small:
- **`_read_refs`** routes an image ref to `_describe_image_ref` and injects the description where
  the file text would have gone. **`_route_one`** splits `at_refs` (`_split_image_refs`), describes
  the images once, and threads the block into `extra_context` for *every* branch — so a screenshot
  reaches the file/multi-file flows too, not just the direct answer.
- **An image ref is itself the file-op signal** (`_wants_image_build`). `_wants_file_op` needs a
  verb AND a target noun — but with a screenshot the noun *is* the image, and the ref is stripped
  out of the text, so "build this @shot.png" matched nothing and dead-ended on `_direct_answer`:
  the page was printed into the terminal and no file was written. An image ref plus a build verb
  now routes straight to `_file_op`. `_IMAGE_BUILD_VERB_RE` is deliberately its own regex (it adds
  recreate/replicate/clone/copy/mimic/convert/turn/code/design — how people actually talk about a
  mockup) and is consulted ONLY when an image ref is present, so it cannot change how a text-only
  request routes. `_EXPLAIN_QUESTION_RE` keeps "what does @shot.png show?" an answer, not a write.
- **An image is never an edit target.** `_resolve_ref` filters images out, and `_strip_at_refs`
  *removes* an image ref from the message entirely (text refs still just lose their `@`) — leaving
  "screenshot.png" in the text made `_extract_filename` target the PNG and the build got written
  onto the screenshot.
- **`_run_subtasks`** gives a text ref only to the step that names it (target pinning), but gives
  the image to **all** of them: a screenshot is the visual reference for the whole request.
  `_describe_image_ref` memoizes on (path, mtime, size), so N sub-tasks still cost ONE vision call.

**Model swapping is Ollama's job.** Only one 7B fits in 8 GB of VRAM, so the vision call loads
`qwen2.5vl:7b` and the generation call swaps it back for `qwen2.5-coder:7b` — a few seconds each
way. Do NOT add preloading/keep-alive or try to hold both. The vision `ChatOllama` is built
on-demand in `_describe_image` and is **separate** from the agent's own instances (own model name,
own `vision_num_ctx=4096` — the output is short). The user sees the swap via
`AgentCore.status_hook`: the REPL installs one per turn that writes `[vision] Analyzing shot.png …`
into the Live spinner and reprints the lines under it (the Live region is transient, so a status
line left only there vanishes). Status lines are rendered as `Text`, never markup — `[vision]`
would otherwise parse as a Rich style tag.

**Every failure is non-fatal, by design.** Missing model (the message names `ollama pull …`),
unreadable/empty/oversized file (`settings.max_image_bytes`, 20 MB), connection error, or an empty
/ sub-40-char answer all return `None`, and the turn proceeds exactly as if the image had never
been referenced — text-only. `settings.vision_enabled = False` is the kill switch and skips the ref
before any model is constructed. **The image read path is jailed:** `_describe_image_ref` runs the
same `app/tools/filesystem._jail_check` the file tools use before reading the bytes, so an
`@../../secret.png` that escapes `sandbox_root` is skipped (non-fatal) rather than base64'd off-disk
— the vision pipeline reads bytes directly and would otherwise bypass the executor's jail. `VISION_MODEL=llava:7b` in `.env` swaps the model with no code
change. `tests/test_vision.py` covers all of it offline (fake `ChatOllama`, bytes in `tmp_path`).

### Click the page, edit the source (`app/agent/pointer.py`, `/point`)

The premise: **a 7B is not bad at writing twenty lines of markup — it is bad at deciding
where.** `_extract_filename` → `_locate_named_file` → `_resolve_target_from_spec` →
`_last_write_fallback` → the tool-loop escalation is five stacked heuristics answering that one
question by inference, and each carries a comment about the live build where its guess was
wrong. A click answers it by observation, and `/point` is the seam.

The second-order effect is the bigger one. The mapping ends at a span of **real text taken out
of the template on disk**, so the SEARCH half of the edit is *constructed*, not quoted:
`_surgical_edit(pinned=…)` asks the model for the REPLACEMENT ONLY. A pinned SEARCH cannot be
misquoted, cannot fail to match, and never reaches the rewrite fallback — the entire failure
class `patch.py` exists to survive is unreachable on this path.

- **The mapping is pure and the browser is not.** Everything above `capture_click` is text and
  dataclasses in, `PointerTarget` or `Decline` out, so all of it is tested with no browser, no
  server and no model. `Element` deliberately holds no Playwright types.
- **URL → template goes through the graph, never a second copy of its rules.**
  `TemplateGraph.resolve_template` was made public for this: a route stores the template NAME
  as its source writes it (`render_template("products.html")`, `res.render("products")`) and
  neither is a path. A path is judged against **every** matching route — `check_links`' union
  rule, for its reason — and routes that disagree on the template are an ambiguity, so a
  refusal.
- **Element → span, three rules, first one with exactly ONE candidate wins**: `id`, then the
  full class list, then verbatim visible text. Text is last because a template's text is often
  an expression (the value is in the database, not the file), and first-tried it would match
  the loop body of every card. Scanning runs on `verify.mask_template_tags`' masked copy — a
  `>` inside `{% if a > b %}` otherwise truncates the tag — and the span is sliced out of the
  **real** source, so template expressions reach the SEARCH block intact.
- **An unbalanced document declines rather than guessing where the element ends.** Splicing at
  a guessed boundary is exactly how a repair pass corrupts a file.
- **A click on the nav resolves to the LAYOUT, not the page.** A child template inherits its
  shell, so an unresolved click retries against `graph.parents(...)` before giving up — and
  says which file it landed in.
- **A stale click refuses.** If the pinned span is no longer in the file (another turn, an
  editor, a repair pass), nothing is written and the answer says to point again. Editing a
  stale span is a silent wrong edit; re-pointing is one click.
- **The picker window is HEADED, and that is the only one in the codebase.** Everything else
  drives Chromium headless because it is *measuring*; this is the user's own eyes. Localhost
  is checked before launch — `browser.py`'s rule, and it matters more here because the window
  is interactive. A missing Playwright is `install_hint()`, never a silent no-op.
- Knobs: `point_timeout` (180s). `/point` requires `/run` first — pointing needs a live page.

### One matcher for every edit, and two truncation guards (`app/agent/patch.py`)

The reported failure was "it rewrites the whole file and loses my code". The block format
already existed; what produced the rewrites was a set of defects around it, each of which
turned a targeted edit into a full regeneration — and one that truncated the regeneration.

- **The matching ladder is one module with two callers.** It lived inside
  `core._apply_block_linewise`, so the `edit_file` **tool** — the path the native tool loop
  takes — had none of it: `old_str not in original` refused a misquoted indent that the
  agent's own editor would have applied, and a 7B answers a refused edit with `write_file`
  and the file regenerated. Rungs: exact substring → trailing-ws → strip-tolerant with the
  replacement **re-indented to the file** → normalized fuzzy at `FUZZY_RATIO` (0.90).
  **Rung 4 refuses more than it accepts, and that is the whole safety argument for having
  it**: never a single-line SEARCH (`return None` occurs eleven times; the first hit is a
  coin flip), and never two candidates at that ratio. A wrong replacement is silent; a
  refused one is reported and retried.
- **The file is shown NUMBERED, and `strip_line_numbers` takes the gutter back off.** A
  gutter gives a small model a way to say *where*, and creates a way for it to quote the
  numbers into SEARCH; undoing that deterministically is what makes showing it safe. The
  stripper is strict on purpose — every non-blank line must carry the prefix and the numbers
  must strictly increase — so `mask = 1 | 2` is never mistaken for a gutter.
- **A failed SEARCH is answered with the file's real lines, not with "not found".**
  `nearest_region` names the closest window deterministically and numbers it; `_surgical_edit`
  re-asks **only the blocks that missed** (once, against the text the successful blocks
  already produced, so a correction cannot revert a change that landed), and `edit_file` puts
  the same region in its error. Told only that it failed, the model rewrites the file; shown
  the lines, it quotes them back.
- **`max_edit_context_chars` (24000) replaced a hardcoded 6000 that was never a context
  limit.** `llm_num_ctx` is 16384 *tokens*. All 6000 ever bought was a guaranteed miss on any
  edit aimed past it — and a guaranteed miss falls through to the whole-file rewrite, so the
  wrong number was not cosmetic: it decided which path ran. When a file genuinely does not
  fit, the cut is **stated** (`_edit_view`), because an unannounced one reads to the model as
  the whole file.
- **The rewrite prompt pasted `full_existing[:4000]` under the words "return the COMPLETE
  updated file".** Every file over 4000 characters was shown cut off, came back cut off, and
  was written — the reported truncation, produced by the pipeline rather than by the model,
  and invisible to every check because half a file parses. The paste is now whole, and above
  `max_rewrite_chars` the rewrite is **refused** (`_rewrite_refusal`, before the LLM call)
  rather than run against a partial view. **A rewrite that cannot see the whole file must not
  run.**
- **`_shrink_refused` is the last line, on both write paths.** A write keeping under
  `shrink_guard_floor` (0.6) of the file's bytes is a truncation, not an edit — `check_file`
  cannot see it, and neither can the intent judge. Two exemptions keep it from refusing real
  work: files under `shrink_guard_min_chars` (400), where a three-line file legitimately
  becomes a one-line file, and a request that **asked** for removal (`patch.wants_deletion`,
  which is also the escape hatch — "rewrite it from scratch" writes).
- **`apply_diff` is all-or-nothing** by design: a half-applied multi-edit leaves a file nobody
  planned and the model cannot see what state it is in, so its next move is a rewrite. Either
  every edit matched and the file is written once, or nothing is touched and the report names
  the edit that missed *and* the text it came closest to.
- `_tool_guidance` and `_EDIT_INSTRUCTIONS` state the anchoring rules the 7B needs (anchor on
  a `def`/tag/unique string, never a bare `}`; ≥3 lines of context; never a whole-file SEARCH;
  `write_file` on an existing file destroys every line you did not repeat). They are prompt
  hints, so each has a deterministic backstop above — that pairing is `strip_external_assets`'
  rule.

### Tool-call loop (native function calling)

`_run_tool_loop()` binds the registry (`ToolRegistry.to_openai_tools()` → OpenAI function format)
via `ChatOllama.bind_tools()` and consumes structured `AIMessage.tool_calls` — there is **no
hand-rolled JSON protocol and no output parsing/repair** (deleted in roadmap Tier 1 #2). Loop
shape: a response with tool calls → execute each via the executor, feed each result back as a
`ToolMessage` (paired by `tool_call_id`, preceded by the assistant message that carried the calls);
a response with **no** tool calls is the final answer. The loop LLM is plain-mode — `format="json"`
would fight native tool calls. What remains of the hardening:
- A `"Tool not found"` result triggers a **firm correction** ToolMessage (lists valid tools, tells
  it to answer directly) instead of letting it retry a hallucinated tool until `max_steps`.
- Real tool failures get one `recovery_hint()` (§11), then the loop **gives up gracefully** after
  `settings.max_tool_failures` failures of the same tool.
- LLM invoke exceptions retry up to `settings.max_tool_retries`.
- **Old-Ollama fallback (`_parse_textual_tool_call`)**: Ollama servers ≤ ~0.31 never populate
  `tool_calls` — the model's tool JSON arrives as plain content (confirmed live on 0.31.1). If a
  response's ENTIRE content is one `{"name": <str>, "arguments": <dict>}` object (optionally
  fenced), it is executed as a tool call; anything else is a final answer. Upgrading Ollama makes
  native `tool_calls` arrive and this fallback stop firing — do not widen it into a JSON repairer.

**The model's own task list (`app/agent/todos.py`, `settings.todo_tool`).** The loop used to
execute blind: step 7 saw only the ToolMessage history, never "3 of 6 done, next is X", and on a
long turn the early steps fell out of the context window — every deterministic repair pass
(`_verify_blueprint_coverage`, `_repair_dead_references`, `_wire_missing_endpoints`) is a patch
for a step the model forgot. `update_todos` is a builtin that stores a checklist in a per-core
`TodoStore` and nothing else — no file, no shell, `permissions=[]`, so no gate ever fires and a
bot `viewer` may use it. Zero extra LLM calls: the model updates the list inside calls it already
makes. Rules, all inherited:
- **The list is restated after EVERY tool round**, riding the round's last ToolMessage (the one
  legal place — a ToolMessage must pair with a call). Stated once, it is the first thing history
  eviction deletes; that restatement IS the mechanism.
- **An unparseable update keeps the old list** (`parse_verdict`'s rule), and the handler **always
  returns success** — a failed tool counts toward `max_tool_failures`, so two note-taking slips
  would otherwise end the whole turn.
- **A hint to the model, never a gate.** Nothing downstream trusts it: a todo marked done that
  wasn't is still caught by the deterministic checks. Unknown status reads as `todo`, never
  `done` — the misreading that makes the model finish the item.
- **Capped** (20 items, 160 chars each): the block recurs every round, and an unbounded list
  evicts the file context the work depends on (`_sibling_context`'s lesson).
- **Per-turn state**, cleared at the top of `chat()` beside `_blueprint`/`_build_spec`; the
  max-steps give-up message names the unfinished items instead of only a count. Registered in
  `AgentCore.__init__` (closure over that core's store), never in the shared `get_registry()`.

### Tool registry & executor — the central hub

- `app/agent/tool_registry.py` — every tool (builtin, MCP-discovered, skill-unlocked) must be
  registered here. `create_registry()` builds the default with all 14 builtin tools; `get_registry()`
  is a **lazy** cached accessor (Step 12 / A1 — no eager import-time singleton). Tools carry
  `source` = `"builtin"` | `"mcp:<server>"`
  | `"skill:<skill>"`; `unregister_by_source()` is how MCP disconnect cleans up. Every tool also
  carries `permissions` tags — builtins use `fs:read` / `fs:write` / `fs:delete` / `shell` /
  `git:read` / `git:write`; MCP tools are tagged `mcp` as a class.
  **Builtins are never shadowed:** `register()` refuses to let a non-builtin tool take a name a
  builtin already owns and gives it a namespaced alias instead (`filesystem_write_file`), returning
  the name it actually used. This is load-bearing — `@modelcontextprotocol/server-filesystem`
  advertises `read_file`/`write_file`/`edit_file`/`list_directory`/`search_files`, and before the
  guard those overwrote the builtins, so the next `unregister_by_source("mcp:filesystem")` on
  disconnect **deleted** them and every file flow died with `Tool not found: 'write_file'`.
  `MCPManager.connect_server` records the aliases on `conn.renamed_tools` (surfaced by `/mcp list`).
- `app/agent/executor.py` — async `execute()`: **refuses any tool whose `permissions` intersect
  `settings.denied_permissions`** (default empty = allow all), validates args against the tool's
  JSON Schema, consults the **approval gate** (below), then awaits async handlers (MCP) or runs sync
  handlers in a thread pool. **Every tool handler must return `{"success": bool, "result": str,
  "error": str | None}`** — this contract is assumed everywhere (REPL tool-step rendering, the tool
  loop's result feedback). Mutating file tools may add a display-only `"diff"` key (unified diff):
  the REPL renders it under the tool step; the tool loop feeds only `result["result"]` to the model.
- **Approval gate (Step 6 / S3, S6):** before running any tool whose permissions intersect
  `settings.approval_gated_permissions` (`fs:write`/`fs:delete`/`shell`), `execute()` consults an
  optional async `approval_hook`. The REPL installs `CoderREPL._approve_tool` (prompts
  `[a]llow / allow [s]ession / [d]eny`, remembers session-allows, pauses the Rich `Live` while
  prompting) **only when `stdin.isatty()` and not `--yolo`**. With no hook installed (tests, piped
  input, evals) the default is **allow**, except under `--safe` which denies
  `settings.safe_deny_permissions` (`shell`/`fs:delete`) so a non-interactive run can't silently
  run them. `--yolo` sets `settings.auto_approve` → gate skipped entirely. NB: the deterministic
  `_file_op_flow`/`_surgical_edit` writes go through `executor.execute("write_file", …)` like every
  other tool call, so they are gated too — and they resolve `write_file` **by name in the registry**,
  which is why the no-shadowing rule below matters.
- **Safe writes** (`app/tools/filesystem.py`): `write_file` (overwrite), `edit_file`, and
  `delete_file` back up the previous content into `settings.backups_dir` before mutating — a
  failed backup aborts the mutation. `undo_write` (builtin tool, also the `/undo` REPL command)
  restores and consumes the newest backup (optionally per path); backups are pruned to
  `settings.max_write_backups`. The original absolute path is URL-quoted into the backup
  filename after the first `__`.
- **Path jail (Step 5 / S2):** every file tool (`read`/`write`/`edit`/`create`/`delete`/`list`/
  `search`) runs `_jail_check()` first — a path that resolves outside `settings.sandbox_root` is
  refused unless `settings.allow_outside_root` (`--allow-outside-root`). The jail is **inert when
  `sandbox_root` is None** (tests / library import impose no policy); `main.py` sets it to cwd at
  startup and `AgentCore.load_project` narrows it to the project dir.
- **Shell hardening (Step 7 / S1, S4)** (`app/tools/terminal.py`): `run_command` keeps the denylist
  (`_is_blocked`), and adds an opt-in **allowlist** (`settings.command_allowlist`, enforced only
  when non-empty) plus a **network gate** (refuses `settings.network_commands` and pip/npm/git-style
  remote fetches unless `settings.allow_network` / `--allow-network`). Both split the command on
  shell operators (`;`, `&&`, `||`, `|`, `&`) and check **every** chained binary, so a compound
  command can't smuggle a denied/network binary past the first token. `shell=True` stays on Windows
  for usability; the per-segment analysis is the "gate metacharacters" half of the step.

### RAG pipeline

`Retriever` ([app/rag/retriever.py](app/rag/retriever.py)) wraps `VectorStore` (ChromaDB) and the
embedder. **One ChromaDB collection per project**, named after the folder. Tree-sitter chunker
([app/rag/chunker.py](app/rag/chunker.py)) emits semantic chunks (functions/classes), falling back
to token-window sliding for non-code or oversized nodes.

**Incremental indexing (Step 2 / P1, P2):** `index_project` skips files whose SHA-256 content hash
matches what's already stored — the hash rides in each chunk's `content_hash` metadata and is read
back via `VectorStore.get_file_hashes()`. So re-loading an unchanged repo re-embeds **zero** chunks;
`index_project` returns `indexed`/`skipped` counts alongside `files`/`chunks`. Test doubles that
don't implement `get_file_hashes` degrade gracefully to full re-indexing (`getattr` guard). The
embedder ([app/rag/embedder.py](app/rag/embedder.py)) is a **two-tier cache** keyed by SHA-256 of
the text: an in-process LRU dict over a **persistent on-disk cache** (`settings.embed_cache_dir`,
one JSON file per key, LRU-pruned to `settings.max_embed_cache_entries`), so embeddings survive
restarts. The `OllamaEmbeddings` client is memoized (`functools.lru_cache`). `clear_cache()` wipes
both tiers; the pytest `conftest.py` autouse fixture points `embed_cache_dir` at a tmp dir so tests
never touch the repo cwd.

**Skips & caps (Step 3 / P4, C4):** the indexer honors the project's root `.gitignore` (via
`pathspec` — declared in `pyproject.toml`), skips files over `settings.max_index_file_bytes`, and
keeps the existing dot/`__pycache__`/`node_modules` skips. `read_file` truncates at
`settings.max_read_file_bytes` with a "truncated" note; `search_files` skips binary files (NUL byte
in the first 1 KiB) and vendored/hidden dirs.

**Live auto-reindex (Step 4 / P3):** `AgentCore.load_project` starts a `ProjectWatcher`
([app/rag/watcher.py](app/rag/watcher.py)) — a debounced `watchdog` observer on the project root
that feeds changes into `retriever.index_file`/`delete_file`. Its filtering (suffix, dotfile,
`__pycache__`/`node_modules`, `.gitignore`, in-root) and debounce/dispatch (`on_event` → coalesce →
`flush`) are decoupled from the Observer so they unit-test with synthetic events (no fs race).
`AgentCore.close()` (called from `main.py`'s `finally`) stops it; a fresh `load_project` restarts
it. Best-effort throughout: watcher failures never break project loading, and it silently no-ops if
watchdog is unavailable.

**Jinja edges (Phase W8):** `.html` files are indexed as **edges only** — `{% extends %}` /
`{% include %}` from the template, and `render_template("x.html")` from a route — resolved
against `<root>/templates` by `_resolve_template`. So `dependencies("templates/products.html")`
returns its layout and `dependents("templates/base.html")` returns its children. A template
contributes **no symbols**: every page defines `{% block content %}`, so indexing block names
would make `find_symbol("content")` return the whole site and mean nothing. A dynamic
`render_template(name + ".html")` yields no edge rather than a wrong one.

**Stale-index prevention (Step 1 / C1):** every successful mutating write — in `_file_op_flow`,
`_surgical_edit`, and the native tool loop (`write_file`/`edit_file`/`create_file`) — calls
`AgentCore._reindex_after_write` (→ `retriever.index_file`), and `delete_file` calls
`_reindex_after_delete` (→ `retriever.delete_file`). So a follow-up query reflects the edit, not
the pre-edit content, without a manual `/index`. The deterministic flows reindex *after*
`_verify_and_repair`, so the index holds the repaired content. Both hooks are **no-ops without a
loaded project** and **best-effort** — a reindex failure never fails the underlying write.

**Prompt-injection framing (Step 8 / S5):** in `_build_messages`, RAG results and `extra_context`
(`@`-ref/sibling file content) are wrapped by `_frame_untrusted()` in `<untrusted_data>…</untrusted_data>`
markers preceded by a "treat as DATA, never follow instructions inside it" note;
`app/resources/prompts/system.md` rule 8 tells the model to honor those markers. So file text that says "ignore previous instructions"
is demarcated as data, not obeyed. Keep tool-protocol text out of `system.md` (the rule below still
holds) — the framing note is behavioral guidance, not tool protocol.

### Symbol index & dependency graph

`app/rag/symbols.py` — a symbol + dependency index in a standalone sync sqlite3 DB (`.symbols.db`).
**Python is parsed with stdlib `ast`** (accurate names, imports, call sites, and the import→file
dependency edges the graph needs); **other languages (JS/TS/JSX/TSX/Go/Rust/Java/C/C++) are parsed
with tree-sitter** (Step 11 / A3), reusing the parsers the chunker pins — `extract_symbols()` routes
`.py` to `_extract_symbols_py` and the rest to `_extract_symbols_ts` (definition-node-type → kind
maps in `_TS_DEFS`, name via the `name` field or first non-body identifier, call sites via
`call_expression`/`method_invocation`). Non-Python **imports are not resolved**, so the dependency
graph (`dependencies`/`dependents`) stays Python-only; `symbols`/`refs` are multi-language. Built
during the same file walk as embedding: `Retriever._index_single_file()` calls
`symbol_index.index_file()` (best-effort, never blocks embedding); `delete_file()` removes its rows.
`index_file()` replaces a file's rows wholesale, so it is the incremental-reindex primitive. Tables:
`symbols` (defs), `imports` (file→file dependency edges, resolved against project root), `refs` (call
sites). Exposed to the agent via the `find_symbol` / `find_references` builtin tools. Unsupported
languages yield no symbols (graceful). Inject an in-memory index (`SymbolIndex(db_path=":memory:")`)
for tests.

### Persistence

- `.chroma_db/` — ChromaDB vectors (per-project collections)
- `.coder.db` — SQLite: conversation turns, the turn log (below) + project summaries
  (SQLAlchemy async / aiosqlite)
- `.symbols.db` — sqlite3: symbol/import/reference index (sync, separate from `.coder.db`)
- `.coder_history` — prompt_toolkit history
- `.coder_backups/` — pre-mutation snapshots for `undo_write` (pruned to `max_write_backups`).
  **Per-project (Step 10 / C3):** when a project is loaded these live under
  `<sandbox_root>/.coder_backups/`, so `/undo` never restores a file from another project; without a
  loaded project the relative default resolves against cwd.
- `.coder_embed_cache/` — persistent embedding cache, one JSON per SHA-256 (pruned to
  `max_embed_cache_entries`); gitignored

### The working history (`app/memory/turnlog.py`, `/export`)

Phase T0 of `docs/telegram-bot-plan.md`. `conversation_turns` stores role + content — the
transcript a chat UI needs, and not the one the work needs. The route `chat()` took and the
tool trace, which is where the reasoning actually is, were returned to the caller and dropped.
Rebuilt from what was stored, the history of a turn that scaffolded a project, generated eleven
files, repaired four and ran a smoke test was the request and the reply.

`turn_events` stores the rest: `source`, `project`, `task_type`, `flow`, the compacted tool
trace, the files written, and the duration. `/export [file]` renders it as Markdown
(`/export sessions` lists what was recorded, `--session <id>` picks another one).

- **Recording is best-effort and never raises.** `record_turn` returns False instead — it runs
  at the `chat()` seam beside `memory.add_ai`, so a history that will not write must never cost
  a turn whose files already landed. `ProjectSpec.save`'s rule.
- **`source` (`"cli"`, or `"telegram:<user_id>"` once T2 lands) is the point of the table.**
  A session driven by two front-ends otherwise reads as one actor, and "they worked on it at
  the same time" is not checkable from the record afterwards. It is an `AgentCore` attribute
  (`turn_source`), not a `chat()` argument, so every existing caller and test is untouched.
- **`flow` is recorded, not re-derived.** The decision is made from state that is gone by the
  time the answer comes back — the spec, the blueprint, the compound splitter's verdict — so
  `chat()` stamps each branch as it takes it. Both per-turn fields are reset at the TOP of
  `chat()`, so a turn that raises cannot leave its route attached to the next one.
- **`render_transcript` is pure** — plain dicts in, a string out, no DB and no event loop — so
  the format is unit-tested directly. Tool results are capped (`MAX_RESULT_CHARS`): the trace is
  stored for the record, not for replay, and an unbounded blob makes the table larger than the
  project it describes.
- The DB tests pass an explicit `session_factory`. `AsyncSessionLocal` is bound to an engine
  built at import from `settings.sqlite_path`, so monkeypatching the setting would not move it
  and the suite would write into the repo's real history.

### Two front-ends, one machine (`app/agent/sessions.py`, `projectlock.py`, `scope.py`)

Phase T1 of `docs/telegram-bot-plan.md`, and deliberately built **before** the bot that needs
it: "the app and the bot at the same time" is a property of this seam, not a retrofit onto a
transport. Nothing here is Telegram-specific — a second front-end is just a second caller.

`SessionRegistry` maps a resolved project path to **one** `AgentCore`, one `asyncio.Lock` and a
last-used stamp. `registry.turn(path, source=…)` is an async context manager that holds
everything a turn needs and puts it back:

```
async with registry.turn(project, front_end="telegram", source="telegram:42") as agent:
    answer, trace = await agent.chat(msg)
```

- **One core per project, shared by every front-end.** Two cores would each hold their own
  `_spec`, and turn 2's amendment would plan against turn 1's stale contract — the staleness
  the multi-turn eval suite exists to catch. `adopt()` is how the REPL joins with the core it
  already built rather than creating a second one for the same folder.
- **Turns on one project are serialized; turns on different projects overlap.** `AgentCore`
  carries per-turn state (`_blueprint`, `_spec_doc`, `_entry_routes`, `_last_write_path`), so
  two interleaved turns on one core read each other's.
- **The sandbox root is a `ContextVar`, not a saved-and-restored global** (`scope.py`,
  `effective_sandbox_root()`, read by `filesystem._jail_check` and `_backup_root`). A
  save/restore of `settings.sandbox_root` was written first and **measured failing**: two
  concurrent turns on two projects, and the second turn's assignment moved the FIRST turn's
  jail while it was still writing. The lock cannot prevent that — different projects are
  supposed to overlap. Each asyncio Task gets its own copy of the context, which is the
  property a global cannot have. `settings.sandbox_root` stays the fallback, so a
  single-front-end session, a library import and every existing test are unchanged.
  `project_settings()` also restores the global regardless, because `load_project` assigns it.
- **`<project>/.coder/coder.lock` is the cross-process half.** An `asyncio.Lock` cannot see a
  `--bot-only` daemon in another terminal. A held lock makes the other front-end **wait and say
  who holds it** (`on_wait`, `LockInfo.describe`) — never barge, never fail silently; past
  `turn_lock_timeout` the caller gets `TurnBusy` with the holder named.
- **Stale locks are reclaimed by PID liveness, not age.** A build turn legitimately runs for
  minutes. `turn_lock_stale_after` is only the PID-reuse backstop, and a lock written by a
  different **host** is never reclaimed. **Windows liveness goes through `OpenProcess`, never
  `os.kill(pid, 0)`** — on Windows that is `TerminateProcess`, i.e. the probe would kill the
  other front-end mid-turn. `tests/test_sessions.py` drives an actual second process for this,
  because every other test uses our own PID and so proves the policy rather than that it sees a
  foreign process at all.
- **A lock is released only by the process still holding it** (each acquire writes a random
  token), so a process whose lock was reclaimed cannot delete its successor's.
- **`init_db` sets WAL + `busy_timeout`.** Two processes appending turns to `.coder.db` are
  otherwise a `database is locked` on the write that records the answer.
- Knobs: `cross_process_lock` (kill switch), `turn_lock_timeout` (900s), `turn_lock_stale_after`
  (3600s), `session_idle_timeout` (1800s — `close_idle` stops the watcher on projects nobody is
  using; a session with a turn in flight is never closed).

### The Telegram front-end (`app/bot/`)

Phase T2 of `docs/telegram-bot-plan.md`. A second front-end over the T1 seam, and **nothing
about it is special**: it calls `registry.turn(...)` and `agent.chat(...)` exactly as the REPL
does, so "the app and the bot on one project" is already true before the bot exists.

Four files, and the split is the point:
- **`render.py` is pure** — answer markdown to Telegram HTML, and chunking.
- **`transport.py` is the only file that imports `telegram`**, behind a `Transport` protocol
  (`send`/`edit`/`typing`/`ask`).
- **`service.py` holds every decision** and talks only to the protocol, so the whole of it is
  tested with a fake transport, a fake agent, no token and the library not installed.
- **`telegram_bot.py` is wiring.** A rule that ends up there has ended up where the suite
  cannot reach it.

- **Off by default** (`telegram_enabled`), and the first dependency that talks to the network —
  an optional extra (`pip install -e ".[telegram]"`). It is a real exception to the offline
  rule and is scoped: the model, the index and every file operation stay local; what leaves the
  machine is the conversation, which is what a chat front-end *is*. `transport.available()` /
  `install_hint()` follow `browser.py` — a missing library is a loud reason, never a silent
  no-op.
- **HTML parse mode, not MarkdownV2.** MarkdownV2 needs sixteen characters escaped *including
  inside code spans*, and one miss makes the API reject the whole message — so an answer full
  of code would routinely fail to send at all. HTML needs `&<>`.
- **Chunking splits the SOURCE and reopens an open fence**, so no message carries an unbalanced
  `<pre>` (Telegram rejects it, and the rest of the answer then arrives as unformatted text).
  The budget is measured on the ESCAPED length: `&` becomes `&amp;`, so a source-length cap
  under-counts by five times on exactly the text — a diff, a URL query — already near the limit.
- **Streaming is one message, edited on a timer** (`LiveMessage`, `telegram_edit_interval`).
  `on_token` cannot await, so it only buffers and a `pump` task does the editing; an unchanged
  preview is not re-sent (Telegram rejects it); the preview shows the stream's **tail**.
  `status_hook` lines ride above it, so `[vision] Analyzing …` reaches the phone.
- **Approvals reuse `executor.approval_hook`** — an inline keyboard instead of the REPL's
  prompt. **Anything that is not an explicit Allow is a Deny**: a timeout, a dismissed
  keyboard, an unrecognised payload. "Allow for session" is remembered **per chat**, and the
  question names the TARGET (`write_file` on *which* file).
- **The hook is cleared in a `finally`.** In the embedded mode the CLI shares that executor, so
  a hook left installed would send the terminal's next write to Telegram for approval.
- **Authorization is the numeric `user_id`, never `@username`** (reassignable — an allowlist of
  names is an impersonation surface), and **empty means nobody**: an unconfigured bot refuses
  everyone including its owner. T3 adds pairing, roles and audit; until then the allowlist is
  the whole story, so the bot is never open in between. A callback button is an authorization
  decision too and is checked the same way.
- **`handle()` never raises** — an exception escaping it kills the poller for every chat — and
  each message runs as its own task, so a minutes-long build does not stop the bot answering.
- Knobs: `telegram_token` (env only, never printed), `telegram_allowed_users`,
  `telegram_poll_timeout`, `telegram_edit_interval` (1.5s), `telegram_approval_timeout` (120s),
  `telegram_max_concurrent_turns` (2), `telegram_rate_burst`/`telegram_rate_seconds`.
- Long polling, never a webhook: a webhook needs an inbound public port and a certificate —
  a worse security story, and undemonstrable on a laptop.

### Bot authentication and authorization (`app/bot/auth.py`, `audit.py`)

Phase T3. **A role is a projection onto enforcement that already exists, never a new gate.**
`viewer` is not "the bot refuses to write" — it is `fs:write`/`fs:delete`/`shell` in the
executor's deny set, so the refusal happens in `Executor.execute` before the tool is reached
and would still happen if every line of `service.py` were wrong.
`tests/test_bot_auth.py::test_a_viewers_write_is_refused_by_the_EXECUTOR` is the load-bearing
test: it drives the executor directly and asserts the handler never ran.

| role | enforcement |
| --- | --- |
| `viewer` | denied `fs:write`, `fs:delete`, `shell` — refused before the tool runs |
| `developer` | denies nothing outright; writes go through the approval keyboard |
| `owner` | plus the `OWNER_ONLY` commands — `/load`, `/model`, `/mcp`, `/pair`, `/users`, `/revoke`, `/stack` |

- **The deny list rides the turn scope, not the setting** (`scope.effective_denied_permissions`,
  read by `Executor.execute`). Same reason as the sandbox root one layer up, with worse
  consequences: a `viewer`'s turn and an `owner`'s turn can overlap, and a global would let
  whichever started last decide what BOTH may do — privilege escalation produced by timing.
  **A scope may only ADD refusals** — it unions with `settings.denied_permissions`, so it can
  never be a way to ask for more than the process was started with.
- **Identity is the numeric `user_id`.** A `@username` is reassignable, so an allowlist of
  names is an impersonation surface.
- **`telegram_allowed_users` is the bootstrap and outranks the table** — otherwise a paired
  user could demote the owner by acquiring a row. It is edited in `.env`, so `/revoke` reports
  `False` for those ids rather than pretending. **Empty means nobody**, and `preflight()`
  refuses to start in that state: nobody could pair either, because minting is owner-only.
- **Everyone else arrives by pairing.** `/pair` mints a single-use, TTL'd code stored **hashed**
  (`.coder.db` travels with the project; a plaintext code in it is a live grant to whoever
  looks). `/login <code>` is the ONE thing an unauthorized user may do, and it is not a way in
  by itself — someone with the machine minted the code. **Every refusal reads identically**:
  distinguishing expired from used from unknown only helps someone guessing. A code is marked
  used **before** the grant, so a code shown to somebody cannot stay live after a failed
  redemption. A store that cannot be read denies rather than admits.
- **`audit.py` is separate from `turnlog` on purpose.** The turn log records what a TURN did
  and is the deliverable transcript; the audit log records what ACCESS CONTROL did — including
  the refusals that never became a turn, which is exactly the half a transcript cannot show.
  JSON Lines, append-only, per project under `.coder/` (already skipped by the indexer, so an
  audit log is never embedded or picked as an edit target), best-effort but logged on failure.
- What no role can do, because none of it is reachable from the prompt or from a chat: widen
  the path jail (`allow_outside_root` is a process flag), reach outside `sandbox_root`, or lift
  the shell denylist. **A remote caller is strictly less trusted than the terminal, never more.**
- Knobs: `telegram_pairing_ttl` (300s), `bot_audit_log` (`.coder/bot_audit.log`).

### Running both front-ends (`--bot`, `--bot-only`, `/bot`)

Phase T4 — the entry points, and the one change that makes the earlier phases *true* rather
than merely possible: **the REPL's own turn now goes through the registry**
(`CoderREPL._chat_in_turn`). It called `agent.chat` directly, so the CLI never took the
project's lock and "the bot waits for the terminal" was false in exactly the direction that
matters.

- **`coder --bot`** (or `/bot start`) runs the bot in the REPL's process, sharing
  `session_registry()` — hence one `AgentCore`, one lock, one conversation for the loaded
  project. Bot messages print into the terminal (`on_activity`), so one screen recording shows
  both front-ends.
- **`coder --bot-only`** is the headless daemon: no REPL and no `AgentCore` of its own, each
  chat picks its folder with `/load`, and the registry gives each one its own agent, lock and
  jail. Its loop does nothing but `close_idle()` — the library's updater owns the polling.
- **`/bot pair [role]`** mints a code from the machine itself. Without it the first remote user
  could never get in: minting over Telegram is owner-only, and an owner has to exist first.
- **`session_registry()` is a lazy cached accessor**, never a module-level singleton — the
  `get_registry`/`get_retriever` rule, and `tests/test_no_import_side_effects.py`'s.
- **`load_project` calls `registry.adopt(...)`** so the bot reuses the REPL's agent instead of
  building a second one for the same folder.
- **The CLI waits without a timeout** (`float("inf")`), unlike the bot's `turn_lock_timeout`:
  this is a person at a keyboard who can press Ctrl-C, and dropping their request after fifteen
  minutes would lose work they are watching.
- **A bot turn RESTORES the executor's approval hook, it does not clear it**
  (`Executor.approval_hook` exists for this). In the embedded mode both front-ends share one
  executor; clearing would leave the terminal with no hook, and no hook means the executor's
  default — allow. A front-end silently losing its approval prompt is the worst way for this to
  fail, and `tests/test_bot_runmodes.py` pins it.
- `tests/test_bot_runmodes.py::test_the_bot_waits_for_the_terminals_turn` drives both paths on
  one project in one process and asserts they overlap and then serialize;
  `test_two_projects_progress_at_once` asserts the opposite for two projects.

### MCP servers (`app/mcp/`)

stdio transport only. `MCPManager.connect_server()` runs a background asyncio task
(`MCPServerConnection._run`) that holds the stdio session open via an `asyncio.Event` gate; tools
are discovered (`list_tools()`), wrapped as async `ToolDefinition`s with `source="mcp:<name>"`, and
registered. `CoderREPL.run()` auto-loads servers from `settings.mcp_config`
(`app/resources/mcp_servers.json`) on startup.

### Bundled resources & packaging (Step 13 / D1)

Prompts, skills, project scaffolds, and the default MCP config live **inside the `app` package** at
`app/resources/{prompts,skills,scaffolds,mcp_servers.json}`, declared as `package-data` in `pyproject.toml`.
So a non-editable **`pipx`/wheel install ships them** — `settings._RESOURCES` (= `<base>/app/resources`,
where `<base>` is the config-dir parent, i.e. the repo root in editable installs and site-packages in
a wheel) resolves them identically in both. `CODER_HOME` still overrides the base. Never load these
from cwd or the repo layout — always via `settings.prompts_dir` / `skills_dir` / `scaffolds_dir` /
`mcp_config`. The `package-data` entry is a recursive glob (`resources/**/*`), so a new resource
*directory* needs no `pyproject.toml` change — but that glob does **not** reliably include
dotfiles, which is why `scaffolds/flask/` stores `.gitignore`/`.gitkeep` as `gitignore`/`gitkeep`
and `scaffold.py` restores the dot on the way out.

### The two-tier build gate (Phase B, `settings.web_intent_fallback`)

`should_blueprint()` is a verb×noun regex, and no noun list enumerates what people build —
"a recipe organizer", "somewhere to track my expenses", "a place my club can post events" all
missed it and silently shipped static HTML with no server and no database. Phase B keeps the
regex as **tier 1** (free, and right when it fires) and adds **tier 2**: one temperature-0,
one-word `YES/NO` call (`_classify_web_build`), reached only when `may_be_web_build()` — a
pure pre-filter — says the message is a genuine candidate. An ordinary turn costs zero extra
calls.

- **`may_be_web_build` is the leash.** It rejects what tier 1 already accepted (no point
  asking twice), questions, splits, *opening* edits, single-file requests, and messages
  opening with a dev command (`_COMMAND_RE` — "run the build" and "deploy the site" both
  carry words the build regexes like). What survives must then show some sign the user wants
  something made: a build verb, or "I need…"/"help me…" phrasing.
- **Anything but a clear YES is NO.** A false positive costs a multi-file build in place of a
  one-line answer, so an LLM error, an empty answer or a hedge all leave routing alone — the
  same rule `intent.parse_verdict` follows.
- **Two vetoes in `should_blueprint` were wrong and are fixed.** `_EDIT_INTO_RE` is now
  ANCHORED: unanchored it matched the trailing clause of ordinary greenfield requests, so
  "build a shop and add reviews to it" was read as an edit and never blueprinted. And the
  single-file veto now yields to `_APP_NOUN_RE`, so "build me a website with a css file for
  the styling" builds while "create a css file" still doesn't. `page`/`form` are deliberately
  NOT app nouns — "a login page" must blueprint, "an html file for the about page" must not.
- **Escape hatch:** `wants_static_only()` ("just html", "no backend", "static only") makes
  `_expand_requirements` use `prefer="none"`, so the build is still planned but proposes no
  backend, no scaffold and no data layer. Refusing to plan it at all would be the wrong shape
  — a static multi-page site is a real request.

**Precedence is load-bearing and unchanged:** `should_amend` still runs first in `chat()`, so
widening the build gate cannot make turn 2 rebuild turn 1's project.

### Building from a referenced document (`settings.max_spec_doc_chars`)

"Build the website described in @PRD.md" is how a real specification arrives, and until now
the document contributed **nothing**. `_read_refs` read it into `_multi_file_flow`'s `context`
— whose only consumer is `_plan_file_ops`, which `_run_blueprint` **skips** because it
preplans the ops — and the two calls that decide what the app *is*, `_extract_schema` and
`_expand_requirements`, were never given it at all. Measured on a 12.5 KB marketplace PRD
specifying auctions, proxy bidding, cash-on-delivery with SMS OTP and a buyer reliability
score: the build came out `products`/`users`/`orders` with a cart. Every table, page and route
had been derived from the one sentence that names the file.

`AgentCore._requirements_doc_context(at_refs)` reads it once at the top of `chat()` into
`self._spec_doc`, and it reaches all three stages. Per-turn state rather than a parameter so
the stages keep the signatures their callers and tests already use — `""` on every turn that
references no prose file, which is what makes them behave exactly as they did before.

- **Prose extensions only** (`_SPEC_DOC_EXTS`: `.md`/`.txt`/`.rst`/…). An `@app.py` on a build
  request is code to work from; quoting it to the schema call as requirements would model the
  *code* instead of the product.
- **The budget is total, not per document** — `_sibling_context`'s rule. A per-file cap is how
  several references overflow the context window and evict the block they were meant to add.
- **Truncation is stated in the block** (`[TRUNCATED: n of m …]`). A silently halved PRD is a
  requirement the build will not have and nobody will know is missing.
- **It is part of the `_extract_schema` cache key.** The same sentence with a different
  document behind it is a different request; the cache is keyed on the message, and would
  otherwise answer a question it was never asked.
- **A greenfield build never TARGETS the document** (`_resolve_ref(exclude_docs=…)`, keyed on
  `should_blueprint`). An @ref pins the edit target, so with the blueprint stage off or
  declining, "build the website described in @PRD.md" resolved its target to the PRD and
  `_file_op_flow` sends an existing file to `_surgical_edit` — the one file the user could not
  regenerate, overwritten with a web page. Off by default because the ordinary case is the
  opposite: "fix the typo in @README.md" must still target the README.

Both prompts state the precedence (`schema.md`, `blueprint.md`): when a document is present the
request is only a pointer to it, so "keep it small" is never a reason to drop a table the
document asks for, and every capability it describes is `requested`, not `optional`.

### Two repairs that were making builds worse

Both were found by that same live build, and both are the same shape: a check whose safety
argument holds for a static site and inverts inside a server-rendered one.

- **`_repair_page_links` must not run inside the stack's template directory.** "The corrected
  target exists as a sibling file" is what makes rewriting `href="/about"` → `about.html` safe
  on a build opened over `file://`. In `templates/` **every route's page exists as a sibling**,
  because that is what a template directory is — so the scaffold's `<a href="/users">` was
  rewritten to `href="users.html"` in `base.html`, and every page of the finished site linked
  at a URL Flask does not serve. `/users` was a route and `templates/users.html` was its
  template; the check could not tell them apart. A `.html` file outside the template dir is
  still a static page, which is the case the pass was written for.
- **`_verify_blueprint_coverage` now also creates the templates a ROUTE RENDERS**
  (`_create_rendered_templates`). Check 1 covers the files the blueprint *planned*; nothing
  covered the ones generation *invented*. The same build added `/signup`, `/cart` and
  `/order/<id>` to `app.py` rendering `signup.html`, `cart.html` and `order_confirmation.html`
  — none planned, none written, three `TemplateNotFound` 500s on links in the site's own nav.
  Nothing else could see it: `_repair_dead_references` reads HTML/CSS/JS and never Python, and
  the endpoint check asks the opposite question ("is the route there?"), which for these three
  it was. Routes are read off the file **on disk** (`routes_from_source`'s rule — the spec is
  additive and would name a template this turn's edit removed), only a **literal** template
  name counts (`render_template(name + ".html")` cannot be resolved, and writing a file for a
  guessed name is worse than the 500), and a name that escapes the template directory is
  skipped rather than written outside it.
- **`_wire_missing_endpoints` acts on the unwired-endpoint report instead of only printing
  it** (`settings.wire_missing_endpoints`). The blueprint plans eleven routes; the model's one
  surgical edit to `app.py` lands six; the pages the SAME build wrote then 500 on
  `url_for('new_category')`. Coverage already computed that list deterministically — reporting
  a defect this specific and this fatal while declining to act on it is a check that never
  runs. Five rules keep it from being the churn the report was protecting against: **one
  attempt, never a loop**; **nothing missing means no LLM call and a byte-identical file**;
  **an edit that fails `check_file` is reverted** (`_intent_repair`'s rule, and it matters more
  here — every page is downstream of this one file, so a bad edit is a total outage rather than
  one 500); **the instruction NAMES what is missing** rather than asking the model what it
  thinks the app needs; and **what is still missing afterwards is recomputed from disk and
  reported**. `_unresolved_view_names` is the Flask half (Jinja names a route by its VIEW) and
  runs at the END, when the entry file is final — the same pass writes the pages and edits
  `app.py`, so a name is legitimately unresolved mid-build and only a defect once it has
  stopped. It returns `[]` for `.ejs`, where linking is by PATH and `check_links` owns the
  question.
- **The `/` route is re-asserted at the END of `chat()`, not only in `_restore_scaffold_invariants`**
  (`_restore_entry_route_note`). That pass runs inside `_run_blueprint`; coverage, the endpoint
  wiring above and the **smoke repair** all rewrite the entry file wholesale afterwards, and
  each deletes the route straight back out. Measured across three live builds: the answer said,
  truthfully, that the home page had been restored and the finished site still 404'd on its own
  front door. **The invariant belongs wherever the file stops being rewritten, not at the point
  it was first broken.** `restore_entry_route` returns `(source, False)` when `/` is still
  routed, so this is idempotent and a build that never lost it is untouched.
- **`_fix_macro_import` — Jinja's `import` is NOT inherited.** `base.html` importing
  `_macros.html` does nothing for a child, so a page calling `ui.field(...)` without its own
  import line is `UndefinedError: 'ui' is undefined`: a 500 on a file that parses, balances,
  passes the intent judge and looks exactly like the pages that work. `ui_context()` states the
  requirement and a 7B drops it anyway — measured on one page of fourteen, which is the worst
  frequency a defect can have. Stage 0 of `_verify_and_repair`, deterministic, and narrow in
  `_repair_missing_imports`' sense: it fires only when the file USES the name and does not bind
  it, and the line inserted is fixed text. It goes **after `{% extends %}`** — a statement above
  it is outside every block, so the import would parse and still not bind. `.html` only: EJS has
  no macro construct and `ui` is a required module there.

### Schema first, layout derived from it (Phase C, `settings.schema_first`)

Phase C of `docs/always-fullstack-plan.md`. The schema used to arrive as free text inside the
blueprint's own answer (`"users(email TEXT PRIMARY KEY, …)"`), so the pages and the tables
backing them were invented in the same breath and agreed only by luck. It is now decided
first, on its own:

1. **`_extract_schema`** — one temp-0 call against `prompts/schema.md`, parsed by the pure
   `projectspec.entities_from_data`. Returns `()` on any failure, and everything downstream
   then behaves exactly as it did before Phase C.
2. **`_expand_requirements(msg, entities)`** — the layout call is handed the tables as a
   block it plans AROUND. When entities are present they are **authoritative**:
   `contract.data_schema` is printed from them rather than taken from the model's own free
   text, so the tables `crud.py` generates and the tables the prompt describes cannot differ.
3. **`blueprint.derive_pages_from_entities`** — pure, no LLM. Every entity gets a list page,
   a create form, and the routes behind them. A prompt rule is a hope on a 7B model (a
   four-table request routinely comes back with pages for two); this makes "every entity is
   reachable" a postcondition, the same way `scaffold.py` and `crud.py` do for boilerplate.
4. `ProjectSpec.from_blueprint` then takes `Page.reads` and `SpecEndpoint.entity` from what
   the layout call **declared**, with the old prose inference and `_guess_entity`'s substring
   match demoted to fallbacks.

Four rules that are easy to get wrong, three of them learned by breaking them here:
- **Routes are synthesized only for templates this pass creates.** Routing the model's own
  pages too breaks both ways: `GET /<table>` lands beside the model's own listing route (one
  of them rendering a template nobody created), and the coarse "this entity already has a
  GET" guard that was meant to prevent that swallowed `GET /<table>/new`, leaving the form
  page unreachable — the exact dead end the pass exists to prevent.
- **A synthesized form gets BOTH routes.** GET serves it, POST accepts it; with only the POST
  the page cannot be opened at all.
- **`IMAGE`/`FILE` are not SQLite types**, so `_norm_type` collapses them to TEXT — which
  would throw away the only signal that the column holds an upload. `_fields_from_data`
  moves the signal into the *name* (`cover` → `cover_path`), because `Field.is_upload`,
  `crud.upload_helper_source` and `verify.fix_form_enctype` all key off the name.
- **Form-word detection tokenizes the stem**, it does not use `\b`: `_` is a word character,
  so `\badd\b` does not match `add_product` and the model's own form page reads as a listing,
  earning the entity a second, duplicate form.

`blueprint.py` must NOT import `projectspec` at runtime (that module imports it) — `Entity`
is a `TYPE_CHECKING`-only import, which works because `from __future__ import annotations`
keeps every annotation a string. **`conftest.py` defaults `schema_first` OFF in tests** for a
sharper version of the `check_intent` trap: it runs *before* `_expand_requirements`, so a
test that opts back into the blueprint stage and patches only that method would still reach a
real ChatOllama.

### Two stacks behind one seam (`app/agent/stacks/`, Phases N0–N6)

`docs/node-stack-plan.md`. There are two named stacks — `flask` (Python / Flask /
Jinja2 / sqlite3, the default) and `node` (Node / Express / EJS / PostgreSQL) —
and **nothing outside `app/agent/stacks/` decides between them**. `core.py` used
to write `workdir / "app.py"` at four call sites, `workdir.glob("*.py")` at a
fifth and `[sys.executable, "seed.py"]` at a sixth; those all read
`self._adapter` now.

- **`FlaskAdapter` is delegation only.** Every method forwards to the function
  that already did the job (`scaffold_flask`, `crud.models_source`,
  `impact.migration_block`, `projectspec.routes_from_source`). That is what makes
  N0's exit criterion checkable — the whole suite passes **unmodified**. A method
  in `flask_adapter.py` that grows a branch of its own has become a rewrite, and
  the guarantee stops being provable.
- **`get_adapter()` is total.** `""`, `None`, `"auto"` and a typo all return the
  Flask adapter. Specs written before the seam have no stack key, and a KeyError
  there would turn a missing field in an old `project.json` into a dead turn.
- **The spec outranks the setting** (`stacks.resolve_key`, pinned once per turn
  in `chat()` via `_select_stack`). Opening a Node project with `web_stack` left
  at `flask` would otherwise send `_amend_project` to write Python
  `ensure_column` calls into a `db.py` that does not exist — a silent, total
  failure on turn 2. `spec.language` is checked before `spec.backend`, because
  `runtime_probe._node()` reports `backend="stdlib"` with the network off and
  that collides with the *Python* stdlib stack.
- **`resolve_key` and `probe_prefer` are different questions and must stay
  apart.** The first answers "which adapter" (closed set, Flask default, so
  `stdlib` → Flask). The second answers "what to pass `detect_stack(prefer=…)`"
  and must preserve `stdlib`/`fastapi`/`auto`/`none`. Conflating them silently
  rebuilds a stdlib project as a Flask one.
- **Two named stacks, never two axes.** Database and framework are not
  independent; four combinations would mean four scaffolds and a test matrix
  nobody maintains.
- **`style.css` and `theme.css` are byte-identical copies**, and `write_theme`
  takes the stack's relative path. They are pure custom properties, so all of
  W1's design-system work transfers for free — fork them and the two stacks stop
  looking like one product. `ui.js` carries the **same helper names** as
  `_macros.html` for the same reason: `ui_context()` has to say the same thing on
  both stacks. It is a `.js` module rather than the plan's `views/_macros.ejs`
  because EJS has no macro construct — an included partial cannot export
  callables — and the call site is identical either way
  (`{{ ui.table(...) }}` / `<%- ui.table(...) %>`).
- **The Node adapter states its gaps, and `/stack` prints them.** They are real
  and they are printed verbatim: no template-scoped editing, the *import*
  dependency graph stays Python-only, routes are read with a regex rather than a
  parser, no duplicate-definition check, and missing-require repair binds only
  builtins and this project's own modules (an npm package is reported).
  A menu that listed the two stacks as equals is how a demo gets built on the
  weaker one by accident. **This list must be maintained as the gaps close** —
  N4 landed three of the things it denied (route validation, the `.ejs` check,
  spec adoption) and the list went on saying they were missing, which is worse
  than having no list: it tells someone choosing a stack the opposite of the
  truth. `tests/test_stacks.py::test_the_gaps_are_gaps_the_code_really_has`
  checks each claim against the code so it cannot go stale silently again.
- **One entity model, two dialects** (`projectspec.Dialect`, `SQLITE` /
  `POSTGRES`, Phase N3). `crud.py` and `crud_node.py` emit from the SAME `Entity`
  objects; the only things allowed to differ are the five in that table
  (placeholder `?` vs `$1`, `INTEGER PRIMARY KEY AUTOINCREMENT` vs
  `SERIAL PRIMARY KEY`, the type map, `lastrowid` vs `RETURNING id`, and the
  migration call). Two DDL writers would be two schemas wearing one spec.
  `SQLITE` is the default on every method, so Flask's output is byte-identical.
  - **`REAL` maps to `NUMERIC`, not to PostgreSQL's `REAL`.** The latter is a
    4-byte float, and `REAL` in a generated schema is overwhelmingly a price.
  - **`crud_node.js_strings` closes the `_creates_table` trap for JavaScript.**
    `db.js` ships a *commented* `CREATE TABLE ... widgets` example exactly as
    `db.py` does, and counting one as real already cost the Flask side a live
    build. There is no Python-side JS parser, so it strips comments and reads
    string literals, erring toward reporting FEWER literals — writing a
    `CREATE TABLE IF NOT EXISTS` twice is a no-op, skipping one is a dead app.
  - **`passwords.js` is generated, not prompted.** Node has no
    `werkzeug.security`, and `bcrypt` means a native build on a machine whose
    point is that it works offline. `crypto.scrypt` needs nothing installed, is
    salted per password, and compares with `timingSafeEqual`.
  - **`derive_pages_from_entities` / `derive_home_page` run for any stack with a
    SCAFFOLD**, not just Flask — a fixed `templates/`+`@app.route` or
    `views/`+`app.get` layout is what makes a synthesized path correct. stdlib
    and fastapi still derive nothing, because there the file layout is the
    model's own and a guessed filename is a file nothing serves.
- **`adapter.readiness(root)` skips the smoke test rather than failing it.**
  Node + Postgres has four ways to be un-runnable where Flask has one (no node,
  no `node_modules`, nothing listening on 5432, and a database that does not
  exist or will not take the credentials), and none of them is a defect in
  the generated code — without this, `_smoke_repair_instruction` sends the model
  to rewrite correct code because `npm install` was never run. **The skip is
  reported as `may not meet:`, never as a pass.** Flask returns `""` always, so
  no check that used to run is now gated.
- **The generated Express app exits non-zero when `initDb()` fails.** Starting
  anyway would serve `/` with a 200 while every data page 500s — a build that
  reports success and is broken, which is the one failure this codebase exists to
  prevent.
- Scaffold dotfiles follow the existing packaging rule: stored as
  `gitignore`/`gitkeep`, dot restored on write.

**Verification for Node (Phase N4).** Four checks the Node stack was missing, all
of them the Flask equivalent restated rather than reinvented:

- **`.ejs` structural checking** (`verify.strip_ejs` → `_check_ejs_text`, wired
  into `check_text` and `is_verifiable`). The JavaScript comes **out first** —
  `<% if (a) { %>` is not an element — and then the markup underneath is
  balanced by the same parser `.html` uses. An unterminated `<%` is *the* EJS
  error and it kills the page at render time with "Could not find matching close
  tag"; nothing else in the pipeline can see it, because the file is valid-ish
  HTML, `node --check` does not read it and the browser never gets that far.
  Measured: `<%# … #%>` (there is no `#%>`) broke the scaffold's home page during
  N2. Two rules keep it from false-failing: a view may be a **fragment or a whole
  document** (every view except `layout.ejs` is a fragment, so requiring a
  document would fail all of them), and the prose guard is narrow (below).
  - **The JavaScript is checked too** (`verify.ejs_script` → `_check_ejs_javascript`,
    in `check_file` beside the `.js`/`.ts` cases). `strip_ejs` throws that half
    away — it takes the code OUT so the markup can be balanced — and the code is
    where views really break. Measured on a live build: `views/users.ejs` shipped
    `<%- users.forEach(user => { %>` (an OUTPUT tag around a statement, so EJS
    emits `__append(users.forEach(u => {)`), `views/items.ejs` had a sentence of
    prose welded into a call's argument list, and both files were structurally
    perfect markup that passed every check and threw at render — which on that
    build meant the home page, `/users` and `/items` were all 500s. `ejs_script`
    does EJS's own translation (`<% %>` → code, `<%= %>`/`<%- %>` →
    `__append(x);`, `<%# %>`/`<%%` → nothing, markup → nothing) and wraps it in
    an `async function` so a legitimate top-level `await`/`return` parses. Then
    `node --check` on a temp file — never the view itself. **A missing `node` is
    a skip, exactly as for `.js`**: a check that could not run has verified
    nothing, and calling that "broken" blocks a correct write.
  - **The prose guard is "no markup AND no EJS tags", never "looks like prose".**
    A hero paragraph is a legitimate fragment, unlike in a `.js` file where prose
    means the wrong kind of content entirely — so the general guard stays off.
    But a file with neither a tag nor an element renders literally nothing and
    can only be the model answering *in* the file instead of writing one.
    Measured: "fix the files inside users.ejs" produced a `users.ejs` whose whole
    content was "To address the request … I need more information about the
    specific issues", and it passed as a valid view.
- **Link validation dispatches through `adapter.check_links(text, routes)`**, on
  both adapters, from `core._check_endpoints` — which accepts **the stack's own
  `template_ext`** as well as `.html`/`.htm`. That gate is load-bearing: filtered
  on `.html` alone it returned `""` for every `.ejs` view before `check_links`
  was ever reached, so the Node half of the check was written, tested at the
  adapter and dead at the call site — which reads exactly like a passing one.
  `template_ext` is `.html` on Flask, so that stack is unchanged. Jinja names a
  route by its VIEW
  (`url_for('products')`), EJS by its PATH (`href="/products"`); the rules are
  identical either way and are W2's — repoint only an unambiguous near miss
  (`references._name_key`, **exactly one** candidate), report everything else,
  because sending a link to the wrong page is worse than the 404 it replaces.
  The path lookup allows for `:id` segments. **A form is judged against the UNION
  of every matching route, not the first match** — `/products/:id` also matches
  `/products/new`, so first-match reported a genuine `POST /products/new` handler
  as a 405, a false failure on correct code. Empty route list = report nothing:
  that means the parser could not read the server file, and calling every link
  broken would be the flood, not a finding.
- **The template graph generalised** (`templatedeps.parse_ejs_template`;
  `build_graph` gained `template_dir` / `template_ext` / `parser` /
  `routes_reader`). **Every keyword defaults to the Flask layout**, so existing
  callers and tests are untouched — N4 added arguments, it did not change a
  default. `views` (the Python view bodies) is read **only when `parser is
  None`**: `view_bodies` is `def name():`-shaped and running it over another
  language's entry file would produce garbage edges rather than missing ones, so
  a Node graph simply has none. The load-bearing rule survives the port —
  identifiers come from EJS expressions with **strings stripped first**, so
  `layout.ejs`, whose nav says "Products" and whose href is `/products`, is not a
  reader of every entity. `_resolve` falls back to matching a **stem** when the
  name carries no extension at all (Express writes `res.render("products")` and
  `include("_filters")` where Jinja always writes the full filename); it is
  reached only after every name match has come back empty, so it can turn a `""`
  into a hit and never one answer into a different one.
- **`ProjectSpec.from_disk` adopts a Node repo.** Python/Flask is tried first and
  is unchanged, so an existing Flask repo adopts exactly as it did; Node is
  reached only when the Python pass finds no routes at all. Routes come off
  `server.js`, tables off `db.js` (via `crud_node.js_strings`, the JS half of the
  `_creates_table` trap), and `views/` + `.ejs` turns `res.render("products")`
  back into `views/products.ejs`. Every D1 rule still holds: it **declines**
  without a real route, so a plain JS folder never acquires an invented contract;
  it records only what it can SEE; and it **saves nothing**.

**Proving the database, not just the port (Phase N5, `NodeAdapter.database_reason`).**
A socket to 5432 proves *a* server is listening. It does not prove that **this
project's** database exists or that its credentials work — and both fail inside
`initDb()`, which the generated app treats as fatal. Before N5 that surfaced as
"the smoke test failed", for a reason that is not in the code at all.

- **The probe runs through `node` and the project's own `pg`, not a Python
  driver.** `node_modules` has already been established by the time it runs, `pg`
  is the project's own dependency, and this needs nothing installed on the Python
  side — the same reasoning that makes `crypto.scrypt` the password helper. It is
  a `node -e` string, not a file written into the project: readiness is a READ,
  and dropping a script into someone's repo to answer a question about it is the
  side effect `from_disk` refuses to have. `require` resolves from cwd under
  `-e`, which is why the subprocess runs with `cwd=root`.
- **It reads the connection string out of the project's own `db.js`.** A probe
  that guessed its own URL could pass while the app fails, or fail while the app
  works — either way it would be measuring something other than the thing that
  has to work.
- **Every uncertainty resolves to `""` — run the check.** No `node`, no URL to
  try, a crash, a timeout, unparseable output: all of them mean "we could not
  find out", and skipping is only correct when we KNOW the environment is at
  fault. The smoke test is the real measurement; a probe that could not complete
  must never replace it.
- **A `db.js` that will not load is a CODE defect and must never read as an
  environment one** — reporting it here would skip the only check that can
  report it. The probe therefore tells an *absent* `db.js` (fine: an adopted repo
  may configure the URL elsewhere, so fall through to `DATABASE_URL`) apart from
  a *broken* one (`CODER_BAD_DBJS` → "cannot tell"), instead of collapsing both
  into one try/except. Collapsing them made the rule hold only by accident —
  it broke the moment `DATABASE_URL` was set in the environment.
- **The SQLSTATEs are the whole point of the phase**: `3D000` (invalid_catalog_name)
  becomes "the database *X* does not exist — create it once with `createdb X`",
  `28P01`/`28000` become a credentials message, and an unrecognised code is still
  *reported* rather than swallowed. `tests/test_crud_node.py` has a live tier
  asserting PostgreSQL really emits `3D000` for a missing database — the offline
  tests drive a fake `pg`, so they prove the handling, not that assumption.
- **`node --check` on `_PROBE_SCRIPT` is not ceremony** (`pageaudit.py`'s lesson):
  a syntax error there fails silently and in the worst direction — the probe
  returns None, that reads as "cannot tell", and every readiness check reports a
  clean environment forever.
- Ordered cheapest-first, and each step is a precondition of the next: the
  subprocess runs only once node, `node_modules` and the port have all passed.
  Bounded by `settings.db_probe_timeout` (6s), because an unreachable host hangs
  far longer than a refused connection.
- **`/run` consults it too** (`app/cli/commands.py`), so a missing database is
  named *before* launching rather than relayed as "server.js exited on startup".

### Forcing the stack (`app/agent/runtime_probe.py`, `settings.web_stack`)

Phase A of `docs/always-fullstack-plan.md`. `detect_stack()` used to *probe* — richest
importable framework wins — so the full-stack promise silently depended on Flask being
importable, which it is here only because Coder's own environment installs it. `web_stack`
(default `"node"` since 2026-08-04; `"flask"` before that) is now passed as `prefer=` at both
call sites, and `"auto"` restores probing.

> **The session default is `node`; `stacks.DEFAULT_KEY` is still `flask` and must stay so.**
> They answer different questions. `DEFAULT_KEY` is what an empty/unrecognised/missing key
> resolves to — and a spec written before the stack seam has no key at all, so resolving those
> to Node would reinterpret every existing Flask project as an Express one on its next turn,
> writing `ensure_column` calls into a `db.js` that does not exist. An accident should land on
> the better-verified path; a deliberate choice is what `web_stack` expresses.

**A forced stack is never silently downgraded.** `prefer="flask"` with Flask absent returns
the Flask stack with `runnable=False` and an `install_hint`, NOT the stdlib stack —
downstream cannot tell a downgraded build apart from one that was always meant to be
stdlib, which is the same silent-truncation failure class `blueprint_max_files` and the
coverage check exist to prevent. Two consequences, both deliberate:
- **`install_hint` is separate from `note`, and must stay separate.** `note` is the
  generation instruction and still says "build on Flask"; `prompts/blueprint.md` tells the
  model *not* to use a framework that isn't available, so folding the warning into `note`
  would make it quietly write a stdlib app — the exact downgrade being reported.
  `_run_blueprint` leads its answer with `install_hint`; the model never sees it.
- **The smoke test is skipped when `stack.runnable` is False.** The app would die on
  `import flask` and `_smoke_repair_instruction` would send the model to rewrite code that
  is correct.

Flask is declared in `pyproject.toml` for this reason — it is the *generated* apps' runtime
(`smoke.py`/`apprunner.py` launch them with `sys.executable`), not something Coder imports.

### Deterministic project scaffold (`app/agent/scaffold.py`)

The highest-leverage rule in `docs/fullstack-web-plan.md`: **deterministic beats generated.** Before
a blueprint build generates anything, `_run_blueprint` copies a real, runnable Flask skeleton
(`app.py` / `db.py` / `models.py` / `seed.py` / `templates/base.html` + `index.html` / `static/` /
`requirements.txt` / `Procfile` / `.gitignore`) into the project — no LLM call, so no failure mode.
The app therefore **starts and serves `/` with a 200 before the model has written a line**. This
exists because the Phase 0 baseline measured the 7B model getting hand-written Flask *boilerplate*
wrong ~1 build in 4 (`routes.py` using `@app.route`/`sqlite3`/`DATABASE` without importing any of
them — `docs/phase0-baseline.md`).

Three properties are load-bearing:
- **It never overwrites.** Re-running is a no-op, so an amendment turn cannot revert turn 1's work.
- **Only `_FROZEN` files are dropped from the build plan** (`requirements.txt`, `Procfile`,
  `.gitignore`, `.gitkeep`). Everything else the scaffold wrote stays planned and gets **edited** on
  top of the working skeleton — `_file_op_flow` routes an existing file to `_surgical_edit`, so the
  model adds this project's routes to a running `app.py`. Freezing them all would ship the
  placeholder home page as the finished site.
- **The home page is planned deterministically** (`blueprint.derive_home_page`). Staying out of
  `_FROZEN` was necessary but not sufficient: a file is only edited if something *plans* it, and
  nothing planned `templates/index.html`. The layout call is not asked for a home page (the Flask
  layout table named `templates/<page>.html`, and no prompt said "home page"), and
  `derive_pages_from_entities` covers entities — which the home page is not one of. So the scaffold
  wrote it once, `scaffold_flask` declined to overwrite it, and **every build shipped the identical
  "This project was scaffolded by Coder" hero as its front door.** `derive_home_page` runs after
  `derive_pages_from_entities` so it can link the routes that pass just added; it plans an **edit**,
  it links listing pages but not form or parameterised routes ("add a product" is a button on the
  products page, not a section of the site), and **whatever the layout call planned wins** —
  planning the file twice would put it through two generation passes, the second overwriting the
  first. Flask only, for `derive_pages_from_entities`' reason. `blueprint.md` also asks for the file
  now, and `scaffold_context` no longer calls the placeholder "the home page", which read as done.
- **Placeholder substitution is exact-literal**, not a template engine: `{{PROJECT_NAME}}` /
  `{{SECRET_KEY}}` share Jinja's delimiters, and `{{ url_for(...) }}` must survive into the
  generated project untouched.

Gated by `is_web_app(blueprint)` (a runnable backend **and** an endpoint or a page) plus
`stack.backend == "flask"`, and best-effort — a scaffold failure costs nothing but today's behaviour.

**Generation then breaks the skeleton, so `_restore_scaffold_invariants` puts it back** (end of
`_run_blueprint`, deterministic, no LLM). A 7B model's SEARCH/REPLACE replaces the block it was
asked to add to: measured on *every* live build, the edit to `app.py` deleted the scaffold's `/`
route, leaving the finished site 404ing on its own home page. `restore_index_route` re-adds it —
declining rather than guessing when the file isn't a recognisable Flask app, when `/` is still
routed, or when `render_template` isn't imported (a synthesized route that raises `NameError` would
be worse than the 404 it replaces). `convert_to_child_template` rewrites a page that shipped as a
full `<html>` document into `{% extends "base.html" %}` + `{% block content %}`, dropping the
`<header>`/`<nav>`/`<footer>` base.html already renders — leaving them renders *two* navbars, which
is worse than the drift the layout exists to prevent. Related: `base.html` links home with a literal
`/` rather than `url_for('index')` **on purpose** — a BuildError there fires on every page, so one
deleted route would 500 the entire site instead of 404ing one page.

### The design system — components shipped, not generated (Phase W1)

`docs/web-quality-plan.md`. The scaffold made the app *run* before generation; this makes it
*look* finished. Same principle one layer up: a table, an alert and an empty state contain no
decisions, and a 7B asked to invent them writes a different one on every page.

- **`static/css/style.css` is the component sheet** (~480 lines): `.table-wrap` + `.table`,
  `.alert-*`, `.empty`, `.badge`, `.card-media`, `.grid`/`.stack`/`.cluster`,
  `.sidebar-layout`, `.breadcrumb`, `.pagination`, focus rings, reduced-motion. Every rule is
  written in **custom properties**, never a literal — `test_component_sheet_is_written_in_variables_not_literals`
  fails the build otherwise, because a rule naming `#2563eb` is a rule a restyle cannot reach.
- **`templates/_macros.html` is the markup library** — `page_header`, `table`, `card`,
  `field`, `badge`, `empty_state`, `flash_messages`. `_FROZEN`, like `requirements.txt`.
- **`static/css/theme.css` holds every colour/font token and is linked LAST**, so it wins.
  That is also why the **dark scheme lives there and not in style.css**: a `@media` block in
  the earlier sheet loses to a plain `:root` in the later one, and the site would be stranded
  in light colours with no visible cause.
- **A requested style is written, not described.** `buildspec.resolve_theme(message)` →
  `theme_tokens` → `theme_css` → `scaffold.write_theme`, all deterministic, called from
  `_run_blueprint` beside the scaffold copy. `to_context_block` still *states* the palette so
  the model knows which variables exist, but the look no longer depends on it complying.
  A message with **no style words and no colours yields no theme** — inventing a look nobody
  asked for is the failure `_clean_nav` exists to prevent.
- **Colours the request names outright beat the preset it also matched**
  (`buildspec.find_requested_colors` / `palette_from_colors`). The preset table answers "what
  does *pastel* look like" and had no answer for the most direct way anyone asks — naming the
  colours. "a dark blue and gold colour scheme" and "a purple and white palette" matched no
  preset, so `resolve_theme` returned `{}` and the *default* theme shipped; "a modern site in
  purple" matched the modern preset and came out blue. Named colours now supply the palette and
  the preset keeps supplying the typography, so "an elegant site in navy and gold" stays serif.
  Three rules: a colour word only counts **in a styling context** (a style word, a restyle verb,
  or a colour/palette/theme word — otherwise "a green energy company" repaints the site, which is
  `_clean_nav`'s failure in another hat); a **literal hex needs no such evidence**; and the page
  stays **light unless the request asked for dark**, because "a dark blue and gold site" usually
  means navy on white and the conservative reading is the one that cannot ruin the build.
- **Six presets were added for style words that already parsed but matched nothing.**
  `_STYLE_WORD_RE` recognised `warm/cozy/rustic`, `professional/corporate`, `neon`,
  `industrial/brutalist`, `muted/monochrome` and `airy` — so `find_style_keywords` reported a
  styled request — while no `_STYLE_PRESETS` pattern matched them and `_preset_for` returned
  `{}`. The style was "understood" and the default look shipped.
- **A restyle works on later turns** (`buildspec.wants_restyle` → `core._restyle_project`).
  `write_theme`'s only caller sat beside the scaffold copy, and `scaffold_flask` returns nothing
  once the files exist — so from turn 2 on, every restyle was deterministically a no-op, on a
  demo that is built in parts. `_amend_project` now runs the restyle **before** the delta call,
  because a pure restyle produces an empty delta and would otherwise return None and defer to
  routing that rewrites no theme; `should_amend` accepts restyle wording (yielding to
  `should_blueprint`, so a greenfield request naming a look still builds); and `_run_blueprint`'s
  theme write is no longer gated on `scaffolded`. The guard that keeps a hand-tuned theme safe is
  now `wants_restyle` — it needs restyle wording **and** a theme that resolves, so "make it
  responsive" matches the first test and is correctly not a restyle.
- **`theme_tokens` decides which END of the palette is the page before assigning roles.**
  Reading "lightest = background" is right until a dark palette arrives: the `dark mode`
  preset is mostly dark, so its lightest entry is the *text*, and assigning that to the page
  turned "build me a dark mode dashboard" into a light theme with a blue surface.
- **`_ensure_contrast` moves the accent along its own lightness axis until it clears WCAG AA**
  against the page, keeping the hue. `test_every_preset_is_legible` asserts text-on-page,
  accent-on-page and label-on-accent all clear 4.5:1 for all 17 presets.
- **`scaffold.ui_context()` is not optional** — the `crud.api_context()` rule again. Shipping
  components without telling the model they exist does not stop it writing its own
  `.product-card`; it just means the site now has two design systems. Two drift tests keep the
  block honest: every macro on disk is advertised, nothing that isn't a macro is, and every
  class it names is really defined in style.css. A block that drifts is worse than none — it
  names a macro that doesn't exist and the page 500s on render.

### Endpoint and form-method validation (Phase W2, `verify.py` + `core._check_endpoints`)

`{{ url_for('product') }}` against a view named `products` is a Jinja `BuildError` — a 500 on
that page, from a file that parses, renders in isolation and passed every check that existed:
`references.py` deliberately skips `url_for` (it cannot tell a route from a file path), the
syntax check only balances tags, and the functional probe sees the 500 without ever being able
to name the endpoint as the cause. Runs as a **stage-0 deterministic fix** in
`_verify_and_repair`, `.html` only.

- **Known routes are read off `app.py` on disk** (`routes_from_source`), never off the
  ProjectSpec: the spec is additive by design (`reconcile_with_disk` never deletes), so it can
  name a route this turn's edit removed, and a check that trusted it would go quiet on exactly
  the page that just broke.
- **A near miss is repaired, anything else is reported.** The key rule is inherited from
  `references._name_key` — punctuation dropped, one trailing plural collapsed — so
  `product`→`products` and `addproduct`→`add_product` are naming slips, while
  `edit_product`→`add_product` and `product_list`→`products` are **different handlers** and
  are left alone. Rewriting only happens when **exactly one** candidate matches. Sending a
  form to the wrong handler is worse than the 500 it replaces.
- **`form_method_mismatches` reports, never fixes**: a `<form method="post">` whose action
  route is GET-only is a 405 that every other check passes, but the repair is
  `methods=["GET","POST"]` in app.py — a Python edit, and this pass runs per HTML file. A form
  with **no** action is not judged: which route it posts to cannot be known from the template,
  and a false failure here would send the repair loop to rewrite working code.

### The browser layer (`app/agent/browser.py`, Phase W4)

Every check before this one reads **bytes** — `verify.py` parses a file, `references.py` scans
hrefs, `smoke.py` fetches with `urllib`. None lays out CSS or executes JavaScript, so a table
900px wide inside a 390px viewport, a `<button onclick="addToCart(1)">` with no `addToCart`,
and a `<script src>` that 404s at runtime were all structurally invisible.

`browser_session()` yields a `Session` (`probe` / `screenshot` / `click`) or **None**, so
every caller degrades with one `if session is None` — the shape `vision.py` uses for a missing
model. `PageProbe` carries facts and **no verdicts**: thresholds belong to W5/W6 as pure
functions over a probe, which is what lets them unit-test with no browser.

- **Playwright's Chromium download needs the network once.** This is a real, deliberate
  exception to the offline rule — no pure-Python renderer does modern CSS layout. Handled like
  a missing Flask: `settings.browser_checks` ships **off**, `install_hint()` is loud and
  separate from any generation instruction, and a machine without a browser behaves exactly as
  it did before. **A skipped check must never read as a passing one.**
- **Sync API on purpose**, like `smoke.py`, called with `asyncio.to_thread`. Playwright's sync
  API refuses to run in a thread owning a live event loop, and the `to_thread` worker doesn't
  own one. Don't "fix" it to the async API without moving every call site.
- **Localhost only, checked before launch.** This renders pages and runs their JS; pointed at
  an arbitrary URL it is a web client executing untrusted script.
- The two tests that drive a real browser are gated on `importorskip("playwright")` and skip
  here, so **nothing in this stack has ever run against a real DOM on this machine.**

### Layout audit + the dead-button probe (`app/agent/pageaudit.py`, Phases W5/W6)

`browser.py` reports facts; this turns them into findings. Every decision is a **pure
function over a `PageProbe`** (`horizontal_overflow`, `low_contrast`, `console_findings`,
`triage_controls`, `click_findings`), which is what lets 65 tests run with no browser.
`audit_site()` is the only part that drives one: one session, one navigation per page per
width, then one per control. It runs through `run_smoke_test(on_serving=…)` — inside the
server window the smoke test already opened, because two servers fight over :5000 and
`app.db`.

**A false failure is worse than no check** — `functional_probe` step 3's lesson, and it cost
three checks their obvious form:
- **An element past the viewport's right edge is not a defect; a page that SCROLLS is.**
  `.table-wrap` is a horizontal scroll container by design, so the obvious check would fail
  every page that uses the component W1 shipped. Overflow is judged on `document.scrollWidth`
  and the offending elements are named as the culprit, because a measurement with no culprit
  is unactionable.
- **Contrast applies WCAG's large-text allowance (3:1 at ≥24px, or ≥18.66px bold)** and is
  *skipped* whenever the backdrop is unknowable — a background image, any translucent layer.
  The ratio, size and weight come from the page; the threshold lives in Python, per W4's rule
  that no verdict lives in JavaScript.
- **`Finding.severity`**: an image with no intrinsic size is a `warning` (an image that hasn't
  decoded measures the same as one that never will), and warnings never reach the repair loop.
  `empty` fires only when `<main>` has no text, no media *and* no element children — an empty
  listing table is a page that rendered.

**Forms are never submitted by the browser, and the skip is reported.** Native validation
blocks a submit with an empty required field, so "nothing changed" would fail a *correct*
form; and a POST that went through would insert a second row behind the checks asserting
against seeded data. `functional_probe` already posts to every write endpoint with real
values and requires the value to come back — what the browser adds is the *control*, so that
is what it probes. A GET form with no empty required field is submitted (that is the demo's
search box). `Session.click` reports `innerHTML` length as well as `innerText`: a handler that
only toggles a class moves neither the URL nor the text, and a check on the text alone would
call a working tab switch dead.

**`ProbeCheck.owner` is why browser findings don't corrupt the smoke repair.** They ride in
the smoke result's check list (which is how they reach the answer for free), but
`_smoke_repair_instruction` rewrites **app.py** — so "the products table scrolls sideways" would
have sent the model to fix a CSS problem in the server file and then report success. Only
`owner == "app"` failures go there. The rest go to `_repair_browser_findings`, targeted at the
page's own template via the ProjectSpec and **only when that file exists on disk**
(`_resolve_target_from_spec`'s strictness: a missing path reads as "create this", which would
turn a measurement into a new empty file). Bounded by `settings.max_browser_repairs` (1), and
what it leaves alone it says. `contrast` and `network` findings are **reported, never
repaired**: the palette lives in the frozen, deterministically written `theme.css`, and a
reference to a missing file is `_repair_dead_references`' job — that pass creates the file
rather than rewriting the page that asked for it.

**`node --check` on `AUDIT_SCRIPT`/`CONTROLS_SCRIPT` is not ceremony.** A syntax error there
fails silently and in the worst direction: `Session.probe` swallows the `evaluate` failure,
the key is absent from `probe.data`, and every check then reports a clean page.

### The vision critique on the rendered page (`app/agent/visualcheck.py`, Phase W7)

W5 measures the page and W6 exercises it. This is what is left: it fits, the console is clean,
every button works — and it still looks wrong. **The least reliable stage in the pipeline, and
built to be reverted.** It is `intent.py`'s shape one layer up (a 7B judging a 7B), with the
same four rules:
- **A five-point checklist, never "critique this."** An open prompt on a 7B VL returns "the page
  has a clean and modern feel", then "consider adding testimonials".
- **`intent.parse_verdict` is reused verbatim** — which is why the prompt asks for the literal
  `MISSING:` marker. A second parser is a second thing that can read noise as a defect;
  "unparseable = PASS" is what protects a page that is fine.
- **A complaint must name a visible SYMPTOM and must not ask for new content.** "Add a hero
  image" is a feature request — the same tension `buildspec._clean_nav` resolves.
- **`_guarded_repair` reverts a pass that made things worse.** It snapshots the files a repair
  is about to rewrite, re-measures, and restores them byte-for-byte if the error count rose;
  the restored files are identical to the ones that produced the earlier audit, so that audit's
  numbers are true again with no third server start. This wraps the **W5/W6** repair too.

Screenshots are taken by `audit_site(screenshot_pages=…)` inside the smoke window (the only
moment a live server exists), **viewport not full-page** — a 4000px-tall PNG is downscaled to
illegibility by `vision._prepare_image`'s 1536px cap. `vision.ask_about_image` sends bytes, so
nothing is written to disk. Gated by `check_visual` (off, **and defaulted off in conftest** —
the same trap as `check_intent`/`schema_first`, a third time), `visual_max_pages`,
`max_visual_repairs`.

### The template dependency graph (`app/agent/templatedeps.py`, Phase W8)

`symbols.py` resolved imports for Python only, so the graph stopped at `app.py` and everything
downstream had to answer "which template shows a product?" from `Page.reads` — inferred from
blueprint prose and routinely empty on the listing page that matters. `build_graph(root)` reads
the real edges off disk: `extends`/`include`, route→template, `url_for` targets, form actions,
static assets, and the **entities a template displays**. `impacted_files(…, graph=…)` unions
them with `Page.reads` (never substitutes), and `_resolve_target_from_spec` falls back to them
when no page matches by name.

- **The entity hint takes identifiers ONLY from Jinja expressions, strings stripped first.**
  Otherwise `base.html` — whose nav says "Products" and whose href is
  `{{ url_for('products') }}` — becomes a reader of every entity, and an amendment rewrites the
  site layout to "show price for each product". Layout templates are excluded outright.
- **Matching uses word segments**: a template writes `{% for product in products %}` while the
  view behind a generically-named page writes `models.get_all_products()`. Whole-identifier
  matching finds the first and misses the second.
- **`view_bodies` stops at the next decorator**, not the next `def` — `@app.route("/products")`
  sits between two functions and made `index` look like a page that displays products.
- **Additive and best-effort**: a template the parser cannot read yields no edges, never a wrong
  one. `symbols.py` stores the same edges (`.html` files and `render_template("x.html")`) as
  `imports` rows, so `dependencies`/`dependents` work for templates; a template contributes **no
  symbols** — every page defines `{% block content %}`, so indexing block names would make
  `find_symbol("content")` return the whole site.

### Best-of-N and roles per model (`app/agent/candidates.py`, Phase W9)

Offline on a 7B, sampling is the only lever left: generate a high-value file twice at
`best_of_temperature`, score both with the **deterministic** checks, keep the winner. Sound only
because W2/W5/W6 made those checks objective. `verify.check_file` was split into `check_text`
(tooling-free, takes a string) plus the node/tsc step, so a candidate can be scored before
anything is written.
- **Default `best_of_n=1` — off.** Every request already costs minutes; doubling that is the
  user's choice, and the answer says what it cost.
- **Ties go to the first candidate.** N>1 must never make a build merely *different*.
- **Nothing while streaming** (the user is watching #1's tokens), and nothing for a file where a
  defect is cheap (`is_high_value`: `.html`/`.py` only).
- **The `url_for` score is skipped, not guessed, when `app.py` is absent** — an unknown endpoint
  set makes every candidate look broken. **`unresolved_local_calls` is not scored at all**:
  `app.py` legitimately calls a `models.py` helper the build writes two files later.
- **`_llm_planner` / `_llm_judge` are properties**, returning `_llm_blueprint` / `_llm_edit`
  until `planner_model` / `judge_model` name something. An attribute captured at construction
  would silently hold the pre-`set_model` instance and defeat every test that patches those.
- **`_extract_schema` is cached per session** — temperature 0, and `/plan` previews an amendment
  with the message the build then uses. A failed call is never cached.

### Editing a child template one block at a time (Phase W3)

`_surgical_edit` ran SEARCH/REPLACE across the whole file, and a 7B asked to add something to
a page answers with *just that block* — so its edit deleted `{% extends %}` and the title, which
is exactly why `_restore_scaffold_invariants` / `convert_to_child_template` exist. Repairing it
afterwards is strictly worse than not causing it.

`scaffold.template_edit_region(filename, text)` returns the `{% block %}` an edit belongs in
(pure, no LLM); `_surgical_edit(region=…)` shows the model *only* that body, matches its blocks
*only* against that body, and `BlockRegion.splice` puts it back — so everything outside the
block is copied through byte-for-byte and cannot be lost.
- **It declines on everything ambiguous**: not a `.html` under `templates/`, no `{% extends %}`
  (that is `convert_to_child_template`'s file, not this one), unbalanced tags, a nested block,
  an empty body, or two equally plausible blocks. `title`/`nav`/`head`/`scripts`/`styles` are
  never edited alone — a one-line block gives SEARCH nothing to match.
- **The fallback is the whole-file SEARCH/REPLACE path, then the rewrite** — in that order. A
  block attempt that matches nothing (a request about the title, say) re-runs `_surgical_edit`
  with no region; falling straight to a regeneration would make every title edit cost a full
  file rewrite. A non-template edit still spends exactly one `_llm_edit` call.

### ProjectSpec — memory between turns (`app/agent/projectspec.py`)

Phase 2 of `docs/fullstack-web-plan.md`, and the fix for its biggest gap: `chat()` resets
`self._blueprint = None` every turn, so the endpoints/schema/features existed for exactly one turn.
Turn 2 ("add an admin page") never saw turn 1's contract. `ProjectSpec` persists it to
**`<project>/.coder/project.json`** — inside the project so it is inspectable, diffable in git, and
travels with the folder. `chat()` reloads it into `self._spec` at the top of every turn (it is NOT
reset like the blueprint); `_run_blueprint` saves it after a build; `/spec` prints it.

**`entities` is the load-bearing part.** `ApiContract.data_schema` is free text
(`"users(email TEXT PRIMARY KEY, …)"`), and free text cannot be diffed, so it cannot produce a
migration. `parse_schema_line` turns it into structured fields, each stamped with the revision it
arrived in — so `ddl()` emits `CREATE TABLE` for revision-1 fields and `migrations(since=n)` emits
`ensure_column` calls for everything later. That split is what lets turn 3 add a column without
dropping turn 1's data.

**A `Field` carries its constraints, because what the model cannot represent it silently discards.**
Measured against a 12.5 KB marketplace PRD that printed its PostgreSQL DDL in full: every `UNIQUE`,
every `REFERENCES`, every `CHECK (status IN (…))` and every `DEFAULT` was gone from the generated
`db.js` — not because the schema call ignored them but because `Field` was
name/type/pk/required/added_in and there was nowhere to put them. So `unique`, `default`,
`references`, `check` and `max_length` are fields now, read by all three parsers
(`_parse_columns` for a DDL line, `_fields_from_data` for the schema call's JSON,
`entities_from_sql` for adoption) and emitted by `Dialect.column_ddl`. Five rules:
- **Every one of them reaches DDL as text with no binding, so every one is validated at
  construction** — `_safe_default` (an allowlisted keyword, a number, or a quoted literal with no
  quote inside it), `_safe_reference` (both halves through `_ident`), `_safe_check_values` (an
  enumeration, not free SQL). `_ident`'s rule applied to the parts of a schema that are not
  identifiers. Anything unrecognised is **dropped, never escaped and passed through**.
- **`check` is a value list, not a SQL expression.** A `CHECK` clause written by a 7B and
  interpolated into DDL is an injection surface, and an enumeration is what a PRD's CHECK
  constraints overwhelmingly are.
- **A generated primary key takes no extra clauses.** `SERIAL PRIMARY KEY` and
  `TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text` are complete declarations; a UNIQUE or a
  DEFAULT bolted on is redundant at best and a conflict with the one already there at worst.
- **`migration_call` never emits NOT NULL, UNIQUE or CHECK.** All three can be true of a new column
  and false of the rows already stored, so adding one raises against real data — and a migration
  that fails on startup takes the whole app down, which is worse than a column that is merely less
  constrained than the spec says. A DEFAULT and a foreign key are safe: both apply going forward.
- **`Entity.summary()` states them, so the prompt does too.** Told only `status TEXT` a model
  invents its own status words, and the CHECK then rejects every insert the app makes.
Everything defaults to "absent", so a `Field` built the old way emits byte-for-byte the DDL it
always did. What is still NOT modelled: indices, composite keys, and `ON DELETE` behaviour.

Rules the rest of the code depends on:
- **A corrupt `project.json` returns None, never raises**, and saving is best-effort — a spec that
  won't save must never cost a turn whose files were written.
- **`save()` writes directly** (tmp + `os.replace`), NOT via `executor.execute("write_file", …)`:
  the spec is agent state, and routing it through the tool would hit the approval gate every turn
  and push a backup into `.coder_backups/`, evicting the user's real undo history.
- **`.coder/` is a dot-directory**, so the RAG indexer and `project_memory._scan_project` already
  skip it. Deliberate — the spec must not be embedded and retrieved back as if it were source.
- **A project Coder did not build still gets memory** (`ProjectSpec.from_disk`, D1 of
  `docs/always-fullstack-plan.md`). `from_blueprint` was the only way a spec came into existence,
  so a repo cloned from git, one built before the spec existed, or one whose `project.json` was
  deleted had no amendment path, no impact analysis and no migrations. `from_disk` reads the
  contract off the files instead — `entities_from_sql` for tables, `routes_from_source` for routes,
  `is_layout_template` to keep `base.html` out of `pages` — and `AgentCore._load_or_adopt_spec`
  (used by `chat()`, `get_spec()`, `preview_amendment` and the smoke test) prefers a saved spec and
  falls back to adoption. Three rules: it **declines** (returns None) unless a real route is
  defined, so an ordinary Python folder never acquires an invented contract; it records only what
  it can SEE (a route whose template is missing yields an endpoint but no page — the context block
  says "these already exist", so listing an absent page instructs the model not to build it); and
  it **saves nothing** — writing `.coder/project.json` into someone's repo because they asked a
  question about it is an unrequested side effect, and the first amendment persists it anyway. It
  is recomputed per turn rather than cached: the scan is trivial next to an LLM call, and a cache
  would go stale exactly when a turn wrote a route without amending.
  - **`_write_readme` only overwrites a README carrying `README_MARKER`** (emitted by `to_readme`,
    shipped in the scaffold's copy). Adoption is what made this necessary: an existing repo can now
    reach the amendment path on turn 1, and regenerating a hand-written README would destroy the
    user's work to document our own. Don't remove the marker from the scaffold README — without it
    a scaffolded project's generic README survives every amendment, which is the file Phase 6
    exists to replace.
- **`files` is a `FileRecord`, not a role string** (D2): `role`, `purpose`, `defines` (the
  routes/handlers a file really declares), `reads` (entities), `revision`. Four role words
  could say "this is a page" but never "which file do I edit for *that*". Both shapes still
  load — `_load_files` accepts a pre-D2 `project.json`, and `ProjectSpec.__post_init__`
  normalises a `path -> role` dict built in code, so the one point of construction guarantees
  the type instead of every reader re-checking it (the first `isinstance` anybody forgets
  crashes inside best-effort `save()`, which would swallow it).
- **`reconcile_with_disk` keeps memory current** (D3), called from `_sync_spec_after_writes`
  at the `chat()` seam after every repair pass — so it covers the single-file, multi-file,
  subtask and tool-loop paths alike, not just blueprint/amend turns. Before it, an ordinary
  `_file_op_flow` edit that added a route left the spec describing a project that no longer
  existed, and the *next* amendment planned against that. Two rules: it is **additive** — a
  route that vanished is not deleted, because `impact.vanished_routes` treats a missing route
  as a regression to restore and removing it here destroys that evidence; and it **only
  updates an already-saved spec**, never persisting an adopted one, which is D1's rule.
  Entities are only taken when the spec has none: re-deriving them from SQL would flatten
  every `added_in` stamp to 1, and those stamps are what `migrations(since=…)` diffs on.
- **The spec reaches every prompt, and can pick the edit target** (D4).
  `_build_messages` and `_file_op_flow` both get `to_context_block()` via `_spec_context()`,
  which returns "" when the caller already threaded a contract (`_run_blueprint` /
  `_amend_project` build richer ones — stating the same routes twice spends `llm_num_ctx` to
  contradict nothing). `_resolve_target_from_spec` turns "the products page" into
  `templates/products.html` by matching a page's nav label, route or template stem as a whole
  word; it is deliberately strict — **exactly one** match, ≥3 characters, and the file must
  exist (the caller reads a missing path as "create this", which would turn a remembered page
  into a new empty one). Two matches means the message was ambiguous, and falling through to
  `_last_write_fallback` beats a coin flip.
- **The spec records what was BUILT, not merely what was planned.** `from_blueprint` takes `root`
  and reads it: page routes come from the real `@app.route` → `render_template` pairs in `app.py`,
  pages the blueprint never listed (the scaffold's own `index.html`) are picked up from real routes,
  `base.html` is excluded via `is_layout_template` (it is the shell, not a page), and a declared
  endpoint is dropped unless the backend really defines it. That last one matters: the context block
  says *"routes that already exist — do not redefine"*, so listing an unbuilt route instructs the
  model **not** to build it. Measured live — the blueprint declared `POST /api/login`, the coverage
  check reported it unwired on the same turn, and the spec claimed it existed.
- `to_context_block()` is budgeted to `CONTEXT_BUDGET_CHARS` (1200) and drops sections bottom-up, so
  the schema — the part a migration depends on — is the last thing to go.

### Demo surface: `/run`, `/spec`, `/plan` (`app/agent/apprunner.py`)

Phase 6. `AppRunner` holds **one** long-lived generated-app process, owned by the session rather
than a turn — the smoke test kills its subject within seconds by design, which is the wrong shape
when someone wants a URL that keeps working while the next turn amends the project. `/run` starts
it and prints `http://127.0.0.1:5000`; `/run restart` picks up a change; `/run stop` ends it.
Never a pool: two copies fight over the port and over `app.db`. It reuses `smoke._kill_tree` and
registers an `atexit` hook, so a crashed REPL cannot orphan something holding :5000 — and an app
that starts but never answers is reported as such rather than given a URL that returns nothing.

**A dead process is described by its EXIT CODE, not only by its stderr** (`_exit_reason`,
`_loudest_line`). `_drain_stderr` read one stream and said `no output` when it was empty, which
describes the diagnosis rather than the failure: an entry file that has lost its `app.listen` runs
to the end and exits **0** in silence, and that read identically to a crash whose output went
somewhere else — opposite problems, one message. So the code is always stated, **stdout is drained
too** (a generated app that reports its own failure with `console.log` had that line discarded),
and a silent 0 says what a silent 0 means. The three near-identical copies of the
"loudest line" rule (`smoke.py`, `node_adapter._last_error_line`) all follow it: prefer a line
naming an error, else the last line, never the whole log.

**`/run` re-asserts the startup invariants before launching** (`AgentCore.repair_entry_before_run`,
called from both `/run` and `/run restart`). `_restore_boot_block_note` and
`_repair_entry_module_calls` run at the `chat()` seam, and `/run` is the one path that launches a
project with **no turn around it** — so an entry file a rewrite ended at its last route stayed
broken through every `/run` until another build turn happened to run the same passes again, with
`server.js exited on startup` as the only evidence. It pins the stack from the project's own spec
(`resolve_key`'s precedence — a Node project left on a Flask default would otherwise be "repaired"
with the wrong adapter's answers), is idempotent (a healthy file is read and not written), and is
best-effort: a repair that fails must never withhold the launch, since the app may start anyway and
the runner now names the exit code either way. It repairs SHAPE only — nothing here regenerates
code, which stays the build's job.

**`/run` does the setup, it does not print it (`StackAdapter.autosetup`, `settings.auto_setup`).**
`readiness` names why the generated app cannot start, and on the Node stack three of the reasons it
names are *commands* — `npm install`, `createdb`, the seed — that have to be typed in another
terminal, in the right order, before one page can be opened. For the person this product is for,
that is the whole difference between a URL and a terminal exercise, so `/run` now performs those
three itself and prints only what it really did. **Flask returns `[]`**: sqlite is a file and the
generated app's one dependency lives in the venv Coder runs from, so that stack is unchanged.

- **This is the second deliberate exception to the offline rule, and the only one that ships ON.**
  Playwright's Chromium download stays off because a missing browser costs an optional *check*;
  missing `node_modules` means the app cannot run **at all**, so the network here is the difference
  between a URL and instructions. Nothing about the BUILD reaches the network either way, and
  `AUTO_SETUP=false` restores the previous behaviour exactly.
- **It only ever does what `readiness` would have told you to do**, and never a repair of generated
  code. Nothing is speculative: `node_modules` is installed because it is absent, the database is
  created because PostgreSQL said `3D000`.
- **A refused login is never answered by creating a database.** Only `3D000` reaches
  `_create_database`; `28P01` is a password this cannot guess, and creating something under a
  different working credential would hide that. `_probe_database` returning **None means "could not
  tell"** — N5's rule — and also creates nothing.
- **The database name is validated before it is interpolated** (`_DB_NAME_RE`, `projectspec._ident`'s
  rule): `CREATE DATABASE` takes no bound parameter. The maintenance connection is derived from the
  project's **own `db.js`** with the path swapped to `/postgres`, for `_probe_database`'s reason — a
  URL we guessed could succeed where the app fails. `42P04` (it already exists) is a success.
- **A step that failed is never reported as done**, and nothing is attempted on top of it; the
  readiness check then names the same blocker it named before. The seed runs **only after a database
  we just created** — re-seeding an existing one would write rows into someone's data — and its
  failure never withholds the URL, since the app creates its own tables on startup.
- **What it will never do: install Node, start a PostgreSQL service, or guess a password.** Those
  need an installer or an administrator and stay `readiness`'s job, which now names the download and
  the service rather than a bare fact.
- `_CREATE_DB_SCRIPT` is covered by `test_the_node_scripts_are_valid_javascript` for
  `pageaudit.py`'s reason: a syntax error in an embedded `node -e` script fails silently and in the
  direction that reads as success.

`/point` is the other half of the demo surface — see "Click the page, edit the source" above.
It needs `/run` first, because it drives the app the runner is already serving.

`/plan` gained an amendment preview (`AgentCore.preview_amendment`): with a spec loaded, a change
request shows the delta, the new files, and a table of **existing** files that will be updated with
the reason for each, before anything happens. Costs the same single delta call the real amendment
would; falls back to the ordinary planner otherwise.

`README.md` is rendered from the spec (`ProjectSpec.to_readme`) on every save, builds and
amendments alike — real pages, routes and columns, plus `added in revision N` on later fields. The
scaffold's generic README describes the *template*; by turn 3 a README documenting turn 1 is worse
than none.

### The functional probe — "it works", not "it started" (`app/agent/smoke.py`)

Phase 5, closing Gap 3. `run_smoke_test(..., spec=…)` no longer just pings the server: it
exercises it against its own contract. Every phase before this could report a passing smoke test
on a broken app because **any HTTP status counted as alive** — Phase 1 announced
`GET /posts/new -> 200` while every POST returned 500; Phase 4 counted `GET /api/login -> 404`
as up. Three checks, and the third is the point:

1. every page in the spec renders (2xx **and** a non-empty body);
2. every write endpoint accepts a real submission — a genuine 1×1 PNG built from stdlib
   `zlib`+`struct` (no Pillow) posted as real multipart, so the upload branch is actually taken;
3. **the posted value comes back** — only this can fail on a build whose INSERT silently does
   nothing. `tests/test_functional_probe.py::_SILENT_APP` is exactly that app: starts, answers,
   returns 302, and adding a product does nothing.

Details that are load-bearing:
- **`spec=None` reproduces the old liveness behaviour exactly**, so existing callers are unaffected.
- **Step 3 checks EVERY page, not just those whose `reads` names the entity.** `reads` is inferred
  from blueprint prose and is routinely empty on the very listing page that matters — probing only
  tagged pages produced a false failure for a row that had persisted. A false failure here is worse
  than no check: `_smoke_repair_instruction` would send the model to rewrite working code.
- **`server_error()` lifts the exception out of a 5xx**, so the repair prompt says
  `POST /x -> 500 NameError: name 'Product' is not defined` instead of "POST failed". Generic error
  pages return `""` rather than repeating the status.
- `_request` retries once: the dev server occasionally resets a connection mid-probe, and
  "no response" would discard a real named exception.
- The repair loop now fires on functional failures too, not only startup crashes — startup still
  takes priority, since a server that never came up makes the other checks meaningless.

### The data layer is generated, not prompted (`app/agent/crud.py`)

Phase 4a/4d. `db.py`'s `CREATE TABLE`s, `models.py` and `seed.py` are written **before any
generation** from the blueprint's declared schema, and dropped from the plan. They contain no
decisions — the table *is* the fields, the query *is* the table, the demo row *is* the field types
— and leaving them to a 7B model produced, on live builds, an `init_db()` with no `CREATE TABLE`
at all and an `app.py` calling `models.get_all_posts` against a `models.py` defining only
`add_post`. Phase 2's structured entities are what make this possible.

Two properties then hold **by construction**, not by inspection: SQL injection is impossible
(values bound as `?`; identifiers come from `projectspec._ident`, which admits only
`[A-Za-z_][A-Za-z0-9_]*`), and the column lists cannot drift from the tables (both printed from the
same `Entity`). `tests/test_crud.py` executes the generated SQL against real in-memory sqlite3.

Three rules that are easy to get wrong, each learned from a live regression:
- **`api_context()` is not optional.** Taking `models.py` away from the model is only safe if the
  model is told what replaced it — otherwise it invents an API and the app dies at import
  (`from models import get_user_by_email, get_all_products, User, Product`). It is threaded into
  `_run_blueprint`'s `extra_context` beside the scaffold and contract blocks.
- **Idempotency checks must read string literals, never raw text.** `_creates_table` scanning raw
  text made the scaffold's *commented* `CREATE TABLE ... products` example count as a real table,
  so the real one was skipped. Same trap as the `ensure_column` example in Phase 3. Use
  `pyimports.searchable_sql`, which is shared by `missing_tables`, `entities_from_sql` and
  `_creates_table` for exactly this reason — and require an actual SQL statement keyword, or prose
  like "printed **from the** same definition" reports a table called `the`.
- **`seed.py` is RUN, once, after the build** (`core._seed_demo_data`). 4d's promise is that no page
  starts empty, and a seed script guarded by `if __name__ == "__main__"` that nobody executes does
  not keep it. This is a deliberate exception to "never execute generated code": `seed.py` and the
  schema are written by `crud.py`, not by the model. Short timeout, failure reported not raised.

Uploads (4b): `crud.upload_helper_source()` emits a `save_upload()` with an extension **allowlist**,
`secure_filename`, a jail to `UPLOAD_DIR`, and collision-safe naming; `verify.fix_form_enctype`
supplies the `enctype` a file input cannot work without. Auth (4c) was trimmed per the plan, keeping
only `plaintext_password_writes` — a check on the CODE, deliberately never a prompt instruction,
which caught a live `password_hash = request.form["password"]`.

### The amendment flow — turn N changing turn 1's project (`app/agent/impact.py`)

Phase 3. `should_amend(msg, spec_exists)` is the mirror of `should_blueprint()`: it fires on exactly
the incremental verbs that gate rejects (add/update/change/remove/also/now) and **only when a
ProjectSpec exists**, so without memory routing is completely unchanged. `chat()` consults it ahead
of the blueprint gate; a greenfield "build me a blog" has no incremental verb and still blueprints.

`_amend_project` is five steps: **delta** (one temp-0 call against `prompts/amend.md`, given the
spec's context block) → **impact** (`impacted_files`, no LLM) → **apply** → **persist**
(`merge_delta`, revision bump, history) → **verify**.

- **The model is asked only what CHANGED, never which files to edit.** "What else does this break?"
  is the question a 7B model answers worst — it lists `app.py` and stops. `impact.py` derives it
  from the spec: a new field on `product` means `db.py`, `models.py`, `seed.py`, every template
  whose `reads` include the entity, every form template that writes it, and `app.py`.
- **One reason = one edit.** Reasons for the same file are NOT merged. Measured live: `app.py` got
  three reasons at once and the model did only the first, so `POST /admin/products` was silently
  never written. `_file_op_flow` re-reads the file per call, so sequential surgical edits compose;
  a file's edits are kept adjacent so each runs against what the previous one wrote.
- **`db.py` is never handed to the model.** Its migration comes from `spec.migrations(since=…)` via
  `apply_migration_block`, which inserts before `conn.commit()` and declines rather than guessing on
  an unrecognisable file. A migration is exactly derivable from `added_in`; letting a 7B model write
  `ALTER TABLE` against live data is risk with no upside.
- **Regression detection (`vanished_routes` / `restore_page_routes`).** The amendment's own edit to
  `app.py` deleted turn 1's `/products` route on a live run — the file compiled, the new route
  worked, the turn reported success. Only the spec could see it, because it records which routes
  existed *and at which revision*. A deleted GET page route is restored exactly (its body is just
  `render_template`); a deleted POST handler is reported, never invented — that body is domain logic.
  Routes added *this* turn are excluded: unwritten ≠ regressed, and that's the coverage check's job.
- **`self._blueprint = _blueprint_from_spec(spec)` at the end is load-bearing.** `chat()` gates BOTH
  the coverage check and the smoke test on that attribute and clears it every turn, so an amendment
  that skipped this would be the only kind of turn never verified and never run — invisibly, since
  it still reports success. `tests/test_amend.py` pins it.

**Upload forms (`verify.fix_form_enctype`).** A `<form>` with `<input type="file">` and no
`enctype="multipart/form-data"` posts only the filename, so `request.files[...]` raises and the
upload silently never happens — invisible to every other check, because the HTML is valid and the
page renders. Deterministic and purely additive; runs in `_verify_and_repair` alongside the other
stage-0 fixes, on `.html` **and the stack's own `template_ext`** — nothing about a missing enctype
is Jinja-specific, and this has exactly one caller, so while that caller filtered on `.html` a
`.ejs` upload form could not be repaired by anything.

**Runtime defects `compile()` cannot see (`app/agent/pyimports.py`).** `check_file` compiles a
`.py`, so it catches `SyntaxError` and is blind to `NameError`/`AttributeError` — which only fire
when the line runs. That blind spot is how a generated app ships `verified OK` and then 500s, and it
was measured on every live build. Four deterministic checks, one repair and three report-only:
- **`add_missing_imports` (repairs).** Names loaded but never bound anywhere in the module get their
  import added — **allowlist only**, so an unknown name is reported, never guessed, and `import
  models` is added only when `models.py` exists. Binding is collected *flat* across the module
  (deliberately over-approximating scope) so the error direction is always "do nothing", and the
  result is re-parsed before being returned so the pass can't hand back a file it broke. Runs per
  file, last in `_verify_and_repair`, because an intent rewrite can reintroduce the names.
- **`unresolved_local_calls`, `missing_tables`, `duplicate_definitions` (report only).** A call into
  a sibling module that the sibling never defines; SQL against a table nothing creates; a top-level
  def written twice (the later silently wins). These run **once at the end of the turn**
  (`_check_cross_module_calls`), never per file — `app.py` is written before `models.py` is
  regenerated, so a per-file check reported `models.add_post` missing while the next file in the
  same build defined it. Fixing them means inventing a query or a schema, which is generation, not
  repair; the structured entity list that would make it deterministic is Phase 2's `ProjectSpec`.
- `missing_tables` scans **string literals only** (via `ast`), not raw source: `from flask import
  Flask` otherwise matches its `FROM <table>` pattern. A pleasant side effect is that the scaffold's
  *commented* `CREATE TABLE` example correctly doesn't count as creating a table.

**The same, for JavaScript (`app/agent/jsimports.py`).** All of the above was Python-shaped down to
the `.py` suffix gate and the `werkzeug.security` advice, so the Node stack had none of it and
`node --check` happily passes a file full of undefined names. Measured on a live build: one
`server.js` called `bcrypt.compareSync(...)` with `bcrypt` never required, assigned
`req.session.userId` with no session middleware mounted, and stored the raw `password_hash` form
field while the project's own generated `passwords.js` sat unused — three runtime-fatal defects in
one file, and nothing in the pipeline able to see any of them. `core._repair_missing_imports` now
dispatches through **`adapter.repair_runtime_names(filename, source, root)`**; `FlaskAdapter`'s copy
is the old body moved, so that stack is unchanged.
- **Parsing is tree-sitter** (the parser `symbols.py`/`chunker.py` already pin), and identity does
  **not** work on tree-sitter nodes — `node is other` is False for the same node, because each
  attribute access builds a fresh wrapper. Getting that wrong is not a subtle mis-report: it made
  `const path = require("path")` read as an undefined `path`, i.e. every declaration in the file
  reported as a defect. Bindings are found by walking a construct's pattern subtree
  (`_BINDERS`), never by asking whether an identifier happens to be its parent's `name` field.
- **`add_missing_requires` binds from the allowlist ONLY**, and the allowlist is narrower than
  Python's: a Node builtin, or a sibling module in this project that really exports the name.
  An npm package is **reported, never required** — `require("bcrypt")` against something absent from
  `node_modules` turns one broken route into an app that will not boot, which is strictly worse than
  the defect being repaired.
- **A hash call on an UNBOUND name does not count as hashing.** `plaintext_password_writes` inherits
  the Python rule (silent when the module hashes anywhere, so read-then-hash is fine) and that rule
  inverts here: the measured `server.js` mentioned `bcrypt.compareSync` in a *different* route with
  `bcrypt` undefined, and text-matching alone read that as "this module hashes" and went quiet about
  the raw password it really did store. It takes `undefined_names`' answer for exactly this.
- **`middleware_gaps` reports what is not a binding at all.** `req.session` is a property of a
  parameter, so `undefined_names` cannot see it, `node --check` is happy, and the route throws
  `Cannot set properties of undefined` on the first successful login. Reported only — mounting a
  session store is a design decision with a secret and a backing store behind it, not an import.

**Generated sites are kept offline too.** Coder is offline; the sites it generated were not.
`buildspec.py` used to instruct the model to load Google Fonts with a `<link>` in every page, and
`references.py` deliberately ignores external URLs, so nothing stripped it — offline that means a
dead DNS lookup per page and then the wrong font, or for a CDN stylesheet a completely unstyled
page. Two guards: `BuildSpec.to_context_block(allow_network=...)` emits **system font stacks**
(keeping each preset's display/body pairing) instead of a Google Fonts link when the network is off,
and `_verify_and_repair` runs `verify.strip_external_assets` as a deterministic stage 0, removing
`<link href="http…">` / `<script src="http…">` / CSS `@import` of a URL and reporting it in the
answer. `<a href="https://…">` is never touched — a hyperlink is not a render dependency.
`to_context_block` **defaults to the offline branch** so a caller that forgets the argument cannot
ship a dead CDN dependency. The strip pass covers the stack's own `template_ext` as well as
`.html`/CSS: until it did, the *first* guard was the only one a Node build had, and a prompt-level
hint with no deterministic backstop is exactly the arrangement the second guard exists to replace.

**Both stage-0 passes mask template expressions before matching
(`verify.mask_template_tags`).** `<% … %>` and `{% if a > b %}` can contain a `>`, and every regex
in `verify.py` scans an attribute list with `[^>]*`, which stops dead at that inner `>`. The match
is then a truncated half-tag — and these two passes **write**, so the truncation gets written back:
`<form action="<%= u %>">` became `<form action="<%= u % enctype="…">`, a broken page produced by
the pass whose whole job is to prevent one. Masking blanks those expressions to spaces of **equal
length**, so a span found in the masked text still addresses the same characters in the real one;
`strip_external_assets` then cuts by span rather than re-running `re.sub` on the raw text, because
the two regions differ around an expression and the difference is a dangling `">`. A file with no
template tags is returned byte-for-byte, which is why every Flask behaviour is unchanged — and the
Jinja half of the hazard (`{% if a > b %}` inside a tag) was being corrupted before this too.

### Skills (`app/resources/skills/`)

Each skill = a folder with a `SKILL.md` containing **`## Description`, `## Trigger Keywords`,
`## Instructions`** (parser is header-strict; a skill with neither description nor instructions is
dropped). `SkillLoader.load_all()` scans **once at startup** — there is no hot-reload, adding/editing
a skill needs a restart. Per turn, `match_skills()` scores each enabled skill (0.5·keyword-overlap +
0.5·embedding-cosine, threshold 0.25, **max 2** injected) and the result is injected as a system
prompt block.

### Project instructions the USER writes (`app/agent/instructions.py`)

`<project>/.coder/INSTRUCTIONS.md` — the third kind of project knowledge, and neither of the
other two could hold it. The **ProjectSpec** records what the project *contains* and is written
by the agent from the files; **skills** are matched per turn by keyword and are global to the
install. Missing was *how the user wants work done here* — "this repo uses tabs", "never touch
`vendor/`", "tests go beside the module". Not derivable from code, not keyword-triggered: it
must simply be true for every turn in this project.

`load_project` reads it into `AgentCore._instructions` (and nothing else assigns it, so opening
a second project cannot leave the first one's conventions in effect); `_instructions_context()`
states it at **all three** generation sites — `_build_messages`, `_file_op_flow`'s generation
prompt, and `_surgical_edit`'s editor prompt. A convention that reached only some of them would
look like the model ignoring it at random, so `tests/test_instructions.py` pins the call-site
count. `/instructions` prints exactly what the model got, `/help` documents it, and
`settings.project_instructions` is the kill switch.

- **It is CONVENTIONS, never capability**, and the block says so. What makes that more than a
  hope is that nothing here touches the enforcement layers: the executor's permission gate,
  `denied_permissions`, the approval hook, the path jail and the shell denylist all sit *below*
  the prompt and are unreachable from it. An instruction file cannot unlock a tool, widen the
  sandbox or approve a write — at worst it wastes a turn.
- **Deliberately NOT wrapped in `<untrusted_data>`.** It is the user's configuration, like a
  skill, not file text the model happened to retrieve — framing it as data would mean telling
  the model to ignore the instructions it was just given. The file does travel with the folder,
  though, so a cloned repo can carry one: loading it is therefore **reported** (`load_project`
  returns `instructions_chars`, the REPL prints the path and size). It grants nothing and it is
  never silent — those two together are the trust argument.
- **Bounded, and a cut says so** (`max_instructions_chars`, 4000). This text is prepended to
  every prompt in the project, so an unbounded file evicts the sibling/RAG context the answer
  depends on. Truncation is stated in the block — `_requirements_doc_context`'s rule: a silently
  halved file is a rule the model will not follow and nobody will know is missing.
- **Containment is checked even though the path is constructed.** The argument is never
  user-supplied, but a symlink at `.coder/INSTRUCTIONS.md` would put an arbitrary file into
  every prompt for this project, so the resolved path must be inside the root.
- **Every failure is `""`** — missing, empty, a directory, unreadable, undecodable — and the
  prompt then reads exactly as it did before the file could exist.
- **`.coder/` is a dot-directory**, so the RAG indexer, `project_memory._scan_project` and
  `_locate_named_file` already skip it: the instructions are never embedded and retrieved back
  as if they were source, and never picked as an edit target.
- **No hot-reload**, same as skills: `/index` (or a reload) picks up an edit.

### Config

`config/settings.py` — single pydantic-settings `Settings` instance reading `.env`. Import as
`from config.settings import settings`. For shell commands `blocked_commands` (denylist) is always
enforced (in `app/tools/terminal.py`); `command_allowlist` adds an opt-in allowlist enforced only
when non-empty; `allowed_commands` remains deliberately informational. `allow_network` /
`network_commands` gate network-reaching commands. Tool-level gating is `denied_permissions`
(hard-refuse) and the approval gate (`approval_gated_permissions`, `safe_deny_permissions`,
`auto_approve`, `safe_mode`). Path jail: `sandbox_root` (None = off) and `allow_outside_root`.
**`llm_num_ctx` (default 16384) is set explicitly on every `ChatOllama`** — Ollama's own default is
4096 regardless of what the model supports, and it *silently truncates* rather than erroring, so
leaving it unset meant budgeting 8192 prompt tokens into a 4096 window and losing the overflow.
Verified in the request payload via `_chat_params(...)["options"]["num_ctx"]`. It must stay above
`max_context_tokens` with headroom for the generated file; lower it on a RAM/VRAM-tight machine.
`max_sibling_context_chars` (6000) is the TOTAL cap on already-written sibling files threaded into
the next step of a multi-file build — see `_sibling_context`. `extract_build_spec` (default on)
allows the one extra pre-planning LLM call that distils the shared nav/design spec
(`app/agent/buildspec.py`); turning it off reverts multi-file builds to the pre-spec behavior.
`project_instructions` (default on) reads `<project>/.coder/INSTRUCTIONS.md` into every prompt
for that project, capped by `max_instructions_chars` (4000) — see "Project instructions the USER
writes" above; false ignores the file entirely.
Full-stack web knobs (`docs/fullstack-web-plan.md`): `expand_requirements` and
`blueprint_smoke_test` both ship **on** (Phase 0); **`web_stack` (default `"node"`) forces the
backend stack** rather than probing for one — `detect_stack(prefer=…)`, see "Forcing the stack"
below; `scaffolds_dir` locates the runnable project skeletons; `blueprint_max_files` (24) caps one build's fan-out and now **reports** what it drops as
`may not meet:` rather than truncating silently — `_run_blueprint` and `_verify_blueprint_coverage`
apply the same slice, so a hidden truncation was invisible to the check that exists to catch missing
files. `allow_network` additionally decides whether generated pages may reference CDN fonts/scripts.
Browser knobs (Phase W4, `docs/web-quality-plan.md`): **`browser_checks` ships OFF** — it is
the one feature whose install step needs the network (`python -m playwright install
chromium`), so it is opt-in and its absence is *reported* via `browser.install_hint()` rather
than silently skipped; `browser_timeout`, `browser_widths` (`[1280, 390]` — the narrow one is
the point, horizontal overflow cannot be seen at desktop width), `browser_max_pages` and
`browser_max_controls` bound one turn's navigations (the W6 dead-button probe reloads the page
once per control). `max_browser_repairs` (1) caps how many FILES one turn may rewrite over what
the browser measured; 0 reports the findings and changes nothing. W7's `check_visual` ships
**off** and needs a browser as well (there is no screenshot without one); `visual_max_pages` (2)
bounds the vision calls and `max_visual_repairs` (1) the rewrites.
W9 knobs: `best_of_n` (**1** = off) and `best_of_temperature` (0.4) — generate a high-value file
N times and keep the best-scoring candidate; `planner_model` / `judge_model` (both empty = use
`llm_model`, i.e. exactly today's behaviour) put a different model on the reasoning calls.
`max_context_tokens` is the per-prompt token budget enforced by `app/agent/context_budget.py`
(oldest history dropped first in `_build_messages`); `max_repair_attempts` caps the
verify-and-repair loop; `backups_dir` / `max_write_backups` configure safe-write snapshots.
Surgical-edit knobs (`app/agent/patch.py`): `max_edit_context_chars` (24000) is how much of a
file the SEARCH/REPLACE editor is shown, `max_rewrite_chars` (24000) the largest file a
whole-file rewrite may regenerate — above it the rewrite is refused rather than run against a
partial view — and `shrink_guard` / `shrink_guard_floor` (0.6) / `shrink_guard_min_chars` (400)
reject a write that keeps too little of the file unless the request asked for a deletion. RAG
knobs: `embed_cache_dir` / `max_embed_cache_entries` (persistent embedding cache),
`max_index_file_bytes` (indexer size cap), `max_read_file_bytes` (`read_file` truncation cap).
Vision knobs: `vision_model` / `vision_enabled` (kill switch) / `vision_num_ctx` /
`image_extensions` (what counts as an image `@`-ref) / `max_image_bytes` /
`max_image_dimension` (long-edge px cap; the image is downscaled to this before
the vision call — a byte cap does NOT bound resolution, and Ollama silently
truncates to `vision_num_ctx`, so a high-res screenshot would otherwise be
half-described; 0 disables. Best-effort via Pillow — see `vision._prepare_image`).

**Observability (Step 9 / C2):** best-effort paths that used to `except Exception: pass` now log via
a module-level `logging.getLogger(__name__)` (`retriever`, `core`, `vector_store`, `project_memory`)
at `debug`/`warning` — behavior is unchanged (still best-effort) but failures are visible. There's
no global logging config; if one is added later, route these through it.

### Multi-turn webapp evals (Phase 7)

`python -m evals.run --webapp` runs the demo turn for turn. `EvalTask` gained
`prompts: list[str]` alongside `prompt`, and `run_task` runs a whole **conversation** against ONE
workdir with ONE `AgentCore`, checking only after the last turn. The shared agent is not an
optimisation — a fresh one per turn would reload the spec from disk and mask exactly the in-memory
staleness the suite exists to catch. A turn that raises stops the conversation; `prompt` still works
so every single-turn task is untouched.

**Phase E (`docs/always-fullstack-plan.md`) added request-SHAPE tasks** (`web_shape_*`) next
to the demo turns. Their point is that the checks name **no table and no route** — they read
the project's own spec — so a request whose schema the eval author could not have guessed is
measured as strictly as the e-commerce one: `is_full_stack_app` (A+B: a server exists at all),
`every_entity_has_a_table` (C1: the declared schema really ran), `entities_are_usable`
(C3: every table is browsable AND writable, checked by starting the app **once** and looping
the entities inside it). `web_shape_offlist` is the Phase B regression test — not one word of
"something to organize my recipes and what goes in them" is in `_BLUEPRINT_NOUN_RE` and there
is no build verb either, so before Phase B it shipped static HTML and every file-level check
still passed.

The checks assert the app WORKS, not that plausibly-named files appeared:
- **`db_has_column` asks the database, not the source.** A `CREATE TABLE` in a file nobody executes
  proves nothing — Phase 1 and Phase 4 both shipped builds that would pass a source-level check and
  fail this one.
- **`post_persists`** POSTs and then requires the value to come back. A handler that answers 302 and
  never writes passes everything else and fails this.
- **`earlier_pages_still_work` is the headline number.** Not "did turn 3 work" but *"did turn 3
  break turn 1"* — the regression Phase 3 caught live, where an amendment deleted turn 1's
  `/products` route while reporting success.

**Phase W10 added the checks that LOOK at the page** (`no_horizontal_overflow`,
`no_console_errors`, `every_control_does_something`, `nav_on_every_page`, `contrast_ok`,
`style_stable_across_turns`), driving `browser.py` against an `AppRunner`. They name no route
and no selector — they read the project's own spec — and the browser pass is **memoized on the
`CheckContext`** so five checks cost one server launch and one Chromium start.
- **No browser is a FAILURE naming the install command**, never a skip and never a pass: a check
  that rendered nothing has verified nothing. They live on their own tasks (`web_quality_*`) so
  a machine without Chromium loses those two rather than dragging every existing task down.
- **`style_stable_across_turns` compares the pages to EACH OTHER at the end** — one computed
  body font and background across all of them, no page-local `<style>`, every page still using
  the shipped component classes. Not a screenshot hash: a pixel diff fails whenever the seeded
  data changes, which is noise, not a measurement. Fewer than two pages fails rather than
  passing vacuously.

`--webapp` turns `blueprint_smoke_test` off: the checks start the app themselves and the two would
fight for port 5000. Expect a lower score than `--blueprint` — it asks harder questions, and the
earlier suites were reporting passes while builds were visibly broken (Phase 0's 4/4 coexisted with
one of four apps not starting). A suite that scores 100% on a broken app is worse than one that
scores 50% honestly.

**The same suite, either stack (Phase N6).** `python -m evals.run --webapp --stack node`
runs the same tasks against a Node build. The plan's bet held for *what* the checks
assert — they name no table and no route, they read the project's own spec — but not for
how they REACH the app: `workdir / "app.py"`, `"flask" in source`, `templates/`,
`AppRunner().start(workdir)` and `sqlite3` were written in directly at eleven sites, so
against an Express build that works, every one reported a static pile of HTML.

- **`CheckContext.adapter` reads the stack off the built project's own
  `.coder/project.json`, never off `settings.web_stack`** — `resolve_key`'s precedence,
  for `resolve_key`'s reason. A check that trusted the session setting would look for
  `app.py` in a Node project the moment someone left the default on Flask and call a
  working app static. Resolved once per task and cached, like the `_browser_report` memo.
- **`is_full_stack_app` asks `adapter.routes_from_source`** instead of matching
  `@app.route`. Strictly better even on Flask: it is the same parser the agent uses to
  decide what was built, so the check and the project's own memory cannot disagree.
- **`adapter.table_columns(root)`** answers "what does the database really have" — sqlite
  `PRAGMA` on Flask, `information_schema` over the project's own `pg` on Node (N5's probe
  transport, reused). **`None` means *could not read* and is never collapsed into "no
  tables"**: reporting an unreachable database as an empty schema turns an environment
  problem into "the build created no tables".
- **The environment does not generalise, and the suite says so.** A generated Node project
  cannot run until `npm install` has fetched Express and `pg`, and that needs the network.
  So `_start` consults the N5 readiness gate and the app-running checks **fail naming the
  command** rather than skipping — W10's rule exactly. `--npm-install` lets the *runner*
  (never Coder) install into each task dir, via `run_task`'s `prepare` hook; without it the
  run prints up front that it is measuring the files, not the running app.

### Eval harness (`evals/`)

The measuring stick for model/prompt changes. `evals/tasks.py` holds ~12 golden tasks asserting
**observable** outcomes (file on disk, answer token, N files written) via declarative checks
(`evals/checks.py`). `evals/harness.py` runs each prompt through `AgentCore.chat` in an isolated
cwd and scores the suite; the harness logic is unit-tested offline (`tests/test_evals.py`) with a
scripted LLM. The **live** run is `python -m evals.run` (needs Ollama; NOT part of `pytest`) —
`--keep DIR`, `--min SCORE`, `--only ids`. Run it before/after a model or prompt change: the first
baseline (qwen2.5-coder:7b) was 10/12 and immediately caught a real multi-file routing bug; the
suite is now 14 tasks and last measured **14/14**. Two lessons from using it: the planner runs at
temperature 0.2, so **a single run proves nothing** — re-run a suspect task ~5x against a stashed
baseline before calling a change a regression *or* a fix (a 3/3 baseline vs 1/3 was what exposed the
`FILENAME:` spill); and `--keep DIR` is the fastest diagnosis, because the wrong content sitting
inside the wrong file names the bug immediately.

## Gotchas

- **Tree-sitter semantic chunking is LIVE (pinned).** `tree-sitter==0.21.3` +
  `tree-sitter-languages 1.10.2` — `get_parser('python')` works and `_chunk_with_tree_sitter`
  emits real function/class chunks (verified by `tests/test_rag.py::test_chunk_python_is_semantic_not_token_fallback`,
  which asserts 2 top-level defs → 2 chunks, i.e. NOT the token-window fallback). Do **not** bump
  `tree-sitter` to 0.25.x: 0.25 + `tree-sitter-languages` 1.10.2 are incompatible
  (`get_parser` raises `TypeError: __init__() takes exactly 1 argument (2 given)`), and
  `_chunk_with_tree_sitter` silently swallows that into the token-window fallback — the failure is
  invisible except via that regression test. If you must upgrade, migrate to
  `tree-sitter-language-pack`. (You'll see a harmless `FutureWarning: Language(path, name) is
  deprecated` from 0.21.3 — that is expected, not the breakage.) The symbol index (`symbols.py`)
  uses stdlib `ast` and is unaffected either way.
- **Lazy singletons (Step 12 / A1).** Importing the package no longer creates `.chroma_db/` or
  `.symbols.db`: the ChromaDB client, symbol index, retriever, and registry are built on first use
  via `get_vector_store()` / `get_symbol_index()` / `get_retriever()` / `get_registry()` (each a
  cached module-level accessor), **not** at import. `tests/test_no_import_side_effects.py` guards
  this by importing the modules in a subprocess and asserting no state files appear. Do not
  reintroduce eager `X = VectorStore()`-style module singletons. (`.coder.db` is still created lazily
  by the async SQLAlchemy layer on first DB use, not at import.)
- **Suite wall-clock depends on whether Ollama holds a model, not on the tests.** Measured on the
  same 47 tests, same code: **171s with `qwen2.5-coder:7b` resident vs 46s with `ollama serve`
  stopped** — 3.7x. The tests never call it (see the offline rule below); the resident model just
  starves the machine of memory while ~70 tests each construct an `AgentCore` (~2.3s) plus ChromaDB
  and the embedder. So **time the suite with Ollama stopped**, or a warm model will look exactly
  like a performance regression in whatever you just changed. The ~9 min figure above is the
  Ollama-stopped number for 661 tests.
- **Blocked-command matching** (`_is_blocked`): bare executable names (`format`, `mkfs`) match only
  the *invoked* command (first token), while multi-token/path patterns (`rm -rf /`, `dd if=/dev/zero`)
  substring-match anywhere. Don't revert to plain substring matching — it falsely blocks args like
  `'{}'.format(x)`.
- **Tests must stay offline.** Mock the LLM (`ScriptedLLM`), monkeypatch `embedder._get_embeddings`,
  and use the in-memory `_FakeStore` for the retriever. `conftest.py` at repo root puts the project
  root on `sys.path`; `pytest.ini` sets `asyncio_mode = auto`. Git tool tests `importorskip("git")`.

## Stubs (not implemented)

- `app/terminal/runner.py` — empty (the working terminal tool is `app/tools/terminal.py`)
- `app/gui/` — Phase 3, do not implement yet
