from __future__ import annotations

import asyncio
import hashlib
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
    active_context_window,
    active_output_reserve,
    bounded_int,
    provider_supports_vision,
)
from agent.commands import (
    CommandCoordinator,
    CommandRouter,
    register_builtin_commands,
)
from agent.commands.composition import ensure_command_router
from agent.runtime.system_prompt import refresh_system_prompt
from agent.memory.system import ConsolidationEngine, ContextManager, FactAssertion, LTMStore, LocalRetriever, MemoryPalace, normalize_memory_chapter
from agent.plugins.catalog import PluginCatalog
from agent.channels.registry import ChannelRegistry
from agent.memory.plugin import MemoryPlugin
from agent.core.agent_plugin import AgentPlugin
from agent.tools.plugin import ToolsPlugin
from agent.memory.session_plugin import SessionPlugin
from agent.ralph.plugin import RalphPlugin
from agent.runtime.factory import RuntimeFactory
from agent.runtime import AgentCore, TurnRunner
from agent.ralph import (
    RalphIterationResult,
    RalphService,
    RalphTaskStore,
    RalphVerifier,
    open_task_store,
)
from agent.tools.files import FileService, resolve_file_access_config
from agent.runtime.services import require_service
from agent.pathing import project_memory_scope

BaseAgent = agent_module.BaseAgent
EvolutionEngine = agent_module.EvolutionEngine


def _reserve_input_context(
    max_tokens: int,
    context_window: int | None,
    *,
    reserve: int = shared.DEFAULT_OUTPUT_RESERVE,
    announce: bool = False,
) -> int:
    """Keep the output cap from consuming the provider's whole context.

    The cap is a ceiling on one response, not a claim on the window: a request
    reserves ``reserve`` tokens for its answer and spends the rest on input.  So
    the only config this has to correct is one whose cap leaves less than that
    reserve free.

    The guard used to be ``max_tokens >= context_window``, which caught only the
    most extreme case and missed the one that actually occurs: deepseek's 64000
    against a 128000 window passed the test while leaving 64000 for the entire
    conversation.  The input budget no longer derives from the cap at all (see
    ``BaseAgent._input_token_budget``), so that config is now handled where it
    belongs; this remains to catch a cap that would leave the window with no
    room for input whatsoever.
    """
    if context_window is None:
        return max_tokens
    adjusted = max(1, context_window - max(1, int(reserve)))
    if max_tokens <= adjusted:
        return max_tokens
    if announce:
        shared.CONSOLE.print(
            f"[yellow]max_tokens={max_tokens} leaves less than the "
            f"{reserve}-token output reserve free in a {context_window}-token "
            f"window; using {adjusted} to reserve input context[/yellow]"
        )
    return adjusted


def build_gateway_channels(cfg: dict) -> list[Channel]:
    """Every channel a configuration enables, collected from the channel plugins.

    This used to hold a branch per channel -- read the section, import the class, filter
    the config against its dataclass, construct, repeat.  Two channels meant two
    branches, and the reason that mattered is not the line count: **a plugin could not
    contribute a channel at all**, so a third party's transport had to be added here.

    Now every channel is a plugin registering on the ``channels`` service, the built-ins
    included, and this is the composition that mounts them.  A channel whose dependency
    is missing reports and is skipped rather than aborting startup, which is the one
    behaviour of the old branches that was worth keeping -- see
    :mod:`agent.channels.plugin`.

    The host is local to this call: the gateway's channels are built once, per process,
    and a registry that outlived the call would keep transports the process is not
    running.
    """
    from agent.channels.plugin import FeishuChannelPlugin, WebChannelPlugin
    from agent.channels.registry import ChannelRegistry
    from agent.kernel.plugin import PluginHost

    registry = ChannelRegistry()
    host = PluginHost()
    host.services.provide("cfg", cfg)
    host.services.provide("channels", registry)
    # Unconditionally: each contributes nothing unless its section says `enabled`, so the
    # switch stays in the config rather than moving back into this function.
    for plugin in (WebChannelPlugin(), FeishuChannelPlugin()):
        host.load(plugin)
    return list(registry.all())


