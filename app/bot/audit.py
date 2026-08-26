"""What the bot did, and for whom (Phase T3).

Separate from `turnlog`, deliberately. The turn log records what a TURN did and
is the deliverable transcript; this records what the ACCESS CONTROL did — who
was refused, who paired, who approved a write — including the events that never
became a turn at all, which is exactly the half a transcript cannot show.

Plain JSON Lines: one object per line, appended, no schema migration and
readable with `tail`. It is evidence, so it is never rewritten and never
truncated by this module.

Best-effort, like every other observability path here: an unwritable audit file
must not cost a turn. It is logged when it fails, so "no audit" cannot look the
same as "nothing happened".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# Events. Named rather than free text so the file can be filtered.
REFUSED = "refused"
PAIRED = "paired"
GRANTED = "granted"
REVOKED = "revoked"
TURN = "turn"
APPROVAL = "approval"
COMMAND = "command"


def audit_path(project: Path | str | None = None) -> Path:
    """Where the log lives: `<project>/.coder/bot_audit.log` by default.

    Per project, so a record travels with the folder it describes; and inside
    `.coder/`, which the RAG indexer, `project_memory._scan_project` and
    `_locate_named_file` already skip — an audit log must never be embedded,
    retrieved as if it were source, or chosen as a file to edit.
    """
    configured = Path(settings.bot_audit_log)
    if configured.is_absolute() or project is None:
        return configured
    return Path(project) / configured


def record(
    event: str,
    user_id: int | None = None,
    project: Path | str | None = None,
    **fields: Any,
) -> bool:
    """Append one event. Returns False rather than raising."""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "user_id": user_id,
        "project": str(project) if project is not None else "",
    }
    for key, value in fields.items():
        entry[key] = value if _is_plain(value) else str(value)
    try:
        path = audit_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logger.warning("audit entry not written: %s", event, exc_info=True)
        return False


def _is_plain(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict))


def read_entries(project: Path | str | None = None, limit: int = 50) -> list[dict]:
    """The last `limit` entries, oldest first. Unreadable lines are skipped."""
    path = audit_path(project)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out
