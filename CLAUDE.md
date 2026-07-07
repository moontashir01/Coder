# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Coder** — a fully **offline** AI coding assistant. It talks only to a local Ollama
(`http://localhost:11434`); nothing leaves the machine. `qwen2.5-coder:7b` is the default LLM,
`nomic-embed-text` the only embedding model. Primary interface is a CLI/REPL. ChromaDB for
vectors, SQLite for memory, LangChain for the Ollama wrappers.

## Prerequisites

Ollama running with both models pulled:
```
ollama serve
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```
All Python work uses the venv (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Unix).

## Common Commands

```bash
python main.py                          # start the REPL
python main.py --project /path/to/proj  # load + index a project on startup
python main.py --session work           # named conversation session (persists in SQLite)
pip install -e .                        # installable CLI: `coder` == `python main.py`
coder --version                         # works without Ollama (eager typer callback)

pytest tests/ -v                        # all tests (~28s, fully offline — no Ollama needed)
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
**`_verify_and_repair`**: `app/agent/verify.py:check_file()` syntax-checks the file (`.py`
in-process `compile()`, `.js` `node --check`, `.ts` `tsc --noEmit`, `.html` tag-balance parser;
unknown ext / missing checker binary = unverifiable-ok, never "broken"), and on failure feeds the
error back for a complete-file regeneration, capped at `settings.max_repair_attempts`.

**Why three paths:** the 3B model these paths were built for is unreliable at the JSON tool
protocol (see the "3B-era hardening" note below — the default is now `qwen2.5-coder:7b`). So:
- **Create/edit a single file → `_file_op_flow`** (the common case). `_wants_file_op()` is a
  verb+target regex ("make/create/edit … html/file/`*.ext`"); note `classify()` tags file
  *creation* as `code_generation`, so the regex — not the classifier — is what catches it. Files land
  in the loaded project, else **cwd**.
  - **Create / new file:** ONE plain LLM call for `FILENAME: <name>\n<full contents>`, parsed by
    `_parse_file_output` (strips code fences, incl. stray/unmatched ones), written via `write_file`.
  - **Edit existing file → surgical first (`_surgical_edit`).** Asks `_llm_edit` (temperature 0,
    few-shot, editor-only system prompt — NOT the persona, whose "confirm what you did" rule causes
    prose) for `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks. `_apply_search_replace` applies
    them: exact substring → trailing-ws-tolerant → strip-tolerant **with replacement re-indented to
    the file** (3B routinely drops the SEARCH indentation). One retry, then **fall back to a
    whole-file rewrite** if no block parses/matches. NB: with `qwen2.5-coder:3b` surgical edits fire
    reliably (~3/3 in practice); a non-code model like `qwen2.5vl:3b` rarely emits valid blocks and
    keeps falling back. The path is fully unit-tested regardless of model.
- **`@path` references** (`_extract_at_refs`): in any message, `@src/app.py` pins the edit *target*
  (`_resolve_ref`, prefers an existing file) and, for non-edit questions, injects the file as context
  (`_read_refs`). The `@` is stripped before the model/classifier see the text. Emails are ignored.
- **Split/reorganize across several files → `_multi_file_flow`** (`wants_multifile()` regex:
  separate/split/extract… + plural "files" or ≥2 languages). One `_plan_file_ops` LLM call returns
  `{"files": [{filename, action, instruction}]}` (`_parse_file_plan`, tolerant), then each op runs
  through `_file_op_flow`. **Cross-file consistency:** every per-file call gets the full plan
  manifest as `extra_context`, plus the content of already-written siblings, so
  `<link href>`/`<script src>`/shared names line up.
- **Genuine multi-step work in a loaded project → `_run_tool_loop`** (native tool calling).
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

**`prompts/system.md` must NOT contain tool-protocol text** — the tool loop's behavioral guidance
comes from `_tool_guidance()` and the schemas from `bind_tools`. If you put tool-protocol text in
system.md it leaks into `_direct_answer`/`_file_op_flow` and the model emits fake tool-call JSON
instead of the file/answer.

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

### Tool registry & executor — the central hub

- `app/agent/tool_registry.py` — every tool (builtin, MCP-discovered, skill-unlocked) must be
  registered here. `create_registry()` builds the default with all 13 builtin tools; a
  module-level `registry` singleton exists. Tools carry `source` = `"builtin"` | `"mcp:<server>"`
  | `"skill:<skill>"`; `unregister_by_source()` is how MCP disconnect cleans up. Every tool also
  carries `permissions` tags — builtins use `fs:read` / `fs:write` / `fs:delete` / `shell` /
  `git:read` / `git:write`; MCP tools are tagged `mcp` as a class.
- `app/agent/executor.py` — async `execute()`: **refuses any tool whose `permissions` intersect
  `settings.denied_permissions`** (default empty = allow all), then validates args against the
  tool's JSON Schema, then awaits async handlers (MCP) or runs sync handlers in a thread pool.
  **Every tool handler must return `{"success": bool, "result": str, "error": str | None}`** —
  this contract is assumed everywhere (REPL tool-step rendering, the tool loop's result
  feedback). Mutating file tools may add a display-only `"diff"` key (unified diff): the REPL
  renders it under the tool step; the tool loop feeds only `result["result"]` to the model.
