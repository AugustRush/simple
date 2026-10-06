"""LLM error classification, testable without an agent.

These predicates used to be static and class methods on ``BaseAgent``, so
``tests/test_llm_retry_policy.py`` built a full ``BaseAgent(object(),
ToolRegistry(), ...)`` in nine test functions to ask questions that touch no
instance state.  The classification now lives in
:mod:`agent.core.llm_errors` as pure functions, which is what makes the tests
below possible: no agent, no registry, no model client.

The agent keeps thin delegating methods, so the existing tests that call
``agent._is_llm_retryable(...)`` still pass unchanged -- that compatibility is
asserted here too, because a delegation that silently stopped delegating is
exactly the kind of split this refactor keeps finding.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.llm_errors import (
    is_account_exhausted,
    is_content_filter_block,
    is_llm_retryable,
    merge_continuation_text,
    retryable_llm_classes,
)


# ── Retryable classes ────────────────────────────────────────────────────────


def test_builtin_error_classes_are_always_retryable() -> None:
    classes = retryable_llm_classes()
    assert asyncio.TimeoutError in classes
    assert TimeoutError in classes
    assert ConnectionError in classes


def test_the_class_tuple_is_cached() -> None:
    """Built once: the answer depends on installed SDKs, not on the caller."""
    assert retryable_llm_classes() is retryable_llm_classes()


def test_sdk_classes_are_included_when_the_sdk_is_installed() -> None:
    """anthropic and openai are dependencies of this project, so both appear."""
    import anthropic
    import openai

    classes = retryable_llm_classes()
    for module in (anthropic, openai):
        for name in (
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "InternalServerError",
        ):
            assert getattr(module, name) in classes, name


# ── Account exhaustion is checked before class-based retryability ────────────


def test_an_exhausted_account_is_not_retryable_even_as_a_rate_limit() -> None:
    """The ordering that is the point of the function.

    A spent quota arrives as ``RateLimitError``: the class says "throttled",
    the body says "this account is done until tomorrow".  If the class branch
    ran first it would be retried with backoff on a window closed for hours.
    """
    import openai

    quota = openai.RateLimitError(
        "Error code: 429 - insufficient_quota",
        response=_fake_response(429),
        body={"error": {"code": "insufficient_quota", "message": "quota exceeded"}},
    )
    assert isinstance(quota, openai.RateLimitError), "the premise"
    assert is_account_exhausted(quota) is True
    assert is_llm_retryable(quota) is False


def test_an_ordinary_rate_limit_is_still_retryable() -> None:
    """Control: narrowing the budget must not disable it for transient errors."""
    import openai

    throttled = openai.RateLimitError(
        "Error code: 429 - too many requests",
        response=_fake_response(429),
        body={"error": {"message": "rate limit reached, please retry"}},
    )
    assert is_account_exhausted(throttled) is False
    assert is_llm_retryable(throttled) is True


def test_a_connection_blip_is_retryable() -> None:
    import openai

    assert is_llm_retryable(openai.APIConnectionError(request=None)) is True  # type: ignore[arg-type]


def test_account_denials_are_recognised_across_providers() -> None:
    """Four spellings, one state, none of them inheriting ``RateLimitError``."""
    for text in (
        "Arrearage",
        "INSUFFICIENT_BALANCE",
        "Your credit balance is too low to access the Anthropic API.",
        "insufficient_quota",
    ):
        assert is_account_exhausted(Exception(text)) is True, text


# ── Token fallback for providers that are not the SDKs ───────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429 from gateway",
        " 500 Internal Server Error",
        " 502 Bad Gateway",
        " 503 Service Temporarily Unavailable",
        " 504 Gateway Timeout",
        "rate limit exceeded",
        "too many requests, slow down",
        "service unavailable",
        "the model is overloaded",
    ],
)
def test_unambiguous_http_tokens_are_retryable(message: str) -> None:
    assert is_llm_retryable(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "invalid api key",
        "model not found",
        "context length exceeded",
        "unsupported parameter",
    ],
)
def test_ambiguous_or_permanent_errors_are_not_retryable(message: str) -> None:
    """``timeout`` and ``connection`` are deliberately absent from the token list.

    They appear inside permanent messages too, so matching them would retry
    things that cannot succeed.
    """
    assert is_llm_retryable(Exception(message)) is False


# ── Content filter ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    ["Content Exists Risk", "content filter triggered", "CONTENT EXISTS RISK"],
)
def test_content_filter_blocks_are_recognised(message: str) -> None:
    assert is_content_filter_block(Exception(message)) is True


def test_an_unrelated_error_is_not_a_content_filter_block() -> None:
    assert is_content_filter_block(Exception("rate limit exceeded")) is False


# ── Continuation merging ─────────────────────────────────────────────────────


def test_an_empty_prefix_yields_the_continuation() -> None:
    assert merge_continuation_text("", "hello") == "hello"


def test_an_empty_continuation_yields_the_prefix() -> None:
    assert merge_continuation_text("hello", "") == "hello"


def test_a_duplicated_overlap_is_trimmed_once() -> None:
    """A provider restating the tail must not duplicate it."""
    assert merge_continuation_text("the quick brown", "brown fox") == "the quick brown fox"


def test_the_longest_overlap_wins() -> None:
    assert merge_continuation_text("abcabc", "abcX") == "abcabcX"


def test_disjoint_text_is_concatenated() -> None:
    assert merge_continuation_text("abc", "xyz") == "abcxyz"


def test_the_overlap_search_is_bounded() -> None:
    """The search caps at 64 characters, so the restatement is only partly trimmed.

    A 100-character prefix and a 65-character restatement: the true overlap is
    65, which is out of reach, and the loop settles for the 64 it can see --
    leaving one duplicated ``x`` in the result.  Documented rather than fixed:
    the bound is what keeps this cheap, and replacing it with a general diff is
    a different function than the truncation-recovery path needs.
    """
    prefix = "x" * 100
    continuation = "x" * 65 + "y"
    merged = merge_continuation_text(prefix, continuation)
    assert merged == prefix + "xy"
    assert len(merged) == 102


# ── The agent's delegating methods still delegate ────────────────────────────


def test_the_agent_methods_agree_with_the_functions() -> None:
    """A delegation that stopped delegating is the failure mode to catch."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    throttled = Exception(" 429 rate limit")
    assert agent._is_llm_retryable(throttled) is is_llm_retryable(throttled)
    assert agent._is_account_exhausted(throttled) is is_account_exhausted(throttled)
    assert agent._is_content_filter_block(throttled) is is_content_filter_block(throttled)
    assert agent._retryable_llm_classes() is retryable_llm_classes()
    assert agent._merge_continuation_text("a", "ab") == merge_continuation_text("a", "ab")


def _fake_response(status_code: int):
    """The minimum ``httpx.Response`` shape the SDK constructors need."""
    import httpx

    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))
