from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import shlex
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from agent import shared
from agent.core.output import _active_sink
from agent.exec import ExecRequest, provider_from
from agent.exec.subprocess import OUTPUT_MAX_BYTES
from agent.pathing import path_contains, resolve_workspace_path
from agent.scheduler.models import LOCAL_TIMEZONE
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
from agent.tools.files import FileProvider, _normalize_write_scope

from .executor import report_tool_progress
from .memory_tools import MemoryTools
from .output_tools import OutputTools
from .file_tools import FileTools
from .signal_tools import SignalTools
from .schedule_tools import ScheduleTools
from .step_output_tools import (
    READ_STEP_OUTPUT_DEFAULT_CHARS,
    READ_STEP_OUTPUT_MAX_CHARS,
    StepOutputTools,
)
from .web_tools import TAVILY_SEARCH_MAX_RESULTS, WebTools
from .runtime import ToolRegistry


# ── Constants ─────────────────────────────────────────────────────────────────

_atomic_write_text = shared._atomic_write_text



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




#: One step, as a tool call writes it.  Shared by ``workflow_create`` and
#: ``workflow_update`` because they are the same language applied to a
#: different starting point: a chain edited with a vocabulary that had drifted
#: from the one it was created with is a chain that means something different
#: after the edit, and the difference would show up as a step that runs at the
#: wrong time rather than as an error.
_WORKFLOW_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": (
                "Short stable id for this step, used by other steps' "
                "depends_on, e.g. collect or summarise. On an edit this is the "
                "identity: a key already in the chain is that same step and "
                "keeps everything the call does not mention, and a key that is "
                "not there yet is a new step. Changing a key makes a new step "
                "and abandons the old one's run history."
            ),
        },
        "name": {"type": "string", "description": "Short step name"},
        "action_type": {
            "type": "string",
            "description": (
                "one of: message, agent_task, system_job. "
                "Inferred from the content field when left "
                "out -- `instruction` makes it an agent "
                "task, `message_text` a message, "
                "`job_name` a system job -- so filling in "
                "only one of those is enough."
            ),
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
        "timezone_name": {
            "type": "string",
            "default": LOCAL_TIMEZONE,
            "description": (
                "IANA timezone name. Omit to use this machine's timezone."
            ),
        },
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
        "produces": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The files this step has to leave behind, as "
                "paths relative to its project folder. Declare "
                "them on the step that writes them: the run is "
                "then told where to write instead of guessing, "
                "and the steps below are handed the resolved "
                "absolute paths of the ones that exist. A "
                "declared file that is missing when the step "
                "ends fails that step, so the steps below it do "
                "not run on nothing."
            ),
        },
    },
    # `action_type` is deliberately not required: the content field names the
    # action, and requiring the label as well refused the obvious spelling of a
    # step with a complaint about a field its author never used -- see
    # ``action_payload``.
    "required": ["key", "name"],
}


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
        self._memory_tools = MemoryTools(
            self.registry, memory, self.chapter_normalizer, self._ok, self._error
        )
        self._web_tools = WebTools(self.registry, self._ok, self._error)
        self._output_dir = output_dir
        self._output_tools = OutputTools(
            self.registry, output_dir, self._ok, self._error
        )
        self._signal_tools = SignalTools(
            self.registry, self._schedule_store, self._ok, self._error
        )
        self._step_output_tools = StepOutputTools(
            self.registry, self._schedule_store, self._ok, self._error
        )
        self._schedule_tools = ScheduleTools(
            self.registry,
            self._schedule_store,
            self._active_workspace_root,
            self._chosen_workspace_root,
            self._ok,
            self._error,
        )
        self._file_tools = FileTools(
            self.registry,
            self.workspace_root,
            file_service,
            self._active_workspace_root,
            self._process_output_dir,
            self._ok,
            self._error,
        )
        self._cached_schedule_store: Any = None
        self._register()

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
        fallback here -- see ``schedule_create``, which refuses a
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
            self._file_tools.read_file,
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
            self._file_tools.write_file,
            authorizer=self._file_tools.file_mutation_authorizer,
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
            self._file_tools.send_file,
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
            self._file_tools.list_files,
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
            self._file_tools.edit_file,
            authorizer=self._file_tools.file_mutation_authorizer,
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
            self._memory_tools.write,
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
            self._memory_tools.set_identity,
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
            self._memory_tools.read,
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
            self._memory_tools.search,
            source="builtin",
        )

        r.register(
            "memory_index",
            "Show the memory palace index.",
            {"type": "object", "properties": {}, "required": []},
            self._memory_tools.index,
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
            self._memory_tools.clear,
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
                "The user has to ask: a question about how something works, or about the "
                "process behind it, is answered in the reply and is not a request to add a "
                "task -- if one would help, propose it and let the user say yes. "
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
                "task should work in a directory other than the current session's. "
                "The task outlives this conversation, so `intent` has to carry the "
                "user's own words: it is checked against the request that commissioned "
                "this turn, and a call whose intent cannot be found in it is refused."
            ),
            {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": (
                            "Fill this in, or the call is refused. Quote the "
                            "user, verbatim -- at least six characters copied "
                            "from what they actually typed this turn, e.g. "
                            "their sentence 「每天早上九点提醒我看盘」. "
                            "It is matched against this turn's request, so a "
                            "paraphrase, a translation, or a sentence about why "
                            "the task would be useful is refused. If nothing in "
                            "this turn asked for a task, do not call this at "
                            "all: propose it in your reply and let the user ask."
                        ),
                    },
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
                        "description": (
                            "IANA timezone name like Asia/Shanghai. Omit it to use "
                            "the timezone this machine is already in -- a wall-clock "
                            "time that names no zone means the local one, never UTC."
                        ),
                        "default": LOCAL_TIMEZONE,
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
                    "produces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The files this task has to leave behind, as paths "
                            "relative to the project folder -- e.g. report.md or "
                            "state/equity.csv. Fill this in whenever the work's "
                            "product is a file: it is the only way the run is told "
                            "where to write (until now the path existed only in "
                            "prose, or only inside verify_command, checking a file "
                            "the run was never told about), it is what a downstream "
                            "task is handed, and a declared file that is missing "
                            "when the run ends fails the run on its own. Must be "
                            "relative and inside the folder: no absolute paths, no "
                            "'..'. Omit for a task whose only output is its answer."
                        ),
                    },
                },
                "required": ["name", "trigger_type"],
            },
            self._schedule_tools.schedule_create,
            source="builtin",
        )

        r.register(
            "schedule_list",
            "List persistent scheduled tasks.",
            {"type": "object", "properties": {}, "required": []},
            self._schedule_tools.schedule_list,
            source="builtin",
        )

        r.register(
            "schedule_runs",
            (
                "Read a scheduled task's run history -- when each run happened, "
                "whether it succeeded, and, when it did not, why -- together with "
                "the task's whole current definition (its instruction, its trigger, "
                "its criteria, the files it declares, its permission posture), in "
                "the same vocabulary schedule_update takes -- that is, every field "
                "under `task` except `id`, `workflow_id`, `step_key`, `enabled`, "
                "`delivery_mode`, `delivery_target` and `request_quote`, which are "
                "reported for reading only because the graph, "
                "schedule_set_enabled and the channel own them. Use it after "
                "schedule_create or workflow_create to find out what actually "
                "happened, and whenever the user asks whether a task ran, whether "
                "it worked, or what went wrong with it. A scheduled run happens "
                "with nobody watching and its outcome exists nowhere else, so this "
                "is the only way to tell a task that has been succeeding from one "
                "that has failed every night since it was made. Returns the run's "
                "`error` and the acceptance check's own result; a run's full text "
                "is at the `output_path` it returns. The most common failure is a "
                "defect in the definition rather than in the work, so read the "
                "definition here and fix it with schedule_update -- retrying a "
                "definition that cannot be satisfied only fails again."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "The task's id, from schedule_list or workflow_list."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many recent runs to return (default 10, max 50).",
                        "default": 10,
                    },
                },
                "required": ["task_id"],
            },
            self._schedule_tools.schedule_runs,
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
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this task is being deleted, and what "
                            "it was for."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.schedule_delete,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "schedule_update",
            (
                "Change part of an existing scheduled task. Only the fields named in "
                "the call change; everything else keeps the value the task already "
                "has. Use this whenever a task is wrong rather than missing -- the "
                "verify_command names a file the step never writes, the criterion is "
                "not what the user meant, the instruction needs a line added, a "
                "declared product was never filled in. Read the current definition "
                "first with schedule_runs, which returns it in exactly the vocabulary "
                "this takes. "
                "The edit keeps the task id, so its run history survives and every "
                "step subscribed to its signal keeps working -- which is why this, "
                "and not deleting the task and creating it again, is how a definition "
                "is corrected. Deleting takes the id and the history with it. "
                "If the task is a step of a workflow, its trigger belongs to the graph "
                "and cannot be changed here; edit the workflow instead. Whether the "
                "task runs at all is schedule_set_enabled's. Pass only the fields you "
                "are changing: a field sent with the same value it already had is "
                "harmless, and `changed` in the reply says what actually moved."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list or schedule_runs",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. What this edit does and why, specifically "
                            "-- e.g. 「把 verify_command 改成检查 07_正文.md，它实际写在 "
                            "日期目录下」."
                        ),
                    },
                    "name": {"type": "string", "description": "New task name"},
                    "action_type": {
                        "type": "string",
                        "description": "one of: message, agent_task, system_job",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "New agent instruction (action_type=agent_task)",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Same as `prompt`; accepted for symmetry with workflow_create",
                    },
                    "message_text": {
                        "type": "string",
                        "description": "New literal message (action_type=message)",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Internal system job name (action_type=system_job)",
                    },
                    "trigger_type": {
                        "type": "string",
                        "description": (
                            "New trigger kind: once, interval, daily, weekly, "
                            "weekdays, monthly, signal. Refused on a step that "
                            "waits on upstreams."
                        ),
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO datetime, for trigger_type=once or interval",
                    },
                    "every": {"type": "integer", "description": "Interval count"},
                    "unit": {
                        "type": "string",
                        "description": "minutes|hours|days|weeks, for interval",
                    },
                    "time_of_day": {
                        "type": "string",
                        "description": "HH:MM for daily/weekly/weekdays/monthly",
                    },
                    "day_of_week": {"type": "string", "description": "mon|tue|...|sun"},
                    "day_of_month": {"type": "integer", "description": "1-31"},
                    "signal_name": {
                        "type": "string",
                        "description": "Signal to wait for, when trigger_type=signal",
                    },
                    "timezone_name": {
                        "type": "string",
                        "description": "IANA timezone name, e.g. Asia/Shanghai",
                    },
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replaces the success criteria. Sending [] removes "
                            "them -- which is an answer, not an omission: a "
                            "field left out is what keeps them."
                        ),
                    },
                    "verify_command": {
                        "type": "string",
                        "description": (
                            "Replaces the acceptance command whose exit code "
                            "decides success. Must be a single low-risk command: "
                            "no pipes, redirection, `rm`, or inline interpreters."
                        ),
                    },
                    "produces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replaces the files this task declares it will leave "
                            "behind, as paths relative to the project folder. "
                            "Call out [] to declare none."
                        ),
                    },
                    "permission_profile": {
                        "type": "string",
                        "description": "inherit|read_only|workspace_write",
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": "Absolute path of the project folder the run works in",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "How long one run may take, 10 to 604800",
                    },
                    # The four below are here so that a definition read back
                    # with ``schedule_runs`` can be handed straight in.  They
                    # are the rest of what the builder reads, and leaving them
                    # out did not hide them -- it made the whole read-back
                    # unusable, because this schema refuses unknown fields and
                    # the reply names all of them.
                    "context_policy": {
                        "type": "string",
                        "description": "stateless|task_history|shared_memory",
                    },
                    "retry_policy": {
                        "type": "object",
                        "description": (
                            "How a failed run is retried: "
                            "{max_attempts, backoff_seconds}"
                        ),
                    },
                    "selected_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replaces the skills pinned to this task. "
                            "Call out [] to pin none."
                        ),
                    },
                    "model_override": {
                        "type": ["string", "null"],
                        "description": (
                            "Model id this task's runs use; null to follow the "
                            "active provider. An id no provider group owns is "
                            "refused here rather than at 3am."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.schedule_update,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "schedule_set_enabled",
            (
                "Switch a scheduled task on or off. Use it to pause something that "
                "is failing or unwanted, and to bring it back. The task keeps its "
                "definition and its history; what changes is whether it is still "
                "triggered. A task that is already running or queued is not affected "
                "-- to stop a run in flight use schedule_cancel. Refused for a step "
                "of a workflow that still exists: a step's switch is rewritten from "
                "its workflow's on every save, so pausing the workflow is the switch "
                "that holds."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "false pauses the task, true lets it run again",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this task is being switched, "
                            "specifically."
                        ),
                    },
                },
                "required": ["task_id", "enabled", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.schedule_set_enabled,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "schedule_run",
            (
                "Start a run of a scheduled task right now instead of waiting for its "
                "next occurrence. With no `run_id` it runs the task as it is now, "
                "which is how an edit is confirmed: fix the definition with "
                "schedule_update, then run it and read the result with schedule_runs. "
                "With a `run_id` it re-runs the snapshot that run was started with, so "
                "the outcome is about the work and not about the definition -- the "
                "same flake gets another try unchanged, and a fix cannot be mistaken "
                "for one. Returns the new run id. The run itself happens in the "
                "background and may take minutes: the outcome is in schedule_runs, "
                "not in this reply. Refused while the task already has a run in "
                "flight."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list",
                    },
                    "run_id": {
                        "type": "string",
                        "description": (
                            "Optional. Re-run this earlier run's own snapshot "
                            "instead of the task as it is now."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this task is being run now, specifically."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.schedule_run,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "schedule_cancel",
            (
                "Ask a run that is in flight to stop. Use it when a run is stuck, "
                "overrunning its timeout, or clearly doing the wrong thing. It is a "
                "request, not a kill: the run stops at its next checkpoint, so the "
                "reply says the cancellation was asked for rather than done -- check "
                "schedule_runs for where it actually ended. Omit `run_id` to mean the "
                "task's current run, which is the only one it can mean. To stop a task "
                "from being triggered again, use schedule_set_enabled instead."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "Optional. Defaults to the run currently in flight.",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this run is being cancelled, "
                            "specifically."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.schedule_cancel,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "workflow_create",
            (
                "Create a chain of scheduled tasks, where each step runs only after the "
                "steps it depends on have succeeded. Use this when the user asks for "
                "several jobs to run in an order -- fetch, then transform, then report -- "
                "rather than one job; describing an existing process is not asking for a "
                "new one to be built, so propose the chain instead of creating it. "
                "Do not chain steps with schedule_create and signals "
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
                "step that can never be judged is refused with the step named. "
                "The chain outlives this conversation, so `intent` has to carry the "
                "user's own words: it is checked against the request that commissioned "
                "this turn, and a call whose intent cannot be found in it is refused."
            ),
            {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": (
                            "Fill this in, or the call is refused. Quote the "
                            "user, verbatim -- at least six characters copied "
                            "from what they actually typed this turn, the "
                            "sentence that asked for this chain. It is matched "
                            "against this turn's request, so a paraphrase or a "
                            "sentence about why the chain would be useful is "
                            "refused. If nothing in this turn asked for one, do "
                            "not call this at all: propose the chain in your reply."
                        ),
                    },
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
                        "items": _WORKFLOW_STEP_SCHEMA,
                    },
                },
                "required": ["name", "steps"],
            },
            self._schedule_tools.workflow_create,
            source="builtin",
        )

        r.register(
            "workflow_list",
            (
                "List workflows, with each one's steps and the edges between them. "
                "Use before creating a chain, to see whether one already exists."
            ),
            {"type": "object", "properties": {}, "required": []},
            self._schedule_tools.workflow_list,
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
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this chain is being deleted, and what "
                            "it was for."
                        ),
                    },
                },
                "required": ["workflow_id", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.workflow_delete,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

        r.register(
            "workflow_update",
            (
                "Change an existing chain in place: add a step, remove one, move one, "
                "reword one, rename or pause the whole chain. Use this whenever a "
                "workflow needs adjusting -- never delete it and build it again, "
                "which cuts every edge: a step's task id appears in the signal names "
                "the steps below it subscribe to, so a rebuilt chain carries on "
                "subscribed to ids nothing emits. Editing keeps those ids. "
                "Steps are matched by `key`, and that is the whole contract: a key "
                "already in the chain is the same step and keeps every field this "
                "call does not mention; a new key is a new step; a stored key missing "
                "from the list is removed, and its task is switched off rather than "
                "deleted, so its run history survives. "
                "Leave `steps` out entirely to rename or pause without restating the "
                "graph. When you do send it, send the whole list in the order the "
                "chain should run, keeping the existing keys for the steps that "
                "already exist -- changing a key abandons that step's history. Read "
                "the current chain first with workflow_list."
            ),
            {
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow id, from workflow_list",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. What this change does and why, specifically."
                        ),
                    },
                    "name": {"type": "string", "description": "New workflow name"},
                    "description": {
                        "type": "string",
                        "description": "New one-line description of what the chain is for",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": (
                            "Pause or resume every step. This is the switch that "
                            "holds for a chain, since a step cannot be switched "
                            "on its own."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "description": (
                            "The whole chain, in the order it should run; "
                            "`depends_on` gives the actual order. Omit to leave the "
                            "steps untouched. Keep the existing keys."
                        ),
                        "items": _WORKFLOW_STEP_SCHEMA,
                    },
                },
                "required": ["workflow_id", "intent"],
                "additionalProperties": False,
            },
            self._schedule_tools.workflow_update,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
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
            "need no call. Call `list_signals` first to see which names are already in use. "
            "Emitting starts whatever is waiting on that name, so `intent` has to carry "
            "the user's own words: it is checked against the request that commissioned "
            "this turn, and a call whose intent cannot be found in it is refused."
            ),
            {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": (
                            "Fill this in, or the call is refused. Quote the "
                            "user, verbatim -- at least six characters copied "
                            "from what they actually typed this turn, e.g. "
                            "「把 xhs.package.ready 这个信号发出去」. An emission "
                            "wakes tasks, so it is an action rather than a note, "
                            "and it is matched against this turn's request. If "
                            "nothing in this turn asked for a signal, say what "
                            "you would emit in your reply instead."
                        ),
                    },
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
            self._signal_tools.emit_signal,
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
            self._signal_tools.list_signals,
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
            self._step_output_tools.read,
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
            self._signal_tools.report_outcome,
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
                        "description": f"Maximum number of results to return (1-{TAVILY_SEARCH_MAX_RESULTS})",
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
            self._web_tools.search,
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
            self._web_tools.fetch,
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
            self._web_tools.tavily_search,
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
                self._output_tools.clean,
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
        """Trigger a plugin reload from the four names the reload actually reads.

        This used to hand ``PluginCatalog.reload`` the whole component
        container.  The reload reads exactly four keys of it --
        ``command_router``, ``skill_catalog``, ``mcp_client`` and ``registry``
        -- so the container is rebuilt from those names here, and the
        requirements are now legible at the call site instead of implied by a
        container that could also have been used for anything else.
        """
        plugin_catalog = self.registry.get_context("plugin_catalog")
        if plugin_catalog is None:
            return {
                "ok": False,
                "error": "plugin_catalog not wired into registry context",
            }
        components = {
            "command_router": self.registry.get_context("command_router"),
            "skill_catalog": self.registry.get_context("skill_catalog"),
            "mcp_client": self.registry.get_context("mcp_client"),
            "registry": self.registry.get_context("registry"),
        }
        try:
            return await plugin_catalog.reload(components)
        except Exception as exc:
            return {"ok": False, "error": f"reload failed: {exc}"}

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

    def _ok(self, **payload: Any) -> dict[str, Any]:
        return {"ok": True, **payload}

    def _error(self, message: str, **payload: Any) -> dict[str, Any]:
        return {"ok": False, "error": message, **payload}

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
            if approved is None:
                # Nobody could be asked: the sink has no dialog (a gateway, a pipe, a
                # scheduled run's sink, a browser that is not connected).  **This is not a
                # refusal**, and retiring the token here is what made `/confirm <token>`
                # answer "invalid, expired, or belongs to another user" for the one kind of
                # sink that command exists for.  The pending record stays, and the caller
                # keeps the structured confirmation-required result the human redeems later.
                return False
            if not approved:
                # Asked and declined: terminal, so retire the token -- no later approval
                # reply may redeem the command the human just refused.
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
                workspace_before = self._file_tools.workspace_file_snapshot()
        else:
            if not workspace_read and self._file_tools.path_is_inside_workspace(
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
                moved_artifacts = self._file_tools.move_new_workspace_files_to_output_dir(
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
            resolved, _root_kind = self._file_tools.resolve_tool_path(path)
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
            from agent.scheduler import open_store
            self._cached_schedule_store = open_store()
        return self._cached_schedule_store

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
