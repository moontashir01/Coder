#!/usr/bin/env python3
"""Coder — Offline AI Coding Assistant"""

import asyncio
import subprocess
import sys
from pathlib import Path

import typer

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from app import __version__
from app.agent.core import AgentCore
from app.cli.repl import CoderREPL
from app.mcp.manager import MCPManager
from app.models.llm import test_connection
from app.skills.loader import SkillLoader
from config.settings import settings

app = typer.Typer(help="Coder — offline AI coding assistant powered by Ollama.")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"coder {__version__}")
        raise typer.Exit()


def _startup_project(project: str | None, no_index: bool) -> str | None:
    """Which project to load on startup (Step 15 / U2).

    Explicit --project wins; otherwise auto-load the current directory unless
    --no-index was passed.
    """
    if project:
        return project
    if no_index:
        return None
    return str(Path.cwd())


def _run_update(dry_run: bool) -> None:
    """`coder --update` (Step 14 / D4): pull the latest source and reinstall.

    Editable/source install → `git pull` + `pip install -e .` in the install
    dir (the console script is regenerated, so the PATH shim keeps working). A
    non-git install (pipx/wheel) has nothing to pull → point at `pipx upgrade`.
    """
    install_dir = Path(__file__).resolve().parent
    is_git = (install_dir / ".git").is_dir()
    if is_git:
        actions = [
            ["git", "-C", str(install_dir), "pull", "--ff-only"],
            [sys.executable, "-m", "pip", "install", "-e", str(install_dir)],
        ]
    else:
        actions = [["pipx", "upgrade", "coder"]]

    if dry_run:
        typer.echo("coder --update would run:")
        for a in actions:
            typer.echo("  " + " ".join(a))
        return

    for a in actions:
        typer.echo("$ " + " ".join(a))
        try:
            subprocess.run(a, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            typer.echo(f"[ERROR] update step failed: {e}", err=True)
            raise typer.Exit(code=1)
    typer.echo("Update complete — restart coder to load the new version.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", help="Project folder to load on startup"
    ),
    session: str = typer.Option(
        "default", "--session", "-s", help="Conversation session ID"
    ),
    allow_outside_root: bool = typer.Option(
        False,
        "--allow-outside-root",
        help="Let file tools read/write outside the project root (Step 5)",
    ),
    yolo: bool = typer.Option(
        False, "--yolo", help="Auto-approve all writes, deletes, and commands"
    ),
    safe: bool = typer.Option(
        False,
        "--safe",
        help="Deny shell and file deletes unless interactively approved",
    ),
    allow_network: bool = typer.Option(
        False, "--allow-network", help="Permit network-reaching shell commands"
    ),
    no_index: bool = typer.Option(
        False, "--no-index", help="Don't auto-load/index the current directory on startup"
    ),
    update: bool = typer.Option(
        False, "--update", help="Update Coder to the latest version and exit"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="With --update: print the actions instead of running them"
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,  # print-and-exit before the Ollama connection check
        help="Show version and exit",
    ),
) -> None:
    """Start the Coder interactive assistant."""
    # --update runs before the Ollama check (no local model needed to upgrade).
    if update:
        _run_update(dry_run)
        raise typer.Exit()

    # A subcommand (coder init / coder config) handles the invocation itself.
    if ctx.invoked_subcommand is not None:
        return

    # Security profile (Phase B). Default the path jail to cwd; if --project is
    # given, load_project narrows it to the project dir.
    settings.sandbox_root = Path.cwd().resolve()
    settings.allow_outside_root = allow_outside_root
    settings.auto_approve = yolo
    settings.safe_mode = safe
    settings.allow_network = allow_network

    try:
        test_connection()
    except RuntimeError as e:
        typer.echo(f"[ERROR] {e}", err=True)
        raise typer.Exit(code=1)

    mcp_manager = MCPManager()
    skill_loader = SkillLoader()
    skill_loader.load_all()
    agent = AgentCore(session_id=session, mcp_manager=mcp_manager, skill_loader=skill_loader)
    repl = CoderREPL(agent=agent, mcp_manager=mcp_manager, skill_loader=skill_loader)

    startup_project = _startup_project(project, no_index)

    async def _run() -> None:
        try:
            if startup_project:
                await repl.load_project(startup_project)
            await repl.run()
        finally:
            agent.close()  # stop the live-reindex file watcher

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# First-run setup / settings (Step 15 / U3)
# ---------------------------------------------------------------------------

_ENV_TEMPLATE = """\
# Coder configuration. Uncomment and edit to override the defaults, then
# restart coder. Keys are case-insensitive and map to config/settings.py fields.
# LLM_MODEL=qwen2.5-coder:7b
# EMBEDDING_MODEL=nomic-embed-text
# OLLAMA_BASE_URL=http://localhost:11434
# MAX_CONTEXT_TOKENS=8192
# RETRIEVAL_TOP_K=5
"""

# Settings surfaced by `coder config` (name → the live value).
_CONFIG_KEYS = (
    "llm_model",
    "embedding_model",
    "ollama_base_url",
    "max_context_tokens",
    "retrieval_top_k",
)


def _set_env_var(env_path: Path, key: str, value: str) -> None:
    """Insert or replace a KEY=VALUE line in a .env file."""
    key = key.upper()
    lines = (
        env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    )
    out, replaced = [], False
    for line in lines:
        stripped = line.lstrip("# ").strip()
        if stripped.upper().startswith(key + "="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .env"),
) -> None:
    """Create a .env with common settings and print first-run steps."""
    env_path = Path(".env")
    if env_path.exists() and not force:
        typer.echo(f".env already exists at {env_path.resolve()} (use --force to overwrite).")
    else:
        env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")
        typer.echo(f"Wrote {env_path.resolve()}")
    typer.echo("\nNext steps:")
    typer.echo("  1. ollama serve")
    typer.echo(f"  2. ollama pull {settings.llm_model}")
    typer.echo(f"  3. ollama pull {settings.embedding_model}")
    typer.echo("  4. coder")


@app.command()
def config(
    key: str = typer.Argument(None, help="Setting to change (e.g. llm_model)"),
    value: str = typer.Argument(None, help="New value"),
) -> None:
    """Show the current settings, or `coder config KEY VALUE` to set one in .env."""
    if key and value is not None:
        _set_env_var(Path(".env"), key, value)
        typer.echo(f"Set {key.upper()}={value} in {Path('.env').resolve()}")
        return
    typer.echo("Current configuration:")
    for k in _CONFIG_KEYS:
        typer.echo(f"  {k} = {getattr(settings, k)}")
    typer.echo(f"\nEdit {Path('.env').resolve()} or run: coder config <KEY> <VALUE>")


if __name__ == "__main__":
    app()
