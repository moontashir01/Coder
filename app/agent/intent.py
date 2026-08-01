"""Intent verification — "is this file what the user actually ASKED for?"

`app/agent/verify.py` answers a different question: does the file parse, and is
it the right KIND of content. Both are structural. A file that compiles cleanly,
contains real CSS, and balances every tag can still be the wrong page entirely —
a contact form when the request said login, a sort that returns the input
untouched — and the old pipeline reported it `verified OK` and moved on. Worse,
the repair prompt in `_verify_and_repair` never even received the user's
message, so there was no layer in the whole write path that had both the request
and the result in front of it at once.

This module is that layer. It is deliberately pure and offline — the LLM call
lives in `AgentCore`; everything here is prompt construction, parsing, and the
deterministic gates that decide whether a complaint is worth acting on.

The design problem is that the judge is the same 7B model that wrote the file,
so a naive "review this" loop produces confident nonsense and rewrites working
files into worse ones. Three rules keep that in check:

  * **Unparseable output means PASS.** A verdict we can't read is not a defect.
    Every ambiguity resolves toward leaving the file alone.
  * **Complaints are filtered deterministically** (`filter_complaints`) before
    anything is rewritten: suggestions ("could also add a footer"), complaints
    about *other* files, and complaints whose own words are all already in the
    file are dropped without a second LLM call.
  * **Only absence counts.** The prompt asks for requirements that are missing,
    never for opinions about style, naming, or structure — those are exactly
    what a small model will generate forever if you let it.

The caller adds the fourth rule, which this module cannot enforce: a rewrite
that breaks the syntax check is reverted, so intent repair can never turn a
working file into a broken one.
"""

from __future__ import annotations

import re
from pathlib import Path

# How much of the file the judge sees. The judge only has to spot ABSENCE, and
# what's absent is absent from the first 6 KB too; sending the whole file makes
# the call slower and pushes the request itself out of the model's attention.
MAX_JUDGE_CHARS = 6000

# The caller's `extra_context` in a multi-file build is the plan manifest, the
# build spec AND the sibling files' markup — several KB that would swamp the one
# thing the judge is here to compare (this file against this request). It gets a
# small slice purely so shared requirements aren't read as unrequested extras.
MAX_JUDGE_CONTEXT_CHARS = 1200

# A request shorter than this says too little to judge a file against ("fix it",
# "now the css"), and judging against it produces invented requirements.
_MIN_REQUEST_CHARS = 12

# More than a handful of complaints means the judge is reviewing, not checking.
MAX_COMPLAINTS = 5

# A single complaint longer than this is a paragraph of commentary, not a named
# missing requirement.
_MAX_COMPLAINT_CHARS = 200

INTENT_JUDGE_SYSTEM = (
    "You are a strict requirements checker. You are given a user's request and "
    "the contents of ONE file written to satisfy it. Your only job is to decide "
    "whether the file does what the request asked for. You never rewrite the "
    "file, and you never give advice."
)

INTENT_JUDGE_INSTRUCTIONS = """
Answer in EXACTLY one of these two forms, and nothing else:

PASS

or:

MISSING:
- <something the request asked for that this file does not do>
- <another one>

Rules:
- Judge ONLY against what the request explicitly asks for. A file you would have
  written differently still passes. If it does what was asked, answer PASS.
- Report only things that are ABSENT. Never report style, formatting, naming,
  wording, performance, accessibility, or "could also add" ideas.
- Never report anything about a DIFFERENT file. Missing stylesheets, scripts and
  images are handled elsewhere — judge only the contents of this one file.
- Each line names one concrete missing thing, in a few words.
- If you are unsure, answer PASS."""

