"""What may be done without asking: the admission decision, made once, in one place.

Why this is separate from the patrol
------------------------------------
The patrol finds candidates. This decides what may *happen* to them, and the two
questions are asked at different times by different code: a later execution
layer will ask this function "may I?" and a person reading the page will ask it
"why not?". Keeping the answer in one function is what makes those two agree.

The three answers, and why there are three
------------------------------------------
* ``auto`` -- reversible, within today's budget. Do it, log it, tell nobody.
* ``prepare`` -- irreversible, or over the budget. Do *not* do it: write the
  exact call that would have been made, and wait. This is the answer for
  anything that sends, deletes, or runs a command.
* ``drop`` -- the same suggestion was already declined, or its confidence is
  below the floor. Do not write anything; the row already says why.

The middle answer is the one that matters. A system with only "do" and "don't"
either takes risks it cannot take back or is so cautious it is useless; "prepare"
is the shape that lets an agent be useful *and* safe -- it does the thinking
(which call, which arguments) and leaves the commitment to a person.

Why the budget is a count and not a score
-----------------------------------------
"Expected value > cost" is the right idea and an untestable one: tokens are
measurable, "value" is not, and a formula with an unmeasurable term is a
formality that will be tuned until it says yes. So the budget here is
deliberately crude -- **N actions per day, M proposals per day** -- and the
*calibration* is what later replaces it. A cap that is wrong in a visible way
beats a formula that is wrong in an invisible one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # the model lives in the package's __init__; import it for types only
    from . import Proposal

__all__ = [
    "ADMISSION_ANSWERS",
    "AdmissionDecision",
    "AdmissionPolicy",
    "decide_admission",
]

UTC = timezone.utc

ADMISSION_ANSWERS = ("auto", "prepare", "drop")


@dataclass
class AdmissionPolicy:
    """The caps. Every number here is a guess that the proposal rows will correct."""

    #: How many actions may be taken without asking, per rolling day. Zero is a
    #: legitimate setting: it means "shadow only", which is where this started.
    max_auto_actions_per_day: int = 0
    #: How many proposals may be *written* per day, of any kind. This bounds the
    #: attention a person is asked to spend even when nothing is executed.
    max_proposals_per_day: int = 20
    #: Below this confidence, a proposal is dropped rather than shown. A person's
    #: attention is the scarce resource; a 0.2-confidence guess spends it.
    min_confidence: float = 0.5


@dataclass
class AdmissionDecision:
    """The verdict, with the sentence a reader needs to understand it."""

    answer: str
    reason: str
    #: For ``prepare``: what would have been done. The exact call, so a person can
    #: approve it rather than reconstruct it.
    prepared: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "reason": self.reason,
            "prepared": self.prepared,
        }


def _start_of_day(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def decide_admission(
    proposal: "Proposal",
    *,
    now: Optional[datetime] = None,
    policy: Optional[AdmissionPolicy] = None,
    auto_actions_today: int = 0,
    proposals_today: int = 0,
    prior_decision: Optional[str] = None,
    arguments: Optional[dict[str, Any]] = None,
) -> AdmissionDecision:
    """``auto`` / ``prepare`` / ``drop`` for one proposal.

    The order of the checks is the priority order, and each one can only make the
    answer *more* cautious:

    1. **Already answered** -- a declined suggestion is dropped, never re-run.
    2. **Confidence floor** -- a weak guess is dropped, not shown.
    3. **Budget** -- over today's cap, it is prepared rather than done.
    4. **Reversibility** -- anything that can have changed the world outside the
       workspace is prepared, always, whatever the budget says.

    Reversibility is checked last and wins: a reversible action that is over
    budget is still "prepare", and an irreversible one is "prepare" even with an
    empty day. There is no configuration that makes an irreversible action
    automatic.
    """
    now = now or datetime.now(UTC)
    policy = policy or AdmissionPolicy()

    if prior_decision == "declined":
        return AdmissionDecision(
            answer="drop",
            reason="这条建议之前已经被拒过，不再重复提出",
        )
    if prior_decision == "acted":
        return AdmissionDecision(
            answer="drop",
            reason="这条建议已经执行过，不重复执行",
        )

    if proposal.confidence < policy.min_confidence:
        return AdmissionDecision(
            answer="drop",
            reason=(
                f"置信度 {proposal.confidence:.2f} 低于门槛 "
                f"{policy.min_confidence:.2f}，不值得占用注意力"
            ),
        )

    if proposals_today >= policy.max_proposals_per_day:
        return AdmissionDecision(
            answer="drop",
            reason=(
                f"今天的提议数已达上限（{policy.max_proposals_per_day}），"
                "这一条不再写入"
            ),
        )

    if proposal.reversibility == "irreversible":
        return AdmissionDecision(
            answer="prepare",
            reason="这个动作可能改变工作区之外的东西，只能准备好等确认",
            prepared=_prepare(proposal, arguments),
        )

    if proposal.reversibility == "workspace":
        return AdmissionDecision(
            answer="prepare",
            reason=(
                "这个动作会写工作区里的文件；准备好，让人看一眼再执行"
            ),
            prepared=_prepare(proposal, arguments),
        )

    if auto_actions_today >= policy.max_auto_actions_per_day:
        return AdmissionDecision(
            answer="prepare",
            reason=(
                f"今天自动执行的额度已用完（{policy.max_auto_actions_per_day} 次/天），"
                "这一条改为准备好等确认"
            ),
            prepared=_prepare(proposal, arguments),
        )

    return AdmissionDecision(
        answer="auto",
        reason="只读动作，且在今天的额度之内",
        prepared=_prepare(proposal, arguments),
    )


def _prepare(
    proposal: "Proposal", arguments: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """The exact call that would have been made.

    A "prepare" answer without this is a question the reader has to reconstruct:
    *which* tool, with *what* arguments. The whole point of the middle answer is
    that the thinking is already done.
    """
    return {
        "tool": proposal.tool,
        "arguments": dict(arguments or {}),
        "action": proposal.action,
        "reversibility": proposal.reversibility,
    }


def day_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """The rolling-day window the counters are about, for a caller that stores them."""
    moment = now or datetime.now(UTC)
    start = _start_of_day(moment)
    return start, start + timedelta(days=1)
