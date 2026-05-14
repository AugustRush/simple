from __future__ import annotations

import asyncio
import contextvars
from contextlib import AsyncExitStack
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import time
import traceback
import urllib.request
import uuid
from typing import Any, Callable, Optional
from datetime import datetime, timezone
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import mcp

from agent import shared
from agent.core.output import OutputSink, _active_sink
from agent.pathing import path_contains, resolve_workspace_path

_active_schedule_target: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("_active_schedule_target", default=None)
)

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable
    source: str = "runtime"
    capabilities: frozenset[str] = field(default_factory=frozenset)


class ToolRegistry:
    """Central registry for all tools."""

    _DEFAULT_TOOL_CAPABILITIES: dict[tuple[str, str], frozenset[str]] = {
        ("builtin", "current_time"): frozenset({"read"}),
        ("builtin", "read_file"): frozenset({"read"}),
        ("builtin", "list_files"): frozenset({"read"}),
        ("builtin", "memory_read"): frozenset({"read"}),
        ("builtin", "memory_search"): frozenset({"read"}),
        ("builtin", "memory_index"): frozenset({"read"}),
        ("builtin", "context_retrieve"): frozenset({"read"}),
        ("builtin", "schedule_list"): frozenset({"read"}),
        ("builtin", "web_search"): frozenset({"read"}),
        ("builtin", "web_fetch"): frozenset({"read"}),
        ("builtin", "tavily_search"): frozenset({"read"}),
        ("builtin", "write_file"): frozenset({"workspace_write"}),
        ("builtin", "clean_output"): frozenset({"output_write"}),
        ("builtin", "shell"): frozenset({"shell"}),
        ("builtin", "transcribe_audio"): frozenset({"read"}),
        ("builtin", "send_file"): frozenset({"side_effect"}),
        ("builtin", "clean_output"): frozenset({"output_write"}),
        ("builtin", "memory_write"): frozenset({"state_write"}),
        ("builtin", "schedule_create"): frozenset({"state_write"}),
        ("builtin", "schedule_delete"): frozenset({"state_write"}),
        ("runtime:skill", "activate_skill"): frozenset({"read"}),
        ("runtime:skill", "list_skill_files"): frozenset({"read"}),
        ("runtime:skill", "read_skill_file"): frozenset({"read"}),
        ("runtime:skill", "create_skill"): frozenset({"state_write"}),
        ("runtime:skill", "update_skill"): frozenset({"state_write"}),
        ("runtime:skill", "delete_skill"): frozenset({"state_write"}),
        ("runtime:skill", "write_skill_file"): frozenset({"state_write"}),
    }

    def __init__(self, console: Optional[Any] = None):
        self._tools: dict[str, ToolDef] = {}
        self._context: dict[str, Any] = {}
        self._context_override: contextvars.ContextVar[Optional[dict[str, Any]]] = (
            contextvars.ContextVar("tool_registry_context_override", default=None)
        )
        self.console = console

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: Callable,
        *,
        replace: bool = False,
        source: str = "runtime",
        capabilities: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
    ):
        if name in self._tools:
            existing = self._tools[name]
            if not replace:
                raise ValueError(
                    f"Tool '{name}' is already registered by source '{existing.source}'. "
                    "Pass replace=True to overwrite it."
                )
            if existing.source != source:
                raise ValueError(
                    f"Tool '{name}' is already registered by source '{existing.source}'. "
                    f"Only the same source may replace it; got '{source}'."
                )
        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            source=source,
            capabilities=self._coerce_capabilities(name, source, capabilities),
        )

    @classmethod
    def _coerce_capabilities(
        cls,
        name: str,
        source: str,
        capabilities: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None,
    ) -> frozenset[str]:
        if capabilities is None:
            return cls._DEFAULT_TOOL_CAPABILITIES.get((source, name), frozenset())
        return frozenset(str(item) for item in capabilities if str(item).strip())

    def tool(self, name: str, description: str, parameters: dict):
        def decorator(fn: Callable):
            self.register(name, description, parameters, fn)
            return fn

        return decorator

    def to_anthropic_format(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in self._tools.values()
        ]

    @staticmethod
    def _error_payload(tool_name: str, message: str) -> str:
        return json.dumps(
            {"ok": False, "tool": tool_name, "error": message},
            ensure_ascii=False,
        )

    @staticmethod
    def _tool_has_safe_kwargs(tool_input: dict) -> bool:
        """Return True if all keys in *tool_input* are valid Python identifiers."""
        return all(
            str(k).isidentifier()
            for k in tool_input
        )

    async def call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name not in self._tools:
            return self._error_payload(tool_name, f"tool '{tool_name}' not found")
        override_registry: Optional["ToolRegistry"] = None
        override_token = None
        try:
            fn = self._tools[tool_name].fn
            owner = getattr(fn, "__self__", None)
            owner_registry = getattr(owner, "registry", None)
            if isinstance(owner_registry, ToolRegistry) and owner_registry is not self:
                merged_context = dict(owner_registry._context)
                merged_context.update(self._context)
                override_registry = owner_registry
                override_token = owner_registry._context_override.set(merged_context)
            if self._tool_has_safe_kwargs(tool_input):
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**tool_input)
                else:
                    result = fn(**tool_input)
            else:
                # MCP tools and others with non-identifier parameter names
                # can't use ** unpacking — pass the dict directly.
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(tool_input)
                else:
                    result = fn(tool_input)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return "" if result is None else str(result)
        except (asyncio.TimeoutError, TimeoutError):
            return self._error_payload(tool_name, f"Timeout calling tool '{tool_name}'")
        except ValueError as e:
            return self._error_payload(
                tool_name, f"Invalid input for tool '{tool_name}': {e}"
            )
        except Exception as e:
            if self.console is not None:
                self.console.print(
                    f"[yellow]Tool '{tool_name}' failed: {e}\n{traceback.format_exc()}[/yellow]"
                )
            return self._error_payload(
                tool_name, f"Error calling tool '{tool_name}': {e}"
            )
        finally:
            if override_registry is not None and override_token is not None:
                override_registry._context_override.reset(override_token)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def set_context(self, key: str, value: Any) -> None:
        self._context[key] = value

    def get_context(self, key: str, default: Any = None) -> Any:
        override = self._context_override.get()
        if override is not None and key in override:
            return override[key]
        return self._context.get(key, default)

    def unregister_by_source_prefix(self, prefix: str) -> None:
        for name in [
            n for n, tool in self._tools.items() if tool.source.startswith(prefix)
        ]:
            self._tools.pop(name, None)



