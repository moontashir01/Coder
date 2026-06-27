import json
import re
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.executor import Executor
from app.agent.planner import Planner, _extract_json
from app.agent.recovery import classify_error, recovery_hint
from app.agent.tool_registry import ToolRegistry, create_registry
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


def _tool_loop_prompt(tool_names: list[str], workdir: str) -> str:
    names = ", ".join(tool_names) if tool_names else "(none)"
    return f"""You can either answer directly or call ONE tool. Output ONLY valid JSON, nothing else.

To call a tool, use this EXACT shape — the tool name goes in "tool", parameters go inside "arguments":
{{"action": "tool_call", "tool": "write_file", "arguments": {{"path": "index.html", "content": "<full file text>"}}}}

To answer (only when no file/command access is needed):
{{"action": "final_answer", "answer": "<your full response, INCLUDING the actual code>"}}

VALID TOOL NAMES — you may ONLY use these, never invent others:
{names}

Working directory: {workdir}
When asked to create or save a file, call write_file (or create_file) with a relative path
like "index.html" — the file is saved in the working directory above.

Rules:
- "action" is ALWAYS the literal string "tool_call" or "final_answer" — never a tool name.
- If the task asks you to create, save, or write a file, you MUST call write_file/create_file. Do not just print the code.
- Never call a tool name that is not in the list above."""


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
        self._llm = get_llm(temperature=0.1, json_mode=True)
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

        # Tool loop instruction (lists the real registered tool names + workdir)
        if include_tool_protocol:
            workdir = self._project_path or str(Path.cwd())
            parts.append("\n" + _tool_loop_prompt(self.registry.names(), workdir))

        system_text = "\n".join(parts)

        # Conversation history
        history = await self.memory.get_messages()
        msgs = [SystemMessage(content=system_text)]
        msgs.extend(history)
        msgs.append(HumanMessage(content=user_message))
        return msgs

    # ------------------------------------------------------------------
    # Tool-call loop (ReAct-style)
    # ------------------------------------------------------------------

    def _parse_action(self, text: str) -> dict | None:
        try:
            return _extract_json(text)
        except Exception:
            return None

    def _normalize_action(self, action: dict) -> dict:
        """Coerce the loose JSON shapes a small model emits into the canonical form.

        Handles e.g. a flattened tool call where the model puts the tool name in
        "action" and the arguments at the top level:
            {"action": "write_file", "path": "x", "content": "..."}
        → {"action": "tool_call", "tool": "write_file", "arguments": {...}}
        """
        if not isinstance(action, dict):
            return action
        act = action.get("action")

        if act in ("tool_call", "final_answer"):
            return action

        valid_tools = set(self.registry.names())

        # Flattened: tool name lives in "action"
        if isinstance(act, str) and act in valid_tools:
            if isinstance(action.get("arguments"), dict):
                args = action["arguments"]
            else:
                args = {k: v for k, v in action.items() if k != "action"}
            return {"action": "tool_call", "tool": act, "arguments": args}

        # No "action" but a recognizable tool field
        tool = action.get("tool")
        if isinstance(tool, str) and tool in valid_tools:
            args = action.get("arguments")
            if not isinstance(args, dict):
                args = {k: v for k, v in action.items() if k not in ("tool", "action")}
            return {"action": "tool_call", "tool": tool, "arguments": args}

        # A bare answer
        if "answer" in action:
            return {"action": "final_answer", "answer": action["answer"]}

        return action

    def _coerce_args(self, tool_name: str, args: dict) -> dict:
        """Map common argument-name synonyms onto the tool's real schema (e.g. file_path → path)."""
        if not isinstance(args, dict):
            return {}
        try:
            props = self.registry.get(tool_name).parameters.get("properties", {})
        except Exception:
            return args
        if "path" in props and "path" not in args:
            for alt in ("file_path", "filepath", "filename", "file"):
                if alt in args:
                    args = dict(args)
                    args["path"] = args.pop(alt)
                    break
        return args

    async def _run_tool_loop(
        self,
        messages: list,
        max_steps: int = 8,
    ) -> tuple[str, list[dict]]:
        """Async tool-call loop. Returns (final_answer, tool_trace)."""
        tool_trace: list[dict] = []
        current_messages = list(messages)
        fail_counts: dict[str, int] = {}  # §11: bail out of doomed retries

        for step in range(max_steps):
            retries = 0
            raw = ""
            while retries <= settings.max_tool_retries:
                try:
                    response = self._llm.invoke(current_messages)
                    raw = response.content
                    break
                except Exception as e:
                    retries += 1
                    if retries > settings.max_tool_retries:
                        return f"LLM error after retries: {e}", tool_trace

            action = self._parse_action(raw)
            if action is not None:
                action = self._normalize_action(action)

            if action is None:
                retries_left = settings.max_tool_retries - step
                if retries_left <= 0:
                    return f"Could not parse LLM output: {raw[:200]}", tool_trace
                current_messages.append(
                    HumanMessage(
                        content=f"ERROR: Your output was not valid JSON. Try again.\nYour output: {raw[:200]}"
                    )
                )
                continue

            if action.get("action") == "final_answer":
                return action.get("answer", raw), tool_trace

            if action.get("action") == "tool_call":
                tool_name = action.get("tool", "")
                arguments = self._coerce_args(tool_name, action.get("arguments", {}))

                result = await self.executor.execute(tool_name, arguments)
                tool_trace.append(
                    {"tool": tool_name, "arguments": arguments, "result": result}
                )

                error = result.get("error") or ""
                # Small models invent tool names — correct them firmly instead of
                # letting them retry the same hallucinated tool until max_steps.
                if not result.get("success") and "Tool not found" in error:
                    valid = ", ".join(self.registry.names())
                    current_messages.append(
                        HumanMessage(
                            content=(
                                f"ERROR: '{tool_name}' is NOT a real tool and was not run. "
                                f"The only valid tools are: {valid}. "
                                f"If you do not need any of these, respond NOW with "
                                f'{{"action": "final_answer", "answer": "<the complete code>"}}.'
                            )
                        )
                    )
                    continue

                # §11: structured recovery for real tool failures. Give one
                # targeted hint, then bail out gracefully instead of letting the
                # model retry a doomed call until max_steps.
                if not result.get("success"):
                    fail_counts[tool_name] = fail_counts.get(tool_name, 0) + 1
                    if fail_counts[tool_name] >= settings.max_tool_failures:
                        category = classify_error(error)
                        return (
                            f"Could not complete the request: tool '{tool_name}' failed "
                            f"repeatedly ({category}). Last error: {error[:200]}"
                        ), tool_trace
                    try:
                        hints = self.registry.get(tool_name).error_hints
                    except Exception:
                        hints = None
                    current_messages.append(
                        HumanMessage(content=recovery_hint(tool_name, error, hints))
                    )
                    continue

                result_text = result.get("result") or error or "No output"
                current_messages.append(
                    HumanMessage(
                        content=f"Tool result for {tool_name}:\n{_truncate_context(result_text, 2000)}"
                    )
                )
                continue

            return raw, tool_trace

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
        self, user_message: str, target: str | None = None
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
                filename, target_path, full_existing, user_message
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
        else:
            answer = f"Failed to write {name}: {result.get('error')}"
        return answer, trace

    async def _surgical_edit(
        self,
        filename: str,
        target_path: Path,
        full_content: str,
        user_message: str,
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
        ctx = (
            f"File: {filename}\nCurrent content:\n{full_content[:6000]}\n\n"
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
        else:
            answer = f"Failed to write {filename}: {result.get('error')}"
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

        if _wants_file_op(clean_message) or task_type == "file_edit":
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
