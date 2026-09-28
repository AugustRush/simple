"""Which LLM failures are worth replaying, and what the user is told.

Written after the 2026-09-27 investigation into "总是出现模型失败、链接错误等问题".
Three distinct shapes were showing up in the same session, and only one of them
is a transient error:

* a 429 whose body says the *plan's* quota is spent -- an account-level state
  whose reset is hours away, which the retry budget must not be spent on;
* a per-minute throttle, which really does clear in seconds -- this one must
  keep retrying, or the fix would trade one failure for another;
* a model id sent to an endpoint that does not serve it, which is a config
  problem and reads as a bare 400 unless something translates it.

The error bodies below are the ones that actually came back, not paraphrase:
the quota 429 is the literal text stored in this project's run history, and the
per-minute body is what the same endpoint returns for throttling.  A hand-written
`recoverable_by_agent: true` style fixture would pass on the pre-fix tree too --
the point of quoting the real bodies is that the fix cannot be satisfied by
matching something the old code already matched.
"""

from __future__ import annotations

import asyncio

import pytest


# The real 429 body, quoted from `scheduled_task_runs.error` (2026-09-23).
_QUOTA_429_BODY = {
    "error": {
        "code": "AccountQuotaExceeded",
        "message": (
            "You have exceeded the 5-hour usage quota. It will reset at "
            "2026-09-23 00:03:07 +0800 CST. We recommend upgrading your plan "
            "for more quota, or waiting for the reset. "
            "Request id: 0217900813734839436a70d8fd44afb73ab39bbb6df35ba879a2b"
        ),
        "param": "",
        "type": "TooManyRequests",
    }
}

# The same endpoint's request-level throttle: clears in seconds.
_THROTTLE_429_BODY = {
    "error": {
        "code": "RateLimitReached",
        "message": "Requests per minute exceeded, please retry later.",
        "type": "TooManyRequests",
    }
}

# OpenAI's documented 429 for a spent billing quota.  Not from this project's
# run history -- nothing here has hit an OpenAI quota yet -- but the wording is
# what OpenAI documents it sends, it rides the `RateLimitError` class like the
# zen body above, and missing it would burn the retry budget the exact same
# way.  Both spellings are pinned: the prose ("exceeded your current quota")
# and the `type` field ("insufficient_quota"), the stabler of the two.
_OPENAI_QUOTA_429_BODY = {
    "error": {
        "message": (
            "You exceeded your current quota, please check your plan and "
            "billing details. For more information on this error, read the "
            "docs: https://platform.openai.com/docs/guides/error-codes."
        ),
        "type": "insufficient_quota",
        "param": None,
        "code": "insufficient_quota",
    }
}

# Verbatim from a live probe of the deepseek endpoint with `deepseek-v4.1-flash`.
_WRONG_ENDPOINT_400_BODY = {
    "error": {
        "code": "invalid_request_error",
        "message": (
            "The supported API model names are deepseek-flash, deepseek-v4-pro, "
            "but you passed deepseek-v4.1-flash."
        ),
        "type": "invalid_request_error",
    }
}


def _status_error(status: int, body: dict):
    """Build the SDK exception a provider's HTTP response becomes in flight."""
    import httpx
    import openai

    classes = {
        400: openai.BadRequestError,
        403: openai.PermissionDeniedError,
        429: openai.RateLimitError,
    }
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(status, request=request, json=body)
    return classes[status](f"Error code: {status} - {body}", response=response, body=body)


def _agent():
    import agent as agent_module

    return agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )


def test_a_spent_plan_quota_is_not_retryable():
    """The quota 429 arrives as a `RateLimitError`, which is exactly why the
    isinstance check alone gets it wrong: the class says "throttled", the body
    says "this account is done until 00:03 tomorrow"."""
    import openai

    agent = _agent()
    quota = _status_error(429, _QUOTA_429_BODY)

    # The premise of the defect: the class the SDK picked says "throttled".
    assert isinstance(quota, openai.RateLimitError)
    assert agent._is_llm_retryable(quota) is False