def _provider_client_factory(provider_cfg: dict, api_format: str) -> Any:
    """One client for a non-active provider (used by RoutingTransport).

    Delegates to the provider registry, which is now the single place a client
    is constructed.  This function previously carried its own copy, and the
    copy had drifted: an unrecognised format silently became an OpenAI client
    here while ``ModelClientFactory.from_config`` raised for the same input.
    """
    from agent.core.providers import build_client

    return build_client(provider_cfg, api_format)


def _web_session_home(session_id: str) -> Path:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:32]
    if not clean:
        raise ValueError("invalid session id")
    return shared.web_session_home(clean)


async def _build_web_session_components(
    session_id: str,
    base_cfg: dict,
    global_components: dict,
    *,
    provider_client_cache: dict | None = None,
) -> dict:
    """Build a lightweight execution view for one multiplexed Web session.

    Provider clients, discovered tools, skills, plugins, MCP connections and
    the durable memory store belong to the process runtime. A Web session only
    owns mutable conversation state, workspace policy and output paths.
    """
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
    session_cfg = dict(base_cfg)
    try:
        persisted = json.loads(manifest.read_text(encoding="utf-8"))
        persisted_workspace = persisted.get("workspace_root")
        if isinstance(persisted_workspace, str) and persisted_workspace.strip():
            session_cfg["workspace_root"] = persisted_workspace
            if persisted.get("workspace_write") is True:
                file_access = dict(session_cfg.get("file_access") or {})
                workspace_access = dict(file_access.get("workspace") or {})
                workspace_access.update({"read": True, "write": True})
                file_access["workspace"] = workspace_access
                session_cfg["file_access"] = file_access
    except (OSError, ValueError, TypeError):
        pass

    output_dir = (home / "output").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    configured_workspace = session_cfg.get("workspace_root")
    workspace_root = (
        Path(str(configured_workspace)).expanduser().resolve(strict=False)
        if configured_workspace
        else Path(global_components.get("workspace_root") or Path.cwd()).resolve()
    )
    session_cfg["output_dir"] = str(output_dir)
    session_cfg["workspace_root"] = str(workspace_root)
    file_policy = resolve_file_access_config(
        session_cfg,
        workspace_root=workspace_root,
        output_dir=output_dir,
    )
    file_service = FileService(
        file_policy,
        write_scope=("*",) if file_policy.workspace_write else (),
    )
    project_scope = project_memory_scope(workspace_root)

    global_registry = global_components["registry"]
    registry = global_registry.fork(
        {
            "workspace_root": str(workspace_root),
            "output_dir": str(output_dir),
            "file_access_policy": file_policy,
            "file_service": file_service,
            "project_memory_scope": project_scope,
        },
        exclude_source_prefixes=("runtime:spawn",),
    )
    global_agent = global_components["agent"]
    # Same per-call model routing as the global agent: a session may switch to
    # any configured provider's model. SDK clients are shared through the
    # process-lifetime cache so per-session rebuilds (config hot-reload) do
    # not leak a connection pool per session per provider.  Handed to the
    # constructor so the session's sub-agents inherit the routing table too.
    #
    # ``session_cfg`` is the config on disk *now* (that is why the session is
    # being rebuilt at all), while the client below was built when the process
    # started.  Those agree only as long as nobody edited ``active_provider``
    # in between, and the routing table is built from the config on disk — so
    # left unguarded, a provider switch routes the *new* provider's models
    # (and its default model, taken from ``active_model_and_tokens`` below)
    # through the *old* provider's client, which answers
    #
    #     400 The supported API model names are ..., but you passed <模型>
    #
    # Recording the inherited client's endpoint/credential/format identity
    # lets the transport detect that and take the client for the config it was
    # given, from the same shared cache. ``global_agent.api_format`` stays the
    # caller's word for the inherited client: when its identity matches, the
    # two travel together; when it does not, the transport rebuilds both from
    # the current config. The client and format the session actually ends up
    # with are read back off the transport rather than resolved a second time.
    from agent.core.transport import (
        build_routing_transport,
        provider_client_cache_key,
    )

    session_transport = build_routing_transport(
        session_cfg,
        global_agent.api_format,
        global_components["client"],
        client_factory=_provider_client_factory,
        client_cache=provider_client_cache,
        # ``None`` when it was never recorded (a hand-built components dict),
        # which means "trust it" — the same behaviour as before.  The process
        # bootstrap records the endpoint/credential/format identity beside
        # the client so an in-place provider edit is detected too.
        default_client_cache_key=global_components.get("client_cache_key"),
    )
    session_model, session_max_tokens = ModelClientFactory.active_model_and_tokens(
        session_cfg
    )
    session_context_window = active_context_window(session_cfg)
    session_output_reserve = active_output_reserve(session_cfg)
    session_max_tokens = _reserve_input_context(
        session_max_tokens, session_context_window, reserve=session_output_reserve
    )
    session_provider = str(session_cfg.get("active_provider") or "")
    session_supports_vision = provider_supports_vision(
        session_cfg, session_provider
    )
    registry.set_context("supports_vision", session_supports_vision)
    session_provider_cfg = (session_cfg.get("providers") or {}).get(
        session_provider, {}
    )
    session_client_cache_key = provider_client_cache_key(
        session_provider_cfg, session_transport.api_format
    )
    # Created before the agent, like the global root's mapping: the session agent
    # reads ``llm`` from it at construction, and the rest is added below.
    session_components = dict(global_components)
    session_components["llm"] = session_transport
    agent = BaseAgent(
        session_transport.default.client,
        registry,
        model=session_model,
        max_tokens=session_max_tokens,
        api_format=session_transport.api_format,
        supports_vision=session_supports_vision,
        context_window=session_context_window,
        output_reserve=session_output_reserve,
        transport=session_transport,
        # One name, for the same reason as the process-wide construction below.
        services={"llm": session_transport},
    )
    for name in (
        "max_parallel_agents",
        "sub_agent_timeout_seconds",
        "sub_agent_retries",
        "max_agents_per_turn",
        "max_tool_call_iterations",
        "max_truncation_continuations",
        "max_rendezvous_rounds",
        "result_content_max_chars",
        "llm_max_retries",
        "llm_retry_base_delay",
    ):
        setattr(agent, name, getattr(global_agent, name))
    agent.content_filter = global_agent.content_filter
    agent.plugin_catalog = global_components.get("plugin_catalog")
    agent.context_manager = global_components.get("context_manager")
    base_system_prompt = str(global_components.get("base_system_prompt") or "")
    agent.register_spawn_capability(base_system_prompt, workspace_root=workspace_root)
    system_prompt = _compose_system_prompt(
        base_system_prompt,
        registry,
        workspace_root,
        output_dir,
        skill_catalog=global_components.get("skill_catalog"),
        plugin_catalog=global_components.get("plugin_catalog"),
    )

    session_components.update(
        {
            "cfg": session_cfg,
            # The client, the provider it belongs to and the model all come
            # from ``session_transport`` / ``session_cfg`` above rather than
            # from the global components: a session is built against the
            # config on disk, and inheriting any of these would leave it
            # half-built against the config the process started with.
            "client": session_transport.default.client,
            "client_cache_key": session_client_cache_key,
            "model": session_model,
            "client_provider": session_provider,
            "max_tokens": session_max_tokens,
            "registry": registry,
            # The session's own agent -- built just above from the config on disk, which
            # is the whole point of a rebuild.  A blanket removal of `"agent": agent` from
            # this file took this line with it, because the global one had to go and this
            # one has the same text.
            "agent": agent,
            "system_prompt": system_prompt,
            "workspace_root": workspace_root,
            "output_dir": output_dir,
            "file_access_policy": file_policy,
            "file_service": file_service,
            "mcp_task": None,
            "_shares_global_runtime": True,
        }
    )
    session_components["agent_core"] = AgentCore(session_components)
    # Named dependencies; see the note on the global bootstrap path above.
    registry.set_context("command_router", session_components.get("command_router"))
    registry.set_context("skill_catalog", session_components.get("skill_catalog"))
    registry.set_context("registry", registry)
    _session_mcp = session_components.get("mcp_client")
    if _session_mcp is not None:
        registry.set_context("mcp_client", _session_mcp)
    return session_components