- **Safe writes** (`app/tools/filesystem.py`): `write_file` (overwrite), `edit_file`, and
  `delete_file` back up the previous content into `settings.backups_dir` before mutating — a
  failed backup aborts the mutation. `undo_write` (builtin tool, also the `/undo` REPL command)
  restores and consumes the newest backup (optionally per path); backups are pruned to
  `settings.max_write_backups`. The original absolute path is URL-quoted into the backup
  filename after the first `__`.

### RAG pipeline

`Retriever` ([app/rag/retriever.py](app/rag/retriever.py)) wraps `VectorStore` (ChromaDB) and the
embedder. **One ChromaDB collection per project**, named after the folder. Tree-sitter chunker
([app/rag/chunker.py](app/rag/chunker.py)) emits semantic chunks (functions/classes), falling back
to token-window sliding for non-code or oversized nodes. Embeddings are cached in-process by
SHA-256 of the text.

### Symbol index & dependency graph

`app/rag/symbols.py` — an **AST-based** (stdlib `ast`, **not** tree-sitter) index of Python
definitions, imports, and call sites, in a standalone sync sqlite3 DB (`.symbols.db`). Built during
the same file walk as embedding: `Retriever._index_single_file()` calls `symbol_index.index_file()`
(best-effort, never blocks embedding); `delete_file()` removes its rows. `index_file()` replaces a
file's rows wholesale, so it is the incremental-reindex primitive. Tables: `symbols` (defs),
`imports` (file→file dependency edges, resolved against project root), `refs` (call sites). Exposed
to the agent via the `find_symbol` / `find_references` builtin tools. Non-Python files yield no
symbols (graceful). Inject an in-memory index (`SymbolIndex(db_path=":memory:")`) for tests.

### Persistence

- `.chroma_db/` — ChromaDB vectors (per-project collections)
- `.coder.db` — SQLite: conversation turns + project summaries (SQLAlchemy async / aiosqlite)
- `.symbols.db` — sqlite3: symbol/import/reference index (sync, separate from `.coder.db`)
- `.coder_history` — prompt_toolkit history
- `.coder_backups/` — pre-mutation snapshots for `undo_write` (pruned to `max_write_backups`)

### MCP servers (`app/mcp/`)

stdio transport only. `MCPManager.connect_server()` runs a background asyncio task
(`MCPServerConnection._run`) that holds the stdio session open via an `asyncio.Event` gate; tools
are discovered (`list_tools()`), wrapped as async `ToolDefinition`s with `source="mcp:<name>"`, and
registered. `CoderREPL.run()` auto-loads servers from `config/mcp_servers.json` on startup.

### Skills (`skills/`)

Each skill = a folder with a `SKILL.md` containing **`## Description`, `## Trigger Keywords`,
`## Instructions`** (parser is header-strict; a skill with neither description nor instructions is
dropped). `SkillLoader.load_all()` scans **once at startup** — there is no hot-reload, adding/editing
a skill needs a restart. Per turn, `match_skills()` scores each enabled skill (0.5·keyword-overlap +
0.5·embedding-cosine, threshold 0.25, **max 2** injected) and the result is injected as a system
prompt block.

### Config

`config/settings.py` — single pydantic-settings `Settings` instance reading `.env`. Import as
`from config.settings import settings`. For shell commands only `blocked_commands` is enforced
(in `app/tools/terminal.py`); `allowed_commands` is deliberately informational (an allowlist
would break legitimate loop commands like `pytest`). Tool-level gating is
`denied_permissions` (enforced in the Executor, default empty). `max_context_tokens` is
the per-prompt token budget enforced by `app/agent/context_budget.py` (oldest history dropped
first in `_build_messages`); `max_repair_attempts` caps the verify-and-repair loop;
`backups_dir` / `max_write_backups` configure safe-write snapshots.

### Eval harness (`evals/`)

The measuring stick for model/prompt changes. `evals/tasks.py` holds ~12 golden tasks asserting
**observable** outcomes (file on disk, answer token, N files written) via declarative checks
(`evals/checks.py`). `evals/harness.py` runs each prompt through `AgentCore.chat` in an isolated
cwd and scores the suite; the harness logic is unit-tested offline (`tests/test_evals.py`) with a
scripted LLM. The **live** run is `python -m evals.run` (needs Ollama; NOT part of `pytest`) —
`--keep DIR`, `--min SCORE`, `--only ids`. Run it before/after a model or prompt change: the first
baseline (qwen2.5-coder:7b) was 10/12 and immediately caught a real multi-file routing bug.

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
- **Import-time singletons.** Importing `app.database.vector_store` constructs the ChromaDB client
  (creates `.chroma_db/`), and `app.agent.tool_registry` builds the registry. Merely importing the
  package writes `.chroma_db/` / `.coder.db` into the cwd. Tests rely on this being side-effect-safe.
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
