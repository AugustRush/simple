from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, TypeVar, overload

from agent.core.attachments import MessageAttachment
from agent.core.output import _active_sink
from agent.skills.catalog import prepare_user_message_for_skills
from agent.tools.runtime import _active_schedule_target

T = TypeVar("T")


@dataclass(frozen=True)
class TurnInput:
    """Normalized input for one agent turn.

    This deliberately stays transport-neutral so CLI, channels, and future
    gateways can enter the same runtime boundary.
    """

    text: str
    session_id: str = "default"
    channel_name: str = "cli"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attachments: tuple[MessageAttachment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "attachments", tuple(self.attachments))

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        session_id: str = "default",
        channel_name: str = "cli",
        metadata: Mapping[str, Any] | None = None,
        attachments: list[MessageAttachment] | tuple[MessageAttachment, ...] = (),
    ) -> "TurnInput":
        return cls(
            text=text,
            session_id=session_id,
            channel_name=channel_name,
            metadata=metadata or {},
            attachments=tuple(attachments),
        )


@dataclass(frozen=True)
class TurnResult:
    """Transport-neutral result for one completed agent turn."""

    text: str
    tool_calls: tuple[str, ...] = ()
    agent_id: str = ""
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def record_tool_use(self, tool_name: str) -> "TurnResult":
        return replace(self, tool_calls=(*self.tool_calls, tool_name))

    @classmethod
    def from_agent_result(cls, result: Any) -> "TurnResult":
        return cls(
            text=getattr(result, "content", None) or "",
            tool_calls=tuple(getattr(result, "tool_calls_made", ()) or ()),
            agent_id=getattr(result, "agent_id", "") or "",
            error=getattr(result, "error", None),
        )


@dataclass(frozen=True)
class RuntimeEvent:
    """Canonical lifecycle fact emitted by runtime services."""

    name: str
    session_id: str
    channel_name: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class RuntimeComponents:
    """Typed access wrapper for bootstrapped runtime dependencies."""

    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", self.values)

    @overload
    def require(self, name: str) -> Any: ...

    @overload
    def require(self, name: str, expected_type: type[T]) -> T: ...

    def require(self, name: str, expected_type: type[T] | None = None) -> Any:
        try:
            value = self.values[name]
        except KeyError as exc:
            raise KeyError(f"missing runtime component: {name}") from exc
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"runtime component {name!r} must be "
                f"{expected_type.__name__}, got {type(value).__name__}"
            )
        return value


@dataclass
class RuntimeSessionState:
    """Mutable per-session state shared by turn-oriented runners."""

    ctx: Any
    tools_used: list[str] = field(default_factory=list)
    turn_count: int = 0
    task_context: str = ""
    context_manager: Any = None
    memory_worker: Any = None

    def ensure_task_context(self, text: str) -> None:
        if not self.task_context:
            self.task_context = text[:300]

    def record_turn(self, tool_calls: list[str]) -> None:
        self.tools_used.extend(tool_calls)
        self.turn_count += 1


class TurnRunner:
    """Executes one normalized turn through the current agent implementation."""

    def __init__(self, components: RuntimeComponents | Mapping[str, Any]) -> None:
        if isinstance(components, RuntimeComponents):
            self._components = components
        else:
            self._components = RuntimeComponents(components)

    async def run(
        self,
        turn_input: TurnInput,
        ctx: Any,
        *,
        stream_callback: Callable[[str], None] | None = None,
    ) -> TurnResult:
        agent = self._components.require("agent")
        kwargs: dict[str, Any] = {"stream_callback": stream_callback}
        if turn_input.attachments:
            kwargs["attachments"] = turn_input.attachments
        result = await agent.send_message(
            ctx,
            turn_input.text,
            **kwargs,
        )
        return TurnResult.from_agent_result(result)

    async def complete_turn(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        result: TurnResult,
    ) -> list[Any]:
        """Finish a turn and return hook results for continue detection."""
        import agent as agent_module

        tool_calls = list(result.tool_calls)
        state.record_turn(tool_calls)

        hook_results: list[Any] = []
        plugin_catalog = self._components.values.get("plugin_catalog")
        if plugin_catalog:
            hook_results = await plugin_catalog.fire_turn_end(
                agent_module.TurnEvent(
                    user_input=turn_input.text,
                    agent_response=result.text or "",
                    tool_calls=tool_calls,
                    session_id=turn_input.session_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    turn_index=state.turn_count,
                )
            )

        maintenance = self._components.values.get("post_turn_maintenance")
        if maintenance is None:
            maintenance = agent_module.BaseAgent._post_turn_maintenance

        maintenance(
            ctx_mgr=state.context_manager,
            agent=self._components.require("agent"),
            ctx=state.ctx,
            user_content=turn_input.text,
            assistant_content=result.text or "",
            channel=turn_input.channel_name,
            record_kwargs={
                "message_id": str(turn_input.metadata.get("message_id", "")),
                "metadata": dict(turn_input.metadata),
            },
            memory_worker=state.memory_worker,
            system_prompt=self._components.require("system_prompt"),
            task_context=state.task_context,
        )
        return hook_results


