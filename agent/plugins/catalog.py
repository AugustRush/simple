from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Optional

from agent import shared

CONSOLE = shared.CONSOLE
PLUGINS_DIR = shared.PLUGINS_DIR
USER_PLUGINS_DIR = shared.USER_PLUGINS_DIR

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

        on_turn_end(event: TurnEvent) -> Optional[HookResult]
            Async-compatible.  Fired after every assistant turn.

        on_session_end(event: SessionEvent) -> None
            Async-compatible.  Fired when the interactive session ends.

        on_pre_tool(event: PreToolEvent) -> Optional[HookResult]
            Async-compatible.  Return HookResult(action="block") to prevent
            the tool from executing.

        on_post_tool(event: PostToolEvent) -> Optional[HookResult]
            Async-compatible.  Purely observational.

    Prompt contribution:
        compose_system_prompt(current_prompt: str) -> str
            Return a **suffix** to append to the system prompt, or ``""``
            to contribute nothing.  The *current_prompt* argument is provided
            for context only — do NOT return it back.

    Slash commands:
        register_slash_commands() -> dict[str, Callable]
            Return {name: async handler(raw_cmd, components)}.
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
    """Return value from plugin hook methods."""

    action: str = "noop"  # "noop" | "block" | "context" | "warning"
    message: str = ""  # human-readable message / block reason
    context: str = ""  # extra context to surface to the agent next turn


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


def _read_plugin_json(plugin_dir: Path) -> Optional[PluginMeta]:
    """Read plugin.json from a plugin directory. Returns None if absent."""
    pj = plugin_dir / "plugin.json"
    if not pj.exists():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        mcp = data.get("mcp_servers", [])
        if isinstance(mcp, str):
            # Path to .mcp.json file
            mcp_path = plugin_dir / mcp
            if mcp_path.exists():
                mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
                if isinstance(mcp, dict):
                    mcp = [mcp]
            else:
                mcp = []
        return PluginMeta(
            name=data.get("name", plugin_dir.name),
            version=data.get("version", ""),
            description=data.get("description", ""),
            skills=data.get("skills", ""),
            mcp_servers=mcp if isinstance(mcp, list) else [],
        )
    except Exception:
        return None


async def _maybe_await(value: Any) -> Any:
    """Await value if it is a coroutine, otherwise return it directly."""
    if asyncio.iscoroutine(value):
        return await value
    return value


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

    # ── Lifecycle event firing ─────────────────────────────────────────────────

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
        """Notify all plugins after each assistant turn; collect HookResults."""
        results: list[HookResult] = []
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "on_turn_end"):
                continue
            try:
                if self._turn_hook_timeout_seconds > 0:
                    r = await asyncio.wait_for(
                        _maybe_await(plugin.on_turn_end(event)),
                        timeout=self._turn_hook_timeout_seconds,
                    )
                else:
                    r = await _maybe_await(plugin.on_turn_end(event))
                if isinstance(r, HookResult):
                    results.append(r)
            except asyncio.TimeoutError:
                _pname = getattr(plugin, "name", "?")
                shared.CONSOLE.print(
                    f"[dim]Plugin '{_pname}' turn_end timed out after "
                    f"{self._turn_hook_timeout_seconds:.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin turn_end error: {exc}[/dim]")
        return results

    async def fire_session_end(self, event: SessionEvent) -> None:
        """Notify all plugins when the interactive session ends."""
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "on_session_end"):
                continue
            try:
                await _maybe_await(plugin.on_session_end(event))
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin session_end error: {exc}[/dim]")

    async def fire_pre_tool(self, event: PreToolEvent) -> HookResult:
        """Fire before a tool call; first blocking result short-circuits the chain."""
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "on_pre_tool"):
                continue
            try:
                r = await _maybe_await(plugin.on_pre_tool(event))
                if isinstance(r, HookResult) and r.action == "block":
                    return r
            except Exception as exc:
                _pname = getattr(plugin, "name", "?")
                shared.CONSOLE.print(f"[dim]Plugin '{_pname}' pre_tool error: {exc}[/dim]")
        return HookResult()

    async def fire_post_tool(self, event: PostToolEvent) -> HookResult:
        """Fire after a tool call completes; last non-noop context wins."""
        result = HookResult()
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "on_post_tool"):
                continue
            try:
                r = await _maybe_await(plugin.on_post_tool(event))
                if isinstance(r, HookResult) and r.context:
                    result = r
            except Exception as exc:
                _pname = getattr(plugin, "name", "?")
                shared.CONSOLE.print(f"[dim]Plugin '{_pname}' post_tool error: {exc}[/dim]")
        return result
