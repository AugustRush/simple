"""Fitting a provider payload into its window -- a property of the request, not of memory.

Extracted from ``agent/memory/context.py`` (see the remaining-big-files survey).  Everything
here was already a ``@classmethod`` whose ``cls`` was a coat: ``fit_to_budget``'s own docstring
said so -- "the whole of ``compact_messages``, minus the dependency on an instance.  That
dependency was the bug: fitting a request into the provider's window is a property of the
request, not of memory".  The decision had been made and half-taken; this finishes it.

The cluster is closed under itself: fitting needs the eviction notices, the protected indexes,
the message-unit arithmetic and the tool-history repair, and those need each other.  Nothing
here touches the store, the session or the engine, which is why it can be a module of
functions taking what they need.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from agent.memory._helpers import _emit_consolidation
from agent.memory.models import ContextLimitError

__all__ = [
    "fit_to_budget",
    "_complete_message_units",
    "_repair_tool_history",
    "_protected_indexes",
    "_with_notice_before_protected_request",
    "_content_blocks",
    "_strip_eviction_notices",
    "_eviction_notice",
    "_anthropic_tool_result_ids",
    "_role_sequence",
    "_is_real_user_request",
    "_EVICTION_SENTINEL",
    "_COMPACTION_LOW_WATER",
]


_EVICTION_SENTINEL = "[context-eviction]"
_COMPACTION_LOW_WATER = 0.5


def _content_blocks(message: dict) -> list[dict]:
    """The message's content blocks, with their one invariant enforced.

    Every writer appends JSON-native blocks: ``ModelTransport`` normalises
    provider content through ``_json_native_blocks``, and the agent's own
    messages are built from dicts.  So a non-dict block is not a shape to
    tolerate -- it means a writer regressed, or the record came back
    corrupted (``store.py``'s ``default=str`` stores a stray SDK object as a
    ``repr`` string).

    Skipping it is the exact failure this layer exists to prevent: an
    unread ``tool_use`` makes its ``tool_result`` look like an orphan, the
    pair is dropped, and the model is handed a conversation missing the tool
    results it already ran -- silently, and permanently, since the repaired
    list is written back to ``ctx.messages``.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return []
    for block in content:
        if not isinstance(block, dict):
            raise ContextLimitError(
                f"message content block is {type(block).__name__}, not a dict; "
                "every message in ctx.messages must be JSON-native"
            )
    return content


def _anthropic_tool_result_ids(message: dict) -> Optional[set[str]]:
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return None
    blocks = _content_blocks(message)
    if not all(block.get("type") == "tool_result" for block in blocks):
        return None
    return {str(block.get("tool_use_id") or "") for block in blocks}


def _is_real_user_request(message: dict) -> bool:
    return message.get("role") == "user" and _anthropic_tool_result_ids(
        message
    ) is None


def _protected_indexes(
    messages: list[dict], protected: Optional[Sequence[dict]]
) -> set[int]:
    """Which messages this cut must not drop, as indexes into *messages*.

    A turn is the unit of intent: compaction may drop history, it may not
    drop what the turn exists to answer.  A caller inside a turn knows the
    message object it appended, so it passes it and the message is found by
    identity -- "which message is the request" is a fact about the object,
    not about how it reads.

    Without one, fall back to the newest message that *looks* like a
    request.  That proxy is exactly what an interjection falsifies: it
    arrives mid-turn as a real user message, so the newest-looking request
    stops being the turn's own and the original becomes evictable.  Callers
    inside a turn should pass the object rather than rely on this.
    """
    if protected:
        indexes = {
            index
            for index, message in enumerate(messages)
            if any(message is item for item in protected)
        }
        if indexes:
            return indexes
    newest = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if _is_real_user_request(messages[index])
        ),
        -1,
    )
    return {newest} if newest >= 0 else set()


def _complete_message_units(
    messages: list[dict], protected: set[int]
) -> list[list[int]]:
    """Group messages into the smallest units a cut is allowed to remove.

    A unit is a provider protocol unit -- an assistant tool call together
    with every result answering it -- or a question and the reply to it.
    ``protected`` indexes messages the cut must keep, and a protected user
    message is deliberately *not* folded together with the assistant reply
    that follows: sharing a unit would mean dropping the reply drops the
    request with it, which is the one thing a cut may not do.
    """
    units: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        openai_calls = message.get("tool_calls") if role == "assistant" else None
        openai_ids = {
            str(call.get("id") or "")
            for call in openai_calls or []
            if isinstance(call, dict)
        }
        anthropic_ids = {
            str(block.get("id") or "")
            for block in _content_blocks(message)
            if block.get("type") == "tool_use"
        }
        expected_ids = openai_ids | anthropic_ids
        if expected_ids:
            if "" in expected_ids:
                raise ContextLimitError("tool history is structurally incomplete")
            unit = [index]
            answered: set[str] = set()
            cursor = index + 1
            while cursor < len(messages):
                following = messages[cursor]
                if following.get("role") == "tool":
                    tool_id = str(following.get("tool_call_id") or "")
                    if tool_id not in expected_ids:
                        raise ContextLimitError(
                            "tool history contains an orphan tool result"
                        )
                    answered.add(tool_id)
                    unit.append(cursor)
                    cursor += 1
                    continue
                result_ids = _anthropic_tool_result_ids(following)
                if result_ids is not None:
                    if not result_ids or not result_ids.issubset(expected_ids):
                        raise ContextLimitError(
                            "tool history contains an orphan tool result"
                        )
                    answered.update(result_ids)
                    unit.append(cursor)
                    cursor += 1
                    continue
                break
            if answered != expected_ids:
                raise ContextLimitError("tool history is structurally incomplete")
            units.append(unit)
            index = cursor
            continue
        if role == "tool" or _anthropic_tool_result_ids(message) is not None:
            raise ContextLimitError("tool history contains an orphan tool result")
        if (
            role == "user"
            and index not in protected
            and index + 1 < len(messages)
            and messages[index + 1].get("role") == "assistant"
            and not messages[index + 1].get("tool_calls")
            and not any(
                block.get("type") == "tool_use"
                for block in _content_blocks(messages[index + 1])
            )
        ):
            units.append([index, index + 1])
            index += 2
            continue
        units.append([index])
        index += 1
    return units


