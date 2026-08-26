"""What a turn DID, not only what it said (Phase T0, docs/telegram-bot-plan.md).

`conversation_turns` stores role + content, which is the transcript a chat UI
needs and not the one the work needs: the routing decision and the tool trace —
where the reasoning actually is — are returned by `AgentCore.chat()` and then
dropped on the floor. A history rebuilt from what was stored showed the request
and the reply for a turn that scaffolded a project, generated eleven files,
repaired four of them and ran a smoke test, and none of that was in it.

This module stores the rest of it, and renders it.

Two rules the rest of the code depends on:

- **Recording is best-effort and never raises.** It runs at the `chat()` seam
  beside `memory.add_ai`, so a log that will not write must never cost a turn
  whose files already landed — the rule `ProjectSpec.save` follows.
- **`render_transcript` is pure.** It takes plain dicts and returns a string, so
  the format is unit-testable with no database and no event loop. The DB half is
  three small functions around it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database.sqlite_db import AsyncSessionLocal, Base, init_db

logger = logging.getLogger(__name__)


# Which of `chat()`'s routes the turn took. Recorded rather than re-derived:
# the routing decision is made from state that is gone by the time the answer
# is returned (the spec, the blueprint, the compound splitter's verdict).
FLOW_AMEND = "amend"
FLOW_BLUEPRINT = "blueprint"
FLOW_SUBTASKS = "subtasks"
FLOW_MULTIFILE = "multifile"
FLOW_SINGLE = "single"

SOURCE_CLI = "cli"

#: Tools whose successful call means a file changed on disk.
_WRITE_TOOLS = ("write_file", "create_file", "edit_file")

#: A tool result can carry a whole file. The trace is stored for the record,
#: not for replay, so each result is capped — an unbounded blob would make the
#: table many times larger than the project it describes.
MAX_RESULT_CHARS = 2000


class TurnEvent(Base):
    """One `chat()` turn: what was asked, how it routed, what it touched."""

    __tablename__ = "turn_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[str] = mapped_column(String(32))
    # "cli" | "telegram:<user_id>". Without this a session driven by two
    # front-ends reads as one actor, and "they worked at the same time" is not
    # checkable from the record afterwards.
    source: Mapped[str] = mapped_column(String(64), default=SOURCE_CLI)
    project: Mapped[str] = mapped_column(String(512), default="")
    user_message: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    task_type: Mapped[str] = mapped_column(String(32), default="")
    flow: Mapped[str] = mapped_column(String(32), default="")
    tool_trace: Mapped[str] = mapped_column(Text, default="[]")
    files_written: Mapped[str] = mapped_column(Text, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


def compact_trace(trace: Iterable[dict] | None) -> list[dict]:
    """Reduce a tool trace to what a transcript can show.

    Keeps the tool, its target, whether it worked and the error if it didn't;
    caps the result body. Never raises — a trace entry of an unexpected shape
    contributes what it has rather than losing the whole turn's record.
    """
    out: list[dict] = []
    for entry in trace or []:
        if not isinstance(entry, dict):
            continue
        args = entry.get("arguments")
        args = args if isinstance(args, dict) else {}
        result = entry.get("result")
        result = result if isinstance(result, dict) else {}
        body = str(result.get("result") or "")
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + f"\n… [{len(body)} chars total]"
        out.append(
            {
                "tool": str(entry.get("tool") or "?"),
                "target": _target_of(args),
                "success": bool(result.get("success")),
                "error": str(result.get("error") or ""),
                "result": body,
            }
        )
    return out


def _target_of(args: dict) -> str:
    """The one argument worth naming in a one-line tool row."""
    for key in ("path", "file_path", "command", "query", "name", "pattern"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def written_files(trace: Iterable[dict] | None) -> list[str]:
    """Paths a trace successfully wrote, in order, de-duplicated.

    Deliberately NOT relative to the project: this is the historical record, and
    a turn can write outside the loaded project (or with none loaded at all).
    """
    out: list[str] = []
    for entry in trace or []:
        if not isinstance(entry, dict) or entry.get("tool") not in _WRITE_TOOLS:
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or not result.get("success"):
            continue
        args = entry.get("arguments")
        path = (args or {}).get("path") if isinstance(args, dict) else None
        if isinstance(path, str) and path and path not in out:
            out.append(path)
    return out


async def record_turn(
    *,
    session_id: str,
    user_message: str,
    answer: str,
    trace: Iterable[dict] | None = None,
    source: str = SOURCE_CLI,
    project: str = "",
    task_type: str = "",
    flow: str = "",
    duration_ms: int = 0,
    session_factory: Any = None,
) -> bool:
    """Store one turn. Best-effort: returns False instead of raising."""
    factory = session_factory or AsyncSessionLocal
    compact = compact_trace(trace)
    try:
        if session_factory is None:
            await init_db()
        async with factory() as session:
            session.add(
                TurnEvent(
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    source=source or SOURCE_CLI,
                    project=project or "",
                    user_message=user_message,
                    answer=answer,
                    task_type=task_type or "",
                    flow=flow or "",
                    tool_trace=json.dumps(compact, ensure_ascii=False),
                    files_written=json.dumps(written_files(trace), ensure_ascii=False),
                    duration_ms=int(duration_ms),
                )
            )
            await session.commit()
        return True
    except Exception:
        logger.warning("turn event not recorded", exc_info=True)
        return False


async def load_turns(
    session_id: str,
    limit: int | None = None,
    session_factory: Any = None,
) -> list[dict]:
    """Every recorded turn for a session, oldest first, as plain dicts."""
    factory = session_factory or AsyncSessionLocal
    if session_factory is None:
        await init_db()
    stmt = select(TurnEvent).where(TurnEvent.session_id == session_id)
    if limit:
        stmt = stmt.order_by(TurnEvent.id.desc()).limit(limit)
    else:
        stmt = stmt.order_by(TurnEvent.id.asc())
    async with factory() as session:
        rows = list((await session.execute(stmt)).scalars().all())
    if limit:
        rows.reverse()
    return [_row_to_dict(r) for r in rows]


async def load_conversation(session_id: str, session_factory: Any = None) -> list[dict]:
    """Rebuild turns from `conversation_turns`, for history older than T0.

    A project Coder built before the turn log existed has a full conversation
    stored and no `turn_events` at all — and that is exactly the project whose
    history someone wants to hand in. Refusing to export it because the richer
    table is empty would lose the only record there is.

    What it cannot know is stated rather than guessed: no route, no tools, no
    files, no duration. `flow` is left empty and the caller says why.
    """
    from app.memory.conversation import ConversationTurn

    factory = session_factory or AsyncSessionLocal
    if session_factory is None:
        await init_db()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ConversationTurn)
                    .where(ConversationTurn.session_id == session_id)
                    .order_by(ConversationTurn.id.asc())
                )
            )
            .scalars()
            .all()
        )

    turns: list[dict] = []
    pending: dict | None = None
    for row in rows:
        if row.role == "human":
            if pending is not None:
                turns.append(pending)
            pending = {
                "session_id": session_id,
                "timestamp": row.timestamp,
                "source": SOURCE_CLI,
                "project": "",
                "user_message": row.content,
                "answer": "",
                "task_type": "",
                "flow": "",
                "tools": [],
                "files_written": [],
                "duration_ms": 0,
            }
        elif pending is not None:
            # An answer with no question before it is a fragment, not a turn.
            pending["answer"] = row.content
            turns.append(pending)
            pending = None
    if pending is not None:
        turns.append(pending)
    return turns


async def conversation_sessions(session_factory: Any = None) -> list[dict]:
    """Sessions that have a stored conversation, whether or not they have turns."""
    from app.memory.conversation import ConversationTurn

    factory = session_factory or AsyncSessionLocal
    if session_factory is None:
        await init_db()
    stmt = (
        select(
            ConversationTurn.session_id,
            func.count(ConversationTurn.id),
            func.min(ConversationTurn.timestamp),
            func.max(ConversationTurn.timestamp),
        )
        .group_by(ConversationTurn.session_id)
        .order_by(func.max(ConversationTurn.timestamp).desc())
    )
    async with factory() as session:
        rows = list((await session.execute(stmt)).all())
    return [
        {"session_id": r[0], "messages": r[1], "first": r[2], "last": r[3]}
        for r in rows
    ]


async def list_sessions(session_factory: Any = None) -> list[dict]:
    """Every session that has recorded turns, with counts and time span."""
    factory = session_factory or AsyncSessionLocal
    if session_factory is None:
        await init_db()
    stmt = (
        select(
            TurnEvent.session_id,
            func.count(TurnEvent.id),
            func.min(TurnEvent.timestamp),
            func.max(TurnEvent.timestamp),
        )
        .group_by(TurnEvent.session_id)
        .order_by(func.max(TurnEvent.timestamp).desc())
    )
    async with factory() as session:
        rows = list((await session.execute(stmt)).all())
    return [
        {"session_id": r[0], "turns": r[1], "first": r[2], "last": r[3]} for r in rows
    ]


def _row_to_dict(row: TurnEvent) -> dict:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "timestamp": row.timestamp,
        "source": row.source,
        "project": row.project,
        "user_message": row.user_message,
        "answer": row.answer,
        "task_type": row.task_type,
        "flow": row.flow,
        "tools": _loads(row.tool_trace, []),
        "files_written": _loads(row.files_written, []),
        "duration_ms": row.duration_ms,
    }


def _loads(raw: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


# ── rendering ──────────────────────────────────────────────────────────────


def render_transcript(turns: list[dict], session_id: str = "", note: str = "") -> str:
    """Render recorded turns as a Markdown transcript. Pure — no DB, no loop."""
    lines: list[str] = []
    title = (
        f"Coder transcript — session `{session_id}`"
        if session_id
        else "Coder transcript"
    )
    lines.append(f"# {title}")
    lines.append("")
    lines.append(_summary_line(turns))
    lines.append("")
    if note:
        # A transcript missing half the record must SAY so. Silence here reads
        # as "this build ran no tools", which is a lie about the work rather
        # than a gap in the record.
        lines.append(f"> **Note:** {note}")
        lines.append("")

    for n, turn in enumerate(turns, start=1):
        lines.extend(_render_turn(n, turn))

    return "\n".join(lines).rstrip() + "\n"


def _summary_line(turns: list[dict]) -> str:
    if not turns:
        return "_No turns recorded._"
    files = sorted({f for t in turns for f in t.get("files_written") or []})
    tools = sum(len(t.get("tools") or []) for t in turns)
    sources = sorted({str(t.get("source") or SOURCE_CLI) for t in turns})
    parts = [
        f"{len(turns)} turn{'s' if len(turns) != 1 else ''}",
        f"{tools} tool call{'s' if tools != 1 else ''}",
        f"{len(files)} file{'s' if len(files) != 1 else ''} written",
        "via " + ", ".join(f"`{s}`" for s in sources),
    ]
    first = str(turns[0].get("timestamp", ""))
    last = str(turns[-1].get("timestamp", ""))
    span = first if first == last else f"{first} → {last}"
    return "_" + " · ".join(parts) + "_\n\n_" + span + "_"


def _render_turn(n: int, turn: dict) -> list[str]:
    stamp = str(turn.get("timestamp") or "")
    source = str(turn.get("source") or SOURCE_CLI)
    lines = [f"## Turn {n} — {stamp} — `{source}`", ""]

    meta = []
    if turn.get("flow"):
        meta.append(f"flow `{turn['flow']}`")
    if turn.get("task_type"):
        meta.append(f"task type `{turn['task_type']}`")
    if turn.get("project"):
        meta.append(f"project `{turn['project']}`")
    duration = int(turn.get("duration_ms") or 0)
    if duration:
        meta.append(f"{duration / 1000:.1f}s")
    if meta:
        lines += ["_" + " · ".join(meta) + "_", ""]

    lines += ["### Prompt", "", _quote(str(turn.get("user_message") or "")), ""]

    tools = turn.get("tools") or []
    if tools:
        lines += [f"### Tools ({len(tools)})", ""]
        lines += ["| # | tool | target | result |", "| --- | --- | --- | --- |"]
        for i, tool in enumerate(tools, start=1):
            ok = "ok" if tool.get("success") else "**failed**"
            detail = str(tool.get("error") or "")
            cell = ok if not detail else f"{ok} — {_cell(detail)}"
            lines.append(
                f"| {i} | `{tool.get('tool', '?')}` | {_cell(str(tool.get('target') or ''))} | {cell} |"
            )
        lines.append("")

    files = turn.get("files_written") or []
    if files:
        lines += [f"### Files written ({len(files)})", ""]
        lines += [f"- `{f}`" for f in files]
        lines.append("")

    lines += ["### Answer", "", str(turn.get("answer") or ""), "", "---", ""]
    return lines


def _quote(text: str) -> str:
    """Block-quote a prompt, so a multi-line one stays one visual unit."""
    if not text.strip():
        return "> _(empty)_"
    return "\n".join("> " + line for line in text.splitlines())


def _cell(text: str, limit: int = 80) -> str:
    """One table cell: no pipes, no newlines, bounded."""
    flat = " ".join(text.split()).replace("|", "\\|")
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return f"`{flat}`" if flat else ""
