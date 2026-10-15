"""Running a plugin tree's hooks: the cross-ecosystem surface.

Why this module exists
---------------------
The loader (:mod:`agent.plugins.catalog`) had grown to own discovery, the two
registries, the reload transaction, the declarative-asset adapter *and* this -- ~300
lines of dispatch, tool matchers, hook environments and subprocess plumbing, none of
which knows what loading is.  It reads a plugin table and runs things, so it is a
collaborator rather than a responsibility of the loader.

What it runs
------------
Two shapes, because two ecosystems describe a plugin that way and neither can be a
kernel subscription (see :data:`agent.plugins.catalog.LOADER_HOOK_BOUNDARY`):

* a **method** on the plugin object (``on_pre_tool``, ``on_turn_end``, ...), called
  with this runtime's payload;
* a **command** in the plugin's manifest (``hooks_config``), run as a subprocess with
  the event as JSON on stdin and its JSON output translated back through the format's
  ``translate_hook_output`` strategy.

The tool bridge
---------------
``ToolPreExecute``/``ToolPostExecute`` are published on the composition's tool bus by
the executor, so the command-hook chain is a *listener* rather than a call: the loader
does not have to be handed to the executor, and a composition that publishes on its own
bus gets its hooks run.  The bridge is subscribed lazily -- only a catalog whose
plugins declare command hooks needs one -- because a catalog with no composition behind
it falls back to the process-wide bus, and subscribing there leaks a listener into every
other catalog in the process (see :meth:`HookDispatcher.ensure_event_bridge`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from agent import shared
from agent.core.event_domains import SessionFinished
from agent.core.events import ToolPostExecute, ToolPreExecute
from agent.core.output import _active_event_collector
from agent.plugins import formats as plugin_formats
from agent.plugins.hook_types import (
    HookResult,
    PostToolEvent,
    PreToolEvent,
    SessionEvent,
    TurnEvent,
)

if TYPE_CHECKING:  # the loader imports this module, so this import is types only
    from agent.plugins.catalog import PluginMeta

__all__ = ["HookDispatcher"]


def _safe_float(value: Any, default: float) -> float:
    """Parse *value* as float, returning *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _emit_plugin_event(name: str, plugin_name: str = "", **fields: Any) -> None:
    collector = _active_event_collector.get()
    if collector is not None:
        collector.emit(name, plugin_name=plugin_name, **fields)


def _plugin_data_dir(plugin_name: str) -> Path:
    """Claude Code-compatible persistent data directory for a plugin."""
    safe_name = re.sub(r"[^0-9A-Za-z_-]+", "-", plugin_name).strip("-") or "plugin"
    return shared.AGENT_HOME / "plugins" / "data" / safe_name


_OUR_TO_CC_TOOL_NAME = {
    # File tools
    "read_file": "Read",
    "write_file": "Write",
    "list_files": "Glob",
    # Shell
    "shell": "Bash",
    # Memory & context
    "memory_read": "Read",
    "memory_write": "Write",
    "set_identity": "Write",
    "memory_search": "Grep",
    # Web
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    # Plugin management
    "install_plugin": "Bash",
    "uninstall_plugin": "Bash",
    # Skills
    "activate_skill": "Skill",
    "create_skill": "Write",
    "update_skill": "Write",
    "delete_skill": "Bash",
    # Scheduling
    "schedule_create": "Task",
    "schedule_list": "Read",
    "schedule_delete": "Bash",
    # Agent
    "spawn_agent": "Task",
    "send_message": "Bash",
}

