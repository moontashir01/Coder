import asyncio
import inspect
from typing import Any, Awaitable, Callable

from app.agent.scope import effective_denied_permissions
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

    @property
    def approval_hook(self) -> ApprovalHook | None:
        """The installed hook, so a caller can RESTORE it rather than clear it.

        T4: in the embedded mode the REPL and the bot share this executor. A bot
        turn installs its own keyboard hook and must put back what it found —
        clearing it would leave the terminal with NO hook, and no hook means the
        executor's default policy, which is allow. A front-end quietly losing
        its approval prompt is the worst possible way for this to fail.
        """
        return self._approval_hook

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

    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Look up, validate, and call a tool. Supports sync and async handlers."""
        try:
            tool = self._registry.get(tool_name)
        except KeyError as e:
            return {"success": False, "result": "", "error": str(e)}

        # Permission gating (Tier 3 #8): refuse before touching arguments.
        # T3: the deny list comes from `scope.effective_denied_permissions()`,
        # which unions the process setting with THIS turn's caller (a bot
        # `viewer` denies fs:write/fs:delete/shell). Read off the setting alone,
        # two overlapping turns would share one deny list and whichever started
        # last would decide what both may do. A scope may only ever add.
        denied = sorted(set(tool.permissions) & set(effective_denied_permissions()))
        if denied:
            return {
                "success": False,
                "result": "",
                "error": (
                    f"Permission denied: tool '{tool_name}' requires {denied}, "
                    "blocked by the caller's denied permissions"
                ),
            }

        error = _validate_args(tool.parameters, arguments)
        if error:
            return {
                "success": False,
                "result": "",
                "error": f"Argument validation failed: {error}",
            }

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
            return {
                "success": False,
                "result": "",
                "error": f"Tool execution error: {e}",
            }
