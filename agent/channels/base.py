from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from agent import shared
from agent.shared import CancelToken
from agent.commands import (
    CommandCoordinator,
    CommandRouter,
    register_builtin_commands,
)
from agent.core.output import CliOutputSink, OutputSink
from agent.core.attachments import MessageAttachment
from agent.runtime import (
    AgentCore,
    RuntimeComponents,
    RuntimeEvent,
    RuntimeSessionState,
    TurnInput,
)

logger = logging.getLogger(__name__)


def _trace_latency(stage: str, **fields: object) -> None:
    shared._trace_latency("channel_runner", stage, **fields)


def _preview_text(text: object, limit: int = 80) -> str:
    return shared._preview_text(text, limit=limit)


def _interaction_log(event: str, **fields: object) -> None:
    shared._interaction_log("channel_runner", event, **fields)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class IncomingMessage:
    """Normalised message arriving on any channel."""

    text: str
    session_id: str = field(default_factory=_new_id)
    channel_name: str = "cli"
    metadata: dict = field(default_factory=dict)
    attachments: tuple[MessageAttachment, ...] = ()

    def __post_init__(self) -> None:
        self.attachments = tuple(self.attachments)


class Channel(ABC):
    """Transport abstraction for one conversation pathway."""

    @abstractmethod
    async def start(
        self,
        handler: Callable[["IncomingMessage", OutputSink], Any],
    ) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def create_sink(self, msg: "IncomingMessage") -> OutputSink: ...


class CliChannel(Channel):
    """CLI stdin/stdout channel (Rich Prompt + Console)."""

    def __init__(self, console: Console) -> None:
        self._console = console

    async def start(
        self,
        handler: Callable[["IncomingMessage", OutputSink], Any],
    ) -> None:
        raise NotImplementedError(
            "CliChannel.start() is not yet wired into ChannelRunner. "
            "ChannelRunner routes the CLI channel through _interactive_loop "
            "directly.  Refactor _interactive_loop into a stateless AgentCore "
            "handler to complete this abstraction."
        )

    async def stop(self) -> None:
        pass

    def create_sink(self, msg: "IncomingMessage") -> CliOutputSink:
        return CliOutputSink(self._console)


