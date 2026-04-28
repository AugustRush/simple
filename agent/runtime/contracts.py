from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping, TypeVar, overload

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        session_id: str = "default",
        channel_name: str = "cli",
        metadata: Mapping[str, Any] | None = None,
    ) -> "TurnInput":
        return cls(
            text=text,
            session_id=session_id,
            channel_name=channel_name,
            metadata=metadata or {},
        )


@dataclass(frozen=True)
class TurnResult:
    """Transport-neutral result for one completed agent turn."""

    text: str
    tool_calls: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def record_tool_use(self, tool_name: str) -> "TurnResult":
        return replace(self, tool_calls=(*self.tool_calls, tool_name))


@dataclass(frozen=True)
class RuntimeComponents:
    """Typed access wrapper for bootstrapped runtime dependencies."""

    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

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
