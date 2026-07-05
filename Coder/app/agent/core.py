import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.agent.executor import Executor
from app.agent.planner import Planner, _extract_json
from app.agent.recovery import classify_error, recovery_hint
from app.agent.tool_registry import ToolRegistry, create_registry
from app.agent.verify import check_file, is_verifiable
from app.memory.conversation import ConversationMemory
from app.memory.project_memory import ProjectMemory, project_memory
from app.models.llm import get_llm, get_streaming_llm
from app.rag.retriever import Retriever
from app.rag.retriever import retriever as _default_retriever
from config.settings import settings

# Imported lazily to avoid circular deps at module init
_MCPManager = None


def _get_mcp_manager_class():
    global _MCPManager
    if _MCPManager is None:
        from app.mcp.manager import MCPManager

        _MCPManager = MCPManager
    return _MCPManager


_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "system.md"


def _tool_guidance(workdir: str) -> str:
    """System-prompt block for the native tool-calling loop.

    Tool schemas are provided via ChatOllama.bind_tools — no JSON protocol
    text belongs here, only behavioral guidance.
    """
    return f"""Working directory: {workdir}

You have access to real tools via native function calling. Use a tool when the
task needs file or command access; answer directly (in plain text) when it does not.
When asked to create or save a file, you MUST call write_file (or create_file)
with a relative path like "index.html" — do not just print the code."""


def _load_system_prompt() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return "You are Coder, an expert offline AI coding assistant."


def _truncate_context(text: str, max_chars: int = 3000) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "\n... [context truncated]"
    return text


# Verb + target heuristics: does the user want a file created/edited on disk?
_FILE_OP_VERB_RE = re.compile(
    r"\b(create|make|write|save|generate|build|scaffold|add|append|insert|"
    r"update|change|modify|edit|refactor|rewrite|fix|implement|put)\b",
    re.IGNORECASE,
)
_FILE_OP_TARGET_RE = re.compile(
    r"\b(file|html|css|page|webpage|website|script|component|module|app)\b"
    r"|\b[\w./-]+\.\w{1,6}\b",  # an explicit filename like index.html
    re.IGNORECASE,
)


def _wants_file_op(message: str) -> bool:
    """True when the message asks to create/edit a file on disk (not just show code)."""
    return bool(_FILE_OP_VERB_RE.search(message) and _FILE_OP_TARGET_RE.search(message))


# A separation/restructure verb that implies touching more than one file.
_MULTIFILE_VERB_RE = re.compile(
    r"\b(separate|split|extract|reorganize|reorganise|restructure)\b",
    re.IGNORECASE,
)
_MOVE_INTO_FILES_RE = re.compile(
    r"\bmove\b.*\binto\b.*\bfiles?\b", re.IGNORECASE | re.DOTALL
)
_FILETYPE_RE = re.compile(
    r"\b(html|css|js|javascript|ts|typescript|python|json|scss)\b", re.IGNORECASE
)


def wants_multifile(message: str) -> bool:
    """True when the request implies operating on several files at once.

    Catches "separate/split/extract … files" and "move the css and js into
    separate files". Deliberately tighter than _wants_file_op so ordinary
    single-file create/edit requests still go through _file_op_flow.
    """
    if _MOVE_INTO_FILES_RE.search(message):
        return True
    if not _MULTIFILE_VERB_RE.search(message):
        return False
    if re.search(r"\bfiles\b", message, re.IGNORECASE):  # plural "files"
        return True
    # …or it names two or more distinct languages to pull apart.
    types = {m.lower() for m in _FILETYPE_RE.findall(message)}
    types.discard("ts")  # avoid double-counting typescript/ts overlap noise
    return len(types) >= 2


_FILENAME_IN_MSG_RE = re.compile(r"\b([\w./-]+\.\w{1,6})\b")

# keyword → default filename when the user names no explicit file
_INFER_FILENAME_TABLE: list[tuple[str, str]] = [
    ("html", "index.html"),
    ("css", "styles.css"),
    ("javascript", "script.js"),
    ("typescript", "script.ts"),
    ("react", "App.jsx"),
    ("python", "main.py"),
    ("markdown", "README.md"),
    ("readme", "README.md"),
    ("json", "data.json"),
    ("yaml", "config.yaml"),
]

