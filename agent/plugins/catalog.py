from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
import importlib.util
import inspect
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Optional

from agent import shared
from agent.core.output import _active_event_collector

CONSOLE = shared.CONSOLE
PLUGINS_DIR = shared.PLUGINS_DIR
USER_PLUGINS_DIR = shared.USER_PLUGINS_DIR


def _emit_plugin_event(name: str, plugin_name: str = "", **fields: Any) -> None:
    collector = _active_event_collector.get()
    if collector is not None:
        collector.emit(name, plugin_name=plugin_name, **fields)

class AgentPlugin:
    """Protocol that plugin objects may implement (duck-typed).

    All methods are optional — implement only the hooks you need.
    ``PluginCatalog`` inspects each plugin with ``hasattr`` at dispatch time.

    Attributes:
        name:    Unique identifier for the plugin (used in log messages).
        version: Semver string (informational only).

    Lifecycle hooks:
        on_session_start(components: dict) -> None
            Synchronous.  Called once after all core components are built.
            Use it to capture references to client, model, memory, etc.

        on_prompt_submit(text: str, metadata: dict) -> Optional[HookResult]
            Async-compatible.  Fired when the user submits a message, BEFORE the
            agent sees it.  Return ``HookResult(action="block")`` to prevent the
            message from being processed, or inject ``context`` to prepend
            guidance to the prompt.

        on_turn_end(event: TurnEvent) -> Optional[HookResult]
            Async-compatible.  Fired after every assistant turn.
            Return ``HookResult(action="continue")`` to automatically run another
            turn with ``message`` as the next user prompt (agent self-feedback loop).

        on_session_end(event: SessionEvent) -> None
            Async-compatible.  Fired when the interactive session ends.

        on_pre_tool(event: PreToolEvent) -> Optional[HookResult]
            Async-compatible.  Return HookResult(action="block") to prevent
            the tool from executing.  Matchers in plugin.json can scope this
            to specific tool names.

        on_post_tool(event: PostToolEvent) -> Optional[HookResult]
            Async-compatible.  Purely observational.  Matchers in plugin.json
            can scope this to specific tool names.

    Prompt contribution:
        compose_system_prompt(current_prompt: str) -> str
            Return a **suffix** to append to the system prompt, or ``""``
            to contribute nothing.  The *current_prompt* argument is provided
            for context only — do NOT return it back.

    Slash commands:
        register_slash_commands() -> dict[str, Callable]
            Return {name: async handler(raw_cmd, components)}.

    Command hooks (plugin.json):
        Hooks may also be declared in ``plugin.json`` under a ``hooks`` key as
        external commands.  These run as subprocesses with event data on stdin
        and a JSON HookResult shape on stdout.  Each entry supports:
        ``matcher`` (regex), ``timeout`` (seconds), and ``command`` (shell string).
    """

    name: str = ""
    version: str = ""


@dataclass
class TurnEvent:
    """Emitted after each assistant turn completes."""

    user_input: str
    agent_response: str
    tool_calls: list[str]
    session_id: str = ""
    timestamp: str = ""
    turn_index: int = 0


@dataclass
class SessionEvent:
    """Emitted when the interactive session ends."""

    messages: list[dict]
    tools_used: list[str]
    session_id: str = ""
    timestamp: str = ""
    turn_count: int = 0


@dataclass
class PreToolEvent:
    """Emitted before a tool call executes."""

    tool_name: str
    tool_kwargs: dict


@dataclass
class PostToolEvent:
    """Emitted after a tool call completes."""

    tool_name: str
    tool_kwargs: dict
    result: str


@dataclass
class HookResult:
    """Return value from plugin hook methods.

    Actions:
        ``"noop"``     — no effect (default).
        ``"block"``    — prevent the operation (tool execution, prompt delivery).
        ``"continue"`` — (turn_end only) run another turn with ``message`` as prompt.
        ``"context"``  — inject ``context`` into the next turn's system instructions.
        ``"warning"``  — surface ``message`` as a user-visible warning.
    """

    action: str = "noop"
    message: str = ""
    context: str = ""


# Valid characters for plugin directory names (P0-3 safety).
_SAFE_PLUGIN_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class PluginMeta:
    """Structured metadata read from plugin.json (if present)."""

    name: str
    version: str = ""
    description: str = ""
    skills: str = ""  # relative path to skills dir
    mcp_servers: list[dict] = field(default_factory=list)
    source: str = ""  # "builtin" or "user"
    enabled: bool = True
    hooks_config: dict[str, list[dict]] = field(default_factory=dict)
    hook_matchers: dict[str, re.Pattern] = field(default_factory=dict)
    hook_timeouts: dict[str, float] = field(default_factory=dict)


