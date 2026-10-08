"""How a model error is classified, decided without an agent.

Why this module exists
----------------------
These predicates were static and class methods on ``BaseAgent``, which meant
answering "is this error retryable?" required constructing an agent -- and the
tests did exactly that: ``tests/test_llm_retry_policy.py`` builds a full
``BaseAgent(object(), ToolRegistry(), ...)`` in nine test functions to call
methods that touch no instance state.  A classification decision that needs an
object to ask is a decision that is harder to test, reuse, and reason about
than it needs to be.

Everything here is a pure function of its argument.  The agent keeps thin
delegating methods so existing call sites and tests are unaffected, but the
logic -- and the retryability question itself -- no longer lives inside the
4700-line class.

Account exhaustion is checked first, everywhere
----------------------------------------------
A spent plan quota or an empty balance arrives as a ``RateLimitError``: the
error *class* says "throttled", the response *body* says "this account is done
until 00:03 tomorrow".  Judged by class alone it would be retried until the
budget ran out, sleeping between attempts, on a window that cannot reopen for
hours.  So :func:`is_llm_retryable` asks :func:`is_account_exhausted` before it
asks anything else, and that ordering is the whole point of the function.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from agent import shared

__all__ = [
    "is_account_exhausted",
    "is_content_filter_block",
    "is_llm_retryable",
    "merge_continuation_text",
    "retryable_llm_classes",
]

#: Lazily-built tuple of SDK error classes worth retrying.
#:
#: Module-level rather than a class attribute: the answer depends only on which
#: SDKs are importable, which is a property of the process, not of any agent.
#: ``None`` means "not built yet"; built once on first use.
_RETRYABLE_CLASSES: Optional[tuple[type[BaseException], ...]] = None

#: HTTP-level tokens for providers whose errors do not inherit from the
#: anthropic/openai SDK classes (DeepSeek, Ollama, gateways).  Ambiguous words
#: such as "timeout" and "connection" are deliberately absent: they appear
#: inside non-transient messages too.
_RETRYABLE_TOKENS: tuple[str, ...] = (
    " 429", " 500", " 502", " 503", " 504",
    "rate limit", "too many requests",
    "service unavailable", "overloaded",
)


def retryable_llm_classes() -> tuple[type[BaseException], ...]:
    """Lazy-import the SDK error classes once and cache them as a tuple."""
    global _RETRYABLE_CLASSES
    if _RETRYABLE_CLASSES is not None:
        return _RETRYABLE_CLASSES
    classes: list[type[BaseException]] = [
        asyncio.TimeoutError, TimeoutError, ConnectionError,
    ]
    for module_name, names in (
        ("anthropic", ("APIConnectionError", "APITimeoutError",
                       "RateLimitError", "InternalServerError")),
        ("openai", ("APIConnectionError", "APITimeoutError",
                    "RateLimitError", "InternalServerError")),
    ):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        for name in names:
            klass = getattr(module, name, None)
            if isinstance(klass, type) and issubclass(klass, BaseException):
                classes.append(klass)
    _RETRYABLE_CLASSES = tuple(classes)
    return _RETRYABLE_CLASSES


def is_account_exhausted(exc: BaseException) -> bool:
    """True when the provider says the *account* ran out, not this request.

    The vocabulary lives in :mod:`agent.shared` because the scheduler's retry
    decision needs the same answer from the *stored* error text; see
    :func:`agent.shared.is_account_exhausted_text`.
    """
    return shared.is_account_exhausted_text(exc)


def is_llm_retryable(exc: BaseException) -> bool:
    """True for transient errors worth another attempt.

    Primary: isinstance against known SDK error classes.  Fallback: unambiguous
    HTTP tokens for providers that do not inherit from those classes.

    Account-level exhaustion is checked *first*; see the module docstring for
    why that ordering is not an optimisation but the point.
    """
    if is_account_exhausted(exc):
        return False
    if isinstance(exc, retryable_llm_classes()):
        return True
    error_msg = str(exc).lower()
    return any(token in error_msg for token in _RETRYABLE_TOKENS)


#: Machine-readable reason codes for a policy rejection.  A *code* is the provider stating its
#: reason, so matching one cannot be the false positive a phrase can be -- the failure mode of a
#: wrong entry here is "no match", exactly like today.
#:
#: This exists because the prose list below **misses the real thing**.  Measured against the
#: error an OpenAI-compatible endpoint actually raises for a safety rejection::
#:
#:     message: "Your request was rejected as a result of our safety system."
#:     code   : "content_policy_violation"
#:     type   : "invalid_request_error"
#:
#: ``str(exc)`` contains neither phrase, so the recovery -- the only mechanism in the product
#: that *repairs* a refusal instead of reporting it -- never ran for the case it exists for.
#: ``invalid_request_error`` is deliberately not treated as a signal: it is the generic 400 type.
_POLICY_REASON_CODES = frozenset({"content_policy_violation", "content_filter"})

#: Fallback for providers that only say it in prose.
_POLICY_REASON_PHRASES = ("content exists risk", "content filter")


def _provider_reason_codes(exc: BaseException) -> list[str]:
    """The provider's own reason codes, wherever this SDK left them.

    ``exc.code``/``exc.type`` cover the OpenAI-compatible clients, which lift the inner code out
    of the envelope (measured).  The body is walked too, for an SDK that hands the envelope
    through unparsed -- one nesting level, because ``{"error": {...}}`` is the documented shape.
    Absent values contribute nothing: nothing here guesses.
    """
    found: list[str] = []

    def collect(mapping: Mapping[str, Any]) -> None:
        for key in ("code", "type"):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip().casefold())

    for source in (exc, getattr(exc, "body", None)):
        if isinstance(source, Mapping):
            collect(source)
            nested = source.get("error")
            if isinstance(nested, Mapping):
                collect(nested)
        else:
            for key in ("code", "type"):
                value = getattr(source, key, None)
                if isinstance(value, str) and value.strip():
                    found.append(value.strip().casefold())
    return found


def is_content_filter_block(exc: BaseException) -> bool:
    """True when the provider rejected the request on content policy.

    The provider's own code is checked first, then the prose: a code survives a provider
    rewording its message, which is the case that made this necessary.
    """
    if _POLICY_REASON_CODES.intersection(_provider_reason_codes(exc)):
        return True
    error_msg = str(exc).lower()
    return any(phrase in error_msg for phrase in _POLICY_REASON_PHRASES)


def merge_continuation_text(prefix: str, continuation: str) -> str:
    """Append continuation text while trimming a simple duplicated overlap.

    A provider that continues a truncated response often restates the last few
    characters, so naive concatenation duplicates them.  The search is bounded
    to 64 characters and takes the longest suffix/prefix match, which is
    cheap and covers the restatement case; it is not a general diff, and does
    not try to be.
    """
    if not prefix:
        return continuation
    if not continuation:
        return prefix
    max_overlap = min(len(prefix), len(continuation), 64)
    for size in range(max_overlap, 0, -1):
        if prefix.endswith(continuation[:size]):
            return prefix + continuation[size:]
    return prefix + continuation
