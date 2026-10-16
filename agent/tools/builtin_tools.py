"""Built-in tools: the host, and the registrations it performs.

Every cluster of tools has moved to a module beside this one -- memory, web,
output, signals, step output, the scheduler and workflows, files, audio,
context, shell, and the plugin/user-tool tools.  What is left is the host: the
``_register`` table that binds each tool name to the cluster that implements it,
the shared accessors the clusters are wired with (``_ok``, ``_error``,
``_schedule_store``, the workspace roots, ``_try_interactive_confirmation``),
and the construction of the cluster objects themselves.

The survey's remaining question is whether each cluster should own its own
registrations rather than being mounted here; that is a larger change than the
moves, and it is recorded in
``docs/superpowers/plans/2026-10-09-big-file-survey.md`` rather than attempted
by accident.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from agent import shared
from agent.core.output import _active_sink
from agent.scheduler.models import LOCAL_TIMEZONE
from agent.tools.files import FileProvider

from . import builtin_signal_tools
from . import builtin_web_tools
from . import builtin_plugin_tools
from . import builtin_schedule_tools
from . import builtin_memory_tools
from . import builtin_file_tools
from .memory_tools import MemoryTools
from .output_tools import OutputTools
from .file_tools import FileTools
from .audio_tools import AudioTools
from .context_tools import ContextTools
from .shell_tools import ShellTools
from .plugin_tools import PluginTools
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




#: One step, as a tool call writes it.  Shared by ``workflow_create`` and
#: ``workflow_update`` because they are the same language applied to a
#: different starting point: a chain edited with a vocabulary that had drifted
#: from the one it was created with is a chain that means something different
#: after the edit, and the difference would show up as a step that runs at the
#: wrong time rather than as an error.


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
        self._shell_tools = ShellTools(
            self.registry,
            self._active_workspace_root,
            self._process_output_dir,
            self._resource_home,
            self._file_tools.path_is_inside_workspace,
            self._file_tools.workspace_file_snapshot,
            self._file_tools.move_new_workspace_files_to_output_dir,
            self._try_interactive_confirmation,
            self._ok,
            self._error,
        )
        self._audio_tools = AudioTools(
            self.registry,
            self._active_workspace_root,
            self._process_output_dir,
            self._file_tools.resolve_tool_path,
            self._shell_tools.terminate_process,
            self._ok,
            self._error,
        )
        self._context_tools = ContextTools(
            self.registry, context_manager, self._ok, self._error
        )
        self._plugin_tools = PluginTools(self.registry)
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

    def _register(self):
        r = self.registry

        builtin_file_tools.register_file_tools(r, current_time=self._current_time, shell_tools=self._shell_tools, file_tools=self._file_tools, audio_tools=self._audio_tools)

        builtin_memory_tools.register_memory_tools(r, memory_tools=self._memory_tools, context_tools=self._context_tools)

        builtin_schedule_tools.register_schedule_tools(r, schedule_tools=self._schedule_tools)

        builtin_signal_tools.register_signal_tools(r, signal_tools=self._signal_tools, step_output_tools=self._step_output_tools)

        builtin_web_tools.register_web_tools(r, web_tools=self._web_tools)

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
            self._context_tools.clear,
            source="builtin",
        )

        builtin_plugin_tools.register_plugin_tools(r, plugin_tools=self._plugin_tools)

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

    def _schedule_store(self) -> SchedulerStore:
        if self._cached_schedule_store is None:
            from agent.scheduler import open_store
            self._cached_schedule_store = open_store()
        return self._cached_schedule_store
