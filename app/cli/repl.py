"""Main REPL loop — Rich + prompt_toolkit."""

from __future__ import annotations

import asyncio
import re
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text

from app.agent.core import AgentCore
from app.cli.commands import handle_command
from config.settings import settings

if TYPE_CHECKING:
    from app.mcp.manager import MCPManager
    from app.skills.loader import SkillLoader

console = Console()

_HISTORY_FILE = Path(".coder_history")
_ERROR_LOG = Path(".coder_errors.log")


def _log_exception(context: str, exc: BaseException) -> Path | None:
    """Append a full traceback to the error log; return where it landed.

    The REPL only shows `str(exc)` so a turn's failure doesn't wreck the
    display — but bare messages like "[Errno 2] No such file or directory"
    are undiagnosable without the frames. Best-effort: logging must never
    replace the original error.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with _ERROR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {stamp} — {context} =====\n{body}")
        return _ERROR_LOG
    except Exception:
        return None


_BANNER = """[bold cyan]
  ██████╗ ██████╗ ██████╗ ███████╗██████╗
 ██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗
 ██║     ██║   ██║██║  ██║█████╗  ██████╔╝
 ██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗
 ╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║
  ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝
[/bold cyan][dim]  Offline AI Coding Assistant  •  powered by {model}[/dim]
""".format(model=settings.llm_model)

_PT_STYLE = PTStyle.from_dict(
    {
        "prompt": "ansicyan bold",
    }
)

_CODE_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def _render_response(text: str) -> None:
    """Print response with syntax-highlighted code blocks."""
    last = 0
    for m in _CODE_FENCE_RE.finditer(text):
        # Print prose before the fence
        before = text[last : m.start()].strip()
        if before:
            console.print(before)
        lang = m.group(1) or "text"
        code = m.group(2)
        console.print(Syntax(code, lang, theme="monokai", line_numbers=True))
        last = m.end()
    remainder = text[last:].strip()
    if remainder:
        console.print(remainder)


_MAX_DIFF_LINES = 60


def _print_tool_step(tool_name: str, result: dict) -> None:
    status = "[green]✓[/green]" if result.get("success") else "[red]✗[/red]"
    console.print(
        f"  [dim cyan][Tool][/dim cyan] {tool_name} {status}", highlight=False
    )
    diff = result.get("diff")
    if diff:
        lines = diff.splitlines()
        console.print(
            Syntax("\n".join(lines[:_MAX_DIFF_LINES]), "diff", theme="monokai")
        )
        if len(lines) > _MAX_DIFF_LINES:
            console.print(
                f"  [dim]... {len(lines) - _MAX_DIFF_LINES} more diff lines[/dim]"
            )


class CoderREPL:
    def __init__(
        self,
        agent: AgentCore,
        mcp_manager: MCPManager | None = None,
        skill_loader: SkillLoader | None = None,
    ) -> None:
        self.agent = agent
        self.mcp_manager = mcp_manager
        self.skill_loader = skill_loader
        self.agent.skill_loader = skill_loader  # keep agent in sync
        self.running = True
        self._session: PromptSession | None = None  # created lazily in run()
        # Approval gate (Step 6 / S3): tool names the user approved for the
        # whole session, and the Live region to pause while prompting.
        self._session_allows: set[str] = set()
        self._active_live: Live | None = None

    # ------------------------------------------------------------------
    # Project loading
    # ------------------------------------------------------------------

    async def load_project(self, path: str) -> None:
        p = Path(path).resolve()
        if not p.exists():
            console.print(f"[red]Path not found: {path}[/red]")
            return
        with console.status(f"[cyan]Indexing {p.name}...[/cyan]"):
            stats = await self.agent.load_project(str(p))
        console.print(
            f"[green]Project loaded:[/green] {p.name}  "
            f"[dim]({stats.get('files', 0)} files, {stats.get('chunks', 0)} chunks)[/dim]"
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Create PromptSession here — requires a real TTY
        self._session = PromptSession(
            history=FileHistory(str(_HISTORY_FILE)),
            auto_suggest=AutoSuggestFromHistory(),
            style=_PT_STYLE,
        )

        # Approval gate (Step 6 / S3): only prompt when there's a real TTY and
        # the user hasn't opted into --yolo (auto_approve). Non-interactive runs
        # leave the hook unset so the executor's default policy applies.
        if sys.stdin.isatty() and not settings.auto_approve:
            self.agent.executor.set_approval_hook(self._approve_tool)

        # Auto-load MCP servers from config
        if self.mcp_manager is not None:
            try:
                result = await self.mcp_manager.load_from_config(self.agent.registry)
                if result.get("connected"):
                    console.print(
                        f"[dim]MCP servers connected: {result['connected']}[/dim]"
                    )
                if result.get("failed"):
                    console.print(
                        f"[yellow]MCP servers failed: {result['failed']}[/yellow]"
                    )
            except Exception as e:
                console.print(f"[yellow]MCP load warning: {e}[/yellow]")

        console.print(_BANNER)
        console.print(
            Panel(
                "[dim]Type a message to start.  Use [cyan]/help[/cyan] for commands.[/dim]",
                border_style="dim",
            )
        )

        while self.running:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._session.prompt("Coder ❯ "),  # type: ignore[union-attr]
                )
            except (EOFError, KeyboardInterrupt):
                console.print("\n[bold yellow]Goodbye![/bold yellow]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # Slash command
            if user_input.startswith("/"):
                try:
                    handled = await handle_command(user_input, self)
                    if not handled:
                        console.print(
                            f"[red]Unknown command: {user_input.split()[0]}[/red]  Type /help for the list."
                        )
                except Exception as e:
                    console.print(f"[red]Command error:[/red] {e}")
                continue

            # Agent turn
            await self._agent_turn(user_input)

    async def _agent_turn(self, user_input: str) -> None:
        """Send message to agent and display response with tool steps.

        Direct answers stream token-by-token into a transient Live region;
        on completion the region is erased and the final answer re-rendered
        with syntax highlighting (so streamed text is never duplicated).
        """
        console.print()  # blank line

        # M6: when the request decomposes into several tasks, show the derived
        # to-do list before executing so partial completion isn't a mystery.
        split = getattr(self.agent, "split_tasks", None)
        tasks = split(user_input) if callable(split) else []
        if len(tasks) > 1:
            plan_lines = "\n".join(
                f"  [cyan]{i}.[/cyan] {t}" for i, t in enumerate(tasks, 1)
            )
            console.print(
                Panel(plan_lines, title="[bold]Plan[/bold]", border_style="cyan")
            )

        try:
            streamed: list[str] = []
            # Progress lines from non-streaming work (the vision model
            # describing an @-referenced screenshot, which swaps the model
            # Ollama has loaded and takes seconds). Shown live in the spinner,
            # then reprinted below it — the Live region is transient, so
            # anything left only there vanishes when the turn ends.
            notes: list[str] = []
            with Live(
                Spinner("dots", text=Text("Thinking...", style="cyan")),
                console=console,
                refresh_per_second=12,
                transient=True,
            ) as live:
                self._active_live = live  # so the approval prompt can pause it

                def on_token(token: str) -> None:
                    streamed.append(token)
                    live.update(Text("".join(streamed)))

                def on_status(message: str) -> None:
                    notes.append(message)
                    # Text(), not markup: a status line contains "[vision]",
                    # which console markup would try to read as a style tag.
                    live.update(Spinner("dots", text=Text(message, style="cyan")))

                self.agent.status_hook = on_status
                try:
                    answer, trace = await self.agent.chat(user_input, on_token=on_token)
                finally:
                    self._active_live = None
                    self.agent.status_hook = None

            for note in notes:
                console.print(Text(f"  {note}", style="dim cyan"))

            # Show tool calls that were made (with diff previews when present)
            for step in trace:
                _print_tool_step(step["tool"], step["result"])

            console.print()
            _render_response(answer)
            console.print()

        except KeyboardInterrupt:
            console.print("\n[yellow](interrupted)[/yellow]")
        except Exception as e:
            log = _log_exception(f"agent turn: {user_input[:120]}", e)
            console.print(
                f"\n[red]Agent error:[/red] "
                f"[bold]{type(e).__name__}[/bold]: {escape(str(e))}"
            )
            if log is not None:
                console.print(f"[dim]Full traceback appended to {log}[/dim]")
            console.print(
                "[dim]Type /clear to reset if the conversation is stuck.[/dim]"
            )

    async def _approve_tool(
        self, tool_name: str, arguments: dict, permissions: list[str]
    ) -> bool:
        """Interactive approval hook for gated tools (Step 6 / S3).

        Returns True to run the call. `[s]ession` remembers the tool for the
        rest of the session so repeated calls aren't re-prompted.
        """
        if tool_name in self._session_allows:
            return True

        live = self._active_live
        if live is not None:
            live.stop()  # release the terminal so the prompt renders cleanly
        try:
            arg_preview = ", ".join(
                f"{k}={str(v)[:60]!r}" for k, v in arguments.items()
            )
            console.print(
                f"\n[bold yellow]Approve tool call?[/bold yellow] "
                f"[cyan]{tool_name}[/cyan] [dim]({', '.join(permissions)})[/dim]"
            )
            if arg_preview:
                console.print(f"  [dim]{arg_preview}[/dim]")
            loop = asyncio.get_event_loop()
            choice = await loop.run_in_executor(
                None,
                lambda: console.input("  [a]llow / allow [s]ession / [d]eny: ")
                .strip()
                .lower(),
            )
        finally:
            if live is not None:
                live.start()

        if choice in ("s", "session"):
            self._session_allows.add(tool_name)
            return True
        return choice in ("a", "allow", "y", "yes")
