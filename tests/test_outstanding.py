"""What the agent is already on the hook for, in front of it every turn.

Two records describe unfinished work and both used to be write-only: a promise
made on Monday was invisible on Tuesday, and a goal could only be rediscovered
by calling `list_goals` for no reason. This module is the reminder.

The tests are on the *rendered block*, not on the helper functions, because the
thing that can go wrong is what the model reads -- a reminder that names the
wrong party, or one that reads as a command, is worse than none.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

from agent.core.outstanding import MAX_SHOWN, outstanding_block


class _Goals:
    def __init__(self, rows):
        self._rows = rows

    def list_goals(self):
        return list(self._rows)


class _NoGoals:
    def list_goals(self):
        return []


def _commitment_store(tmp_path: pathlib.Path, *, hours: float = 2, count: int = 1):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    now = datetime.now(timezone.utc)
    for index in range(count):
        store.create_task(
            NewScheduledTask(
                name=f"整理清单 {index}",
                kind="agent_prompt",
                trigger=TriggerSpec.once(
                    (now + timedelta(hours=hours + index)).isoformat(), "UTC"
                ),
                payload={
                    "prompt": f"给用户整理第 {index} 份清单",
                    "origin": "commitment",
                    "commitment": {
                        "said": f"我两个小时后给你第 {index} 份",
                        "due_at": "",
                    },
                },
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
                workspace_root=str(tmp_path),
            )
        )
    return store


def test_a_promise_is_reminded_with_its_own_words_and_time(tmp_path) -> None:
    """The quote is the part the person heard; the time is the part it is judged by."""
    store = _commitment_store(tmp_path)
    try:
        block = outstanding_block(commitment_store=store, goal_store=_NoGoals())
    finally:
        store.close()

    assert "## Outstanding" in block
    assert "给用户整理第 0 份清单" in block
    assert "我两个小时后给你第 0 份" in block
    assert "小时后" in block, "a promise with no time is a promise with no edge"


def test_a_goal_is_reminded_with_why_it_decided(tmp_path) -> None:
    block = outstanding_block(
        commitment_store=_commitment_store(tmp_path),
        goal_store=_Goals(
            [
                {
                    "goal": "把广告循环整理成能独立运行的脚本",
                    "why": "你昨天说想每天自动跑",
                    "attempts": 1,
                }
            ]
        ),
    )

    assert "Goals you set for yourself" in block
    assert "把广告循环整理成能独立运行的脚本" in block
    assert "你昨天说想每天自动跑" in block


def test_nothing_outstanding_is_no_block_at_all(tmp_path) -> None:
    """An empty heading every turn is noise that trains the reader to skip it."""
    store = _commitment_store(tmp_path, count=0)
    try:
        assert outstanding_block(commitment_store=store, goal_store=_NoGoals()) == ""
    finally:
        store.close()


def test_the_block_does_not_read_as_a_command(tmp_path) -> None:
    """A reminder that says "do these next" outranks what the person just asked.

    The distinction is the whole reason the block exists rather than being
    folded into the task guidance: this is context, not a queue.
    """
    store = _commitment_store(tmp_path)
    try:
        block = outstanding_block(commitment_store=store, goal_store=_NoGoals())
    finally:
        store.close()

    assert "not as a queue that outranks what the person asks for now" in block
    for imperative in ("do these first", "next, do", "you must do"):
        assert imperative not in block.lower()


def test_the_list_is_bounded(tmp_path) -> None:
    """Ten lines is a reminder; fifty is a document nobody finishes."""
    store = _commitment_store(tmp_path, count=MAX_SHOWN + 3)
    try:
        block = outstanding_block(commitment_store=store, goal_store=_NoGoals())
    finally:
        store.close()

    rows = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(rows) == MAX_SHOWN


def test_an_unreadable_store_is_silence_not_a_failed_turn(tmp_path) -> None:
    """A reminder must never be the reason a turn cannot start."""

    class _Broken:
        def list_goals(self):
            raise RuntimeError("store under maintenance")

    block = outstanding_block(commitment_store=None, goal_store=_Broken())

    assert block == ""
