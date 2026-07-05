from typing import Any, Callable

from pydantic import BaseModel


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object
    source: str  # "builtin" | "mcp:<server>" | "skill:<skill>"
    handler: Callable

    # Optional metadata (§10) — used by planning, the executor, and failure
    # recovery. All default to a neutral value so existing definitions and
    # MCP-discovered tools keep working unchanged.
    timeout: int | None = None  # seconds; None = use caller default
    output_schema: dict[str, Any] | None = None
    permissions: list[str] = []  # e.g. ["fs:write", "network"]
    error_hints: str | None = None  # shown to the model on failure

    model_config = {"arbitrary_types_allowed": True}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name!r}. Available: {list(self._tools)}")
        return self._tools[name]

    def list_all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def list_by_source(self, source_prefix: str) -> list[ToolDefinition]:
        return [t for t in self._tools.values() if t.source.startswith(source_prefix)]

    def unregister_by_source(self, source_prefix: str) -> int:
        to_remove = [
            n for n, t in self._tools.items() if t.source.startswith(source_prefix)
        ]
        for name in to_remove:
            del self._tools[name]
        return len(to_remove)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_tools(self) -> list[dict]:
        """All tools in OpenAI function-calling format, for ChatOllama.bind_tools()."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def summary(self) -> str:
        lines = []
        for t in self._tools.values():
            lines.append(f"  [{t.source}] {t.name} — {t.description}")
        return "\n".join(lines) if lines else "  (no tools registered)"


# ---------------------------------------------------------------------------
# Built-in tool definitions
# ---------------------------------------------------------------------------


def _build_builtin_tools() -> list[ToolDefinition]:
    from app.tools.filesystem import (create_file, delete_file, edit_file,
                                      list_directory, read_file, search_files,
                                      write_file)
    from app.tools.git_tool import git_commit, git_diff, git_log, git_status
    from app.tools.symbols_tool import find_references, find_symbol
    from app.tools.terminal import run_command

    return [
        ToolDefinition(
            name="read_file",
            description="Read the contents of a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path",
                    }
                },
                "required": ["path"],
            },
            source="builtin",
            handler=read_file,
        ),
        ToolDefinition(
            name="write_file",
            description="Write or overwrite a file with the given content.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            source="builtin",
            handler=write_file,
        ),
        ToolDefinition(
            name="edit_file",
            description="Find-and-replace a unique string in a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {
                        "type": "string",
                        "description": "Exact string to find (must be unique in file)",
                    },
                    "new_str": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
            source="builtin",
            handler=edit_file,
        ),
        ToolDefinition(
            name="create_file",
            description="Create a new file. Fails if it already exists.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "default": ""},
                },
                "required": ["path"],
            },
            source="builtin",
            handler=create_file,
        ),
        ToolDefinition(
            name="delete_file",
            description="Delete a file. Requires confirm=true.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true to proceed",
                    },
                },
                "required": ["path", "confirm"],
            },
            source="builtin",
            handler=delete_file,
        ),
        ToolDefinition(
            name="list_directory",
            description="List files and directories at a path.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
            source="builtin",
            handler=list_directory,
        ),
        ToolDefinition(
            name="search_files",
            description="Search files under a path for a regex pattern.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "Python regex pattern",
                    },
                },
                "required": ["path", "pattern"],
            },
            source="builtin",
            handler=search_files,
        ),
        ToolDefinition(
            name="run_command",
            description="Execute a shell command safely. Blocked commands are rejected.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds before timeout",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
            source="builtin",
            handler=run_command,
        ),
        ToolDefinition(
            name="git_status",
            description="Show git status of a repository.",
            parameters={
                "type": "object",
                "properties": {"repo_path": {"type": "string"}},
                "required": ["repo_path"],
            },
            source="builtin",
            handler=git_status,
        ),
        ToolDefinition(
            name="git_diff",
            description="Show git diff for a repository or a specific file.",
            parameters={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "file": {
                        "type": "string",
                        "description": "Specific file to diff (optional)",
                    },
                },
                "required": ["repo_path"],
            },
            source="builtin",
            handler=git_diff,
        ),
        ToolDefinition(
            name="git_commit",
            description="Stage all changes and create a git commit.",
            parameters={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["repo_path", "message"],
            },
            source="builtin",
            handler=git_commit,
        ),
        ToolDefinition(
            name="git_log",
            description="Show recent git commits.",
            parameters={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "n": {"type": "integer", "default": 10},
                },
                "required": ["repo_path"],
            },
            source="builtin",
            handler=git_log,
        ),
        ToolDefinition(
            name="find_symbol",
            description="Find where a function/class/method is DEFINED (returns file:line). Use instead of grepping for definitions.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact symbol name"},
                },
                "required": ["name"],
            },
            source="builtin",
            handler=find_symbol,
        ),
        ToolDefinition(
            name="find_references",
            description="Find where a function/class/method is USED/CALLED across the project (returns file:line list).",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact symbol name"},
                },
                "required": ["name"],
            },
            source="builtin",
            handler=find_references,
        ),
    ]


def create_registry() -> ToolRegistry:
    """Create a ToolRegistry pre-loaded with all built-in tools."""
    registry = ToolRegistry()
    for tool in _build_builtin_tools():
        registry.register(tool)
    return registry


# Module-level singleton
registry = create_registry()
