"""Bounding and assembling text that goes back to a model.

Why this module exists
----------------------
Four helpers on ``BaseAgent`` did one job with no instance state: keep a value
under a token-ish size before it is handed to a model, and assemble a labelled
block onto a prompt.  They were ``@staticmethod``/``@classmethod`` but reachable
only through an agent, and they were used from three unrelated places -- the
sub-agent prompt builder, the handoff summary, and the output-contract
validation path -- which is the tell that they were not really part of the agent
at all.

The truncation markers are deliberately distinct
------------------------------------------------
``_bounded_json`` and ``_bounded_struct`` serialize the same way and truncate
differently: the first returns a string ending in ``... [truncated]``, the
second returns ``{"_truncated": True, "preview": ...}`` so a caller that needs a
*value* can still tell it was cut.  ``_bounded_text`` uses its own tail marker.
Collapsing them into one function would silently change what a model is shown,
so they are kept separate and their differences are asserted.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "append_named_block",
    "bounded_json",
    "bounded_structured",
    "bounded_text",
]

#: Suffix marking a truncated string, and the room reserved for it so the
#: result never exceeds the caller's limit.
_JSON_TAIL_RESERVE = 32
_TEXT_TAIL_RESERVE = 24
_TRUNCATED_SUFFIX = "... [truncated]"


def append_named_block(base_text: str, heading: str, lines: list[str]) -> str:
    """Append ``heading`` and ``lines`` to *base_text* as one block.

    An empty *lines* returns *base_text* untouched -- that is what lets a caller
    pass an optional section without branching at every call site -- and an
    empty *base_text* returns the block alone, without a leading blank line.
    """
    if not lines:
        return base_text
    extra = f"{heading}\n" + "\n".join(lines)
    if not base_text:
        return extra
    return f"{base_text}\n\n{extra}"


def bounded_json(value: Any, limit: int = 2400) -> str:
    """Serialize *value* as JSON, truncated to *limit* characters.

    Sorted keys and ``default=str`` so an unserializable leaf cannot raise out of
    a logging path; the truncation is visible in the text because a model
    reading a cut-off JSON document should know it is incomplete.
    """
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= limit:
        return serialized
    return serialized[: max(0, limit - _JSON_TAIL_RESERVE)].rstrip() + _TRUNCATED_SUFFIX


def bounded_text(value: Any, limit: int) -> str:
    """Stringify *value* and truncate it to *limit* characters."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - _TEXT_TAIL_RESERVE)].rstrip() + _TRUNCATED_SUFFIX


def bounded_structured(value: Any, limit: int = 2400) -> Any:
    """Return *value* unchanged, or a truncation marker when it is too large.

    Returns a *value* rather than text: the caller keeps the original object
    when it fits, so a downstream consumer does not have to parse a string back
    into the shape it already had.
    """
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= limit:
        return value
    return {
        "_truncated": True,
        "preview": serialized[: max(0, limit - 64)].rstrip(),
    }
