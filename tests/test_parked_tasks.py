"""Work the person asks for while something else is running.

The scenario: a turn is in progress and the person types something that is not
about it. Folding that into the running turn either derails it or buries the
request; ignoring it loses it. So it is recorded -- the turn is untouched -- and
picked up at the next gap.

Three things have to hold, and the third is the one that failed before:

1. recording is idempotent (a resend parks once),
2. triage tells "about this" from "a different job", and errs toward parking,
3. **the queue is actually consumed**. A store that fills and never drains is
   the `set_goal` mistake: a reminder nobody acts on.
"""

from __future__ import annotations

import pytest

from agent.core.interjection_triage import (
    VERDICT_AMEND,
    VERDICT_NEW,
    VERDICT_UNCLEAR,
    triage_interjection,
)
from agent.core.parked import finish_parked, parked_block, take_next_parked


@pytest.fixture()
def store(tmp_path):
    from agent.memory.store import LTMStore

    (tmp_path / "context").mkdir()
    instance = LTMStore(tmp_path / "context")
    yield instance
    instance.close()


# ── the queue itself ────────────────────────────────────────────────────────


def test_a_parked_task_is_recorded_and_read_back(store) -> None:
    task = store.park_task(
        session_id="s1",
        text="顺便查一下天气",
        reason="new: opener:顺便",
        source_message_id="m9",
    )

    assert task is not None
    assert task.status == "waiting"
    (listed,) = store.list_parked_tasks(session_id="s1")
    assert listed.text == "顺便查一下天气"
    assert listed.reason == "new: opener:顺便"


def test_parking_the_same_message_twice_parks_it_once(store) -> None:
    """A resend, or a replay after a reconnect, must not queue the words twice."""
    first = store.park_task(session_id="s1", text="顺便查天气", source_message_id="m9")
    second = store.park_task(session_id="s1", text="顺便查天气", source_message_id="m9")

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(store.list_parked_tasks(session_id="s1")) == 1


def test_the_queue_is_oldest_first(store) -> None:
    """A list a person wrote down is worked in the order they wrote it."""
    for index, text in enumerate(("第一件", "第二件", "第三件")):
        store.park_task(session_id="s1", text=text, source_message_id=f"m{index}")

    listed = [item.text for item in store.list_parked_tasks(session_id="s1")]

    assert listed == ["第一件", "第二件", "第三件"]


def test_a_task_moves_through_its_states(store) -> None:
    task = store.park_task(session_id="s1", text="查天气", source_message_id="m1")
    assert task is not None

    assert store.set_parked_status(task_id=task.id, status="active")
    assert store.list_parked_tasks(session_id="s1") == []
    active = store.list_parked_tasks(session_id="s1", statuses=("active",))[0]
    assert active.started_at

    assert store.set_parked_status(task_id=task.id, status="done", outcome="晴，18 度")
    done = store.list_parked_tasks(session_id="s1", statuses=("done",))[0]
    assert done.outcome == "晴，18 度"
    assert done.finished_at


def test_one_sessions_queue_is_not_anothers(store) -> None:
    store.park_task(session_id="s1", text="甲的事", source_message_id="m1")
    store.park_task(session_id="s2", text="乙的事", source_message_id="m2")

    assert [item.text for item in store.list_parked_tasks(session_id="s1")] == ["甲的事"]
    assert [item.text for item in store.list_parked_tasks(session_id="s2")] == ["乙的事"]


# ── the block the model reads ───────────────────────────────────────────────


def test_the_block_names_the_parked_work(store) -> None:
    store.park_task(session_id="s1", text="顺便查一下天气", source_message_id="m1")

    block = parked_block(store=store, session_id="s1")

    assert "排着的事" in block
    assert "顺便查一下天气" in block
    # It must not read as an instruction to stop what it is doing.
    assert "不要因为看到这个就停下现在做的事" in block


def test_the_block_is_empty_when_nothing_is_waiting(store) -> None:
    assert parked_block(store=store, session_id="s1") == ""


def test_the_block_caps_what_it_shows(store) -> None:
    """A prompt block is read, not searched: twenty lines is a document."""
    for index in range(8):
        store.park_task(session_id="s1", text=f"第 {index} 件", source_message_id=f"m{index}")

    block = parked_block(store=store, session_id="s1")

    assert "第 0 件" in block
    assert "第 7 件" not in block
    assert "还有 3 件没列出" in block


# ── the pickup: what makes it a promise ─────────────────────────────────────


def test_the_oldest_waiting_task_is_handed_out_and_claimed(store) -> None:
    store.park_task(session_id="s1", text="第一件", source_message_id="m1")
    store.park_task(session_id="s1", text="第二件", source_message_id="m2")

    picked = take_next_parked(store=store, session_id="s1")

    assert picked is not None
    assert picked["text"] == "第一件"
    # Claimed, not merely read: a second gap must not hand out the same task.
    assert take_next_parked(store=store, session_id="s1")["text"] == "第二件"
    assert take_next_parked(store=store, session_id="s1") is None


def test_an_empty_queue_hands_out_nothing(store) -> None:
    assert take_next_parked(store=store, session_id="s1") is None


def test_finishing_a_task_closes_it(store) -> None:
    task = store.park_task(session_id="s1", text="查天气", source_message_id="m1")
    assert task is not None
    store.set_parked_status(task_id=task.id, status="active")

    assert finish_parked(store=store, task_id=task.id, outcome="晴")

    done = store.list_parked_tasks(session_id="s1", statuses=("done",))[0]
    assert done.outcome == "晴"


