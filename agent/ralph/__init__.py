"""Transport-independent Ralph domain primitives."""

from .models import (
    RALPH_COMPLETION_PROMISE,
    RALPH_MAX_PROGRESS_ENTRIES,
    RalphIterationResult,
    RalphTask,
    RalphTaskStatus,
    RalphValidationError,
    VerificationResult,
    VerificationStatus,
    validate_task_id,
)
from .parser import (
    RALPH_DEFAULT_MAX_ITERATIONS,
    RALPH_MAX_ITERATIONS,
    RalphListCommand,
    RalphParseError,
    RalphParsedCommand,
    RalphResumeCommand,
    RalphStartCommand,
    parse_ralph_command,
)
from .store import (
    AmbiguousTaskIdError,
    CorruptTaskError,
    RALPH_MAX_TASK_FILE_BYTES,
    RalphStoreError,
    RalphTaskAmbiguousError,
    RalphTaskCorruptError,
    RalphTaskNotFoundError,
    RalphTaskStore,
    RalphTaskStoreIOError,
    TaskNotFoundError,
)
from .service import (
    RALPH_DIAGNOSTIC_LIMIT,
    RALPH_SUMMARY_LIMIT,
    RalphProgressEvent,
    RalphRunResult,
    RalphService,
)
from agent.verification import (
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    VERIFICATION_OUTPUT_LIMIT,
    VERIFY_ENV_ALLOWLIST,
    CommandVerifier,
)

# Historical names.  The verifier is shared with the scheduler now, so the
# neutral names are the real ones; these keep existing imports working.  They
# are assignments rather than a second class on purpose -- an alias cannot
# drift from the thing it aliases, and a subclass could.
RALPH_DEFAULT_VERIFY_TIMEOUT_SECONDS = DEFAULT_VERIFY_TIMEOUT_SECONDS
RALPH_VERIFICATION_OUTPUT_LIMIT = VERIFICATION_OUTPUT_LIMIT
RALPH_VERIFY_ENV_ALLOWLIST = VERIFY_ENV_ALLOWLIST
RalphVerifier = CommandVerifier

__all__ = [
    "RALPH_COMPLETION_PROMISE",
    "RALPH_DEFAULT_MAX_ITERATIONS",
    "RALPH_DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "RALPH_DIAGNOSTIC_LIMIT",
    "RALPH_MAX_ITERATIONS",
    "RALPH_MAX_PROGRESS_ENTRIES",
    "RALPH_MAX_TASK_FILE_BYTES",
    "RALPH_SUMMARY_LIMIT",
    "RALPH_VERIFICATION_OUTPUT_LIMIT",
    "RALPH_VERIFY_ENV_ALLOWLIST",
    "AmbiguousTaskIdError",
    "CorruptTaskError",
    "RalphIterationResult",
    "RalphListCommand",
    "RalphParseError",
    "RalphProgressEvent",
    "RalphParsedCommand",
    "RalphResumeCommand",
    "RalphStartCommand",
    "RalphRunResult",
    "RalphService",
    "RalphStoreError",
    "RalphTask",
    "RalphTaskAmbiguousError",
    "RalphTaskCorruptError",
    "RalphTaskNotFoundError",
    "RalphTaskStore",
    "RalphTaskStoreIOError",
    "RalphTaskStatus",
    "RalphValidationError",
    "RalphVerifier",
    "VerificationResult",
    "VerificationStatus",
    "TaskNotFoundError",
    "parse_ralph_command",
    "validate_task_id",
]