def _repair_tool_history(messages: list[dict]) -> list[dict]:
    """Drop incomplete provider protocol units left by interrupted turns."""
    repaired: list[dict] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        openai_calls = message.get("tool_calls") if role == "assistant" else None
        openai_ids = {
            str(call.get("id") or "")
            for call in openai_calls or []
            if isinstance(call, dict)
        }
        # `_content_blocks` raises rather than skipping an unreadable block.
        # Here that matters most: this function is what *drops* an orphan
        # tool_result, so an unread `tool_use` is not a missing id -- it is
        # the tool result of a tool the model already ran, deleted from the
        # conversation.
        anthropic_ids = {
            str(block.get("id") or "")
            for block in _content_blocks(message)
            if block.get("type") == "tool_use"
        }
        expected_ids = openai_ids | anthropic_ids
        if expected_ids:
            cursor = index + 1
            answered: set[str] = set()
            result_messages: list[dict] = []
            while cursor < len(messages):
                following = messages[cursor]
                if following.get("role") == "tool":
                    tool_id = str(following.get("tool_call_id") or "")
                    answered.add(tool_id)
                    result_messages.append(following)
                    cursor += 1
                    continue
                result_ids = _anthropic_tool_result_ids(following)
                if result_ids is not None:
                    answered.update(result_ids)
                    result_messages.append(following)
                    cursor += 1
                    continue
                break
            if "" not in expected_ids and answered == expected_ids:
                repaired.append(message)
                repaired.extend(result_messages)
            index = cursor
            continue
        if role == "tool" or _anthropic_tool_result_ids(message) is not None:
            index += 1
            continue
        repaired.append(message)
        index += 1
    if len(repaired) != len(messages):
        _emit_consolidation(
            "tool_history_repaired",
            messages_before=len(messages),
            messages_after=len(repaired),
        )
    return repaired


def _eviction_notice(dropped_count: int) -> dict:
    return {
        "role": "user",
        "content": (
            f"{_EVICTION_SENTINEL} {dropped_count} earlier message(s) were "
            "dropped to fit the input budget. They are recoverable: use "
            "context_retrieve to search this session before assuming anything "
            "about them, rather than guessing or asking the user to repeat."
        ),
    }


