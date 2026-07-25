"""Transport-neutral command contracts and deterministic routing."""

from .coordinator import CommandCoordinator

from .models import (
    CommandAction,
    CommandConcurrency,
    CommandContext,
    CommandDescriptor,
    CommandHandler,
    CommandLevel,
    CommandRequest,
    CommandResult,
    CommandScope,
)
from .router import (
    ClassificationKind,
    CommandClassification,
    CommandRouter,
    CommandSource,
    parse_command,
)

__all__ = [
    "CommandAction",
    "CommandClassification",
    "CommandCoordinator",
    "CommandConcurrency",
    "CommandContext",
    "CommandDescriptor",
    "CommandHandler",
    "CommandLevel",
    "CommandRequest",
    "CommandResult",
    "CommandRouter",
    "CommandScope",
    "CommandSource",
    "ClassificationKind",
    "parse_command",
]
