from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, TypeVar, overload

from agent.core.attachments import MessageAttachment

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
    ) -> None:
        import agent as agent_module

        tool_calls = list(result.tool_calls)
        state.record_turn(tool_calls)

        plugin_catalog = self._components.values.get("plugin_catalog")
        if plugin_catalog:
            await plugin_catalog.fire_turn_end(
                agent_module.TurnEvent(
                    user_input=turn_input.text,
                    agent_response=result.text or "",
                    tool_calls=tool_calls,
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