def _strip_eviction_notices(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove prior notices and report how many messages they accounted for."""
    kept: list[dict] = []
    dropped = 0
    for message in messages:
        content = message.get("content")
        if (
            message.get("role") == "user"
            and isinstance(content, str)
            and content.startswith(_EVICTION_SENTINEL)
        ):
            head = content[len(_EVICTION_SENTINEL):].strip().split(" ", 1)[0]
            if head.isdigit():
                dropped += int(head)
            continue
        kept.append(message)
    return kept, dropped


def fit_to_budget(
    messages: list[dict],
    *,
    input_token_budget: int,
    estimate_tokens: Callable[[list[dict]], int],
    protected: Optional[Sequence[dict]] = None,
) -> list[dict]:
    """Fit a provider payload into ``input_token_budget``, dropping oldest first.

    This is the whole of ``compact_messages``, minus the dependency on an
    instance.  That dependency was the bug: fitting a request into the
    provider's window is a property of the request, not of memory -- it
    reads no durable memory and writes none -- but it lived only on
    ``ContextManager``, so a run that deliberately has no manager (the
    scheduler's ``stateless`` policy nulls it to stop anything carrying
    between runs) also lost the ability to drop its own oldest turns.  Such
    a run hard-failed with ``ContextLimitError`` the moment its transcript
    outgrew the window, where the interactive path would simply have
    compacted.

    ``estimate_tokens`` is injected rather than read from a consolidation
    engine for the same reason: the caller that has no manager still has to
    be able to say what a message costs.
    """
    budget = int(input_token_budget)
    if budget <= 0:
        raise ContextLimitError("provider input token budget is not positive")
    # Collapse any prior notice into a running count so repeated compactions
    # report cumulative loss instead of stacking notices.
    messages, dropped_before = _strip_eviction_notices(messages)
    messages = _repair_tool_history(messages)
    # Resolved once, here, because everything below is stated over it: which
    # units are grouped as one, which may be removed, and where the notice
    # goes.
    keep = _protected_indexes(messages, protected)
    if any(estimate_tokens([messages[index]]) >= budget for index in keep):
        raise ContextLimitError(
            "a message this turn must keep exceeds the provider input budget"
        )

    retained = _complete_message_units(messages, keep)

    def materialize() -> list[dict]:
        kept_indexes = {item for unit in retained for item in unit}
        return [msg for index, msg in enumerate(messages) if index in kept_indexes]

    compacted = materialize()
    if estimate_tokens(compacted) >= budget:
        # Compaction has been triggered, so cut deep rather than to the
        # first size that fits.  The body is what a provider's prefix cache
        # reuses and every cut rewrites its front, so the whole remaining
        # conversation is re-billed at full rate.  Trimming to the brim puts
        # the next step over the line again and re-cuts on nearly every
        # request; trimming to the low-water mark leaves the turns that
        # follow append-only and pushes the next cut many turns away.
        low_water = max(1, int(budget * _COMPACTION_LOW_WATER))
        while retained and estimate_tokens(compacted) > low_water:
            removable = next(
                (unit for unit in retained if not keep.intersection(unit)),
                None,
            )
            if removable is None:
                # Everything left is the newest request, which is never
                # dropped.  If that still does not fit, the request itself
                # is the problem and there is no cut that would help.
                break
            retained.remove(removable)
            compacted = materialize()
        if estimate_tokens(compacted) >= budget:
            raise ContextLimitError(
                "complete conversation context exceeds input budget"
            )
    kept_indexes = {item for unit in retained for item in unit}
    dropped_messages = [
        message
        for index, message in enumerate(messages)
        if index not in kept_indexes
    ]
    dropped_now = dropped_before + len(dropped_messages)
    if dropped_now:
        # Evicted turns remain reachable through staging and the durable
        # store, but the model cannot retrieve what it does not know is
        # missing, so leave an explicit notice.  The notice is an aid, not a
        # requirement: if it does not fit alongside the conversation it is
        # omitted rather than allowed to fail the compaction it describes.
        #
        # It is never prepended and never last.  Position 0 of the body is
        # the one place a rewrite costs the whole cached prefix, and the
        # last message is the one a provider reads as the turn to answer --
        # putting the notice there means the model replies to the notice
        # instead of to the user.  It goes at the tail of the *retained*
        # prefix, immediately before the newest request: every retained
        # message is untouched, and the user's own words stay last.
        notice = _eviction_notice(dropped_now)
        if estimate_tokens(compacted + [notice]) < budget:
            compacted = _with_notice_before_protected_request(
                compacted, notice, messages, keep
            )
    if len(compacted) != len(messages):
        # Counts alone say how much was lost but not what: a caller
        # cannot tell a dropped tool batch from a dropped user request.
        # The role sequence and token estimate make that judgeable.
        _emit_consolidation(
            "compaction",
            messages_before=len(messages),
            messages_after=len(compacted),
            messages_dropped=dropped_now,
            dropped_roles=_role_sequence(dropped_messages),
            dropped_tokens=estimate_tokens(dropped_messages)
            if dropped_messages
            else 0,
        )
    return compacted


def _with_notice_before_protected_request(
    compacted: list[dict],
    notice: dict,
    messages: list[dict],
    protected: set[int],
) -> list[dict]:
    """Place *notice* at the tail of the retained prefix, not after it.

    Appending it to the very end of the body puts the notice where the
    provider reads the current turn, so the model answers the notice rather
    than the user -- ``M = [... retained ..., user(request), user(notice)]``.
    Prepending it instead rewrites position 0, which is the one offset a
    prefix cache cannot tolerate losing.  Immediately before the request is
    the only position that satisfies both: every retained message is
    byte-identical to what the previous call sent, and the turn's own words
    remain the last thing the provider reads.

    ``protected`` indexes ``messages`` and ``compacted`` holds the same
    objects, so the request is located by identity rather than by re-testing
    which message looks like a request.  The *last* protected index is the
    one to sit before: everything the turn must keep stays after the notice.
    """
    if not protected:
        return compacted + [notice]
    request = messages[max(protected)]
    for position, message in enumerate(compacted):
        if message is request:
            return compacted[:position] + [notice] + compacted[position:]
    # The request was not retained, which `fit_to_budget` never does; fall
    # back to appending rather than dropping the notice.
    return compacted + [notice]


def _role_sequence(messages: list[dict], limit: int = 40) -> str:
    """Comma-joined roles of *messages*, truncated to stay log-sized."""
    roles = [str(message.get("role", "?")) for message in messages[:limit]]
    if len(messages) > limit:
        roles.append(f"+{len(messages) - limit}")
    return ",".join(roles)
