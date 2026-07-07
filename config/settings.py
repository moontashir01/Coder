import os
from pathlib import Path

from pydantic_settings import BaseSettings

# Install base: the directory that ships with Coder's bundled resources
# (prompts/, skills/, config/). Resolved once, independent of the current
# working directory, so a globally-installed `coder` finds its own resources
# no matter which project folder it is run from. Order of precedence:
#   1. $CODER_HOME (explicit override)
#   2. the source tree — this file lives at <base>/config/settings.py
# NB: runtime STATE paths below (chroma/sqlite/symbols/backups/history) stay
# relative so they land in the *current* project folder, per-project.
_BASE = Path(os.environ.get("CODER_HOME") or Path(__file__).resolve().parent.parent)


class Settings(BaseSettings):
    # Model config
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5-coder:7b"
    embedding_model: str = "nomic-embed-text"
    # 7B is meaningfully slower than the old 3B default on the same hardware;
    # keep a generous per-request timeout so longer generations aren't cut off.
    llm_request_timeout_seconds: int = 120

    # Bundled-resource paths — anchored to the install base (see _BASE above)
    # so they resolve identically from any working directory.
    skills_dir: Path = _BASE / "skills"
    prompts_dir: Path = _BASE / "prompts"
    mcp_config: Path = _BASE / "config" / "mcp_servers.json"

    # Per-project paths — relative on purpose (resolved against cwd) so state
    # is created in whatever project folder `coder` is launched from.
    project_root: Path = Path(".")
    projects_dir: Path = Path("projects")
    models_dir: Path = Path("models")
    chroma_persist_dir: Path = Path(".chroma_db")
    sqlite_path: Path = Path(".coder.db")
    symbols_path: Path = Path(".symbols.db")

    # Agent config
    # qwen2.5-coder:7b handles a larger context window than the old 3B default;
    # raised from 4096 accordingly.
    max_context_tokens: int = 8192
    max_tool_retries: int = 3
    max_tool_failures: int = 2  # §11: give up a tool after this many failures
    # Verify-and-repair: how many LLM repair passes to run when a just-written
    # file fails its syntax/structure check.
    max_repair_attempts: int = 2
    retrieval_top_k: int = 5
    conversation_buffer_size: int = 20

    # Safety
    # Safe writes (Tier 3 #8): mutating file tools back up the previous
    # content here first; undo_write restores the most recent backup.
    backups_dir: Path = Path(".coder_backups")
    max_write_backups: int = 20
    # Permission gating (Tier 3 #8): the Executor refuses any tool whose
    # ToolDefinition.permissions intersects this list. Tags in use:
    # fs:read, fs:write, fs:delete, shell, git:read, git:write, mcp.
    denied_permissions: list[str] = []
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