def test_a_broken_store_never_fails_the_turn() -> None:
    """Every read here rides in a turn; a store that cannot be reached means
    "nothing parked", never a turn that fails to start."""

    class Broken:
        def list_parked_tasks(self, **_kwargs):
            raise RuntimeError("database is locked")

        def set_parked_status(self, **_kwargs):
            raise RuntimeError("database is locked")

    assert parked_block(store=Broken(), session_id="s1") == ""
    assert take_next_parked(store=Broken(), session_id="s1") is None
    assert finish_parked(store=Broken(), task_id="x") is False


# ── the triage ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,goal,want",
    [
        ("改成用 Python 写", "写一个爬虫脚本", VERDICT_AMEND),
        ("快点", "写一个爬虫脚本", VERDICT_AMEND),
        ("好的", "写一个爬虫脚本", VERDICT_AMEND),
        ("把爬虫的并发调高一点", "写一个爬虫脚本", VERDICT_AMEND),
        ("顺便帮我看下天气", "写一个爬虫脚本", VERDICT_NEW),
        ("对了，明天几号？", "写一个爬虫脚本", VERDICT_NEW),
        ("再帮我查一下磁盘占用", "写一个爬虫脚本", VERDICT_NEW),
        ("另外，那个报表也重做一遍", "写一个爬虫脚本", VERDICT_NEW),
        ("帮我看看 /tmp 占多少空间", "写一个爬虫脚本", VERDICT_NEW),
    ],
)
def test_triage_separates_amendments_from_new_jobs(text, goal, want) -> None:
    verdict = triage_interjection(text, running_goal=goal)
    assert verdict.verdict == want, f"{text!r} -> {verdict}"


def test_an_explicit_subject_change_wins_over_shared_words() -> None:
    """「顺便问一下，Python 的 list 怎么去重」shares "python" with a Python task
    and is still a different job -- the opener is the person telling you so."""
    verdict = triage_interjection(
        "顺便问一下，Python 的 list 怎么去重",
        running_goal="用 Python 写一个爬虫脚本",
    )
    assert verdict.verdict == VERDICT_NEW


def test_a_shared_word_that_is_not_cjk_still_counts() -> None:
    """A latin word is one term, so sharing it is a strong signal -- unlike a CJK
    phrase, which the lexer splits into overlapping fragments."""
    verdict = triage_interjection(
        "爬虫的 timeout 设成 30 秒", running_goal="写一个爬虫脚本"
    )
    assert verdict.verdict in (VERDICT_AMEND, VERDICT_UNCLEAR)


def test_a_weak_signal_parks_rather_than_interjecting() -> None:
    """The two mistakes are not symmetric: parking an amendment costs a moment,
    interjecting a new job derails the running turn. So unclear parks."""
    verdict = triage_interjection(
        "把那个东西调整一下", running_goal="写一个爬虫脚本"
    )
    assert verdict.verdict in (VERDICT_UNCLEAR, VERDICT_NEW)
    # And the caller parks everything that is not an amendment.
    assert verdict.verdict != VERDICT_AMEND


# ── the wiring: is the queue actually consumed? ─────────────────────────────
#
# The three tests above prove the store and the helpers work. These prove the
# turn loop and the coordinator *use* them -- a helper that is correct and never
# called is exactly the failure this feature exists to avoid.


def test_the_turn_loop_puts_the_parked_block_in_front_of_the_model() -> None:
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "core" / "turn_loop.py"
    ).read_text(encoding="utf-8")

    assert "parked_block(" in source
    assert "turn_blocks.append(parked)" in source


def test_the_coordinator_picks_a_parked_task_up_when_a_turn_ends() -> None:
    """The gap is the turn boundary: nothing the person queued is waiting, so
    the oldest parked task becomes the next turn. Without this the queue is a
    note nobody acts on."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "agent"
        / "commands"
        / "coordinator.py"
    ).read_text(encoding="utf-8")

    assert "_pick_up_parked(" in source
    assert "take_next_parked(" in source
    # It runs only when the person's own queue is empty -- their messages first.
    assert "if not state.restart_queue:\n                picked = self._pick_up_parked" in source


def test_an_interjection_that_is_not_an_amendment_is_parked() -> None:
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "agent"
        / "commands"
        / "coordinator.py"
    ).read_text(encoding="utf-8")

    assert "_triage_interjection(" in source
    assert "verdict.verdict != \"amend\"" in source
    assert "kind=\"parked\"" in source


def test_the_two_closing_tools_are_registered() -> None:
    """The agent has to be able to close a parked task, or the queue only grows."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "agent"
        / "tools"
        / "builtin_tools.py"
    ).read_text(encoding="utf-8")

    assert "register_parked_task_tools" in source


def test_the_closing_tools_resolve_a_task_by_its_words() -> None:
    """The model reads these as numbered sentences in a prompt block, not as ids.
    Requiring an id it never saw would make the tool unusable without a lookup
    call first."""
    from agent.tools.builtin_parked_tools import ParkedTaskTools

    class _Store:
        def __init__(self):
            self.calls = []

        def list_parked_tasks(self, **_kwargs):
            from agent.memory.models import ParkedTask

            return [
                ParkedTask(
                    id="parked-1", session_id="s1", text="顺便查一下天气"
                )
            ]

        def set_parked_status(self, **kwargs):
            self.calls.append(kwargs)
            return True

    class _Registry:
        def get_context(self, _name):
            return "s1"

    store = _Store()
    tools = ParkedTaskTools(
        _Registry(),
        lambda: store,
        lambda **payload: {"ok": True, **payload},
        lambda message: {"ok": False, "error": message},
    )

    result = tools.finish_parked_task("查天气", outcome="晴")

    assert result["ok"] is True
    assert store.calls[0]["task_id"] == "parked-1"
    assert store.calls[0]["status"] == "done"
    assert store.calls[0]["outcome"] == "晴"