def _read_plugin_json(plugin_dir: Path) -> Optional[PluginMeta]:
    """Read plugin.json from a plugin directory. Returns None if absent."""
    pj = plugin_dir / "plugin.json"
    if not pj.exists():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        mcp = data.get("mcp_servers", [])
        if isinstance(mcp, str):
            mcp_path = plugin_dir / mcp
            if mcp_path.exists():
                mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
                if isinstance(mcp, dict):
                    mcp = [mcp]
            else:
                mcp = []
        raw_hooks: dict[str, list[dict]] = {}
        hook_matchers: dict[str, re.Pattern] = {}
        hook_timeouts: dict[str, float] = {}
        hooks_cfg = data.get("hooks", {})
        if isinstance(hooks_cfg, dict):
            for hook_name, entries in hooks_cfg.items():
                if not isinstance(entries, list):
                    continue
                raw_hooks[hook_name] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    raw_hooks[hook_name].append(entry)
                    if isinstance(entry.get("timeout"), (int, float)):
                        hook_timeouts[hook_name] = max(0.0, float(entry["timeout"]))
                    matcher = str(entry.get("matcher", "") or "").strip()
                    if matcher and matcher != "*":
                        try:
                            hook_matchers[hook_name] = re.compile(matcher)
                        except re.error:
                            pass
        return PluginMeta(
            name=data.get("name", plugin_dir.name),
            version=data.get("version", ""),
            description=data.get("description", ""),
            skills=data.get("skills", ""),
            mcp_servers=mcp if isinstance(mcp, list) else [],
            hooks_config=raw_hooks,
            hook_matchers=hook_matchers,
            hook_timeouts=hook_timeouts,
        )
    except Exception:
        return None


async def _maybe_await(value: Any) -> Any:
    """Await value if it is a coroutine, otherwise return it directly."""
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _maybe_await_with_timeout(value: Any, timeout_seconds: float) -> Any:
    if timeout_seconds > 0:
        return await asyncio.wait_for(_maybe_await(value), timeout=timeout_seconds)
    return await _maybe_await(value)


async def _call_hook_with_timeout(
    hook: Callable,
    *args: Any,
    timeout_seconds: float,
) -> Any:
    """Call a plugin hook with timeout covering sync and async hooks."""
    if timeout_seconds <= 0:
        return await _maybe_await(hook(*args))
    if inspect.iscoroutinefunction(hook):
        return await asyncio.wait_for(
            _maybe_await(hook(*args)),
            timeout=timeout_seconds,
        )

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-plugin-hook")
    future = loop.run_in_executor(executor, partial(hook, *args))
    try:
        result = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return await _maybe_await_with_timeout(result, timeout_seconds)


