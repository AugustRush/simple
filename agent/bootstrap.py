from __future__ import annotations

import asyncio
from pathlib import Path
import os
from typing import Optional

import agent as agent_module
from agent.config import ModelClientFactory, _compose_system_prompt, _load_system_prompt, _resolve_output_dir
from agent.memory.system import BackgroundMemoryWorker, ConsolidationEngine, ContextManager, LTMStore, LocalRetriever, MemoryPalace, normalize_memory_chapter
from agent.plugins.catalog import PluginCatalog
from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import BuiltinTools, MCPClient, ToolRegistry, UserToolCatalog

DEFAULT_MAX_PARALLEL_AGENTS = agent_module.DEFAULT_MAX_PARALLEL_AGENTS
DEFAULT_SUB_AGENT_TIMEOUT_SECONDS = agent_module.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS
BaseAgent = agent_module.BaseAgent
EvolutionEngine = agent_module.EvolutionEngine

async def _build_components_async(cfg: dict):
    """Build all components from config using ModelClientFactory."""
    console = agent_module.CONSOLE
    context_dir = agent_module.CONTEXT_DIR
    memory_dir = agent_module.MEMORY_DIR
    plugins_dir = agent_module.PLUGINS_DIR
    user_plugins_dir = agent_module.USER_PLUGINS_DIR
    legacy_memory_aliases = agent_module.LEGACY_MEMORY_ALIASES
    max_categories = agent_module.MAX_CATEGORIES
    decay_factor = agent_module.DECAY_FACTOR
    sleep_token_ratio = agent_module.SLEEP_TOKEN_RATIO
    chars_per_token = agent_module.CHARS_PER_TOKEN
    registry_cls = agent_module.ToolRegistry
    builtin_tools_cls = agent_module.BuiltinTools
    skill_catalog_cls = agent_module.SkillCatalog
    user_tool_catalog_cls = agent_module.UserToolCatalog
    mcp_client_cls = agent_module.MCPClient

    client, model, max_tokens = ModelClientFactory.from_config(cfg)
    system_prompt = _load_system_prompt(cfg)

    # Sub-config sections
    mem_cfg = cfg.get("memory", {})
    orch_cfg = cfg.get("orchestration", {})

    workspace_root = Path.cwd().resolve()
    output_dir = _resolve_output_dir(cfg)

    # Resolve active provider format for format-aware classes
    active_provider = cfg.get("active_provider", "anthropic")
    api_format = (
        cfg.get("providers", {}).get(active_provider, {}).get("api_format", "anthropic")
    )

    registry = registry_cls(console=console)

    # Context Manager — build first so BuiltinTools can reference it
    # Config is split into two sub-sections:
    #   context.storage       — LTM store settings (what to keep)
    #   context.consolidation — trigger settings (when/how to consolidate)
    ctx_cfg = cfg.get("context", {})
    storage_cfg = ctx_cfg.get("storage", ctx_cfg)  # fallback: flat cfg for compat
    cons_cfg = ctx_cfg.get("consolidation", ctx_cfg)  # fallback: flat cfg for compat

    ctx_store = LTMStore(
        context_dir=context_dir,
        max_categories=storage_cfg.get("max_categories", max_categories),
        memory_dir=memory_dir,
    )
    memory = MemoryPalace(
        tidy_interval=mem_cfg.get(
            "tidy_interval_seconds", agent_module.MEMORY_TIDY_INTERVAL
        ),
        tidy_threshold=mem_cfg.get(
            "tidy_file_threshold", agent_module.MEMORY_TIDY_FILE_THRESHOLD
        ),
        base_dir=memory_dir,
        context_dir=context_dir,
        store=ctx_store,
    )
    ctx_manager = ContextManager(
        store=ctx_store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(
            store=ctx_store,
            max_categories=storage_cfg.get("max_categories", max_categories),
            decay_factor=storage_cfg.get("decay_factor", decay_factor),
            sleep_token_ratio=cons_cfg.get("token_ratio", sleep_token_ratio),
            keep_last_messages=cons_cfg.get("keep_last_messages", 6),
            chars_per_token=cons_cfg.get("token_estimation", {}).get(
                "chars_per_token", float(chars_per_token)
            ),
            cjk_chars_per_token=cons_cfg.get("token_estimation", {}).get(
                "cjk_chars_per_token", 1.0
            ),
        ),
        idle_seconds=cons_cfg.get("idle_seconds", 300),
        min_messages=cons_cfg.get("min_messages", 4),
        route_keywords=ctx_cfg.get("route_keywords"),
    )

    builtin_tools_cls(
        memory,
        registry,
        context_manager=ctx_manager,
        workspace_root=workspace_root,
        chapter_normalizer=lambda chapter: normalize_memory_chapter(
            chapter, legacy_memory_aliases
        ),
        output_dir=output_dir,
    )

    # Share output_dir with skills via registry context
    registry.set_context("output_dir", str(output_dir))
    registry.set_context(
        "shell_blocked_commands",
        list(cfg.get("shell_blocked_commands", [])),
    )
    tavily_api_key = cfg.get("tavily_api_key", "")
    if isinstance(tavily_api_key, str) and tavily_api_key.startswith("$"):
        tavily_api_key = os.environ.get(tavily_api_key[1:], "")
    if not tavily_api_key:
        tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    if tavily_api_key:
        registry.set_context("tavily_api_key", tavily_api_key)

    skill_catalog = skill_catalog_cls()
    skill_catalog.load_all()
    skill_catalog.register_tools(registry)
    user_tool_catalog = user_tool_catalog_cls()

    mcp_client = None
    mcp_status = {
        "configured_servers": 0,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }
    if cfg.get("mcp_servers"):
        mcp_client = mcp_client_cls(registry)
        mcp_extra_env = {"AGENT_OUTPUT_DIR": str(output_dir)}
        await mcp_client.connect_from_config(cfg, extra_env=mcp_extra_env)
        mcp_status = mcp_client.status_summary()
        if mcp_status["connected_servers"]:
            console.print(
                "[green]MCP active:[/green] "
                f"{mcp_status['connected_servers']} server(s), "
                f"{mcp_status['registered_tools']} tool(s) registered"
            )
        else:
            console.print(
                "[yellow]MCP configured, but no servers connected successfully.[/yellow]"
            )

    agent = BaseAgent(
        client, registry, model=model, max_tokens=max_tokens, api_format=api_format
    )
    agent.max_parallel_agents = max(
        1,
        int(orch_cfg.get("max_parallel_agents", DEFAULT_MAX_PARALLEL_AGENTS)),
    )
    agent.sub_agent_timeout_seconds = max(
        1,
        int(
            orch_cfg.get("sub_agent_timeout_seconds", DEFAULT_SUB_AGENT_TIMEOUT_SECONDS)
        ),
    )
    loaded_user_tools = user_tool_catalog.load_into_registry(registry)
    if loaded_user_tools:
            console.print(
            "[green]User tools loaded:[/green] " + ", ".join(loaded_user_tools)
        )
    agent.register_spawn_capability(system_prompt, workspace_root=workspace_root)
    base_system_prompt = system_prompt

    # EvolutionEngine is created only when evolution is enabled in config.
    # The evolution plugin (and the `evolve` CLI command) both check for None.
    evo_cfg = cfg.get("evolution", {})
    evolution: Optional[EvolutionEngine] = (
        EvolutionEngine(client, model, memory, api_format=api_format)
        if evo_cfg.get("enabled", True)
        else None
    )

    # ── Plugin Catalog ────────────────────────────────────────────────────────
    plugin_catalog = PluginCatalog(
        builtin_dir=plugins_dir,
        user_dir=user_plugins_dir,
        plugin_config=cfg.get("plugins", {}),
    )
    # Build a partial components dict so plugins can self-initialize via
    # on_session_start(); the dict is updated in-place after discover_and_load.
    _partial_components: dict = {
        "client": client,
        "model": model,
        "api_format": api_format,
        "memory": memory,
        "registry": registry,
        "evolution": evolution,
        "skill_catalog": skill_catalog,
        "user_tool_catalog": user_tool_catalog,
        "output_dir": output_dir,
        "workspace_root": workspace_root,
        "cfg": cfg,
    }
    loaded_plugins = plugin_catalog.discover_and_load()
    if loaded_plugins:
        console.print("[green]Plugins loaded:[/green] " + ", ".join(loaded_plugins))

    # Load skills bundled by plugins into the skill catalog
    for _pname, _skills_root in plugin_catalog.get_bundled_skills():
        skill_catalog._load_root(_skills_root, source=f"plugin:{_pname}")
    skill_catalog._rebuild_aliases()

    # Connect MCP servers bundled by plugins
    bundled_mcp = plugin_catalog.get_bundled_mcp()
    if bundled_mcp and mcp_client is None:
        mcp_client = mcp_client_cls(registry)
    if bundled_mcp and mcp_client is not None:
        bundled_cfg = {"mcp_servers": [cfg for _, cfg in bundled_mcp]}
        mcp_extra_env = {"AGENT_OUTPUT_DIR": str(output_dir)}
        await mcp_client.connect_from_config(bundled_cfg, extra_env=mcp_extra_env)
        mcp_status = mcp_client.status_summary()

    agent.plugin_catalog = plugin_catalog

    # Compose system prompt now that plugins are loaded (they may append rules).
    system_prompt = _compose_system_prompt(
        system_prompt,
        registry,
        workspace_root,
        output_dir,
        skill_catalog=skill_catalog,
        plugin_catalog=plugin_catalog,
    )
    agent.context_manager = ctx_manager

    components = {
        **_partial_components,
        "max_tokens": max_tokens,
        "base_system_prompt": base_system_prompt,
        "system_prompt": system_prompt,
        "agent": agent,
        "plugin_catalog": plugin_catalog,
        "context_manager": ctx_manager,
        "mcp_client": mcp_client,
        "mcp_status": mcp_status,
    }
    return components


def _build_components(cfg: dict):
    """Synchronous compatibility wrapper for commands that do not need async setup."""
    return asyncio.run(_build_components_async(cfg))
