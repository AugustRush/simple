from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from rich.console import Console

from agent.core.output import CliOutputSink, OutputSink, _active_sink


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
        cfg = self._cfg
        ctx_mgr = components.get("context_manager")
        memory_worker = (
            agent_module.BackgroundMemoryWorker(
                ctx_mgr,
                components["client"],
                components["model"],
                components["agent"].api_format,
                client_factory=lambda: agent_module.ModelClientFactory.from_config(
                    cfg, announce=False
                )[0],
            )
            if ctx_mgr
            else None
        )
        if memory_worker:
            memory_worker.start()

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
            if memory_worker:
                memory_worker.stop()
                await memory_worker.wait()
            if plugin_catalog:
                try:
                    all_messages: list[dict] = []
                    all_tools: list[str] = []
                    total_turns = 0
                    for session in sessions.values():
                        all_messages.extend(
                            session.get("ctx", agent_module.AgentContext("")).messages
                        )
                        all_tools.extend(session.get("tools_used", []))
                        total_turns += session.get("turn_count", 0)
                    if total_turns > 0:
                        await plugin_catalog.fire_session_end(
                            agent_module.SessionEvent(
                                messages=all_messages,
                                tools_used=all_tools,
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                turn_count=total_turns,
                            )
                        )
                except Exception as exc:
                    agent_module.CONSOLE.print(
                        f"[dim]Plugin session_end error: {exc}[/dim]"
                    )

    def _make_message_handler(
        self, sessions: dict
    ) -> Callable[["IncomingMessage", OutputSink], Any]:
        components = self._components

        async def _handle(msg: IncomingMessage, sink: OutputSink) -> bool:
            import agent as agent_module

            session_id = msg.metadata.get("chat_id") or msg.session_id
            agent = components["agent"]
            skill_catalog = components["skill_catalog"]
            plugin_catalog = components.get("plugin_catalog")
            ctx_mgr = components.get("context_manager")

            if session_id not in sessions:
                sessions[session_id] = {
                    "ctx": agent_module.AgentContext(
                        system_prompt=components["system_prompt"]
                    ),
                    "tools_used": [],
                    "turn_count": 0,
                    "task_context": "",
                }
            state = sessions[session_id]
            ctx = state["ctx"]
            ctx.metadata["skill_catalog"] = skill_catalog

            if not state["task_context"]:
                state["task_context"] = msg.text[:300]

            if ctx_mgr:
                ctx_mgr.mark_activity()

            token = _active_sink.set(sink)
            try:
                result = await agent.send_message(
                    ctx, msg.text, stream_callback=sink.sync_stream_cb
                )
                sink.on_turn_complete(result.content or "", result.tool_calls_made)
                if hasattr(sink, "drain"):
                    await sink.drain()

                if result.error:
                    sink.on_error(result.error)
                    if hasattr(sink, "drain"):
                        await sink.drain()

                state["tools_used"].extend(result.tool_calls_made)
                state["turn_count"] += 1

                if plugin_catalog:
                    await plugin_catalog.fire_turn_end(
                        agent_module.TurnEvent(
                            user_input=msg.text,
                            agent_response=result.content or "",
                            tool_calls=result.tool_calls_made,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            turn_index=state["turn_count"],
                        )
                    )

                if ctx_mgr:
                    ctx_mgr.staging.append("user", msg.text)
                    if result.content:
                        ctx_mgr.staging.append("assistant", result.content)
                    if ctx_mgr.should_enqueue_consolidation():
                        ctx_mgr.enqueue_consolidation("staged_turns")

                if ctx_mgr and ctx_mgr.should_compact_messages(
                    ctx.messages, agent.max_tokens
                ):
                    ctx.messages = ctx_mgr.compact_messages(ctx.messages)
                    ctx.system_prompt = agent_module._with_task_context(
                        components["system_prompt"], state["task_context"]
                    )
                    if ctx_mgr.staging.count() >= ctx_mgr.min_messages:
                        ctx_mgr.enqueue_consolidation("compact_triggered")

            except Exception as exc:
                sink.on_error(str(exc))
                if hasattr(sink, "drain"):
                    await sink.drain()
            finally:
                _active_sink.reset(token)

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