class PluginCatalog:
    """Discovers, loads, and orchestrates agent plugins from disk.

    Built-in plugins are loaded from ``shared.PLUGINS_DIR`` (package builtin plugins).
    User plugins are loaded from ``shared.USER_PLUGINS_DIR`` (~/.agent/plugins/).

    Each plugin directory must contain ``__init__.py`` with a top-level
    ``register() -> plugin`` function that returns an object implementing
    any subset of the AgentPlugin protocol (duck-typed, no base class needed).

    An optional ``plugin.json`` in the directory provides structured metadata
    (name, version, description, skills path, mcp_servers).

    User plugins with the same name as a built-in plugin override the built-in.
    Plugins can be disabled in config.json via ``plugins.<name>.enabled = false``.
    """

    def __init__(
        self,
        builtin_dir: Path,
        user_dir: Optional[Path] = None,
        plugin_config: Optional[dict] = None,
        turn_hook_timeout_seconds: float = shared.DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._plugin_config = plugin_config or {}
        self._turn_hook_timeout_seconds = max(0.0, float(turn_hook_timeout_seconds))
        # name → (plugin_object, PluginMeta)
        self._plugins: dict[str, tuple[Any, PluginMeta]] = {}
        self._slash_commands: dict[str, Callable] = {}
        # Skills bundled by plugins: list of (plugin_name, skills_root_path)
        self._bundled_skills: list[tuple[str, Path]] = []
        # MCP configs bundled by plugins: list of (plugin_name, server_config_dict)
        self._bundled_mcp: list[tuple[str, dict]] = []

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _is_plugin_enabled(self, name: str) -> bool:
        """Check config.json plugins section for enabled status."""
        pcfg = self._plugin_config.get(name, {})
        if isinstance(pcfg, dict):
            return pcfg.get("enabled", True)
        return True

    def discover_and_load(self) -> list[str]:
        """Scan plugin directories and load all valid plugins.

        Failures in individual plugins are reported but do not abort startup.
        Returns a list of successfully loaded plugin names.
        """
        self._plugins.clear()
        self._slash_commands.clear()
        self._bundled_skills.clear()
        self._bundled_mcp.clear()
        _slash_command_owners: dict[str, str] = {}  # cmd_key → owning plugin name

        # Auto-create user plugins directory
        if self._user_dir:
            self._user_dir.mkdir(parents=True, exist_ok=True)

        # Load builtin first, then user (user overrides builtin)
        search_dirs: list[tuple[Path, str]] = [(self._builtin_dir, "builtin")]
        if self._user_dir:
            search_dirs.append((self._user_dir, "user"))

        for search_dir, source in search_dirs:
            if not search_dir or not search_dir.is_dir():
                continue
            for plugin_dir in sorted(search_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                # P0-3: reject directory names that could collide with real modules.
                if not _SAFE_PLUGIN_NAME.match(plugin_dir.name):
                    shared.CONSOLE.print(
                        f"[yellow]Plugin '{plugin_dir.name}': unsafe name — skipped[/yellow]"
                    )
                    continue
                init_file = plugin_dir / "__init__.py"
                if not init_file.exists():
                    continue

                # Read plugin.json metadata (optional)
                meta = _read_plugin_json(plugin_dir)
                plugin_name = meta.name if meta else plugin_dir.name

                # Check enable/disable in config
                if not self._is_plugin_enabled(plugin_name):
                    continue

                mod_name = f"_agent_plugin_{plugin_dir.name}"
                try:
                    spec = importlib.util.spec_from_file_location(
                        mod_name,
                        init_file,
                        submodule_search_locations=[str(plugin_dir)],
                    )
                    if spec is None or spec.loader is None:
                        continue
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[mod_name] = mod
                    spec.loader.exec_module(mod)  # type: ignore[union-attr]
                    if not hasattr(mod, "register"):
                        shared.CONSOLE.print(
                            f"[yellow]Plugin '{plugin_dir.name}': no register() — skipped[/yellow]"
                        )
                        continue
                    plugin = mod.register()

                    # Build meta from plugin attributes if no plugin.json
                    if meta is None:
                        meta = PluginMeta(
                            name=getattr(plugin, "name", plugin_dir.name),
                            version=getattr(plugin, "version", ""),
                            description=getattr(plugin, "description", ""),
                        )
                    meta.source = source

                    # Slash commands with conflict detection
                    if hasattr(plugin, "register_slash_commands"):
                        for cmd_key, handler in (
                            plugin.register_slash_commands() or {}
                        ).items():
                            if cmd_key in self._slash_commands:
                                existing_owner = _slash_command_owners.get(cmd_key, "?")
                                shared.CONSOLE.print(
                                    f"[yellow]Plugin '{plugin_name}': slash command "
                                    f"'/{cmd_key}' conflicts with plugin "
                                    f"'{existing_owner}' — overriding[/yellow]"
                                )
                            self._slash_commands[cmd_key] = handler
                            _slash_command_owners[cmd_key] = plugin_name

                    # Store (user overrides builtin with same name)
                    self._plugins[plugin_name] = (plugin, meta)

                    # Collect bundled skills
                    if meta.skills:
                        skills_path = (plugin_dir / meta.skills).resolve()
                        if skills_path.is_dir():
                            self._bundled_skills.append((plugin_name, skills_path))

                    # Collect bundled MCP configs
                    for mcp_cfg in meta.mcp_servers:
                        if isinstance(mcp_cfg, dict) and mcp_cfg.get("name"):
                            self._bundled_mcp.append((plugin_name, mcp_cfg))

                except Exception as exc:
                    shared.CONSOLE.print(
                        f"[yellow]Plugin '{plugin_dir.name}' failed to load: {exc}[/yellow]"
                    )
        return [name for name in self._plugins]

    def get_bundled_skills(self) -> list[tuple[str, Path]]:
        """Return list of (plugin_name, skills_root_path) for bundled skills."""
        return list(self._bundled_skills)

    def get_bundled_mcp(self) -> list[tuple[str, dict]]:
        """Return list of (plugin_name, mcp_server_config) for bundled MCP servers."""
        return list(self._bundled_mcp)

    def list_plugins(self) -> list[PluginMeta]:
        """Return metadata for all loaded plugins."""
        return [meta for _, meta in self._plugins.values()]

    # ── Prompt composition ─────────────────────────────────────────────────────

    def compose_all_prompts(self, base: str) -> str:
        """Let each loaded plugin append a suffix to the composed system prompt."""
        result = base
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "compose_system_prompt"):
                continue
            try:
                suffix = plugin.compose_system_prompt(result)
                if suffix:
                    result = result.rstrip() + "\n\n" + suffix.strip()
            except Exception as exc:
                _pname = getattr(plugin, "name", "?")
                shared.CONSOLE.print(
                    f"[dim]Plugin '{_pname}' compose_system_prompt error: {exc}[/dim]"
                )
        return result

    # ── Slash commands ─────────────────────────────────────────────────────────

    def get_slash_commands(self) -> dict[str, Callable]:
        """Return mapping of command name → async handler(raw_cmd, components)."""
        return dict(self._slash_commands)

    # ── Hook helpers ────────────────────────────────────────────────────────────

    def _hook_timeout(self, meta: PluginMeta, hook_name: str) -> float:
        """Return the per-hook timeout override, or the global default."""
        return meta.hook_timeouts.get(hook_name, self._turn_hook_timeout_seconds)

    def _matches_tool(
        self, meta: PluginMeta, hook_name: str, tool_name: str
    ) -> bool:
        """Check whether *hook_name* on *meta* has a matcher for *tool_name*.

        No matcher → always matches (opt-in to filtering by declaring one).
        """
        matcher = meta.hook_matchers.get(hook_name)
        if matcher is None:
            return True
        return bool(matcher.search(tool_name))

    async def _run_command_hooks(
        self,
        event_name: str,
        event_payload: dict,
        meta: PluginMeta,
    ) -> list[HookResult]:
        """Execute command-type hooks declared in plugin.json for *event_name*."""
        results: list[HookResult] = []
        entries = meta.hooks_config.get(event_name, [])
        for entry in entries:
            if entry.get("type") != "command":
                continue
            cmd = str(entry.get("command", "") or "").strip()
            if not cmd:
                continue
            timeout = max(0.0, float(entry.get("timeout", self._hook_timeout(meta, event_name))))
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdin_data = json.dumps(event_payload, ensure_ascii=False).encode("utf-8")
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(stdin_data), timeout=max(1.0, timeout)
                )
                if proc.returncode == 2:
                    reason = stderr_bytes.decode(errors="replace").strip() or "blocked"
                    results.append(HookResult(action="block", message=reason))
                    continue
                try:
                    raw = json.loads(stdout_bytes.decode("utf-8"))
                    if isinstance(raw, dict):
                        results.append(HookResult(
                            action=str(raw.get("action", "noop")),
                            message=str(raw.get("message", "")),
                            context=str(raw.get("context", "")),
                        ))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' command hook '{event_name}' "
                    f"timed out after {timeout:.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' command hook '{event_name}' "
                    f"error: {exc}[/dim]"
                )
        return results

    # ── Lifecycle event firing ─────────────────────────────────────────────────

    async def fire_prompt_submit(
        self, text: str, metadata: Optional[dict] = None
    ) -> HookResult:
        """Fire before the user message reaches the agent.

        Returns the first blocking result, or the last non-noop result.
        """
        meta_dict = metadata or {}
        result = HookResult()
        for plugin, meta in self._plugins.values():
            # Command hooks for this event
            cmd_results = await self._run_command_hooks(
                "on_prompt_submit",
                {"event": "on_prompt_submit", "text": text, "metadata": meta_dict},
                meta,
            )
            for r in cmd_results:
                if r.action == "block":
                    return r
                if r.action != "noop":
                    result = r

            # Python in-process hooks
            if not hasattr(plugin, "on_prompt_submit"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_prompt_submit,
                    text,
                    meta_dict,
                    timeout_seconds=self._hook_timeout(meta, "on_prompt_submit"),
                )
                if isinstance(r, HookResult):
                    if r.action == "block":
                        _emit_plugin_event(
                            "hook_blocked",
                            plugin_name=meta.name,
                            hook_name="on_prompt_submit",
                            reason=r.message,
                        )
                        return r
                    if r.action == "context":
                        _emit_plugin_event(
                            "hook_context_injected",
                            plugin_name=meta.name,
                            hook_name="on_prompt_submit",
                        )
                    if r.action != "noop":
                        result = r
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' prompt_submit timed out[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin prompt_submit error ({meta.name}): {exc}[/dim]"
                )
        return result

    def fire_session_start(self, components: dict) -> None:
        """Synchronous session-start notification; called before the input loop."""
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "on_session_start"):
                continue
            try:
                plugin.on_session_start(components)
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin session_start error: {exc}[/dim]")

    async def fire_turn_end(self, event: TurnEvent) -> list[HookResult]:
        """Notify all plugins after each assistant turn; collect HookResults.

        A result with ``action="continue"`` signals the caller to run another
        turn with ``message`` as the next user prompt.
        """
        results: list[HookResult] = []
        for plugin, meta in self._plugins.values():
            cmd_results = await self._run_command_hooks(
                "on_turn_end",
                {
                    "event": "on_turn_end",
                    "user_input": event.user_input,
                    "agent_response": event.agent_response,
                    "tool_calls": event.tool_calls,
                    "session_id": event.session_id,
                    "timestamp": event.timestamp,
                    "turn_index": event.turn_index,
                },
                meta,
            )
            for r in cmd_results:
                if r.action == "continue":
                    _emit_plugin_event(
                        "hook_continued",
                        plugin_name=meta.name,
                        hook_name="on_turn_end",
                        next_prompt=r.message,
                    )
            results.extend(cmd_results)

            if not hasattr(plugin, "on_turn_end"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_turn_end,
                    event,
                    timeout_seconds=self._hook_timeout(meta, "on_turn_end"),
                )
                if isinstance(r, HookResult):
                    if r.action == "continue":
                        _emit_plugin_event(
                            "hook_continued",
                            plugin_name=meta.name,
                            hook_name="on_turn_end",
                            next_prompt=r.message,
                        )
                    results.append(r)
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' turn_end timed out after "
                    f"{self._hook_timeout(meta, 'on_turn_end'):.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin turn_end error ({meta.name}): {exc}[/dim]")
        return results

    async def fire_session_end(self, event: SessionEvent) -> None:
        """Notify all plugins when the interactive session ends."""
        for plugin, meta in self._plugins.values():
            timeout = self._hook_timeout(meta, "on_session_end")
            if not hasattr(plugin, "on_session_end"):
                continue
            try:
                await _call_hook_with_timeout(
                    plugin.on_session_end,
                    event,
                    timeout_seconds=timeout,
                )
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' session_end timed out after "
                    f"{timeout:.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin session_end error ({meta.name}): {exc}[/dim]"
                )

    async def fire_pre_tool(self, event: PreToolEvent) -> HookResult:
        """Fire before a tool call; first blocking result short-circuits the chain.

        Matchers declared in plugin.json scoped to ``on_pre_tool`` are honoured:
        a plugin without a matcher is called for every tool; a plugin with a
        matcher is only called when the tool name matches.
        """
        for plugin, meta in self._plugins.values():
            if not self._matches_tool(meta, "on_pre_tool", event.tool_name):
                continue

            cmd_results = await self._run_command_hooks(
                "on_pre_tool",
                {
                    "event": "on_pre_tool",
                    "tool_name": event.tool_name,
                    "tool_kwargs": event.tool_kwargs,
                },
                meta,
            )
            for r in cmd_results:
                if r.action == "block":
                    return r

            if not hasattr(plugin, "on_pre_tool"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_pre_tool,
                    event,
                    timeout_seconds=self._hook_timeout(meta, "on_pre_tool"),
                )
                if isinstance(r, HookResult) and r.action == "block":
                    _emit_plugin_event(
                        "hook_blocked",
                        plugin_name=meta.name,
                        hook_name="on_pre_tool",
                        tool_name=event.tool_name,
                        reason=r.message,
                    )
                    return r
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' pre_tool timed out after "
                    f"{self._hook_timeout(meta, 'on_pre_tool'):.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin pre_tool error ({meta.name}): {exc}[/dim]"
                )
        return HookResult()

    async def fire_post_tool(self, event: PostToolEvent) -> HookResult:
        """Fire after a tool call completes; last non-noop context wins.

        Matchers declared in plugin.json scoped to ``on_post_tool`` are honoured.
        """
        result = HookResult()
        for plugin, meta in self._plugins.values():
            if not self._matches_tool(meta, "on_post_tool", event.tool_name):
                continue

            cmd_results = await self._run_command_hooks(
                "on_post_tool",
                {
                    "event": "on_post_tool",
                    "tool_name": event.tool_name,
                    "tool_kwargs": event.tool_kwargs,
                    "result": event.result,
                },
                meta,
            )
            for r in cmd_results:
                if r.context:
                    result = r

            if not hasattr(plugin, "on_post_tool"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_post_tool,
                    event,
                    timeout_seconds=self._hook_timeout(meta, "on_post_tool"),
                )
                if isinstance(r, HookResult) and r.context:
                    result = r
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' post_tool timed out after "
                    f"{self._hook_timeout(meta, 'on_post_tool'):.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin post_tool error ({meta.name}): {exc}[/dim]"
                )
        return result