_FILE_GEN_INSTRUCTIONS = """

## File generation mode
You are creating or updating exactly ONE file on disk. Respond in EXACTLY this format, nothing else:
FILENAME: <relative filename, e.g. index.html>
<the complete file contents>

Do NOT wrap the contents in markdown code fences. Do NOT add any explanation before or after.
Do NOT add "before/after" comments or describe your changes inside the file — output only the
real file contents. Produce complete, production-quality content. For a webpage, include real HTML
structure, CSS styling (in a <style> block or linked file), and meaningful sample content — not a stub."""


def _extract_filename(message: str) -> str | None:
    m = _FILENAME_IN_MSG_RE.search(message)
    return m.group(1) if m else None


# `@path` references, e.g. "change @src/app.py" (Claude-Code style file mention).
_AT_REF_RE = re.compile(r"(?<!\w)@([\w./\\-]+)")


def _extract_at_refs(message: str) -> list[str]:
    """Return the paths referenced with @ in a message, in order."""
    return _AT_REF_RE.findall(message)


def _strip_at_refs(message: str) -> str:
    """Drop the leading @ from each reference so the model sees a plain path."""
    return _AT_REF_RE.sub(lambda m: m.group(1), message)


def _infer_filename(message: str) -> str:
    low = message.lower()
    for keyword, name in _INFER_FILENAME_TABLE:
        if keyword in low:
            return name
    return "output.txt"


# Per-extension content guard — the 3B model otherwise writes JS into a .css
# file (and vice-versa) when a request mentions several languages at once.
_EXT_GUARD: dict[str, str] = {
    ".css": "This file is CSS. Output ONLY CSS rules and selectors. "
    "Do NOT include any HTML tags or JavaScript.",
    ".js": "This file is JavaScript. Output ONLY JavaScript. "
    "Do NOT include any HTML tags, <script> wrappers, or CSS.",
    ".ts": "This file is TypeScript. Output ONLY TypeScript. No HTML or CSS.",
    ".html": 'This file is HTML. Link external CSS with <link rel="stylesheet"> '
    "and external JS with <script src> — do NOT inline large blocks.",
    ".py": "This file is Python. Output ONLY Python source.",
}


def _extension_guard(filename: str) -> str:
    """Return a one-line content rule for the file's extension, or '' if unknown."""
    return _EXT_GUARD.get(Path(filename).suffix.lower(), "")