@dataclass(frozen=True)
class TurnExecution:
    """Result of an application-level turn loop."""

    result: TurnResult
    iterations: int = 1
    blocked: bool = False
    block_reason: str = ""
    events: tuple[RuntimeEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))

    @property
    def failed(self) -> bool:
        return bool(self.result.error)


class AgentCore:
    """Transport-neutral application service for one or more agent turns."""

    def __init__(self, components: RuntimeComponents | Mapping[str, Any]) -> None:
        if isinstance(components, RuntimeComponents):
            self._components = components
        else:
            self._components = RuntimeComponents(components)

    def _turn_runner(self) -> Any:
        turn_runner = self._components.values.get("turn_runner")
        if turn_runner is None or isinstance(turn_runner, TurnRunner):
            return TurnRunner(self._components)
        return turn_runner

    def _skill_catalog(self) -> Any:
        return self._components.values.get("skill_catalog")

    def _plugin_catalog(self) -> Any:
        return self._components.values.get("plugin_catalog")

    @staticmethod
    def _context_metadata(state: RuntimeSessionState) -> Any:
        return getattr(state.ctx, "metadata", None)

    def _metadata_for_prompt_submit(self, turn_input: TurnInput) -> dict[str, Any]:
        metadata = dict(turn_input.metadata)
        return {
            **metadata,
            "channel": turn_input.channel_name,
            "session_id": turn_input.session_id,
        }

    def _prepare_skill_request(self, text: str, state: RuntimeSessionState) -> str:
        skill_catalog = self._skill_catalog()
        ctx_metadata = self._context_metadata(state)
        if skill_catalog is None:
            if ctx_metadata is not None:
                ctx_metadata.pop("required_skills", None)
            return text
        normalized, required_skills = prepare_user_message_for_skills(
            text,
            skill_catalog,
        )
        if required_skills:
            if ctx_metadata is not None:
                ctx_metadata["required_skills"] = required_skills
            return normalized
        if ctx_metadata is not None:
            ctx_metadata.pop("required_skills", None)
        return text

    async def _apply_prompt_submit_hooks(
        self,
        text: str,
        turn_input: TurnInput,
        sink: Any,
    ) -> tuple[bool, str]:
        plugin_catalog = self._plugin_catalog()
        if plugin_catalog is None:
            return False, text

        submit_result = await plugin_catalog.fire_prompt_submit(
            text,
            self._metadata_for_prompt_submit(turn_input),
        )
        if submit_result.action == "block":
            if sink is not None:
                sink.on_status(
                    f"Message blocked: {submit_result.message}",
                    level="warning",
                )
                await self._drain_if_supported(sink)
            return True, str(submit_result.message or "")
        if submit_result.context:
            return False, f"[{submit_result.context}]\n\n{text}"
        return False, text

    def _refresh_skill_prompt_if_needed(
        self,
        state: RuntimeSessionState,
    ) -> None:
        skill_catalog = self._skill_catalog()
        if skill_catalog is None:
            return
        consume_dirty = getattr(skill_catalog, "consume_dirty", None)
        if not callable(consume_dirty) or not consume_dirty():
            return
        import agent as agent_module

        refreshed = agent_module._compose_system_prompt(
            self._components.values.get("base_system_prompt", ""),
            self._components.values.get("registry"),
            self._components.values.get("workspace_root"),
            self._components.values.get("output_dir"),
            skill_catalog=skill_catalog,
            plugin_catalog=self._plugin_catalog(),
        )
        if isinstance(self._components.values, dict):
            self._components.values["system_prompt"] = refreshed
        state.ctx.system_prompt = agent_module._with_task_context(
            refreshed,
            state.task_context,
        )

    @staticmethod
    async def _drain_if_supported(sink: Any) -> None:
        drain = getattr(sink, "drain", None)
        if callable(drain):
            result = drain()
            if hasattr(result, "__await__"):
                await result

    def _schedule_target_for_turn(self, turn_input: TurnInput) -> dict[str, Any] | None:
        if turn_input.channel_name != "feishu":
            return None
        chat_id = turn_input.metadata.get("chat_id")
        if not chat_id:
            return None
        return {
            "delivery_mode": "channel",
            "target_type": "feishu_chat",
            "chat_id": chat_id,
            "chat_type": turn_input.metadata.get("chat_type", "p2p"),
        }

    @staticmethod
    def _runtime_event(
        name: str,
        turn_input: TurnInput,
        **fields: Any,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            name=name,
            session_id=turn_input.session_id,
            channel_name=turn_input.channel_name,
            fields=fields,
            metadata=dict(turn_input.metadata),
        )

    async def handle_turn(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        *,
        sink: Any = None,
        stream_callback: Callable[[str], None] | None = None,
        max_continuations: int = 1,
    ) -> TurnExecution:
        """Run a normalized turn through hooks, tools, memory, and continuations."""
        skill_catalog = self._skill_catalog()
        ctx_metadata = self._context_metadata(state)
        if skill_catalog is not None and ctx_metadata is not None:
            ctx_metadata["skill_catalog"] = skill_catalog
        prompt = self._prepare_skill_request(turn_input.text, state)
        prompted_input = TurnInput(
            text=prompt,
            session_id=turn_input.session_id,
            channel_name=turn_input.channel_name,
            metadata=turn_input.metadata,
            attachments=turn_input.attachments,
        )
        blocked, prompt = await self._apply_prompt_submit_hooks(
            prompt,
            prompted_input,
            sink,
        )
        if blocked:
            return TurnExecution(
                result=TurnResult(text=""),
                iterations=0,
                blocked=True,
                block_reason=prompt,
                events=(
                    self._runtime_event(
                        "prompt_blocked",
                        prompted_input,
                        reason=prompt,
                    ),
                ),
            )

        state.ensure_task_context(prompt)
        if state.context_manager:
            state.context_manager.mark_activity()

        active_sink_token = _active_sink.set(sink) if sink is not None else None
        schedule_target = self._schedule_target_for_turn(prompted_input)
        schedule_target_token = (
            _active_schedule_target.set(schedule_target)
            if schedule_target is not None
            else None
        )
        try:
            final_result = TurnResult(text="")
            iteration_prompt = prompt
            iterations = 0
            events: list[RuntimeEvent] = []
            for iteration_index in range(max(1, int(max_continuations) + 1)):
                iterations = iteration_index + 1
                state.ensure_task_context(iteration_prompt)
                self._refresh_skill_prompt_if_needed(state)
                current_input = TurnInput(
                    text=iteration_prompt,
                    session_id=turn_input.session_id,
                    channel_name=turn_input.channel_name,
                    metadata=turn_input.metadata,
                    attachments=(
                        turn_input.attachments if iteration_index == 0 else ()
                    ),
                )
                callback = (
                    stream_callback
                    if stream_callback is not None
                    else getattr(sink, "sync_stream_cb", None)
                )
                final_result = await self._turn_runner().run(
                    current_input,
                    state.ctx,
                    stream_callback=callback,
                )
                events.append(
                    self._runtime_event(
                        "agent_result_ready",
                        current_input,
                        tool_calls=len(final_result.tool_calls),
                        error=bool(final_result.error),
                        content_len=len(final_result.text or ""),
                        content_preview=(final_result.text or "")[:80],
                    )
                )
                if sink is not None:
                    sink.on_turn_complete(
                        final_result.text or "",
                        list(final_result.tool_calls),
                    )
                    if final_result.error:
                        sink.on_error(final_result.error)
                    await self._drain_if_supported(sink)
                events.append(
                    self._runtime_event(
                        "turn_response_delivered",
                        current_input,
                    )
                )
                if final_result.error:
                    events.append(
                        self._runtime_event(
                            "turn_error_reported",
                            current_input,
                            error=final_result.error,
                        )
                    )
                hook_results = await self._turn_runner().complete_turn(
                    current_input,
                    state,
                    final_result,
                )
                continued = False
                for hook_result in hook_results or []:
                    if (
                        getattr(hook_result, "action", "") == "continue"
                        and getattr(hook_result, "message", "")
                    ):
                        iteration_prompt = str(hook_result.message)
                        events.append(
                            self._runtime_event(
                                "turn_continued",
                                current_input,
                                next_prompt=iteration_prompt,
                            )
                        )
                        continued = True
                        break
                if not continued:
                    break
            return TurnExecution(
                result=final_result,
                iterations=iterations,
                events=tuple(events),
            )
        except Exception as exc:
            events.append(
                self._runtime_event(
                    "turn_failed",
                    turn_input,
                    error=str(exc),
                )
            )
            return TurnExecution(
                result=TurnResult(text="", error=str(exc)),
                iterations=iterations,
                events=tuple(events),
            )
        finally:
            if schedule_target_token is not None:
                _active_schedule_target.reset(schedule_target_token)
            if active_sink_token is not None:
                _active_sink.reset(active_sink_token)