def test_request_level_throttling_and_connection_blips_still_retry():
    """Control: the fix narrows the budget, it does not disable it.

    Without this, `_is_llm_retryable` returning False for everything would pass
    the test above and silently remove retries for the errors that need them.
    """
    import openai

    agent = _agent()

    assert agent._is_llm_retryable(_status_error(429, _THROTTLE_429_BODY)) is True
    assert agent._is_llm_retryable(
        openai.APIConnectionError(request=None)  # type: ignore[arg-type]
    ) is True


def test_account_level_denials_are_recognised_across_providers():
    """Four providers, four spellings, one state: the account, not the request.

    `Arrearage` is dashscope's 400 for an unpaid account, `INSUFFICIENT_BALANCE`
    is ApiKeyFun's 403, and Anthropic phrases the same thing in prose.  None of
    them inherits `RateLimitError`, so the isinstance branch never sees them --
    they are matched by body, which is why the list has to be spelled out.
    OpenAI rides the `RateLimitError` class like the zen body does, so the veto
    must fire before the isinstance branch can claim it.
    """
    import openai

    agent = _agent()

    arrearage = _status_error(
        400, {"error": {"code": "Arrearage", "message": "Access denied, please make sure your account is in good standing"}}
    )
    no_balance = _status_error(403, {"error": {"message": "INSUFFICIENT_BALANCE"}})
    anthropic_credit = Exception(
        "Error code: 400 - Your credit balance is too low to access the Anthropic API."
    )
    openai_quota = _status_error(429, _OPENAI_QUOTA_429_BODY)

    assert agent._is_account_exhausted(arrearage) is True
    assert agent._is_account_exhausted(no_balance) is True
    assert agent._is_account_exhausted(anthropic_credit) is True
    assert agent._is_account_exhausted(openai_quota) is True
    assert isinstance(openai_quota, openai.RateLimitError), (
        "the premise: the SDK class for this body says 'throttled'"
    )
    assert agent._is_llm_retryable(arrearage) is False
    assert agent._is_llm_retryable(openai_quota) is False


def test_a_spent_quota_is_not_replayed_or_slept_on(monkeypatch):
    """`_with_llm_retry` is where the cost is actually paid.

    On the pre-fix tree this spends four attempts and 1+2+4 seconds of backoff
    on a window that is closed for hours -- in an unattended run, that is a
    minute of wall clock per scheduled step for nothing.
    """
    import agent as agent_module

    agent = _agent()
    calls: list[int] = []
    waits: list[float] = []

    async def record_sleep(delay):
        waits.append(delay)

    async def spend_quota(*args, **kwargs):
        calls.append(1)
        raise _status_error(429, _QUOTA_429_BODY)

    monkeypatch.setattr(agent_module.asyncio, "sleep", record_sleep)

    with pytest.raises(Exception, match="AccountQuotaExceeded"):
        asyncio.run(agent._with_llm_retry(spend_quota))

    assert len(calls) == 1, f"replayed an unrecoverable error {len(calls)} times"
    assert waits == [], f"backed off for {waits} on a closed quota window"


def test_a_throttled_request_is_replayed_with_backoff(monkeypatch):
    """Control for the same wiring: a retryable error still gets its retries."""
    import agent as agent_module

    agent = _agent()
    calls: list[int] = []
    waits: list[float] = []

    async def record_sleep(delay):
        waits.append(delay)

    async def always_throttled(*args, **kwargs):
        calls.append(1)
        raise _status_error(429, _THROTTLE_429_BODY)

    monkeypatch.setattr(agent_module.asyncio, "sleep", record_sleep)

    with pytest.raises(Exception, match="RateLimitReached"):
        asyncio.run(agent._with_llm_retry(always_throttled))

    assert len(calls) == agent.llm_max_retries + 1
    assert waits == [1.0, 2.0, 4.0]


