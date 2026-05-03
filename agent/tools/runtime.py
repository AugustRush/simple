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

TOOL_DEFAULT_MAX_READ_BYTES = 64 * 1024
TOOL_DEFAULT_MAX_WRITE_BYTES = 256 * 1024
TOOL_DEFAULT_MAX_LIST_RESULTS = 100
_atomic_write_text = shared._atomic_write_text

WEB_FETCH_MAX_BYTES = 512 * 1024
WEB_FETCH_TIMEOUT = 20
WEB_SEARCH_MAX_RESULTS = 10
TAVILY_SEARCH_MAX_RESULTS = 10
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
WEB_USER_AGENT = (
    "Mozilla/5.0 (compatible; PersonalAgent/1.0; +https://github.com/your/agent)"
)

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
            if asyncio.iscoroutinefunction(fn):
                result = await fn(**tool_input)
            else:
                result = fn(**tool_input)
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


class BuiltinTools:
    """Built-in tools with bounded file access and structured responses."""

    def __init__(
        self,
        memory: Any,
        registry: ToolRegistry,
        context_manager: Optional[Any] = None,
        workspace_root: Optional[Path] = None,
        chapter_normalizer: Optional[Callable[[str], str]] = None,
        output_dir: Optional[Path] = None,
    ):
        self.memory = memory
        self.registry = registry
        self.context_manager = context_manager
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.chapter_normalizer = chapter_normalizer or (lambda chapter: str(chapter))
        self._output_dir = output_dir
        self._register()

    def _register(self):
        r = self.registry

        r.register(
            "current_time",
            "Get the current local or requested timezone time as structured data. Use when the user asks about now, today, current date, or current time.",
            {
                "type": "object",
                "properties": {
                    "timezone_name": {
                        "type": "string",
                        "description": "IANA timezone name like 'Asia/Shanghai'. Default: local system timezone.",
                        "default": "local",
                    },
                },
                "required": [],
            },
            self._current_time,
            source="builtin",
        )

        r.register(
            "shell",
            "Execute a shell command and return stdout/stderr. Use for system operations, running scripts, etc.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30)",
                        "default": 30,
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory inside the workspace or output directory. Use this for downloads and generated artifacts.",
                    },
                },
                "required": ["command"],
            },
            self._shell,
            source="builtin",
        )

        r.register(
            "read_file",
            "Read the contents of a file.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "Maximum bytes to read before truncating",
                        "default": TOOL_DEFAULT_MAX_READ_BYTES,
                    },
                },
                "required": ["path"],
            },
            self._read_file,
            source="builtin",
        )

        r.register(
            "write_file",
            "Write content to a file (creates or overwrites).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"},
                    "max_bytes": {
                        "type": "integer",
                        "description": "Maximum payload size accepted by the tool",
                        "default": TOOL_DEFAULT_MAX_WRITE_BYTES,
                    },
                },
                "required": ["path", "content"],
            },
            self._write_file,
            source="builtin",
        )

        r.register(
            "send_file",
            "Queue an existing file to be sent back to the current user/channel when the turn completes. Use after generating or locating a file the user asked to receive.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path within the workspace or output directory",
                    }
                },
                "required": ["path"],
            },
            self._send_file,
            source="builtin",
        )

        r.register(
            "transcribe_audio",
            "Transcribe an audio file to text using the configured local speech-to-text command. Use this for audio attachments; do not use read_file on audio files.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to an audio file within the workspace or output directory",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional language hint such as zh, en, ja, or ko",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 300,
                    },
                },
                "required": ["path"],
            },
            self._transcribe_audio,
            source="builtin",
        )

        r.register(
            "list_files",
            "List files in a directory.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: current dir)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (default: *)",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to recurse into subdirectories",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of paths to return",
                        "default": TOOL_DEFAULT_MAX_LIST_RESULTS,
                    },
                },
                "required": [],
            },
            self._list_files,
            source="builtin",
        )

        r.register(
            "memory_write",
            "Write or append content to the memory palace.",
            {
                "type": "object",
                "properties": {
                    "chapter": {
                        "type": "string",
                        "description": "Palace locus or legacy alias",
                    },
                    "name": {
                        "type": "string",
                        "description": "File name (without .md)",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                    "append": {
                        "type": "boolean",
                        "description": "Append instead of overwrite",
                        "default": False,
                    },
                },
                "required": ["chapter", "name", "content"],
            },
            self._memory_write,
            source="builtin",
        )

        r.register(
            "memory_read",
            "Read a memory chapter file.",
            {
                "type": "object",
                "properties": {
                    "chapter": {
                        "type": "string",
                        "description": "Palace locus or legacy alias",
                    },
                    "name": {
                        "type": "string",
                        "description": "File name (without .md)",
                    },
                },
                "required": ["chapter", "name"],
            },
            self._memory_read,
            source="builtin",
        )

        r.register(
            "memory_search",
            "Search across all memory files.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            self._memory_search,
            source="builtin",
        )

        r.register(
            "memory_index",
            "Show the memory palace index.",
            {"type": "object", "properties": {}, "required": []},
            self._memory_index,
            source="builtin",
        )

        r.register(
            "context_retrieve",
            (
                "Search long-term context memory for relevant information. "
                "Use to recall past facts, user preferences, project context, "
                "or any information consolidated from previous sessions."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to retrieve relevant context",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            self._context_retrieve,
            source="builtin",
        )

        r.register(
            "schedule_create",
            (
                "Create a persistent scheduled task. Use when the user asks for a reminder, "
                "a delayed follow-up, or a recurring future message. "
                "Choose `action_type=message` for a literal future message, "
                "`action_type=agent_task` for future agent work, or "
                "`action_type=system_job` for internal maintenance. "
                "For once: provide `at`. For interval: provide `every` and `unit`. "
                "For daily: provide `time_of_day`. For weekly: provide `day_of_week` and `time_of_day`."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short task name"},
                    "trigger_type": {
                        "type": "string",
                        "description": "one of: once, interval, daily, weekly",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Backward-compatible content field. Defaults to a literal message unless action_type=agent_task.",
                    },
                    "action_type": {
                        "type": "string",
                        "description": "one of: message, agent_task, system_job",
                    },
                    "message_text": {
                        "type": "string",
                        "description": "Literal message to send at the scheduled time",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Agent instruction to execute at the scheduled time",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Internal system job name, e.g. memory_tidy",
                    },
                    "timezone_name": {
                        "type": "string",
                        "description": "IANA timezone name like Asia/Shanghai",
                        "default": "UTC",
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO datetime for once triggers",
                    },
                    "every": {
                        "type": "integer",
                        "description": "Interval count for interval triggers",
                    },
                    "unit": {
                        "type": "string",
                        "description": "minutes|hours|days|weeks for interval triggers",
                    },
                    "time_of_day": {
                        "type": "string",
                        "description": "HH:MM for daily/weekly triggers",
                    },
                    "day_of_week": {
                        "type": "string",
                        "description": "mon|tue|wed|thu|fri|sat|sun for weekly triggers",
                    },
                    "delivery_mode": {
                        "type": "string",
                        "description": "optional override: standalone or channel",
                    },
                },
                "required": ["name", "trigger_type", "prompt"],
            },
            self._schedule_create,
            source="builtin",
        )

        r.register(
            "schedule_list",
            "List persistent scheduled tasks.",
            {"type": "object", "properties": {}, "required": []},
            self._schedule_list,
            source="builtin",
        )

        r.register(
            "schedule_delete",
            "Delete a persistent scheduled task by id.",
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id to delete",
                    }
                },
                "required": ["task_id"],
            },
            self._schedule_delete,
            source="builtin",
        )

        r.register(
            "web_search",
            (
                "Search the web using Tavily and return a list of results (title, url, snippet). "
                "Use for current events, facts that may have changed, or anything requiring live data. "
                "Requires a Tavily API key (set TAVILY_API_KEY or tavily_api_key in config)."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Maximum number of results to return (1-{WEB_SEARCH_MAX_RESULTS})",
                        "default": 5,
                    },
                    "region": {
                        "type": "string",
                        "description": "DuckDuckGo region code, e.g. 'wt-wt' (worldwide), 'us-en', 'cn-zh'. Default: 'wt-wt'",
                        "default": "wt-wt",
                    },
                },
                "required": ["query"],
            },
            self._web_search,
            source="builtin",
        )

        r.register(
            "web_fetch",
            (
                "Fetch the content of a URL and return it as plain text (HTML tags stripped). "
                "Use to read articles, documentation, or any web page whose URL you already know. "
                "Note: robots.txt is not checked; use responsibly."
            ),
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to fetch (must start with http:// or https://)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters of body text to return (default 8000)",
                        "default": 8000,
                    },
                    "raw_html": {
                        "type": "boolean",
                        "description": "Return raw HTML instead of extracted text (default false)",
                        "default": False,
                    },
                },
                "required": ["url"],
            },
            self._web_fetch,
            source="builtin",
        )

        r.register(
            "tavily_search",
            (
                "Search the web with Tavily and return normalized results. "
                "Useful for current events, news, and broader live-web research when a Tavily API key is configured."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Maximum number of results to return (1-{TAVILY_SEARCH_MAX_RESULTS})",
                        "default": 5,
                    },
                    "search_depth": {
                        "type": "string",
                        "description": "Tavily search depth: 'basic' or 'advanced'",
                        "default": "basic",
                    },
                    "include_answer": {
                        "type": "boolean",
                        "description": "Whether Tavily should include a synthesized short answer",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
            self._tavily_search,
            source="builtin",
        )

        if self._output_dir is not None:
            r.register(
                "clean_output",
                "Clean files from the output directory. Use max_age_hours=0 to remove all files.",
                {
                    "type": "object",
                    "properties": {
                        "max_age_hours": {
                            "type": "number",
                            "description": "Delete files older than N hours. 0 = delete all.",
                            "default": 0,
                        },
                        "subdir": {
                            "type": "string",
                            "description": "Only clean this subdirectory (e.g. 'screenshots'). Empty = entire output dir.",
                            "default": "",
                        },
                    },
                    "required": [],
                },
                self._clean_output,
                source="builtin",
            )

    # ── Web tools ──────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_html(raw: str) -> str:
        """Very lightweight HTML → plain-text: remove tags, decode entities."""
        # Remove <script> and <style> blocks entirely
        raw = re.sub(
            r"<(script|style)[^>]*>.*?</(script|style)>",
            " ",
            raw,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove all remaining tags
        raw = re.sub(r"<[^>]+>", " ", raw)
        # Decode HTML entities (e.g. &amp; &lt; &#39;)
        raw = html.unescape(raw)
        # Collapse whitespace
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()

    @staticmethod
    def _make_urllib_request(url: str, timeout: int = WEB_FETCH_TIMEOUT) -> bytes:
        """Open *url* with a browser-like User-Agent; return raw bytes."""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": WEB_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(WEB_FETCH_MAX_BYTES)

    @staticmethod
    def _make_tavily_request(
        api_key: str,
        query: str,
        max_results: int,
        search_depth: str,
        include_answer: bool,
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_answer": include_answer,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            TAVILY_SEARCH_URL,
            data=payload,
            method="POST",
            headers={
                "User-Agent": WEB_USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=WEB_FETCH_TIMEOUT) as resp:
            raw = resp.read(WEB_FETCH_MAX_BYTES)
        return json.loads(raw.decode("utf-8"))

    def _resolve_tavily_api_key(self) -> str:
        raw = self.registry.get_context("tavily_api_key", "")
        if isinstance(raw, str) and raw.startswith("$"):
            return os.environ.get(raw[1:], "")
        if raw:
            return str(raw)
        return os.environ.get("TAVILY_API_KEY", "")

    def _current_time(self, timezone_name: str = "local") -> dict[str, Any]:
        try:
            if timezone_name == "local":
                local_now = datetime.now().astimezone()
                label = "local"
            else:
                local_now = datetime.now(ZoneInfo(timezone_name))
                label = timezone_name
        except Exception:
            return self._error(
                f"Unknown timezone '{timezone_name}'",
                timezone=timezone_name,
            )

        utc_now = datetime.now(timezone.utc)
        return self._ok(
            timezone=label,
            local_time=local_now.isoformat(),
            utc_time=utc_now.isoformat(),
            unix_timestamp=int(local_now.timestamp()),
        )

    async def _web_fetch(
        self,
        url: str,
        max_chars: int = 8000,
        raw_html: bool = False,
    ) -> dict[str, Any]:
        """Fetch a single URL and return its text content."""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return self._error("URL must start with http:// or https://", url=url)
        max_chars = max(100, min(int(max_chars), WEB_FETCH_MAX_BYTES))
        try:
            raw_bytes = await asyncio.to_thread(self._make_urllib_request, url)
            # Decode – UTF-8 with replacement (never raises)
            raw_text = raw_bytes.decode("utf-8", errors="replace")

            if raw_html:
                body = raw_text[:max_chars]
                truncated = len(raw_text) > max_chars
            else:
                body = self._strip_html(raw_text)
                truncated = len(body) > max_chars
                body = body[:max_chars]

            return self._ok(
                url=url,
                content=body,
                truncated=truncated,
                chars=len(body),
            )
        except Exception as exc:
            return self._error(f"Fetch failed: {exc}", url=url)

    async def _web_search(
        self,
        query: str,
        max_results: int = 5,
        region: str = "wt-wt",
    ) -> dict[str, Any]:
        """Search the web through the Tavily backend under the generic tool name."""
        response = await self._tavily_search(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_answer=False,
        )
        if response.get("ok"):
            response["backend"] = "tavily"
            if region != "wt-wt":
                response["note"] = (
                    "web_search now uses Tavily; DuckDuckGo region hints are ignored."
                )
        return response

    async def _tavily_search(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        include_answer: bool = False,
    ) -> dict[str, Any]:
        api_key = self._resolve_tavily_api_key()
        if not api_key:
            return self._error(
                "Tavily API key not configured. Set TAVILY_API_KEY or registry context 'tavily_api_key'.",
                query=query,
            )

        max_results = max(1, min(int(max_results), TAVILY_SEARCH_MAX_RESULTS))
        search_depth = str(search_depth).strip().lower() or "basic"
        if search_depth not in {"basic", "advanced"}:
            return self._error(
                "search_depth must be 'basic' or 'advanced'",
                query=query,
                search_depth=search_depth,
            )

        try:
            payload = await asyncio.to_thread(
                self._make_tavily_request,
                api_key,
                query.strip(),
                max_results,
                search_depth,
                include_answer,
            )
        except Exception as exc:
            return self._error(f"Tavily search failed: {exc}", query=query)

        items = []
        for result in payload.get("results", [])[:max_results]:
            items.append(
                {
                    "title": result.get("title", ""),
                    "url": result.get("url", ""),
                    "snippet": result.get("content", ""),
                    "score": result.get("score"),
                }
            )

        response = self._ok(
            query=query,
            count=len(items),
            results=items,
        )
        if payload.get("answer"):
            response["answer"] = payload["answer"]
        return response

    def _ok(self, **payload: Any) -> dict[str, Any]:
        return {"ok": True, **payload}

    def _error(self, message: str, **payload: Any) -> dict[str, Any]:
        return {"ok": False, "error": message, **payload}

    def _resolve_tool_path(self, path: str) -> tuple[Path, str]:
        return resolve_workspace_path(
            path,
            workspace_root=self.workspace_root,
            output_dir=self._output_dir,
        )

    def _ensure_within_write_scope(self, path: Path) -> None:
        scope_entries = self.registry.get_context("write_scope") or []
        if not scope_entries:
            return
        allowed: list[str] = []
        for entry in scope_entries:
            scope_path, _root_kind = self._resolve_tool_path(str(entry))
            allowed.append(str(scope_path))
            if path_contains(scope_path, path):
                return
        raise ValueError(
            f"Path '{path}' is outside the sub-agent write scope. "
            f"Allowed paths: {', '.join(allowed)}"
        )

    async def _shell(
        self, command: str, timeout: int = 30, cwd: Optional[str] = None
    ) -> dict[str, Any]:
        # Security: block dangerous commands before spawning any subprocess.
        extra_blocked: list[str] = (
            self.registry.get_context("shell_blocked_commands") or []
        )
        import agent as agent_module

        _shell_command_is_blocked = agent_module._shell_command_is_blocked
        block_reason = _shell_command_is_blocked(command, extra_blocked)
        if block_reason:
            return self._error(
                f"Shell command rejected: {block_reason}", command=command
            )

        proc = None
        try:
            env = os.environ.copy()
            output_dir = self.registry.get_context("output_dir")
            if output_dir:
                env["AGENT_OUTPUT_DIR"] = str(output_dir)
            resolved_cwd = None
            if cwd:
                resolved_cwd, _root_kind = self._resolve_tool_path(cwd)
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=env,
                cwd=str(resolved_cwd) if resolved_cwd is not None else None,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            result = ""
            if out:
                result += f"STDOUT:\n{out}"
            if err:
                result += f"STDERR:\n{err}"
            result += f"\nExit code: {proc.returncode}"
            return self._ok(
                command=command,
                output=result or "(no output)",
                exit_code=proc.returncode,
            )
        except asyncio.TimeoutError:
            await self._terminate_process(proc)
            return self._error(
                f"Command timed out after {timeout}s",
                command=command,
                timed_out=True,
            )
        except asyncio.CancelledError:
            # B6: when the outer coroutine is cancelled (e.g. sub-agent timeout via
            # asyncio.wait_for), ensure the subprocess is killed so it doesn't linger
            # as a zombie process running under a detached session.
            await self._terminate_process(proc)
            raise
        except ValueError as e:
            return self._error(f"Invalid shell input: {e}", command=command)
        except Exception as e:
            return self._error(f"Shell command failed: {e}", command=command)

    def _send_file(self, path: str) -> dict[str, Any]:
        try:
            resolved, _root_kind = self._resolve_tool_path(path)
            if not resolved.exists():
                return self._error(f"'{path}' does not exist", path=str(resolved))
            if not resolved.is_file():
                return self._error(f"'{path}' is not a regular file", path=str(resolved))
            sink = _active_sink.get()
            if sink is None:
                return self._error(
                    "Current channel does not support sending files in this context.",
                    path=str(resolved),
                )
            if type(sink).queue_attachment is OutputSink.queue_attachment:
                return self._error(
                    "Current channel does not support sending files in this context.",
                    path=str(resolved),
                )
            sink.queue_attachment(resolved)
            return self._ok(path=str(resolved), queued=True)
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error queueing file: {e}")

    def _audio_transcription_command(self) -> str:
        raw = self.registry.get_context("audio_transcription_command", "")
        if isinstance(raw, str) and raw.startswith("$"):
            return os.environ.get(raw[1:], "")
        if raw:
            return str(raw)
        return os.environ.get("SIMPLE_AUDIO_TRANSCRIBE_COMMAND", "")

    async def _transcribe_audio(
        self,
        path: str,
        language: str = "",
        timeout: int = 300,
    ) -> dict[str, Any]:
        try:
            resolved, _root_kind = self._resolve_tool_path(path)
            if not resolved.exists():
                return self._error(f"'{path}' does not exist", path=str(resolved))
            if not resolved.is_file():
                return self._error(f"'{path}' is not a regular file", path=str(resolved))
            command_template = self._audio_transcription_command().strip()
            if not command_template:
                return self._error(
                    "Audio transcription is not configured. Set audio.transcription_command "
                    "in config.json or SIMPLE_AUDIO_TRANSCRIBE_COMMAND. Use {path} as the "
                    "audio-file placeholder.",
                    path=str(resolved),
                )
            language = str(language or "").strip()
            replacements = {
                "{path}": shlex.quote(str(resolved)),
                "{language}": shlex.quote(language) if language else "",
            }
            command = command_template
            for needle, value in replacements.items():
                command = command.replace(needle, value)
            if "{path}" not in command_template:
                command = f"{command} {shlex.quote(str(resolved))}"
            timeout = max(1, min(int(timeout), 900))
            proc = None
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ.copy(),
                    "AGENT_OUTPUT_DIR": str(self._output_dir),
                },
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            transcript = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()
            if proc.returncode != 0:
                return self._error(
                    f"Audio transcription failed with exit code {proc.returncode}",
                    path=str(resolved),
                    stderr=err[-4000:],
                    exit_code=proc.returncode,
                )
            return self._ok(
                path=str(resolved),
                transcript=transcript,
                stderr=err[-4000:] if err else "",
                exit_code=proc.returncode,
            )
        except asyncio.TimeoutError:
            await self._terminate_process(proc)
            return self._error(
                f"Audio transcription timed out after {timeout}s",
                path=path,
                timed_out=True,
            )
        except asyncio.CancelledError:
            await self._terminate_process(proc)
            raise
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error transcribing audio: {e}")

    async def _terminate_process(self, proc: Any) -> None:
        if proc is None:
            return
        try:
            if hasattr(os, "killpg") and getattr(proc, "pid", None):
                os.killpg(proc.pid, signal.SIGTERM)
            elif hasattr(proc, "terminate"):
                proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    return
        try:
            await asyncio.wait_for(proc.communicate(), timeout=1)
        except Exception:
            return

    @staticmethod
    def _is_binary_bytes(chunk: bytes) -> bool:
        return b"\x00" in chunk

    def _read_file(
        self, path: str, max_bytes: int = TOOL_DEFAULT_MAX_READ_BYTES
    ) -> dict[str, Any]:
        try:
            p, root_kind = self._resolve_tool_path(path)
            if not p.exists():
                return self._error(f"'{path}' does not exist", path=str(p))
            if not p.is_file():
                return self._error(f"'{path}' is not a regular file", path=str(p))
            max_bytes = max(1, min(int(max_bytes), TOOL_DEFAULT_MAX_READ_BYTES))
            with open(p, "rb") as f:
                chunk = f.read(max_bytes + 1)
            if self._is_binary_bytes(chunk):
                if root_kind == "output_dir":
                    return self._ok(
                        path=str(p),
                        content="",
                        binary=True,
                        truncated=len(chunk) > max_bytes,
                        bytes_read=min(len(chunk), max_bytes),
                        message=(
                            "Binary generated artifact in output directory; "
                            "use the path directly or let the channel send the file."
                        ),
                    )
                return self._error(f"'{path}' appears to be binary", path=str(p))
            text = chunk[:max_bytes].decode("utf-8", errors="replace")
            return self._ok(
                path=str(p),
                content=text,
                binary=False,
                truncated=len(chunk) > max_bytes,
                bytes_read=min(len(chunk), max_bytes),
            )
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error reading file: {e}")

    def _write_file(
        self,
        path: str,
        content: str,
        max_bytes: int = TOOL_DEFAULT_MAX_WRITE_BYTES,
    ) -> dict[str, Any]:
        try:
            p, _root_kind = self._resolve_tool_path(path)
            self._ensure_within_write_scope(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = content.encode("utf-8")
            max_bytes = max(1, min(int(max_bytes), TOOL_DEFAULT_MAX_WRITE_BYTES))
            if len(payload) > max_bytes:
                return self._error(
                    f"Content size {len(payload)} exceeds limit {max_bytes} bytes",
                    path=str(p),
                )
            tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(p)
            return self._ok(path=str(p), bytes_written=len(payload))
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error writing file: {e}")

    def _list_files(
        self,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        max_results: int = TOOL_DEFAULT_MAX_LIST_RESULTS,
    ) -> dict[str, Any]:
        try:
            p, _root_kind = self._resolve_tool_path(path)
            if not p.exists():
                return self._error(f"'{path}' does not exist", path=str(p))
            if not p.is_dir():
                return self._error(f"'{path}' is not a directory", path=str(p))
            max_results = max(1, min(int(max_results), TOOL_DEFAULT_MAX_LIST_RESULTS))
            iterator = p.rglob(pattern) if recursive else p.glob(pattern)
            results = []
            truncated = False
            for candidate in iterator:
                if len(results) >= max_results:
                    truncated = True
                    break
                results.append(str(candidate.resolve()))
            return self._ok(
                path=str(p),
                pattern=pattern,
                recursive=recursive,
                items=sorted(results),
                truncated=truncated,
                count=len(results),
            )
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error listing files: {e}")

    def _memory_write(
        self, chapter: str, name: str, content: str, append: bool = False
    ) -> dict[str, Any]:
        self.memory.write(chapter, name, content, append=append)
        normalized = self.chapter_normalizer(chapter)
        return self._ok(
            action="append" if append else "write",
            path=f"{normalized}/{name}",
            bytes=len(content.encode("utf-8")),
        )

    def _memory_read(self, chapter: str, name: str) -> dict[str, Any]:
        content = self.memory.read(chapter, name)
        normalized = self.chapter_normalizer(chapter)
        if not content:
            return self._error(f"No memory file: {normalized}/{name}")
        return self._ok(path=f"{normalized}/{name}", content=content)

    def _memory_search(self, query: str, top_k: int = 10) -> dict[str, Any]:
        results = self.memory.search(query)
        top_k = max(1, min(int(top_k), 20))
        items = results[:top_k]
        return self._ok(query=query, count=len(items), items=items)

    def _memory_index(self) -> dict[str, Any]:
        return self._ok(content=self.memory.read_index())

    def _context_retrieve(self, query: str, top_k: int = 5) -> dict[str, Any]:
        if self.context_manager is None:
            return self._error("Context manager not available.")
        result = self.context_manager.retrieve_context(query, top_k=top_k)
        sections = [s for s in result.split("\n\n") if s.strip()] if result else []
        return self._ok(
            query=query, count=len(sections), content=result, sections=sections
        )

    def _schedule_store(self) -> SchedulerStore:
        from agent.scheduler import SchedulerStore

        return SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)

    def _schedule_target(self, delivery_mode: Optional[str] = None):
        from agent.scheduler import DeliveryTarget

        active = _active_schedule_target.get()
        if delivery_mode == "standalone":
            return "standalone", DeliveryTarget.standalone()
        if delivery_mode == "channel" and active:
            return "channel", DeliveryTarget.channel(
                target_type=str(active.get("target_type", "feishu_chat")),
                chat_id=str(active["chat_id"]),
                chat_type=str(active.get("chat_type", "p2p")),
            )
        if active:
            return "channel", DeliveryTarget.channel(
                target_type=str(active.get("target_type", "feishu_chat")),
                chat_id=str(active["chat_id"]),
                chat_type=str(active.get("chat_type", "p2p")),
            )
        return "standalone", DeliveryTarget.standalone()

    def _schedule_trigger(
        self,
        *,
        trigger_type: str,
        timezone_name: str,
        at: Optional[str] = None,
        every: Optional[int] = None,
        unit: Optional[str] = None,
        time_of_day: Optional[str] = None,
        day_of_week: Optional[str] = None,
    ):
        from agent.scheduler import TriggerSpec

        kind = str(trigger_type).strip().lower()
        if kind == "once":
            if not at:
                raise ValueError("`at` is required for once triggers")
            return TriggerSpec.once(at, timezone_name)
        if kind == "interval":
            if every is None or not unit or not at:
                raise ValueError("`every`, `unit`, and `at` are required for interval triggers")
            return TriggerSpec.interval(every, unit, at, timezone_name)
        if kind == "daily":
            if not time_of_day:
                raise ValueError("`time_of_day` is required for daily triggers")
            return TriggerSpec.daily(time_of_day, timezone_name)
        if kind == "weekly":
            if not day_of_week or not time_of_day:
                raise ValueError("`day_of_week` and `time_of_day` are required for weekly triggers")
            return TriggerSpec.weekly(day_of_week, time_of_day, timezone_name)
        raise ValueError(f"Unsupported trigger_type '{trigger_type}'")

    def _schedule_create(
        self,
        name: str,
        trigger_type: str,
        prompt: str = "",
        action_type: str = "message",
        message_text: Optional[str] = None,
        instruction: Optional[str] = None,
        job_name: Optional[str] = None,
        timezone_name: str = "UTC",
        at: Optional[str] = None,
        every: Optional[int] = None,
        unit: Optional[str] = None,
        time_of_day: Optional[str] = None,
        day_of_week: Optional[str] = None,
        delivery_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        trigger = self._schedule_trigger(
            trigger_type=trigger_type,
            timezone_name=timezone_name,
            at=at,
            every=every,
            unit=unit,
            time_of_day=time_of_day,
            day_of_week=day_of_week,
        )
        resolved_mode, target = self._schedule_target(delivery_mode)
        from agent.scheduler import NewScheduledTask

        normalized_action = str(action_type or "message").strip().lower()
        task_kind = "message"
        payload: dict[str, Any]
        if normalized_action == "message":
            text = str(message_text or prompt).strip()
            if not text:
                raise ValueError("`message_text` is required for message actions")
            task_kind = "message"
            payload = {"message_text": text}
            summary_text = (
                f"已设置好定时任务！将在 {trigger.initial_run_at().isoformat()} 发送消息“{text}”。"
                if trigger.initial_run_at()
                else f"已设置好定时任务，会发送消息“{text}”。"
            )
        elif normalized_action == "agent_task":
            text = str(instruction or prompt).strip()
            if not text:
                raise ValueError("`instruction` is required for agent_task actions")
            task_kind = "agent_prompt"
            payload = {"prompt": text}
            summary_text = (
                f"已设置好定时任务！将在 {trigger.initial_run_at().isoformat()} 执行任务：{text}"
                if trigger.initial_run_at()
                else f"已设置好定时任务，会执行任务：{text}"
            )
        elif normalized_action == "system_job":
            text = str(job_name or "").strip()
            if not text:
                raise ValueError("`job_name` is required for system_job actions")
            task_kind = "system_job"
            payload = {"job_name": text}
            summary_text = (
                f"已设置好系统定时任务！将在 {trigger.initial_run_at().isoformat()} 执行 {text}。"
                if trigger.initial_run_at()
                else f"已设置好系统定时任务，会执行 {text}。"
            )
        else:
            raise ValueError(f"Unsupported action_type '{action_type}'")

        store = self._schedule_store()
        try:
            new_task = NewScheduledTask(
                name=name,
                kind=task_kind,
                trigger=trigger,
                payload=payload,
                delivery_mode=resolved_mode,
                delivery_target=target,
            )
            task = store.find_matching_task(new_task)
            existing = task is not None
            if task is None:
                task = store.create_task(new_task)
        finally:
            store.close()
        return self._ok(
            task={
                "id": task.id,
                "name": task.name,
                "kind": task.kind,
                "delivery_mode": task.delivery_mode,
                "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                "db_path": str(shared.SCHEDULER_DB_FILE),
                "existing": existing,
            },
            summary_text=summary_text,
        )

    def _schedule_list(self) -> dict[str, Any]:
        store = self._schedule_store()
        try:
            tasks = store.list_tasks()
        finally:
            store.close()
        return self._ok(
            count=len(tasks),
            items=[
                {
                    "id": task.id,
                    "name": task.name,
                    "kind": task.kind,
                    "delivery_mode": task.delivery_mode,
                    "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                    "enabled": task.enabled,
                }
                for task in tasks
            ],
        )

    def _schedule_delete(self, task_id: str) -> dict[str, Any]:
        store = self._schedule_store()
        try:
            store.delete_task(task_id)
        finally:
            store.close()
        return self._ok(task_id=task_id, deleted=True)

    def _clean_output(
        self, max_age_hours: float = 0, subdir: str = ""
    ) -> dict[str, Any]:
        if self._output_dir is None:
            return self._error("Output directory not configured")
        target = self._output_dir / subdir if subdir else self._output_dir
        if not target.is_dir():
            return self._ok(deleted=0, message=f"Directory does not exist: {target}")
        now = time.time()
        deleted = 0
        errors: list[str] = []
        for f in target.rglob("*"):
            if not f.is_file():
                continue
            if max_age_hours > 0 and (now - f.stat().st_mtime) < max_age_hours * 3600:
                continue
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                errors.append(f"{f.name}: {e}")
        for d in sorted((d for d in target.rglob("*") if d.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        result = self._ok(deleted=deleted, target=str(target))
        if errors:
            result["errors"] = errors[:10]
        return result


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
        params = mcp.StdioServerParameters(
            command=command,
            args=list(cfg.get("args", []) or []),
            env=merged_env or None,
            cwd=cfg.get("cwd"),
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

        async def _call_mcp_tool(**kwargs):
            result = await session.call_tool(original_name, arguments=kwargs or None)
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
