from __future__ import annotations

import asyncio
import json
import os
import signal
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


TOOL_DEFAULT_MAX_READ_BYTES = 64 * 1024
TOOL_DEFAULT_MAX_WRITE_BYTES = 256 * 1024
TOOL_DEFAULT_MAX_LIST_RESULTS = 100


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable
    source: str = "runtime"


class ToolRegistry:
    """Central registry for all tools."""

    def __init__(self, console: Optional[Any] = None):
        self._tools: dict[str, ToolDef] = {}
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
    ):
        if name in self._tools:
            existing = self._tools[name]
            if not replace or existing.source != source:
                raise ValueError(
                    f"Tool '{name}' is already registered by source '{existing.source}'"
                )
        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            source=source,
        )

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

    async def call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name not in self._tools:
            return f"Error: tool '{tool_name}' not found"
        try:
            fn = self._tools[tool_name].fn
            if asyncio.iscoroutinefunction(fn):
                result = await fn(**tool_input)
            else:
                result = fn(**tool_input)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return "" if result is None else str(result)
        except Exception as e:
            if self.console is not None:
                self.console.print(
                    f"[yellow]Tool '{tool_name}' failed: {e}\n{traceback.format_exc()}[/yellow]"
                )
            return f"Error calling tool '{tool_name}': {e}"

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def unregister_by_source_prefix(self, prefix: str) -> None:
        for name in [n for n, tool in self._tools.items() if tool.source.startswith(prefix)]:
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
    ):
        self.memory = memory
        self.registry = registry
        self.context_manager = context_manager
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.chapter_normalizer = chapter_normalizer or (lambda chapter: str(chapter))
        self._register()

    def _register(self):
        r = self.registry

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
                    "chapter": {"type": "string", "description": "Palace locus or legacy alias"},
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
                    "chapter": {"type": "string", "description": "Palace locus or legacy alias"},
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

    def _ok(self, **payload: Any) -> dict[str, Any]:
        return {"ok": True, **payload}

    def _error(self, message: str, **payload: Any) -> dict[str, Any]:
        return {"ok": False, "error": message, **payload}

    def _resolve_workspace_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError(
                f"Path '{path}' is outside the workspace root '{self.workspace_root}'"
            )
        return resolved

    async def _shell(self, command: str, timeout: int = 30) -> dict[str, Any]:
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
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
                command=command, output=result or "(no output)", exit_code=proc.returncode
            )
        except asyncio.TimeoutError:
            await self._terminate_process(proc)
            return self._error(
                f"Command timed out after {timeout}s",
                command=command,
                timed_out=True,
            )
        except Exception as e:
            return self._error(str(e), command=command)

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

    def _read_file(self, path: str, max_bytes: int = TOOL_DEFAULT_MAX_READ_BYTES) -> dict[str, Any]:
        try:
            p = self._resolve_workspace_path(path)
            if not p.exists():
                return self._error(f"'{path}' does not exist", path=str(p))
            if not p.is_file():
                return self._error(f"'{path}' is not a regular file", path=str(p))
            max_bytes = max(1, min(int(max_bytes), TOOL_DEFAULT_MAX_READ_BYTES))
            with open(p, "rb") as f:
                chunk = f.read(max_bytes + 1)
            if self._is_binary_bytes(chunk):
                return self._error(f"'{path}' appears to be binary", path=str(p))
            text = chunk[:max_bytes].decode("utf-8", errors="replace")
            return self._ok(
                path=str(p),
                content=text,
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
            p = self._resolve_workspace_path(path)
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
            p = self._resolve_workspace_path(path)
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
        return self._ok(query=query, count=len(sections), content=result, sections=sections)
