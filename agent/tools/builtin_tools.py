from __future__ import annotations

import asyncio
import contextlib
import html
import json
import os
import re
import shutil
import shlex
import signal
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from agent import shared
from agent.core.output import OutputSink, _active_sink
from agent.exec import ExecRequest, provider_from
from agent.exec.subprocess import OUTPUT_MAX_BYTES
from agent.pathing import path_contains, resolve_workspace_path
from agent.security.network import fetch_public_http_url
from agent.security.filesystem_sandbox import (
    SANDBOX_MODE_NONE,
    SandboxUnavailableError,
    ShellSandboxRequest,
    build_sandbox_command,
    effective_sandbox_mode,
    looks_like_sandbox_denial,
    narrow_alternatives_hint,
    new_scratch_dir,
    release_scratch_dir,
)
from agent.security.shell import shell_command_uses_shell_features
from agent.tools.files import (
    FileAccessPolicy,
    FileProvider,
    FileService,
    _normalize_write_scope,
    write_scope_allows,
)

from .executor import report_tool_progress
from .runtime import ToolRegistry


# ── Constants ─────────────────────────────────────────────────────────────────

_atomic_write_text = shared._atomic_write_text

WEB_FETCH_MAX_BYTES = 512 * 1024
WEB_FETCH_TIMEOUT = 20

#: How much of an upstream step's output ``read_step_output`` hands back.
#:
#: Bounded because the tool exists to feed a model's context, and a step's
#: output is unbounded: a run that reads three upstream reports whole can spend
#: its entire budget before it starts working.  The default is a comfortable
#: read; the ceiling is what a caller who has asked for "all of it" gets, with
#: the path returned alongside so the rest is one more call away -- and with
#: ``READ_STEP_OUTPUT_BUDGET_SHARE`` applied on top, so "all of it" cannot mean
#: "all of the window".
READ_STEP_OUTPUT_DEFAULT_CHARS = 8000
READ_STEP_OUTPUT_MAX_CHARS = 40000
#: The largest share of a run's input budget one read may spend.
#:
#: A flat character ceiling cannot know whether it is asking for a tenth of the
#: budget or three quarters of it, because the cost of a read is measured in the
#: units the wall is: CJK text is about one token per character, so 40000
#: characters of Chinese is 40000 tokens.  A quarter is a read that cannot by
#: itself exhaust the window, and a run that wants more reads it in pieces.
READ_STEP_OUTPUT_BUDGET_SHARE = 4
WEB_SEARCH_MAX_RESULTS = 10
TAVILY_SEARCH_MAX_RESULTS = 10
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
WEB_USER_AGENT = (
    "Mozilla/5.0 (compatible; PersonalAgent/1.0; +https://github.com/your/agent)"
)


def _workflow_ancestors(steps, key: str) -> set[str]:
    """Every step ``key`` transitively stands on.

    Read from the authored graph rather than from the triggers, because the
    question is about the shape of the chain: a step may read what it stands
    on, and nothing else.  A step beside it has not run for this round, and a
    step below it has not run at all -- reading either returns an earlier
    round's text, which is exactly the confusion the "not your own step" rule
    exists to prevent, one edge further out.
    """
    depends = {
        str(step.key).strip(): [str(item).strip() for item in (step.depends_on or [])]
        for step in steps
    }
    seen: set[str] = set()
    pending = list(depends.get(str(key).strip(), []))
    while pending:
        current = pending.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        pending.extend(depends.get(current, []))
    return seen


# Model-facing guidance attached to every confirmation-required shell error.
_SHELL_CONFIRMATION_GUIDANCE = (
    "Only high-risk commands (disk/system destruction, destructive options, "
    "shell operators, pipe-to-shell patterns) require human approval; "
    "medium-risk commands run without asking. An interactive terminal shows "
    "an approval menu automatically; in non-terminal channels "
    "(Feishu/gateway), show the exact command to the user and ask them to "
    "reply “同意” (or run /confirm <token>). After approval, retry the "
    "byte-identical command within 5 minutes — do not modify it and do not "
    "route around the shell tool."
)


def _looks_like_plugin(dir_path: Path) -> bool:
    """Return True if *dir_path* contains at least one recognisable plugin marker."""
    cc_plugin = dir_path / ".claude-plugin"
    if (dir_path / "plugin.json").exists() or (cc_plugin / "plugin.json").exists():
        return True
    if (cc_plugin / "marketplace.json").exists():
        return True
    if (dir_path / "__init__.py").exists():
        return True
    if (dir_path / "skills").is_dir() or (dir_path / "commands").is_dir():
        return True
    return False


def _canonicalize_plugin_source(source: str) -> str:
    """Normalize a plugin repo reference to the standard https transport.

    ``git://`` (port 9418) is a legacy transport that is frequently blocked
    by home/corporate networks and carries no credentials, so it is
    rewritten to the equivalent https URL before cloning.  https, ssh
    (``git@host:path``) and local paths are left untouched — they are
    legitimate authenticated/local transports.
    """
    if source.startswith("git://"):
        return "https://" + source[len("git://") :]
    return source


def _plugin_executable_content(target: Path) -> str:
    """Describe executable content a plugin ships ("" = declarative only).

    Installations that run code (Python entry point, MCP server, hooks) are
    equivalent to executing arbitrary code and must pass human confirmation
    before activation.
    """
    if (target / "__init__.py").exists():
        return "Python 入口 (__init__.py)"
    if (target / ".mcp.json").exists():
        return "MCP server (.mcp.json)"
    for manifest in (
        target / "plugin.json",
        target / ".claude-plugin" / "plugin.json",
    ):
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if data.get("mcp_servers") or data.get("mcpServers"):
            return "MCP server (plugin.json)"
        if data.get("hooks"):
            return "hooks (plugin.json)"
    hooks_dir = target / ".claude-plugin" / "hooks"
    if hooks_dir.is_dir() and any(hooks_dir.iterdir()):
        return "hooks (.claude-plugin/hooks)"
    return ""


_PLUGIN_INSTALL_LOCK: Optional[asyncio.Lock] = None


def _plugin_install_lock() -> asyncio.Lock:
    """One lock serializes install/replace transactions process-wide."""
    global _PLUGIN_INSTALL_LOCK
    if _PLUGIN_INSTALL_LOCK is None:
        _PLUGIN_INSTALL_LOCK = asyncio.Lock()
    return _PLUGIN_INSTALL_LOCK


