"""MCP client — stdio transport, persistent session per server."""
from __future__ import annotations

import asyncio
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import Tool as MCPTool

from app.agent.tool_registry import ToolDefinition, ToolRegistry


class MCPServerConnection:
    """Holds a live stdio MCP session for one server."""

    def __init__(self, config: dict) -> None:
        self.name: str = config["name"]
        self.config = config
        self.session: ClientSession | None = None
        self.tools: list[MCPTool] = []
        self.connected: bool = False

        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._error: str | None = None

    async def _run(self) -> None:
        """Background task — keeps the stdio connection alive."""
        params = StdioServerParameters(
            command=self.config["command"],
            args=self.config.get("args", []),
            env=self.config.get("env") or None,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    self.tools = result.tools
                    self.session = session
                    self.connected = True
                    self._ready.set()
                    await self._stop.wait()   # block until disconnect()
        except Exception as e:
            self._error = str(e)
            self._ready.set()   # unblock waiters even on failure
        finally:
            self.connected = False
            self.session = None

    async def connect(self, timeout: float = 15.0) -> None:
        self._stop.clear()
        self._ready.clear()
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._stop.set()
            raise RuntimeError(f"MCP server '{self.name}' did not respond within {timeout}s")
        if self._error:
            raise RuntimeError(f"MCP server '{self.name}' error: {self._error}")

    async def disconnect(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self.connected = False
        self.session = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.session or not self.connected:
            return {"success": False, "result": "", "error": f"MCP server '{self.name}' not connected"}
        try:
            result = await self.session.call_tool(tool_name, arguments)
            # Flatten content list to string
            text_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    text_parts.append(item.text)
                else:
                    text_parts.append(str(item))
            return {"success": True, "result": "\n".join(text_parts), "error": None}
        except Exception as e:
            return {"success": False, "result": "", "error": str(e)}

    def make_tool_definitions(self) -> list[ToolDefinition]:
        """Wrap MCP tools as ToolDefinitions for the registry."""
        defs: list[ToolDefinition] = []
        conn = self   # capture for closure

        for mcp_tool in self.tools:
            tool_name = mcp_tool.name
            # Build a closure per tool to capture tool_name
            def _make_handler(name: str, connection: MCPServerConnection):
                async def handler(**kwargs: Any) -> dict[str, Any]:
                    return await connection.call_tool(name, kwargs)
                return handler

            params = {}
            if mcp_tool.inputSchema:
                params = dict(mcp_tool.inputSchema)

            defs.append(ToolDefinition(
                name=tool_name,
                description=mcp_tool.description or f"MCP tool: {tool_name}",
                parameters=params or {"type": "object", "properties": {}},
                source=f"mcp:{self.name}",
                handler=_make_handler(tool_name, conn),
                # Deniable as a class via settings.denied_permissions
                permissions=["mcp"],
            ))

        return defs
