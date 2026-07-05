from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Model config
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5-coder:7b"
    embedding_model: str = "nomic-embed-text"
    # 7B is meaningfully slower than the old 3B default on the same hardware;
    # keep a generous per-request timeout so longer generations aren't cut off.
    llm_request_timeout_seconds: int = 120

    # Paths
    project_root: Path = Path(".")
    skills_dir: Path = Path("skills")
    projects_dir: Path = Path("projects")
    models_dir: Path = Path("models")
    chroma_persist_dir: Path = Path(".chroma_db")
    sqlite_path: Path = Path(".coder.db")
    symbols_path: Path = Path(".symbols.db")
    mcp_config: Path = Path("config/mcp_servers.json")

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
