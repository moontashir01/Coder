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
                SystemMessage(content=_PLAN_PROMPT),
                HumanMessage(content=f"Task: {user_message}"),
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
