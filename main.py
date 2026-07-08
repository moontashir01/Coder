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


@app.command()
def main(
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

    async def _run() -> None:
        try:
            if project:
                await repl.load_project(project)
            await repl.run()
        finally:
            agent.close()  # stop the live-reindex file watcher

    asyncio.run(_run())


if __name__ == "__main__":
    app()