def _empty_mcp_status(configured_servers: int = 0) -> dict[str, int]:
    return {
        "configured_servers": configured_servers,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }


def _refresh_components_system_prompt(components: dict) -> str:
    """Recompose the prompt and publish it.

    The composition stays here (see agent/runtime/system_prompt.py for why the
    *publish* is shared and the *compose* is not), but the write goes through one
    function so it cannot drift from the other publishers.
    """
    return refresh_system_prompt(components)


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

    # Record the human-readable provider name beside the client; the exact
    # endpoint/credential/format identity is recorded after the transport is
    # built, so web-session rebuilds can decide whether this client is current.
    client_provider = str(cfg.get("active_provider") or "")

    providers = cfg.get("providers", {})
    context_window = active_context_window(cfg)
    output_reserve = active_output_reserve(cfg)
    max_tokens = _reserve_input_context(
        max_tokens, context_window, reserve=output_reserve, announce=announce
    )

    system_prompt = _load_system_prompt(cfg, prompts_dir=prompts_dir)

    # Sub-config sections
    mem_cfg = cfg.get("memory", {})
    orch_cfg = cfg.get("orchestration", {})

    configured_workspace = cfg.get("workspace_root")
    workspace_root = Path(str(configured_workspace)).expanduser().resolve() if configured_workspace else Path.cwd().resolve()
    output_dir = _resolve_output_dir(cfg)
    # Construct the immutable file access policy before any file or shell
    # tool is registered.  Invalid configuration or overlapping roots abort
    # bootstrap rather than degrading into an unsafe runtime.
    file_policy = resolve_file_access_config(
        cfg,
        workspace_root=workspace_root,
        output_dir=output_dir,
    )
    # The primary agent owns the selected project root. Child agents still
    # receive their own narrowed write_scope when they are spawned.
    file_service = FileService(
        file_policy,
        write_scope=("*",) if file_policy.workspace_write else (),
    )

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
    registry.set_context("workspace_root", str(workspace_root))
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

    # web_fetch's egress proxy.  Unset means "read the standard environment
    # variables"; a URL pins one; "none" forces direct connections.  This
    # matters behind a fake-IP resolver (Clash's enhanced-mode: fake-ip), where
    # every name answers with a reserved address that the direct path refuses.
    web_proxy = cfg.get("web_proxy", "")
    if isinstance(web_proxy, str) and web_proxy.strip():
        registry.set_context("web_proxy", web_proxy.strip())

    skill_catalog = skill_catalog_cls(
        user_root=user_skills_dir,
        # The switches from config.json, so a skill turned off before this
        # process started is off when it starts.  The rest of the file is
        # irrelevant to the catalog, and a non-dict section reads as "no
        # switches" rather than raising: a hand-edited config must not stop
        # the agent from loading.
        skill_config=cfg.get("skills"),
    )
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

    # Route per-call model overrides to the provider that owns the model, so
    # the UI's model dropdown can offer every configured provider's models.
    # The active provider stays the default; unknown model ids keep today's
    # behavior (sent to the active client).  The transport is handed to the
    # constructor rather than assigned afterwards so everything the agent
    # derives — sub-agents above all — sees the routing table from the start.
    from agent.core.transport import (
        build_routing_transport,
        provider_client_cache_key,
    )

    # Process-lifetime SDK client cache shared with every web session's
    # routing transport, so per-session rebuilds do not leak one connection
    # pool per session per provider.
    provider_client_cache: dict = {}
    routing_transport = build_routing_transport(
        cfg,
        api_format,
        client,
        client_factory=_provider_client_factory,
        client_cache=provider_client_cache,
    )
    client_cache_key = provider_client_cache_key(
        (providers or {}).get(client_provider, {}), api_format
    )
    # ── The composition mapping ──────────────────────────────────────────────
    # Created **before** the agent, and populated as the rest is built, because
    # everything below is a participant in it.  A mapping assembled after the agent
    # existed cannot be read by the agent: it would have to be handed over
    # afterwards, which is a second way for a participant to receive its
    # composition, and the point of a service container is that there is one.
    #
    # ``llm`` is the model adapter.  It is provided here rather than only passed as
    # a constructor argument so that a composition replaces the model plugin by
    # providing this name -- the plan's ``llm`` seam, and the edge the agent loop
    # declares.
    _partial_components: dict = {
        "client": client,
        "client_cache_key": client_cache_key,
        "model": model,
        "api_format": api_format,
        "registry": registry,
        "max_tokens": max_tokens,
        "skill_catalog": skill_catalog,
        "user_tool_catalog": user_tool_catalog,
        "output_dir": output_dir,
        "workspace_root": workspace_root,
        "file_access_policy": file_policy,
        "file_service": file_service,
        "client_provider": client_provider,
        "llm": routing_transport,
        "cfg": cfg,
    }
    components = _partial_components
    user_tools_cfg = cfg.get("user_tools", {})
    user_tools_enabled = (
        bool(user_tools_cfg.get("enabled", False))
        if isinstance(user_tools_cfg, dict)
        else False
    )
    _partial_components["user_tools_enabled"] = user_tools_enabled
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
    base_system_prompt = system_prompt

    # No EvolutionEngine here any more.  The plugin builds the one it uses, reading the
    # config section itself -- the same shape the channel plugins have ("mounted
    # unconditionally, contributes nothing unless its section says enabled", see
    # agent/channels/plugin.py).  It was constructed here because `on_session_start` was
    # handed a mapping once, so the engine had to exist *before* the catalog that would
    # have let the plugin build it; a kernel plugin's setup runs after mounting, and can
    # read what it needs.
    #
    # That ordering was also what blocked `memory` from becoming a plugin: this line used
    # the memory object, and it sits above the catalog.  See the status document.

    # The channels a composition runs.  Provided so a plugin can contribute one -- a
    # tool, a command, a role, an MCP config and a skill root were all contributable and
    # a channel was not, because the gateway's set was a branch per built-in in this
    # file.  See `agent/channels/registry.py`.
    _partial_components["channels"] = ChannelRegistry()

    # The named way to build a runtime, for anything inside a composition that needs one.
    # Six call sites already reach ``agent._build_components_async``; five of them are
    # this root building itself, and the sixth is a scheduled run, which is not the root
    # and should not have to import it.  ``agent/runtime/factory.py`` explains the rest.
    _partial_components["runtime_factory"] = RuntimeFactory()

    # ── Plugin Catalog ────────────────────────────────────────────────────────
    # Constructed after the partial components because the kernel adopts them.
    plugin_catalog = PluginCatalog(
        builtin_dir=plugins_dir,
        user_dir=user_plugins_dir,
        plugin_config=cfg.get("plugins", {}),
        components=_partial_components,
    )
    _partial_components["plugin_catalog"] = plugin_catalog
    loaded_plugins = plugin_catalog.discover_and_load()
    # Mounted before the blocks below, because those resolve what these provide.  Ralph
    # and Memory are the two plugins the composition root mounts itself; everything else
    # is discovered from disk.
    plugin_catalog.mount(RalphPlugin())
    plugin_catalog.mount(MemoryPlugin())
    # After Memory: it injects `ltm_store`.
    plugin_catalog.mount(SessionPlugin())
    # Last of the three composition-owned plugins: it injects `context_manager`
    # and `plugin_catalog`, both provided by the time this runs.
    # Handed the class, so this composition still decides which one runs: the name below
    # is the module-level `BaseAgent`, which is what a substitution replaces.
    plugin_catalog.mount(AgentPlugin(BaseAgent))
    # After the three above: it injects `memory`, `context_manager` and the rest.  The
    # built-in tool set used to be constructed here directly, which made every tool it
    # registers permanent for the life of the process.
    plugin_catalog.mount(ToolsPlugin(builtin_tools_cls))
    # Resolved, not built: the agent is a plugin's now.  This call is the composition
    # teaching it about the spawn capability, which is composition knowledge.
    require_service(components, "agent").register_spawn_capability(
        system_prompt, workspace_root=workspace_root
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


    # Compose system prompt now that plugins are loaded (they may append rules).
    system_prompt = _compose_system_prompt(
        system_prompt,
        registry,
        workspace_root,
        output_dir,
        skill_catalog=skill_catalog,
        plugin_catalog=plugin_catalog,
    )

    # Updated **in place** rather than copied into a new dict.  The plugin catalog's
    # kernel adopts this mapping as its service storage, so a copy would leave two
    # namespaces that diverge for everything added after the catalog was built:
    # ``agent``, ``system_prompt``, ``context_manager`` and ``mcp_client`` would be
    # invisible to a plugin's ``inject``, and the "one namespace" the container is
    # for would be true only of the entries that happened to exist earlier.
    _partial_components.update({
        "base_system_prompt": base_system_prompt,
        "system_prompt": system_prompt,
        "plugin_catalog": plugin_catalog,
        "mcp_client": mcp_client,
        "mcp_status": mcp_status,
        "mcp_task": None,
        "config_revision": 0,
    })
    # The values above were written straight into the mapping, and a direct write runs no
    # ``setup`` -- so nothing re-checked the plugins that were waiting for them.  Measured:
    # with the `TurnRunner` mount below removed, `ralph` stayed pending forever while
    # `system_prompt` had been present the whole time, reported as "waiting for: ()".
    #
    # So the composition says when it has finished writing.
    plugin_catalog.flush_pending()

    # The agent loop is mounted like any other plugin, into the composition's own
    # host, so it activates only once ``agent`` and ``system_prompt`` exist -- which
    # is why this is here rather than beside the catalog.  Mounting it means a
    # composition replaces the loop by providing ``agent_loop`` itself, and a
    # composition with no agent has no loop rather than one that fails on the first
    # turn.
    plugin_catalog.mount(TurnRunner())
    # WebChannel uses this hook to provision one independent agent home per
    # browser session under ``~/.agent/web/sessions/<session-id>``.
    async def _session_components_factory(session_id: str) -> dict:
        # Resolve the global config at session-runtime creation time so config
        # changes apply to subsequent turns without interrupting active ones.
        global_cfg, _ = agent_module.load_config()
        return await _build_web_session_components(
            session_id,
            global_cfg or cfg,
            components,
            provider_client_cache=provider_client_cache,
        )

    components["session_components_factory"] = _session_components_factory
    components["session_store_factory"] = lambda _session_id: require_service(
        components, "ltm_store"
    )
    components["use_memory_worker_pool"] = True

    # Ralph is mounted as a plugin rather than built here.  It was already a *service*
    # -- ``commands/builtin.py`` resolves ``ralph_service`` -- so what this changes is
    # that a composition can choose not to have one, and that its dependencies are
    # declared rather than closed over.
    components["agent_core"] = AgentCore(components)
    # One constructor for all four former sites; see agent/commands/composition.py.
    command_router = ensure_command_router(components)

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
    # Named dependencies rather than the whole ``components`` dict.  The
    # container made every service boundary optional on the tool side: a reader
    # could reach around whatever it had been given, and two of them did.  Only
    # four keys were ever read through it, and they are these.
    registry.set_context("command_router", command_router)
    registry.set_context("skill_catalog", skill_catalog)
    registry.set_context("registry", registry)
    if mcp_client is not None:
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

    # The composition says what did not load -- at the end, not in the middle.
    #
    # Placed here rather than beside the mounts because a plugin can be *transiently*
    # pending: `system_prompt` is written into the mapping below the mounts, so a
    # report taken just after them announced 'ralph is waiting for: system_prompt'
    # for a plugin that was loaded by the time the composition returned.  A warning
    # that is usually wrong is one people learn to skip.
    #
    # Both halves were computed and neither was shown: `pending_report()` had no caller at
    # all, and a plugin refused by a raising `setup` was in neither list -- its only trace
    # was a log line, which is not something an operator reads or a caller can assert on.
    # Printed here rather than inside the catalog because the catalog does not own the
    # console, and recorded in the mapping so the gateway and the CLI can surface it where
    # the user actually is.
    plugin_failures = plugin_catalog.failed_report()
    plugin_pending = plugin_catalog.pending_report()
    components["plugin_failures"] = plugin_failures
    components["plugin_pending"] = plugin_pending
    for plugin_name, error in sorted(plugin_failures.items()):
        shared.CONSOLE.print(
            f"[red]Plugin '{plugin_name}' failed to load: {error}[/red]"
        )
    for plugin_name, missing in sorted(plugin_pending.items()):
        shared.CONSOLE.print(
            f"[yellow]Plugin '{plugin_name}' is waiting for: "
            f"{', '.join(missing)}[/yellow]"
        )

    return components


def _build_components(cfg: dict):
    """Synchronous compatibility wrapper for commands that do not need async setup."""
    return asyncio.run(_build_components_async(cfg))
