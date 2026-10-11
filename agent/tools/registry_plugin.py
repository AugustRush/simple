"""The tool registry as a plugin: it provides ``registry``.

Why a plugin
------------
Every plugin that contributes a tool registers on the registry, and the loader
resolves it too, so it looked like the composition's floor.  It is not one: the
registry needs a console and the *paths* its context carries, and those are
values the composition computes from the config.  Providing it from a plugin is
what makes it replaceable -- a registry that audits every tool call, or one that
persists its registrations, is now a plugin that provides this name instead of a
patch to the composition root.

What moved with it, and what did not
------------------------------------
The **static** context moved: the five resource paths and the three config-derived
secrets the tool implementations read at call time.  The **runtime** context did
not: sandbox posture, permission levels, the output directory and the file-access
policy stay with the composition, which is the only place that knows the rules a
particular run is under.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["RegistryPlugin"]


class RegistryPlugin:
    """Provides ``registry``."""

    name = "registry"

    #: The paths are computed by the composition (they depend on the CLI's
    #: ``--name``/``--resource`` choices), so they arrive as services rather than
    #: being re-derived here from ``shared``.
    inject = (
        "cfg",
        "resource_root",
        "workspace_root",
        "user_skills_dir",
        "user_tools_dir",
        "user_plugins_dir",
    )

    def setup(self, ctx: Any) -> None:
        import agent as agent_module

        from agent import shared

        # Read through the package attribute, at setup time, so substituting
        # ``agent.ToolRegistry`` reaches this construction.
        registry = agent_module.ToolRegistry(console=shared.CONSOLE)

        # Resource definitions (skills, tools, plugins, prompts) are global for web
        # sessions, while state directories below the session home stay isolated.
        # Explicit here because tools resolve these paths at call time.
        registry.set_context("resource_home", str(ctx.get("resource_root")))
        registry.set_context("workspace_root", str(ctx.get("workspace_root")))
        registry.set_context("user_skills_dir", str(ctx.get("user_skills_dir")))
        registry.set_context("user_tools_dir", str(ctx.get("user_tools_dir")))
        registry.set_context("user_plugins_dir", str(ctx.get("user_plugins_dir")))

        cfg = ctx.get("cfg", None) or {}
        audio_cfg = cfg.get("audio")
        if isinstance(audio_cfg, dict):
            transcription = str(audio_cfg.get("transcription_command", "") or "").strip()
            if transcription:
                registry.set_context("audio_transcription_command", transcription)

        tavily_api_key = cfg.get("tavily_api_key", "")
        if isinstance(tavily_api_key, str) and tavily_api_key.startswith("$"):
            tavily_api_key = os.environ.get(tavily_api_key[1:], "")
        if not tavily_api_key:
            tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
        if tavily_api_key:
            registry.set_context("tavily_api_key", tavily_api_key)

        # web_fetch's egress proxy.  Unset means "read the standard environment
        # variables"; a URL pins one; "none" forces direct connections.  This
        # matters behind a fake-IP resolver (Clash's enhanced-mode: fake-ip), where
        # every name answers with a reserved address and the direct path refuses.
        web_proxy = cfg.get("web_proxy", "")
        if isinstance(web_proxy, str) and web_proxy.strip():
            registry.set_context("web_proxy", web_proxy.strip())

        ctx.provide("registry", registry)
