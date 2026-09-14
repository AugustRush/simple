from __future__ import annotations

import asyncio
import atexit
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from functools import partial
import html
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import threading
import time
import traceback
import urllib.request
from typing import Any, Callable, Optional
from datetime import datetime, timezone
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import mcp

from agent import shared
from agent.core.output import OutputSink, _active_sink
from agent.pathing import path_contains, resolve_workspace_path
from agent.tools import user_tools

_active_schedule_target: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("_active_schedule_target", default=None)
)

#: The signal this turn is answering, when the turn is a scheduled run that
#: was woken by one.  A tool that emits a signal from inside such a run has to
#: know how far from the cascade root it already is, or every hop would look
#: like a fresh start and the depth ceiling would never be reached.  Invisible
#: to ordinary conversation turns, where it stays ``None``.
_active_signal_context: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("_active_signal_context", default=None)
)

# ── Synchronous tool dispatch ────────────────────────────────────────────────
#
# Most tools (file I/O, memory/SQLite, scheduler CRUD) are plain sync
# functions.  Calling them inline stalls the whole event loop: channel
# heartbeats stop, other sessions' messages queue up, and — worst — the
# ToolExecutor's own timeout/progress machinery cannot run, so
# ``tool_timeout_seconds`` silently does not apply to them.  Dispatching
# through a dedicated pool keeps the loop responsive and makes those
# timeouts real.
#
# The pool is deliberately *not* the loop's default executor: channels
# (notably Feishu) push their blocking SDK calls through
# ``run_in_executor(None, ...)``, and a burst of slow tools must not be able
# to starve message delivery.
_SYNC_TOOL_EXECUTOR: Optional[ThreadPoolExecutor] = None
_SYNC_TOOL_EXECUTOR_LOCK = threading.Lock()


def _sync_tool_max_workers() -> int:
    configured = os.environ.get("SIMPLE_SYNC_TOOL_WORKERS", "").strip()
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    return max(4, min(16, (os.cpu_count() or 4) * 2))


def sync_tool_executor() -> ThreadPoolExecutor:
    """Return the process-wide pool used to run synchronous tools."""
    global _SYNC_TOOL_EXECUTOR
    if _SYNC_TOOL_EXECUTOR is None:
        with _SYNC_TOOL_EXECUTOR_LOCK:
            if _SYNC_TOOL_EXECUTOR is None:
                _SYNC_TOOL_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_sync_tool_max_workers(),
                    thread_name_prefix="agent-sync-tool",
                )
                # Runs before ThreadPoolExecutor's own atexit join (LIFO), so
                # tools still sitting in the queue are dropped rather than
                # started during interpreter shutdown.
                atexit.register(shutdown_sync_tool_executor)
    return _SYNC_TOOL_EXECUTOR


def shutdown_sync_tool_executor(wait: bool = False) -> None:
    """Tear the pool down at process shutdown.  Safe to call more than once."""
    global _SYNC_TOOL_EXECUTOR
    with _SYNC_TOOL_EXECUTOR_LOCK:
        executor, _SYNC_TOOL_EXECUTOR = _SYNC_TOOL_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


async def _call_sync_tool(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking tool off-loop, preserving the caller's context.

    ``copy_context()`` carries ``_active_sink``, ``_active_agent_context`` and
    the registry's ``_context_override`` into the worker thread, so tools that
    resolve per-turn state keep seeing the turn that invoked them.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        sync_tool_executor(), partial(ctx.run, partial(fn, *args, **kwargs))
    )


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable
    source: str = "runtime"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    authorizer: Optional[Callable[[dict, "ToolRegistry"], Optional[dict]]] = None


# JSON Schema keywords this registry can validate deterministically.  Schemas
# using any other keyword fall back to legacy behavior instead of pretending
# they were fully validated.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "pattern",
        "format",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "const",
        "multipleOf",
        "$ref",
    }
)

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _invalid_request(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": message,
            "details": {},
            "retryable": False,
        },
    }


