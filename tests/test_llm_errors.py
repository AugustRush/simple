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
    # ``_merge_continuation_text`` used to be checked here too.  It is gone rather
    # than kept in agreement: it only delegated, and its caller now calls the module
    # function directly.  One fewer delegation is the direction this refactor moves,
    # so the assertion goes with it instead of being pointed at a method that should
    # not come back.


def _fake_response(status_code: int):
    """The minimum ``httpx.Response`` shape the SDK constructors need."""
    import httpx

    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))


def test_the_funds_vocabulary_a_provider_actually_uses_is_recognised() -> None:
    """The phrasing that prompted this, plus the neighbours it generalises to.

    ``opencode.ai`` answers a spent account with ``402 {'error': {'message':
    'Upstream request failed: Insufficient account funds'}}`` -- an *account* problem, not a
    slow request.  It was not in the list, and the list is read by three decisions: the LLM
    retry loop (never replay it), the scheduler (never schedule a retry), and the agent's
    formatter (say "switch provider or top up" instead of quoting the body).  All three were
    getting the wrong answer for this provider.
    """
    for text in (
        "Upstream request failed: Insufficient account funds",
        "402 Payment Required",
        "you are out of credits",
        "Insufficient funds",
    ):
        assert is_account_exhausted(Exception(text)) is True, text
        assert is_llm_retryable(Exception(text)) is False, text


def test_the_word_insufficient_alone_is_not_an_account_problem() -> None:
    """Control: the markers are about money, not about the word.

    A list grown by matching "insufficient" would also swallow permissions errors, and a
    swallowed one is a permanent failure that no retry can fix -- the same mistake in the
    other direction.
    """
    assert is_account_exhausted(Exception("insufficient permissions")) is False
    assert is_account_exhausted(Exception("insufficient context window")) is False
    assert is_llm_retryable(Exception("insufficient permissions")) is False, (
        "and it is not retryable either -- it is simply not an account problem"
    )


def _openai_status_error(body: dict):
    """A real SDK error object, built the way the SDK builds one.

    Constructed rather than hand-rolled: the predicate's job is to read what the *client*
    carries, so a ``SimpleNamespace(code=...)`` would test the test instead of the integration.
    """
    import httpx
    import openai

    return openai.BadRequestError(
        f"Error code: 400 - {body}",
        response=httpx.Response(
            400, request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions")
        ),
        body=body,
    )


def test_a_policy_rejection_is_recognised_by_the_providers_own_code() -> None:
    """The measured case the phrase list missed.

    The message an OpenAI-compatible endpoint sends for a safety rejection --
    "Your request was rejected as a result of our safety system." -- contains neither
    "content exists risk" nor "content filter", so ``str(exc)`` alone never matched it and the
    recovery path never ran for the rejection it was written for.  The provider's own code does
    match, and it is the one signal that survives a rewording.
    """
    from agent.core.llm_errors import is_content_filter_block

    body = {
        "message": "Your request was rejected as a result of our safety system.",
        "type": "invalid_request_error",
        "code": "content_policy_violation",
    }
    error = _openai_status_error(body)

    assert "content filter" not in str(error).lower(), (
        "the point of the test is that the prose carries no match"
    )
    assert getattr(error, "code", None) == "content_policy_violation"
    assert is_content_filter_block(error) is True


def test_an_unparsed_envelope_is_read_one_level_deep() -> None:
    """An SDK that hands the envelope through: ``{"error": {"code": ...}}``."""
    from agent.core.llm_errors import _provider_reason_codes

    class _Unparsed(Exception):
        body = {"error": {"type": "invalid_request_error", "code": "content_filter"}}

    assert "content_filter" in _provider_reason_codes(_Unparsed("boom"))


@pytest.mark.parametrize(
    "body",
    [
        # The generic 400 type must never be a policy signal, however often it appears.
        {"message": "max_tokens is too large", "type": "invalid_request_error"},
        # Prose that mentions filtering without being about content policy.
        {"message": "response filtering is not supported by this model",
         "code": "unsupported_parameter"},
        # A code the predicate does not know, on a body it can read.
        {"message": "model not found", "type": "invalid_request_error",
         "code": "model_not_found"},
    ],
)
def test_a_provider_error_that_is_not_a_policy_block_stays_unmatched(body: dict) -> None:
    """Control: reading codes must not become "any 400 is a policy block".

    A false positive here is expensive in the other direction: the recovery rolls the last
    round out of the conversation and re-asks, so it would repair a request that was never
    rejected on policy grounds.
    """
    from agent.core.llm_errors import is_content_filter_block

    assert is_content_filter_block(_openai_status_error(body)) is False
