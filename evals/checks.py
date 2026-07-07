"""Declarative outcome checks for eval tasks.

Each factory returns a ``Check``: a callable ``(CheckContext) -> (bool, str)``.
The string is a human-readable detail shown when the check fails (and kept for
passing checks too, so a report can explain what was verified).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Tuple

if TYPE_CHECKING:
    from evals.harness import CheckContext

Check = Callable[["CheckContext"], Tuple[bool, str]]


def answer_contains(substring: str, case_insensitive: bool = True) -> Check:
    """The agent's textual answer includes ``substring``."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        hay = ctx.answer or ""
        needle = substring
        if case_insensitive:
            hay, needle = hay.lower(), needle.lower()
        ok = needle in hay
        return ok, f"answer {'contains' if ok else 'is missing'} {substring!r}"

    return check


def file_exists(relpath: str) -> Check:
    """A file was created at ``relpath`` under the task's working dir."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        ok = (ctx.workdir / relpath).is_file()
        return ok, f"file {relpath} {'exists' if ok else 'was not created'}"

    return check


def file_contains(relpath: str, substring: str) -> Check:
    """File ``relpath`` exists and its text includes ``substring``."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        p = ctx.workdir / relpath
        if not p.is_file():
            return (
                False,
                f"file {relpath} not found (expected to contain {substring!r})",
            )
        ok = substring in p.read_text(encoding="utf-8", errors="replace")
        return ok, f"{relpath} {'contains' if ok else 'is missing'} {substring!r}"

    return check


def file_excludes(relpath: str, substring: str) -> Check:
    """File ``relpath`` does NOT contain ``substring`` (a missing file passes).

    Use for "the inline <style> was moved out of index.html".
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        p = ctx.workdir / relpath
        if not p.is_file():
            return True, f"file {relpath} absent → cannot contain {substring!r}"
        ok = substring not in p.read_text(encoding="utf-8", errors="replace")
        return ok, f"{relpath} {'excludes' if ok else 'still contains'} {substring!r}"

    return check


def used_tool(tool_name: str) -> Check:
    """The tool trace contains a call to ``tool_name``."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        ok = any(t.get("tool") == tool_name for t in ctx.trace)
        return ok, f"tool {tool_name} {'was' if ok else 'was NOT'} called"

    return check


def min_files_written(n: int) -> Check:
    """At least ``n`` successful write_file/create_file calls happened."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        count = sum(
            1
            for t in ctx.trace
            if t.get("tool") in ("write_file", "create_file")
            and (t.get("result") or {}).get("success")
        )
        ok = count >= n
        return ok, f"{count} file(s) written (need >= {n})"

    return check
