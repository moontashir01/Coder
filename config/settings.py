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

    # Bundled-resource paths — anchored to the package's resources dir (see
    # _RESOURCES above) so they resolve identically from any working directory
    # and ship as package data in a wheel/pipx install.
    skills_dir: Path = _RESOURCES / "skills"
    prompts_dir: Path = _RESOURCES / "prompts"
    mcp_config: Path = _RESOURCES / "mcp_servers.json"

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
    max_tool_retries: int = 3
    max_tool_failures: int = 2  # §11: give up a tool after this many failures
    # M4: how many tool-call rounds the native tool loop may take before it
    # stops. Raised from the old hard-coded 8 so genuinely multi-part work has
    # room to finish every step.
    max_tool_steps: int = 12
    # M1: split a compound request ("do A, then B, and C") into ordered
    # sub-tasks and route each one, instead of only handling the first. When the
    # cheap regex splitter sees a single task but the planner classifies it as
    # multi_step, fall back to the LLM planner to decompose it.
    decompose_multitask: bool = True
    # Verify-and-repair: how many LLM repair passes to run when a just-written
    # file fails its syntax/structure check.
    max_repair_attempts: int = 2
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