class ChannelRunner:
    """Manages concurrent startup/teardown of one or more channels."""

    def __init__(
        self,
        channels: list[Channel],
        components: dict,
        cfg: dict,
    ) -> None:
        self._channels = channels
        self._components = components
        self._cfg = cfg

    def _build_session_context_manager(self, session_id: str, components: dict):
        base_ctx_mgr = components.get("context_manager")
        if base_ctx_mgr is None:
            return None
        staging = getattr(base_ctx_mgr, "staging", None)
        if str(getattr(staging, "session_id", "") or "") == str(session_id):
            return base_ctx_mgr
        spawn_session = getattr(base_ctx_mgr, "spawn_session", None)
        if callable(spawn_session):
            return spawn_session(
                session_id,
                project_scope=str(components.get("project_memory_scope") or ""),
            )
        return base_ctx_mgr

    def _build_session_memory_worker(self, session_ctx_mgr, components: dict):
        import agent as agent_module

        if session_ctx_mgr is None:
            return None
        agent = components.get("agent")
        if agent is None or not hasattr(agent, "endpoint_for"):
            return None
        cfg = components.get("cfg") or {}
        model = agent.consolidation_model(cfg)
        endpoint = agent.consolidation_endpoint(cfg)
        session_id = getattr(session_ctx_mgr.staging, "session_id", "")
        pool = components.get("memory_worker_pool")
        if pool is not None:
            pool.register(session_id, session_ctx_mgr, model, endpoint)
            handle = agent_module.PooledMemoryWorkerHandle(pool, session_id)
            handle.start()
            return handle
        worker = agent_module.BackgroundMemoryWorker(
            session_ctx_mgr, endpoint, model
        )
        worker.start()
        return worker

    @staticmethod
    def _log_runtime_event(event: RuntimeEvent) -> None:
        """Convert any RuntimeEvent into a structured interaction log.

        Every event name becomes the log ``event`` key.  Fields and metadata
        are flattened into the log payload so the event stream is the single
        source of truth — no per-event-name branching required.
        """
        payload: dict[str, object] = dict(event.fields)
        payload["session_id"] = event.session_id
        payload["channel"] = event.channel_name
        message_id = event.metadata.get("message_id")
        if message_id:
            payload["message_id"] = message_id
        _interaction_log(event.name, **{k: v for k, v in payload.items() if v is not None})

    async def _ensure_session_state(
        self,
        sessions: dict[str, RuntimeSessionState],
        session_id: str,
        components: dict,
    ) -> RuntimeSessionState:
        import agent as agent_module

        state = sessions.get(session_id)
        if state is not None:
            return state

        session_ctx_mgr = self._build_session_context_manager(session_id, components)
        initial_ctx = agent_module.AgentContext(
            system_prompt=components["system_prompt"]
        )
        if session_ctx_mgr is not None:
            load_checkpoint = getattr(
                session_ctx_mgr, "load_provider_checkpoint", None
            )
            if callable(load_checkpoint):
                try:
                    checkpoint = load_checkpoint()
                except Exception:
                    logger.exception(
                        "failed to restore provider checkpoint for session %s",
                        session_id,
                    )
                else:
                    if isinstance(checkpoint, dict):
                        messages = checkpoint.get("messages")
                        if isinstance(messages, list):
                            initial_ctx.messages = [
                                item for item in messages if isinstance(item, dict)
                            ]
                        initial_ctx.metadata["_checkpoint_summary"] = str(
                            checkpoint.get("summary") or ""
                        )
                        initial_ctx.metadata["_has_provider_checkpoint"] = True
        workspace_root = components.get("workspace_root")
        if workspace_root is not None:
            try:
                workspace_path = Path(workspace_root).expanduser().resolve(strict=False)
                initial_ctx.metadata["workspace_root"] = str(workspace_path)
                initial_ctx.metadata["workspace_status"] = (
                    "ready" if workspace_path.is_dir() else "missing"
                )
                policy = components.get("file_access_policy")
                if policy is not None:
                    initial_ctx.metadata["workspace_read"] = bool(
                        getattr(policy, "workspace_read", True)
                    )
                    initial_ctx.metadata["workspace_write"] = bool(
                        getattr(policy, "workspace_write", False)
                    )
            except (OSError, TypeError, ValueError):
                pass
        state = RuntimeSessionState(
            ctx=initial_ctx,
            context_manager=session_ctx_mgr,
            memory_worker=self._build_session_memory_worker(session_ctx_mgr, components),
            cancel_token=CancelToken(),
        )
        sessions[session_id] = state
        return state

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._run_channel(ch)) for ch in self._channels]
        try:
            await asyncio.gather(*tasks)
        finally:
            for ch in self._channels:
                try:
                    await ch.stop()
                except Exception:
                    pass
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_channel(self, channel: Channel) -> None:
        import agent as agent_module

        if isinstance(channel, CliChannel):
            await agent_module._interactive_loop(self._components, self._cfg)
            return

        components = self._components
        memory_pool = None
        if components.get("use_memory_worker_pool"):
            pool_cls = getattr(agent_module, "BackgroundMemoryWorkerPool", None)
            if pool_cls is not None:
                consolidation_cfg = (
                    components.get("cfg", {}).get("context", {}).get("consolidation", {})
                )
                memory_pool = pool_cls(
                    poll_seconds=float(consolidation_cfg.get("poll_seconds", 1.0) or 1.0),
                )
                components["memory_worker_pool"] = memory_pool
                memory_pool.start()
        plugin_catalog = components.get("plugin_catalog")
        if plugin_catalog:
            plugin_catalog.fire_session_start(components)

        set_output_dir = getattr(channel, "set_output_dir", None)
        if callable(set_output_dir):
            set_output_dir(components.get("output_dir"))

        sessions: dict[str, RuntimeSessionState] = {}
        handler = self._make_message_handler(sessions)
        bind_runtime = getattr(channel, "bind_runtime", None)
        if callable(bind_runtime):
            bind_runtime(sessions, components)

        try:
            await channel.start(handler)
        finally:
            for session in sessions.values():
                worker = session.memory_worker
                if worker is None:
                    continue
                worker.stop()
                await worker.wait()
            flush_on_end = bool(
                self._cfg.get("context", {})
                .get("consolidation", {})
                .get("flush_on_session_end", False)
            )
            agent = components.get("agent")
            flush_model = (
                agent.consolidation_model(components.get("cfg") or {})
                if flush_on_end and agent is not None
                else ""
            )
            flush_endpoint = agent.endpoint_for(flush_model) if flush_model else None
            for session in sessions.values():
                if flush_endpoint is None:
                    continue
                manager = session.context_manager
                should_flush = getattr(manager, "should_session_end_sleep", None)
                if manager is None or not callable(should_flush) or not should_flush():
                    continue
                enqueue = getattr(manager, "enqueue_consolidation", None)
                process = getattr(manager, "process_one_job", None)
                pending = getattr(manager, "pending_jobs", None)
                if not all(callable(fn) for fn in (enqueue, process, pending)):
                    continue
                flush_timeout = max(
                    1.0,
                    float(
                        self._cfg.get("memory", {}).get(
                            "session_end_flush_timeout_seconds",
                            shared.DEFAULT_SESSION_END_FLUSH_TIMEOUT_SECONDS,
                        )
                    ),
                )
                try:
                    async with asyncio.timeout(flush_timeout):
                        enqueue("session_end")
                        while pending():
                            processed = await process(flush_endpoint, flush_model)
                            if not processed:
                                break
                except TimeoutError:
                    logging.getLogger(__name__).warning(
                        "Session-end consolidation timed out; staging retained",
                        extra={"session_id": manager.staging.session_id},
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Session-end consolidation failed; staging retained",
                        extra={"session_id": manager.staging.session_id},
                    )
            if plugin_catalog:
                try:
                    for session_id, session in sessions.items():
                        turn_count = session.turn_count
                        if turn_count <= 0:
                            continue
                        await plugin_catalog.fire_session_end(
                            agent_module.SessionEvent(
                                messages=session.ctx.messages,
                                tools_used=list(session.tools_used),
                                session_id=session_id,
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                turn_count=turn_count,
                            )
                        )
                except Exception as exc:
                    agent_module.CONSOLE.print(
                        f"[dim]Plugin session_end error: {exc}[/dim]"
                    )
            for session in sessions.values():
                staging = getattr(session.context_manager, "staging", None)
                close = getattr(staging, "close", None)
                if callable(close):
                    close()
            if memory_pool is not None:
                memory_pool.stop()
                await memory_pool.wait()

    def _make_message_handler(
        self, sessions: dict[str, RuntimeSessionState]
    ) -> Callable[["IncomingMessage", OutputSink], Any]:
        import agent as agent_module

        components = self._components
        session_components: dict[str, dict] = {}
        session_revisions: dict[str, int] = {}
        session_coordinators: dict[str, CommandCoordinator] = {}
        session_plugins_started: set[str] = set()

        async def _components_for_session(session_id: str) -> dict:
            existing = session_components.get(session_id)
            revision = int(components.get("config_revision", 0) or 0)
            if existing is not None and session_revisions.get(session_id) == revision:
                return existing
            if existing is not None:
                close_components = getattr(agent_module, "_close_components", None)
                if (
                    callable(close_components)
                    and not existing.get("_shares_global_runtime")
                ):
                    await close_components(existing)
                session_coordinators.pop(session_id, None)
                session_plugins_started.discard(session_id)
            factory = components.get("session_components_factory")
            if callable(factory):
                built = factory(session_id)
                if hasattr(built, "__await__"):
                    built = await built
                if isinstance(built, dict):
                    session_components[session_id] = built
                    session_revisions[session_id] = revision
                    return built
            session_components[session_id] = components
            session_revisions[session_id] = revision
            return components

        async def _cleanup_session_runtime(session_id: str) -> None:
            runtime = session_components.pop(session_id, None)
            session_revisions.pop(session_id, None)
            session_coordinators.pop(session_id, None)
            session_plugins_started.discard(session_id)
            if (
                runtime is None
                or runtime is components
                or runtime.get("_shares_global_runtime")
            ):
                return
            close_components = getattr(agent_module, "_close_components", None)
            if callable(close_components):
                await close_components(runtime)

        async def _evict_session(session_id: str) -> None:
            state = sessions.get(session_id)
            if state is None or state.operation_state != "idle":
                return
            # The web channel processes messages concurrently, so eviction
            # must be decided atomically with the coordinator's idle->active
            # claim, which happens under the same session turn lock. The
            # lock is only ever held across synchronous state publication,
            # so acquiring it here cannot wedge eviction.
            async with state.turn_lock:
                if (
                    sessions.get(session_id) is not state
                    or state.operation_state != "idle"
                ):
                    return
                sessions.pop(session_id, None)
            worker = state.memory_worker
            if worker is not None:
                with shared._suppress_with_log("session worker stop failed"):
                    worker.stop()
                with shared._suppress_with_log("session worker wait failed"):
                    await worker.wait()
            staging = getattr(state.context_manager, "staging", None)
            close = getattr(staging, "close", None)
            if callable(close):
                with shared._suppress_with_log("session staging close failed"):
                    close()
            await _cleanup_session_runtime(session_id)

        async def _evict_idle_sessions(protected_session_id: str) -> None:
            web_cfg = self._cfg.get("channels", {}).get("web", {})
            max_active = max(1, int(web_cfg.get("max_active_sessions", 16) or 16))
            idle_ttl = max(
                1.0,
                float(web_cfg.get("session_idle_ttl_seconds", 900) or 900),
            )
            now = time.time()
            idle = sorted(
                (
                    (session_id, state)
                    for session_id, state in sessions.items()
                    if session_id != protected_session_id
                    and getattr(state, "operation_state", "idle") == "idle"
                ),
                key=lambda item: float(getattr(item[1], "last_activity", 0.0) or 0.0),
            )
            expired = [
                session_id
                for session_id, state in idle
                if now - float(getattr(state, "last_activity", 0.0) or 0.0) >= idle_ttl
            ]
            for session_id in expired:
                await _evict_session(session_id)
            remaining_idle = [
                session_id for session_id, _state in idle if session_id in sessions
            ]
            while len(sessions) >= max_active and remaining_idle:
                await _evict_session(remaining_idle.pop(0))

        components.setdefault("session_runtime_cleanup", _cleanup_session_runtime)
        agent_core = components.get("agent_core")
        if agent_core is None:
            agent_core = AgentCore(RuntimeComponents(components))
        router = components.get("command_router")
        if router is None:
            router = CommandRouter(skill_catalog=components.get("skill_catalog"))
            register_builtin_commands(router)
            plugin_catalog = components.get("plugin_catalog")
            if plugin_catalog is not None and hasattr(
                plugin_catalog, "get_slash_commands"
            ):
                router.register_plugin_catalog(plugin_catalog)
            components["command_router"] = router
        runtime_event_buffer: contextvars.ContextVar[list[RuntimeEvent] | None] = (
            contextvars.ContextVar("channel_runtime_event_buffer", default=None)
        )

        def _record_runtime_event(event: RuntimeEvent) -> None:
            current = runtime_event_buffer.get()
            if current is not None:
                current.append(event)
            self._log_runtime_event(event)
            # Runtime events are also durable session history.  The web UI
            # uses these records to restore tool traces after a restart.
            # RuntimeSessionState owns the session-scoped ContextManager whose
            # staging buffer is keyed by the Web session id. The component
            # factory's base manager has its own random staging id; using it
            # here sends tool events to an unrelated session and makes traces
            # disappear after a browser refresh.
            state = sessions.get(event.session_id)
            ctx_mgr = getattr(state, "context_manager", None)
            if ctx_mgr is None:
                runtime = session_components.get(event.session_id, components)
                ctx_mgr = (
                    runtime.get("context_manager")
                    if isinstance(runtime, dict)
                    else None
                )
            record_event = getattr(ctx_mgr, "record_runtime_event", None)
            if callable(record_event):
                metadata = dict(event.metadata or {})
                turn_id = str(
                    metadata.get("turn_id")
                    or metadata.get("message_id")
                    or ""
                )
                try:
                    record_event(event.name, dict(event.fields), turn_id=turn_id)
                except Exception:
                    logger.exception("failed to persist runtime event: %s", event.name)

        coordinator_factory = components.get("command_coordinator_factory")
        coordinator = (
            coordinator_factory(event_hook=_record_runtime_event)
            if callable(coordinator_factory)
            else CommandCoordinator(
                agent_core,
                router,
                components=components,
                config=self._cfg,
                event_hook=_record_runtime_event,
            )
        )

        async def _handle(msg: IncomingMessage, sink: OutputSink) -> bool:
            turn_started_at = time.perf_counter()
            session_id = msg.metadata.get("chat_id") or msg.session_id
            skill_catalog = components["skill_catalog"]
            session_runtime = await _components_for_session(session_id)
            state = await self._ensure_session_state(sessions, session_id, session_runtime)
            # Touch activity before evicting: a freshly created session has
            # last_activity == 0.0, and evaluating idle expiry before the
            # touch would evict the session this very message just created.
            state.last_activity = time.time()
            await _evict_idle_sessions(session_id)
            revision = int(components.get("config_revision", 0) or 0)
            if getattr(state, "runtime_revision", revision) != revision:
                old_worker = getattr(state, "memory_worker", None)
                if old_worker is not None:
                    old_worker.stop()
                    await old_worker.wait()
                state.context_manager = self._build_session_context_manager(
                    session_id, session_runtime
                )
                state.memory_worker = self._build_session_memory_worker(
                    state.context_manager, session_runtime
                )
                state.ctx.system_prompt = session_runtime["system_prompt"]
                state.runtime_revision = revision
            else:
                state.runtime_revision = revision
            ctx = state.ctx
            # Model selection is a per-turn request override.  Keep it on the
            # mutable session state so AgentCore publishes it into the request
            # context without mutating the shared BaseAgent/model used by
            # other web sessions.
            if "model_override" in msg.metadata:
                override = msg.metadata.get("model_override")
                state.model_override = (
                    override
                    if isinstance(override, str) and override.strip()
                    else None
                )
            session_skill_catalog = session_runtime.get("skill_catalog", skill_catalog)
            ctx.metadata["skill_catalog"] = session_skill_catalog
            if session_id not in session_coordinators:
                session_router = session_runtime.get("command_router")
                session_agent_core = session_runtime.get("agent_core")
                if session_router is not None and session_agent_core is not None:
                    session_coordinators[session_id] = CommandCoordinator(
                        session_agent_core,
                        session_router,
                        components=session_runtime,
                        config=session_runtime.get("cfg", self._cfg),
                        event_hook=_record_runtime_event,
                    )
                else:
                    session_coordinators[session_id] = coordinator
                plugin = session_runtime.get("plugin_catalog")
                if plugin is not None and session_id not in session_plugins_started:
                    plugin.fire_session_start(session_runtime)
                    session_plugins_started.add(session_id)
            _interaction_log(
                "turn_started",
                session_id=session_id,
                channel=msg.channel_name,
                message_id=msg.metadata.get("message_id"),
                chat_id=msg.metadata.get("chat_id"),
                text_len=len(msg.text),
                text_preview=_preview_text(msg.text),
            )
            _trace_latency(
                "message_handler_started",
                session_id=session_id,
                channel=msg.channel_name,
                message_id=msg.metadata.get("message_id"),
                chat_id=msg.metadata.get("chat_id"),
                sink=type(sink).__name__,
                text_len=len(msg.text),
            )
            current_events: list[RuntimeEvent] = []
            event_token = runtime_event_buffer.set(current_events)
            try:
                await session_coordinators[session_id].handle(
                    TurnInput.from_text(
                        msg.text,
                        session_id=session_id,
                        channel_name=msg.channel_name,
                        metadata=msg.metadata,
                        attachments=msg.attachments,
                    ),
                    state,
                    sink,
                )
            finally:
                runtime_event_buffer.reset(event_token)
                _trace_latency(
                    "message_handler_finished",
                    session_id=session_id,
                    channel=msg.channel_name,
                    message_id=msg.metadata.get("message_id"),
                    duration_ms=f"{(time.perf_counter() - turn_started_at) * 1000:.1f}",
                    turn_count=state.turn_count,
                )
            return not any(
                event.name == "prompt_blocked" for event in current_events
            )

        return _handle


