"""How memory state is *rendered*: working-state text, checkpoint summaries, notices.

Extracted from ``ContextManager`` (see the remaining-big-files survey).  These are the pure
halves of the working-state pipeline: given the state, produce the words; given a checkpoint,
summarise it; decide what counts as visible text.  None of them touch the store, the session
or the engine, which is why the rendering rules can be read and tested apart from when they run.

Imported back into ``agent/memory/context.py``, so the call sites and any reader of these names
keep working unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.memory.models import ConsolidationResult

__all__ = [
    "_format_working_state_text",
    "_shows_text",
    "_checkpoint_summary",
    "_clip_working_state_text",
    "_format_fact_value",
    "_coerce_consolidation_result",
]


def _coerce_consolidation_result(result: Any) -> ConsolidationResult:
    """A consolidator's return value as a result, with a reason when there is one.

    The last branch is a consolidator that answered with something that is not a
    result at all (a bare ``None``, a flag).  Falsy is a failure, and it is one this
    code *can* describe -- so it does, instead of leaving the reporter to say only
    "extraction returned failure" about a return value it never saw.
    """
    if isinstance(result, ConsolidationResult):
        return result
    if isinstance(result, list):
        return ConsolidationResult(success=True, compressed_messages=result)
    success = bool(result)
    return ConsolidationResult(
        success=success,
        compressed_messages=[],
        error="" if success else f"consolidator returned {type(result).__name__}",
    )


def _format_fact_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _shows_text(content: str, text: str) -> bool:
    """Whether *content* carries *text* as one of its ``\\n\\n``-separated parts.

    A user message is not the user's words: it is
    ``"\\n\\n".join([turn_context, user_message, attachment_context])`` (see
    ``BaseAgent._build_user_message_content``), and ``turn_context`` is itself
    a ``"\\n\\n"`` join of this turn's blocks.  So the user's words are one
    *part* of the content -- not the whole of it, and not necessarily the
    last one, because a non-vision attachment's context is appended after
    them.

    Comparing the whole string by equality is what this did, and it stopped
    matching the moment per-turn context moved out of the system prompt and
    into the message.  Taking the tail instead recovers the attachment
    context whenever there is one, and only the *last paragraph* of a
    multi-paragraph message -- both measured, both still re-injected.

    Matching a delimited run is exact for every shape, because a part of a
    ``"\\n\\n"`` join is always bounded by ``"\\n\\n"`` or by the ends of the
    string.  It accepts everything the equality test and the tail test
    accepted, so it can only cause *fewer* re-injections, and a match still
    means what it should: this text was already sent.
    """
    if not content or not text:
        return False
    return (
        content == text
        or content.startswith(text + "\n\n")
        or content.endswith("\n\n" + text)
        or f"\n\n{text}\n\n" in content
    )


def _format_working_state_text(state: dict[str, Any]) -> str:
    if not isinstance(state, dict) or not state:
        return ""
    fields = [
        ("active_goal", "Active goal"),
        ("status", "Status"),
        ("progress", "Progress"),
        ("next_action", "Next action"),
        ("last_error", "Last error"),
    ]
    lines = ["## Restored Working Context"]
    lines.append(
        "A prior working context exists for this session. Use it when relevant; "
        "if the user's new message is unrelated, do not force the old task."
    )
    for key, label in fields:
        value = str(state.get(key, "") or "").strip()
        if value:
            lines.append(f"- {label}: {value}")
    artifacts = state.get("artifacts")
    if isinstance(artifacts, list):
        clean_artifacts = [str(item).strip() for item in artifacts if str(item).strip()]
        if clean_artifacts:
            lines.append("- Artifacts: " + ", ".join(clean_artifacts[:8]))
    recent_turns = state.get("recent_turns")
    if isinstance(recent_turns, list) and recent_turns:
        lines.append("- Recent turns:")
        for turn in recent_turns[-4:]:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role", "") or "").upper()
            content = str(turn.get("content", "") or "").strip()
            if role and content:
                lines.append(f"  - {role}: {content}")
    return "\n".join(lines) if len(lines) > 2 else ""


def _checkpoint_summary(messages: list[dict], limit: int = 4000) -> str:
    lines: list[str] = []
    length = 0
    for message in messages:
        role = str(message.get("role", "unknown") or "unknown").upper()
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                # Tolerated on purpose: this builds a readable summary, so
                # a block it cannot read costs a line of text, not a tool
                # result.  Failing here would lose the whole checkpoint.
                if not isinstance(block, dict):
                    continue
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            content = " ".join(parts)
        clean = re.sub(r"\s+", " ", str(content or "")).strip()
        if not clean:
            continue
        line = f"- {role}: {clean}"
        remaining = limit - length
        if remaining <= 0:
            break
        lines.append(line[:remaining])
        length += min(len(line), remaining)
    return "\n".join(lines)


def _clip_working_state_text(text: str, limit: int = 800) -> str:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"
