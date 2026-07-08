#!/usr/bin/env python3
"""Coder — Offline AI Coding Assistant"""

import asyncio
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
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,  # print-and-exit before the Ollama connection check
        help="Show version and exit",
    ),
) -> None:
    """Start the Coder interactive assistant."""
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