def _build_gateway_channels(cfg: dict) -> list[Channel]:
    import agent as agent_module

    channels: list[Channel] = []
    feishu_cfg = cfg.get("channels", {}).get("feishu", {})
    web_cfg = cfg.get("channels", {}).get("web", {})
    if web_cfg.get("enabled"):
        try:
            from agent.channels.web import WebChannel, WebConfig  # noqa: PLC0415

            known_fields = WebConfig.__dataclass_fields__
            filtered = {k: v for k, v in web_cfg.items() if k in known_fields}
            channels.append(WebChannel(WebConfig(**filtered)))
            agent_module.CONSOLE.print("[dim]Web channel enabled[/dim]")
        except ImportError as exc:
            agent_module.CONSOLE.print(
                f"[red]Web channel requires starlette/uvicorn: {exc}[/red]"
            )
        except Exception as exc:
            agent_module.CONSOLE.print(f"[red]Web channel init failed: {exc}[/red]")
    if feishu_cfg.get("enabled"):
        try:
            from agent.channels.feishu import FeishuChannel, FeishuConfig  # noqa: PLC0415

            known_fields = FeishuConfig.__dataclass_fields__
            filtered = {k: v for k, v in feishu_cfg.items() if k in known_fields}
            channels.append(FeishuChannel(FeishuConfig(**filtered)))
            agent_module.CONSOLE.print("[dim]Feishu channel enabled[/dim]")
        except ImportError:
            agent_module.CONSOLE.print(
                f"[red]{agent_module._missing_feishu_dependency_hint()}[/red]"
            )
        except Exception as exc:
            agent_module.CONSOLE.print(f"[red]Feishu channel init failed: {exc}[/red]")

    return channels


__all__ = [
    "Channel",
    "ChannelRunner",
    "CliChannel",
    "IncomingMessage",
    "_build_gateway_channels",
]
