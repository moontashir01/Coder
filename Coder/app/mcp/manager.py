"""MCP server lifecycle manager."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.tool_registry import ToolRegistry
from app.mcp.client import MCPServerConnection
from config.settings import settings


class MCPManager:
    def __init__(self) -> None:
        self._connections: dict[str, MCPServerConnection] = {}

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    async def connect_server(
        self, config: dict, registry: ToolRegistry, retry: bool = True
    ) -> MCPServerConnection:
        """Connect to an MCP server and register its tools."""
        name = config["name"]

        # Disconnect existing connection with same name
        if name in self._connections:
            await self.disconnect_server(name, registry)

        conn = MCPServerConnection(config)
        try:
            await conn.connect()
        except RuntimeError as e:
            if retry:
                # Retry once
                conn = MCPServerConnection(config)
                await conn.connect()
            else:
                raise

        self._connections[name] = conn

        # Register tools
        for tool_def in conn.make_tool_definitions():
            registry.register(tool_def)

        return conn

    async def disconnect_server(self, name: str, registry: ToolRegistry) -> None:
        """Disconnect server and remove its tools from the registry."""
        conn = self._connections.pop(name, None)
        if conn:
            await conn.disconnect()
        registry.unregister_by_source(f"mcp:{name}")

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def load_from_config(self, registry: ToolRegistry) -> dict[str, Any]:
        """Read mcp_servers.json and connect all configured servers."""
        config_path = Path(settings.mcp_config)
        if not config_path.exists():
            return {"connected": [], "failed": []}

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {"connected": [], "failed": [], "error": str(e)}

        connected: list[str] = []
        failed: list[dict] = []

        for server_cfg in data.get("servers", []):
            try:
                await self.connect_server(server_cfg, registry, retry=False)
                connected.append(server_cfg["name"])
            except Exception as e:
                failed.append({"name": server_cfg.get("name", "?"), "error": str(e)})

        return {"connected": connected, "failed": failed}

    async def reload_servers(self, registry: ToolRegistry) -> dict[str, Any]:
        """Disconnect all and reconnect from config file."""
        for name in list(self._connections.keys()):
            await self.disconnect_server(name, registry)
        return await self.load_from_config(registry)

    async def disconnect_all(self, registry: ToolRegistry) -> None:
        for name in list(self._connections.keys()):
            await self.disconnect_server(name, registry)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_servers(self) -> list[dict[str, Any]]:
        result = []
        for name, conn in self._connections.items():
            result.append({
                "name": name,
                "connected": conn.connected,
                "tool_count": len(conn.tools),
                "tools": [t.name for t in conn.tools],
                "command": conn.config.get("command", ""),
            })
        return result

    def get_connection(self, name: str) -> MCPServerConnection | None:
        return self._connections.get(name)

    def save_to_config(self) -> None:
        """Persist current server list to mcp_servers.json."""
        config_path = Path(settings.mcp_config)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        servers = [
            {
                "name": conn.config["name"],
                "command": conn.config["command"],
                "args": conn.config.get("args", []),
                "env": conn.config.get("env", {}),
            }
            for conn in self._connections.values()
        ]
        config_path.write_text(
            json.dumps({"servers": servers}, indent=2), encoding="utf-8"
        )