def _schema_has_unsupported_keywords(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return True
    return any(keyword in schema for keyword in _UNSUPPORTED_SCHEMA_KEYWORDS)


def _validate_value(value: Any, schema: Any, field: str) -> Optional[dict]:
    if _schema_has_unsupported_keywords(schema):
        return None
    expected = _JSON_TYPE_MAP.get(schema.get("type", ""))
    if expected is not None:
        if schema.get("type") == "integer" and isinstance(value, bool):
            return _invalid_request(
                f"invalid value for field '{field}': expected integer"
            )
        if not isinstance(value, expected) or (
            schema.get("type") == "boolean" and not isinstance(value, bool)
        ):
            return _invalid_request(
                f"invalid value for field '{field}': expected "
                f"{schema.get('type')}, got {type(value).__name__}"
            )
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values and value not in enum_values:
        return _invalid_request(
            f"invalid value for field '{field}': {value!r} is not one of "
            + ", ".join(repr(item) for item in enum_values)
        )
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if minimum is not None and value < minimum:
            return _invalid_request(
                f"invalid value for field '{field}': must be >= {minimum}"
            )
        if maximum is not None and value > maximum:
            return _invalid_request(
                f"invalid value for field '{field}': must be <= {maximum}"
            )
    if schema.get("type") == "array":
        items_schema = schema.get("items")
        if isinstance(items_schema, dict) and isinstance(value, list):
            for index, item in enumerate(value):
                error = _validate_value(item, items_schema, f"{field}[{index}]")
                if error is not None:
                    return error
    return None


def _validate_tool_input(tool_input: Any, schema: Any) -> Optional[dict]:
    if not isinstance(tool_input, dict):
        return _invalid_request("tool input must be an object")
    if _schema_has_unsupported_keywords(schema) or schema.get("type") != "object":
        return None  # unsupported schema shape -> legacy behavior
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    for key in schema.get("required", []):
        # A property that declares a default is supplied by the handler when
        # absent, so it is not a hard requirement regardless of the schema's
        # legacy "required" list.
        prop_schema = properties.get(key)
        if isinstance(prop_schema, dict) and "default" in prop_schema:
            continue
        if key not in tool_input:
            return _invalid_request(f"missing required field: {key}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(tool_input) - set(properties))
        if unknown:
            return _invalid_request(
                "unknown field(s): " + ", ".join(unknown)
            )
    for key, value in tool_input.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            continue
        error = _validate_value(value, prop_schema, key)
        if error is not None:
            return error
    return None


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
        ("builtin", "write_file"): frozenset({"output_write"}),
        ("builtin", "edit_file"): frozenset({"output_write"}),
        ("builtin", "clean_output"): frozenset({"output_write"}),
        ("builtin", "clear_context"): frozenset({"state_write"}),
        ("builtin", "shell"): frozenset({"shell", "requires_intent"}),
        ("builtin", "transcribe_audio"): frozenset({"read"}),
        ("builtin", "send_file"): frozenset({"side_effect"}),
        ("builtin", "memory_write"): frozenset({"state_write"}),
        ("builtin", "set_identity"): frozenset({"state_write"}),
        ("builtin", "memory_clear"): frozenset({"state_write"}),
        ("builtin", "schedule_create"): frozenset({"state_write"}),
        ("builtin", "schedule_delete"): frozenset({"state_write"}),
        ("runtime:skill", "activate_skill"): frozenset({"read"}),
        ("runtime:skill", "list_skill_files"): frozenset({"read"}),
        ("runtime:skill", "read_skill_file"): frozenset({"read"}),
        ("runtime:skill", "create_skill"): frozenset({"state_write"}),
        ("runtime:skill", "update_skill"): frozenset({"state_write"}),
        ("runtime:skill", "delete_skill"): frozenset({"state_write"}),
        ("runtime:skill", "write_skill_file"): frozenset({"state_write"}),
        ("runtime:spawn", "spawn_agent"): frozenset({"orchestration"}),
    }

    def __init__(self, console: Optional[Any] = None):
        self._tools: dict[str, ToolDef] = {}
        self._prompt_generation: int = 0
        self._context: dict[str, Any] = {}
        self._context_override: contextvars.ContextVar[Optional[dict[str, Any]]] = (
            contextvars.ContextVar("tool_registry_context_override", default=None)
        )
        self.console = console
        # Cache for to_anthropic_format(), invalidated by _prompt_generation.
        # The tool list is rebuilt once per registration change rather than on
        # every tool-loop iteration; register()/unregister_*() bump the counter.
        self._anthropic_tools_cache: Optional[list[dict]] = None
        self._anthropic_tools_generation: int = -1

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
        authorizer: Optional[
            Callable[[dict, "ToolRegistry"], Optional[dict]]
        ] = None,
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
            authorizer=authorizer,
        )
        self._prompt_generation += 1

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

    def to_anthropic_format(self, names: Optional[set[str]] = None) -> list[dict]:
        if (
            self._anthropic_tools_cache is None
            or self._anthropic_tools_generation != self._prompt_generation
        ):
            self._anthropic_tools_cache = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in self._tools.values()
            ]
            self._anthropic_tools_generation = self._prompt_generation
        if names is None:
            return self._anthropic_tools_cache
        return [tool for tool in self._anthropic_tools_cache if tool["name"] in names]

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
            validation_error = _validate_tool_input(tool_input, self._tools[tool_name].parameters)
            if validation_error is not None:
                return json.dumps(validation_error, ensure_ascii=False)
            authorizer = self._tools[tool_name].authorizer
            if authorizer is not None:
                denial = authorizer(tool_input, self)
                if denial is not None:
                    return json.dumps(denial, ensure_ascii=False)
            if self._tool_has_safe_kwargs(tool_input):
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**tool_input)
                else:
                    result = await _call_sync_tool(fn, **tool_input)
            else:
                # MCP tools and others with non-identifier parameter names
                # can't use ** unpacking — pass the dict directly.
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(tool_input)
                else:
                    result = await _call_sync_tool(fn, tool_input)
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

    def tool_capabilities(self, name: str) -> frozenset[str]:
        """Capabilities declared by a registered tool (empty if unknown)."""
        tool = self._tools.get(name)
        return tool.capabilities if tool is not None else frozenset()

    def tool_source(self, name: str) -> str:
        """Registration source of a tool (empty when it is not registered)."""
        tool = self._tools.get(name)
        return tool.source if tool is not None else ""

    def tools_with_capability(self, capability: str) -> list[str]:
        return [name for name, t in self._tools.items() if capability in t.capabilities]

    def set_context(self, key: str, value: Any) -> None:
        self._context[key] = value

    def get_context(self, key: str, default: Any = None) -> Any:
        override = self._context_override.get()
        if override is not None and key in override:
            return override[key]
        return self._context.get(key, default)

    def fork(
        self,
        context: Optional[dict[str, Any]] = None,
        *,
        exclude_source_prefixes: tuple[str, ...] = (),
    ) -> "ToolRegistry":
        """Create a cheap session view over the registered tool functions.

        Tool definitions are immutable after registration for the lifetime of
        a config revision, so a shallow copy is sufficient. Bound tool methods
        continue to use their owner registry; ``call`` already installs this
        view's context as a ContextVar override while invoking them.
        """

        forked = type(self)(console=self.console)
        forked._tools = {
            name: tool
            for name, tool in self._tools.items()
            if not any(
                tool.source.startswith(prefix)
                for prefix in exclude_source_prefixes
            )
        }
        forked._context = dict(self._context)
        if context:
            forked._context.update(context)
        forked._prompt_generation = self._prompt_generation
        return forked

    def unregister_by_source_prefix(self, prefix: str) -> None:
        for name in [
            n for n, tool in self._tools.items() if tool.source.startswith(prefix)
        ]:
            self._tools.pop(name, None)
        self._prompt_generation += 1



