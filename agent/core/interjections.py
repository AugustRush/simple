"""Draining a session's interjection mailbox into the turn's messages.

Split out of ``BaseAgent`` because it is the loop's business rather than the
agent's: it is called once per turn iteration, it is reached only from the turn
loop, and the only thing it needs from the agent is the tool registry -- which is
now a parameter, so the dependency is visible in the signature instead of being
implied by ``self``.

An *interjection* is a message the user sends while a turn is already running.  It
belongs to what this turn was asked to do, so it is appended to the messages the
model sees and recorded on the turn: a transcript without it shows an answer to a
question nobody can see, which is what the durable record used to do when only the
message that opened the turn was journaled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.core.agent import AgentContext


def inject_pending_interjections(
ctx: "AgentContext", pending: list[dict], registry: Any
) -> None:
    """Drain the per-session mailbox into ctx.messages as a user_interjection.

    ``pending`` is shared mutable state owned by RuntimeSessionState — we
    list-copy then ``clear()`` in place so the channel handler's
    subsequent appends only see fresh entries.  Each entry contributes
    one ``<user_interjection>`` block; an instruction footer tells the
    LLM how to handle it (continue, adjust, summarize, stop).
    """
    drained = list(pending)
    pending.clear()
    if not drained:
        return
    blocks: list[str] = []
    interjected: list[str] = []
    #: (entry, text) pairs, kept alongside the rendered blocks: the record
    #: written at turn end needs the id and the urgency of each one, and
    #: re-deriving them from `drained` afterwards would have to skip the
    #: same blanks twice, in step.
    read: list[tuple[dict[str, Any], str]] = []
    for entry in drained:
        text = str(entry.get("text", "") or "").strip()
        if not text:
            continue
        interjected.append(text)
        read.append((entry, text))
        who = str(entry.get("from_user", "") or "user")
        urgency = str(entry.get("urgency", "normal") or "normal")
        arrived = entry.get("arrived_at")
        when = ""
        if isinstance(arrived, (int, float)):
            from datetime import datetime as _dt
            when = _dt.fromtimestamp(arrived).strftime("%H:%M:%S")
        attrs = f' from="{who}" urgency="{urgency}"'
        if when:
            attrs += f' at="{when}"'
        blocks.append(f"<user_interjection{attrs}>\n{text}\n</user_interjection>")
    if not blocks:
        return
    footer = (
        "\n\nThe user interjected during your in-progress task. "
        "Briefly acknowledge what you heard, then decide: continue / "
        "adjust direction / pause and ask for clarification / stop and "
        "summarize what you have so far.  Treat urgency=\"now\" as "
        "explicit interruption (read and respond first); urgency="
        "\"normal\" as info added in passing (acknowledge and "
        "incorporate when sensible)."
    )
    ctx.messages.append({
        "role": "user",
        "content": "\n\n".join(blocks) + footer,
    })
    # Held for the turn's record rather than only for the payload: the model
    # reads these mid-turn and answers them in the same turn, so a
    # transcript without them shows an answer to a question nobody can see
    # -- which is what the durable record used to do, since only the message
    # that *opened* the turn was journaled.  The record is written at turn
    # end from this list; it is not cleared here because one turn may drain
    # the mailbox several times, and every drained message is part of what
    # the turn answered.
    recorded = ctx.metadata.setdefault("_turn_interjections", [])
    if isinstance(recorded, list):
        for entry, text in read:
            recorded.append(
                {
                    "text": text,
                    "message_id": str(entry.get("message_id") or ""),
                    "urgency": str(entry.get("urgency") or "normal"),
                    "arrived_at": entry.get("arrived_at"),
                }
            )
    # An interjection is the user asking for something *during* this turn,
    # so it belongs to what this turn was asked to do.  Leaving it out would
    # make a tool refuse an instruction the user had just typed, which reads
    # as the agent ignoring them.
    earlier = str(registry.get_context("turn_request") or "")
    addition = "\n".join(interjected)
    registry.set_context(
        "turn_request", f"{earlier}\n{addition}" if earlier else addition
    )