_FENCE_BLOCK_RE = re.compile(r"```[\w+.-]*\n(.*?)\n?```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    # Whole string is one fenced block → unwrap it.
    m = re.match(r"^```[\w+.-]*\n(.*?)\n?```$", t, re.DOTALL)
    if m:
        return m.group(1)
    # Model wrapped the file in a fence but added prose around it → take the
    # largest fenced block so prose doesn't get written into the file.
    blocks = _FENCE_BLOCK_RE.findall(t)
    if blocks:
        return max(blocks, key=len)
    # Strip a stray unmatched fence line at the very start or end.
    lines = t.split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_file_output(raw: str, fallback: str) -> tuple[str, str]:
    """Split a `FILENAME: x\\n<content>` response into (name, content)."""
    text = raw.strip()
    name = fallback
    m = re.search(r"^\s*FILENAME:\s*(\S+)\s*$", text, re.IGNORECASE | re.MULTILINE)
    if m:
        name = m.group(1).strip().strip("`\"'")
        text = text[m.end() :].lstrip("\n")
    content = _strip_code_fences(text)
    return (name or "output.txt"), content


# --- Surgical SEARCH/REPLACE editing -------------------------------------

_EDIT_INSTRUCTIONS = """

## Edit mode — SEARCH/REPLACE
Change the file by emitting one or more edit blocks in EXACTLY this format:
<<<<<<< SEARCH
<lines copied verbatim from the current file>
=======
<the replacement lines>
>>>>>>> REPLACE

Rules:
- The SEARCH section MUST match text in the current file exactly — copy it character for character.
- Keep each block minimal: only the lines that change, plus a little surrounding context.
- Use a separate block for each distinct change.
- Output ONLY the blocks. No explanation, no prose, no markdown code fences.

Example — given this file:
def greet(name):
    return "hi"
and the request "make greet return hello", you output ONLY:
<<<<<<< SEARCH
    return "hi"
=======
    return "hello"
>>>>>>> REPLACE"""

_MULTIFILE_PLAN_INSTRUCTIONS = """
You are planning how to split or reorganize code across MULTIPLE files.
Return ONLY a JSON object, nothing else, in exactly this shape:
{"files": [
  {"filename": "<relative path>", "action": "create" | "edit", "instruction": "<what to put in / change about this file>"}
]}

Rules:
- "create" = a brand-new file. "edit" = modify a file that already exists.
- When you move code OUT of an existing file, you MUST include an "edit" entry
  for that existing file whose instruction says to REMOVE the moved code and add
  the link/import (e.g. for index.html: remove the inline <style>/<script> and
  add <link rel="stylesheet" href="styles.css"> and <script src="script.js">).
- Keep each instruction specific and self-contained.
- Output ONLY the JSON. No prose, no markdown fences."""

_SR_BLOCK_RE = re.compile(
    r"<{3,}\s*SEARCH\s*\n(.*?)\n={3,}\s*\n(.*?)\n>{3,}\s*REPLACE",
    re.DOTALL,
)


def _parse_search_replace(text: str) -> list[tuple[str, str]]:
    """Extract (search, replace) pairs from a model response."""
    return [(m.group(1), m.group(2)) for m in _SR_BLOCK_RE.finditer(text)]


def _leading_ws(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _apply_block_linewise(content: str, search: str, replace: str) -> str | None:
    """Whitespace-tolerant fallback matcher (small models mangle indentation).

    Tier 1: match ignoring trailing whitespace.
    Tier 2: match ignoring all leading/trailing whitespace, then re-indent the
    replacement to the file's indentation (3B models routinely drop the indent
    from the SEARCH lines they copy).
    """
    c_lines = content.split("\n")
    s_lines = search.split("\n")
    n = len(s_lines)
    if n == 0:
        return None

    cs = [x.rstrip() for x in c_lines]
    ss = [x.rstrip() for x in s_lines]
    for i in range(0, len(c_lines) - n + 1):
        if cs[i : i + n] == ss:
            return "\n".join(c_lines[:i] + replace.split("\n") + c_lines[i + n :])

    csf = [x.strip() for x in c_lines]
    ssf = [x.strip() for x in s_lines]
    for i in range(0, len(c_lines) - n + 1):
        if csf[i : i + n] == ssf:
            file_indent = _leading_ws(c_lines[i])
            search_indent = _leading_ws(s_lines[0])
            pad = (
                file_indent[: len(file_indent) - len(search_indent)]
                if file_indent.endswith(search_indent)
                else ""
            )
            r_lines = [(pad + rl if rl.strip() else rl) for rl in replace.split("\n")]
            return "\n".join(c_lines[:i] + r_lines + c_lines[i + n :])
    return None


def _apply_search_replace(
    content: str, blocks: list[tuple[str, str]]
) -> tuple[str, int, int]:
    """Apply SEARCH/REPLACE blocks. Returns (new_content, applied, failed)."""
    new = content
    applied = 0
    failed = 0
    for search, replace in blocks:
        if search and search in new:
            new = new.replace(search, replace, 1)
            applied += 1
            continue
        patched = _apply_block_linewise(new, search, replace) if search else None
        if patched is not None:
            new = patched
            applied += 1
        else:
            failed += 1
    return new, applied, failed


# --- Multi-file planning --------------------------------------------------


@dataclass(frozen=True)
class FileOp:
    """One planned per-file operation produced by the multi-file planner."""

    filename: str
    action: str  # "create" | "edit"
    instruction: str


def _parse_file_plan(raw: str) -> list[FileOp]:
    """Parse a planner response of {"files": [{filename, action, instruction}]}.

    Tolerant of prose around the JSON (reuses _extract_json). Entries without a
    filename are skipped; a missing/blank action defaults to "create".
    """
    try:
        data = _extract_json(raw)
    except Exception:
        return []
    items = data.get("files") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    ops: list[FileOp] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "").strip()
        if not name:
            continue
        action = str(item.get("action") or "create").strip().lower()
        if action not in ("create", "edit"):
            action = "create"
        ops.append(
            FileOp(
                filename=name,
                action=action,
                instruction=str(item.get("instruction") or "").strip(),
            )
        )
    return ops


class AgentCore:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        retriever: Retriever | None = None,
        pm: ProjectMemory | None = None,
        session_id: str = "default",
        mcp_manager=None,
        skill_loader=None,
    ) -> None:
        self.registry = registry or create_registry()
        self.retriever = retriever or _default_retriever
        self.pm = pm or project_memory
        self.memory = ConversationMemory(session_id=session_id)
        self.executor = Executor(self.registry)
        self.planner = Planner()
        # Tool loop uses native function calling (bind_tools) — plain mode, NOT
        # json_mode: format="json" would fight the tool-call output format.
        self._llm = get_llm(temperature=0.1, json_mode=False)
        self._llm_direct = get_llm(temperature=0.2, json_mode=False)
        self._llm_edit = get_llm(
            temperature=0.0, json_mode=False
        )  # format-strict edits
        self._llm_stream = get_streaming_llm(temperature=0.1)
        self._project_path: str | None = None
        self._skills_context: str = ""
        self.mcp_manager = mcp_manager
        self.skill_loader = skill_loader  # SkillLoader | None

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    async def load_project(self, project_path: str) -> dict[str, Any]:
        self._project_path = project_path
        index_stats = self.retriever.index_project(project_path)
        await self.pm.index_project(project_path)
        return index_stats

    def set_skills_context(self, skills_text: str) -> None:
        self._skills_context = skills_text

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    async def _build_messages(
        self,
        user_message: str,
        extra_context: str = "",
        include_tool_protocol: bool = True,
    ) -> list:
        parts: list[str] = [_load_system_prompt()]

        # Injected skill instructions
        if self._skills_context:
            parts.append(f"\n## Active Skills\n{self._skills_context}")

        # Project summary
        if self._project_path:
            proj_block = await self.pm.get_prompt_block(self._project_path)
            if proj_block:
                parts.append(f"\n{proj_block}")

        # RAG context
        if self._project_path and user_message.strip():
            try:
                results = self.retriever.query(
                    user_message, top_k=settings.retrieval_top_k
                )
                rag_ctx = self.retriever.format_context(results, max_tokens=1200)
                if rag_ctx:
                    parts.append(f"\n## Relevant Code\n{_truncate_context(rag_ctx)}")
            except Exception:
                pass

        if extra_context:
            parts.append(f"\n## Additional Context\n{extra_context}")

        # Tool-loop guidance (workdir + when to use tools; schemas come from bind_tools)
        if include_tool_protocol:
            workdir = self._project_path or str(Path.cwd())
            parts.append("\n" + _tool_guidance(workdir))

        system_text = "\n".join(parts)

        # Conversation history
        history = await self.memory.get_messages()
        msgs = [SystemMessage(content=system_text)]
        msgs.extend(history)
        msgs.append(HumanMessage(content=user_message))
        return msgs

    # ------------------------------------------------------------------
    # Tool-call loop (native function calling)
    # ------------------------------------------------------------------

    async def _run_tool_loop(
        self,
        messages: list,
        max_steps: int = 8,
    ) -> tuple[str, list[dict]]:
        """Async tool-call loop via native function calling.

        The model emits structured tool calls through ChatOllama.bind_tools —
        no hand-rolled JSON protocol, no output parsing/repair. A response
        without tool calls is the final answer. Returns (final_answer, trace).
        """
        tool_trace: list[dict] = []
        current_messages = list(messages)
        fail_counts: dict[str, int] = {}  # §11: bail out of doomed retries
        llm = self._llm.bind_tools(self.registry.to_openai_tools())

        for _step in range(max_steps):
            retries = 0
            response = None
            while retries <= settings.max_tool_retries:
                try:
                    response = llm.invoke(current_messages)
                    break
                except Exception as e:
                    retries += 1
                    if retries > settings.max_tool_retries:
                        return f"LLM error after retries: {e}", tool_trace

            tool_calls = list(getattr(response, "tool_calls", None) or [])
            if not tool_calls:
                return str(getattr(response, "content", "") or ""), tool_trace

            # The assistant message carrying the tool calls must precede the
            # ToolMessages that answer them.
            current_messages.append(response)
            give_up: str | None = None
            for call in tool_calls:
                tool_name = call.get("name", "")
                arguments = call.get("args") or {}
                call_id = call.get("id") or ""

                result = await self.executor.execute(tool_name, arguments)
                tool_trace.append(
                    {"tool": tool_name, "arguments": arguments, "result": result}
                )

                error = result.get("error") or ""
                if not result.get("success") and "Tool not found" in error:
                    # Model invented a tool name — correct it firmly instead of
                    # letting it retry the hallucination until max_steps.
                    valid = ", ".join(self.registry.names())
                    feedback = (
                        f"ERROR: '{tool_name}' is NOT a real tool and was not run. "
                        f"The only valid tools are: {valid}. "
                        f"If you do not need any of these, answer directly now."
                    )
                elif not result.get("success"):
                    # §11: structured recovery — one targeted hint, then bail
                    # out gracefully instead of retrying a doomed call.
                    fail_counts[tool_name] = fail_counts.get(tool_name, 0) + 1
                    if fail_counts[tool_name] >= settings.max_tool_failures:
                        category = classify_error(error)
                        give_up = (
                            f"Could not complete the request: tool '{tool_name}' "
                            f"failed repeatedly ({category}). "
                            f"Last error: {error[:200]}"
                        )
                    try:
                        hints = self.registry.get(tool_name).error_hints
                    except Exception:
                        hints = None
                    feedback = recovery_hint(tool_name, error, hints)
                else:
                    result_text = result.get("result") or error or "No output"
                    feedback = (
                        f"Tool result for {tool_name}:\n"
                        f"{_truncate_context(result_text, 2000)}"
                    )

                current_messages.append(
                    ToolMessage(content=feedback, tool_call_id=call_id)
                )

            if give_up:
                return give_up, tool_trace

        return "Reached maximum steps without a final answer.", tool_trace

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _update_skills_context(self, user_message: str) -> None:
        """Match skills to user message and update injection context."""
        if self.skill_loader is None:
            return
        try:
            from app.skills.matcher import build_skills_context, match_skills

            matched = match_skills(user_message, self.skill_loader)
            self._skills_context = build_skills_context(matched)
        except Exception:
            self._skills_context = ""

    def _resolve_ref(self, refs: list[str]) -> str | None:
        """Pick the @-referenced file to act on: first that exists, else the first given."""
        if not refs:
            return None
        workdir = Path(self._project_path or Path.cwd())
        for ref in refs:
            try:
                if (workdir / ref).is_file():
                    return ref
            except Exception:
                continue
        return refs[0]

    def _read_refs(self, refs: list[str], max_chars: int = 4000) -> str:
        """Read the @-referenced files into a context block for non-edit answers."""
        if not refs:
            return ""
        workdir = Path(self._project_path or Path.cwd())
        blocks: list[str] = []
        for ref in refs:
            p = workdir / ref
            try:
                if p.is_file():
                    body = p.read_text(encoding="utf-8", errors="replace")[:max_chars]
                    blocks.append(f"### {ref}\n{body}")
            except Exception:
                continue
        return "\n\n".join(blocks)

    async def _direct_answer(self, user_message: str, extra_context: str = "") -> str:
        """Single plain-language LLM call — no tool protocol, guaranteed prose/code."""
        messages = await self._build_messages(
            user_message, extra_context=extra_context, include_tool_protocol=False
        )
        try:
            response = self._llm_direct.invoke(messages)
            return response.content
        except Exception as e:
            return f"LLM error: {e}"

    async def _file_op_flow(
        self,
        user_message: str,
        target: str | None = None,
        extra_context: str = "",
    ) -> tuple[str, list[dict]]:
        """Deterministically create/update a single file on disk.

        Generates the full file with one plain (no-JSON) call — far more reliable
        and higher quality on a 3B model than the ReAct tool protocol — then writes
        it via the write_file tool. Files land in the loaded project, else cwd.
        ``target`` (e.g. from an @ reference) takes precedence over guessing the
        filename from the message text.
        """
        workdir = Path(self._project_path or Path.cwd())
        filename = target or _extract_filename(user_message)

        full_existing = ""
        target_path: Path | None = None
        if filename:
            target_path = workdir / filename
            try:
                if target_path.is_file():
                    full_existing = target_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
            except Exception:
                full_existing = ""

        # Editing an existing file → try a surgical SEARCH/REPLACE edit first.
        if full_existing and target_path is not None:
            edited = await self._surgical_edit(
                filename,
                target_path,
                full_existing,
                user_message,
                extra_context=extra_context,
            )
            if edited is not None:
                return edited
            # else: blocks didn't parse/match → fall through to whole-file rewrite.

        # Create (or whole-file rewrite fallback) via FILENAME: full-content generation.
        sys_parts = [_load_system_prompt()]
        if self._skills_context:
            sys_parts.append(f"\n## Active Skills\n{self._skills_context}")
        sys_parts.append(_FILE_GEN_INSTRUCTIONS)

        ctx = f"User request: {user_message}\n\nWorking directory: {workdir}"
        guard = _extension_guard(filename) if filename else ""
        if guard:
            ctx += f"\n\nIMPORTANT: {guard}"
        if extra_context:
            ctx += f"\n\n{extra_context}"
        if full_existing:
            ctx += (
                f"\n\nThe file '{filename}' already exists. Apply the requested change "
                f"and return the COMPLETE updated file:\n\n{full_existing[:4000]}"
            )

        messages = [
            SystemMessage(content="\n".join(sys_parts)),
            HumanMessage(content=ctx),
        ]
        try:
            raw = self._llm_direct.invoke(messages).content
        except Exception as e:
            return f"LLM error while generating the file: {e}", []

        name, content = _parse_file_output(
            raw, fallback=filename or _infer_filename(user_message)
        )
        out_path = workdir / name
        result = await self.executor.execute(
            "write_file", {"path": str(out_path), "content": content}
        )
        trace = [
            {
                "tool": "write_file",
                "arguments": {"path": str(out_path)},
                "result": result,
            }
        ]

        if result.get("success"):
            verb = "Updated" if full_existing else "Created"
            answer = f"{verb} `{name}` ({len(content)} bytes) in {workdir}"
            note, extra = await self._verify_and_repair(out_path, name)
            trace.extend(extra)
            if note:
                answer += f" — {note}"
        else:
            answer = f"Failed to write {name}: {result.get('error')}"
        return answer, trace

    async def _surgical_edit(
        self,
        filename: str,
        target_path: Path,
        full_content: str,
        user_message: str,
        extra_context: str = "",
    ) -> tuple[str, list[dict]] | None:
        """Edit an existing file via SEARCH/REPLACE blocks.

        Returns (answer, trace) on success, or None to signal the caller should
        fall back to a whole-file rewrite (no blocks parsed, or none matched).
        """
        # Deliberately NOT the full persona prompt — its "confirm what you did"
        # rule pushes the model toward prose. Keep it a strict editing engine.
        sys_parts = ["You are a precise code-editing engine. You output only edits."]
        if self._skills_context:
            sys_parts.append(f"\n## Active Skills\n{self._skills_context}")
        sys_parts.append(_EDIT_INSTRUCTIONS)

        guard = _extension_guard(filename)
        guard_line = f"IMPORTANT: {guard}\n\n" if guard else ""
        extra_block = f"{extra_context}\n\n" if extra_context else ""
        ctx = (
            f"File: {filename}\nCurrent content:\n{full_content[:6000]}\n\n"
            f"{extra_block}"
            f"{guard_line}"
            f"Request: {user_message}\n\n"
            f"Output the SEARCH/REPLACE block(s) now:"
        )
        messages = [
            SystemMessage(content="\n".join(sys_parts)),
            HumanMessage(content=ctx),
        ]
        try:
            raw = self._llm_edit.invoke(messages).content
        except Exception:
            return None

        blocks = _parse_search_replace(raw)
        if not blocks:
            # One firm retry before giving up and falling back to a rewrite.
            messages.append(
                HumanMessage(
                    content=(
                        "You did not output a SEARCH/REPLACE block. Output ONLY one or "
                        "more blocks in the exact <<<<<<< SEARCH / ======= / >>>>>>> "
                        "REPLACE format. No prose, no code fences."
                    )
                )
            )
            try:
                raw = self._llm_edit.invoke(messages).content
            except Exception:
                return None
            blocks = _parse_search_replace(raw)
            if not blocks:
                return None

        new_content, applied, failed = _apply_search_replace(full_content, blocks)
        if applied == 0:
            return None  # nothing matched → let caller rewrite the whole file

        result = await self.executor.execute(
            "write_file", {"path": str(target_path), "content": new_content}
        )
        trace = [
            {
                "tool": "write_file",
                "arguments": {"path": str(target_path)},
                "result": result,
            }
        ]
        if result.get("success"):
            answer = f"Edited `{filename}`: {applied} change(s) applied"
            if failed:
                answer += f" ({failed} block(s) didn't match the file)"
            note, extra = await self._verify_and_repair(target_path, filename)
            trace.extend(extra)
            if note:
                answer += f" — {note}"
        else:
            answer = f"Failed to write {filename}: {result.get('error')}"
        return answer, trace

    async def _verify_and_repair(
        self, target_path: Path, filename: str
    ) -> tuple[str, list[dict]]:
        """Syntax-check a just-written file; feed failures back for repair.

        Roadmap Tier 1 #1: generate → check → send the error back → the model
        returns the complete corrected file → re-check, capped at
        settings.max_repair_attempts. Returns (status_note, extra_trace); the
        note is "" for file types check_file cannot validate on this machine.
        """
        if not is_verifiable(target_path):
            return "", []
        ok, error = check_file(target_path)
        if ok:
            return "verified OK", []

        trace: list[dict] = []
        guard = _extension_guard(filename)
        for attempt in range(1, settings.max_repair_attempts + 1):
            content = target_path.read_text(encoding="utf-8", errors="replace")
            ctx = (
                f"The file '{filename}' was just written but FAILED its syntax check.\n\n"
                f"Check error:\n{error}\n\n"
                f"Current content:\n{content[:6000]}\n\n"
                + (f"IMPORTANT: {guard}\n\n" if guard else "")
                + "Fix the error and return the COMPLETE corrected file."
            )
            messages = [
                SystemMessage(
                    content="You are a code-repair engine. You fix files so they "
                    "parse cleanly, changing as little as possible."
                    + _FILE_GEN_INSTRUCTIONS
                ),
                HumanMessage(content=ctx),
            ]
            try:
                raw = self._llm_direct.invoke(messages).content
            except Exception as e:
                return (
                    f"verification failed ({error[:120]}); repair LLM error: {e}",
                    trace,
                )
            _, fixed = _parse_file_output(raw, fallback=filename)
            result = await self.executor.execute(
                "write_file", {"path": str(target_path), "content": fixed}
            )
            trace.append(
                {
                    "tool": "write_file",
                    "arguments": {"path": str(target_path)},
                    "result": result,
                }
            )
            if not result.get("success"):
                return (
                    f"verification failed ({error[:120]}); repair write failed: "
                    f"{result.get('error')}",
                    trace,
                )
            ok, error = check_file(target_path)
            if ok:
                return f"auto-repaired after {attempt} attempt(s)", trace
        return (
            f"verification failed after {settings.max_repair_attempts} repair "
            f"attempt(s): {error[:200]}",
            trace,
        )

    async def _plan_file_ops(self, user_message: str, context: str) -> list[FileOp]:
        """One LLM call → an ordered list of per-file operations.

        ``context`` is the text of the existing files relevant to the request
        (so the planner knows what to split out). Returns [] on any failure;
        the caller falls back to the single-file flow.
        """
        messages = [
            SystemMessage(
                content="You are a precise multi-file refactoring planner. "
                "You output only JSON." + _MULTIFILE_PLAN_INSTRUCTIONS
            ),
            HumanMessage(
                content=(
                    f"Request: {user_message}\n\n"
                    f"Existing files:\n{context or '(none)'}\n\n"
                    f"Output the JSON plan now:"
                )
            ),
        ]
        try:
            raw = self._llm_direct.invoke(messages).content
        except Exception:
            return []
        return _parse_file_plan(raw)

    async def _multi_file_flow(
        self, user_message: str, refs: list[str]
    ) -> tuple[str, list[dict]]:
        """Plan a set of per-file operations, then run each through _file_op_flow.

        Reads the existing files relevant to the request (the @refs plus any
        file named in the message that exists on disk) so the planner can decide
        what to split out, then executes create/edit for each planned file by
        delegating to the already-tested single-file flow.
        """
        workdir = Path(self._project_path or Path.cwd())

        # Gather context: @refs first, then any existing filename mentioned in text.
        ctx_names: list[str] = list(refs)
        guessed = _extract_filename(user_message)
        if guessed and guessed not in ctx_names:
            ctx_names.append(guessed)
        context = self._read_refs([n for n in ctx_names if (workdir / n).is_file()])

        ops = await self._plan_file_ops(user_message, context)
        if not ops:
            return (
                "I couldn't plan the multi-file change — try naming the files, "
                "e.g. 'split index.html into styles.css and script.js'.",
                [],
            )

        # Cross-file consistency (Tier 1 #3): every per-file call sees the full
        # plan (so filenames/links agree even before siblings exist), and each
        # subsequent call additionally sees the already-written siblings.
        manifest = (
            "## Multi-file plan\n"
            "All files below are part of ONE change and must be consistent with "
            "each other (matching filenames, links/imports, class/function/id "
            "names):\n"
            + "\n".join(
                f"- {op.filename} ({op.action}): {op.instruction or '(as requested)'}"
                for op in ops
            )
        )

        trace: list[dict] = []
        summaries: list[str] = []
        written: list[str] = []
        for op in ops:
            # Each op reuses the single-file flow: create → FILENAME gen,
            # edit on an existing file → surgical SEARCH/REPLACE then rewrite.
            sub_msg = op.instruction or user_message
            extra = manifest
            siblings = self._read_refs(written, max_chars=2500)
            if siblings:
                extra += (
                    "\n\n## Already-written files in this change\n"
                    "Make every reference to them (paths, selectors, ids, "
                    "function names) match EXACTLY:\n\n" + siblings
                )
            ans, sub_trace = await self._file_op_flow(
                sub_msg, target=op.filename, extra_context=extra
            )
            trace.extend(sub_trace)
            summaries.append(f"- {op.filename}: {ans}")
            written.append(op.filename)

        answer = f"Handled {len(ops)} file(s):\n" + "\n".join(summaries)
        return answer, trace

    async def chat(self, user_message: str) -> tuple[str, list[dict]]:
        """Process one user message. Returns (answer, tool_trace)."""
        # @path references: pull them out, then work with a cleaned message so the
        # classifier/model see plain paths rather than "@foo".
        at_refs = _extract_at_refs(user_message)
        clean_message = _strip_at_refs(user_message)

        self._update_skills_context(clean_message)
        await self.memory.add_human(user_message)

        task_type = self.planner.classify(clean_message)

        if wants_multifile(clean_message):
            # Plan + execute several file operations in one turn.
            answer, trace = await self._multi_file_flow(clean_message, refs=at_refs)
        elif _wants_file_op(clean_message) or task_type == "file_edit":
            # Create/update a single file deterministically; an @ref pins the target.
            target = self._resolve_ref(at_refs)
            answer, trace = await self._file_op_flow(clean_message, target=target)
        elif task_type == "multi_step" and self._project_path is not None:
            # Genuine multi-step work in a loaded project → ReAct tool loop.
            messages = await self._build_messages(clean_message)
            answer, trace = await self._run_tool_loop(messages)
        else:
            # Plain answer; inject any @-referenced files as context.
            answer = await self._direct_answer(
                clean_message, extra_context=self._read_refs(at_refs)
            )
            trace = []

        await self.memory.add_ai(answer)
        return answer, trace

    async def stream_chat(self, user_message: str) -> AsyncIterator[str]:
        """Stream final answer tokens for simple (non-tool) responses."""
        await self.memory.add_human(user_message)
        messages = await self._build_messages(user_message)

        answer, trace = await self._run_tool_loop(messages)
        await self.memory.add_ai(answer)

        # Stream the final answer token by token (word-level simulation)
        for word in answer.split(" "):
            yield word + " "

    def get_plan(self, user_message: str) -> dict:
        """Return the planner's task plan without executing it."""
        return self.planner.plan(user_message)

    async def clear_memory(self) -> None:
        await self.memory.clear_all(delete_db=False)
