"""Slash-command handlers for the Coder REPL."""
from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

if TYPE_CHECKING:
    from app.cli.repl import CoderREPL

console = Console()

HELP_TEXT = """
[bold cyan]Coder — Slash Commands[/bold cyan]

[yellow]Project[/yellow]
  /load <path>          Load and index a project folder
  /project              Show the currently loaded project
  /index                Re-index the current project

[yellow]Tools & Context[/yellow]
  /tools                List all registered tools (builtin + MCP)
  /undo [path]          Undo the last file write/edit/delete (restores backup)
  /history              Show recent conversation turns
  /clear                Clear conversation history

[yellow]MCP Servers[/yellow]
  /mcp list             List connected MCP servers and their tools
  /mcp add <name> <cmd> [args...]  Add and connect an MCP server
  /mcp remove <name>    Disconnect an MCP server

[yellow]Skills[/yellow]
  /skills list          List all discovered skills
  /skills enable <name> Enable a skill
  /skills disable <name> Disable a skill

[yellow]Session[/yellow]
  /help                 Show this help
  /exit  /quit          Exit Coder
"""


async def handle_command(line: str, repl: CoderREPL) -> bool:
    """Dispatch a slash command. Returns True if handled, False if unknown."""
    parts = shlex.split(line.lstrip("/").strip()) if line.strip() else []
    if not parts:
        return False

    cmd = parts[0].lower()
    args = parts[1:]

    # ── /help ──────────────────────────────────────────────────────────
    if cmd == "help":
        console.print(HELP_TEXT)
        return True

    # ── /exit /quit ────────────────────────────────────────────────────
    if cmd in ("exit", "quit"):
        console.print("[bold yellow]Goodbye![/bold yellow]")
        repl.running = False
        return True

    # ── /load ──────────────────────────────────────────────────────────
    if cmd == "load":
        if not args:
            console.print("[red]Usage: /load <path>[/red]")
            return True
        path = " ".join(args)
        await repl.load_project(path)
        return True

    # ── /project ───────────────────────────────────────────────────────
    if cmd == "project":
        if repl.agent.project_path:
            console.print(f"[green]Active project:[/green] {repl.agent.project_path}")
        else:
            console.print("[yellow]No project loaded. Use /load <path>[/yellow]")
        return True

    # ── /index ─────────────────────────────────────────────────────────
    if cmd == "index":
        if not repl.agent.project_path:
            console.print("[red]No project loaded.[/red]")
            return True
        console.print("[cyan]Re-indexing...[/cyan]")
        stats = await repl.agent.load_project(repl.agent.project_path)
        console.print(f"[green]Indexed:[/green] {stats}")
        return True

    # ── /tools ─────────────────────────────────────────────────────────
    if cmd == "tools":
        tools = repl.agent.registry.list_all()
        table = Table(title="Registered Tools", show_lines=False)
        table.add_column("Name", style="cyan")
        table.add_column("Source", style="yellow")
        table.add_column("Description")
        for t in tools:
            table.add_row(t.name, t.source, t.description)
        console.print(table)
        return True

    # ── /undo ──────────────────────────────────────────────────────────
    if cmd == "undo":
        from app.tools.filesystem import undo_write

        res = undo_write(path=" ".join(args) if args else None)
        if res["success"]:
            console.print(f"[green]{res['result']}[/green]")
        else:
            console.print(f"[yellow]{res['error']}[/yellow]")
        return True

    # ── /clear ─────────────────────────────────────────────────────────
    if cmd == "clear":
        await repl.agent.clear_memory()
        console.print("[green]Conversation history cleared.[/green]")
        return True

    # ── /history ───────────────────────────────────────────────────────
    if cmd == "history":
        turns = await repl.agent.memory.recent_turns(10)
        if not turns:
            console.print("[yellow]No history yet.[/yellow]")
        for t in turns:
            role_color = "green" if t["role"] == "human" else "blue"
            label = "You" if t["role"] == "human" else "Coder"
            console.print(f"[{role_color}]{label}:[/{role_color}] {t['content'][:120]}")
        return True

    # ── /mcp ───────────────────────────────────────────────────────────
    if cmd == "mcp":
        if not args:
            console.print("[red]Usage: /mcp list | /mcp add <name> <cmd> [...] | /mcp remove <name>[/red]")
            return True
        sub = args[0].lower()

        if sub == "list":
            mgr = _get_mcp_manager(repl)
            if mgr is None:
                return True
            servers = mgr.list_servers()
            if not servers:
                console.print("[yellow]No MCP servers configured.[/yellow]")
            for s in servers:
                status = "[green]connected[/green]" if s.get("connected") else "[red]disconnected[/red]"
                console.print(f"  {s['name']} — {status} — {s.get('tool_count', 0)} tools")
            return True

        if sub == "add":
            if len(args) < 3:
                console.print("[red]Usage: /mcp add <name> <command> [args...][/red]")
                return True
            mgr = _get_mcp_manager(repl)
            if mgr is None:
                return True
            name, command, *cmd_args = args[1:]
            config = {"name": name, "command": command, "args": cmd_args, "env": {}}
            await mgr.connect_server(config, repl.agent.registry)
            console.print(f"[green]Connected MCP server:[/green] {name}")
            return True

        if sub == "remove":
            if len(args) < 2:
                console.print("[red]Usage: /mcp remove <name>[/red]")
                return True
            mgr = _get_mcp_manager(repl)
            if mgr is None:
                return True
            await mgr.disconnect_server(args[1], repl.agent.registry)
            console.print(f"[yellow]Disconnected:[/yellow] {args[1]}")
            return True

        console.print(f"[red]Unknown mcp sub-command: {sub}[/red]")
        return True

    # ── /skills ────────────────────────────────────────────────────────
    if cmd == "skills":
        if not args:
            console.print("[red]Usage: /skills list | /skills enable <name> | /skills disable <name>[/red]")
            return True
        sub = args[0].lower()
        loader = _get_skill_loader(repl)
        if loader is None:
            return True

        if sub == "list":
            skills = loader.list_skills()
            if not skills:
                console.print("[yellow]No skills found.[/yellow]")
                return True
            table = Table(title="Skills", show_lines=False)
            table.add_column("Name", style="cyan")
            table.add_column("Status", style="yellow")
            table.add_column("Keywords")
            for s in skills:
                status = "[green]enabled[/green]" if s.enabled else "[dim]disabled[/dim]"
                table.add_row(s.name, status, ", ".join(s.trigger_keywords[:5]))
            console.print(table)
            return True

        if sub == "enable":
            if len(args) < 2:
                console.print("[red]Usage: /skills enable <name>[/red]")
                return True
            loader.enable(args[1])
            console.print(f"[green]Enabled skill:[/green] {args[1]}")
            return True

        if sub == "disable":
            if len(args) < 2:
                console.print("[red]Usage: /skills disable <name>[/red]")
                return True
            loader.disable(args[1])
            console.print(f"[yellow]Disabled skill:[/yellow] {args[1]}")
            return True

        console.print(f"[red]Unknown skills sub-command: {sub}[/red]")
        return True

    return False   # unknown command


def _get_mcp_manager(repl: CoderREPL):
    mgr = getattr(repl, "mcp_manager", None)
    if mgr is None:
        console.print("[yellow]MCP manager not initialised.[/yellow]")
    return mgr


def _get_skill_loader(repl: CoderREPL):
    loader = getattr(repl, "skill_loader", None)
    if loader is None:
        console.print("[yellow]Skills loader not initialised.[/yellow]")
    return loader