class _PluginInstallError(Exception):
    """Internal control flow carrying the structured install failure."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("error") or "plugin install failed"))


def _resolve_user_plugin_target(name: str, root: Path | None = None) -> Path:
    if name != name.strip() or re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?\Z", name
    ) is None:
        raise ValueError("plugin name must be a canonical slug")

    root = (root or shared.USER_PLUGINS_DIR).expanduser().resolve(strict=False)
    target = (root / name).resolve(strict=False)
    if target.parent != root:
        raise ValueError(
            "plugin target must be a direct child of USER_PLUGINS_DIR"
        )
    return target


from .runtime import (  # noqa: E402
    _active_run_self_report,
    _active_schedule_target,
    _active_signal_context,
)
from agent.verification import VERDICT_FAILED  # noqa: E402


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
        file_service: Optional[FileProvider] = None,
    ):
        self.memory = memory
        self.registry = registry
        self.context_manager = context_manager
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        # The directory a person actually chose, kept apart from the cwd
        # fallback above.  ``_active_workspace_root`` is happy to answer with
        # the process working directory, which is fine for reading and never
        # acceptable as the directory an unattended *write* task is pinned to.
        self._declared_workspace_root = (
            Path(workspace_root).expanduser().resolve(strict=False)
            if workspace_root is not None
            else None
        )
        self.chapter_normalizer = chapter_normalizer or (lambda chapter: str(chapter))
        self._output_dir = output_dir
        self._injected_file_service = file_service
        self._fallback_file_service: FileProvider | None = None
        self._cached_schedule_store: Any = None
        self._register()

    @property
    def _file_service(self) -> FileProvider:
        """The file provider, resolved late so injection always wins.

        Resolved on use rather than in ``__init__`` because the registry
        context is not necessarily populated by the time the tools are built —
        bootstrap injects the constructor argument first and sets the context
        afterwards.  Reading the context eagerly would silently miss a
        provider that arrives a few lines later, and the fallback below would
        take its place: a second service, with default policy, that nobody
        asked for.
        """
        # Constructor injection remains the default for ordinary CLI calls;
        # a forked session may override it through ToolRegistry's ContextVar.
        override = getattr(self.registry, "_context_override", None)
        active_override = override.get() if override is not None else None
        if isinstance(active_override, dict) and "file_service" in active_override:
            return active_override["file_service"]
        if self._injected_file_service is not None:
            return self._injected_file_service
        from_context = self.registry.get_context("file_service")
        if from_context is not None:
            return from_context
        if self._fallback_file_service is None:
            # Default policy mirrors the startup defaults: a readable,
            # non-writable workspace plus an always-usable output_dir.
            # The write_scope is resolved per call from the active registry
            # context so sub-agent scopes apply even though the policy is
            # immutable.
            self._fallback_file_service = FileService(
                FileAccessPolicy(
                    workspace_root=self.workspace_root,
                    output_root=self._process_output_dir(),
                ),
                write_scope=lambda: self.registry.get_context("write_scope") or (),
            )
        return self._fallback_file_service

    def _active_workspace_root(self) -> Path:
        raw = self.registry.get_context("workspace_root")
        if raw:
            return Path(str(raw)).expanduser().resolve(strict=False)
        return self.workspace_root.expanduser().resolve(strict=False)

    def _chosen_workspace_root(self) -> Optional[Path]:
        """The workspace a person picked, or ``None`` when only cwd is left.

        Read the registry context first: a forked session carries the
        directory the user opened, and that is the only value that counts as
        *chosen*.  The process working directory is deliberately **not** a
        fallback here -- see ``_schedule_create``, which refuses a
        write-granting profile rather than pinning it to a directory nobody
        can predict from the task's own definition.
        """
        raw = self.registry.get_context("workspace_root")
        if raw:
            return Path(str(raw)).expanduser().resolve(strict=False)
        return self._declared_workspace_root

    def _process_output_dir(self) -> Path:
        raw = self.registry.get_context("output_dir")
        if raw:
            output_dir = Path(str(raw)).expanduser().resolve(strict=False)
        elif self._output_dir is not None:
            output_dir = Path(self._output_dir).expanduser().resolve(strict=False)
        else:
            output_dir = shared.DEFAULT_OUTPUT_DIR.expanduser().resolve(strict=False)
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _resource_home(self) -> Path:
        raw = self.registry.get_context("resource_home")
        return Path(str(raw)).expanduser().resolve(strict=False) if raw else shared.AGENT_HOME

    def _sandbox_dir(self) -> Path:
        """Dedicated scratch directory for shell commands.

        Isolated from both the workspace (repo) and the output directory so
        that downloads, clones, and other generated artifacts never pollute
        the project workspace.
        """
        output_dir = self._process_output_dir()
        sandbox = output_dir / "sandbox"
        sandbox.mkdir(parents=True, exist_ok=True)
        return sandbox

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
            "Execute a shell command and return stdout/stderr. Use for system operations, running scripts, etc. "
            "Medium-risk commands (ssh/curl/rm/interpreters and similar) run automatically. High-risk commands "
            "(disk/system destruction, destructive options, shell operators, pipe-to-shell patterns) require "
            "human approval: an interactive terminal shows an approval menu automatically; in non-terminal "
            "channels, show the exact command to the user and ask them to reply “同意” (or run /confirm <token>), "
            "then retry the identical command string after approval.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Explain what this exact command will do and why running it is necessary for the user's task.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 300, max 3600). Use 600+ for large downloads or long builds.",
                        "default": 300,
                    },
                    "root": {
                        "type": "string",
                        "enum": ["output_dir", "workspace"],
                        "default": "workspace",
                        "description": "Security domain for this shell call. workspace (default) is the selected project directory; output_dir is for generated deliverables, downloads, and temporary files.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory. Relative paths resolve inside root (default: the root itself). Absolute paths are accepted only when inside the workspace or output directory.",
                    },
                },
                "required": ["command", "intent"],
            },
            self._shell,
            source="builtin",
        )

        r.register(
            "read_file",
            "Read a bounded line window of a UTF-8 text file. Returns an exact sha256 revision of the file; pass that revision back as expected_revision to write_file or edit_file before mutating. Use root=workspace for repository files and root=output_dir for generated artifacts.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain (default workspace)",
                        "default": "workspace",
                    },
                    "path": {
                        "type": "string",
                        "description": "Root-relative file path",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "One-based first line to return (default 1)",
                        "default": 1,
                    },
                    "line_count": {
                        "type": "integer",
                        "description": "Maximum number of complete lines to return (default 200)",
                        "default": 200,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            self._read_file,
            source="builtin",
        )

        r.register(
            "write_file",
            "Atomically create or overwrite a whole UTF-8 text file under an explicit root. mode=create requires the target to be absent; mode=overwrite requires the exact expected_revision from read_file and preserves the file mode and UTF-8 BOM.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain: workspace requires startup write enablement plus write_scope; output_dir is always writable",
                    },
                    "path": {"type": "string", "description": "File path"},
                    "mode": {
                        "type": "string",
                        "enum": ["create", "overwrite"],
                        "description": "create fails if the target exists; overwrite requires expected_revision",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                    "expected_revision": {
                        "type": "string",
                        "description": "Required for overwrite: the revision returned by a previous read_file",
                    },
                },
                "required": ["root", "path", "mode", "content"],
                "additionalProperties": False,
            },
            self._write_file,
            authorizer=self._file_mutation_authorizer,
            source="builtin",
        )

        r.register(
            "send_file",
            "Queue an existing file to be sent back to the current user/channel when the turn completes. Use after generating or locating a file the user asked to receive. For generated images, queue each final deliverable once and omit source grids, previews, contact sheets, and other intermediate files unless the user explicitly requests them.",
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
            "List entries under an explicit root with bounded, deterministic results and an opaque cursor for continuation. Never follows symlinks.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain (default workspace)",
                        "default": "workspace",
                    },
                    "path": {
                        "type": "string",
                        "description": "Root-relative directory path (default: .)",
                        "default": ".",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Basename glob pattern with no path separator (default: *)",
                        "default": "*",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to recurse into subdirectories",
                        "default": False,
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque continuation token from a previous truncated listing; must be used with identical request parameters",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of paths to return",
                        "default": 1000,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            self._list_files,
            source="builtin",
        )

        r.register(
            "edit_file",
            "Apply an ordered batch of exact, non-overlapping text replacements to one snapshot revision. Every replacement must declare the exact expected occurrence count; any mismatch leaves the file byte-for-byte unchanged. Preserves the original BOM, mode, and all bytes outside replaced spans.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain: workspace requires startup write enablement plus write_scope; output_dir is always writable",
                    },
                    "path": {"type": "string", "description": "Root-relative file path"},
                    "expected_revision": {
                        "type": "string",
                        "description": "Exact revision returned by a previous read_file",
                    },
                    "replacements": {
                        "type": "array",
                        "description": "Ordered exact replacements; each later replacement may match text inserted by an earlier one",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                                "expected_count": {"type": "integer"},
                            },
                            "required": ["old_text", "new_text", "expected_count"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["root", "path", "expected_revision", "replacements"],
                "additionalProperties": False,
            },
            self._edit_file,
            authorizer=self._file_mutation_authorizer,
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
            "set_identity",
            "Set who the assistant is (or record who the user is). Use this "
            "whenever the user names, renames, or redefines the persona — the "
            "newest setting replaces the old one.",
            {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "enum": ["assistant", "user"],
                        "description": "Whose identity this is. Defaults to assistant.",
                        "default": "assistant",
                    },
                    "name": {
                        "type": "string",
                        "description": "What they are called. Omit to leave unchanged.",
                    },
                    "role": {
                        "type": "string",
                        "description": "Their role, e.g. '编程助手'. Omit to leave unchanged.",
                    },
                    "persona": {
                        "type": "string",
                        "description": (
                            "Free-text persona/setting in the user's own wording, "
                            "e.g. '一位可爱的小女孩'. Omit to leave unchanged."
                        ),
                    },
                },
                "required": [],
            },
            self._set_identity,
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
            "memory_clear",
            (
                "Permanently clear all durable long-term memory, retrievable "
                "conversation history, facts, working-state snapshots, and pending "
                "memory consolidation. Use when the user asks to clear/erase/forget "
                "all memory. This tool asks the human for interactive approval itself; "
                "do not use shell/SQLite and do not ask for a separate chat confirmation."
            ),
            {"type": "object", "properties": {}, "required": []},
            self._memory_clear,
            source="builtin",
        )

        r.register(
            "context_retrieve",
            (
                "Search long-term context memory for relevant information. "
                "Use to recall past facts, user preferences, project context, "
                "or any information consolidated from previous sessions. "
                "ALSO use it when the context already injected into this turn "
                "does not contain what you need — do not tell the user you "
                "have no record until you have searched.\n"
                "The index is LEXICAL, not semantic: it finds memories that "
                "share WORDS with your query, not ones that merely mean the "
                "same thing. So pass SEVERAL phrasings in `queries`, not just "
                "the user's question verbatim:\n"
                "- the other language (a memory written in English will not "
                "match a Chinese question, and vice versa)\n"
                "- the concrete words the memory itself would use "
                "(for 'which company do I work at', try the employer's name)\n"
                "- distinctive nouns rather than the question's phrasing"
            ),
            {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Two to four different phrasings of what you are "
                            "looking for. More phrasings find more; they are "
                            "unioned, so a wrong guess costs nothing."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Single query (legacy). Prefer `queries` — one "
                            "phrasing is what makes lexical search miss."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": [],
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
                "For once: provide `at`. For interval: provide `every`, `unit`, and `at` (anchor). "
                "For daily or weekdays: provide `time_of_day`. For weekly: provide `day_of_week` and `time_of_day`. "
                "For monthly: provide `day_of_month` and `time_of_day`. "
                "For signal: provide `signal_name` -- the task runs when that signal is emitted, "
                "which is how a step can be made to follow another task instead of a clock. "
                "The run is unattended: set `permission_profile` to say what it may do "
                "(inherit/read_only/workspace_write), and pass `workspace_root` when the "
                "task should work in a directory other than the current session's."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short task name"},
                    "trigger_type": {
                        "type": "string",
                        "description": "one of: once, interval, daily, weekly, weekdays, monthly, signal",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": (
                            "Signal to wait for when trigger_type=signal. A "
                            "finished task's own signal is "
                            "task:<task_id>:succeeded, task:<task_id>:failed, "
                            "task:<task_id>:cancelled or task:<task_id>:interrupted "
                            "-- use schedule_list to find task ids. For a signal "
                            "you will emit yourself, check list_signals for the "
                            "name already in use."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Backward-compatible content field. Defaults to a literal message unless action_type=agent_task.",
                        "default": "",
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
                    "day_of_month": {
                        "type": "integer",
                        "description": "1-31 for monthly triggers; months without that date are skipped",
                    },
                    "delivery_mode": {
                        "type": "string",
                        "description": "optional override: standalone or channel",
                    },
                    "permission_profile": {
                        "type": "string",
                        "description": (
                            "What this unattended run may do. one of: inherit "
                            "(no overrides; anything needing confirmation is refused), "
                            "read_only (never write the workspace), "
                            "workspace_write (write inside the workspace and run commands "
                            "without asking -- also requires `workspace_root`). "
                            "Defaults to inherit."
                        ),
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": (
                            "Absolute path of the project folder the run works in. "
                            "Required for permission_profile=workspace_write; "
                            "defaults to the current session's folder."
                        ),
                    },
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "What has to be true for a run of this task to count as a "
                            "success, in plain sentences. Read by whoever looks at a "
                            "failed run, and given to the run itself so it knows what "
                            "it is aiming at. Omit for a task with no checkable "
                            "outcome, which then succeeds as soon as it delivers."
                        ),
                    },
                    "verify_command": {
                        "type": "string",
                        "description": (
                            "Optional command that decides success by its exit code -- "
                            "run in the task's folder after the work, 0 means it "
                            "worked. Use this whenever success is mechanically "
                            "checkable (a test suite, a build, a linter, a script that "
                            "validates the output); it is the only evidence a run "
                            "cannot talk its way out of. Must be a single low-risk "
                            "command: no pipes, redirection, `rm`, or inline "
                            "interpreters such as `python -c`. A command that cannot "
                            "be run is refused here, while you can still fix it."
                        ),
                    },
                },
                "required": ["name", "trigger_type"],
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
            (
                "Delete a persistent scheduled task by id, along with its run "
                "history. Refused for a task that is running (cancel the run "
                "first) and for a step of a workflow that still exists -- a step "
                "is the graph's to remove, so delete the workflow or edit it "
                "instead. A step left behind by an already-deleted workflow is "
                "an ordinary task and can be deleted here."
            ),
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
            "workflow_create",
            (
                "Create a chain of scheduled tasks, where each step runs only after the "
                "steps it depends on have succeeded. Use this when a request is really "
                "several jobs in an order -- fetch, then transform, then report -- rather "
                "than one job. Do not chain steps with schedule_create and signals "
                "instead: a chain built that way has no recorded edges, so nothing knows "
                "which steps a failure should stop. "
                "Give every step a `key`, and list its upstreams in `depends_on`. A step "
                "with no upstreams is where the chain starts and must carry its own "
                "`trigger_type` (the same vocabulary as schedule_create); a step with "
                "upstreams must NOT have one, because its upstreams are what starts it. "
                "Give each step `criteria` saying what has to be true for it to count as "
                "done, and a `verify_command` whenever that is mechanically checkable -- "
                "that check is what decides whether the next step runs. The graph is "
                "validated before anything is written: a cycle, a missing upstream, or a "
                "step that can never be judged is refused with the step named."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short workflow name"},
                    "description": {
                        "type": "string",
                        "description": "What this chain is for, in one line",
                        "default": "",
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": (
                            "Absolute path of the project folder the chain works in. "
                            "Steps that do not name their own folder use this one."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "description": "The steps, in any order; `depends_on` gives the order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "description": (
                                        "Short stable id for this step, used by other "
                                        "steps' depends_on, e.g. collect or summarise"
                                    ),
                                },
                                "name": {"type": "string", "description": "Short step name"},
                                "action_type": {
                                    "type": "string",
                                    "description": "one of: message, agent_task, system_job",
                                },
                                "instruction": {
                                    "type": "string",
                                    "description": "Agent instruction, for action_type=agent_task",
                                },
                                "message_text": {
                                    "type": "string",
                                    "description": "Literal message, for action_type=message",
                                },
                                "job_name": {
                                    "type": "string",
                                    "description": "Internal job name, for action_type=system_job",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Keys of the steps that must succeed before this "
                                        "one runs. Empty or absent makes this an entry step."
                                    ),
                                },
                                "trigger_type": {
                                    "type": "string",
                                    "description": (
                                        "Entry steps only: one of once, interval, daily, "
                                        "weekly, weekdays, monthly, signal. A step with "
                                        "upstreams must leave this out."
                                    ),
                                },
                                "signal_name": {"type": "string"},
                                "at": {"type": "string", "description": "ISO datetime for once"},
                                "every": {"type": "integer"},
                                "unit": {"type": "string", "description": "minutes|hours|days|weeks"},
                                "time_of_day": {"type": "string", "description": "HH:MM"},
                                "day_of_week": {"type": "string", "description": "mon|tue|...|sun"},
                                "day_of_month": {"type": "integer", "description": "1-31"},
                                "timezone_name": {"type": "string", "default": "UTC"},
                                "workspace_root": {
                                    "type": "string",
                                    "description": "Overrides the workflow's folder for this step",
                                },
                                "permission_profile": {
                                    "type": "string",
                                    "description": "inherit|read_only|workspace_write",
                                },
                                "delivery_mode": {
                                    "type": "string",
                                    "description": "optional override: standalone or channel",
                                },
                                "criteria": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "What has to be true for this step to count as a "
                                        "success. Decides whether the steps below it run."
                                    ),
                                },
                                "verify_command": {
                                    "type": "string",
                                    "description": (
                                        "Command whose exit code decides this step's "
                                        "success; run in the step's folder. Must be a "
                                        "single low-risk command -- no pipes, redirection, "
                                        "`rm`, or inline interpreters."
                                    ),
                                },
                            },
                            "required": ["key", "name", "action_type"],
                        },
                    },
                },
                "required": ["name", "steps"],
            },
            self._workflow_create,
            source="builtin",
        )

        r.register(
            "workflow_list",
            (
                "List workflows, with each one's steps and the edges between them. "
                "Use before creating a chain, to see whether one already exists."
            ),
            {"type": "object", "properties": {}, "required": []},
            self._workflow_list,
            source="builtin",
        )

        r.register(
            "workflow_delete",
            (
                "Delete a workflow by id. The tasks it built are disabled rather than "
                "erased, so their run history stays readable; this stops the chain and "
                "stops any signal subscription its steps hold. The leftovers are then "
                "ordinary tasks: schedule_delete removes any of them, and its history, "
                "when nobody needs to read it any more."
            ),
            {
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow id to delete; workflow_list shows them",
                    }
                },
                "required": ["workflow_id"],
            },
            self._workflow_delete,
            source="builtin",
        )

        r.register(
            "emit_signal",
            (
                "Announce that something happened, so any task waiting on that signal runs. "
                "Use this to make a step follow this one without waiting for a clock: emit a "
                "signal here, and another task created with trigger_type=signal and the same "
                "signal_name will run. Subscribe before you emit: a signal with nobody "
                "waiting is recorded and then closed as unmatched, and it will not start a "
                "run for a subscriber created afterwards. "
                "Signals a task emits by itself (task:<id>:succeeded / :failed / :cancelled) "
                "need no call. Call `list_signals` first to see which names are already in use."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Signal name. Use a dotted name you choose, e.g. "
                            "report.ready, or task:<task_id>:succeeded to follow a task."
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "Facts to pass downstream, readable by the subscriber in the "
                            "run's signal context. Keep it small."
                        ),
                    },
                },
                "required": ["name"],
            },
            self._emit_signal,
            source="builtin",
        )

        r.register(
            "list_signals",
            (
                "List signal names that have been emitted recently, with how many people "
                "are waiting on each. Use before creating a signal-triggered task so the "
                "subscription matches an existing emission instead of a guess."
            ),
            {"type": "object", "properties": {}, "required": []},
            self._list_signals,
            source="builtin",
        )

        r.register(
            "read_step_output",
            (
                "Read the latest successful output of a step this one stands on. "
                "Use it when this run needs what an upstream step actually produced, rather "
                "than the one-line summary already in this run's context -- for example to "
                "decide what a message should say. Address the step by its key (step2), not "
                "by a task or run id. Only the steps this one depends on, directly or "
                "transitively, are readable, and only outputs the scheduler itself recorded, "
                "so this cannot read arbitrary files."
            ),
            {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "string",
                        "description": "Step key of the upstream step, e.g. step2",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            f"Maximum characters of the output to return "
                            f"(1-{READ_STEP_OUTPUT_MAX_CHARS}). May be capped "
                            f"lower by this run's remaining context budget; the "
                            f"result says so and gives the path to read the rest."
                        ),
                        "default": READ_STEP_OUTPUT_DEFAULT_CHARS,
                    },
                },
                "required": ["step"],
            },
            self._read_step_output,
            source="builtin",
        )

        r.register(
            "report_outcome",
            (
                "Declare that this scheduled run could not do the job it was given. "
                "Call it when you discover the work cannot be done -- the data source is "
                "gone, a required file is missing, the inputs contradict each other -- "
                "instead of writing a plausible answer anyway. The run is then recorded "
                "as failed with your reason, the steps below it do not run, and a person "
                "is told. Do not call it to report success: an unattended run is judged "
                "by its own acceptance check, and your assurance is not evidence. Only "
                "usable inside a scheduled run."
            ),
            {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why the work could not be done, in one or two sentences"
                        ),
                    },
                },
                "required": ["reason"],
            },
            self._report_outcome,
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

        r.register(
            "clear_context",
            "Reset the conversation context to just the current user request. "
            "Use this when the conversation history has grown too large or "
            "triggered a content policy filter. The current task will be "
            "preserved as a summary, and the agent will restart with a clean "
            "context.",
            {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A brief summary of the original request and what has been done so far (optional, auto-generated if empty).",
                        "default": "",
                    },
                },
                "required": [],
            },
            self._clear_context,
            source="builtin",
        )

        r.register(
            "install_plugin",
            (
                "Install a plugin from a git URL or local path into ~/.agent/plugins/<name>/. "
                "Supports Claude Code / Codex layouts (with .claude-plugin/plugin.json) and "
                "marketplaces (a repo with .claude-plugin/marketplace.json listing multiple "
                "plugins).  Hot-reloads after install so the plugin's skills, commands, agents, "
                "MCP servers and hooks become available immediately without restart. "
                "Plugins with executable content (Python entry, MCP server, hooks) require "
                "human confirmation before activation; in non-terminal channels ask the "
                "user to reply 同意, then retry the identical source."
            ),
            {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Git URL (https://, git@) or absolute local path. "
                            "git:// is normalized to https automatically."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional target directory name. Default: derived from source.",
                    },
                    "replace": {
                        "type": "boolean",
                        "description": (
                            "Replace an already-installed plugin with the same name "
                            "(upgrade). Default: false."
                        ),
                        "default": False,
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Why this plugin is being installed.",
                    },
                },
                "required": ["source", "intent"],
            },
            self._install_plugin,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "uninstall_plugin",
            (
                "Remove a user-installed plugin directory and hot-reload the catalog. "
                "Note: MCP server subprocesses connected by the plugin remain running until "
                "the next agent restart; only their tool registrations are dropped."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Plugin directory name under ~/.agent/plugins/.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Why this plugin is being removed.",
                    },
                },
                "required": ["name", "intent"],
            },
            self._uninstall_plugin,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "list_installed_plugins",
            (
                "List all plugins installed under ~/.agent/plugins/ and which are currently "
                "loaded.  Use to verify install state or inspect available extensions."
            ),
            {"type": "object", "properties": {}, "required": []},
            self._list_installed_plugins,
            source="builtin",
        )

        # ── User tool authoring ──────────────────────────────────────────
        #
        # This is the supported way to answer "build me a tool that ...".
        # Writing a .py file with write_file/shell does NOT create a tool,
        # and installing packages with pip/uv from the shell would mutate
        # the user's project — use install_tool_dependency instead.

        r.register(
            "create_tool",
            (
                "Create a new reusable user tool from Python source and activate it for "
                "this and future sessions. USE THIS whenever the user asks to build, add, "
                "or generate a tool/capability — never hand-write a .py file into the "
                "tools directory and never pip-install from the shell. The module must "
                "define `register(registry)` that calls "
                "`registry.register(name, description, json_schema, async_fn)` for each "
                "tool it provides. The source is syntax-checked, then imported in a "
                "separate process to prove it loads, then shown to the user for approval "
                "before anything runs in the agent. Third-party imports must be installed "
                "first with install_tool_dependency."
            ),
            {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": (
                            "Module id under ~/.agent/tools (lowercase letters, digits, "
                            "underscores), e.g. 'markdown_to_html'."
                        ),
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Complete Python module source defining register(registry). "
                            "Tool functions must be async and accept keyword arguments "
                            "matching their JSON schema."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. What this tool does and why the user needs it.",
                    },
                },
                "required": ["tool_id", "code", "intent"],
            },
            self._create_tool,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "update_tool",
            (
                "Replace the source of an existing user tool and reactivate it. Same "
                "validation and approval path as create_tool; because approval is bound "
                "to the file's contents, a changed tool is re-confirmed."
            ),
            {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "Existing tool module id under ~/.agent/tools.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Complete replacement module source defining register(registry).",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. What is changing and why.",
                    },
                },
                "required": ["tool_id", "code", "intent"],
            },
            self._update_tool,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "delete_tool",
            "Delete a user tool, revoke its approval, and unload it from the session.",
            {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "Tool module id under ~/.agent/tools.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Why this tool is being removed.",
                    },
                },
                "required": ["tool_id", "intent"],
            },
            self._delete_tool,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "list_tools",
            (
                "List user tools on disk with their approval and load state, plus the "
                "packages installed in the isolated tool dependency directory. Use before "
                "creating a tool to check whether one already exists."
            ),
            {"type": "object", "properties": {}, "required": []},
            self._list_user_tools,
            source="builtin",
        )

        r.register(
            "install_tool_dependency",
            (
                "Install a Python package that a user tool needs. The package goes into "
                "the agent's isolated dependency directory (~/.agent/tools/_deps) and is "
                "importable by user tools. This is the ONLY supported way to add a Python "
                "dependency: running pip/uv/poetry from the shell would modify the "
                "user's project or the ambient interpreter and is blocked."
            ),
            {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": (
                            "Distribution name with optional extras and one version "
                            "constraint, e.g. 'markdown', 'httpx[http2]', 'markdown>=3.5'."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Which tool needs this package and why.",
                    },
                },
                "required": ["package", "intent"],
            },
            self._install_tool_dependency,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

    # ── User tool authoring implementations ───────────────────────────────

    async def _create_tool(
        self, tool_id: str, code: str, intent: str = ""
    ) -> dict:
        from agent.tools import user_tools

        normalized = user_tools.normalize_tool_id(tool_id)
        if not normalized:
            return {
                "ok": False,
                "error": (
                    f"Could not derive a usable module id from '{tool_id}'. Use "
                    "lowercase letters, digits, and underscores."
                ),
            }
        return await user_tools.author_tool(
            normalized, code, registry=self.registry, replace=False
        )

    async def _update_tool(
        self, tool_id: str, code: str, intent: str = ""
    ) -> dict:
        from agent.tools import user_tools

        normalized = user_tools.normalize_tool_id(tool_id)
        if not normalized:
            return {"ok": False, "error": f"Invalid tool id '{tool_id}'"}
        return await user_tools.author_tool(
            normalized, code, registry=self.registry, replace=True
        )

    async def _delete_tool(self, tool_id: str, intent: str = "") -> dict:
        from agent.tools import user_tools

        normalized = user_tools.normalize_tool_id(tool_id)
        if not normalized:
            return {"ok": False, "error": f"Invalid tool id '{tool_id}'"}
        return user_tools.remove_tool(normalized, registry=self.registry)

    def _list_user_tools(self) -> dict:
        from agent.tools import user_tools

        return user_tools.describe_tools(self.registry)

    async def _install_tool_dependency(
        self, package: str, intent: str = ""
    ) -> dict:
        from agent.tools import user_tools

        return await user_tools.install_dependency(package)

    # ── Plugin install / uninstall implementations ────────────────────────

    async def _install_plugin(
        self,
        source: str,
        intent: str = "",
        name: Optional[str] = None,
        replace: bool = False,
    ) -> dict:
        """Install a plugin; serialized so concurrent installs cannot race."""
        async with _plugin_install_lock():
            return await self._install_plugin_locked(
                source,
                intent=intent,
                name=name,
                replace=replace,
            )

    async def _install_plugin_locked(
        self,
        source: str,
        intent: str = "",
        name: Optional[str] = None,
        replace: bool = False,
    ) -> dict:
        import shutil
        import urllib.parse as _urlparse

        from agent.security.plugin_approval import plugin_install_record_pending

        if not source.strip():
            return {"ok": False, "error": "source is required"}

        source = _canonicalize_plugin_source(source)

        # Derive name from URL or path when not provided.
        if name is None:
            raw = source.rstrip("/")
            if raw.endswith(".git"):
                raw = raw[:-4]
            # Take the last URL/path segment as the slug.
            slug = _urlparse.urlparse(raw).path.rsplit("/", 1)[-1] if "://" in raw else Path(raw).name
            slug = slug or "plugin"
            name = re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-") or "plugin"

        try:
            target = _resolve_user_plugin_target(name, self.registry.get_context("user_plugins_dir") and Path(str(self.registry.get_context("user_plugins_dir"))))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        target.parent.mkdir(parents=True, exist_ok=True)

        # Replace/upgrade: move the current version aside so a failed install
        # can restore it instead of losing the working copy.
        backup: Optional[Path] = None
        if target.exists():
            if not replace:
                return {
                    "ok": False,
                    "error": f"plugin '{name}' already exists at {target}",
                    "recovery_hint": (
                        "use replace=true to upgrade it, or uninstall_plugin first"
                    ),
                }
            backup = target.parent / f".{target.name}.old-{uuid.uuid4().hex[:8]}"
            try:
                shutil.move(str(target), str(backup))
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"failed to back up existing plugin: {exc}",
                }

        def _rollback() -> None:
            shutil.rmtree(target, ignore_errors=True)
            if backup is not None and backup.exists() and not target.exists():
                shutil.move(str(backup), str(target))

        try:
            fetch_result = await self._fetch_plugin_source(source, target)
            if not fetch_result.get("ok"):
                raise _PluginInstallError(fetch_result)

            # Validate that the installed directory looks like a plugin.
            if not _looks_like_plugin(target):
                raise _PluginInstallError(
                    {
                        "ok": False,
                        "error": (
                            f"Installed directory does not appear to be a valid plugin. "
                            f"Expected one of: plugin.json, .claude-plugin/plugin.json, "
                            f"__init__.py, skills/, or commands/."
                        ),
                    }
                )

            # Executable plugins are arbitrary code: ask the human first.
            executable_reason = _plugin_executable_content(target)
            if executable_reason:
                scope = self._plugin_authorization_scope()
                approved, needs_pending = await self._confirm_plugin_install(
                    source=source,
                    reason=executable_reason,
                    scope=scope,
                )
                if not approved:
                    payload = {
                        "ok": False,
                        "error": (
                            "plugin install cancelled: "
                            f"{executable_reason} requires human confirmation"
                        ),
                        "cancelled": True,
                    }
                    if needs_pending:
                        plugin_install_record_pending(scope, source)
                        payload.update(
                            {
                                "requires_confirmation": True,
                                "confirmation_guidance": (
                                    "该插件包含可执行内容（Python 入口/MCP server/hooks），"
                                    "安装后激活等于执行任意代码。请向用户展示要安装的来源，"
                                    "并请用户回复『同意』；批准后请用完全相同的 source "
                                    "重试 install_plugin（如需覆盖已安装版本请带 replace=true）。"
                                ),
                            }
                        )
                    raise _PluginInstallError(payload)

            # Hot-reload the catalog so the new plugin's assets are live.
            reload_result = await self._reload_plugins()
            if not reload_result.get("ok", False):
                raise _PluginInstallError(
                    {
                        "ok": False,
                        "error": (
                            "plugin files installed but activation failed; "
                            "install rolled back"
                        ),
                        "reload": reload_result,
                        "recovery_hint": (
                            "fix the reload error and retry install_plugin"
                        ),
                    }
                )
        except _PluginInstallError as exc:
            _rollback()
            return exc.payload
        except Exception as exc:
            _rollback()
            return {
                "ok": False,
                "error": f"install failed: {exc}",
                "recovery_hint": (
                    "fix the error and retry; the previous version was restored"
                    if backup is not None
                    else "fix the error and retry"
                ),
            }
        finally:
            if backup is not None and backup.exists():
                shutil.rmtree(backup, ignore_errors=True)

        return {
            "ok": True,
            "installed_at": str(target),
            "name": name,
            "replaced": backup is not None,
            "reload": reload_result,
            "summary_text": (
                f"{'Replaced' if backup is not None else 'Installed'} plugin "
                f"'{name}' from {source}. "
                f"Added: {', '.join(reload_result.get('added_plugins', [])) or 'none'}; "
                f"connected MCP: {', '.join(reload_result.get('newly_connected_mcp', [])) or 'none'}."
            ),
        }

    async def _fetch_plugin_source(self, source: str, target: Path) -> dict:
        """Clone/copy *source* into *target*; clean partial results on failure."""
        import shutil

        is_url = source.startswith(("http://", "https://", "git@", "git://"))
        if is_url:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", source, str(target),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Register cleanup so /cancel kills the clone.
            def _cancel_clone(level: str) -> None:
                sig = signal.SIGKILL if level == "force" else signal.SIGTERM
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    proc.send_signal(sig)

            active_token = shared._active_cancel_token.get()
            _deregister_proc = (
                active_token.register_cleanup(
                    f"git-clone:{source[:60]}", _cancel_clone
                )
                if active_token is not None
                else (lambda: None)
            )

            # Heartbeat to keep the watchdog alive during long clones.
            async def _heartbeat() -> None:
                while True:
                    await asyncio.sleep(10)
                    try:
                        report_tool_progress(
                            status="cloning",
                            message=f"git clone {source[:80]} in progress",
                        )
                    except Exception:
                        pass

            heartbeat_task = asyncio.create_task(_heartbeat())
            try:
                _, stderr = await proc.communicate()
            finally:
                _deregister_proc()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            if proc.returncode != 0:
                shutil.rmtree(target, ignore_errors=True)
                return {
                    "ok": False,
                    "error": f"git clone failed: {stderr.decode(errors='replace').strip()}",
                }
            return {"ok": True}

        src_path = Path(source).expanduser().resolve()
        if not src_path.is_dir():
            return {"ok": False, "error": f"source path not found: {source}"}
        try:
            shutil.copytree(src_path, target)
        except Exception as exc:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "error": f"copy failed: {exc}"}
        return {"ok": True}

    @staticmethod
    def _plugin_authorization_scope() -> Any:
        from agent.core.agent import _active_agent_context
        from agent.security.shell import ShellAuthorizationScope

        active_context = _active_agent_context.get()
        metadata = active_context.metadata if active_context is not None else {}
        return ShellAuthorizationScope(
            str(metadata.get("session_id") or "default"),
            str(metadata.get("channel_name") or "cli"),
            str(metadata.get("user_id") or ""),
        )

    async def _confirm_plugin_install(
        self,
        *,
        source: str,
        reason: str,
        scope: Any,
    ) -> tuple[bool, bool]:
        """Ask the human before activating executable plugin content.

        Returns ``(approved, needs_pending)``.  Interactive sinks decide
        immediately; a decline is final.  Non-interactive sinks leave a
        pending record so the coordinator can redeem a later "同意" reply.
        """
        from agent.core.output import _APPROVAL_LOCK, _active_sink
        from agent.security.plugin_approval import (
            plugin_install_mark_approved,
            plugin_install_was_approved,
        )

        async with _APPROVAL_LOCK:
            if plugin_install_was_approved(scope, source):
                return True, False
            sink = _active_sink.get()
            if sink is None:
                return False, False
            interactive = bool(getattr(sink, "interactive_confirmation", False))
            approved = await sink.on_tool_confirmation(
                "install_plugin",
                command=source,
                risk_level="high",
                reason=reason,
                confirmation_token="",
                scope=scope,
            )
            if approved:
                plugin_install_mark_approved(scope, source)
                return True, False
            return False, not interactive

    async def _uninstall_plugin(self, name: str, intent: str = "") -> dict:
        import shutil

        try:
            target = _resolve_user_plugin_target(name, self.registry.get_context("user_plugins_dir") and Path(str(self.registry.get_context("user_plugins_dir"))))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not target.is_dir():
            return {"ok": False, "error": f"plugin '{name}' not found at {target}"}

        try:
            shutil.rmtree(target)
        except Exception as exc:
            return {"ok": False, "error": f"rmtree failed: {exc}"}

        reload_result = await self._reload_plugins()
        return {
            "ok": True,
            "removed_from": str(target),
            "reload": reload_result,
            "summary_text": (
                f"Uninstalled plugin '{name}'. "
                f"Removed: {', '.join(reload_result.get('removed_plugins', [])) or 'none'}."
            ),
        }

    def _list_installed_plugins(self) -> dict:
        plugin_catalog = self.registry.get_context("plugin_catalog")
        loaded_names: set[str] = set()
        has_marketplace_support = hasattr(plugin_catalog, "get_loaded_names_for_directory")
        if plugin_catalog is not None and hasattr(plugin_catalog, "list_plugins"):
            try:
                loaded_names = {
                    getattr(p, "name", "") for p in plugin_catalog.list_plugins()
                }
            except Exception:
                loaded_names = set()

        on_disk: list[dict] = []
        user_dir_loaded_count = 0
        user_plugins_dir = Path(str(self.registry.get_context("user_plugins_dir") or shared.USER_PLUGINS_DIR))
        if user_plugins_dir.is_dir():
            for entry in sorted(user_plugins_dir.iterdir()):
                if not entry.is_dir():
                    continue
                # Check if the directory name matches a loaded plugin, OR
                # if it's a marketplace directory, check its sub-plugins.
                is_loaded = entry.name in loaded_names
                if not is_loaded and has_marketplace_support:
                    sub_names = plugin_catalog.get_loaded_names_for_directory(entry.name)
                    is_loaded = bool(sub_names)
                if is_loaded:
                    user_dir_loaded_count += 1
                on_disk.append({
                    "name": entry.name,
                    "path": str(entry),
                    "loaded": is_loaded,
                })
        return {
            "ok": True,
            "user_plugins_dir": str(user_plugins_dir),
            "plugins": on_disk,
            # Count of loaded plugins present in the user plugin directory listing.
            "loaded_count": user_dir_loaded_count,
            # Total count across all loaded plugins (builtin + user + others).
            # Useful when loaded plugins exist outside USER_PLUGINS_DIR.
            "global_loaded_count": len(loaded_names),
        }

    async def _reload_plugins(self) -> dict:
        """Trigger PluginCatalog.reload using the components dict stashed on the registry context."""
        plugin_catalog = self.registry.get_context("plugin_catalog")
        components = self.registry.get_context("components")
        if plugin_catalog is None or components is None:
            return {
                "ok": False,
                "error": "plugin_catalog/components not wired into registry context",
            }
        try:
            return await plugin_catalog.reload(components)
        except Exception as exc:
            return {"ok": False, "error": f"reload failed: {exc}"}

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
        """Fetch *url* through the validated, address-pinned network boundary."""
        result = fetch_public_http_url(
            url,
            timeout=timeout,
            max_bytes=WEB_FETCH_MAX_BYTES,
            headers={
                "User-Agent": WEB_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            on_progress=lambda bytes_done, total: report_tool_progress(
                status="downloading",
                current=bytes_done,
                total=total,
                bytes_done=bytes_done,
                bytes_total=total,
            ),
        )
        return result.body

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
            # The network layer caps the body at WEB_FETCH_MAX_BYTES and does
            # not signal the cut, so a page that fills the cap must be reported
            # as truncated even when the char limit below would not have.
            truncated_network = len(raw_bytes) >= WEB_FETCH_MAX_BYTES

            if raw_html:
                body = raw_text[:max_chars]
                truncated = truncated_network or len(raw_text) > max_chars
            else:
                body = self._strip_html(raw_text)
                truncated = truncated_network or len(body) > max_chars
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

    def _file_mutation_authorizer(
        self,
        tool_input: dict[str, Any],
        registry: ToolRegistry,
    ) -> dict[str, Any] | None:
        """Call-aware authorization for root-dependent file mutations.

        ``write_file``/``edit_file`` carry the base ``output_write``
        capability and are available in every Agent profile for
        ``output_dir``.  A workspace target additionally requires the
        ``workspace_write`` capability (implementation profile with an
        explicit write_scope), the startup workspace write switch, and
        containment in the effective write scope.
        """
        if tool_input.get("root") != "workspace":
            return None
        profile = registry.get_context("capability_profile")
        if profile is not None and profile != "full":
            if profile != "implementation":
                return self._structured_access_denied(
                    "workspace writes require the workspace_write capability"
                )
            scope = registry.get_context("write_scope") or ()
            path = tool_input.get("path", "")
            if not write_scope_allows(scope, path):
                return self._structured_access_denied(
                    f"path is outside the effective write scope: {path}"
                )
        policy = registry.get_context("file_access_policy")
        if policy is not None and not policy.workspace_write:
            return self._structured_access_denied(
                "workspace writes are disabled by the file access policy"
            )
        return None

    @staticmethod
    def _structured_access_denied(message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": "access_denied",
                "message": message,
                "details": {},
                "retryable": False,
            },
        }

    async def _try_interactive_confirmation(
        self,
        *,
        safety: Any,
        command: str,
        extra_blocked: list[str],
        authorization_scope: Any,
    ) -> bool:
        """Ask the human at the active sink for consent to a medium-risk command.

        Returns True only when the human approved AND the scoped
        confirmation token was consumed by shell.py, so the command now
        passes the session allowlist.  Every other outcome — no interactive
        sink, decline, cancelled turn, invalid or expired token — returns
        False and the caller keeps the structured confirmation-required
        result.  The model can never fabricate approval: the prompt is a
        terminal prompt, and the token is generated and validated by
        shell.py.
        """
        if not safety.confirmation_token:
            return False
        from agent.core.output import _APPROVAL_LOCK
        from agent.security.shell import (
            shell_command_confirm,
            shell_pending_reject,
            shell_session_allowlist_contains,
        )

        async with _APPROVAL_LOCK:
            if shell_session_allowlist_contains(
                command, scope=authorization_scope
            ):
                # A parallel call already obtained human approval for the
                # identical command in this scope; skip the redundant prompt.
                return True
            sink = _active_sink.get()
            if sink is None:
                return False
            approved = await sink.on_tool_confirmation(
                "shell",
                command=command,
                risk_level=safety.risk_level,
                reason=safety.reason,
                confirmation_token=safety.confirmation_token,
                scope=authorization_scope,
            )
            if not approved:
                # Refusal is terminal: retire the token so no later approval
                # reply can redeem the command the human just declined.
                shell_pending_reject(
                    safety.confirmation_token,
                    scope=authorization_scope,
                )
                return False
            active_token = shared._active_cancel_token.get()
            if active_token is not None and active_token.is_cancelled:
                shell_pending_reject(
                    safety.confirmation_token,
                    scope=authorization_scope,
                )
                return False
            return bool(
                shell_command_confirm(
                    safety.confirmation_token,
                    scope=authorization_scope,
                )
            )

    def _resolve_tool_path(self, path: str) -> tuple[Path, str]:
        return resolve_workspace_path(
            path,
            workspace_root=self._active_workspace_root(),
            output_dir=self._process_output_dir(),
        )

    def _resolve_output_path(self, path: str) -> tuple[Path, str]:
        """Resolve relative paths against the output directory.

        Generated/downloaded files belong in output_dir, not the
        workspace (repo).  Absolute paths are accepted when they are already
        inside an allowed root.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            output_dir = self._process_output_dir()
            path = str(output_dir / candidate)
        return resolve_workspace_path(
            path,
            workspace_root=self._active_workspace_root(),
            output_dir=self._process_output_dir(),
        )

    def _path_is_inside_workspace(self, path: Path) -> bool:
        return path_contains(
            self._active_workspace_root(),
            path.expanduser().resolve(strict=False),
        )

    def _path_is_inside_output_dir(self, path: Path) -> bool:
        return path_contains(
            self._process_output_dir(),
            path.expanduser().resolve(strict=False),
        )

    def _workspace_file_snapshot(self) -> set[Path]:
        """Return files currently in the workspace, ignoring agent outputs."""
        files: set[Path] = set()
        workspace_root = self._active_workspace_root()
        output_dir = self._process_output_dir()
        if not workspace_root.exists():
            return files
        for path in workspace_root.rglob("*"):
            if path.is_symlink():
                continue
            resolved = path.resolve(strict=False)
            if path.is_dir():
                if path.name in {".git", "__pycache__", ".pytest_cache"}:
                    continue
                continue
            if path_contains(output_dir, resolved):
                continue
            files.add(resolved)
        return files

    def _move_new_workspace_files_to_output_dir(
        self,
        *,
        before: set[Path],
        cwd: Path,
    ) -> list[dict[str, str]]:
        output_dir = self._process_output_dir()
        workspace_root = self._active_workspace_root()
        moved: list[dict[str, str]] = []
        for path in sorted(self._workspace_file_snapshot() - before):
            if not path.is_file() or path_contains(output_dir, path):
                continue
            try:
                relative = path.relative_to(workspace_root)
            except ValueError:
                relative = path.name
            dest = output_dir / "workspace-artifacts" / relative
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}-{uuid.uuid4().hex[:8]}{dest.suffix}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved.append({"from": str(path), "to": str(dest)})

            # Clean up empty directories left by nested generated artifacts,
            # stopping at cwd/workspace boundaries.
            parent = path.parent
            stop_dirs = {workspace_root, cwd.resolve(strict=False)}
            while parent not in stop_dirs and parent != parent.parent:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return moved


    async def _shell(
        self,
        command: str,
        intent: str = "",
        timeout: int = 300,
        root: str = "workspace",
        cwd: Optional[str] = None,
    ) -> dict[str, Any]:
        # Security: block dangerous commands before spawning any subprocess.
        extra_blocked: list[str] = (
            self.registry.get_context("shell_blocked_commands") or []
        )
        pre_approved: list[str] = (
            self.registry.get_context("shell_allowed_commands") or []
        )
        permission_level: str = str(
            self.registry.get_context("shell_permission_level") or "ask"
        )
        import agent as agent_module

        from agent.core.agent import _active_agent_context
        from agent.security.shell import (
            ShellAuthorizationScope,
            shell_effective_permission_level,
            shell_session_sandbox_get,
        )

        active_context = _active_agent_context.get()
        metadata = active_context.metadata if active_context is not None else {}
        authorization_scope = ShellAuthorizationScope(
            str(metadata.get("session_id") or "default"),
            str(metadata.get("channel_name") or "cli"),
            str(metadata.get("user_id") or ""),
        )
        # Sandbox mode is linked to the permission level: an unsandboxed
        # (danger-full-access) run is only honored when the effective level
        # is "full"; otherwise it fails safe back to the read-all profile.
        sandbox_mode = str(
            self.registry.get_context("shell_sandbox_mode") or "read_all"
        )
        session_sandbox = shell_session_sandbox_get(authorization_scope)
        if session_sandbox:
            sandbox_mode = session_sandbox
        effective_level = shell_effective_permission_level(
            authorization_scope, permission_level
        )
        if sandbox_mode == SANDBOX_MODE_NONE and effective_level != "full":
            sandbox_mode = effective_sandbox_mode(sandbox_mode, effective_level)
        unsandboxed = sandbox_mode == SANDBOX_MODE_NONE
        shell_devices = bool(
            self.registry.get_context("shell_devices", True)
        )

        output_dir = self._process_output_dir()
        workspace_root = self._active_workspace_root()
        sandbox_dir = self._sandbox_dir()
        _shell_command_check = agent_module._shell_command_check
        safety = _shell_command_check(
            command,
            extra_blocked,
            allowed_roots=frozenset({workspace_root, output_dir}),
            scope=authorization_scope,
            pre_approved=pre_approved,
            permission_level=permission_level,
        )
        if safety.requires_confirmation and await self._try_interactive_confirmation(
            safety=safety,
            command=command,
            extra_blocked=extra_blocked,
            authorization_scope=authorization_scope,
        ):
            # The human approved at the terminal; the command is now on the
            # session allowlist.  Re-check so the rest of this function runs
            # the normal allowed path.
            safety = _shell_command_check(
                command,
                extra_blocked,
                allowed_roots=frozenset({workspace_root, output_dir}),
                scope=authorization_scope,
                pre_approved=pre_approved,
                permission_level=permission_level,
            )
        if not safety.allowed:
            if safety.requires_confirmation:
                error_message = (
                    f"Shell command requires confirmation: {safety.reason} "
                    f"{_SHELL_CONFIRMATION_GUIDANCE}"
                )
                return self._error(
                    error_message,
                    command=command,
                    risk_level=safety.risk_level,
                    requires_confirmation=True,
                    confirmation_token=safety.confirmation_token,
                )
            return self._error(
                f"Shell command rejected: {safety.reason}",
                command=command,
                risk_level=safety.risk_level,
            )

        # The same immutable file policy governs shell descendants.  The
        # sandbox is an OS-level boundary; the pre/post snapshot moving below
        # is not an authorization mechanism.
        file_policy = self.registry.get_context("file_access_policy")
        workspace_read = file_policy.workspace_read if file_policy is not None else True
        workspace_write = (
            file_policy.workspace_write if file_policy is not None else False
        )
        write_scope = _normalize_write_scope(
            self.registry.get_context("write_scope") or ()
        )
        root = str(root or "workspace").strip().casefold()
        if root not in ("workspace", "output_dir"):
            return self._error(
                "Shell root must be 'workspace' or 'output_dir'",
                command=command,
            )
        if cwd:
            candidate = Path(cwd).expanduser()
            if candidate.is_absolute():
                resolved_cwd, call_root = resolve_workspace_path(
                    candidate,
                    workspace_root=workspace_root,
                    output_dir=output_dir,
                )
            else:
                base = workspace_root if root == "workspace" else output_dir
                resolved_cwd, _ = resolve_workspace_path(
                    base / candidate,
                    workspace_root=workspace_root,
                    output_dir=output_dir,
                )
                if not path_contains(base, resolved_cwd):
                    return self._error(
                        f"Shell cwd '{cwd}' escapes root '{root}'",
                        command=command,
                    )
                call_root = root
        else:
            if root == "workspace":
                resolved_cwd, call_root = workspace_root, "workspace"
            else:
                resolved_cwd, call_root = output_dir, "output_dir"

        sandbox = None
        scratch_dir: Optional[Path] = None
        workspace_before: set[Path] | None = None
        if unsandboxed:
            # Danger-full-access: the OS sandbox is off, but the file-domain
            # invariant still holds at the tool layer — output-domain calls
            # must not leave new files in the workspace.
            if call_root == "output_dir":
                workspace_before = self._workspace_file_snapshot()
        else:
            if not workspace_read and self._path_is_inside_workspace(
                resolved_cwd
            ):
                return self._error(
                    "Workspace cwd is not allowed when workspace reads are disabled",
                    command=command,
                    sandbox_unavailable=True,
                )
            try:
                scratch_dir = new_scratch_dir(output_dir)
                sandbox = build_sandbox_command(
                    ShellSandboxRequest(
                        workspace_root=workspace_root,
                        output_root=output_dir,
                        workspace_read=workspace_read,
                        workspace_write=workspace_write,
                        write_scope=write_scope,
                        scratch_dir=scratch_dir,
                        mode=sandbox_mode,
                        devices=shell_devices,
                        extra_secret_paths=tuple(
                            self.registry.get_context("shell_secret_paths") or ()
                        ),
                        agent_home=self._resource_home(),
                    )
                )
            except SandboxUnavailableError as exc:
                return self._error(
                    str(exc),
                    command=command,
                    sandbox_unavailable=True,
                )

        try:
            env = os.environ.copy()
            env["AGENT_OUTPUT_DIR"] = str(output_dir)
            env["AGENT_WORKSPACE_ROOT"] = str(workspace_root)
            env["AGENT_SANDBOX_DIR"] = str(sandbox_dir)
            # The provider prepends the sandbox argv and merges its env, kills
            # the process group on timeout or `/cancel` (so cancellation does
            # not have to wait for the command, and no child outlives us), and
            # heartbeats so a long quiet command is not mistaken for a stalled
            # one.
            exec_result = await provider_from(self.registry).run(
                ExecRequest(
                    argv=("/bin/sh", "-c", command),
                    cwd=str(resolved_cwd) if resolved_cwd is not None else None,
                    env=env,
                    sandbox=sandbox,
                    timeout=timeout,
                    cancel_label=f"shell:{command[:60]}",
                    heartbeat_message="shell command in progress",
                )
            )
            if exec_result.timed_out:
                return self._error(
                    f"Command timed out after {timeout}s",
                    command=command,
                    timed_out=True,
                )
            out = exec_result.stdout_text()
            err = exec_result.stderr_text()
            result = ""
            if out:
                result += f"STDOUT:\n{out}"
            if err:
                result += f"STDERR:\n{err}"
            result += f"\nExit code: {exec_result.returncode}"
            if exec_result.truncated:
                result += (
                    f"\n[output truncated: exceeded {OUTPUT_MAX_BYTES} "
                    "bytes; redirect to a file or use a more specific command "
                    "to inspect the rest]"
                )
            # When a command fails *because the sandbox refused it*, say which
            # narrow knob matches. The reason a user turns the sandbox off
            # wholesale is almost never that they wanted no boundary — it is
            # that something broke and `none` was the only option visible at
            # that moment. (Measured: GPU/Metal works fine under `read_all`
            # with the default `shell_devices: true`; disabling the sandbox
            # for it buys nothing.)
            if exec_result.returncode != 0 and looks_like_sandbox_denial(err):
                hint = narrow_alternatives_hint(sandbox_mode)
                if hint:
                    result += f"\n\n{hint}"
            moved_artifacts: list[dict[str, str]] = []
            if workspace_before is not None:
                moved_artifacts = self._move_new_workspace_files_to_output_dir(
                    before=workspace_before,
                    cwd=resolved_cwd,
                )
                if moved_artifacts:
                    result += "\nMoved generated workspace artifacts to output_dir:"
                    for item in moved_artifacts[:20]:
                        result += f"\n- {item['from']} -> {item['to']}"
                    if len(moved_artifacts) > 20:
                        result += f"\n- ... {len(moved_artifacts) - 20} more"
            return self._ok(
                command=command,
                root=call_root,
                cwd=str(resolved_cwd),
                output=result or "(no output)",
                exit_code=exec_result.returncode,
                moved_artifacts=moved_artifacts,
            )
        except ValueError as e:
            return self._error(f"Invalid shell input: {e}", command=command)
        except Exception as e:
            return self._error(f"Shell command failed: {e}", command=command)
        finally:
            # The scratch dir is this command's TMPDIR.  Every call gets a
            # fresh one, so nothing can depend on it outliving the call —
            # and without this the directory (plus whatever the command
            # wrote into it) leaked once per shell invocation, unbounded for
            # a long-running gateway.
            release_scratch_dir(scratch_dir)

    def _send_file(self, path: str) -> dict[str, Any]:
        try:
            resolved, _root_kind = self._resolve_output_path(path)
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
            # Channels that support attachments override queue_attachment.
            # Base class no-op means the channel doesn't support file delivery.
            has_override = (
                type(sink).queue_attachment
                is not OutputSink.queue_attachment
            )
            if not has_override:
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

    def _build_audio_transcription_argv(
        self, command_template: str, audio_path: Path, language: str
    ) -> list[str]:
        if shell_command_uses_shell_features(command_template):
            raise ValueError(
                "unsafe audio transcription command: shell operators are not allowed"
            )
        try:
            template_parts = shlex.split(command_template)
        except ValueError as e:
            raise ValueError(
                f"unsafe audio transcription command: invalid quoting ({e})"
            ) from e
        if not template_parts:
            raise ValueError("unsafe audio transcription command: empty command")

        path_was_used = False
        argv: list[str] = []
        for part in template_parts:
            if "{path}" in part:
                path_was_used = True
                part = part.replace("{path}", str(audio_path))
            if "{language}" in part:
                part = part.replace("{language}", language)
            if part:
                argv.append(part)
        if not path_was_used:
            argv.append(str(audio_path))
        return argv

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
            argv = self._build_audio_transcription_argv(
                command_template, resolved, language
            )
            timeout = max(1, min(int(timeout), 900))
            proc = None
            output_dir = self._process_output_dir()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ.copy(),
                    "AGENT_OUTPUT_DIR": str(output_dir),
                    "AGENT_WORKSPACE_ROOT": str(self._active_workspace_root()),
                },
                cwd=str(output_dir),
                start_new_session=True,
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
        if getattr(proc, "returncode", None) is None:
            try:
                if hasattr(os, "killpg") and getattr(proc, "pid", None):
                    os.killpg(proc.pid, signal.SIGTERM)
                elif hasattr(proc, "terminate"):
                    proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                if hasattr(proc, "terminate"):
                    with contextlib.suppress(Exception):
                        proc.terminate()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=1)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        try:
            if hasattr(os, "killpg") and getattr(proc, "pid", None):
                os.killpg(proc.pid, signal.SIGKILL)
            elif hasattr(proc, "kill"):
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            if hasattr(proc, "kill"):
                with contextlib.suppress(Exception):
                    proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()

    def _read_file(
        self,
        root: str = "workspace",
        path: str = "",
        start_line: int = 1,
        line_count: Optional[int] = None,
    ) -> dict[str, Any]:
        return self._file_service.read_file(
            root,
            path,
            start_line=start_line,
            line_count=line_count,
        )

    def _write_file(
        self,
        root: str,
        path: str,
        mode: str,
        content: str,
        expected_revision: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._file_service.write_file(
            root,
            path,
            mode=mode,
            content=content,
            expected_revision=expected_revision,
        )

    def _edit_file(
        self,
        root: str,
        path: str,
        expected_revision: str,
        replacements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._file_service.edit_file(
            root,
            path,
            expected_revision=expected_revision,
            replacements=replacements,
        )

    def _list_files(
        self,
        root: str = "workspace",
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        cursor: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> dict[str, Any]:
        return self._file_service.list_files(
            root,
            path,
            recursive=recursive,
            pattern=pattern,
            cursor=cursor,
            max_results=max_results,
        )

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

    def _set_identity(
        self,
        subject: str = "assistant",
        name: Optional[str] = None,
        role: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> dict[str, Any]:
        if name is None and role is None and persona is None:
            return self._error(
                "Nothing to set: provide at least one of name, role, or persona."
            )
        try:
            applied = self.memory.set_identity(
                subject=subject, name=name, role=role, persona=persona
            )
        except ValueError as exc:
            return self._error(str(exc))
        return self._ok(subject=subject, applied=applied)

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

    async def _memory_clear(self) -> dict[str, Any]:
        """Clear memory only after consent from the active human sink."""
        from agent.core.output import _APPROVAL_LOCK

        sink = _active_sink.get()
        if sink is None or not getattr(sink, "interactive_confirmation", False):
            return self._error(
                "Clearing memory requires interactive human approval.",
                requires_confirmation=True,
            )
        async with _APPROVAL_LOCK:
            approved = await sink.on_tool_confirmation(
                "memory_clear",
                command=(
                    "永久清空全部长期记忆、可检索对话历史、事实索引、"
                    "工作状态和待整理记忆"
                ),
                risk_level="high",
                reason="该操作不可恢复",
                confirmation_token="",
                scope=None,
            )
        if not approved:
            return self._error("Memory clear was declined by the user.")

        context_manager = None
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            context_manager = (
                active_ctx.metadata.get("context_manager")
                if active_ctx is not None
                else None
            )
        if context_manager is not None:
            context_manager.on_memory_cleared()
        deleted = self.memory.clear()
        return self._ok(
            action="clear",
            deleted=sum(deleted.values()),
            deleted_by_store=deleted,
        )

    def _context_retrieve(
        self,
        queries: Optional[list[str] | str] = None,
        query: str = "",
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Search LTM with one or more phrasings.

        Plural by design.  The dominant retrieval failure is not ranking but
        that a single unmodified question is a poor query against a lexical
        index; measured on the eval set, 7 of 7 stage-1 misses were
        recoverable by re-asking in the memory's own wording or language.
        Encoding that in the parameter shape makes reformulation the default
        path, which prose in a tool description does not reliably achieve.

        ``queries`` accepts a bare string as well as a list.  A model filling
        an array parameter with a single string is routine, and iterating a
        str yields its characters — silently turning one good query into
        dozens of one-character ones that match everything.
        """
        if isinstance(queries, str):
            queries = [queries] if queries.strip() else []
        wanted = [str(q).strip() for q in (queries or []) if str(q).strip()]
        if query and str(query).strip():
            wanted.append(str(query).strip())
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        wanted = [q for q in wanted if not (q in seen or seen.add(q))]
        if not wanted:
            return self._error(
                "Provide `queries` (preferred) or `query`.",
                hint="Pass 2-4 phrasings, including the other language.",
            )
        context_manager = self.context_manager
        current_turn_id = ""
        # Tool definitions are shared across multiplexed sessions. Resolve the
        # manager from the active turn instead of capturing the bootstrap
        # manager, whose staging session is not user-facing.
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            if active_ctx is not None:
                context_manager = active_ctx.metadata.get(
                    "context_manager",
                    context_manager,
                )
                current_turn_id = str(active_ctx.metadata.get("turn_id") or "")
        if context_manager is None:
            return self._error("Context manager not available.")
        result = context_manager.retrieve_context(
            wanted,
            top_k=top_k,
            exclude_message_id=current_turn_id,
        )
        sections = [s for s in result.split("\n\n") if s.strip()] if result else []
        if not sections:
            # Feedback at the point of failure: an empty result is far more
            # often a badly-shaped query than an absent memory, and without
            # saying so the model concludes "no record" and tells the user.
            return self._ok(
                queries=wanted,
                count=0,
                content="",
                sections=[],
                hint=(
                    "Nothing matched. This index is lexical, so try again with "
                    "different WORDS before concluding the memory is absent: "
                    "the other language, the concrete nouns the memory would "
                    "use, or a name rather than a description."
                ),
            )
        return self._ok(
            queries=wanted,
            count=len(sections),
            content=result,
            sections=sections,
        )

    def _schedule_store(self) -> SchedulerStore:
        if self._cached_schedule_store is None:
            from agent.scheduler import SchedulerStore
            self._cached_schedule_store = SchedulerStore(
                db_path=shared.SCHEDULER_DB_FILE
            )
        return self._cached_schedule_store

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
        day_of_month: Optional[int] = None,
        signal_name: Optional[str] = None,
    ):
        from agent.scheduler import TriggerSpec

        kind = str(trigger_type).strip().lower()
        if kind == "signal":
            wanted = str(signal_name or "").strip()
            if not wanted:
                raise ValueError("`signal_name` is required for signal triggers")
            # Subscribing is exact-name matching, so a typo is not a near miss
            # -- it is a task that never runs.  What can be checked is checked
            # (see the store's validator); what cannot is allowed, because a
            # new name is how a new emitter is paired and refusing it would
            # make the two sides have to be created in a particular order.
            problem = self._schedule_store().describe_signal_problem(wanted)
            if problem:
                raise ValueError(f"signal_name「{wanted}」无效：{problem}")
            return TriggerSpec.signal(wanted)
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
        if kind == "weekdays":
            if not time_of_day:
                raise ValueError("`time_of_day` is required for weekdays triggers")
            return TriggerSpec.weekdays(time_of_day, timezone_name)
        if kind == "monthly":
            if day_of_month is None or not time_of_day:
                raise ValueError("`day_of_month` and `time_of_day` are required for monthly triggers")
            return TriggerSpec.monthly(day_of_month, time_of_day, timezone_name)
        raise ValueError(f"Unsupported trigger_type '{trigger_type}'")

    @staticmethod
    def _describe_trigger_when(trigger) -> str:
        """How to say *when* a task runs, in the terms the trigger uses.

        Returns a fragment meant to be followed by the action ("发送消息…",
        "执行任务…"), or an empty string when there is no honest way to name a
        time -- which is not a bug to paper over with "None" but the normal
        case for a task that waits for a signal.
        """
        if trigger.trigger_type == "signal":
            name = str(trigger.payload.get("name", "")).strip()
            return f"收到信号「{name}」时"
        initial = trigger.initial_run_at()
        return f"将在 {initial.isoformat()} " if initial else ""

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
        day_of_month: Optional[int] = None,
        signal_name: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        permission_profile: Optional[str] = None,
        workspace_root: Optional[str] = None,
        criteria: Optional[list[str]] = None,
        verify_command: Optional[str] = None,
    ) -> dict[str, Any]:
        trigger = self._schedule_trigger(
            trigger_type=trigger_type,
            timezone_name=timezone_name,
            at=at,
            every=every,
            unit=unit,
            time_of_day=time_of_day,
            day_of_week=day_of_week,
            day_of_month=day_of_month,
            signal_name=signal_name,
        )
        resolved_mode, target = self._schedule_target(delivery_mode)
        from agent.scheduler import Acceptance, NewScheduledTask

        # A task woken by a signal is still a scheduled task in every way that
        # matters -- it just has no date to name.  Saying when it runs is
        # therefore a different sentence, not a missing one, and the summary
        # should read as "it will run", not as "it will run at None".
        when_text = self._describe_trigger_when(trigger)
        noun = "信号任务" if trigger.trigger_type == "signal" else "定时任务"
        job_noun = f"系统{noun}"
        task_kind, payload = self._action_payload(
            action_type,
            prompt=prompt,
            message_text=message_text,
            instruction=instruction,
            job_name=job_name,
        )
        if task_kind == "message":
            summary_text = (
                f"已设置好{noun}！{when_text}发送消息“{payload['message_text']}”。"
                if when_text
                else f"已设置好{noun}，会发送消息“{payload['message_text']}”。"
            )
        elif task_kind == "agent_prompt":
            summary_text = (
                f"已设置好{noun}！{when_text}执行任务：{payload['prompt']}"
                if when_text
                else f"已设置好{noun}，会执行任务：{payload['prompt']}"
            )
        else:
            summary_text = (
                f"已设置好{job_noun}！{when_text}执行 {payload['job_name']}。"
                if when_text
                else f"已设置好{job_noun}，会执行 {payload['job_name']}。"
            )

        # The envelope is resolved here rather than left to the dataclass
        # defaults.  Those defaults (``inherit`` plus the process working
        # directory) describe a posture, but not one anybody chose: the task
        # the user asked for in conversation would run under whatever the
        # global config happened to say, writing wherever the gateway
        # happened to be started from.  Naming both explicitly is what makes
        # "which profile is this?" answerable from the task's own definition.
        from agent.scheduler.profiles import (
            PERMISSION_PROFILES,
            resolve_permission_profile,
        )

        requested_profile = str(permission_profile or "").strip()
        if requested_profile and requested_profile not in PERMISSION_PROFILES:
            # Refused, not silently substituted.  A narrower profile would
            # still be an envelope nobody picked, and the caller that asked
            # for it deserves to hear that the name is wrong rather than
            # discover the substitution when the task fails at 3am.
            raise ValueError(
                f"Unsupported permission_profile '{requested_profile}'; "
                "expected one of: " + ", ".join(PERMISSION_PROFILES)
            )
        profile = resolve_permission_profile(requested_profile)

        explicit_workspace = str(workspace_root or "").strip()
        chosen_workspace = (
            Path(explicit_workspace).expanduser().resolve(strict=False)
            if explicit_workspace
            else self._chosen_workspace_root()
        )
        if profile.requires_workspace_root and chosen_workspace is None:
            raise ValueError(
                f"权限策略「{profile.label}」需要显式指定项目文件夹，"
                "不能回落到服务进程的当前目录"
            )
        task_workspace = chosen_workspace or self._active_workspace_root()
        if task_kind == "agent_prompt" and not task_workspace.is_dir():
            raise ValueError(f"项目文件夹不存在：{task_workspace}")

        store = self._schedule_store()
        acceptance = Acceptance(
            criteria=[str(item) for item in (criteria or []) if str(item).strip()],
            verify_command=str(verify_command or "").strip(),
        )
        new_task = NewScheduledTask(
            name=name,
            kind=task_kind,
            trigger=trigger,
            payload=payload,
            delivery_mode=resolved_mode,
            delivery_target=target,
            workspace_root=str(task_workspace),
            permission_profile=profile.key,
            acceptance=acceptance,
        )
        task = store.find_matching_task(new_task)
        existing = task is not None
        if task is None:
            task = store.create_task(new_task)
        return self._ok(
            task={
                "id": task.id,
                "name": task.name,
                "kind": task.kind,
                "delivery_mode": task.delivery_mode,
                "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                # Echoed back so the caller can confirm the envelope that was
                # actually stored instead of assuming the defaults held.
                "permission_profile": task.permission_profile,
                "workspace_root": task.workspace_root,
                "acceptance": task.acceptance.to_dict(),
                "db_path": str(shared.SCHEDULER_DB_FILE),
                "existing": existing,
            },
            summary_text=(
                f"{summary_text}\n运行权限：{profile.label}；项目文件夹：{task_workspace}"
                f"{self._describe_acceptance(task.acceptance)}"
            ),
        )

    @staticmethod
    def _describe_acceptance(acceptance: Any) -> str:
        """What this task will be judged by, in one line for the user.

        Said out loud rather than stored quietly, because a criterion nobody
        was told about is indistinguishable from no criterion -- and the
        difference decides whether a failed run looks like a bug.
        """
        criteria = list(getattr(acceptance, "criteria", []) or [])
        command = str(getattr(acceptance, "verify_command", "") or "").strip()
        if not criteria and not command:
            return ""
        parts = []
        if criteria:
            parts.append("；".join(str(item) for item in criteria))
        if command:
            parts.append(f"验收命令：{command}")
        return "\n判定成功的依据：" + "；".join(parts)

    @staticmethod
    def _action_payload(
        action_type: Optional[str],
        *,
        prompt: str = "",
        message_text: Optional[str] = None,
        instruction: Optional[str] = None,
        job_name: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        """Translate a tool's action vocabulary into a task kind and payload.

        One definition, because two callers need it: creating a single
        scheduled task, and creating each step of a workflow.  A workflow whose
        steps understood ``action_type`` differently from a standalone task
        would be a second dialect of the same language.
        """
        normalized = str(action_type or "message").strip().lower()
        if normalized == "message":
            text = str(message_text or prompt).strip()
            if not text:
                raise ValueError("`message_text` is required for message actions")
            return "message", {"message_text": text}
        if normalized == "agent_task":
            text = str(instruction or prompt).strip()
            if not text:
                raise ValueError("`instruction` is required for agent_task actions")
            return "agent_prompt", {"prompt": text}
        if normalized == "system_job":
            text = str(job_name or "").strip()
            if not text:
                raise ValueError("`job_name` is required for system_job actions")
            return "system_job", {"job_name": text}
        raise ValueError(f"Unsupported action_type '{action_type}'")

    def _schedule_list(self) -> dict[str, Any]:
        store = self._schedule_store()
        tasks = store.list_tasks()
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
        """Delete a task, and its run history with it.

        Which tasks may be deleted here is the store's answer, not this
        tool's: a running task, and a step of a workflow that still exists,
        are refused with a reason.  Asking the store rather than checking
        first is what keeps the rule in one place -- the interface refuses
        the same two cases, and a step left behind by a deleted workflow is
        deletable from both.
        """
        store = self._schedule_store()
        store.delete_task(task_id)
        return self._ok(task_id=task_id, deleted=True)

    def _workflow_create(
        self,
        name: str,
        steps: list[dict[str, Any]],
        description: str = "",
        workspace_root: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a chain of tasks whose order is the graph, not a clock.

        The whole reason this exists as one tool rather than as "call
        schedule_create a few times" is the edges.  Two tasks that happen to
        subscribe to each other's signals look identical, from the signals
        alone, to two tasks that were meant to be one job -- and the
        difference is exactly what a failure has to know: when a step fails,
        the steps below it must not run, and only the graph can say which
        those are.  Building the chain here records that.

        Every refusal comes from the same validator the store uses on write,
        so a graph rejected here is rejected before any row exists.  A
        workflow row with no tasks behind it is the failure mode this tool is
        shaped to make impossible: it looks like a plan and runs nothing.
        """
        from agent.scheduler import Acceptance, Workflow, WorkflowStep
        from agent.scheduler.profiles import resolve_permission_profile

        raw_steps = [item for item in (steps or []) if isinstance(item, dict)]
        if not raw_steps:
            raise ValueError("`steps` 至少要有一个步骤")

        # Resolved once for the whole graph.  A step without a folder of its
        # own inherits the entry step's, so this is what decides where the
        # chain runs -- and leaving it to the store's fallback would put it
        # wherever the gateway happened to be started from, which is the one
        # thing nobody can predict from the graph's own definition.
        chosen_workspace = self._chosen_workspace_root()
        fallback_workspace = self._active_workspace_root()

        built: list[WorkflowStep] = []
        for index, raw in enumerate(raw_steps, start=1):
            key = str(raw.get("key") or "").strip()
            label = f"步骤「{key or index}」"
            kind, payload = self._action_payload(
                raw.get("action_type"),
                instruction=raw.get("instruction"),
                message_text=raw.get("message_text"),
                job_name=raw.get("job_name"),
            )
            depends_on = [
                str(item).strip()
                for item in (raw.get("depends_on") or [])
                if str(item).strip()
            ]
            # Only an entry step carries a trigger.  A dependent step's
            # trigger *is* its upstreams, and giving it both would be two
            # answers to "when does this run".  Refused rather than ignored:
            # dropping a `trigger_type` the caller wrote down would answer
            # "run this daily at 10" with a step that runs at no particular
            # time, and say nothing about having done so.
            trigger_fields = {
                "trigger_type": raw.get("trigger_type"),
                "signal_name": raw.get("signal_name"),
                "at": raw.get("at"),
                "every": raw.get("every"),
                "unit": raw.get("unit"),
                "time_of_day": raw.get("time_of_day"),
                "day_of_week": raw.get("day_of_week"),
                "day_of_month": raw.get("day_of_month"),
            }
            asked_when = [
                field for field, value in trigger_fields.items()
                if value is not None and str(value).strip()
            ]
            if depends_on and asked_when:
                raise ValueError(
                    f"{label}既有上游（{'、'.join(depends_on)}），又写了"
                    f"{'、'.join(asked_when)}。它的触发方式由上游决定，"
                    "不能再另外指定；否则「什么时候运行」会有两个答案。"
                    "如果这一步真的需要按时间运行，把它改成没有上游的入口步骤。"
                )
            trigger = None
            if not depends_on:
                trigger_type = str(raw.get("trigger_type") or "").strip()
                if trigger_type:
                    trigger = self._schedule_trigger(
                        trigger_type=trigger_type,
                        timezone_name=str(raw.get("timezone_name") or "UTC"),
                        at=raw.get("at"),
                        every=raw.get("every"),
                        unit=raw.get("unit"),
                        time_of_day=raw.get("time_of_day"),
                        day_of_week=raw.get("day_of_week"),
                        day_of_month=raw.get("day_of_month"),
                        signal_name=raw.get("signal_name"),
                    )
            profile = resolve_permission_profile(raw.get("permission_profile"))
            own_workspace = str(raw.get("workspace_root") or "").strip()
            if own_workspace:
                step_workspace = str(
                    Path(own_workspace).expanduser().resolve(strict=False)
                )
            elif chosen_workspace is not None:
                step_workspace = str(chosen_workspace)
            elif profile.requires_workspace_root:
                # Same refusal, and for the same reason, as ``schedule_create``:
                # a write-granting step pinned to the process working directory
                # would write somewhere nobody can derive from the graph.
                raise ValueError(
                    f"{label}使用了权限策略「{profile.label}」，需要显式指定项目文件夹，"
                    "不能回落到服务进程的当前目录"
                )
            else:
                step_workspace = str(fallback_workspace)
            mode, target = self._schedule_target(raw.get("delivery_mode"))
            built.append(
                WorkflowStep(
                    key=key,
                    name=str(raw.get("name") or key or label).strip(),
                    kind=kind,
                    payload=payload,
                    depends_on=depends_on,
                    trigger=trigger,
                    workspace_root=step_workspace,
                    permission_profile=profile.key,
                    acceptance=Acceptance(
                        criteria=[
                            str(item)
                            for item in (raw.get("criteria") or [])
                            if str(item).strip()
                        ],
                        verify_command=str(raw.get("verify_command") or "").strip(),
                    ),
                    delivery_mode=mode,
                    delivery_target=target,
                )
            )

        workflow = Workflow(
            name=str(name or "").strip(),
            description=str(description or "").strip(),
            steps=built,
        )
        store = self._schedule_store()
        created = store.create_workflow(workflow)
        tasks = store.step_tasks(created.id)
        return self._ok(
            workflow={
                "id": created.id,
                "name": created.name,
                "description": created.description,
                "enabled": created.enabled,
                "steps": [
                    {
                        "key": step.key,
                        "name": step.name,
                        "kind": step.kind,
                        "depends_on": list(step.depends_on),
                        "trigger": (
                            json.loads(step.trigger.to_json())
                            if step.trigger is not None
                            else None
                        ),
                        "task_id": tasks[step.key].id if step.key in tasks else None,
                        "workspace_root": step.workspace_root,
                        "permission_profile": step.permission_profile,
                        "acceptance": step.acceptance.to_dict(),
                    }
                    for step in created.steps
                ],
                "db_path": str(shared.SCHEDULER_DB_FILE),
            },
            summary_text=self._describe_workflow(created),
        )

    @staticmethod
    def _describe_workflow(workflow: Any) -> str:
        """The chain, in the order it will run, as a sentence.

        Written out rather than summarised as a count, because the two things
        that go wrong with a graph -- a step in the wrong place, and a step
        whose criterion was not what the person meant -- are both visible in
        the sequence and invisible in "created 3 steps".
        """
        lines = [f"已创建流程「{workflow.name}」，共 {len(workflow.steps)} 个步骤："]
        for step in workflow.steps:
            upstreams = "、".join(str(item) for item in step.depends_on)
            when = f"在 {upstreams} 成功后运行" if upstreams else "按自身触发方式运行"
            lines.append(f"- {step.key}（{step.name}）：{when}")
            criteria = list(getattr(step.acceptance, "criteria", []) or [])
            command = str(getattr(step.acceptance, "verify_command", "") or "").strip()
            if criteria or command:
                judged = "；".join(str(item) for item in criteria)
                if command:
                    judged = f"{judged}；验收命令：{command}" if judged else f"验收命令：{command}"
                lines.append(f"  判定成功的依据：{judged}")
        return "\n".join(lines)

    def _workflow_list(self) -> dict[str, Any]:
        store = self._schedule_store()
        workflows = store.list_workflows()
        return self._ok(
            count=len(workflows),
            items=[
                {
                    "id": workflow.id,
                    "name": workflow.name,
                    "description": workflow.description,
                    "enabled": workflow.enabled,
                    "steps": [
                        {
                            "key": step.key,
                            "name": step.name,
                            "depends_on": list(step.depends_on),
                        }
                        for step in workflow.steps
                    ],
                }
                for workflow in workflows
            ],
        )

    def _workflow_delete(self, workflow_id: str) -> dict[str, Any]:
        """Stop a workflow, keeping the record that it ran.

        The tasks behind it are disabled rather than deleted, which is what the
        store does; this only says so, because "deleted" would suggest the run
        history went with it and somebody reading the log later would not know
        to look for it.  Nothing else refers to the graph afterwards, so the
        leftovers are ordinary tasks from that moment on: they show up in the
        task list and schedule_delete removes them.
        """
        store = self._schedule_store()
        disabled = store.delete_workflow(workflow_id)
        return self._ok(
            workflow_id=workflow_id,
            deleted=True,
            disabled_task_ids=disabled,
        )

    def _report_outcome(self, reason: str) -> dict[str, Any]:
        """Record that this run could not do its job.

        There is deliberately no way to report success.  An unattended run is
        judged by its acceptance check, and an agent's own assurance that it
        did well is a claim rather than evidence -- offering a parameter that
        could only ever be used in the harmful direction would be an invitation
        to use it.

        Refused outside a scheduled run rather than quietly accepted: in a
        conversation there is a person to tell, and a tool that wrote a verdict
        to nowhere would look as though it had done something.
        """
        text = str(reason or "").strip()
        if not text:
            raise ValueError("`reason` is required")
        report = _active_run_self_report.get()
        if report is None:
            raise ValueError(
                "report_outcome 只能在定时任务的运行中使用；"
                "在会话里直接说明遇到的问题即可。"
            )
        report.verdict = VERDICT_FAILED
        report.reason = text
        return self._ok(
            reported=True,
            reason=text,
            note="本次运行将被记为失败，下游步骤不会运行。",
        )

    def _emit_signal(
        self, name: str, payload: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Record a signal so whoever is waiting on it runs.

        Deliberately does *not* run the subscriber here.  Emitting and running
        are separate steps on purpose: this call may be made from inside a run
        that the scheduler is currently executing, and starting another run
        from there would nest one scheduler lifetime inside another.  The
        scheduler picks the emission up on its next pass, which is also what
        lets an emission survive a process that stops right after it.
        """
        store = self._schedule_store()
        parent = _active_signal_context.get() or {}
        try:
            emission = store.emit_signal(
                name,
                payload if isinstance(payload, dict) else {},
                source="agent",
                depth=int(parent.get("depth", 0) or 0) + 1 if parent else 0,
                origin_id=str(parent.get("origin_id", "") or "") if parent else "",
            )
        except ValueError as exc:
            return self._error(str(exc))
        subscribers = [
            task.name
            for task in store.list_tasks()
            if task.enabled
            and task.trigger.trigger_type == "signal"
            and str(task.trigger.payload.get("name", "")).strip() == emission.name
        ]
        return self._ok(
            emission_id=emission.id,
            name=emission.name,
            depth=emission.depth,
            cascade_id=emission.origin_id,
            subscribers=subscribers,
            summary_text=(
                f"信号「{emission.name}」已发出，{len(subscribers)} 个任务会在下一轮运行。"
                if subscribers
                else f"信号「{emission.name}」已记录，但目前没有任务订阅它。"
            ),
        )

    def _list_signals(self) -> dict[str, Any]:
        store = self._schedule_store()
        emissions = store.signal_names()
        subscribers: dict[str, int] = {}
        for task in store.list_tasks():
            if not task.enabled or task.trigger.trigger_type != "signal":
                continue
            key = str(task.trigger.payload.get("name", "")).strip()
            subscribers[key] = subscribers.get(key, 0) + 1
        return self._ok(
            count=len(emissions),
            items=[
                {
                    "name": entry["name"],
                    "last_emitted_at": entry.get("last_at"),
                    "emission_count": entry.get("count"),
                    "subscriber_count": subscribers.get(entry["name"], 0),
                }
                for entry in emissions
            ],
            # Names nobody has emitted yet cannot appear above, but a
            # subscription written up front still exists and is worth seeing.
            waiting_on_unemitted=[
                {"name": key, "subscriber_count": value}
                for key, value in sorted(subscribers.items())
                if key and key not in {entry["name"] for entry in emissions}
            ],
        )

    def _step_output_limit(self, requested: Any) -> tuple[int, str]:
        """How much of an upstream's output one read may bring in, and why.

        The ceiling comes from this run's input budget, which the agent core
        publishes before every provider call, rather than from a constant: the
        flat 40000 was three quarters of a 53841-token budget in a single call,
        so a run could walk into the limit and lose everything it had done --
        the failure mode being a provider error that says nothing about the
        tool that caused it.

        Returns the limit and a sentence explaining it when the budget is what
        bound, because a caller told "you got less than you asked for" with no
        reason will simply ask again.
        """
        budget = 0
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            if active_ctx is not None:
                metadata = getattr(active_ctx, "metadata", {}) or {}
                budget = int(metadata.get("_last_input_token_budget", 0) or 0)
        ceiling = READ_STEP_OUTPUT_MAX_CHARS
        if budget > 0:
            ceiling = min(ceiling, max(1, budget // READ_STEP_OUTPUT_BUDGET_SHARE))
        wanted = int(requested or 0) or READ_STEP_OUTPUT_DEFAULT_CHARS
        limit = max(1, min(wanted, ceiling))
        if budget > 0 and wanted > ceiling:
            return limit, (
                f"本次运行的输入预算约 {budget} tokens，单次读取最多用掉其中 "
                f"{budget // READ_STEP_OUTPUT_BUDGET_SHARE} tokens，"
                f"所以这里最多返回 {limit} 个字符。"
            )
        return limit, ""

    def _read_step_output(
        self, step: str, max_chars: int = READ_STEP_OUTPUT_DEFAULT_CHARS
    ) -> dict[str, Any]:
        """The latest successful output of a step this one stands on.

        Addressed by *step key* because that is the name the graph gives a
        step and the only one a model can be expected to know: task ids and run
        ids are internal, and a step that has to look them up is a step that
        will look them up wrong.  The workflow is taken from the run being
        served, not from an argument, so a step cannot reach outside its own
        graph by naming one.

        Restricted to ancestors of the current step, not to the whole graph.
        A step beside it has not run for this round and a step below it has not
        run at all, so reading either hands back an earlier round's text as if
        it were this round's work -- which is the failure the tool exists to
        make impossible rather than to make unlikely.

        Only ``output_path`` values the scheduler recorded are read.  That is
        the tool's whole safety property: it is not a file reader with a
        narrower name, it is a lookup into this workflow's own history, and a
        path it was not told about is not reachable through it.

        How much comes back is capped by this run's input budget as well as by
        ``max_chars`` (see ``_step_output_limit``), so asking for everything
        cannot cost the run the context it needs to act on what it read.
        """
        wanted = str(step or "").strip()
        if not wanted:
            return self._error("step 不能为空；用上游步骤的 key，例如 step2")

        context: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            if active_ctx is not None:
                context = dict(getattr(active_ctx, "metadata", {}) or {})
        workflow_id = str(context.get("scheduler_workflow_id", "") or "").strip()
        if not workflow_id:
            return self._error(
                "当前运行不属于任何流程，read_step_output 只能读同一条流程里的步骤。"
                "单独的任务请用 read_file 或 shell 读它的产物路径。"
            )

        store = self._schedule_store()
        steps = store.step_tasks(workflow_id)
        target = steps.get(wanted)
        if target is None:
            available = "、".join(sorted(steps)) or "（无）"
            return self._error(
                f"流程里没有步骤「{wanted}」。可选的步骤：{available}"
            )
        if wanted == str(context.get("scheduler_step_key", "") or "").strip():
            # Reading your own output would return the previous round's text
            # as if it were the upstream's, which is the kind of answer that
            # looks right and is not.
            return self._error(
                f"「{wanted}」就是当前这一步，读它只会拿到自己上一轮的结果。"
            )
        workflow = store.get_workflow(workflow_id)
        current_key = str(context.get("scheduler_step_key", "") or "").strip()
        ancestors = (
            _workflow_ancestors(workflow.steps, current_key)
            if workflow is not None
            else set()
        )
        if wanted not in ancestors:
            # Same reasoning as the line above, one edge further out: a step
            # that has not run yet, or ran in an earlier round, has nothing
            # about *this* round to hand over.
            known = "、".join(sorted(ancestors)) or "（无）"
            return self._error(
                f"「{wanted}」不是当前这一步的上游，读它只会拿到它上一轮的文本。"
                f"当前步骤的上游：{known}"
            )

        latest = None
        for run in store.list_runs(target.id):
            if run.status == "succeeded" and str(run.output_path or "").strip():
                latest = run
        if latest is None:
            return self._error(
                f"步骤「{wanted}」还没有成功过，所以没有产物可读。"
            )

        path = Path(str(latest.output_path)).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self._error(f"读取步骤「{wanted}」的输出失败：{exc}")

        limit, budget_note = self._step_output_limit(max_chars)
        truncated = len(text) > limit
        notes: list[str] = []
        if truncated:
            notes.append(f"只返回了前 {limit} 个字符，共 {len(text)} 个。")
        if budget_note:
            notes.append(budget_note)
        if notes:
            notes.append("需要其余内容就读上面的 output_path。")
        return self._ok(
            step=wanted,
            task_id=target.id,
            task_name=target.name,
            run_id=latest.id,
            finished_at=latest.finished_at.isoformat() if latest.finished_at else None,
            summary=latest.summary,
            output_path=str(path),
            text=text[:limit],
            truncated=truncated,
            total_chars=len(text),
            # Said out loud rather than left to be discovered: a step that
            # believes it read the whole report and got two thirds of it will
            # act on the two thirds.
            note=" ".join(notes),
        )

    def _clean_output(
        self, max_age_hours: float = 0, subdir: str = ""
    ) -> dict[str, Any]:
        if self._output_dir is None:
            return self._error("Output directory not configured")
        output_dir = self._output_dir.expanduser().resolve(strict=False)
        target = (output_dir / subdir).resolve(strict=False) if subdir else output_dir
        if not path_contains(output_dir, target):
            return self._error("subdir must stay inside the output directory")
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

    def _clear_context(self, summary: str = "") -> dict[str, Any]:
        """Request a context reset after the current tool batch finishes.

        The agent loop applies the reset after all concurrently requested tools
        return. Mutating ctx.messages from inside one concurrent tool would
        leave sibling tool results detached from their assistant tool-call.
        """
        try:
            from agent.core.agent import _active_agent_context

            ctx = _active_agent_context.get()
            if ctx is None or not ctx.messages:
                return self._error(
                    "No active agent context available to clear. "
                    "This tool must be used during an active conversation."
                )

            def _text_from_user_message(msg: dict[str, Any]) -> str:
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Anthropic tool results are represented as role=user
                    # messages too; skip those when finding the active task.
                    if any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    ):
                        return ""
                    return "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                return str(content or "")

            # Find the latest real user request. In a multi-turn conversation,
            # the first user message may belong to an old task.
            current_user = None
            for msg in reversed(ctx.messages):
                if msg.get("role") == "user":
                    content = _text_from_user_message(msg).strip()
                    if content and str(content).strip():
                        current_user = str(content).strip()
                        break

            if not current_user:
                return self._error("No current user message found in context.")

            # Build a clean context with the summary and current task.
            summary_text = str(summary or "").strip()
            restart_message = (
                "[Context has been cleared to reduce token usage.]\n\n"
            )
            if summary_text:
                restart_message += (
                    f"## Summary of work so far\n{summary_text}\n\n"
                )
            restart_message += (
                f"## Current request\n{current_user}\n\n"
                "Please continue from where you left off, using the summary "
                "above as context for what has already been done."
            )

            return self._ok(
                clear_context_requested=True,
                restart_message=restart_message,
                current_request=current_user[:500],
                summary_provided=bool(summary_text),
            )
        except ImportError:
            return self._error(
                "clear_context is not available — agent runtime not accessible."
            )
        except Exception as e:
            return self._error(f"Failed to clear context: {e}")
