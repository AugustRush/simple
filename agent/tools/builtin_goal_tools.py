"""Registering the goal tools: the agent's own intentions, as three calls."""

from __future__ import annotations


def register_goal_tools(r, *, goal_tools) -> None:
    r.register(
        "set_goal",
        (
            "Record something you intend to bring about **on your own initiative** "
            "-- not what the person asked for (just do it), not what you promised "
            "them (that is `commit`), not a schedule. Use it when you decide a "
            "state is worth reaching: a half-finished piece of work you want to "
            "come back to, something you noticed is wrong, an improvement worth "
            "making. Say `why` in the person's terms -- a goal with no stated "
            "reason is indistinguishable from a whim, and they are the one "
            "deciding whether to trust it. A goal authorises nothing: reaching it "
            "still goes through the same tool permissions every turn does. The "
            "same goal restated is one goal, not two."
        ),
        {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "The state you intend to bring about, as a sentence you "
                        "would say out loud."
                    ),
                },
                "why": {
                    "type": "string",
                    "description": (
                        "Why this is worth doing, in the person's terms. Shown to "
                        "them verbatim."
                    ),
                    "default": "",
                },
                "done_when": {
                    "type": "string",
                    "description": (
                        "How you will know it is reached. A goal that can never "
                        "be checked can never be closed either."
                    ),
                    "default": "",
                },
            },
            "required": ["goal"],
        },
        goal_tools.set_goal,
        source="builtin",
        capabilities=("state_write",),
    )

    r.register(
        "list_goals",
        (
            "The goals you are still working towards, oldest first, with how many "
            "times the patrol has offered to continue each one. Read it before "
            "setting a new goal, and when the person asks what you are working on."
        ),
        {"type": "object", "properties": {}, "required": []},
        goal_tools.list_goals,
        source="builtin",
        capabilities=("read",),
    )

    r.register(
        "close_goal",
        (
            "Finish a goal you reached, or abandon one you decided not to pursue. "
            "`achieved=false` is the honest way to drop one: the record stays and "
            "the reason travels with it. A goal that quietly disappears teaches "
            "the person that the list is not worth reading."
        ),
        {
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "string",
                    "description": "The goal's id, from list_goals.",
                },
                "outcome": {
                    "type": "string",
                    "description": "What came of it, or why you stopped.",
                    "default": "",
                },
                "achieved": {
                    "type": "boolean",
                    "description": "True when you reached it; false when you are dropping it.",
                    "default": True,
                },
            },
            "required": ["goal_id"],
        },
        goal_tools.close_goal,
        source="builtin",
        capabilities=("state_write",),
    )
