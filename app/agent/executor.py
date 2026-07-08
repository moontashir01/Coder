import asyncio
import inspect
from typing import Any, Awaitable, Callable

from app.agent.tool_registry import ToolRegistry
from config.settings import settings

# An approval hook decides whether a gated tool call may run. It receives the
# tool name, its arguments, and its permission tags, and returns True to allow.
ApprovalHook = Callable[[str, dict[str, Any], list[str]], Awaitable[bool]]


def _validate_args(parameters: dict, arguments: dict) -> str | None:
    required = parameters.get("required", [])
    props = parameters.get("properties", {})

    missing = [k for k in required if k not in arguments]
    if missing:
        return f"Missing required arguments: {missing}"

    type_map = {"string": str, "integer": int, "boolean": bool, "number": (int, float)}
    for key, val in arguments.items():
        if key in props:
            expected = props[key].get("type")
            if expected and expected in type_map:
                if not isinstance(val, type_map[expected]):
                    return f"Argument {key!r}: expected {expected}, got {type(val).__name__}"
    return None


class Executor:
    def __init__(
        self, registry: ToolRegistry, approval_hook: ApprovalHook | None = None
    ) -> None:
        self._registry = registry
        self._approval_hook = approval_hook

    def set_approval_hook(self, hook: ApprovalHook | None) -> None:
        """Install (or clear) the interactive approval hook (Step 6 / S3)."""
        self._approval_hook = hook

    def _needs_approval(self, tool) -> bool:
        if settings.auto_approve:
            return False
        return bool(set(tool.permissions) & set(settings.approval_gated_permissions))

    async def _approved(self, tool, tool_name: str, arguments: dict) -> bool:
        """Decide whether a gated tool may run.

        An interactive hook (installed by the REPL) gets the final say. With no
        hook — tests, piped input, eval runs — allow by default, EXCEPT under
        --safe, which denies safe_deny_permissions so a non-interactive session
        can't silently run shell/deletes.
        """
        if self._approval_hook is not None:
            try:
                return bool(
                    await self._approval_hook(
                        tool_name, arguments, sorted(tool.permissions)
                    )
                )
            except Exception:
                return False
        if settings.safe_mode and (
            set(tool.permissions) & set(settings.safe_deny_permissions)
        ):
            return False
        return True

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Look up, validate, and call a tool. Supports sync and async handlers."""
        try:
            tool = self._registry.get(tool_name)
        except KeyError as e:
            return {"success": False, "result": "", "error": str(e)}

        # Permission gating (Tier 3 #8): refuse before touching arguments.
        denied = sorted(set(tool.permissions) & set(settings.denied_permissions))
        if denied:
            return {
                "success": False,
                "result": "",
                "error": (
                    f"Permission denied: tool '{tool_name}' requires {denied}, "
                    "blocked by settings.denied_permissions"
                ),
            }

        error = _validate_args(tool.parameters, arguments)
        if error:
            return {"success": False, "result": "", "error": f"Argument validation failed: {error}"}

        # Human-in-the-loop approval (Step 6 / S3, S6): consult the hook before
        # running any mutating/shell tool.
        if self._needs_approval(tool) and not await self._approved(
            tool, tool_name, arguments
        ):
            return {
                "success": False,
                "result": "",
                "error": f"Denied: '{tool_name}' was not approved by the user.",
            }

        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await tool.handler(**arguments)
            else:
                # Run sync handlers in a thread pool to avoid blocking the event loop
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: tool.handler(**arguments)
                )
            if not isinstance(result, dict):
                result = {"success": True, "result": str(result), "error": None}
            return result
        except TypeError as e:
            return {"success": False, "result": "", "error": f"Tool call error: {e}"}
        except Exception as e:
            return {"success": False, "result": "", "error": f"Tool execution error: {e}"}
