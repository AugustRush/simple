from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from rich.console import Console

from agent import shared
from agent.core.output import CliOutputSink, OutputSink, _active_sink
from agent.runtime import RuntimeComponents, RuntimeSessionState, TurnInput, TurnRunner
from agent.tools.runtime import _active_schedule_target

logger = logging.getLogger(__name__)


def _trace_latency(stage: str, **fields: object) -> None:
    if not shared._latency_trace_enabled():
        return
    payload = shared._trace_fields(**fields)
    message = f"latency_trace component=channel_runner stage={stage}"
    if payload:
        message += f" {payload}"
    logger.warning(message)


def _preview_text(text: object, limit: int = 80) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _interaction_log(event: str, **fields: object) -> None:
    payload = shared._trace_fields(**fields)
    message = f"interaction component=channel_runner event={event}"
    if payload:
        message += f" {payload}"
    logger.info(message)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class IncomingMessage:
    """Normalised message arriving on any channel."""

    text: str
    session_id: str = field(default_factory=_new_id)
    channel_name: str = "cli"
    metadata: dict = field(default_factory=dict)


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

    def _build_session_context_manager(self, session_id: str):
        base_ctx_mgr = self._components.get("context_manager")
        if base_ctx_mgr is None:
            return None
        spawn_session = getattr(base_ctx_mgr, "spawn_session", None)
        if callable(spawn_session):
            return spawn_session(session_id)
        return base_ctx_mgr

    def _build_session_memory_worker(self, session_ctx_mgr):
        import agent as agent_module

        if session_ctx_mgr is None:
            return None
        if (
            "client" not in self._components
            or "model" not in self._components
            or "agent" not in self._components
            or not hasattr(self._components["agent"], "api_format")
        ):
            return None
        worker = agent_module.BackgroundMemoryWorker(
            session_ctx_mgr,
            self._components["client"],
            self._components["model"],
            self._components["agent"].api_format,
            client_factory=lambda: agent_module.ModelClientFactory.from_config(
                self._cfg, announce=False
            )[0],
        )
        worker.start()
        return worker

    def _ensure_session_state(
        self, sessions: dict[str, RuntimeSessionState], session_id: str
    ) -> RuntimeSessionState:
        import agent as agent_module

        state = sessions.get(session_id)
        if state is not None:
            return state

        session_ctx_mgr = self._build_session_context_manager(session_id)
        state = RuntimeSessionState(
            ctx=agent_module.AgentContext(
                system_prompt=self._components["system_prompt"]
            ),
            context_manager=session_ctx_mgr,
            memory_worker=self._build_session_memory_worker(session_ctx_mgr),
        )
        sessions[session_id] = state
        return state

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._run_channel(ch)) for ch in self._channels]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for t in tasks:
                t.cancel()
            raise

    async def _run_channel(self, channel: Channel) -> None:
        import agent as agent_module

        if isinstance(channel, CliChannel):
            await agent_module._interactive_loop(self._components, self._cfg)
            return

        components = self._components
        plugin_catalog = components.get("plugin_catalog")
        if plugin_catalog:
            plugin_catalog.fire_session_start(components)

        set_output_dir = getattr(channel, "set_output_dir", None)
        if callable(set_output_dir):
            set_output_dir(components.get("output_dir"))

        sessions: dict[str, dict] = {}

        try:
            await channel.start(self._make_message_handler(sessions))
        finally:
            for session in sessions.values():
                worker = session.memory_worker
                if worker is None:
                    continue
                worker.stop()
                await worker.wait()
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

    def _make_message_handler(
        self, sessions: dict[str, RuntimeSessionState]
    ) -> Callable[["IncomingMessage", OutputSink], Any]:
        components = self._components
        turn_runner = components.get("turn_runner")
        if turn_runner is None:
            turn_runner = TurnRunner(RuntimeComponents(components))

        async def _handle(msg: IncomingMessage, sink: OutputSink) -> bool:
            import agent as agent_module

            turn_started_at = time.perf_counter()
            session_id = msg.metadata.get("chat_id") or msg.session_id
            skill_catalog = components["skill_catalog"]
            state = self._ensure_session_state(sessions, session_id)
            ctx = state.ctx
            ctx_mgr = state.context_manager
            ctx.metadata["skill_catalog"] = skill_catalog

            state.ensure_task_context(msg.text)

            if ctx_mgr:
                ctx_mgr.mark_activity()

            token = _active_sink.set(sink)
            schedule_target_token = None
            if msg.channel_name == "feishu" and msg.metadata.get("chat_id"):
                schedule_target_token = _active_schedule_target.set(
                    {
                        "delivery_mode": "channel",
                        "target_type": "feishu_chat",
                        "chat_id": msg.metadata["chat_id"],
                        "chat_type": msg.metadata.get("chat_type", "p2p"),
                    }
                )
            try:
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
                agent_started_at = time.perf_counter()
                turn_input = TurnInput.from_text(
                    msg.text,
                    session_id=session_id,
                    channel_name=msg.channel_name,
                    metadata=msg.metadata,
                )
                result = await turn_runner.run(
                    turn_input,
                    ctx,
                    stream_callback=sink.sync_stream_cb,
                )
                tool_calls = list(result.tool_calls)
                _trace_latency(
                    "agent_send_message_finished",
                    session_id=session_id,
                    message_id=msg.metadata.get("message_id"),
                    duration_ms=f"{(time.perf_counter() - agent_started_at) * 1000:.1f}",
                    tool_calls=len(tool_calls),
                    error=bool(result.error),
                )
                _interaction_log(
                    "agent_result_ready",
                    session_id=session_id,
                    message_id=msg.metadata.get("message_id"),
                    duration_ms=f"{(time.perf_counter() - agent_started_at) * 1000:.1f}",
                    tool_calls=len(tool_calls),
                    error=bool(result.error),
                    content_len=len(result.text or ""),
                    content_preview=_preview_text(result.text or ""),
                )
                sink_started_at = time.perf_counter()
                sink.on_turn_complete(result.text or "", tool_calls)
                if hasattr(sink, "drain"):
                    await sink.drain()
                _trace_latency(
                    "sink_turn_complete_finished",
                    session_id=session_id,
                    message_id=msg.metadata.get("message_id"),
                    duration_ms=f"{(time.perf_counter() - sink_started_at) * 1000:.1f}",
                )
                _interaction_log(
                    "turn_response_delivered",
                    session_id=session_id,
                    message_id=msg.metadata.get("message_id"),
                    duration_ms=f"{(time.perf_counter() - sink_started_at) * 1000:.1f}",
                )

                if result.error:
                    error_started_at = time.perf_counter()
                    sink.on_error(result.error)
                    if hasattr(sink, "drain"):
                        await sink.drain()
                    _trace_latency(
                        "sink_error_finished",
                        session_id=session_id,
                        message_id=msg.metadata.get("message_id"),
                        duration_ms=f"{(time.perf_counter() - error_started_at) * 1000:.1f}",
                    )
                    _interaction_log(
                        "turn_error_reported",
                        session_id=session_id,
                        message_id=msg.metadata.get("message_id"),
                        duration_ms=f"{(time.perf_counter() - error_started_at) * 1000:.1f}",
                        error=result.error,
                    )

                await turn_runner.complete_turn(turn_input, state, result)

            except Exception as exc:
                _interaction_log(
                    "turn_failed",
                    session_id=session_id,
                    channel=msg.channel_name,
                    message_id=msg.metadata.get("message_id"),
                    error=str(exc),
                )
                sink.on_error(str(exc))
                if hasattr(sink, "drain"):
                    await sink.drain()
            finally:
                _trace_latency(
                    "message_handler_finished",
                    session_id=session_id,
                    channel=msg.channel_name,
                    message_id=msg.metadata.get("message_id"),
                    duration_ms=f"{(time.perf_counter() - turn_started_at) * 1000:.1f}",
                    turn_count=state.turn_count,
                )
                _active_sink.reset(token)
                if schedule_target_token is not None:
                    _active_schedule_target.reset(schedule_target_token)

            return True

        return _handle


def _build_gateway_channels(cfg: dict) -> list[Channel]:
    import agent as agent_module

    channels: list[Channel] = []
    feishu_cfg = cfg.get("channels", {}).get("feishu", {})
    if feishu_cfg.get("enabled"):
        try:
            from channels.feishu import FeishuChannel, FeishuConfig  # noqa: PLC0415

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
