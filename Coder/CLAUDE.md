# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Coder** — a fully **offline** AI coding assistant. It talks only to a local Ollama
(`http://localhost:11434`); nothing leaves the machine. `qwen2.5-coder:3b` is the default LLM,
`nomic-embed-text` the only embedding model. Primary interface is a CLI/REPL. ChromaDB for
vectors, SQLite for memory, LangChain for the Ollama wrappers.

## Prerequisites

Ollama running with both models pulled:
```
ollama serve
ollama pull qwen2.5-coder:3b
ollama pull nomic-embed-text
```
All Python work uses the venv (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Unix).

## Common Commands

```bash
python main.py                          # start the REPL
python main.py --project /path/to/proj  # load + index a project on startup
python main.py --session work           # named conversation session (persists in SQLite)

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
  ├─ if _wants_file_op(msg) or task_type=="file_edit":
  │      _file_op_flow(msg)                  # DETERMINISTIC: gen full file → write_file
  │  elif task_type=="multi_step" and project loaded:
  │      _build_messages() → _run_tool_loop()   # ReAct JSON tool loop
  │  else:
  │      _direct_answer()                    # one plain LLM call, NO tool protocol
  └─ memory.add_ai(answer)
```

**Why three paths:** the 3B model is unreliable at the JSON tool protocol. So:
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
- **Genuine multi-step work in a loaded project → `_run_tool_loop`** (ReAct).
- **Everything else (write/explain code, Q&A) → `_direct_answer`** (one call, no tools).

**`prompts/system.md` must NOT contain the tool-call JSON instructions** — the tool loop gets its
protocol from `_tool_loop_prompt()` instead. If you put tool-protocol text back in system.md it
leaks into `_direct_answer`/`_file_op_flow` and the model emits fake tool-call JSON instead of the
file/answer.

### Tool-call protocol (the ReAct loop)

`_run_tool_loop()` prompts the LLM to emit ONLY JSON, two shapes:
```json
{"action": "tool_call", "tool": "<name>", "arguments": {}}
{"action": "final_answer", "answer": "<text>"}
```
Small-model hardening lives here and in `_tool_loop_prompt()`:
- The prompt **lists the real registered tool names** and tells the model to return
  `final_answer` directly when no file/command access is needed (don't invent tools).
- A `"Tool not found"` result triggers a **firm correction** message (lists valid tools, tells it
  to answer now) instead of letting it retry a hallucinated tool until `max_steps`.
- `_normalize_action()` repairs loose JSON: a flattened call
  (`{"action":"write_file","path":...}`) or a bare `{"tool":...}`/`{"answer":...}` is coerced into
  the canonical shape. `_coerce_args()` then maps arg synonyms (`file_path`/`filename` → `path`).
- Unparseable output retries up to `settings.max_tool_retries`.

### Tool registry & executor — the central hub

- `app/agent/tool_registry.py` — every tool (builtin, MCP-discovered, skill-unlocked) must be
  registered here. `create_registry()` builds the default with all 12 builtin tools; a
  module-level `registry` singleton exists. Tools carry `source` = `"builtin"` | `"mcp:<server>"`
  | `"skill:<skill>"`; `unregister_by_source()` is how MCP disconnect cleans up.
- `app/agent/executor.py` — async `execute()`: validates args against the tool's JSON Schema, then
  awaits async handlers (MCP) or runs sync handlers in a thread pool. **Every tool handler must
  return `{"success": bool, "result": str, "error": str | None}`** — this contract is assumed
  everywhere (REPL tool-step rendering, the tool loop's result feedback).

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
`from config.settings import settings`. Only `blocked_commands` is enforced (in
`app/tools/terminal.py`); `allowed_commands` is currently informational.

## Gotchas

- **Tree-sitter is currently broken in this env.** `tree-sitter 0.25.2` + `tree-sitter-languages
  1.10.2` are incompatible (`get_parser('python')` raises `TypeError: __init__() takes exactly 1
  argument (2 given)`). `_chunk_with_tree_sitter` swallows this and silently falls back to
  token-window chunking, so semantic chunking is **not** actually running. To restore it, either pin
  `tree-sitter==0.21.3` or migrate to `tree-sitter-language-pack`. The symbol index (`symbols.py`)
  sidesteps this entirely by using stdlib `ast` for Python.
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
