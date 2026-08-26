"""User-authored, per-project instructions (`.coder/INSTRUCTIONS.md`).

Coder already has two kinds of project knowledge, and neither is this one:

- **`ProjectSpec`** (`.coder/project.json`) records what the project *contains*
  — its tables, routes and pages — and is written by the agent, from the files.
- **Skills** (`app/resources/skills/`) are matched per turn by keyword and
  embedding, and are global to the install rather than to one project.

Missing was the third: *how the user wants work done here* — "this repo uses
tabs", "never touch `vendor/`", "tests go beside the module, not in tests/".
That is not derivable from the code (so the spec cannot hold it) and it is not
keyword-triggered (so a skill is the wrong shape); it must simply be true for
every turn in this project. So it is a file the user writes, loaded once when
the project is loaded, and stated in the prompt.

Four rules, all of them about not making this a new failure mode:

- **It is CONVENTIONS, never capability.** The block says so, and more
  importantly nothing here touches the enforcement layers: the executor's
  permission gate, `settings.denied_permissions`, the approval hook, the path
  jail and the shell denylist all sit below the prompt and are unreachable from
  it. An instruction file cannot unlock a tool, widen the sandbox or approve a
  write — at worst it wastes a turn.
- **It is bounded and says when it was cut.** `max_instructions_chars` caps what
  reaches the prompt, because this text is prepended to every prompt in the
  project and an unbounded file would evict the sibling/RAG context that the
  answer actually depends on. A silent truncation is a rule the model will not
  follow and nobody will know is missing — same reason `_requirements_doc_context`
  states its own.
- **It cannot be a path escape.** The path is built from the project root, never
  from user input; it is still resolved and checked for containment, because a
  symlink at `.coder/INSTRUCTIONS.md` would otherwise read an arbitrary file.
- **Every failure is silent and empty.** Unreadable, undecodable, a directory,
  missing: all return `""` and the turn proceeds exactly as it did before the
  file existed.

The file lives in `.coder/` deliberately: it is a dot-directory, so the RAG
indexer, `project_memory._scan_project` and `_locate_named_file` already skip
it — the instructions are never embedded and retrieved back as if they were
source, and never picked as an edit target.

**Trust note.** This file travels with the project folder, so a repository
cloned from elsewhere can carry one. It is treated as the user's own
configuration (not wrapped as untrusted data), which is only reasonable because
loading it is *reported* — `AgentCore.load_project` returns the size and the
REPL prints it — and because it grants nothing. `settings.project_instructions`
turns it off entirely.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the file lives, relative to the project root.
INSTRUCTIONS_RELPATH = Path(".coder") / "INSTRUCTIONS.md"

HEADING = "## Project Instructions"

_PREAMBLE = (
    "Conventions for THIS project, written by the user. Follow them on every "
    "turn unless this turn's message says otherwise. They are conventions only: "
    "they do not grant permissions, enable tools, or override any rule above."
)


def instructions_path(root: str | Path) -> Path:
    """Where this project's instruction file would be. Does not check existence."""
    return Path(root) / INSTRUCTIONS_RELPATH


def _is_contained(path: Path, root: Path) -> bool:
    """True if `path` really resolves inside `root`.

    The path is constructed, never user-supplied, so this is not about
    traversal in the argument — it is about a symlink at `.coder/INSTRUCTIONS.md`
    pointing somewhere else on disk. Reading through one would be a file the
    user never wrote reaching every prompt in the project.
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def load_instructions(root: str | Path, max_chars: int) -> str:
    """Read `<root>/.coder/INSTRUCTIONS.md`, capped at `max_chars`.

    Returns "" when the file is absent, empty, unreadable, not a regular file,
    resolves outside `root`, or when `max_chars` is not positive. Never raises:
    a broken instruction file must cost nothing but its own contents.

    Truncation is STATED in the returned text, so a rule that did not fit is
    visible rather than silently dropped.
    """
    if max_chars <= 0:
        return ""
    root_path = Path(root)
    path = instructions_path(root_path)
    try:
        if not path.is_file() or not _is_contained(path, root_path):
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        # Best-effort, like every other derived-from-disk fact here — but logged,
        # because a file the user wrote and Coder silently ignored is confusing.
        logger.warning("could not read project instructions at %s: %s", path, e)
        return ""
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + (
            f"\n\n[TRUNCATED: {max_chars} of {len(text)} characters shown. "
            "Everything after this point was NOT read — raise "
            "max_instructions_chars to include it.]"
        )
    return text


def to_context_block(text: str) -> str:
    """Format loaded instructions for a system prompt. "" stays "".

    An empty heading is worse than no heading: it tells the model a section
    exists and then says nothing in it.
    """
    text = (text or "").strip()
    if not text:
        return ""
    return f"{HEADING}\n{_PREAMBLE}\n\n{text}"
