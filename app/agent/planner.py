import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.models.llm import get_llm
from config.settings import settings

TASK_TYPES = ("simple_qa", "code_generation", "file_edit", "multi_step", "explanation")

_CLASSIFY_PROMPT = """Classify the user task into exactly one of these categories:
- simple_qa: a factual or conversational question with no code required
- explanation: explain existing code or a concept
- code_generation: write new code from scratch
- file_edit: modify existing code files
- multi_step: requires multiple distinct operations (read + edit + run, etc.)

Reply with ONLY valid JSON: {"task_type": "<one of the five types>"}"""

_PLAN_PROMPT = """You are a task planner for a coding assistant. Break the user task into ordered steps.

Reply with ONLY valid JSON matching this schema exactly:
{
  "task_type": "multi_step",
  "steps": [
    {
      "step_description": "string describing what to do",
      "suggested_tool": "tool_name or null",
      "expected_output": "what this step produces"
    }
  ]
}

Available tools: read_file, write_file, edit_file, create_file, delete_file,
list_directory, search_files, run_command, git_status, git_diff, git_commit, git_log"""

# Decomposition prompt (M1 robust pass): turn ONE natural-language request into
# an ordered list of concrete build steps. Unlike _PLAN_PROMPT this is tuned for
# multi-file feature builds — it names the file per step and insists later steps
# stay consistent with earlier ones (shared stylesheet, matching links/ids).
_DECOMPOSE_PROMPT = """You break ONE coding request into an ordered list of concrete build steps.

Reply with ONLY valid JSON in exactly this shape:
{"steps": [{"step_description": "..."}]}

Rules:
- Each step is ONE file to create or edit, or ONE command to run, in the order
  it should happen. Order steps so shared files (e.g. a stylesheet or a JS
  helper) and any file another file links to are usable when they are needed.
- When several files are involved, give each its OWN step and NAME the file in
  the description, e.g. "Create login.html: ..." / "Create styles.css: ...".
- Keep the steps consistent with each other: reuse the SAME file names across
  steps; if pages share styling, put it in one CSS file and have each page link
  that exact file; if pages navigate to each other or a button redirects,
  reference the exact target file name.
- Any stylesheet or script an HTML file references (e.g. script.js, styles.css)
  MUST have its own "Create <file>" step so the file actually exists — never
  reference a file you did not also create.
- Put page behavior (form validation, redirects, button handlers) in ONE shared
  script file and give it a create step; do not leave logic only described.
- If editing an existing file would be needed so another file links to it,
  include that edit as its own step.
- If the whole request is genuinely a single file or a single action, return
  exactly ONE step.
- Output ONLY the JSON. No prose, no markdown fences."""


def _extract_json(text: str) -> dict:
    """Extract the first JSON object from text, tolerating markdown fences."""
    text = text.strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    # Find first {...}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON object found in LLM output: {text[:200]!r}")


class Planner:
    def __init__(self) -> None:
        self._llm_fast = get_llm(temperature=0.1, json_mode=True)
        self._llm_plan = get_llm(temperature=0.3, json_mode=True)

    def classify(self, user_message: str) -> str:
        """Return one of the five task type strings."""
        try:
            messages = [
                SystemMessage(content=_CLASSIFY_PROMPT),
                HumanMessage(content=user_message),
            ]
            response = self._llm_fast.invoke(messages)
            data = _extract_json(response.content)
            task_type = data.get("task_type", "simple_qa")
            return task_type if task_type in TASK_TYPES else "simple_qa"
        except Exception:
            return "simple_qa"

    def decompose(self, user_message: str) -> list[str]:
        """Break a multi-step request into ordered step descriptions (M1).

        Runs ONLY the planning LLM call — the caller has already decided this is
        multi_step, so classify() is skipped. Returns the ordered
        step_description strings, or [] when it can't produce 2+ steps (so the
        caller falls back to routing the whole message as one task).
        """
        try:
            messages = [
                SystemMessage(content=_DECOMPOSE_PROMPT),
                HumanMessage(content=f"Request: {user_message}"),
            ]
            data = _extract_json(self._llm_plan.invoke(messages).content)
            steps = data.get("steps") if isinstance(data, dict) else None
            if not isinstance(steps, list):
                return []
            out = [
                str(s.get("step_description") or "").strip()
                for s in steps
                if isinstance(s, dict) and str(s.get("step_description") or "").strip()
            ]
            return out if len(out) >= 2 else []
        except Exception:
            return []

    def plan(self, user_message: str) -> dict[str, Any]:
        """Return a full plan dict. For non-multi_step tasks returns minimal plan."""
        task_type = self.classify(user_message)

        if task_type != "multi_step":
            return {
                "task_type": task_type,
                "steps": [
                    {
                        "step_description": user_message,
                        "suggested_tool": None,
                        "expected_output": "LLM response",
                    }
                ],
            }

        try:
            messages = [
                SystemMessage(content=_PLAN_PROMPT),
                HumanMessage(content=f"Task: {user_message}"),
            ]
            response = self._llm_plan.invoke(messages)
            data = _extract_json(response.content)
            # Validate structure
            if "steps" not in data:
                raise ValueError("Missing 'steps' in plan")
            data["task_type"] = "multi_step"
            return data
        except Exception as e:
            # Fallback: single-step plan
            return {
                "task_type": "multi_step",
                "steps": [
                    {
                        "step_description": user_message,
                        "suggested_tool": None,
                        "expected_output": "LLM response",
                    }
                ],
                "_plan_error": str(e),
            }
