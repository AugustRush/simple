"""Deciding what an interjection is, before it touches the running turn.

The problem
-----------
A message that arrives while a turn is running had exactly one destination: the
interjection mailbox, which folds it into the turn in progress. That is right
when the message is *about* that work -- "use Python, not Go", "faster", "stop
after this step". It is wrong when it is a different job: "顺便帮我看下 X" is
not an amendment to what is running, and folding it in either derails the
running task or buries the new request inside an answer about something else.

Measured against the code: `interjections.inject_pending_interjections` offers
the model four options -- continue / adjust / pause / stop -- and none of them
is "this is a different job". A request that is not about the current task has
no correct choice available.

What this module decides
------------------------
One of three things, from the message and the turn it is arriving during:

* **amend** -- it refers to the running task. Fold it in (the old behaviour).
* **new** -- it shares nothing with the running task and reads like a request.
  Park it: recorded now, picked up at the next gap, running turn untouched.
* **unclear** -- neither signal is strong. **Park it anyway**, and say so in the
  reply.

Why unclear parks rather than interjects
----------------------------------------
The two mistakes are not symmetric:

* parking something that was really an amendment costs a moment -- the agent
  finishes what it was doing and then does the thing, and the person can see it
  was recorded.
* interjecting something that was really a new task either derails the running
  work or swallows the request inside an unrelated answer.

So the default is the recoverable one. This is the same rule as the import
merge: when unsure, take the action that can be undone.

What is deliberately NOT used
-----------------------------
No model call. A model call here would add a second of latency to every message
sent during a turn, and it would make the behaviour depend on a judgement that
cannot be tested. The signals below are lexical and structural, they are cheap,
and when they are weak the answer is "park it", which needs no judgement at all.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.lexical import lexical_terms

__all__ = ["Triage", "triage_interjection", "VERDICT_AMEND", "VERDICT_NEW", "VERDICT_UNCLEAR"]

VERDICT_AMEND = "amend"
VERDICT_NEW = "new"
VERDICT_UNCLEAR = "unclear"

#: Words that make a message *about* the work in progress rather than a new job.
#: Kept to phrases that are unambiguous: a bare "也" or "还" appears in new
#: requests too, and a false amendment is the expensive mistake.
_AMEND_MARKERS = (
    "改成",
    "改用",
    "换成",
    "用这个",
    "别用",
    "不要用",
    "快点",
    "快一点",
    "慢一点",
    "停一下",
    "停下",
    "暂停",
    "继续",
    "接着",
    "先别",
    "等等",
    "等一下",
    "算了",
    "取消",
    "这样不对",
    "不对",
    "重来",
    "换个方式",
    "换个方法",
    "继续做",
    "往下做",
    "更详细",
    "简单点",
    "详细点",
    "简短",
    "缩短",
    "展开",
    "补充",
    "注意",
    "记得",
    "另外记得",
)

#: Openers that mark a *separate* request. These are the strongest signal
#: available: they are how a person changes subject in one sentence.
_NEW_TASK_MARKERS = (
    "顺便",
    "另外",
    "还有一件事",
    "还有件事",
    "再帮我",
    "帮我再",
    "另外帮我",
    "另一个",
    "新的任务",
    "新任务",
    "单独",
    "等下再",
    "之后再",
    "回头",
    "对了",
    "by the way",
    "btw",
    "also ",
    "separately",
    "another thing",
)

#: A message that is only a reaction to what is happening -- short, and made of
#: these -- is an amendment even if it names no marker.
_SHORT_REACTIONS = frozenset(
    {"好", "好的", "嗯", "嗯嗯", "行", "可以", "对", "是的", "收到", "ok", "okay", "yes", "no"}
)


class Triage:
    """The verdict and the reason, so the caller can record why it parked."""

    __slots__ = ("verdict", "reason", "overlap")

    def __init__(self, verdict: str, reason: str, overlap: float = 0.0) -> None:
        self.verdict = verdict
        self.reason = reason
        self.overlap = overlap

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Triage({self.verdict!r}, {self.reason!r}, overlap={self.overlap:.2f})"

    def as_reason(self) -> str:
        """What goes into the parked row's `reason` column."""
        return f"{self.verdict}: {self.reason}"


