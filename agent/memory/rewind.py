"""Where a turn's request sits in the provider's replay tail, and how to cut it.

Moved out of ``session_service`` for two reasons, and the second is the one that
mattered: the replay tail is a *memory-layer* object (its messages are the
provider's wire shapes, repaired and compacted by ``ContextManager``), and
cutting it from a service that only knows the transcript is exactly the
reach-past-the-contract that ``test_nothing_outside_the_package_calls_past_the_contract``
exists to catch.

A provider message carries no transcript id, so the request is found by its
words.  They are a run *inside* the message rather than the whole of it: the
turn's retrieved context is prepended and attachment context appended, so the
match is containment, not equality.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["request_message_index", "cut_provider_checkpoint"]


def _carries_request_text(message: dict[str, Any], text: str) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return text in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if text in str(block.get("text") or ""):
                    return True
    return False


def request_message_index(
    messages: list[dict[str, Any]], text: str
) -> Optional[int]:
    """The provider-history position of the turn that asked ``text``.

    Searched newest-first, and only over messages that *are* requests: for some
    providers tool results travel as ``user`` messages carrying a ``tool_result``
    block, and an interjection is a ``user`` message too.  Matching either would
    cut the history in the middle of the turn instead of at its start.

    ``None`` is a real answer -- the turn has been compacted away and survives
    only inside a summary -- and callers must not guess in that case.
    """
    needle = str(text or "").strip()
    if not needle:
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            continue
        if _carries_request_text(message, needle):
            return index
    return None


def cut_provider_checkpoint(store: Any, session_id: str, text: str) -> int:
    """Remove the turn that asked ``text`` from the stored replay tail.

    Returns how many messages were removed.  Nothing is written when the request
    is not in the tail: a checkpoint that has been compacted past this turn does
    not contain it, and truncating at some earlier position would delete a
    *different* turn from the model's history.  That is a degraded rewind rather
    than a wrong one -- the model's memory of the turn is a summary, which is
    what it would be for a "send it again" anyway.
    """
    load = getattr(store, "load_provider_checkpoint", None)
    save = getattr(store, "save_provider_checkpoint", None)
    if not callable(load) or not callable(save):
        return 0
    checkpoint = load(session_id)
    if not isinstance(checkpoint, dict):
        return 0
    messages = checkpoint.get("messages")
    if not isinstance(messages, list):
        return 0
    index = request_message_index(messages, text)
    if index is None:
        return 0
    removed = len(messages) - index
    if removed <= 0:
        return 0
    save(session_id, messages[:index], summary=str(checkpoint.get("summary") or ""))
    return removed
