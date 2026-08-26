"""The bot's logic, with no Telegram in it (Phase T2).

Everything here runs against the `Transport` protocol, so the whole file is
exercised offline by `tests/test_bot.py` with a fake transport and a fake agent:
no token, no network, and the library need not even be installed. The Telegram
wiring is `telegram_bot.py`, and it contains no decisions.

The service owns four things the REPL already had in some form, and one it did
not:

- **routing** a message to a turn or a command (the REPL's `handle_command`);
- **streaming** into a single message that is edited as tokens arrive (the
  REPL's transient `Live` region);
- **progress**, from the same `AgentCore.status_hook` the REPL installs;
- **approvals**, through the same `executor.approval_hook` — an inline keyboard
  instead of a terminal prompt, and **a timeout is a Deny**;
- and the new one: every turn goes through `SessionRegistry.turn`, so a message
  arriving while the CLI is mid-turn WAITS and is told who holds the project,
  rather than interleaving writes into it.

T3 adds the caller's ROLE to that turn (`denied_permissions_for(role)`), which
is passed to the registry and enforced by the executor — not here. Nothing in
this file refuses a write; it only decides which deny list the turn carries, and
`app/bot/auth.py` explains why that distinction is the whole point.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agent.sessions import SessionRegistry, TurnBusy
from app.bot import audit, render
from app.bot.auth import (
    OWNER,
    UserStore,
    denied_permissions_for,
    may_run_command,
)
from app.bot.transport import TIMED_OUT, Transport
from app.memory import turnlog
from config.settings import settings

logger = logging.getLogger(__name__)

_APPROVE_OPTIONS = [
    ("Allow", "allow"),
    ("Allow for session", "session"),
    ("Deny", "deny"),
]

HELP = """<b>Coder — Telegram</b>

Send a message to work on the current project.

<b>Project</b>
/project — what is loaded
/load &lt;path&gt; — load another project
/spec — what Coder remembers about it
/run [restart|stop|status] — start the generated app

<b>Session</b>
/history — recent turns
/export — this project's full working history
/whoami — your id and what you may do
/cancel — forget a pending approval
/help — this

