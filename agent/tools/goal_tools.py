"""Goals: the work the agent decided to do on its own, and how it closes them.

Why this is not a promise, a schedule, or a proposal
----------------------------------------------------
Four things in this codebase look alike from a distance and are each a different
promise to the person reading them:

* ``commit``      — "I told you I would do this" — owed to a *person*, at a time.
* ``automation_create`` — "you told me to do this every day" — an instruction.
* a proposal      — "here is something I found; may I?" — the patrol's guess
                    about *operations*.
* a **goal**      — "I intend to bring this about" — the agent's own intention,
                    with no deadline and no instruction behind it.

A goal is what lets the agent answer "what are you working on?" with something
other than "nothing, I was waiting". Keeping it a separate record rather than a
scheduled task is what lets the board show it as *an intention* instead of
pretending it is a schedule.

What a goal deliberately does not carry
---------------------------------------
No deadline (that is a promise), no trigger (that is a schedule), no permission
of its own (a goal authorises nothing: whatever it does to reach the goal still
goes through the same tool permissions every turn does). A goal that could
authorise actions would be a way to launder an irreversible step through a
sentence.

Why the cap exists
------------------
The patrol may offer to work on a goal a bounded number of times. A goal that
keeps being offered and never progresses is not persistence, it is a loop that
has stopped being useful -- and the abandonment is reported, because "I stopped
trying" is exactly the kind of thing an autonomous agent must say out loud.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = ["GoalTools", "goal_store"]


def goal_store() -> Any:
    """The autonomy store, or ``None`` when this deployment has none.

    ``None`` is a real answer: a composition without autonomy has no goals, and
    every tool here says so rather than inventing a store.
    """
    try:
        from agent.autonomy.store import open_proposal_store

        return open_proposal_store()
    except Exception:
        return None


class GoalTools:
    """The three calls that make an intention a thing with a life."""

    def __init__(
        self,
        registry: Any,
        store_factory: Callable[[], Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.
        self.registry = registry
        self._store_factory = store_factory
        self._ok = ok
        self._error = error

    def set_goal(
        self,
        goal: str,
        why: str = "",
        done_when: str = "",
    ) -> dict[str, Any]:
        """Record something you intend to bring about, on your own initiative.

        Use this when you decide -- not when you are told -- that a state is
        worth reaching: a half-finished piece of work you want to come back to,
        something you noticed is wrong and want to fix, an improvement you think
        the person would want. It is *not* for what they asked for (do it), nor
        for what you promised (that is `commit`), nor for a schedule.

        Say why in the person's terms. A goal with no stated reason is
        indistinguishable from a whim, and they are the one deciding whether to
        trust it.
        """
        text = " ".join(str(goal or "").split())
        if not text:
            return self._error("goal 不能为空：写下你想达成的那个状态。")
        store = self._store_factory()
        if store is None:
            return self._error("这个部署没有自主库，记不了目标。")
        try:
            goal_id = store.set_goal(
                text,
                why=str(why or "").strip(),
                done_when=str(done_when or "").strip(),
                source_session=str(self.registry.get_context("turn_session_id") or ""),
            )
            open_goals = store.list_goals()
        except ValueError as exc:
            return self._error(str(exc))
        finally:
            try:
                store.close()
            except Exception:
                pass
        return self._ok(
            goal_id=goal_id,
            open_count=len(open_goals),
            note=(
                "记下了。它会出现在看板的「它给自己定的目标」里；"
                "你没让它做之前，它不会动任何东西。"
            ),
        )

    def list_goals(self) -> dict[str, Any]:
        """The goals you are still working towards, oldest first."""
        store = self._store_factory()
        if store is None:
            return self._error("这个部署没有自主库，读不到目标。")
        try:
            rows = store.list_goals()
        finally:
            try:
                store.close()
            except Exception:
                pass
        items = [
            {
                "goal_id": str(row.get("id") or ""),
                "goal": str(row.get("goal") or ""),
                "why": str(row.get("why") or ""),
                "done_when": str(row.get("done_when") or ""),
                "attempts": int(row.get("attempts") or 0),
                "created_at": str(row.get("created_at") or ""),
            }
            for row in rows
        ]
        return self._ok(count=len(items), goals=items)

    def close_goal(self, goal_id: str, outcome: str = "", achieved: bool = True) -> dict[str, Any]:
        """Finish a goal you reached, or abandon one you decided not to pursue.

        ``achieved=False`` is the honest way to drop one: the record stays, and
        the reason travels with it. A goal that quietly disappears teaches the
        person that the list is not worth reading.
        """
        wanted = str(goal_id or "").strip()
        if not wanted:
            return self._error("goal_id 不能为空；用 list_goals 里的 id。")
        store = self._store_factory()
        if store is None:
            return self._error("这个部署没有自主库。")
        try:
            closed = store.close_goal(
                wanted, status="done" if achieved else "abandoned"
            )
        finally:
            try:
                store.close()
            except Exception:
                pass
        if not closed:
            return self._error(
                f"没有 id 为「{wanted}」的未完成目标。用 list_goals 看现在有哪些。"
            )
        return self._ok(
            goal_id=wanted,
            status="done" if achieved else "abandoned",
            outcome=str(outcome or "").strip(),
            note="已关闭。" if achieved else "已放弃——它会留在记录里，不会悄悄消失。",
        )