class MCPClient:
    """Connect to external MCP servers and inject tools into registry."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._sessions = []
        self._stack = AsyncExitStack()
        self._configured_servers = 0
        self._connected_servers = 0
        self._failed_servers = 0
        self._registered_tools = 0

    @staticmethod
    def _safe_name(value: str) -> str:
        name = re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip().lower())
        return name.strip("_") or "mcp"

    async def connect_from_config(
        self, config: dict, extra_env: dict[str, str] | None = None
    ):
        self._extra_env = extra_env or {}
        self._configured_servers = len(config.get("mcp_servers", []) or [])
        mcp_servers = config.get("mcp_servers", [])
        for server_cfg in mcp_servers:
            try:
                await self._connect_server(server_cfg)
                self._connected_servers += 1
            except Exception as e:
                self._failed_servers += 1
                shared.CONSOLE.print(
                    f"[yellow]MCP server connect failed ({server_cfg.get('name', '?')}): {e}[/yellow]"
                )

    async def _connect_server(self, cfg: dict):
        command = str(cfg.get("command", "")).strip()
        if not command:
            raise ValueError("MCP server config requires 'command'")

        server_name = self._safe_name(
            str(cfg.get("name") or Path(command).name or "mcp")
        )
        # Merge: agent-level env < server-specific env (server wins)
        server_env = dict(cfg.get("env", {}) or {})
        merged_env = (
            {**self._extra_env, **server_env} if self._extra_env or server_env else None
        )
        cwd = cfg.get("cwd") or self._extra_env.get("AGENT_OUTPUT_DIR")
        params = mcp.StdioServerParameters(
            command=command,
            args=list(cfg.get("args", []) or []),
            env=merged_env or None,
            cwd=str(cwd) if cwd else None,
        )
        read_stream, write_stream = await self._stack.enter_async_context(
            mcp.stdio_client(params)
        )
        session = await self._stack.enter_async_context(
            mcp.ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions.append({"name": server_name, "session": session})

        tools_result = await session.list_tools()
        for tool in getattr(tools_result, "tools", []):
            self._register_tool(server_name, session, tool)

    def _register_tool(self, server_name: str, session: Any, tool: Any) -> None:
        original_name = str(getattr(tool, "name", "")).strip()
        if not original_name:
            return
        registered_name = f"mcp_{server_name}_{self._safe_name(original_name)}"
        description = getattr(tool, "description", None) or f"MCP tool {original_name}"
        parameters = getattr(tool, "inputSchema", None) or {
            "type": "object",
            "properties": {},
            "required": [],
        }

        async def _call_mcp_tool(tool_args: dict | None = None, **extra: Any):
            # tool_args is the raw input dict when keys are non-identifiers;
            # extra captures any keyword-style params from legacy callers.
            arguments = tool_args if isinstance(tool_args, dict) else extra
            result = await session.call_tool(original_name, arguments=arguments or None)
            text_blocks = []
            for block in getattr(result, "content", []) or []:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text_blocks.append(getattr(block, "text", ""))
                else:
                    text_blocks.append(str(block))
            return {
                "ok": not bool(getattr(result, "isError", False)),
                "server": server_name,
                "tool": original_name,
                "text": "\n".join(b for b in text_blocks if b).strip(),
                "structured": getattr(result, "structuredContent", None),
            }

        self.registry.register(
            registered_name,
            description,
            parameters,
            _call_mcp_tool,
            source=f"mcp:{server_name}",
        )
        self._registered_tools += 1

    def status_summary(self) -> dict[str, Any]:
        return {
            "configured_servers": self._configured_servers,
            "connected_servers": self._connected_servers,
            "failed_servers": self._failed_servers,
            "registered_tools": self._registered_tools,
        }

    async def close(self) -> None:
        await self._stack.aclose()


class _UserToolRegistryFacade:
    def __init__(self, registry: ToolRegistry, source: str):
        self._registry = registry
        self._source = source

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: Callable,
        *,
        replace: bool = False,
        capabilities: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
    ) -> None:
        self._registry.register(
            name,
            description,
            parameters,
            fn,
            replace=replace,
            source=self._source,
            capabilities=capabilities,
        )


class UserToolCatalog:
    """Discover and load user-authored Python tool plugins."""

    def __init__(self, root: Optional[Path] = None):
        self.root = root or shared.TOOLS_DIR

    def load_into_registry(self, registry: ToolRegistry) -> list[str]:
        self.root.mkdir(parents=True, exist_ok=True)
        registry.unregister_by_source_prefix("user_tool:")
        loaded: list[str] = []
        for tool_file in sorted(self.root.rglob("*.py")):
            plugin_id = tool_file.relative_to(self.root).with_suffix("").as_posix()
            source = f"user_tool:{plugin_id}"
            try:
                module_name = f"agent_user_tool_{uuid.uuid4().hex}"
                spec = importlib.util.spec_from_file_location(module_name, tool_file)
                if spec is None or spec.loader is None:
                    raise ValueError("unable to create import spec")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise ValueError(
                        "tool plugin must define callable register(registry)"
                    )
                register(_UserToolRegistryFacade(registry, source))
                loaded.append(plugin_id)
            except Exception as e:
                shared.CONSOLE.print(
                    f"[yellow]Failed to load user tool plugin {tool_file}: {e}[/yellow]"
                )
        return loaded