<b>Access (owner only)</b>
/pair [viewer|developer|owner] — mint a one-time invite code
/login &lt;code&gt; — redeem one (anyone, once)
/users — who may use this bot
/revoke &lt;id&gt; — remove a paired user
"""


@dataclass
class ChatState:
    """What one Telegram chat is currently pointed at."""

    project: Path
    approvals_for_session: set[str] = field(default_factory=set)
    turns: int = 0


class RateLimiter:
    """A token bucket per user.

    A build turn costs minutes of a single GPU, so an unbounded queue is a
    denial of service by accident as easily as on purpose.
    """

    def __init__(self, capacity: int, per_seconds: float) -> None:
        self.capacity = max(1, capacity)
        self.per_seconds = max(1.0, per_seconds)
        self._buckets: dict[int, tuple[float, float]] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(user_id, (float(self.capacity), now))
        tokens = min(
            float(self.capacity),
            tokens + (now - last) * (self.capacity / self.per_seconds),
        )
        if tokens < 1.0:
            self._buckets[user_id] = (tokens, now)
            return False
        self._buckets[user_id] = (tokens - 1.0, now)
        return True


class BotService:
    def __init__(
        self,
        registry: SessionRegistry,
        transport: Transport,
        default_project: Path | str,
        allowed_users: list[int] | None = None,
        users: UserStore | None = None,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.default_project = Path(default_project).resolve()
        # T3: roles live in `bot_users`; `telegram_allowed_users` is the
        # bootstrap set of owners and outranks the table. Both are DENY BY
        # DEFAULT — with neither configured the bot refuses everyone, including
        # the person who started it. `allowed_users` here is a test seam and is
        # merged with the setting rather than replacing it.
        self.users = users or UserStore()
        self.extra_owners = set(allowed_users or [])
        self.chats: dict[int, ChatState] = {}
        self._limiter = RateLimiter(
            settings.telegram_rate_burst, settings.telegram_rate_seconds
        )
        self._turn_slots = asyncio.Semaphore(
            max(1, settings.telegram_max_concurrent_turns)
        )

    # ── authorization ─────────────────────────────────────────────────────

    async def role_for(self, user_id: int) -> str | None:
        """This user's role, or None if they may not use the bot at all."""
        if user_id in self.extra_owners:
            return OWNER
        return await self.users.role_for(user_id)

    async def is_authorized(self, user_id: int) -> bool:
        return await self.role_for(user_id) is not None

    def state(self, chat_id: int) -> ChatState:
        existing = self.chats.get(chat_id)
        if existing is None:
            existing = ChatState(project=self.default_project)
            self.chats[chat_id] = existing
        return existing

    # ── entry point ───────────────────────────────────────────────────────

    async def handle(self, chat_id: int, user_id: int, text: str) -> None:
        """One inbound message. Never raises — a crash would kill the poller."""
        try:
            text = (text or "").strip()
            if not text:
                return
            state = self.state(chat_id)
            role = await self.role_for(user_id)

            if role is None:
                # `/login` is the ONE thing an unknown user may do, and it is
                # not a way in by itself — it redeems a code that somebody at
                # the machine minted. Everything else is refused and recorded.
                if text.lower().startswith("/login"):
                    await self._login(chat_id, user_id, text)
                    return
                audit.record(
                    audit.REFUSED,
                    user_id=user_id,
                    project=state.project,
                    chat_id=chat_id,
                    message=text[:200],
                )
                await self._say(
                    chat_id,
                    "Not authorized. Ask the person running Coder for a "
                    "pairing code, then send <code>/login CODE</code>. Your id "
                    f"is <code>{user_id}</code>.",
                    already_html=True,
                )
                return

            if not self._limiter.allow(user_id):
                await self._say(chat_id, "Slow down a moment — too many requests.")
                return
            if text.startswith("/"):
                await self._command(chat_id, user_id, role, text)
                return
            await self._turn(chat_id, user_id, role, text)
        except Exception:
            logger.warning("bot message handling failed", exc_info=True)
            await self._say(chat_id, "Something went wrong handling that message.")

    # ── commands ──────────────────────────────────────────────────────────

    async def _command(self, chat_id: int, user_id: int, role: str, text: str) -> None:
        parts = text[1:].split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:]
        state = self.state(chat_id)

        if not may_run_command(role, cmd):
            audit.record(
                audit.COMMAND,
                user_id=user_id,
                project=state.project,
                command=cmd,
                role=role,
                allowed=False,
            )
            await self._say(chat_id, f"/{cmd} is owner-only. You are a {role}.")
            return

        if cmd in ("start", "help"):
            await self._say(chat_id, HELP, already_html=True)
            return

        if cmd == "login":
            await self._say(chat_id, f"Already paired as {role}.")
            return

        if cmd == "whoami":
            denied = denied_permissions_for(role) or ["nothing"]
            await self._say(
                chat_id,
                f"id <code>{user_id}</code>\n"
                f"role <b>{role}</b>\n"
                f"refused outright: {', '.join(denied)}\n"
                f"project <code>{render.escape_html(str(state.project))}</code>",
                already_html=True,
            )
            return

        if cmd == "pair":
            await self._pair(chat_id, user_id, args)
            return

        if cmd == "users":
            await self._users(chat_id)
            return

        if cmd == "revoke":
            await self._revoke(chat_id, user_id, args)
            return

        if cmd == "project":
            await self._say(chat_id, f"Project: `{state.project}`")
            return

        if cmd == "load":
            if not args:
                await self._say(chat_id, "Usage: /load <path>")
                return
            root = Path(" ".join(args)).expanduser()
            if not root.is_dir():
                await self._say(chat_id, f"No such folder: `{root}`")
                return
            state.project = root.resolve()
            await self._say(chat_id, f"Loading `{state.project}` …")
            await self.registry.get(state.project)
            await self._say(chat_id, f"Loaded `{state.project}`")
            return

        if cmd == "cancel":
            state.approvals_for_session.clear()
            await self._say(chat_id, "Cleared this chat's session approvals.")
            return

        if cmd == "export":
            await self._export(chat_id, state)
            return

        if cmd == "history":
            await self._history(chat_id, state)
            return

        # Everything else is delegated to the agent as a turn, so /spec, /run
        # and friends keep ONE implementation rather than a second, drifting
        # copy of the REPL's. A command the agent does not know comes back as
        # an ordinary answer.
        await self._turn(chat_id, user_id, role, text)

    # ── access control ────────────────────────────────────────────────────

    async def _login(self, chat_id: int, user_id: int, text: str) -> None:
        parts = text.split()
        code = parts[1] if len(parts) > 1 else ""
        grant = await self.users.redeem(code, user_id)
        audit.record(
            audit.PAIRED if grant.role else audit.REFUSED,
            user_id=user_id,
            project=self.state(chat_id).project,
            role=grant.role or "",
            via="login",
        )
        await self._say(chat_id, grant.reason)

    async def _pair(self, chat_id: int, user_id: int, args: list[str]) -> None:
        role = args[0] if args else "developer"
        code, ttl = await self.users.mint_code(role, created_by=f"telegram:{user_id}")
        audit.record(
            audit.GRANTED,
            user_id=user_id,
            project=self.state(chat_id).project,
            role=role,
            minted=True,
        )
        await self._say(
            chat_id,
            f"Send this to the person you are inviting — it works once and "
            f"expires in {int(ttl // 60)} minutes:\n"
            f"<code>/login {code}</code>",
            already_html=True,
        )

    async def _users(self, chat_id: int) -> None:
        rows = await self.users.list_users()
        if not rows:
            await self._say(chat_id, "Nobody is authorized — not even you. Odd.")
            return
        lines = [
            f"• `{r['user_id']}` — {r['role']}"
            + (f" ({r['source']})" if r["source"] == "env" else "")
            for r in rows
        ]
        await self._say(chat_id, "\n".join(lines))

    async def _revoke(self, chat_id: int, user_id: int, args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            await self._say(chat_id, "Usage: /revoke <numeric user id>")
            return
        target = int(args[0])
        removed = await self.users.revoke(target)
        audit.record(
            audit.REVOKED,
            user_id=user_id,
            project=self.state(chat_id).project,
            target=target,
            removed=removed,
        )
        await self._say(
            chat_id,
            (
                f"Revoked `{target}`."
                if removed
                else f"`{target}` is not a paired user (an id in TELEGRAM_ALLOWED_USERS "
                "is changed in .env, not here)."
            ),
        )

    async def _export(self, chat_id: int, state: ChatState) -> None:
        session = await self.registry.get(state.project)
        turns = await turnlog.load_turns(session.session_id)
        if not turns:
            await self._say(chat_id, "No turns recorded for this project yet.")
            return
        out = Path(state.project) / f"coder-transcript-{session.session_id}.md"
        try:
            out.write_text(
                turnlog.render_transcript(turns, session_id=session.session_id),
                encoding="utf-8",
            )
        except OSError as exc:
            await self._say(chat_id, f"Could not write the transcript: {exc}")
            return
        await self._say(chat_id, f"Wrote {len(turns)} turn(s) to `{out}`")

    async def _history(self, chat_id: int, state: ChatState) -> None:
        session = await self.registry.get(state.project)
        turns = await turnlog.load_turns(session.session_id, limit=5)
        if not turns:
            await self._say(chat_id, "Nothing recorded yet.")
            return
        lines = [f"• `{t['source']}` — {t['user_message'][:80]}" for t in turns]
        await self._say(chat_id, "\n".join(lines))

    # ── a turn ────────────────────────────────────────────────────────────

    async def _turn(self, chat_id: int, user_id: int, role: str, text: str) -> None:
        state = self.state(chat_id)
        source = f"telegram:{user_id}"
        live = LiveMessage(self.transport, chat_id)
        await live.start()

        async with self._turn_slots:
            try:
                async with self.registry.turn(
                    state.project,
                    front_end=f"telegram:{user_id}",
                    source=source,
                    message=text,
                    on_wait=live.status,
                    # The role, expressed where it is ENFORCED: the executor
                    # refuses these before the tool runs, so a `viewer` cannot
                    # write however the conversation goes.
                    denied_permissions=denied_permissions_for(role),
                ) as agent:
                    state.turns += 1
                    await self._run_turn(agent, chat_id, state, text, live)
                    audit.record(
                        audit.TURN,
                        user_id=user_id,
                        project=state.project,
                        role=role,
                        chat_id=chat_id,
                        message=text[:200],
                    )
            except TurnBusy as busy:
                await live.finish(f"Busy — {busy}. Try again in a moment.")
            except Exception:
                logger.warning("turn failed", exc_info=True)
                await live.finish("That turn failed. Check the Coder log.")

    async def _run_turn(
        self,
        agent: Any,
        chat_id: int,
        state: ChatState,
        text: str,
        live: LiveMessage,
    ) -> None:
        previous_status = getattr(agent, "status_hook", None)
        agent.status_hook = live.status
        executor = getattr(agent, "executor", None)
        previous_hook = getattr(executor, "approval_hook", None)
        if executor is not None:
            executor.set_approval_hook(self._approval_hook(chat_id, state))
        pump = asyncio.create_task(live.pump())
        try:
            answer, trace = await agent.chat(text, on_token=live.feed)
        finally:
            pump.cancel()
            agent.status_hook = previous_status
            if executor is not None:
                # RESTORE, never clear. A hook bound to this chat must not
                # outlive the turn — the CLI shares this executor and its next
                # write would ask Telegram for permission. But clearing it is
                # just as wrong: no hook means the executor's default, which is
                # allow, so the terminal would silently lose its own prompt.
                executor.set_approval_hook(previous_hook)

        for line in [
            render.tool_line(t.get("tool", "?"), t.get("result")) for t in trace
        ][: settings.telegram_max_tool_lines]:
            logger.debug("%s", line)
        await live.finish(answer)

    def _approval_hook(
        self, chat_id: int, state: ChatState
    ) -> Callable[[str, dict, list[str]], Any]:
        async def hook(tool_name: str, arguments: dict, permissions: list[str]) -> bool:
            if tool_name in state.approvals_for_session:
                return True
            answer = await self.transport.ask(
                chat_id,
                render.approval_question(tool_name, arguments, permissions),
                _APPROVE_OPTIONS,
                timeout=settings.telegram_approval_timeout,
            )
            if answer == "session":
                state.approvals_for_session.add(tool_name)
                return True
            # Anything that is not an explicit allow is a DENY: a timeout, a
            # dismissed keyboard, an unrecognised payload. The alternative is a
            # write proceeding because nobody was looking at their phone.
            return answer == "allow"

        return hook

    # ── output ────────────────────────────────────────────────────────────

    async def _say(self, chat_id: int, text: str, already_html: bool = False) -> None:
        if already_html:
            await self.transport.send(chat_id, text)
            return
        for chunk in render.render_chunks(text):
            await self.transport.send(chat_id, chunk)


class LiveMessage:
    """One message, edited as the turn progresses, then replaced by the answer.

    The REPL shows tokens in a transient `Live` region and prints the finished
    render underneath. Telegram has no transient region, so the same message is
    edited — which is also why the edit rate is throttled: the API rejects a
    burst of edits to one message, and a stream produces hundreds of tokens a
    second.
    """

    def __init__(
        self,
        transport: Transport,
        chat_id: int,
        interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.chat_id = chat_id
        self.interval = (
            settings.telegram_edit_interval if interval is None else interval
        )
        self._clock = clock
        self._message_id: int | None = None
        self._tokens: list[str] = []
        self._status: list[str] = []
        self._last_edit = 0.0
        self._last_sent = ""

    async def start(self, text: str = "Working …") -> None:
        self._message_id = await self.transport.send(self.chat_id, text)
        self._last_edit = self._clock()

    # `on_token` is called from inside the streaming coroutine and cannot await,
    # so it only buffers; `pump` does the editing.
    def feed(self, token: str) -> None:
        self._tokens.append(token)

    def status(self, line: str) -> None:
        self._status.append(str(line))
        del self._status[:-3]

    def preview(self) -> str:
        body = "".join(self._tokens)
        # The tail, not the head: the interesting end of a stream is the end,
        # and a whole build answer will not fit in one message anyway.
        if len(body) > 3000:
            body = "…" + body[-3000:]
        parts = [render.escape_html(s) for s in self._status]
        if body:
            parts.append(render.escape_html(body))
        return "\n".join(parts) or "Working …"

    async def pump(self) -> None:
        """Edit the message on a timer until cancelled."""
        try:
            while True:
                await asyncio.sleep(self.interval)
                await self.tick()
        except asyncio.CancelledError:
            return

    async def tick(self) -> None:
        if self._message_id is None:
            return
        text = self.preview()
        if text == self._last_sent:
            return  # Telegram rejects an unchanged edit; do not spend the call.
        self._last_sent = text
        self._last_edit = self._clock()
        await self.transport.edit(self.chat_id, self._message_id, text)
        await self.transport.typing(self.chat_id)

    async def finish(self, answer: str) -> None:
        """Replace the live message with the finished answer, in chunks."""
        chunks = render.render_chunks(answer or "(no answer)")
        first, rest = chunks[0], chunks[1:]
        if self._message_id is None:
            await self.transport.send(self.chat_id, first)
        else:
            await self.transport.edit(self.chat_id, self._message_id, first)
        for chunk in rest:
            await self.transport.send(self.chat_id, chunk)
