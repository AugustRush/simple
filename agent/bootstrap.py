from __future__ import annotations

import asyncio
from pathlib import Path
import os
from typing import Any, Optional

import agent as agent_module
from agent import shared
from agent.config import (
    ModelClientFactory,
    _compose_system_prompt,
    _load_system_prompt,
    _resolve_output_dir,
    provider_supports_vision,
)
from agent.memory.system import BackgroundMemoryWorker, ConsolidationEngine, ContextManager, FactAssertion, LTMStore, LocalRetriever, MemoryPalace, normalize_memory_chapter
from agent.plugins.catalog import PluginCatalog
from agent.runtime import AgentCore, TurnRunner
from agent.skills.catalog import SkillCatalog
from agent.tools.builtin_tools import BuiltinTools
from agent.tools.runtime import MCPClient, ToolRegistry, UserToolCatalog

BaseAgent = agent_module.BaseAgent
EvolutionEngine = agent_module.EvolutionEngine


def _bounded_int(
    value: object,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, min_value), max_value)


def _empty_mcp_status(configured_servers: int = 0) -> dict[str, int]:
    return {
        "configured_servers": configured_servers,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }


def _refresh_components_system_prompt(components: dict) -> str:
    refreshed = _compose_system_prompt(
        components.get("base_system_prompt", ""),
        components.get("registry"),
        components.get("workspace_root"),
        components.get("output_dir"),
        skill_catalog=components.get("skill_catalog"),
        plugin_catalog=components.get("plugin_catalog"),
    )
    components["system_prompt"] = refreshed
    ctx = components.get("ctx")
    if ctx is not None:
        ctx.system_prompt = refreshed
    return refreshed


async def _connect_mcp_in_background(
    components: dict,
    mcp_client: Any,
    mcp_config: dict,
    *,
    extra_env: dict[str, str],
) -> None:
    try:
        await mcp_client.connect_from_config(mcp_config, extra_env=extra_env)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        shared.CONSOLE.print(f"[yellow]MCP background connect failed: {exc}[/yellow]")
    finally:
        status = mcp_client.status_summary()
        components["mcp_status"].clear()
        components["mcp_status"].update(status)
        if status.get("registered_tools", 0):
            _refresh_components_system_prompt(components)
        if status.get("connected_servers", 0):
            shared.CONSOLE.print(
                "[green]MCP active:[/green] "
                f"{status['connected_servers']} server(s), "
                f"{status['registered_tools']} tool(s) registered"
            )
        elif status.get("configured_servers", 0):
            shared.CONSOLE.print(
                "[yellow]MCP configured, but no servers connected successfully.[/yellow]"
            )


