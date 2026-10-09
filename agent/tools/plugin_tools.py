"""The plugin and user-tool tools: installing code, and taking it away.

Why this module exists
----------------------
Thirteen methods -- 442 lines -- of ``BuiltinTools`` are about one thing: code
that arrives after the deployment was built.  ``install_plugin`` /
``uninstall_plugin`` / ``list_installed_plugins`` / ``reload_plugins`` move a
plugin in, out, and into the running container; ``create_tool`` / ``update_tool``
/ ``delete_tool`` / ``list_tools`` do the same for a single user-authored tool;
and the rest is the machinery that makes those safe.

Unlike every other cluster, this one needs **nothing from the host but the
registry** -- one injected value, no callbacks.  Its six registry context keys
(``user_plugins_dir``, ``plugin_catalog``, ``command_router``, ``skill_catalog``,
``mcp_client`` and the registry itself) are reached through it, and that is the
whole dependency.

The helpers moved with it because this is their only reader, and each carries a
decision:

- **``canonicalize_plugin_source`` rewrites ``git://`` to https.**  Port 9418 is
  a legacy transport that home and corporate networks routinely block and that
  carries no credentials; https, ssh and local paths are left alone because they
  are legitimate authenticated transports.
- **``plugin_executable_content`` names what will *run*.**  A plugin with a
  Python entry point, an MCP server or hooks is equivalent to executing
  arbitrary code, and the install path must be able to say which of those it is
  before a person approves it.  A declarative-only plugin returns "".
- **``resolve_user_plugin_target`` refuses a name that is not a canonical slug**
  and refuses a target that is not a direct child of the plugins directory --
  the path-traversal boundary for a name that arrives from a model.
- **``plugin_install_lock`` serialises installs process-wide**, because an
  install is a multi-step transaction (clone, validate, swap, reload) that two
  concurrent calls would interleave.
- **``_PluginInstallError`` is control flow, not failure**: it carries the
  structured payload from the middle of the transaction out to the caller, so a
  failed upgrade can roll back and report the same shape.

``reload`` passes exactly the four keys the container rebuild needs; passing
more would quietly restore the dependency the named-keys boundary exists to
remove, which is what ``tests/test_registry_context_boundary.py`` asserts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import signal
import uuid
from pathlib import Path
from typing import Any, Optional

from agent import shared
from agent.core.output import _active_sink

from .executor import report_tool_progress


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


class PluginTools:
    """The plugin and user-tool tools, with the registry they read."""

    def __init__(self, registry: Any) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  Hiding it would install a plugin into the
        # composition's directory while the session watched.
        self.registry = registry

    async def create_tool(
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

    async def update_tool(
        self, tool_id: str, code: str, intent: str = ""
    ) -> dict:
        from agent.tools import user_tools

        normalized = user_tools.normalize_tool_id(tool_id)
        if not normalized:
            return {"ok": False, "error": f"Invalid tool id '{tool_id}'"}
        return await user_tools.author_tool(
            normalized, code, registry=self.registry, replace=True
        )

    async def delete_tool(self, tool_id: str, intent: str = "") -> dict:
        from agent.tools import user_tools

        normalized = user_tools.normalize_tool_id(tool_id)
        if not normalized:
            return {"ok": False, "error": f"Invalid tool id '{tool_id}'"}
        return user_tools.remove_tool(normalized, registry=self.registry)

    def list_user_tools(self) -> dict:
        from agent.tools import user_tools

        return user_tools.describe_tools(self.registry)

    async def install_tool_dependency(
        self, package: str, intent: str = ""
    ) -> dict:
        from agent.tools import user_tools

        return await user_tools.install_dependency(package)

    # ── Plugin install / uninstall implementations ────────────────────────

    async def install_plugin(
        self,
        source: str,
        intent: str = "",
        name: Optional[str] = None,
        replace: bool = False,
    ) -> dict:
        """Install a plugin; serialized so concurrent installs cannot race."""
        async with _plugin_install_lock():
            return await self.install_plugin_locked(
                source,
                intent=intent,
                name=name,
                replace=replace,
            )

    async def install_plugin_locked(
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
            fetch_result = await self.fetch_plugin_source(source, target)
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
                scope = self.authorization_scope()
                approved, needs_pending = await self.confirm_plugin_install(
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
            reload_result = await self.reload_plugins()
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

    async def fetch_plugin_source(self, source: str, target: Path) -> dict:
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
    def authorization_scope() -> Any:
        from agent.core.agent import _active_agent_context
        from agent.security.shell import ShellAuthorizationScope

        active_context = _active_agent_context.get()
        metadata = active_context.metadata if active_context is not None else {}
        return ShellAuthorizationScope(
            str(metadata.get("session_id") or "default"),
            str(metadata.get("channel_name") or "cli"),
            str(metadata.get("user_id") or ""),
        )

    async def confirm_plugin_install(
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

    async def uninstall_plugin(self, name: str, intent: str = "") -> dict:
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

        reload_result = await self.reload_plugins()
        return {
            "ok": True,
            "removed_from": str(target),
            "reload": reload_result,
            "summary_text": (
                f"Uninstalled plugin '{name}'. "
                f"Removed: {', '.join(reload_result.get('removed_plugins', [])) or 'none'}."
            ),
        }

    def list_installed_plugins(self) -> dict:
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

    async def reload_plugins(self) -> dict:
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
