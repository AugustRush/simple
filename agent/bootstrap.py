from __future__ import annotations

import asyncio
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path
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
from agent.commands import (
    CommandCoordinator,
    CommandRouter,
    register_builtin_commands,
)
from agent.memory.system import ConsolidationEngine, ContextManager, FactAssertion, LTMStore, LocalRetriever, MemoryPalace, normalize_memory_chapter
from agent.plugins.catalog import PluginCatalog
from agent.runtime import AgentCore, TurnRunner
from agent.ralph import RalphIterationResult, RalphService, RalphTaskStore, RalphVerifier
from agent.tools.files import FileService, resolve_file_access_config

BaseAgent = agent_module.BaseAgent
EvolutionEngine = agent_module.EvolutionEngine

_SESSION_BUILD_LOCK = asyncio.Lock()


def _web_session_home(session_id: str) -> Path:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:32]
    if not clean:
        raise ValueError("invalid session id")
    return shared.web_session_home(clean)


async def _build_web_session_components(session_id: str, base_cfg: dict) -> dict:
    """Build a fully home-scoped runtime for one multiplexed web session."""
    home = _web_session_home(session_id)
    home.mkdir(parents=True, exist_ok=True)
    (home / ".web-session").touch(exist_ok=True)
    # Configuration and executable resources are global. Only conversational
    # state lives below the session home; this avoids stale per-session copies
    # of provider, skill, plugin, and tool settings.
    manifest = home / ".session.json"
    if not manifest.is_file():
        active_provider = str(base_cfg.get("active_provider") or "")
        provider_cfg = (base_cfg.get("providers") or {}).get(active_provider, {})
        model = str(
            (provider_cfg or {}).get("default_model")
            or base_cfg.get("model")
            or ""
        )
        shared._atomic_write_text(
            manifest,
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": str(session_id),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "provider": active_provider,
                    "model": model,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    previous_home = shared.AGENT_HOME
    # Most bootstrap helpers intentionally late-bind shared paths. Serialize
    # the short construction window, then restore the gateway's home; the
    # resulting components retain explicit paths for their own home.
    async with _SESSION_BUILD_LOCK:
        try:
            agent_module._set_agent_home(home)
            session_cfg = dict(base_cfg)
            # Generated files and attachments are always session-owned. A
            # global output_dir setting is intentionally ignored for Web
            # runtimes to prevent cross-session leakage.
            session_cfg["output_dir"] = str(home / "output")
            return await _build_components_async(
                session_cfg, announce=False, resource_home=previous_home
            )
        finally:
            agent_module._set_agent_home(previous_home)


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


async def _build_components_async(
    cfg: dict,
    *,
    announce: bool = True,
    resource_home: Path | None = None,
):
    """Build all components from config using ModelClientFactory."""
    console = shared.CONSOLE
    context_dir = shared.CONTEXT_DIR
    memory_dir = shared.MEMORY_DIR
    plugins_dir = shared.PLUGINS_DIR
    resource_root = (resource_home or shared.AGENT_HOME).resolve()
    if resource_home is None:
        # Preserve the normal CLI/test path mirrors; Web explicitly passes the
        # gateway home so its resource directories are global to all sessions.
        user_plugins_dir = shared.USER_PLUGINS_DIR
        user_skills_dir = shared.SKILLS_DIR
        user_tools_dir = shared.TOOLS_DIR
        prompts_dir = shared.PROMPTS_DIR
    else:
        user_plugins_dir = resource_root / "plugins"
        user_skills_dir = resource_root / "skills"
        user_tools_dir = resource_root / "tools"
        prompts_dir = resource_root / "prompts"
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

    if announce:
        client, model, max_tokens = ModelClientFactory.from_config(cfg)
    else:
        client, model, max_tokens = ModelClientFactory.from_config(
            cfg,
            announce=False,
        )

    # Resolve context_window from provider config, falling back to the
    # DEFAULT_CONTEXT_WINDOW constant.  Follows the same resolution order
    # as max_tokens: top-level cfg key overrides provider-level key.
    providers = cfg.get("providers", {})
    active_name = cfg.get("active_provider", "anthropic")
    provider_cfg = providers.get(active_name, {})
    context_window = (
        cfg.get("context_window")
        or provider_cfg.get("context_window")
    )
    if context_window is not None:
        context_window = int(context_window)

    # ``max_tokens`` is an output budget, so it cannot consume the entire
    # provider context window: system instructions, tool schemas, and the
    # user's conversation also need room.  A provider config that sets both
    # values to the same number otherwise makes every turn fail before the
    # request is sent (especially visible when a historical web session is
    # lazily re-created after a restart). Keep the configured value when it is
    # safe, otherwise reserve a bounded input slice automatically.
    if context_window is not None and max_tokens >= context_window:
        reserve = max(4096, min(16384, context_window // 10))
        adjusted = max(1, context_window - reserve)
        if adjusted < max_tokens:
            if announce:
                console.print(
                    f"[yellow]max_tokens={max_tokens} equals context_window={context_window}; "
                    f"using {adjusted} to reserve input context[/yellow]"
                )
            max_tokens = adjusted

    system_prompt = _load_system_prompt(cfg, prompts_dir=prompts_dir)

    # Sub-config sections
    mem_cfg = cfg.get("memory", {})
    orch_cfg = cfg.get("orchestration", {})

    workspace_root = Path.cwd().resolve()
    output_dir = _resolve_output_dir(cfg)
    # Construct the immutable file access policy before any file or shell
    # tool is registered.  Invalid configuration or overlapping roots abort
    # bootstrap rather than degrading into an unsafe runtime.
    file_policy = resolve_file_access_config(
        cfg,
        workspace_root=workspace_root,
        output_dir=output_dir,
    )
    file_service = FileService(file_policy)

    # Resolve active provider format for format-aware classes
    active_provider = cfg.get("active_provider", "anthropic")
    api_format = (
        cfg.get("providers", {}).get(active_provider, {}).get("api_format", "anthropic")
    )
    supports_vision = provider_supports_vision(cfg, active_provider)

    registry = registry_cls(console=console)
    # Resource definitions (skills, tools, plugins, prompts) are global for
    # Web sessions, while state directories below the session home remain
    # isolated.  Keep this explicit for tools that resolve paths at call time.
    registry.set_context("resource_home", str(resource_root))
    registry.set_context("user_skills_dir", str(user_skills_dir))
    registry.set_context("user_tools_dir", str(user_tools_dir))
    registry.set_context("user_plugins_dir", str(user_plugins_dir))

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
        file_service=file_service,
    )

    # Share output_dir with skills via registry context
    registry.set_context("output_dir", str(output_dir))
    registry.set_context("file_access_policy", file_policy)
    registry.set_context("supports_vision", supports_vision)
    registry.set_context(
        "shell_blocked_commands",
        list(cfg.get("shell_blocked_commands", [])),
    )
    registry.set_context(
        "shell_allowed_commands",
        list(cfg.get("shell_allowed_commands", [])),
    )
    registry.set_context(
        "shell_permission_level",
        str(
            (cfg.get("permissions") or {}).get("shell_level", "ask") or "ask"
        ),
    )
    registry.set_context(
        "shell_sandbox_mode",
        str(
            (cfg.get("permissions") or {}).get("shell_sandbox", "read_all")
            or "read_all"
        ),
    )
    registry.set_context(
        "shell_devices",
        bool((cfg.get("permissions") or {}).get("shell_devices", True)),
    )
    registry.set_context(
        "shell_secret_paths",
        tuple(
            str(entry).strip()
            for entry in (
                (cfg.get("permissions") or {}).get("shell_secret_paths") or ()
            )
            if str(entry).strip()
        ),
    )
    if announce:
        # A posture nobody can see is a posture nobody maintains.  Running
        # unsandboxed is a deliberate, temporary choice that lives in a
        # permanent file, so it has to announce itself on every start.
        from agent.security.filesystem_sandbox import sandbox_posture_warning

        posture = sandbox_posture_warning(
            str(registry.get_context("shell_sandbox_mode") or "read_all"),
            devices=bool(registry.get_context("shell_devices", True)),
        )
        if posture:
            console.print(f"[bold yellow]⚠ {posture}[/bold yellow]")
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

    skill_catalog = skill_catalog_cls(user_root=user_skills_dir)
    skill_catalog.load_all()
    skill_catalog.register_tools(registry)
    user_tool_catalog = user_tool_catalog_cls(root=user_tools_dir)

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
        context_window=context_window,
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
    # Bounds total sub-agents per turn, not concurrency.  0 means "derive from
    # max_parallel_agents".
    agent.max_agents_per_turn = max(
        0,
        int(orch_cfg.get("max_agents_per_turn", 0)),
    )
    # `max_steps` is the preferred spelling; `max_tool_call_iterations` stays
    # accepted because it is a published config key and dropping it would
    # break existing configs.  Both name the same bound: the number of steps
    # (model request + its tools) allowed in one turn.
    agent.max_tool_call_iterations = _bounded_int(
        cfg.get(
            "max_steps",
            cfg.get("max_tool_call_iterations", shared.MAX_TOOL_CALL_ITERATIONS),
        ),
        default=shared.MAX_TOOL_CALL_ITERATIONS,
        min_value=1,
        max_value=shared.MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS,
    )
    agent.max_truncation_continuations = _bounded_int(
        cfg.get(
            "max_truncation_continuations",
            shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
        ),
        default=shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
        min_value=0,
        max_value=shared.MAX_CONFIGURABLE_TRUNCATION_CONTINUATIONS,
    )
    agent.llm_max_retries = max(
        0,
        int(cfg.get("llm_max_retries", shared.DEFAULT_LLM_MAX_RETRIES)),
    )
    agent.llm_retry_base_delay = max(
        0.1,
        float(cfg.get("llm_retry_base_delay", shared.DEFAULT_LLM_RETRY_BASE_DELAY)),
    )
    agent.result_content_max_chars = min(
        shared.MAX_RESULT_CONTENT_CHARS,
        max(
            shared.MIN_RESULT_CONTENT_CHARS,
            int(
                orch_cfg.get(
                    "result_content_max_chars",
                    shared.DEFAULT_RESULT_CONTENT_MAX_CHARS,
                )
            ),
        ),
    )
    user_tools_cfg = cfg.get("user_tools", {})
    user_tools_enabled = (
        bool(user_tools_cfg.get("enabled", False))
        if isinstance(user_tools_cfg, dict)
        else False
    )
    # Two admission modes, never zero.  Enabling user_tools trusts the whole
    # directory; leaving it off still loads the individual tools the user
    # approved by hand, so a tool created and confirmed in an earlier session
    # keeps working instead of silently vanishing on restart.
    loaded_user_tools = user_tool_catalog.load_into_registry(
        registry, require_approval=not user_tools_enabled
    )
    if announce:
        if loaded_user_tools:
            scope = "all" if user_tools_enabled else "approved"
            console.print(
                f"[green]User tools loaded ({scope}):[/green] "
                + ", ".join(loaded_user_tools)
            )
        elif not user_tools_enabled:
            console.print(
                "[dim]No approved user tools; create one with create_tool, or set "
                "user_tools.enabled=true to load every ~/.agent/tools/*.py[/dim]"
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
        "file_access_policy": file_policy,
        "file_service": file_service,
        "cfg": cfg,
    }
    loaded_plugins = plugin_catalog.discover_and_load()
    if loaded_plugins and announce:
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
        "config_revision": 0,
    }
    # WebChannel uses this hook to provision one independent agent home per
    # browser session under ``~/.agent/web/sessions/<session-id>``.
    async def _session_components_factory(session_id: str) -> dict:
        # Resolve the global config at session-runtime creation time so config
        # changes apply to subsequent turns without interrupting active ones.
        global_cfg, _ = agent_module.load_config()
        return await _build_web_session_components(session_id, global_cfg or cfg)

    components["session_components_factory"] = _session_components_factory
    components["session_store_factory"] = lambda session_id: LTMStore(
        context_dir=_web_session_home(str(session_id)) / "context",
        memory_dir=_web_session_home(str(session_id)) / "memory",
    )

    async def _execute_ralph_iteration(
        iter_ctx: Any,
        prompt: str,
        *,
        cancel_token: Any = None,
        model_override: str | None = None,
    ) -> RalphIterationResult:
        result = await agent.send_message(iter_ctx, prompt)
        return RalphIterationResult(
            iteration=1,
            summary=result.content or "",
            tool_calls=tuple(result.tool_calls_made or ()),
            error=result.error,
        )

    def _new_ralph_context() -> Any:
        iter_ctx = agent_module.AgentContext(system_prompt=system_prompt)
        iter_ctx.metadata["skill_catalog"] = skill_catalog
        return iter_ctx

    components["ralph_service"] = RalphService(
        turn_executor=_execute_ralph_iteration,
        store=RalphTaskStore(agent_module.TASKS_DIR),
        verifier=RalphVerifier(
            workspace_root=workspace_root,
            output_dir=output_dir,
            blocked_commands=tuple(cfg.get("shell_blocked_commands", ())),
        ),
        context_factory=_new_ralph_context,
        context_manager=ctx_manager,
    )
    components["turn_runner"] = TurnRunner(components)
    components["agent_core"] = AgentCore(components)
    command_router = CommandRouter(skill_catalog=skill_catalog)
    register_builtin_commands(command_router)
    if hasattr(plugin_catalog, "get_slash_commands"):
        command_router.register_plugin_catalog(plugin_catalog)
    components["command_router"] = command_router

    def _command_coordinator_factory(
        *,
        event_hook: Any = None,
        cancel_token_factory: Any = None,
    ) -> CommandCoordinator:
        return CommandCoordinator(
            components["agent_core"],
            command_router,
            components=components,
            config=cfg,
            cancel_token_factory=cancel_token_factory,
            event_hook=event_hook,
        )

    components["command_coordinator_factory"] = _command_coordinator_factory
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
