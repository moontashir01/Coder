"""The turn's own task list — state the model keeps, restated every round.

The tool loop plans once and then executes blind: step 7 sees only the
ToolMessage history, never "3 of 6 done, next is X", and on a long turn the
early steps fall out of the context window entirely. Every deterministic
repair pass downstream (`_verify_blueprint_coverage`, `_repair_dead_references`,
`_wire_missing_endpoints`) is a patch for a step the model forgot; this module
is the thing that stops the forgetting at source, the way Claude Code's own
todo list does.

Design rules, all inherited from elsewhere in this codebase:

- **Pure state + a prompt block, zero extra LLM calls.** The model updates the
  list inside tool calls it was already making (unlike `check_intent`, which
  spends one call per file). Everything here is dataclasses and strings, so all
  of it unit-tests with no LLM and no browser — `pointer.py`'s split.
- **An unparseable update never destroys the list** (`parse_verdict`'s rule:
  silence is the safe answer). The handler also never *fails*: a failed tool
  counts toward `max_tool_failures` and two malformed updates would end the
  whole turn — a note-taking slip must never cost a turn whose files already
  landed (`ProjectSpec.save`'s rule).
- **The list is a hint to the model, never a gate.** Nothing downstream trusts
  it: a todo marked done that wasn't is still caught by the deterministic
  checks. At worst it wastes context, which is why it is capped.
- **Capped** (`MAX_ITEMS`/`MAX_TEXT_CHARS`) — `llm_num_ctx` is 16384 and the
  block is restated every round, so an unbounded list evicts the sibling/file
  context the actual work depends on (`_sibling_context`'s lesson).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid a runtime import cycle with tool_registry consumers
    from app.agent.tool_registry import ToolDefinition

MAX_ITEMS = 20
MAX_TEXT_CHARS = 160

STATUS_TODO = "todo"
STATUS_WORKING = "working"
STATUS_DONE = "done"

# Tolerant status mapping: a 7B writes "in_progress", "completed", "pending"…
# Anything unrecognised is "todo" — the reading that makes the model finish the
# item, never the one that silently marks work done.
_STATUS_ALIASES = {
    "todo": STATUS_TODO,
    "pending": STATUS_TODO,
    "open": STATUS_TODO,
    "not_started": STATUS_TODO,
    "working": STATUS_WORKING,
    "in_progress": STATUS_WORKING,
    "in-progress": STATUS_WORKING,
    "doing": STATUS_WORKING,
    "active": STATUS_WORKING,
    "current": STATUS_WORKING,
    "done": STATUS_DONE,
    "complete": STATUS_DONE,
    "completed": STATUS_DONE,
    "finished": STATUS_DONE,
}

_MARKS = {STATUS_TODO: "[ ]", STATUS_WORKING: "[>]", STATUS_DONE: "[x]"}


@dataclass
class Todo:
    text: str
    status: str = STATUS_TODO


def _normalize_status(raw: Any) -> str:
    return _STATUS_ALIASES.get(str(raw or "").strip().lower(), STATUS_TODO)


def parse_items(raw: Any) -> list[Todo] | None:
    """Turn the tool's ``todos`` argument into a list of Todo, or None.

    Tolerant on shape (a list of dicts, a list of plain strings, mixed), strict
    on meaning: None means "could not read this at all", and the caller then
    keeps the old list — an update must never be able to wipe state by being
    malformed. An empty list is a VALID answer (the model clearing its list).
    """
    if not isinstance(raw, list):
        return None
    items: list[Todo] = []
    for entry in raw:
        if isinstance(entry, str):
            text, status = entry, STATUS_TODO
        elif isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("task") or "").strip()
            status = _normalize_status(entry.get("status"))
        else:
            continue
        text = " ".join(str(text).split())  # collapse newlines/runs of spaces
        if not text:
            continue
        items.append(Todo(text=text[:MAX_TEXT_CHARS], status=status))
        if len(items) >= MAX_ITEMS:
            break
    if not items and raw:
        return None  # non-empty input, nothing readable in it
    return items


class TodoStore:
    """This turn's list. Owned by one AgentCore; cleared at the top of chat()
    beside `_blueprint`/`_build_spec` (the per-turn-state rule — turns on one
    core are serialized by the session registry, so no locking here)."""

    def __init__(self) -> None:
        self.items: list[Todo] = []

    def clear(self) -> None:
        self.items = []

    def replace(self, items: list[Todo]) -> None:
        self.items = list(items[:MAX_ITEMS])

    def remaining(self) -> list[str]:
        return [t.text for t in self.items if t.status != STATUS_DONE]

    def render(self) -> str:
        """The block restated to the model each round. Empty string when there
        is no list, so callers can append it unconditionally."""
        if not self.items:
            return ""
        lines = [f"{_MARKS[t.status]} {t.text}" for t in self.items]
        done = sum(1 for t in self.items if t.status == STATUS_DONE)
        header = f"CURRENT TASK LIST ({done}/{len(self.items)} done):"
        return "\n".join([header, *lines])


def build_todo_tool(store: TodoStore) -> "ToolDefinition":
    """The `update_todos` builtin, bound to one agent's store.

    Registered per-AgentCore (each core builds its own registry), never into
    the shared `get_registry()` default — a handler closed over one agent's
    state must not be visible from another agent.

    `permissions=[]`: it touches no file, no shell, no network, so the approval
    gate and `denied_permissions` never fire and a bot `viewer` may use it.

    The handler ALWAYS returns success=True. A malformed update reports itself
    in `result` (so the model can correct course) but is never a tool failure —
    `max_tool_failures` would otherwise end the turn over a note-taking slip.
    """
    from app.agent.tool_registry import ToolDefinition

    def update_todos(todos: Any = None) -> dict:
        items = parse_items(todos)
        if items is None:
            return {
                "success": True,
                "result": (
                    "Task list unchanged — could not parse the update. Send "
                    'todos as a list of {"text": str, "status": '
                    '"todo"|"working"|"done"} objects.'
                ),
                "error": None,
            }
        store.replace(items)
        rendered = store.render() or "Task list cleared."
        return {"success": True, "result": rendered, "error": None}

    return ToolDefinition(
        name="update_todos",
        description=(
            "Keep your task list for this request. Call it FIRST on any "
            "multi-step task with every step listed, then again whenever a "
            "step is finished (status done) or started (status working). "
            "Replaces the whole list each call. This does not touch any file "
            "— it is your own working memory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": (
                        "The full list, in order. Each item: "
                        '{"text": "<the step>", "status": '
                        '"todo"|"working"|"done"}.'
                    ),
                },
            },
            "required": ["todos"],
        },
        source="builtin",
        handler=update_todos,
        permissions=[],
    )
