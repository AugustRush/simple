from __future__ import annotations

import shlex
from dataclasses import dataclass


RALPH_DEFAULT_MAX_ITERATIONS = 10
RALPH_MAX_ITERATIONS = 100


class RalphParseError(ValueError):
    """A stable, transport-independent Ralph command validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RalphStartCommand:
    goal: str
    max_iterations: int = RALPH_DEFAULT_MAX_ITERATIONS
    verify_command: str | None = None


@dataclass(frozen=True, slots=True)
class RalphListCommand:
    pass


@dataclass(frozen=True, slots=True)
class RalphResumeCommand:
    task_id_prefix: str


RalphParsedCommand = RalphStartCommand | RalphListCommand | RalphResumeCommand


def parse_ralph_command(text: str) -> RalphParsedCommand:
    """Parse the text following ``/ralph`` without transport dependencies."""
    if not isinstance(text, str):
        raise RalphParseError("invalid_input", "Ralph command must be text")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise RalphParseError("malformed_quotes", "Ralph command has malformed quotes") from exc

    if not tokens:
        raise RalphParseError("empty_goal", "Ralph goal cannot be empty")

    if tokens[0] == "list":
        if len(tokens) != 1:
            raise RalphParseError("unexpected_arguments", "Ralph list takes no arguments")
        return RalphListCommand()
    if tokens[0] == "resume":
        if len(tokens) != 2:
            raise RalphParseError(
                "invalid_resume", "Ralph resume requires exactly one task ID prefix"
            )
        return RalphResumeCommand(task_id_prefix=tokens[1])

    goal_tokens: list[str] = []
    max_iterations = RALPH_DEFAULT_MAX_ITERATIONS
    verify_command: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--max":
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise RalphParseError("missing_option_value", "--max requires a value")
            raw_max = tokens[index + 1]
            try:
                max_iterations = int(raw_max)
            except ValueError as exc:
                raise RalphParseError(
                    "invalid_max_iterations", "--max must be an integer"
                ) from exc
            if not 1 <= max_iterations <= RALPH_MAX_ITERATIONS:
                raise RalphParseError(
                    "max_iterations_out_of_range",
                    f"--max must be between 1 and {RALPH_MAX_ITERATIONS}",
                )
            index += 2
            continue
        if token == "--verify":
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise RalphParseError("missing_option_value", "--verify requires a value")
            verify_command = tokens[index + 1]
            if not verify_command.strip():
                raise RalphParseError("missing_option_value", "--verify requires a value")
            index += 2
            continue
        if token.startswith("-"):
            raise RalphParseError("unknown_option", f"Unknown Ralph option: {token}")
        goal_tokens.append(token)
        index += 1

    goal = " ".join(goal_tokens).strip()
    if not goal:
        raise RalphParseError("empty_goal", "Ralph goal cannot be empty")
    return RalphStartCommand(
        goal=goal,
        max_iterations=max_iterations,
        verify_command=verify_command,
    )


__all__ = [
    "RALPH_DEFAULT_MAX_ITERATIONS",
    "RALPH_MAX_ITERATIONS",
    "RalphListCommand",
    "RalphParseError",
    "RalphParsedCommand",
    "RalphResumeCommand",
    "RalphStartCommand",
    "parse_ralph_command",
]
