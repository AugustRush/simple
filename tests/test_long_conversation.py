"""A conversation that never ends still works.

The product now has one conversation, and it is never replaced. That makes one
question load-bearing in a way it was not before: what happens when it is long?
Two different things have to hold, and they are easy to confuse:

* the **record** keeps everything -- the person can page back to the first
  sentence months later, because the transcript is paged, not truncated;
* the **model's view** is cut to fit the provider's window, with the newest turns
  kept and an explicit eviction notice where the history was removed -- so a long
  conversation costs the same per turn as a short one instead of failing.

The second is what a user experiences as "it forgot everything". The tests here
pin both, at the two layers that own them: the store (record) and the fitting
algorithm (view).
"""

from __future__ import annotations

import pytest

from agent.memory.fitting import _EVICTION_SENTINEL, fit_to_budget


def _turn(index: int) -> list[dict]:
    return [
        {"role": "user", "content": f"第 {index} 个问题：这一轮我们聊聊部署的事情，细节略。"},
        {
            "role": "assistant",
            "content": f"第 {index} 个回答：" + ("这是一段比较长的推理过程。" * 4),
        },
    ]


def test_the_record_keeps_every_turn_and_pages_back(tmp_path):
    """A long conversation is paged, not truncated: nothing is dropped."""
    from agent import LTMStore
    from agent.session_service import SessionService

    store = LTMStore(context_dir=tmp_path / "context")
    service = SessionService(store=store)
    total = 120
    for index in range(total):
        store.write_conversation_exchange(
            session_id="s-long",
            user_content=f"第 {index} 问",
            assistant_content=f"第 {index} 答",
        )

    first = service.get_messages("s-long", limit=20)
    assert len(first["messages"]) == 20, "the newest window is bounded"
    assert first["has_more"] is True, "and says there is older history behind it"

    # Walk back to the first sentence. If paging stopped early the oldest turn
    # would be unreachable, which for a single permanent conversation means the
    # beginning of the relationship is silently gone.
    seen = len(first["messages"])
    cursor = first["oldest_seq"]
    for _ in range(20):
        if not cursor:
            break
        page = service.get_messages("s-long", limit=20, before_seq=cursor)
        if not page["messages"]:
            break
        seen += len(page["messages"])
        cursor = page["oldest_seq"]
    # Two rows per turn: the question and the answer.
    assert seen == total * 2, f"paged {seen} rows of {total * 2}"


def test_a_long_history_is_cut_to_the_window_with_a_notice():
    """The model's view is bounded, the newest turns survive, the cut is stated."""
    messages: list[dict] = []
    for index in range(200):
        messages.extend(_turn(index))

    fitted = fit_to_budget(
        messages,
        input_token_budget=4000,
        estimate_tokens=lambda unit: sum(
            max(1, len(str(item.get("content", ""))) // 3) for item in unit
        ),
    )

    assert len(fitted) < len(messages), "a 400-message history cannot fit a 4000-token window"
    # The tail is what the turn is about: it must be there, verbatim.
    assert fitted[-1]["content"].startswith("第 199 个回答")
    assert fitted[-2]["content"].startswith("第 199 个问题")
    # And the cut is not silent: a model that lost its history without being
    # told will happily claim it remembers.
    joined = " ".join(str(item.get("content", "")) for item in fitted)
    assert _EVICTION_SENTINEL in joined, "the eviction notice must say what was removed"
    # The first sentence of the conversation is the thing that was cut.
    assert "第 0 个问题" not in joined


def test_a_short_conversation_is_not_touched():
    """Fitting must be a no-op when it fits -- no notice, no rewriting."""
    messages: list[dict] = []
    for index in range(3):
        messages.extend(_turn(index))

    fitted = fit_to_budget(
        messages,
        input_token_budget=100_000,
        estimate_tokens=lambda unit: sum(
            max(1, len(str(item.get("content", ""))) // 3) for item in unit
        ),
    )

    assert fitted == messages
