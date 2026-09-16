"""The vocabulary of a verification, and the one rule that reads it.

``VerificationStatus`` has six values and exactly one of them is a verdict
about the work.  The other five are statements about the *check* -- it was
refused, it timed out, it could not start, it was cancelled -- and a system
that folds them into "failed" ends up asserting something it never observed:
"this work is wrong" when the truth is "we never looked".

That is why :func:`verdict_for` is a function rather than a membership test.
Writing ``status != "passed"`` inline is the mistake; asking this module is
the correct answer, and there is one of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class VerificationError(ValueError):
    """A verification value that cannot be constructed as asked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class VerificationStatus(str, Enum):
    #: The command ran and exited zero.  The only value that is a verdict
    #: about the work.
    PASSED = "passed"
    #: The command ran and exited nonzero.  Also a verdict about the work.
    FAILED = "failed"
    #: Statements about the check, not about the work.
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    SETUP_ERROR = "setup_error"


#: A run whose work was judged and met the bar.
VERDICT_PASSED = "passed"
#: A run whose work was judged and did not meet the bar.
VERDICT_FAILED = "failed"
#: A run whose bar could not be evaluated.  Not a synonym for ``failed``.
VERDICT_UNKNOWN = "unknown"
#: No bar was declared, so nothing was judged.  The default for every task
#: that predates acceptance criteria.
VERDICT_NONE = ""

VERDICTS: tuple[str, ...] = (
    VERDICT_NONE,
    VERDICT_PASSED,
    VERDICT_FAILED,
    VERDICT_UNKNOWN,
)

#: The statuses that are a judgement about the work rather than about the
#: check.  Named so the distinction is a value rather than a comment.
VERIFICATION_STATUSES_ABOUT_THE_WORK: tuple[str, ...] = (
    VerificationStatus.PASSED.value,
    VerificationStatus.FAILED.value,
)


def verdict_for(status: VerificationStatus | str) -> str:
    """The verdict a verification status supports.

    Only a command that ran and returned a code says anything about the work.
    Everything else -- refused, timed out, could not start, cancelled -- is
    :data:`VERDICT_UNKNOWN`, because those outcomes leave the work unjudged.
    """
    try:
        resolved = VerificationStatus(status)
    except ValueError:
        return VERDICT_UNKNOWN
    if resolved is VerificationStatus.PASSED:
        return VERDICT_PASSED
    if resolved is VerificationStatus.FAILED:
        return VERDICT_FAILED
    return VERDICT_UNKNOWN


def combine_verdicts(*verdicts: str) -> str:
    """One verdict from several sources.

    The rule is deliberately asymmetric: **either source can fail the run, and
    both must pass for it to pass.**  A source with nothing to say is ignored;
    if every source has nothing to say the result is :data:`VERDICT_NONE`.

    This is what stops a run's own assurance from upgrading a check that could
    not run into a pass.  An agent saying "done" is not evidence that a command
    which never executed would have agreed.
    """
    known = [item for item in verdicts if item]
    if not known:
        return VERDICT_NONE
    if VERDICT_FAILED in known:
        return VERDICT_FAILED
    if VERDICT_UNKNOWN in known:
        return VERDICT_UNKNOWN
    return VERDICT_PASSED


def _require_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationError("invalid_schema", f"{field_name} must be an integer")
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
            raise VerificationError(
                "invalid_verification_status", "unknown verification status"
            ) from exc
        object.__setattr__(self, "status", status)
        if self.exit_code is not None:
            _require_int(self.exit_code, field_name="exit_code")
        if self.status is VerificationStatus.PASSED and self.exit_code != 0:
            raise VerificationError(
                "invalid_schema", "passed verification requires exit code 0"
            )
        if self.status is VerificationStatus.FAILED and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise VerificationError(
                "invalid_schema", "failed verification requires a nonzero exit code"
            )
        for name in ("stdout_tail", "stderr_tail"):
            if not isinstance(getattr(self, name), str):
                raise VerificationError("invalid_schema", f"{name} must be text")
        if self.error is not None and not isinstance(self.error, str):
            raise VerificationError("invalid_schema", "error must be text or null")

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def infrastructure_error(self) -> bool:
        return self.status is VerificationStatus.SETUP_ERROR

    @property
    def verdict(self) -> str:
        """What this result says about the work it was checking."""
        return verdict_for(self.status)

    def diagnostic(self) -> str:
        """A one-line reason, for the run's own error field.

        Returns "" when there is nothing to add -- a pass needs no excuse --
        so an ordinary run does not grow a line explaining that it was fine.
        """
        if self.status is VerificationStatus.PASSED:
            return ""
        if self.status is VerificationStatus.FAILED:
            detail = (self.stderr_tail or self.stdout_tail or "").strip()
            head = f"验收命令未通过（退出码 {self.exit_code}）"
            return f"{head}：{detail}" if detail else head
        if self.error:
            return f"验收无法判定（{self.status.value}）：{self.error}"
        return f"验收无法判定（{self.status.value}）"

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
            raise VerificationError(
                "invalid_schema", "verification result must be an object"
            )
        try:
            return cls(
                status=VerificationStatus(data["status"]),
                exit_code=data.get("exit_code"),
                stdout_tail=data.get("stdout_tail", ""),
                stderr_tail=data.get("stderr_tail", ""),
                error=data.get("error"),
            )
        except (KeyError, ValueError) as exc:
            if isinstance(exc, VerificationError):
                raise
            raise VerificationError(
                "invalid_schema", "invalid verification result"
            ) from exc


def encode_verification(result: VerificationResult | None) -> str:
    """Store a verification result as JSON, or "" when there was none."""
    if result is None:
        return ""
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


def decode_verification(raw: Any) -> VerificationResult | None:
    """Read back what :func:`encode_verification` wrote.

    Unreadable JSON returns ``None`` rather than raising.  This value rides
    beside a run that already happened; a corrupt column must not make the run
    history unreadable, and "no verification recorded" is the honest reading of
    a blob nobody can parse.
    """
    if not raw:
        return None
    if isinstance(raw, Mapping):
        try:
            return VerificationResult.from_dict(raw)
        except VerificationError:
            return None
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    try:
        return VerificationResult.from_dict(parsed)
    except VerificationError:
        return None


__all__ = [
    "VERDICT_FAILED",
    "VERDICT_NONE",
    "VERDICT_PASSED",
    "VERDICT_UNKNOWN",
    "VERDICTS",
    "VERIFICATION_STATUSES_ABOUT_THE_WORK",
    "VerificationError",
    "VerificationResult",
    "VerificationStatus",
    "combine_verdicts",
    "decode_verification",
    "encode_verification",
    "verdict_for",
]
