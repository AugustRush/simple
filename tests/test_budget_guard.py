"""The spend guard: when a turn is allowed to call the model, and what it says.

Two things are worth pinning and neither is visible from the endpoint tests:

* **The guard is opt-in.** ``max_session_tokens = 0`` (the default, and every
  existing config) must keep behaving exactly as before -- no lookup, no
  message, no change.  A guard that treated "unset" as "zero" would stop the
  first turn of every deployment the moment this shipped.
* **A guard that cannot read is not a failure.** A service that cannot total its
  usage, or one that raises, keeps the turn running: the ceiling is a courtesy,
  and failing a user's turn because the accounting table is unreadable would
  turn a safety feature into an outage.

The end-to-end proof -- that the second model call does not happen -- lives in
``tests/test_functional_turns.py``, because it needs a real turn to be worth
anything.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.core.turn_loop import budget_stop_text


def _deps(limit: int, spent: int | None = None, *, raises: bool = False) -> SimpleNamespace:
    if spent is None:
        return SimpleNamespace(max_session_tokens=limit, session_token_spend=None)

    def reader(_ctx):
        if raises:
            raise RuntimeError("usage table is unreadable")
        return spent

    return SimpleNamespace(max_session_tokens=limit, session_token_spend=reader)


def test_under_the_limit_the_turn_continues_silently():
    assert budget_stop_text(_deps(1000, 999), SimpleNamespace(), "你好") is None


def test_at_the_limit_the_turn_stops_and_says_what_it_spent():
    text = budget_stop_text(_deps(1000, 1234), SimpleNamespace(), "你好")
    assert text is not None
    # Both numbers, because "you are over budget" without "by how much" leaves
    # the reader guessing whether raising the ceiling needs to be a big change.
    assert "1,234" in text and "1,000" in text
    assert "没有再调用模型" in text
    assert "max_session_tokens" in text, "the notice has to name the setting to change"


def test_the_notice_follows_the_turns_language():
    """The same reason cancellation is localized: the person reading it is mid-tool."""

    chinese = budget_stop_text(_deps(10, 20), SimpleNamespace(), "帮我看一下")
    english = budget_stop_text(_deps(10, 20), SimpleNamespace(), "have a look")
    assert chinese is not None and english is not None
    assert "上限" in chinese
    assert "budget" in english
    assert chinese != english


def test_no_ceiling_means_no_guard_at_all():
    """``0`` is "unset", not "zero tokens" -- the one reading that would break every deploy."""
    assert budget_stop_text(_deps(0, 10_000), SimpleNamespace(), "你好") is None
    assert budget_stop_text(_deps(-5, 10_000), SimpleNamespace(), "你好") is None


def test_a_service_without_a_reader_keeps_the_turn_running():
    assert budget_stop_text(_deps(100, spent=None), SimpleNamespace(), "你好") is None


def test_an_unreadable_total_is_not_a_failed_turn():
    """The ceiling is a courtesy; an accounting outage must not become an outage."""
    assert budget_stop_text(
        _deps(100, spent=0, raises=True), SimpleNamespace(), "你好"
    ) is None


def test_a_zero_ceiling_is_not_exhausted_at_zero_spend():
    """``spent == 0`` with a ceiling of one is *under* budget, not over it."""
    assert budget_stop_text(_deps(1, 0), SimpleNamespace(), "你好") is None
    assert budget_stop_text(_deps(1, 1), SimpleNamespace(), "你好") is not None