def _normalized(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _contains_any(haystack: str, needles: tuple[str, ...]) -> Optional[str]:
    for needle in needles:
        if needle in haystack:
            return needle
    return None


def _overlap_ratio(message: str, running: str) -> float:
    """How much of the message's subject matter appears in the running task.

    Measured in **characters covered by a shared word**, not in words shared.
    The first version divided shared *terms* by the message's terms, and CJK
    n-grams made that meaningless: "把爬虫的并发调高一点" against "写一个爬虫脚本"
    shares exactly one meaningful word (爬虫) but is split into seventeen 2-3
    character fragments, so the ratio came out at 0.06 -- below the "unrelated"
    threshold, for a message that is plainly about the running task.

    Counting characters instead: 爬虫 covers 2 of the message's 9 characters, and
    that is a number with a meaning a reader would agree with.
    """
    clean_message = "".join(str(message or "").split()).lower()
    if not clean_message:
        return 0.0
    running_terms = {
        term for term in lexical_terms(running) if len(term) >= 2
    }
    if not running_terms:
        return 0.0
    # Greedy longest-first, so "爬虫脚" claims its three characters before
    # "爬虫" claims two of the same ones.
    covered = 0
    remaining = clean_message
    for term in sorted(running_terms, key=len, reverse=True):
        if term and term in remaining:
            covered += len(term)
            remaining = remaining.replace(term, "\u0000")
    return min(1.0, covered / len(clean_message))


def triage_interjection(
    text: str,
    *,
    running_goal: str = "",
    running_progress: str = "",
    recent_user_turns: "list[str] | tuple[str, ...]" = (),
) -> Triage:
    """What this message is, given what is running.

    ``running_goal`` is the task in progress; ``recent_user_turns`` are the
    person's own previous messages in that task, which is where an amendment's
    vocabulary usually comes from ("it" refers to something said two turns ago).
    """
    clean = _normalized(text)
    if not clean:
        return Triage(VERDICT_UNCLEAR, "empty")

    # 1. An explicit subject change wins outright. This is the one signal a
    #    person gives deliberately, and it should not be overridden by a shared
    #    word: "顺便问一下，Python 的 list 怎么去重" shares "python" with a
    #    Python task and is still a different job.
    new_marker = _contains_any(clean, _NEW_TASK_MARKERS)
    if new_marker:
        return Triage(VERDICT_NEW, f"opener:{new_marker}")

    # 2. An explicit amendment marker. Checked second so that "顺便" wins, but
    #    before the lexical score: "改用 pandas 吧" is an amendment even if it
    #    happens to share no vocabulary with the goal.
    amend_marker = _contains_any(clean, _AMEND_MARKERS)
    if amend_marker:
        return Triage(VERDICT_AMEND, f"marker:{amend_marker}")

    # 3. A bare acknowledgement. Too short to be a new task, and folding it in
    #    is what the person expects.
    stripped = clean.strip("。．.!！?？~ ")
    if stripped in _SHORT_REACTIONS or len(stripped) <= 2:
        return Triage(VERDICT_AMEND, "short_reaction")

    # 4. Vocabulary overlap with what is running. The thresholds are wide on
    #    purpose -- between them the answer is "unclear", which parks.
    context = " ".join(
        part for part in (running_goal, running_progress, *recent_user_turns) if part
    )
    overlap = _overlap_ratio(clean, context)
    # Any real shared word is enough for "about the running task": measured,
    # "把爬虫的并发调高一点" covers 20% of its characters with words from "写一个
    # 爬虫脚本", and that is one word (爬虫) carrying the whole subject. The bar
    # is low because a *wrong* amend is caught by the markers above -- a message
    # that changes subject usually says so ("顺便", "另外") or shares nothing.
    if overlap >= 0.15:
        return Triage(VERDICT_AMEND, "shared_vocabulary", overlap)
    if overlap <= 0.05:
        return Triage(VERDICT_NEW, "no_shared_vocabulary", overlap)
    return Triage(VERDICT_UNCLEAR, "weak_overlap", overlap)
