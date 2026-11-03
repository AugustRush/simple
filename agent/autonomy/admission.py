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
    """What is worth showing, and nothing else.

    The two caps that used to live here -- actions per day, proposals per day --
    are gone. They were the wrong instrument for the question: a count decides
    *which* finite set of work to skip, and skips it by arrival order rather than
    by importance. What actually bounds the work is the facts themselves: a rule
    fires at most once per session per fact, a live proposal is not written twice,
    and an answered one never returns. So there is nothing for a cap to protect
    that dedupe and reversibility are not already protecting.

    What is left is the one judgement a policy should hold: a guess too weak to
    show is dropped, because a person's attention is the scarce resource.
    """

    #: Below this confidence, a proposal is dropped rather than shown.
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
    prior_decision: Optional[str] = None,
    arguments: Optional[dict[str, Any]] = None,
) -> AdmissionDecision:
    """``auto`` / ``prepare`` / ``drop`` for one proposal.

    The order of the checks is the priority order, and each one can only make the
    answer *more* cautious:

    1. **Already answered** -- a declined suggestion is dropped, never re-run.
    2. **Confidence floor** -- a weak guess is dropped, not shown.
    3. **Reversibility** -- anything that can have changed the world outside the
       workspace is prepared, always.

    There is no longer a budget step. It used to sit between the floor and
    reversibility ("over today's cap, prepare instead of do"), and it was the
    wrong shape: a count decides *which* finite set of work to skip and skips it
    by arrival order, while the set of candidates is already bounded by the facts
    -- a rule fires at most once per session per fact, and an answered proposal
    never returns. What was left for the budget to do was delay work that the
    funnel had already decided was worth doing, in an order nobody chose.

    So the answer is now entirely about *permission*: reversible is done,
    irreversible is prepared. ``patrol_tick`` decides the order -- highest
    priority first, then oldest -- which is the part a number was never able to
    express.
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

    return AdmissionDecision(
        answer="auto",
        # The reason says what the answer is based on -- a fact about the tool,
        # not a count of what already ran. "Within today's budget" would be true
        # and irrelevant now that there is no budget.
        reason="只读动作：不会改变工作区之外的东西，所以直接做",
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
