from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .parser import RALPH_DEFAULT_MAX_ITERATIONS, RALPH_MAX_ITERATIONS


RALPH_COMPLETION_PROMISE = "<promise>COMPLETE</promise>"
RALPH_MAX_PROGRESS_ENTRIES = 1_000
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class RalphValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RalphTaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    SETUP_ERROR = "setup_error"


def validate_task_id(task_id: str, *, label: str = "task ID") -> str:
    if not isinstance(task_id, str) or not _TASK_ID_PATTERN.fullmatch(task_id):
        raise RalphValidationError(
            "invalid_task_id",
            f"{label} must contain only letters, digits, '_' or '-' and be at most 128 characters",
        )
    return task_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain_json(value: Any, *, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise RalphValidationError(
            "invalid_json_value", f"{field_name} must contain only JSON values"
        ) from exc


def _require_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RalphValidationError("invalid_schema", f"{field_name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        try:
            status = VerificationStatus(self.status)
        except ValueError as exc:
            raise RalphValidationError(
                "invalid_verification_status", "unknown verification status"
            ) from exc
        object.__setattr__(self, "status", status)
        if self.exit_code is not None:
            _require_int(self.exit_code, field_name="exit_code")
        if self.status is VerificationStatus.PASSED and self.exit_code != 0:
            raise RalphValidationError("invalid_schema", "passed verification requires exit code 0")
        if self.status is VerificationStatus.FAILED and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise RalphValidationError(
                "invalid_schema", "failed verification requires a nonzero exit code"
            )
        for name in ("stdout_tail", "stderr_tail"):
            if not isinstance(getattr(self, name), str):
                raise RalphValidationError("invalid_schema", f"{name} must be text")
        if self.error is not None and not isinstance(self.error, str):
            raise RalphValidationError("invalid_schema", "error must be text or null")

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def infrastructure_error(self) -> bool:
        return self.status is VerificationStatus.SETUP_ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationResult":
        if not isinstance(data, Mapping):
            raise RalphValidationError("invalid_schema", "verification result must be an object")
        try:
            return cls(
                status=VerificationStatus(data["status"]),
                exit_code=data.get("exit_code"),
                stdout_tail=data.get("stdout_tail", ""),
                stderr_tail=data.get("stderr_tail", ""),
                error=data.get("error"),
            )
        except (KeyError, ValueError) as exc:
            if isinstance(exc, RalphValidationError):
                raise
            raise RalphValidationError("invalid_schema", "invalid verification result") from exc


@dataclass(frozen=True, slots=True)
class RalphIterationResult:
    iteration: int
    summary: str
    tool_calls: tuple[str, ...] = ()
    completed_by: str | None = None
    error: str | None = None
    verification: VerificationResult | None = None
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        iteration = _require_int(self.iteration, field_name="iteration")
        if iteration < 1:
            raise RalphValidationError("invalid_schema", "iteration must be positive")
        if not isinstance(self.summary, str):
            raise RalphValidationError("invalid_schema", "summary must be text")
        try:
            tool_calls = tuple(self.tool_calls)
        except TypeError as exc:
            raise RalphValidationError("invalid_schema", "tool_calls must be an array") from exc
        object.__setattr__(self, "tool_calls", tool_calls)
        if any(not isinstance(item, str) for item in self.tool_calls):
            raise RalphValidationError("invalid_schema", "tool_calls must contain text")
        for name in ("completed_by", "error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise RalphValidationError("invalid_schema", f"{name} must be text or null")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise RalphValidationError("invalid_schema", "created_at must be non-empty text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "summary": self.summary,
            "tool_calls": list(self.tool_calls),
            "completed_by": self.completed_by,
            "error": self.error,
            "verification": self.verification.to_dict() if self.verification else None,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RalphIterationResult":
        if not isinstance(data, Mapping):
            raise RalphValidationError("invalid_schema", "iteration result must be an object")
        try:
            verification_data = data.get("verification")
            return cls(
                iteration=data["iteration"],
                summary=data.get("summary", ""),
                tool_calls=tuple(data.get("tool_calls", ())),
                completed_by=data.get("completed_by"),
                error=data.get("error"),
                verification=(
                    VerificationResult.from_dict(verification_data)
                    if verification_data is not None
                    else None
                ),
                created_at=data.get("created_at") or _now_iso(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RalphValidationError):
                raise
            raise RalphValidationError("invalid_schema", "invalid iteration result") from exc


@dataclass
class RalphTask:
    """Validated Ralph state; ``current_iteration`` is the last durable attempt."""

    id: str
    goal: str
    completion_criteria: list[str] = field(default_factory=list)
    verify_command: str | None = None
    completion_promise: str = RALPH_COMPLETION_PROMISE
    max_iterations: int = RALPH_DEFAULT_MAX_ITERATIONS
    current_iteration: int = 0
    status: RalphTaskStatus = RalphTaskStatus.RUNNING
    progress: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    last_error: str | None = None
    iterations: list[RalphIterationResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_task_id(self.id)
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise RalphValidationError("invalid_goal", "goal must be non-empty text")
        if not isinstance(self.completion_criteria, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.completion_criteria
        ):
            raise RalphValidationError(
                "invalid_schema", "completion_criteria must be a list of non-empty text"
            )
        if self.verify_command is not None and (
            not isinstance(self.verify_command, str) or not self.verify_command.strip()
        ):
            raise RalphValidationError("invalid_schema", "verify_command must be text or null")
        if not isinstance(self.completion_promise, str) or not self.completion_promise:
            raise RalphValidationError("invalid_schema", "completion_promise must be non-empty text")
        maximum = _require_int(self.max_iterations, field_name="max_iterations")
        if not 1 <= maximum <= RALPH_MAX_ITERATIONS:
            raise RalphValidationError(
                "max_iterations_out_of_range",
                f"max_iterations must be between 1 and {RALPH_MAX_ITERATIONS}",
            )
        cursor = _require_int(self.current_iteration, field_name="current_iteration")
        if not 0 <= cursor <= maximum:
            raise RalphValidationError(
                "invalid_iteration", "current_iteration must be between zero and max_iterations"
            )
        try:
            self.status = RalphTaskStatus(self.status)
        except ValueError as exc:
            raise RalphValidationError("invalid_status", "unknown Ralph task status") from exc
        if not isinstance(self.progress, list) or len(self.progress) > RALPH_MAX_PROGRESS_ENTRIES:
            raise RalphValidationError("invalid_schema", "progress history is invalid or too large")
        progress = _plain_json(self.progress, field_name="progress")
        if any(not isinstance(item, dict) for item in progress):
            raise RalphValidationError("invalid_schema", "progress entries must be objects")
        self.progress = progress
        if not isinstance(self.iterations, list) or len(self.iterations) > RALPH_MAX_PROGRESS_ENTRIES:
            raise RalphValidationError("invalid_schema", "iteration history is invalid or too large")
        self.iterations = [
            item if isinstance(item, RalphIterationResult) else RalphIterationResult.from_dict(item)
            for item in self.iterations
        ]
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise RalphValidationError("invalid_schema", "last_error must be text or null")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise RalphValidationError("invalid_schema", "created_at must be non-empty text")

    def to_dict(self) -> dict[str, Any]:
        # Revalidate mutable legacy fields before they cross a persistence boundary.
        self.__post_init__()
        return {
            "id": self.id,
            "goal": self.goal,
            "completion_criteria": list(self.completion_criteria),
            "verify_command": self.verify_command,
            "completion_promise": self.completion_promise,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            "status": self.status.value,
            "progress": _plain_json(self.progress, field_name="progress"),
            "created_at": self.created_at,
            "last_error": self.last_error,
            "iterations": [item.to_dict() for item in self.iterations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RalphTask":
        if not isinstance(data, Mapping):
            raise RalphValidationError("invalid_schema", "task JSON must be an object")
        completion_criteria = data.get("completion_criteria", [])
        progress = data.get("progress", [])
        iterations = data.get("iterations", [])
        for name, value in (
            ("completion_criteria", completion_criteria),
            ("progress", progress),
            ("iterations", iterations),
        ):
            if not isinstance(value, list):
                raise RalphValidationError("invalid_schema", f"{name} must be an array")
        try:
            return cls(
                id=data["id"],
                goal=data["goal"],
                completion_criteria=list(completion_criteria),
                verify_command=data.get("verify_command"),
                completion_promise=data.get("completion_promise", RALPH_COMPLETION_PROMISE),
                max_iterations=data.get("max_iterations", RALPH_DEFAULT_MAX_ITERATIONS),
                current_iteration=data.get("current_iteration", 0),
                status=data.get("status", RalphTaskStatus.RUNNING.value),
                progress=list(progress),
                created_at=data.get("created_at") or _now_iso(),
                last_error=data.get("last_error"),
                iterations=list(iterations),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RalphValidationError):
                raise
            raise RalphValidationError("invalid_schema", "invalid Ralph task JSON") from exc


__all__ = [
    "RALPH_COMPLETION_PROMISE",
    "RALPH_MAX_PROGRESS_ENTRIES",
    "RalphIterationResult",
    "RalphTask",
    "RalphTaskStatus",
    "RalphValidationError",
    "VerificationResult",
    "VerificationStatus",
    "validate_task_id",
]