async def _build_components_async(cfg: dict):
    """Build all components from config using ModelClientFactory."""
    console = shared.CONSOLE
    context_dir = shared.CONTEXT_DIR
    memory_dir = shared.MEMORY_DIR
    plugins_dir = shared.PLUGINS_DIR
    user_plugins_dir = shared.USER_PLUGINS_DIR
    legacy_memory_aliases = shared.LEGACY_MEMORY_ALIASES
    max_categories = shared.MAX_CATEGORIES
    decay_factor = shared.DECAY_FACTOR
    sleep_token_ratio = shared.SLEEP_TOKEN_RATIO
    chars_per_token = shared.CHARS_PER_TOKEN
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
    supports_vision = provider_supports_vision(cfg, active_provider)

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
    assistant_identity_cfg = cfg.get("assistant_identity", {})
    assistant_name = str(assistant_identity_cfg.get("name", "") or "").strip()
    assistant_role = str(assistant_identity_cfg.get("role", "") or "").strip()
    if assistant_name:
        ctx_store.add_fact_assertion(
            FactAssertion(
                id=f"bootstrap-assistant-name-{assistant_name.lower()}",
                subject="assistant",
                predicate="name",
                value=assistant_name,
                source_kind="bootstrap",
                source_id="config.assistant_identity.name",
            )
        )
    if assistant_role:
        ctx_store.add_fact_assertion(
            FactAssertion(
                id=f"bootstrap-assistant-role-{assistant_role.lower().replace(' ', '_')}",
                subject="assistant",
                predicate="role",
                value=assistant_role,
                source_kind="bootstrap",
                source_id="config.assistant_identity.role",
            )
        )
    memory = MemoryPalace(
        tidy_interval=mem_cfg.get("tidy_interval_seconds", shared.MEMORY_TIDY_INTERVAL),
        tidy_threshold=mem_cfg.get("tidy_file_threshold", shared.MEMORY_TIDY_FILE_THRESHOLD),
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
    registry.set_context("supports_vision", supports_vision)
    registry.set_context(
        "shell_blocked_commands",
        list(cfg.get("shell_blocked_commands", [])),
    )
    audio_cfg = cfg.get("audio", {})
    audio_transcription_command = ""
    if isinstance(audio_cfg, dict):
        audio_transcription_command = str(
            audio_cfg.get("transcription_command", "") or ""
        ).strip()
    if audio_transcription_command:
        registry.set_context(
            "audio_transcription_command",
            audio_transcription_command,
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
    mcp_server_configs = list(cfg.get("mcp_servers", []) or [])
    mcp_status = _empty_mcp_status(len(mcp_server_configs))
    if mcp_server_configs:
        mcp_client = mcp_client_cls(registry)
        console.print(
            "[dim]MCP connecting in background: "
            f"{len(mcp_server_configs)} configured server(s)[/dim]"
        )

    agent = BaseAgent(
        client,
        registry,
        model=model,
        max_tokens=max_tokens,
        api_format=api_format,
        supports_vision=supports_vision,
    )
    agent.max_parallel_agents = max(
        1,
        int(orch_cfg.get("max_parallel_agents", shared.DEFAULT_MAX_PARALLEL_AGENTS)),
    )
    agent.sub_agent_timeout_seconds = max(
        1,
        int(
            orch_cfg.get("sub_agent_timeout_seconds", shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS)
        ),
    )
    agent.sub_agent_retries = max(
        0,
        int(orch_cfg.get("sub_agent_retries", shared.DEFAULT_SUB_AGENT_RETRIES)),
    )
    agent.max_tool_call_iterations = _bounded_int(
        cfg.get("max_tool_call_iterations", shared.MAX_TOOL_CALL_ITERATIONS),
        default=shared.MAX_TOOL_CALL_ITERATIONS,
        min_value=1,
        max_value=shared.MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS,
    )
    agent.llm_max_retries = max(
        0,
        int(cfg.get("llm_max_retries", shared.DEFAULT_LLM_MAX_RETRIES)),
    )
    agent.llm_retry_base_delay = max(
        0.1,
        float(cfg.get("llm_retry_base_delay", shared.DEFAULT_LLM_RETRY_BASE_DELAY)),
    )
    agent.result_content_max_chars = max(
        0,
        int(orch_cfg.get("result_content_max_chars", shared.DEFAULT_RESULT_CONTENT_MAX_CHARS)),
    )
    user_tools_cfg = cfg.get("user_tools", {})
    user_tools_enabled = (
        bool(user_tools_cfg.get("enabled", False))
        if isinstance(user_tools_cfg, dict)
        else False
    )
    loaded_user_tools: list[str] = []
    if user_tools_enabled:
        loaded_user_tools = user_tool_catalog.load_into_registry(registry)
        if loaded_user_tools:
            console.print(
                "[green]User tools loaded:[/green] " + ", ".join(loaded_user_tools)
            )
    else:
        console.print(
            "[dim]User Python tools disabled; set user_tools.enabled=true to load trusted ~/.agent/tools/*.py[/dim]"
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
        "user_tools_enabled": user_tools_enabled,
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
    if bundled_mcp:
        mcp_server_configs.extend(server_cfg for _, server_cfg in bundled_mcp)
        mcp_status = _empty_mcp_status(len(mcp_server_configs))
    if bundled_mcp and mcp_client is None:
        mcp_client = mcp_client_cls(registry)

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
        "mcp_task": None,
    }
    components["turn_runner"] = TurnRunner(components)
    components["agent_core"] = AgentCore(components)
    # Stash references so builtin tools (install_plugin / uninstall_plugin /
    # list_installed_plugins) can trigger hot-reload through the registry
    # context.  Same dict so reload sees subsequent updates in place.
    registry.set_context("plugin_catalog", plugin_catalog)
    registry.set_context("components", components)
    registry.set_context("mcp_client", mcp_client)
    if mcp_client is not None and mcp_server_configs:
        components["mcp_task"] = asyncio.create_task(
            _connect_mcp_in_background(
                components,
                mcp_client,
                {"mcp_servers": mcp_server_configs},
                extra_env={"AGENT_OUTPUT_DIR": str(output_dir)},
            ),
            name="mcp-connect",
        )
    return components


def _build_components(cfg: dict):
    """Synchronous compatibility wrapper for commands that do not need async setup."""
    return asyncio.run(_build_components_async(cfg))
