# Coder

A **fully offline** AI coding assistant for your terminal. It talks only to a local
[Ollama](https://ollama.com) instance — nothing leaves your machine. Install it once, then type
**`coder`** in any project folder and it works on that folder, like `claude` or `opencode`.

- 🔒 **Offline** — your code never leaves the machine
- 🧠 Local LLM (`qwen2.5-coder:7b`) + embeddings (`nomic-embed-text`) via Ollama
- 🗂️ Project-aware: RAG retrieval, a symbol/dependency index, skills, and MCP tools
- 🛠️ Reads, writes, and edits files; runs commands; git-aware — all with safe-write backups

---

## Requirements

| | |
|---|---|
| **OS** | Windows, macOS, or Linux |
| **Python** | 3.11 or 3.12 &nbsp;·&nbsp; *not 3.13+* (a pinned dependency has no wheels above 3.12) |
| **Ollama** | Installed and running — the installer sets this up for you |
| **Disk** | ~5 GB for the two models |

The installer will find or install Python 3.12 and Ollama for you where possible, so on most
machines you don't need to install anything by hand first.

---

## Install

```bash
git clone https://github.com/moontashir01/Coder.git
cd Coder
```

**Windows** (PowerShell):
```powershell
./install.ps1
# If script execution is blocked:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**macOS / Linux**:
```bash
./install.sh
```

The installer will:
1. find or install a compatible **Python 3.11/3.12**,
2. create an isolated `.venv` and install Coder into it,
3. register a global **`coder`** command on your PATH,
4. ensure **Ollama** is installed and running, and pull the two required models.

> Add `-NoOllama` (Windows) or `--no-ollama` (macOS/Linux) to set up only the CLI and manage
> Ollama yourself.

When it finishes, **open a new terminal** so the updated PATH takes effect.

---

## Use

From any project directory:

```bash
cd path/to/your/project
coder
```

That starts the interactive assistant scoped to the current folder. Useful things to try:

- Ask a question: `explain what @src/app.py does`
- Create a file: `make an index.html landing page`
- Load the project for retrieval + symbol search: `/load .`
- List commands: `/help`

Coder writes its per-project state (`.chroma_db/`, `.coder.db`, `.symbols.db`, `.coder_backups/`,
`.coder_embed_cache/`) into the folder you launch it from, so each project stays isolated. The
embedding cache persists across restarts and files honored by `.gitignore` are skipped, so the
second load of an unchanged repo is near-instant. Once a project is loaded, edits on disk are
re-indexed automatically within about a second — no manual `/index` needed.

Other entry points:

```bash
coder --project path/to/proj   # load + index a project on startup
coder --session work           # named, persistent conversation session
coder --version
```

---

## Manual install (no script)

```bash
# from the repo root, with Python 3.12 available as `py -3.12` (Win) or `python3.12` (Unix)
py -3.12 -m venv .venv                 # Windows
python3.12 -m venv .venv               # macOS/Linux

.venv\Scripts\activate                 # Windows
source .venv/bin/activate              # macOS/Linux

pip install -e .
coder                                  # available while the venv is active
```

Then make sure Ollama is serving with the models:

```bash
ollama serve
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

---

## Uninstall

Delete the shim and the cloned folder:

- **Windows:** remove `%LOCALAPPDATA%\Coder\bin` (and drop it from your user PATH), then delete the repo.
- **macOS/Linux:** `rm ~/.local/bin/coder`, then delete the repo.

---

## Development

Run the offline test suite (no Ollama needed):

```bash
pip install -e ".[dev]"
pytest -q
```

Architecture notes, design rationale, and contributor guidance live in
[CLAUDE.md](CLAUDE.md).