class MCPClient:
    """Connect to external MCP servers and inject tools into registry."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._sessions = []
        # AnyIO cancel scopes must be exited by the task that entered them.
        # Each stdio server therefore owns its context managers in a dedicated
        # task which remains alive until ``close()`` signals shutdown.
        self._server_tasks: list[asyncio.Task[None]] = []
        self._shutdown = asyncio.Event()
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
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        task = asyncio.create_task(
            self._run_server(cfg, ready),
            name=f"mcp-server-{self._safe_name(str(cfg.get('name') or 'mcp'))}",
        )
        self._server_tasks.append(task)
        try:
            await ready
        except asyncio.CancelledError:
            # If startup itself is cancelled, do not leave an unowned stdio
            # process behind.  The owner task performs its own context unwind.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _run_server(
        self,
        cfg: dict,
        ready: asyncio.Future[None],
    ) -> None:
        """Own one MCP server's complete enter/wait/exit lifecycle."""
        command = str(cfg.get("command", "")).strip()
        if not command:
            if not ready.done():
                ready.set_exception(ValueError("MCP server config requires 'command'"))
            return

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
        try:
            async with AsyncExitStack() as stack:
                # The MCP SDK otherwise binds the child process' stderr
                # directly to the real terminal.  Keep diagnostics, but route
                # them away from prompt_toolkit's live input line.
                errlog = self._open_server_errlog(stack, server_name)
                read_stream, write_stream = await stack.enter_async_context(
                    mcp.stdio_client(params, errlog=errlog)
                )
                session = await stack.enter_async_context(
                    mcp.ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                self._sessions.append({"name": server_name, "session": session})

                tools_result = await session.list_tools()
                for tool in getattr(tools_result, "tools", []):
                    self._register_tool(server_name, session, tool)
                if not ready.done():
                    ready.set_result(None)
                await self._shutdown.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                shared.CONSOLE.print(
                    f"[yellow]MCP server stopped ({server_name}): {exc}[/yellow]"
                )

    def _open_server_errlog(self, stack: AsyncExitStack, server_name: str):
        """Return a process-compatible stderr target owned by this client."""
        output_dir = str(self._extra_env.get("AGENT_OUTPUT_DIR", "") or "").strip()
        if not output_dir:
            return subprocess.DEVNULL
        try:
            log_dir = Path(output_dir) / "mcp-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            return stack.enter_context(
                (log_dir / f"{server_name}.stderr.log").open(
                    "a", encoding="utf-8"
                )
            )
        except OSError:
            # A logging failure must not prevent an otherwise healthy MCP
            # server from connecting, and falling back to the terminal would
            # reintroduce the input-line corruption this guard prevents.
            return subprocess.DEVNULL

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
        self._shutdown.set()
        if self._server_tasks:
            await asyncio.gather(*self._server_tasks, return_exceptions=True)
            self._server_tasks.clear()


