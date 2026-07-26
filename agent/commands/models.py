from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, Mapping, TypeAlias

CommandScope: TypeAlias = Literal["all", "cli", "feishu"]
CommandConcurrency: TypeAlias = Literal["anytime", "idle_only", "interrupt"]
CommandAction: TypeAlias = Literal["exit_cli"]
CommandLevel: TypeAlias = Literal["info", "warning", "error"]

_VALID_SCOPES = frozenset({"all", "cli", "feishu"})
_VALID_CONCURRENCY = frozenset({"anytime", "idle_only", "interrupt"})
_VALID_LEVELS = frozenset({"info", "warning", "error"})


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _freeze_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_metadata(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_metadata(item) for item in value)
    return value


def _normalize_command_token(value: str, *, label: str) -> str:
    if not value:
        raise ValueError(f"command {label} cannot be empty")
    if value.startswith("/") or any(character.isspace() for character in value):
        raise ValueError(
            f"command {label} must not start with '/' or contain whitespace"
        )
    return value.casefold()


@dataclass(frozen=True)
class CommandRequest:
    """A parsed command plus its transport identity."""

    original_text: str
    name: str
    args: str = ""
    channel_name: str = "cli"
    session_id: str = "default"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip().casefold())
        object.__setattr__(self, "args", self.args.strip())
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True)
class CommandContext:
    """Per-request dependencies supplied to a command handler."""

    components: Mapping[str, Any]
    config: Mapping[str, Any]
    session_state: Any
    sink: Any
    channel_name: str = "cli"
    session_id: str = "default"
    message_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", _immutable_mapping(self.components))
        object.__setattr__(self, "config", _immutable_mapping(self.config))
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True)
class CommandResult:
    """Structured, transport-neutral outcome of command execution."""

    handled: bool = True
    response_text: str | None = None
    attachments: tuple[Any, ...] = ()
    forward_text: str | None = None
    action: CommandAction | None = None
    level: CommandLevel = "info"
    error: str | None = None
    temporary_attachments: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        attachments = tuple(self.attachments)
        temporary_attachments = tuple(self.temporary_attachments)
        if any(item not in attachments for item in temporary_attachments):
            raise ValueError("temporary attachments must be included in attachments")
        object.__setattr__(self, "attachments", attachments)
        object.__setattr__(self, "temporary_attachments", temporary_attachments)
        if self.level not in _VALID_LEVELS:
            raise ValueError(f"invalid command result level: {self.level!r}")

    @property
    def text(self) -> str | None:
        """Concise alias for consumers that use generic result rendering."""

        return self.response_text


CommandHandler: TypeAlias = Callable[
    [CommandRequest, CommandContext], Awaitable[CommandResult]
]


@dataclass(frozen=True)
class CommandDescriptor:
    """Registration metadata and execution policy for one command."""

    name: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    usage: str = ""
    description: str = ""
    scopes: frozenset[CommandScope] = frozenset({"all"})
    concurrency: CommandConcurrency = "idle_only"
    accepts_interjections: bool = False

    def __post_init__(self) -> None:
        name = _normalize_command_token(self.name, label="name")
        aliases = tuple(
            _normalize_command_token(alias, label="alias") for alias in self.aliases
        )
        scopes = frozenset(str(scope).casefold() for scope in self.scopes)
        invalid_scopes = scopes - _VALID_SCOPES
        if invalid_scopes:
            invalid = ", ".join(sorted(invalid_scopes))
            raise ValueError(f"invalid command scope: {invalid}")
        if not scopes:
            raise ValueError("command scopes cannot be empty")
        if self.concurrency not in _VALID_CONCURRENCY:
            raise ValueError(
                f"invalid command concurrency policy: {self.concurrency!r}"
            )
        if not callable(self.handler):
            raise TypeError("command handler must be callable")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "scopes", scopes)
