import asyncio
import inspect
from typing import Any

from app.agent.tool_registry import ToolRegistry
from config.settings import settings


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
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

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