_FENCE_LINE_RE = re.compile(r"^\s*```[\w-]*\s*$|^\s*```\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_PASS_LINE_RE = re.compile(r"^\s*\**\s*pass\b[\s.!*]*$", re.IGNORECASE)
_MISSING_MARKER_RE = re.compile(r"^\s*\**\s*missing\s*:?\s*\**\s*", re.IGNORECASE)

# Hedging = a suggestion, not a missing requirement. The judge is told not to
# produce these; a small model produces them anyway, so drop them here.
_SUGGESTION_RE = re.compile(
    r"\b(consider|could|would be|might|maybe|perhaps|suggest\w*|recommend\w*|"
    r"improve\w*|enhance\w*|better|nicer|cleaner|optional\w*|ideally|"
    r"nice\s+to\s+have|for\s+completeness)\b",
    re.IGNORECASE,
)

# "the styles.css file is missing", "no script.js" — a cross-file complaint.
# _repair_dead_references owns that; rewriting THIS file cannot fix it.
_FILENAME_RE = re.compile(r"\b[\w-]+\.(?:css|js|jsx|ts|tsx|html|htm|py|json|png|svg)\b")

_WORD_RE = re.compile(r"[a-z0-9]+")

# Words that carry no signal when checking whether a complaint is already
# satisfied by the file's own text.
_STOPWORDS = {
    "does",
    "have",
    "with",
    "that",
    "this",
    "from",
    "into",
    "when",
    "there",
    "here",
    "file",
    "code",
    "page",
    "content",
    "section",
    "element",
    "missing",
    "lacks",
    "lacking",
    "absent",
    "without",
    "needs",
    "need",
    "should",
    "must",
    "requested",
    "request",
    "asked",
    "user",
    "only",
    "also",
    "some",
    "any",
    "each",
    "which",
    "where",
    "what",
    "they",
    "them",
    "their",
    "been",
    "being",
    "does",
    "doesn",
    "isn",
    "not",
    "none",
}


def should_check_intent(user_message: str, filename: str | Path) -> bool:
    """Is it worth spending a judge call on this write?

    Needs a request substantial enough to judge against. A bare follow-up ("fix
    it", "again") names no requirement, so a judge given one invents them.
    """
    if not (user_message or "").strip():
        return False
    if len((user_message or "").strip()) < _MIN_REQUEST_CHARS:
        return False
    return bool(str(filename or "").strip())


def build_judge_prompt(
    user_message: str,
    filename: str,
    content: str,
    extra_context: str = "",
) -> str:
    """The judge's HumanMessage: the request, the file, and the question."""
    body = content[:MAX_JUDGE_CHARS]
    if len(content) > MAX_JUDGE_CHARS:
        body += "\n... [truncated]"
    extra_block = ""
    if extra_context:
        trimmed = extra_context[:MAX_JUDGE_CONTEXT_CHARS]
        extra_block = f"\nBuild context (for reference only):\n{trimmed}\n"
    return (
        f"The user asked for:\n{user_message}\n"
        f"{extra_block}"
        f"\nThe file `{filename}` was written to satisfy it:\n"
        f"---\n{body}\n---\n\n"
        f"Does `{filename}` do what the request asked for?"
        f"{INTENT_JUDGE_INSTRUCTIONS}"
    )


def parse_verdict(raw: str) -> list[str]:
    """Read the judge's answer into a list of complaints; [] means pass.

    Every unreadable answer parses as PASS. A verdict we cannot understand is
    not evidence of a defect, and acting on it would rewrite a file on noise.
    """
    text = (raw or "").strip()
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if not _FENCE_LINE_RE.match(ln)]

    marker_at = -1
    for i, line in enumerate(lines):
        if _MISSING_MARKER_RE.match(line):
            marker_at = i
            break
        if _PASS_LINE_RE.match(line):
            return []  # concluded PASS before any complaint — trust it
    if marker_at == -1:
        return []  # no MISSING section at all → pass

    complaints: list[str] = []
    same_line = _MISSING_MARKER_RE.sub("", lines[marker_at]).strip()
    if same_line:
        complaints.append(same_line)
    for line in lines[marker_at + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if _MISSING_MARKER_RE.match(stripped):
            continue
        bullet = _BULLET_RE.match(stripped)
        if bullet:
            complaints.append(stripped[bullet.end() :].strip())
        elif not complaints:
            # An unbulleted first line right under the marker is still the item.
            complaints.append(stripped)
        else:
            # Prose after the list is commentary — the list has ended.
            break

    return _tidy(complaints)


def _tidy(complaints: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for c in complaints:
        c = c.strip().strip("`").rstrip(".;,").strip()
        if not c or len(c) > _MAX_COMPLAINT_CHARS:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= MAX_COMPLAINTS:
            break
    return out


def _content_tokens(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall(text.lower()) if len(w) >= 4 and w not in _STOPWORDS
    }


def filter_complaints(complaints: list[str], content: str, filename: str) -> list[str]:
    """Drop the complaints that aren't worth a rewrite — no LLM call needed.

    Three deterministic gates, all biased toward leaving the file alone:

      * **Hedged** — "could also add a footer" is a suggestion. The judge was
        told not to emit these; it does anyway.
      * **About another file** — "styles.css is missing" names a file that isn't
        this one. `_repair_dead_references` creates those; rewriting this file
        cannot.
      * **Already satisfied** — every meaningful word of the complaint is
        already in the file. This is the characteristic small-model false alarm
        (it skims, then reports a section it just read as absent).

    The last gate can discard a real complaint whose vocabulary happens to
    appear in the file ("no form validation" when the words `form` and
    `validation` are both present but nothing validates). That trade is
    deliberate: a missed complaint leaves a file that a human can still review,
    while a false one rewrites a good file with a worse one.
    """
    stem = Path(filename).name.lower()
    file_tokens = _content_tokens(content)
    kept: list[str] = []
    for c in complaints:
        if _SUGGESTION_RE.search(c):
            continue
        others = [m.group(0).lower() for m in _FILENAME_RE.finditer(c)]
        if others and all(o != stem for o in others):
            continue
        tokens = _content_tokens(c)
        if not tokens:
            continue  # nothing concrete named
        if tokens <= file_tokens:
            continue  # the file already contains every word of the complaint
        kept.append(c)
    return kept


def build_repair_prompt(
    user_message: str,
    filename: str,
    content: str,
    complaints: list[str],
    extra_context: str = "",
) -> str:
    """Ask for the complete file again, with the unmet requirements named.

    Note what this does NOT say: it never mentions style, quality, or
    improvement. The model is told to keep everything that already works and
    add only what is listed, because "regenerate this file, better" is how a 7B
    model loses half the page it got right the first time.
    """
    body = content[:MAX_JUDGE_CHARS]
    if len(content) > MAX_JUDGE_CHARS:
        body += "\n... [truncated]"
    items = "\n".join(f"- {c}" for c in complaints)
    extra_block = f"\n{extra_context}\n" if extra_context else ""
    return (
        f"The user asked for:\n{user_message}\n"
        f"{extra_block}"
        f"\nThe current `{filename}` does NOT yet do all of it. Unmet:\n{items}\n\n"
        f"Current content:\n{body}\n\n"
        f"Return the COMPLETE `{filename}` with those points addressed. Keep "
        f"everything that already works — same structure, names, ids, classes "
        f"and links — and change only what is needed to satisfy the list above. "
        f"Do not remove existing content and do not rename anything."
    )