def test_the_quota_message_quotes_the_reset_time_the_provider_supplied():
    """The one fact the user cannot derive is when the window reopens."""
    agent = _agent()
    message = agent._format_agent_error(_status_error(429, _QUOTA_429_BODY))

    assert "2026-09-23 00:03:07 +0800 CST" in message
    assert "账户" in message
    assert "AccountQuotaExceeded" in message, "the raw body must stay visible"


def test_the_formatted_message_keeps_the_scheduler_veto_able_to_see_it():
    """The retry veto is a chain, and the formatter is its middle link.

    A failed agent run reaches the scheduler as ``RuntimeError(result.error)``
    (agent/cli.py) where ``result.error`` is this formatter's output, and
    ``SchedulerRuntime._automatic_retry_at`` re-reads that stored text with
    ``is_account_exhausted_text`` to refuse the retry.  The Chinese prose
    carries no marker, so that veto rides entirely on the ``原始错误：`` quote
    keeping the raw body in -- trim the quote and the scheduler goes silently
    back to spending a whole step per retry on a quota window that cannot
    reopen.  Nothing else fails, which is exactly why this link needs its
    own test rather than borrowing the one above.
    """
    from agent.shared import is_account_exhausted_text

    agent = _agent()
    message = agent._format_agent_error(_status_error(429, _QUOTA_429_BODY))

    assert is_account_exhausted_text(message) is True, (
        "the formatter must keep the raw body quoted: the scheduler's retry "
        "veto reads the stored (formatted) error text, not the raw exception"
    )


def test_a_throttled_request_is_still_reported_as_itself():
    """Control: the quota prose must not be pasted onto an ordinary throttle.

    A message that told every 429 to "change provider or top up" would send the
    user to the config for something that fixes itself in a minute.
    """
    agent = _agent()
    message = agent._format_agent_error(_status_error(429, _THROTTLE_429_BODY))

    assert "账户" not in message
    assert "RateLimitReached" in message


def test_an_endpoint_that_answers_with_a_web_page_names_whose_page_it_was():
    """A wall of HTML is not a diagnosis; whose page it is, is.

    Measured in this project's run history: the error field of a failed run held
    **5663 characters** of opencode's own 404 page, starting `<!DOCTYPE html>`
    and carrying `<title>Not Found | opencode</title>`.  Nothing in it says the
    request never reached an API route -- which is the entire finding, and is
    what the title gives away.  The page is quoted down to its head so the
    message can be read at all.
    """
    agent = _agent()
    page = (
        '<!DOCTYPE html><html lang="en" dir="ltr" data-locale="en"><head>'
        '<meta charset="utf-8"><title>Not Found | opencode</title>'
        '<meta name="description" content="OpenCode - The open source coding agent.">'
        '</head><body><div id="app"></div></body></html>'
    )

    message = agent._format_agent_error(Exception(page))

    assert "Not Found | opencode" in message, "which site answered is the finding"
    assert "base_url" in message
    assert len(message) < 600, "the raw page must not be pasted through"

    # Control: an ordinary error that happens to contain "<" is not a page.
    plain = agent._format_agent_error(Exception("Error code: 500 - upstream broke (a < b)"))
    assert "base_url" not in plain


def test_a_model_id_sent_to_the_wrong_endpoint_names_the_config_as_the_fix():
    """`400 - {'model': ...}` tells the user nothing actionable on its own.

    The endpoint lists the names it serves, which is the evidence that the model
    is owned by another provider in `config.json` -- but nothing in the raw 400
    says the fix is configuration rather than a retry.
    """
    agent = _agent()
    message = agent._format_agent_error(_status_error(400, _WRONG_ENDPOINT_400_BODY))

    assert "配置" in message
    assert "config.json" in message
    assert "deepseek-v4-pro" in message, "the endpoint's own list is the evidence"
