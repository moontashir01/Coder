# Changelog

All notable changes to Coder are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
