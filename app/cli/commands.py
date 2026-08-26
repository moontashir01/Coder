"""Slash-command handlers for the Coder REPL."""

from __future__ import annotations

import asyncio
import shlex
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from app.agent.instructions import instructions_path
from app.agent.stacks import describe_stacks, get_adapter, resolve_key, stack_keys
from config.settings import settings

if TYPE_CHECKING:
    from app.cli.repl import CoderREPL

console = Console()

HELP_TEXT = """
[bold cyan]Coder — Slash Commands[/bold cyan]

[yellow]Project[/yellow]
  /load <path>          Load and index a project folder
  /project              Show the currently loaded project
  /index                Re-index the current project
  /spec                 Show what the agent remembers about this project
                        (tables, routes, pages — from .coder/project.json)
  /instructions         Show this project's conventions, which you write in
                        .coder/INSTRUCTIONS.md and which apply to every turn
  /stack [flask|node]   Show or choose the stack a build targets. A project
                        that already has a spec keeps ITS stack regardless
  /run [restart|stop|status]  Start the generated app and keep it up across
                        turns; prints the URL to open. Does whatever setup the
                        project needs first (on Node: installs its packages,
                        creates its database, loads demo data)
  /point [change]       Click a part of the running page and change it. Opens
                        the app, waits for a click, and edits exactly the
                        template lines behind what you picked — no guessing at
                        which file. Needs /run first

[yellow]Tools & Context[/yellow]
  /tools                List all registered tools (builtin + MCP)
  /plan <task>          Preview how a task decomposes into steps (no execution)
  /model [name]         Show or switch the Ollama model (e.g. qwen2.5-coder:14b)
  /undo [path]          Undo the last file write/edit/delete (restores backup)
  /history              Show recent conversation turns
  /export [file]        Write this session's full working history to Markdown —
                        every turn, the route it took, the tools it ran and the
                        files it wrote. [dim]/export sessions[/dim] lists what was
                        recorded; [dim]--session <id>[/dim] exports another one
  /clear                Clear conversation history

[yellow]MCP Servers[/yellow]
  /mcp list             List connected MCP servers and their tools
  /mcp add <name> <cmd> [args...]  Add and connect an MCP server
  /mcp remove <name>    Disconnect an MCP server

[yellow]Skills[/yellow]
  /skills list          List all discovered skills
  /skills enable <name> Enable a skill
  /skills disable <name> Disable a skill

[yellow]Telegram bot[/yellow]
  /bot start            Run the Telegram front-end alongside this terminal —
                        same project, same memory, one turn at a time
  /bot stop  /bot status
  /bot pair [role]      Mint a one-time invite code (viewer|developer|owner)

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

    # ── /run ───────────────────────────────────────────────────────────
    # Keep the generated app up ACROSS turns, so the demo has a live URL while
    # the next turn amends the project (docs/fullstack-web-plan.md Phase 6).
    if cmd == "run":
        from app.agent.apprunner import get_runner

        runner = get_runner()
        action = args[0].lower() if args else "start"

        async def repair_entry(root) -> None:
            """Re-assert the entry file's startup invariants before launching.

            The passes that keep `server.js` / `app.py` startable run at the
            build seam, and `/run` is the one path that launches a project with
            no turn around it — so a rewrite that ended the entry file at its
            last route stayed broken through every `/run`, reported only as
            "exited on startup". Deterministic and idempotent: a healthy file
            is read and not written. Guarded on the attribute because the CLI
            is driven by stand-in agents in the tests, and a `/run` that failed
            on a missing method would be a worse bug than the one this fixes.
            """
            repair = getattr(repl.agent, "repair_entry_before_run", None)
            if repair is None:
                return
            for line in await repair(root):
                console.print(f"[green]✓[/green] {escape(line)}")

        if action in ("stop", "kill"):
            console.print(
                "[green]Stopped.[/green]"
                if runner.stop()
                else "[yellow]Nothing was running.[/yellow]"
            )
            return True

        if action == "status":
            console.print(f"[cyan]App:[/cyan] {runner.status()}")
            return True

        if action == "restart":
            # The restart path is where this matters most: it is what someone
            # types straight after the amendment that broke the file.
            await repair_entry(runner.workdir)
            ok, message = runner.restart()
            console.print(
                f"[green]Restarted:[/green] {message}"
                if ok
                else f"[red]{message}[/red]"
            )
            return True

        # Which file to run is the stack's answer, not a constant: `app.py` on
        # Flask, `server.js` on Node. Read from the project's own spec, so
        # opening a Node project and typing `/run` does not look for an app.py
        # that was never written.
        workdir = repl.agent.project_path or str(Path.cwd())
        adapter = get_adapter(resolve_key(repl.agent.get_spec(), settings.web_stack))

        # Before the environment, the code: a file that cannot start will not
        # start however well `npm install` goes, and this costs one file read
        # when nothing is wrong.
        await repair_entry(workdir)

        # Do the setup before asking whether it can run. Three of the blockers
        # `readiness` names are really just commands — `npm install`, `createdb`,
        # the seed — and printing them is the difference between "here is your
        # site" and a terminal exercise for someone who does not use one. What
        # cannot be done this way (no Node, no PostgreSQL service, a wrong
        # password) is untouched and still reported below. Flask has nothing to
        # do here and returns [], so that stack prints nothing new.
        if getattr(settings, "auto_setup", True):
            for line in adapter.autosetup(
                Path(workdir),
                log=lambda m: console.print(f"[dim]  {escape(m)}…[/dim]"),
            ):
                console.print(f"[green]✓[/green] {escape(line)}")

        # Phase N5: say WHY before launching something that cannot work. Without
        # this the Node stack's answer to a missing database is whatever `pg`
        # printed on the way down, relayed as "server.js exited on startup" —
        # true, unhelpful, and it reads as a defect in the generated code. The
        # readiness gate names the cause instead. Flask returns "" always, so
        # `/run` there is unchanged.
        blocked = adapter.readiness(Path(workdir))
        if blocked:
            console.print(f"[red]Not started — {blocked}[/red]")
            console.print(
                "[dim]Nothing was launched, so nothing here says the app is "
                "broken. Fix the above and run `/run` again.[/dim]"
            )
            return True

        ok, message = runner.start(workdir, adapter.entry_file)
        if ok:
            console.print(f"[green]App {message}[/green]")
            console.print(
                "[dim]It stays up across turns. `/run restart` after a change, "
                "`/run stop` when you're done.[/dim]"
            )
        else:
            console.print(f"[red]{message}[/red]")
        return True

    # ── /point ─────────────────────────────────────────────────────────
    # Click the page, say what to change. The whole value is that the TARGET
    # stops being inferred: `app/agent/pointer.py` turns the click into the
    # exact span of template source behind it, so the model is never asked
    # which file, which block, or which lines — only what the replacement says.
    if cmd == "point":
        from app.agent.apprunner import get_runner
        from app.agent.pointer import Decline, capture_click

        runner = get_runner()
        if not runner.is_running:
            console.print(
                "[yellow]The app is not running.[/yellow] Start it with "
                "[cyan]/run[/cyan] first — pointing needs a live page."
            )
            return True

        console.print(
            f"[cyan]Opening[/cyan] {runner.url} — click the part you want to "
            "change. [dim](Close the window to cancel.)[/dim]"
        )
        # Playwright's sync API refuses to run in a thread that owns a live
        # event loop, and this one is inside the REPL's — `browser.py`'s rule,
        # and the `to_thread` worker does not own one.
        clicked = await asyncio.to_thread(
            capture_click, runner.url, float(settings.point_timeout)
        )
        if isinstance(clicked, Decline):
            console.print(f"[yellow]{escape(clicked.reason)}[/yellow]")
            return True

        label = clicked.text[:60] or clicked.element_id or clicked.tag
        console.print(f"[green]Picked[/green] <{clicked.tag}> {escape(label)}")

        instruction = " ".join(args).strip()
        if not instruction:
            instruction = (await repl.prompt_async("  change it to: ")).strip()
        if not instruction:
            console.print("[yellow]No change described — nothing was done.[/yellow]")
            return True

        answer, _trace = await repl.agent.edit_pointed_element(
            clicked, instruction, repl.agent.project_path
        )
        console.print(escape(answer))
        console.print(
            "[dim]`/run restart` to see it.[/dim]" if runner.is_running else ""
        )
        return True

    # ── /plan ──────────────────────────────────────────────────────────
    if cmd == "plan":
        if not args:
            console.print("[red]Usage: /plan <task description>[/red]")
            return True
        task = " ".join(args)

        # When the project is remembered, a change request has a far more useful
        # preview than a generic step list: the delta, and the existing files it
        # will drag along. Showing that BEFORE it happens is the demo beat.
        preview = await repl.agent.preview_amendment(task)
        if preview:
            console.print(
                f"[bold]Amendment[/bold] to revision {preview['revision']}"
                + (f" — {preview['summary']}" if preview.get("summary") else "")
            )
            if preview["changes"]:
                console.print("[bold]Adds[/bold]")
                for change in preview["changes"]:
                    console.print(f"  [green]+[/green] {change}")
            if preview["new_files"]:
                console.print("[bold]New files[/bold]")
                for name in preview["new_files"]:
                    console.print(f"  [green]+[/green] {name}")
            if preview["edits"]:
                table = Table(
                    title="Existing files that will be updated", show_lines=False
                )
                table.add_column("File", style="cyan")
                table.add_column("Why")
                for filename, reason in preview["edits"]:
                    table.add_row(filename, reason)
                console.print(table)
            else:
                console.print("[dim]No existing files need updating.[/dim]")
            console.print(
                "[dim]Nothing has been changed — run the request to apply it.[/dim]"
            )
            return True
        # Cheap regex decomposition first (what chat() would auto-split).
        cheap = repl.agent.split_tasks(task)
        if len(cheap) > 1:
            console.print("[bold]Detected sub-tasks:[/bold]")
            for i, t in enumerate(cheap, 1):
                console.print(f"  [cyan]{i}.[/cyan] {t}")
        # Then the LLM planner's ordered steps (for a single multi-step task).
        plan = repl.agent.get_plan(task)
        steps = plan.get("steps", [])
        table = Table(title=f"Planner ({plan.get('task_type', '?')})", show_lines=False)
        table.add_column("#", style="cyan")
        table.add_column("Step")
        table.add_column("Tool", style="yellow")
        for i, s in enumerate(steps, 1):
            table.add_row(
                str(i),
                str(s.get("step_description", "")),
                str(s.get("suggested_tool") or "-"),
            )
        console.print(table)
        return True

    # ── /spec ──────────────────────────────────────────────────────────
    # ── /instructions ──────────────────────────────────────────────────
    # Shows EXACTLY what reaches the prompt — already capped and, if it was
    # cut, carrying its own truncation note. A command that re-read the file
    # would show text the model never saw, which is the opposite of the point.
    if cmd == "instructions":
        if not repl.agent.project_path:
            console.print("[yellow]No project loaded. Use /load <path>[/yellow]")
            return True
        # escape(): a path is data, and a "[" anywhere in it would otherwise be
        # parsed as a Rich style tag (the `[vision]` status-line rule).
        path = escape(str(instructions_path(repl.agent.project_path)))
        text = repl.agent.instructions
        if not text:
            reason = (
                "disabled (project_instructions=false)"
                if not settings.project_instructions
                else "no such file"
            )
            console.print(
                f"[yellow]No project instructions[/yellow] [dim]({reason})[/dim]\n"
                f"[dim]Write conventions for this project — coding style, "
                f"directories to leave alone, where tests go — into:[/dim]\n"
                f"  [cyan]{path}[/cyan]\n"
                "[dim]They then apply to every turn in this project. Reload the "
                "project ([cyan]/index[/cyan]) after editing.[/dim]"
            )
            return True
        console.print(
            Panel(
                escape(text),
                title=f"[bold]{path}[/bold]",
                subtitle=f"[dim]{len(text)} chars, in every prompt[/dim]",
                border_style="cyan",
            )
        )
        return True

    # The visible proof that the agent REMEMBERS the project between turns
    # (docs/fullstack-web-plan.md Phase 2). Small command, disproportionate
    # value: it is the answer to "does it actually know what it built?"
    if cmd == "spec":
        spec = repl.agent.get_spec()
        if spec is None:
            console.print(
                "[yellow]No project spec yet.[/yellow] One is written after a "
                "build — e.g. [cyan]build me a blog[/cyan]."
            )
            return True

        console.print(
            f"[bold]{spec.name or 'project'}[/bold] "
            f"[dim]revision {spec.revision} · {spec.language}/{spec.backend}[/dim]"
        )
        if spec.summary:
            console.print(f"[dim]{spec.summary}[/dim]")

        if spec.entities:
            table = Table(title="Data", show_lines=False)
            table.add_column("Entity", style="cyan")
            table.add_column("Table", style="yellow")
            table.add_column("Fields")
            for e in spec.entities:
                fields = ", ".join(
                    f"{f.name} {f.type}"
                    + (" PK" if f.pk else "")
                    + (f" (rev {f.added_in})" if f.added_in > 1 else "")
                    for f in e.fields
                )
                table.add_row(e.name, e.table, fields)
            console.print(table)

        if spec.endpoints:
            table = Table(title="Routes", show_lines=False)
            table.add_column("Method", style="cyan")
            table.add_column("Path", style="yellow")
            table.add_column("Reads/writes")
            table.add_column("Rev", justify="right")
            for e in spec.endpoints:
                table.add_row(e.method, e.path, e.entity or "-", str(e.added_in))
            console.print(table)

        if spec.pages:
            table = Table(title="Pages", show_lines=False)
            table.add_column("Route", style="cyan")
            table.add_column("Template", style="yellow")
            table.add_column("Nav label")
            table.add_column("Rev", justify="right")
            for p in spec.pages:
                table.add_row(
                    p.route or "-",
                    p.template or "-",
                    p.nav_label or "-",
                    str(p.added_in),
                )
            console.print(table)

        if spec.history:
            console.print("[bold]History[/bold]")
            for h in spec.history:
                added = ", ".join(h.added) if h.added else "-"
                console.print(f"  [cyan]rev {h.revision}[/cyan] {h.request} → {added}")
        return True

    # ── /stack ─────────────────────────────────────────────────────────
    # Phase N1 of docs/node-stack-plan.md. Two jobs: choose the stack a NEW
    # build targets, and show honestly what each one guarantees. The gaps are
    # printed, not hidden — Flask has endpoint validation, deterministic
    # migrations and import repair that Node does not, and a menu listing two
    # stacks as equals is how a demo gets built on the weaker one by accident.
    if cmd == "stack":
        spec = repl.agent.get_spec()
        pinned = spec is not None and (spec.language or spec.backend)
        current = resolve_key(spec, settings.web_stack)

        if args:
            wanted = args[0].strip().lower()
            if wanted not in stack_keys():
                console.print(
                    f"[red]Unknown stack: {wanted}[/red] — "
                    f"choose one of {', '.join(stack_keys())}."
                )
                return True
            settings.web_stack = wanted
            console.print(
                f"[green]New builds will target:[/green] "
                f"{get_adapter(wanted).label}"
            )
            if pinned and current != wanted:
                # The setting is a default for the NEXT project, not a
                # conversion of this one. Saying otherwise would be the
                # single most damaging thing this command could do: an
                # amendment run against the wrong stack writes Python
                # migrations into a JavaScript project.
                console.print(
                    f"[yellow]This project stays on {current}[/yellow] — its "
                    "stack is recorded in .coder/project.json and an amendment "
                    "always follows that, not this setting. The change applies "
                    "to the next project you build."
                )
            return True

        console.print(
            f"[bold]Current:[/bold] {get_adapter(current).label}"
            + (
                "  [dim](from this project's spec)[/dim]"
                if pinned
                else "  [dim](session default)[/dim]"
            )
        )
        for row in describe_stacks():
            marker = "[green]*[/green]" if row["key"] == current else " "
            console.print(f"{marker} [cyan]{row['key']}[/cyan]  {row['label']}")
            for line in row["guarantees"]:
                _bullet("[green]+[/green]", line)
            for line in row["gaps"]:
                _bullet("[yellow]-[/yellow]", line)
        console.print("[dim]Switch with: /stack node[/dim]")
        return True

    # ── /model ─────────────────────────────────────────────────────────
    if cmd == "model":
        if not args:
            console.print(f"[green]Current model:[/green] {settings.llm_model}")
            console.print(
                "[dim]Switch with: /model <name> — e.g. qwen2.5-coder:14b or "
                "qwen2.5-coder:32b (must be pulled: ollama pull <name>).[/dim]"
            )
            return True
        new_model = args[0]
        previous = repl.agent.set_model(new_model)
        console.print(f"[green]Model switched:[/green] {previous} → {new_model}")
        console.print(f"[dim]If it isn't pulled yet: ollama pull {new_model}[/dim]")
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

    # ── /export ────────────────────────────────────────────────────────
    if cmd == "export":
        await _handle_export(args, repl)
        return True

    # ── /bot ───────────────────────────────────────────────────────────
    if cmd == "bot":
        await _handle_bot(args, repl)
        return True

    # ── /mcp ───────────────────────────────────────────────────────────
    if cmd == "mcp":
        if not args:
            console.print(
                "[red]Usage: /mcp list | /mcp add <name> <cmd> [...] | /mcp remove <name>[/red]"
            )
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
                status = (
                    "[green]connected[/green]"
                    if s.get("connected")
                    else "[red]disconnected[/red]"
                )
                console.print(
                    f"  {s['name']} — {status} — {s.get('tool_count', 0)} tools"
                )
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
            console.print(
                "[red]Usage: /skills list | /skills enable <name> | /skills disable <name>[/red]"
            )
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
                status = (
                    "[green]enabled[/green]" if s.enabled else "[dim]disabled[/dim]"
                )
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

    return False  # unknown command


def _bullet(marker: str, text: str, indent: int = 6) -> None:
    """One wrapped bullet with a hanging indent.

    Rich wraps to the console width but does not re-indent the continuation, so
    a two-line guarantee runs back to column 0 and stops looking like a list.
    The marker carries Rich markup and the text does not, so the width is
    computed on the text alone.
    """
    pad = " " * indent
    width = max(40, console.width - indent - 2)
    lines = textwrap.wrap(text, width=width) or [""]
    console.print(f"{pad}{marker} {lines[0]}")
    for line in lines[1:]:
        console.print(f"{pad}  {line}")


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


async def _handle_export(args: list[str], repl: CoderREPL) -> None:
    """`/export` — write this session's full working history to Markdown.

    The record is what `turnlog` stored: every turn, the route it took, the
    tools it ran, the files it wrote and who asked for it. `/history` shows the
    conversation; this is the part of it that is evidence.
    """
    from app.memory import turnlog

    session_id = repl.agent.memory.session_id
    out_path: Path | None = None
    rest: list[str] = []

    i = 0
    while i < len(args):
        if args[i] in ("--session", "-s") and i + 1 < len(args):
            session_id = args[i + 1]
            i += 2
            continue
        rest.append(args[i])
        i += 1

    if rest and rest[0].lower() == "sessions":
        recorded = {s["session_id"]: s for s in await turnlog.list_sessions()}
        # Conversations too, not only turn logs: a project built before the
        # turn log existed has a full conversation and no `turn_events`, and
        # that is exactly the history someone wants to hand in.
        chats = await turnlog.conversation_sessions()
        if not recorded and not chats:
            console.print("[yellow]Nothing recorded yet.[/yellow]")
            return
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Session")
        table.add_column("Turns", justify="right")
        table.add_column("Messages", justify="right")
        table.add_column("Last")
        seen = set()
        for row in chats:
            sid = row["session_id"]
            seen.add(sid)
            turns = recorded.get(sid, {}).get("turns", 0)
            table.add_row(
                sid, str(turns or "—"), str(row["messages"]), row["last"][:19]
            )
        for sid, row in recorded.items():
            if sid not in seen:
                table.add_row(sid, str(row["turns"]), "—", row["last"][:19])
        console.print(table)
        return

    if rest:
        out_path = Path(rest[0]).expanduser()

    turns = await turnlog.load_turns(session_id)
    note = ""
    if not turns:
        # Fall back to the plain conversation. A project built before the turn
        # log existed still has its questions and answers stored, and refusing
        # to export them because the richer table is empty would lose the only
        # record there is. What is missing is stated in the file, not implied.
        turns = await turnlog.load_conversation(session_id)
        note = (
            "rebuilt from the stored conversation — this session predates the "
            "turn log, so the route each turn took, the tools it ran and the "
            "files it wrote were never recorded and are absent here."
        )
    if not turns:
        console.print(
            f"[yellow]Nothing recorded for session '{session_id}'.[/yellow] "
            "Try [cyan]/export sessions[/cyan]."
        )
        return

    if out_path is None:
        out_path = Path(f"coder-transcript-{session_id}.md")
    if out_path.is_dir():
        out_path = out_path / f"coder-transcript-{session_id}.md"

    text = turnlog.render_transcript(turns, session_id=session_id, note=note)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Could not write {escape(str(out_path))}: {exc}[/red]")
        return

    files = sorted({f for t in turns for f in t.get("files_written") or []})
    console.print(
        f"[green]Wrote {len(turns)} turn(s) to[/green] {escape(str(out_path.resolve()))} "
        f"[dim]({len(files)} file(s) written across the session)[/dim]"
    )


async def _handle_bot(args: list[str], repl: CoderREPL) -> None:
    """`/bot` — run the Telegram front-end alongside this terminal.

    Embedded on purpose: the bot shares this process's `SessionRegistry`, hence
    ONE `AgentCore` and one lock for the loaded project. That is what makes
    "the app and the bot at the same time" a single conversation with a single
    memory rather than two agents racing over one folder.
    """
    from app.bot.auth import UserStore
    from app.bot.telegram_bot import CoderBot

    action = args[0].lower() if args else "status"

    if action == "pair":
        # Minted from the machine itself, which is the point: the bot never
        # grants access to itself, and this is how the FIRST remote user gets
        # in without an owner already existing on the Telegram side.
        role = args[1] if len(args) > 1 else "developer"
        code, ttl = await UserStore().mint_code(role, created_by="cli")
        console.print(
            Panel(
                f"[bold cyan]/login {code}[/bold cyan]\n\n"
                f"[dim]Send this to the person you are inviting. It grants "
                f"[/dim][cyan]{role}[/cyan][dim], works once, and expires in "
                f"{int(ttl // 60)} minutes.[/dim]",
                title="[bold]Pairing code[/bold]",
                border_style="cyan",
            )
        )
        return

    if action in ("stop", "kill"):
        if repl.bot is None:
            console.print("[yellow]The bot is not running.[/yellow]")
            return
        await repl.bot.stop()
        repl.bot = None
        console.print("[green]Telegram bot stopped.[/green]")
        return

    if action == "status":
        running = repl.bot is not None and repl.bot.running
        console.print(
            f"[cyan]Telegram bot:[/cyan] {'running' if running else 'stopped'}"
        )
        if not running:
            reason = CoderBot(
                repl.registry, repl.agent.project_path or Path.cwd()
            ).preflight()
            if reason:
                console.print(f"[yellow]{escape(reason)}[/yellow]")
        return

    if action != "start":
        console.print("[red]Usage: /bot start | stop | status | pair [role][/red]")
        return

    if repl.bot is not None and repl.bot.running:
        console.print("[yellow]The bot is already running.[/yellow]")
        return

    def on_activity(user_id: int, username: str, text: str) -> None:
        # Printed into THIS terminal so one screen recording shows both
        # front-ends working on the same project.
        who = f"@{username}" if username else str(user_id)
        console.print(
            f"[dim magenta][telegram][/dim magenta] {escape(who)}: "
            f"{escape(text[:120])}"
        )

    bot = CoderBot(
        registry=repl.registry,
        default_project=repl.agent.project_path or Path.cwd(),
        on_activity=on_activity,
    )
    reason = await bot.start()
    if reason:
        console.print(f"[red]{escape(reason)}[/red]")
        return
    repl.bot = bot
    console.print(
        "[green]Telegram bot started.[/green] "
        "[dim]Messages appear here as they arrive; /bot stop ends it.[/dim]"
    )
