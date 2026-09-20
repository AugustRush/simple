"""What "verified" means, independent of who asked.

Two callers need the same answer to the same question — *did this work meet
the bar somebody wrote down?* — and they must not be able to disagree:

- ``/ralph`` loops an agent until a command says it is done;
- a scheduled run is judged once, at the end, against its own acceptance
  criterion.

Both run a command in a workspace and read its exit code.  Neither owns that
definition, which is why it lives here rather than inside either one.
"""

from .models import (
    VERDICT_FAILED,
    VERDICT_NONE,
    VERDICT_PASSED,
    VERDICT_UNKNOWN,
    VERDICTS,
    VerificationError,
    VerificationResult,
    VerificationStatus,
    combine_verdicts,
    decode_verification,
    encode_verification,
    verdict_for,
    verification_payload,
)
from .verify import (
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    VERIFICATION_OUTPUT_LIMIT,
    VERIFY_ENV_ALLOWLIST,
    CommandVerifier,
    command_rejection_reason,
)

__all__ = [
    "DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "VERDICT_FAILED",
    "VERDICT_NONE",
    "VERDICT_PASSED",
    "VERDICT_UNKNOWN",
    "VERDICTS",
    "VERIFICATION_OUTPUT_LIMIT",
    "VERIFY_ENV_ALLOWLIST",
    "CommandVerifier",
    "VerificationError",
    "VerificationResult",
    "VerificationStatus",
    "command_rejection_reason",
    "combine_verdicts",
    "decode_verification",
    "encode_verification",
    "verdict_for",
    "verification_payload",
]
