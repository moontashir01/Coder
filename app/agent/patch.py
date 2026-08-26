"""Block matching for surgical edits — shared by the agent and the file tools.

Why this is a module rather than a helper inside `core.py`: the tolerant
matching lived in `core._apply_block_linewise`, so the `edit_file` TOOL — the
path the native tool loop takes — had none of it. Its `old_str not in original`
refused a misquote the agent's own editor would have applied, and a 7B's answer
to a refused edit is to call `write_file` with the whole file regenerated. That
is the failure this entire ladder exists to prevent, so the matcher has to be
one matcher with two callers, not two.

The ladder, cheapest first. Every rung is a compromise between "a small model
misquotes what it is copying" and "a wrong match corrupts a file silently":

1. exact substring;
2. line-wise, ignoring TRAILING whitespace;
3. line-wise, ignoring ALL leading/trailing whitespace, with the replacement
   re-indented to the file (small models routinely drop the indentation of the
   lines they copy into SEARCH);
4. line-wise with whitespace NORMALIZED inside each line, accepted only at
   ``FUZZY_RATIO`` similarity, only when exactly ONE window in the file reaches
   that ratio, and never for a single-line SEARCH.

Rung 4's three conditions are the whole safety argument for having a rung 4.
A one-line SEARCH is not an anchor — `return None` occurs eleven times in a
file and the first hit is a coin flip — and a fuzzy match with two candidates
is a coin flip by definition. Both refuse rather than guess, because a wrong
replacement is silent and a refused one is reported and retried.

Nothing here writes, reads the disk or calls a model: it is pure text in, text
out, which is what lets the whole ladder be tested without either.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

#: Similarity a normalized window must reach before rung 4 will accept it.
FUZZY_RATIO = 0.90

#: Rung 4 refuses a SEARCH shorter than this many lines. One line is not an
#: anchor, and the cost of matching the wrong one is a silent corruption.
MIN_FUZZY_LINES = 2

#: Below this size a file is not big enough for "it shrank" to mean anything —
#: a three-line file legitimately becomes a one-line file.
SHRINK_MIN_CHARS = 400

#: A rewrite keeping less than this fraction of the original is treated as a
#: truncation, not an edit.
SHRINK_FLOOR = 0.6

_LINE_NO_RE = re.compile(r"^\s*(\d+)\s*\|\s?")

_DELETION_RE = re.compile(
    r"\b(delete|remove|drop|strip|clear|empty|truncate|trim|shorten|"
    r"start over|from scratch|rewrite)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BlockMatch:
    """Where a SEARCH block matched, and how much the replacement must move."""

    start: int  # first line index, inclusive
    end: int  # last line index, exclusive
    tier: str  # "trailing-ws" | "indent" | "fuzzy"
    pad: str = ""  # indentation to prepend to each replacement line


def strip_line_numbers(text: str) -> str:
    """Remove a `  42 | ` gutter the model copied out of a numbered listing.

    We show numbered lines when asking for an edit (they give a small model a
    way to say *where*), which creates a way for it to quote the numbers back
    inside SEARCH. Stripping them defensively is what makes the numbering safe
    to show at all.

    Deliberately strict: every non-blank line must carry the prefix AND the
    numbers must strictly increase, so a line of real code that happens to
    start with a digit and a pipe is never mistaken for a gutter.
    """
    lines = text.split("\n")
    if not lines:
        return text
    numbers: list[int] = []
    stripped: list[str] = []
    for line in lines:
        if not line.strip():
            stripped.append(line)
            continue
        m = _LINE_NO_RE.match(line)
        if not m:
            return text
        numbers.append(int(m.group(1)))
        stripped.append(line[m.end() :])
    if not numbers or numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
        return text
    return "\n".join(stripped)


def _leading_ws(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _pad_for(file_line: str, search_line: str) -> str:
    """The indentation the replacement must gain to sit where the match is."""
    file_indent = _leading_ws(file_line)
    search_indent = _leading_ws(search_line)
    if file_indent.endswith(search_indent):
        return file_indent[: len(file_indent) - len(search_indent)]
    return ""


def _norm(line: str) -> str:
    return " ".join(line.split())


def find_block(content: str, search: str) -> BlockMatch | None:
    """Locate ``search`` in ``content`` by rungs 2-4. Exact is the caller's job.

    Returns None when nothing matched, or when a fuzzy candidate was ambiguous
    — both mean "do not write", which is the only safe direction here.
    """
    if not search:
        return None
    c_lines = content.split("\n")
    s_lines = search.split("\n")
    n = len(s_lines)
    if n == 0 or n > len(c_lines):
        return None

    # Rung 2 — trailing whitespace only.
    cs = [x.rstrip() for x in c_lines]
    ss = [x.rstrip() for x in s_lines]
    for i in range(0, len(c_lines) - n + 1):
        if cs[i : i + n] == ss:
            return BlockMatch(i, i + n, "trailing-ws")

    # Rung 3 — all surrounding whitespace, replacement re-indented.
    csf = [x.strip() for x in c_lines]
    ssf = [x.strip() for x in s_lines]
    for i in range(0, len(c_lines) - n + 1):
        if csf[i : i + n] == ssf:
            return BlockMatch(i, i + n, "indent", _pad_for(c_lines[i], s_lines[0]))

    # Rung 4 — normalized similarity, unique, multi-line only.
    if n < MIN_FUZZY_LINES:
        return None
    needle = "\n".join(_norm(x) for x in s_lines)
    if not needle.strip():
        return None
    hits: list[tuple[float, int]] = []
    for i in range(0, len(c_lines) - n + 1):
        window = "\n".join(_norm(x) for x in c_lines[i : i + n])
        if not window.strip():
            continue
        ratio = difflib.SequenceMatcher(None, needle, window).ratio()
        if ratio >= FUZZY_RATIO:
            hits.append((ratio, i))
    if len(hits) != 1:
        # Zero is a miss; two or more is a coin flip. Both refuse.
        return None
    _, i = hits[0]
    return BlockMatch(i, i + n, "fuzzy", _pad_for(c_lines[i], s_lines[0]))


def apply_block(content: str, search: str, replace: str) -> str | None:
    """Apply one SEARCH/REPLACE pair. Returns the new text, or None if it missed."""
    search = strip_line_numbers(search)
    if not search:
        return None
    if search in content:
        return content.replace(search, replace, 1)
    match = find_block(content, search)
    if match is None:
        return None
    lines = content.split("\n")
    r_lines = replace.split("\n")
    if match.pad:
        r_lines = [(match.pad + rl if rl.strip() else rl) for rl in r_lines]
    return "\n".join(lines[: match.start] + r_lines + lines[match.end :])


def apply_edits(
    content: str, edits: list[tuple[str, str]]
) -> tuple[str, list[int], list[int]]:
    """Apply several pairs in order. Returns (new_content, applied, failed).

    ``applied``/``failed`` are indexes into ``edits``, so a caller can name the
    edit that missed rather than only counting it.
    """
    new = content
    applied: list[int] = []
    failed: list[int] = []
    for i, (search, replace) in enumerate(edits):
        patched = apply_block(new, search, replace)
        if patched is None:
            failed.append(i)
        else:
            new = patched
            applied.append(i)
    return new, applied, failed


def numbered(text: str, start: int = 1, width: int = 4) -> str:
    """Render text with a line-number gutter, as shown to the editing model."""
    return "\n".join(
        f"{start + offset:>{width}} | {line}"
        for offset, line in enumerate(text.split("\n"))
    )


def nearest_region(content: str, search: str, radius: int = 2) -> str:
    """The part of the file a failed SEARCH came closest to, numbered.

    This is the difference between an error that ends the turn and one the next
    call can act on: told only "string not found", a small model rewrites the
    file; shown the real lines, it quotes them back. Returns "" when nothing is
    close enough to be worth showing — a wrong region is a wrong instruction.
    """
    search = strip_line_numbers(search)
    if not search.strip() or not content.strip():
        return ""
    c_lines = content.split("\n")
    s_lines = search.split("\n")
    n = min(max(1, len(s_lines)), len(c_lines))
    needle = "\n".join(_norm(x) for x in s_lines)
    best_ratio = 0.0
    best_i = 0
    for i in range(0, len(c_lines) - n + 1):
        window = "\n".join(_norm(x) for x in c_lines[i : i + n])
        ratio = difflib.SequenceMatcher(None, needle, window).ratio()
        if ratio > best_ratio:
            best_ratio, best_i = ratio, i
    if best_ratio < 0.3:
        return ""
    lo = max(0, best_i - radius)
    hi = min(len(c_lines), best_i + n + radius)
    return numbered("\n".join(c_lines[lo:hi]), start=lo + 1)


def wants_deletion(message: str) -> bool:
    """Does the request itself ask for content to go away?"""
    return bool(_DELETION_RE.search(message or ""))


def is_catastrophic_shrink(
    old: str,
    new: str,
    message: str = "",
    min_chars: int = SHRINK_MIN_CHARS,
    floor: float = SHRINK_FLOOR,
) -> bool:
    """Would writing ``new`` over ``old`` be a truncation rather than an edit?

    The check every other stage is blind to: `check_file` asks whether the
    result parses, and half a Python file parses perfectly. Skipped below
    ``min_chars`` (a three-line file legitimately becomes a one-line file) and
    skipped when the request asked for something to be removed, because then
    the shrink is the point.
    """
    if len(old) < min_chars:
        return False
    if not new.strip():
        return True
    if wants_deletion(message):
        return False
    return len(new) < len(old) * floor
