"""Structured tool-failure recovery (§11).

Pure functions: classify a tool error string into a category, and turn it into
an actionable correction the small model can act on. The tool loop uses these
to give one targeted retry hint, then bail out gracefully instead of looping a
hallucinated/doomed call until max_steps.
"""

from __future__ import annotations

# category -> guidance fed back to the model
_CATEGORY_HINTS: dict[str, str] = {
    "not_found_tool": "that tool does not exist. Use ONLY the listed tools, or answer directly.",
    "invalid_args": "the arguments were wrong. Re-read the tool's required parameters and call it again with correct values.",
    "file_not_found": "the file does not exist. Verify the path with list_directory, or create it first.",
    "permission_denied": "permission was denied. Do NOT retry — report this limitation to the user.",
    "timeout": "the operation timed out. Try a smaller/faster command, or report the slowness.",
    "unknown": "the tool failed. Inspect the error, fix your arguments, and try once more — or answer directly if the tool is not essential.",
}


def classify_error(error: str) -> str:
    """Map a tool error message to a recovery category."""
    e = (error or "").lower()
    if "tool not found" in e:
        return "not_found_tool"
    if (
        "validation failed" in e
        or "missing required" in e
        or ("expected" in e and "got" in e)
    ):
        return "invalid_args"
    if (
        "no such file" in e
        or "errno 2" in e
        or "file not found" in e
        or "filenotfounderror" in e
    ):
        return "file_not_found"
    if "permission denied" in e or "errno 13" in e or "permissionerror" in e:
        return "permission_denied"
    if "timed out" in e or "timeout" in e:
        return "timeout"
    return "unknown"


def recovery_hint(
    tool_name: str, error: str, tool_error_hints: str | None = None
) -> str:
    """Build a corrective message for the model after a tool failure."""
    category = classify_error(error)
    guidance = _CATEGORY_HINTS.get(category, _CATEGORY_HINTS["unknown"])
    msg = (
        f"Tool '{tool_name}' failed ({category}): {error[:200]}\n"
        f"Guidance: {guidance}"
    )
    if tool_error_hints:
        msg += f"\nTool-specific hint: {tool_error_hints}"
    return msg