class UserToolCatalog:
    """Discover and load user-authored Python tool plugins.

    Loading executes local Python inside the live session, so the catalog
    admits a module only when the whole directory is trusted
    (``user_tools.enabled=true``) or the individual file was approved by
    content hash.  Approval survives restarts; editing the file does not
    survive approval.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = root or shared.TOOLS_DIR

    @property
    def deps_dir(self) -> Path:
        return user_tools.deps_dir(self.root)

    def discover(self) -> list[Path]:
        """Tool modules on disk, excluding ``_deps`` and other private paths."""
        if not self.root.is_dir():
            return []
        return [
            path
            for path in sorted(self.root.rglob("*.py"))
            if user_tools.is_tool_module(path, self.root)
        ]

    def tool_id_for(self, path: Path) -> str:
        return path.relative_to(self.root).with_suffix("").as_posix()

    def load_into_registry(
        self,
        registry: ToolRegistry,
        *,
        require_approval: bool = False,
    ) -> list[str]:
        """Register every admissible tool module without importing it here.

        With *require_approval* set, only files whose current contents match a
        recorded approval are loaded — the mode used when ``user_tools`` is
        not globally enabled, so a tool the user explicitly approved keeps
        working across restarts without trusting the whole directory.

        Loading used to ``exec_module`` each file into the live session, which
        put model-authored code in the same process as the provider API keys,
        the memory database and the registry itself.  Instead the module is
        probed out of process for its schema, and what gets registered is a
        proxy that runs the real call in a sandboxed child (see
        ``user_tool_runner``).  Nothing from the tool is imported here.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        user_tools.ensure_deps_on_path(self.root)
        registry.unregister_by_source_prefix("user_tool:")
        loaded: list[str] = []
        for tool_file in self.discover():
            plugin_id = self.tool_id_for(tool_file)
            if require_approval and not user_tools.is_approved(
                plugin_id, user_tools.file_digest(tool_file), self.root
            ):
                continue
            source = f"user_tool:{plugin_id}"
            probe = user_tools.probe_module_sync(tool_file, root=self.root)
            if not probe.ok:
                shared.CONSOLE.print(
                    f"[yellow]Failed to load user tool plugin {tool_file}: "
                    f"{probe.error}[/yellow]"
                )
                continue
            registered_any = False
            for spec in probe.tools:
                name = str(spec.get("name") or "").strip()
                if not name:
                    continue
                registry.register(
                    name,
                    str(spec.get("description") or ""),
                    spec.get("parameters") or {"type": "object", "properties": {}},
                    self._proxy_for(tool_file, name, registry),
                    replace=True,
                    source=source,
                )
                registered_any = True
            if registered_any:
                loaded.append(plugin_id)
            else:
                shared.CONSOLE.print(
                    f"[yellow]User tool plugin {tool_file} registered no tools[/yellow]"
                )
        return loaded

    def _proxy_for(
        self, tool_file: Path, tool_name: str, registry: ToolRegistry
    ) -> Callable:
        """An async callable that forwards one tool call to a child process."""

        async def _invoke(**tool_input: Any) -> Any:
            from agent.tools.user_tool_runner import run_user_tool

            return await run_user_tool(
                tool_file,
                tool_name,
                tool_input,
                registry=registry,
                root=self.root,
            )

        _invoke.__name__ = f"user_tool_{tool_name}"
        _invoke.__doc__ = (
            f"Proxy for user tool {tool_name!r} in {tool_file}; the tool body "
            "runs in a sandboxed child process."
        )
        return _invoke