class HookDispatcher:
    """The hook surface of one plugin tree.

    Constructed by :class:`~agent.plugins.catalog.PluginCatalog`, which owns the tree
    and the executor's lifetime.  Everything it needs is passed in: the **live** plugin
    table (a reference, so a plugin loaded after construction is dispatched to), the
    bus, the bus resolver, the executor and the turn-hook timeout.  Nothing here reads
    a config, a directory or a service.
    """

    def __init__(
        self,
        *,
        plugins: Mapping[str, tuple[Any, Any]],
        event_bus: Any,
        bus_for: Callable[[str], Any],
        executor: Any,
        turn_hook_timeout_seconds: float,
    ) -> None:
        self._plugins = plugins
        self._event_bus = event_bus
        self._bus_for = bus_for
        self._hook_executor = executor
        self._turn_hook_timeout_seconds = float(turn_hook_timeout_seconds)
        #: The tool-bus subscription, empty until :meth:`ensure_event_bridge`.
        self._event_subscriptions: list[Callable[[], None]] = []

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Drop the tool-bus subscription and release the hook thread pool.

        Queued-but-unstarted hooks are dropped.  Hooks already running cannot be
        interrupted, so this does not wait for them -- a plugin that hangs would
        otherwise hold up shutdown indefinitely.
        """
        for unsubscribe in self._event_subscriptions:
            unsubscribe()
        self._event_subscriptions.clear()
        self._hook_executor.shutdown(wait=False, cancel_futures=True)

    def ensure_event_bridge(self) -> None:
        """Subscribe the tool bridge, once, for a catalog that has command hooks.

        The bridge exists to run ``hooks_config`` commands -- external commands an
        operator declares -- so a dispatcher whose plugins declare none has nothing for
        it to do.  Subscribing anyway is not merely idle: a catalog with no composition
        behind it falls back to the **process-wide** bus, so every such catalog ever
        constructed installed two listeners on one shared object.

        That is how it was found: a test's ``PreToolUse: deny`` command hook outlived the
        test, stayed a listener, and gated an unrelated tool call in a *different* test --
        and because ``emit_blocking`` stops at the first refusal, the leftover gate won
        and the failure named the wrong test's reason string.

        Which bus it subscribes to is the composition's, passed in as ``bus_for``.
        Getting that wrong is the same class of failure in the other direction: this
        docstring's own lesson, a listener nobody publishes to.

        Called when a plugin's ``hooks_config`` becomes non-empty, which is during
        loading and therefore before any executor publishes.
        """
        if self._event_subscriptions:
            return
        self._event_subscriptions = [
            self._event_bus.on(ToolPreExecute, self._on_tool_pre_execute),
            self._event_bus.on(ToolPostExecute, self._on_tool_post_execute),
        ]

    def _hook_timeout(self, meta: PluginMeta, hook_name: str) -> float:
        """Return the per-hook timeout override, or the global default."""
        return meta.hook_timeouts.get(hook_name, self._turn_hook_timeout_seconds)

    def _matches_tool(
        self, meta: PluginMeta, hook_name: str, tool_name: str
    ) -> bool:
        """Check whether *hook_name* on *meta* has a matcher for *tool_name*.

        No matcher → always matches (opt-in to filtering by declaring one).
        Honors both our snake_case names and Claude Code PascalCase names
        when a CC plugin's matcher targets the latter.
        """
        matcher = meta.hook_matchers.get(hook_name)
        if matcher is None:
            return True
        if matcher.search(tool_name):
            return True
        cc_name = _OUR_TO_CC_TOOL_NAME.get(tool_name)
        return bool(cc_name and matcher.search(cc_name))

    @staticmethod
    def _entry_matches_tool(entry: dict, tool_name: str) -> bool:
        """Per-entry matcher check (used for Claude Code hook entries that
        carry their own matcher string instead of a per-event compiled one)."""
        matcher_str = str(entry.get("matcher", "") or "").strip()
        if not matcher_str or matcher_str == "*":
            return True
        try:
            pattern = re.compile(matcher_str)
        except re.error:
            return matcher_str == tool_name or matcher_str == _OUR_TO_CC_TOOL_NAME.get(tool_name)
        if pattern.search(tool_name):
            return True
        cc_name = _OUR_TO_CC_TOOL_NAME.get(tool_name)
        return bool(cc_name and pattern.search(cc_name))

    def _hook_env(self, meta: PluginMeta, event_payload: dict) -> dict[str, str]:
        """Build Claude Code-compatible environment variables for hook exec."""
        env = dict(os.environ)
        if meta.path is not None:
            # Every format's spelling of "where this plugin lives" and "where it
            # may write", from the adapters: a Codex hook command says
            # ``"$PLUGIN_ROOT/hooks/x.sh"`` and a Claude Code one says
            # ``${CLAUDE_PLUGIN_ROOT}/...``, and both have to resolve.
            env.update(
                plugin_formats.hook_environment(
                    meta.path, _plugin_data_dir(meta.name), Path.cwd()
                )
            )
        tool_name = str(event_payload.get("tool_name", "") or "")
        if tool_name:
            env["TOOL_NAME"] = _OUR_TO_CC_TOOL_NAME.get(tool_name, tool_name)
        tool_kwargs = event_payload.get("tool_kwargs")
        if tool_kwargs is not None:
            try:
                env["TOOL_INPUT"] = json.dumps(tool_kwargs, ensure_ascii=False)
            except Exception:
                env["TOOL_INPUT"] = str(tool_kwargs)
        result = event_payload.get("result")
        if result is not None:
            env["TOOL_OUTPUT"] = str(result)
        text = event_payload.get("text")
        if text is not None and event_payload.get("event") == "on_prompt_submit":
            env["CLAUDE_USER_PROMPT"] = str(text)
        return env

    async def _run_command_hooks(
        self,
        event_name: str,
        event_payload: dict,
        meta: PluginMeta,
    ) -> list[HookResult]:
        """Execute command-type hooks declared in plugin.json for *event_name*."""
        results: list[HookResult] = []
        entries = meta.hooks_config.get(event_name, [])
        tool_name = str(event_payload.get("tool_name", "") or "")
        env = self._hook_env(meta, event_payload)
        for entry in entries:
            cmd = str(entry.get("command", "") or "").strip()
            if not cmd:
                continue
            # Per-entry matcher (Claude Code style).  Legacy entries with no
            # matcher field always pass and remain compatible with the older
            # per-event matcher cache on PluginMeta.
            if tool_name and "matcher" in entry and not self._entry_matches_tool(entry, tool_name):
                continue
            timeout = max(0.0, _safe_float(entry.get("timeout"), self._hook_timeout(meta, event_name)))
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
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
                        parsed_result = plugin_formats.strategy_for(
                            meta.path
                        ).translate_hook_output(raw, event_name)
                        if parsed_result is not None:
                            results.append(parsed_result)
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
        """Run the ``on_prompt_submit`` command hooks before the message is submitted.

        Returns the first blocking result, or the last non-noop one -- a gate, which is
        why it is the last of the hook dispatchers to lose its Python half: a mistake here
        changes whether a submission can be refused rather than whether a plugin is
        notified.
        """
        meta_dict = metadata or {}
        result = HookResult()
        for _plugin, meta in self._plugins.values():
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
        return result

    async def fire_turn_end(self, event: TurnEvent) -> list[HookResult]:
        """Notify all plugins after each assistant turn; collect HookResults.

        A result with ``action="continue"`` signals the caller to run another
        turn with ``message`` as the next user prompt.
        """
        results: list[HookResult] = []
        for _plugin, meta in self._plugins.values():
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
                if r.action != "noop":
                    _emit_plugin_event(
                        "hook_fired",
                        plugin_name=meta.name,
                        hook_name="on_turn_end",
                        source="command",
                        action=r.action,
                        message=r.message[:200] if r.message else "",
                    )
                if r.context:
                    _emit_plugin_event(
                        "hook_context_injected",
                        plugin_name=meta.name,
                        hook_name="on_turn_end",
                        source="command",
                    )
            results.extend(cmd_results)
        return results

    async def fire_session_end(self, event: SessionEvent) -> None:
        """Announce that the interactive session ended.

        This is the only place the typed event is published, and that matters: emitting
        at the two call sites of this method (``cli`` and the channel layer) would be the
        same event constructed twice, and the day one of them stopped emitting would be a
        plugin that silently stops scoring sessions.

        It used to notify two protocols from here -- the typed event, plus a Python
        ``on_session_end`` hook.  No plugin implements that hook any more; the event is
        what a plugin subscribes to.

        Published on the composition's session bus, which is the one ``SESSION.attach``
        resolves.  The process-wide default would be a third instance of the same miss:
        the event constructed, emitted, and heard by nobody the composition knows about.
        """
        # Emitted unconditionally: the bus does nothing when nobody listens, and a
        # session that ended is a fact whether or not this composition has plugins.
        await self._bus_for("session").emit(
            SessionFinished(
                agent_id="",
                messages=list(event.messages),
                tools_used=list(event.tools_used),
                session_id=str(getattr(event, "session_id", "") or ""),
                turn_count=int(getattr(event, "turn_count", 0) or 0),
            )
        )
    async def _on_tool_pre_execute(self, event: ToolPreExecute) -> Optional[str]:
        """Bus listener for :class:`ToolPreExecute`.

        Runs the ``plugin.json`` command hooks for a tool call and returns their
        refusal reason, or None.  It used to adapt the typed event to a Python hook
        chain as well -- ``on_pre_tool`` -- which no plugin implements now, so only
        the command half remains.  ``hooks_config`` is a separate mechanism: external
        commands the operator declares, not Python methods a plugin defines.
        """
        result = await self.fire_pre_tool(
            PreToolEvent(tool_name=event.tool_name, tool_kwargs=event.arguments)
        )
        if result.action == "block":
            return result.message or "blocked"
        return None

    async def _on_tool_post_execute(self, event: ToolPostExecute) -> None:
        """Bus listener for :class:`ToolPostExecute`; purely observational."""
        await self.fire_post_tool(
            PostToolEvent(
                tool_name=event.tool_name,
                tool_kwargs=event.arguments,
                result=event.result,
            )
        )

    async def fire_pre_tool(self, event: PreToolEvent) -> HookResult:
        """Fire before a tool call; first blocking result short-circuits the chain.

        Matchers declared in plugin.json scoped to ``on_pre_tool`` are honoured:
        a plugin without a matcher is called for every tool; a plugin with a
        matcher is only called when the tool name matches.
        """
        for _plugin, meta in self._plugins.values():
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
        return HookResult()

    async def fire_post_tool(self, event: PostToolEvent) -> HookResult:
        """Fire after a tool call completes; last non-noop context wins.

        Matchers declared in plugin.json scoped to ``on_post_tool`` are honoured.
        """
        result = HookResult()
        for _plugin, meta in self._plugins.values():
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
        return result
