"""What a failure means, said in the terms a person uses.

Measured on this machine, on the board itself: an attention card's second line
read ``OpenDesign: connection lost mid-reply (unexpected EOF)`` for two days, and
another run read ``scheduled task timed out after 1800 seconds``. Both in a
Chinese interface, under a red dot. Neither says what happened in words a reader
can act on, and one of them is the provider's internal phrasing.

Two things the classification decides, and both cost real runs when wrong:

* **what to show** -- the label and the sentence about it;
* **whether to try again** -- `transient`, which is the only input to the grace
  retry. A wrong `True` spends an extra run; a wrong `False` loses the run that
  would have worked.
"""

from __future__ import annotations

import pytest

from agent.scheduler.diagnostics import describe_failure


def test_a_dropped_connection_is_transient_and_says_so():
    reason = describe_failure("OpenDesign: connection lost mid-reply (unexpected EOF)")

    assert reason is not None
    assert reason.label == "连接中断"
    assert reason.transient is True
    assert "重试" in reason.hint, "the hint has to say what will happen next"
    # The original is kept: a support thread needs the provider's own words.
    assert "unexpected EOF" in reason.raw


def test_a_timeout_is_transient_and_names_the_two_ways_out():
    reason = describe_failure("scheduled task timed out after 1800 seconds")

    assert reason is not None
    assert reason.label == "超时"
    assert reason.transient is True
    assert "超时调长" in reason.hint or "拆小" in reason.hint


@pytest.mark.parametrize(
    "text,label,transient",
    [
        ("provider 429", "被限流", True),
        ("Rate limit exceeded", "被限流", True),
        ("502 Bad Gateway", "服务端错误", True),
        ("upstream returned 503", "服务端错误", True),
        ("401 Unauthorized", "鉴权失败", False),
        ("invalid api key", "鉴权失败", False),
        ("400 Bad Request", "请求被拒绝", False),
        ("insufficient_quota: account is out of credit", "额度用尽", False),
    ],
)
def test_the_common_causes_are_classified(text, label, transient):
    reason = describe_failure(text)

    assert reason is not None
    assert reason.label == label, text
    assert reason.transient is transient, text


def test_an_unknown_failure_is_not_retried():
    """Retrying a cause nobody understands spends a run to learn the same thing."""
    reason = describe_failure("the widget collided with a flange at step 4")

    assert reason is not None
    assert reason.transient is False
    assert reason.label.startswith("the widget collided")
    assert reason.hint == ""


def test_no_error_is_not_a_failure():
    assert describe_failure("") is None
    assert describe_failure(None) is None
    assert describe_failure("   \n  ") is None


def test_the_summary_is_one_readable_line():
    reason = describe_failure("connection lost mid-reply")

    assert reason is not None
    line = reason.summary
    assert line.startswith("连接中断")
    assert "\n" not in line


def test_the_raw_line_is_bounded():
    reason = describe_failure("boom " + "x" * 5000)

    assert reason is not None
    assert len(reason.raw) <= 200


# ── the grace retry: a dropped connection did not spend the one attempt ──────


def _run(attempt: int = 1, policy: dict | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(
        id="run-1",
        attempt=attempt,
        config_snapshot={"retry_policy": policy} if policy else {},
    ), SimpleNamespace(retry_policy=policy or {"max_attempts": 1})


def test_a_transient_failure_gets_one_retry_even_though_the_policy_says_one_attempt():
    """`max_attempts: 1` is about the work; a dropped connection never did it.

    Every task on this machine is `max_attempts: 1`, and two of them died on
    "connection lost mid-reply" -- work a single retry would almost certainly
    have completed. A policy that means "one attempt" must not also mean "one
    packet".
    """
    from datetime import datetime, timezone

    from agent.scheduler.runtime import automatic_retry_at

    run, task = _run()
    when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    retry = automatic_retry_at(task, run, when, "connection lost mid-reply")

    assert retry is not None
    assert retry > when, "the retry has to be in the future, not a busy loop"


def test_a_semantic_failure_is_not_retried():
    """The work ran and was wrong: a second try would repeat it."""
    from datetime import datetime, timezone

    from agent.scheduler.runtime import automatic_retry_at

    run, task = _run()
    when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    assert automatic_retry_at(task, run, when, "acceptance failed: npm test exited 1") is None


def test_the_grace_retry_happens_once_and_only_from_the_first_attempt():
    from datetime import datetime, timezone

    from agent.scheduler.runtime import automatic_retry_at

    when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    second_run, task = _run(attempt=2)

    assert automatic_retry_at(task, second_run, when, "connection lost") is None


def test_a_policy_that_asks_for_more_attempts_still_works():
    from datetime import datetime, timezone

    from agent.scheduler.runtime import automatic_retry_at

    when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    run, task = _run(attempt=1, policy={"max_attempts": 3, "backoff_seconds": 10})

    assert automatic_retry_at(task, run, when, "acceptance failed") is not None


def test_an_exhausted_account_is_not_retried_even_when_transient_looking():
    """A provider with no quota cannot serve the retry either."""
    from datetime import datetime, timezone

    from agent.scheduler.runtime import automatic_retry_at

    when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    run, task = _run()

    assert automatic_retry_at(task, run, when, "429 account quota exceeded") is None
