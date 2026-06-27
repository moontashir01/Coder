#!/usr/bin/env python3
"""Coder — Offline AI Coding Assistant"""

import asyncio
import sys
from pathlib import Path

import typer

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from app.agent.core import AgentCore
from app.cli.repl import CoderREPL
from app.mcp.manager import MCPManager
from app.models.llm import test_connection
from app.skills.loader import SkillLoader

app = typer.Typer(help="Coder — offline AI coding assistant powered by Ollama.")


@app.command()
def main(
    project: str = typer.Option(
        None, "--project", "-p", help="Project folder to load on startup"
    ),
    session: str = typer.Option(
        "default", "--session", "-s", help="Conversation session ID"
    ),
) -> None:
    """Start the Coder interactive assistant."""
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
        if project:
            await repl.load_project(project)
        await repl.run()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
