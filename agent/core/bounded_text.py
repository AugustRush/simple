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

from dataclasses import replace

from agent.orchestration.runtime import SubtaskResult, SubtaskSpec

import json
from typing import Any

__all__ = [
    "append_named_block",
    "bounded_json",
    "bounded_structured",
    "bounded_text",
    "_with_upstream_summaries",
    "_with_lead_summary",
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


_UPSTREAM_DETAIL_BUDGET = 2400

_UPSTREAM_STRUCTURED_BUDGET = 1200

_UPSTREAM_SUMMARY_BUDGET = 2400


def _with_upstream_summaries(
    spec: SubtaskSpec,
    upstream_summaries: dict[str, str],
    upstream_results: dict[str, SubtaskResult] | None = None,
) -> SubtaskSpec:
    summary_text = bounded_text(
        "\n".join(
            f"- {dep}: {summary}" for dep, summary in upstream_summaries.items()
        ),
        _UPSTREAM_SUMMARY_BUDGET,
    )
    task = append_named_block(
        spec.task,
        "Upstream summaries:",
        [summary_text] if summary_text else [],
    )
    handoff: dict[str, Any] = dict(spec.handoff)
    structured_lines: list[str] = []
    detail_lines: list[str] = []
    for dep, result in (upstream_results or {}).items():
        if result.structured_content is not None:
            bounded = bounded_structured(result.structured_content)
            handoff[dep] = bounded
            structured_lines.append(f"- {dep}: {bounded_json(bounded)}")
        if result.content and len(result.content) > len(result.summary):
            detail_lines.append(f"- {dep}: {result.content}")
    detail_text = bounded_text(
        "\n".join(detail_lines),
        _UPSTREAM_DETAIL_BUDGET,
    )
    task = append_named_block(
        task,
        "Upstream detail:",
        [detail_text] if detail_text else [],
    )
    structured_text = bounded_text(
        "\n".join(structured_lines),
        _UPSTREAM_STRUCTURED_BUDGET,
    )
    task = append_named_block(
        task,
        "Upstream structured results:",
        [structured_text] if structured_text else [],
    )
    return replace(spec, task=task, handoff=handoff)


def _with_lead_summary(
    spec: SubtaskSpec,
    lead_summary: str,
    lead_structured_context: dict[str, Any] | None = None,
) -> SubtaskSpec:
    if not lead_summary and not lead_structured_context:
        return spec
    task = append_named_block(
        spec.task,
        "Lead summary:",
        [bounded_text(lead_summary, _UPSTREAM_SUMMARY_BUDGET)]
        if lead_summary
        else [],
    )
    structured_lines = []
    for dep, value in (lead_structured_context or {}).items():
        bounded = bounded_structured(value)
        structured_lines.append(f"- {dep}: {bounded_json(bounded)}")
    task = append_named_block(
        task,
        "Lead structured results:",
        [
            bounded_text(
                "\n".join(structured_lines),
                _UPSTREAM_STRUCTURED_BUDGET,
            )
        ]
        if structured_lines
        else [],
    )
    handoff: dict[str, Any] = dict(spec.handoff)
    for dep, value in (lead_structured_context or {}).items():
        handoff[dep] = bounded_structured(value)
    return replace(spec, task=task, handoff=handoff)
