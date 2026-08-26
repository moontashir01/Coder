"""An answer, as Telegram can actually show it (Phase T2).

Pure — text in, text out, no network and no Telegram library — which is what
lets the whole of this file be tested offline, and is the same split
`turnlog.render_transcript` uses.

Three problems, and the third is the one that bites:

1. **Telegram is not Markdown.** MarkdownV2 requires escaping sixteen
   characters *including inside code spans*, and one missed backslash makes the
   API reject the whole message — so an answer full of code would routinely
   fail to send at all. HTML mode needs three characters escaped (`&<>`) and is
   the mode used here.
2. **A message is capped at 4096 characters.** A build answer plus its repair
   notes runs past that regularly.
3. **Splitting at 4096 breaks the markup.** A chunk that ends inside a code
   block sends an unclosed `<pre>`, which Telegram rejects; the next chunk then
   arrives as unformatted text with stray backticks. So the split happens on
   the SOURCE, line by line, and a fence open at a boundary is closed and
   reopened — each chunk is independently well-formed.

The budget is measured on the RENDERED length, not the source: `&` becomes
`&amp;`, so a source-length cap under-counts by up to five times on exactly the
kind of text (a diff, a URL query) that is already near the limit.
"""

from __future__ import annotations

import re

#: Telegram's hard limit on one text message.
MESSAGE_LIMIT = 4096

#: Room left in every chunk for the `<pre><code class="language-python">`
#: wrapper a reopened fence adds, plus the continuation marker.
_WRAPPER_RESERVE = 220

_FENCE_RE = re.compile(r"^\s*```(\w*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML mode reserves."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_html(text: str) -> str:
    """Render one chunk of answer markdown as Telegram HTML.

    Handles fenced blocks, inline code and `**bold**`. Everything else is
    escaped and passed through — a renderer that tried to be complete would
    mangle far more than it improved, and the reader wants the code readable,
    not the prose typeset.
    """
    out: list[str] = []
    in_fence = False
    language = ""
    body: list[str] = []

    for line in text.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            if in_fence:
                out.append(_code_block("\n".join(body), language))
                body, language, in_fence = [], "", False
            else:
                in_fence = True
                language = fence.group(1) or ""
            continue
        if in_fence:
            body.append(line)
        else:
            out.append(_inline(line))

    if in_fence:
        # An unterminated fence is the model's, not ours: render what is there
        # rather than dropping it or emitting an unclosed tag.
        out.append(_code_block("\n".join(body), language))
    return "\n".join(out)


def _code_block(code: str, language: str) -> str:
    escaped = escape_html(code)
    if language:
        return f'<pre><code class="language-{escape_html(language)}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


def _inline(line: str) -> str:
    """Escape a prose line, then restore inline code and bold as tags."""
    escaped = escape_html(line)
    escaped = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    return _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split answer markdown into chunks that each render inside `limit`.

    Splits on line boundaries and **reopens a fence that was open at the
    boundary**, so no chunk ever carries an unbalanced code block. Costs are
    measured after escaping, because that is what Telegram counts.
    """
    budget = max(200, limit - _WRAPPER_RESERVE)
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    in_fence = False
    language = ""

    def flush() -> None:
        nonlocal current, used
        if not current:
            return
        lines = list(current)
        if in_fence:
            lines.append("```")
        chunks.append("\n".join(lines))
        current = []
        used = 0

    for line in text.split("\n"):
        for piece in _hard_split(line, budget):
            cost = len(escape_html(piece)) + 1
            if used + cost > budget and current:
                flush()
                if in_fence:
                    current.append(f"```{language}")
                    used = len(language) + 4
            current.append(piece)
            used += cost

            fence = _FENCE_RE.match(piece)
            if fence:
                if in_fence:
                    in_fence, language = False, ""
                else:
                    in_fence, language = True, fence.group(1) or ""

    flush()
    return chunks or [""]


def _hard_split(line: str, budget: int) -> list[str]:
    """Break one over-long line. A single minified file has no line breaks."""
    if len(escape_html(line)) + 1 <= budget:
        return [line]
    pieces: list[str] = []
    current = ""
    used = 0
    for char in line:
        cost = len(escape_html(char))
        if used + cost > budget - 1:
            pieces.append(current)
            current, used = "", 0
        current += char
        used += cost
    if current:
        pieces.append(current)
    return pieces


def render_chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """The whole job: answer markdown -> a list of sendable HTML messages."""
    return [to_html(chunk) for chunk in split_message(text, limit)]


def tool_line(tool: str, result: dict | None = None) -> str:
    """One progress line for a tool step, matching the REPL's `[Tool] name ✓`."""
    result = result or {}
    mark = "✓" if result.get("success") else "✗"
    detail = ""
    if not result.get("success") and result.get("error"):
        detail = f" — {str(result['error']).splitlines()[0][:120]}"
    return f"[Tool] {tool} {mark}{detail}"


def approval_question(tool: str, arguments: dict, permissions: list[str]) -> str:
    """What the user is being asked to allow. Names the TARGET, not just the tool.

    "Allow write_file?" is unanswerable — the whole question is *which file*.
    """
    target = ""
    for key in ("path", "file_path", "command"):
        value = (arguments or {}).get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            break
    lines = [f"<b>Approve</b> <code>{escape_html(tool)}</code>"]
    if target:
        lines.append(f"target: <code>{escape_html(target[:300])}</code>")
    if permissions:
        lines.append("permissions: " + ", ".join(escape_html(p) for p in permissions))
    return "\n".join(lines)
