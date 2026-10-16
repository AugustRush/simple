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
            self._context_tools.clear,
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
            self._plugin_tools.install_plugin,
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
            self._plugin_tools.uninstall_plugin,
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
            self._plugin_tools.list_installed_plugins,
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
            self._plugin_tools.create_tool,
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
            self._plugin_tools.update_tool,
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
            self._plugin_tools.delete_tool,
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
            self._plugin_tools.list_user_tools,
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
            self._plugin_tools.install_tool_dependency,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )

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
