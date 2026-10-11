"""Turning a finished sub-agent into the payload its parent receives.

Why this module exists
----------------------
This is the last of ``_execute_agent``'s three parts (prepare, run, finalize),
and the only one with a side effect.  Splitting it in two keeps that honest:

- :func:`build_result_payload` is pure -- the answer, the contract check, and the
  error/structured content that follows from them;
- :func:`finalize_sub_agent` is the effectful half -- summarize if the result
  exceeds the window, then persist the finding to long-term memory.

Only the second takes a store, which is what makes "a failed contract does not
write memory" and "an oversized result is summarized before it is stored"
testable without a database.

The full content goes to memory, the summary goes to the parent
--------------------------------------------------------------
Two different sizes for two different readers.  The parent's context is finite, so
an oversized result is summarized before it travels; but the *memory* entry keeps
the full text, because a later retrieval can pay for detail the current turn
cannot afford.  Writing the summary to memory instead would mean the detail is
lost exactly when it was worth keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

__all__ = ["FinalizedResult", "build_result_payload", "finalize_sub_agent"]

#: Importance and confidence recorded for a sub-agent observation.  Between the
#: two: it is more than a passing remark and less than a user-stated fact.
_OBSERVATION_IMPORTANCE = 0.5
_OBSERVATION_CONFIDENCE = 0.7

#: Longest task excerpt quoted inside a memory entry.
_TASK_EXCERPT_CHARS = 500

#: Memory scope when the run has no id to attribute itself to.
_UNTRACKED_SCOPE = "run:untracked"


@dataclass(frozen=True)
class FinalizedResult:
    """What the finalizer changed, for a caller that needs to observe it."""

    payload: dict[str, Any]
    summarized: bool = False
    memory_entry_id: Optional[str] = None


def build_result_payload(
    *,
    result: Any,
    role: str,
    shaped_task: str,
    expected_output: str,
    output_contract: dict[str, Any],
    validate_output_contract: Callable[..., tuple],
) -> dict[str, Any]:
    """The payload for one successful sub-agent run, before finalization.

    The order of the three writes to ``payload["error"]`` is deliberate and
    preserved from the original: a contract failure sets the error, and then the
    sub-agent's own error **overwrites it** when there is one.  A sub-agent that
    both failed and missed its contract reports the failure, because that is the
    cause a reader can act on; the contract miss is a consequence of it.
    """
    payload: dict[str, Any] = {
        "ok": result.error is None,
        "role": role,
        "task": shaped_task,
        "content": result.content or "(no output)",
        "tool_calls_made": result.tool_calls_made,
    }
    contract_ok, contract_content, structured_content, contract_error = (
        validate_output_contract(
            result.content or "",
            expected_output=expected_output,
            output_contract=output_contract,
        )
    )
    if not contract_ok:
        payload["ok"] = False
        payload["error"] = contract_error
    else:
        payload["content"] = contract_content
        if structured_content is not None:
            payload["structured_content"] = structured_content
    if result.error:
        payload["error"] = result.error
    return payload


async def finalize_sub_agent(
    payload: dict[str, Any],
    *,
    full_content: str,
    role: str,
    shaped_task: str,
    run_id: str = "",
    content_limit: int,
    summarize: Callable[[str, str, str], Awaitable[str]],
    context_manager: Any = None,
    new_id: Callable[[], str],
    now: str,
    entry_type: type,
    suppress: Callable[[str], Any],
) -> FinalizedResult:
    """Summarize an oversized result, then persist the finding.

    ``full_content`` is what was produced; ``payload["content"]`` is what the
    parent will see.  Only the memory entry gets the former; see the module
    docstring.

    The memory write is guarded twice: it is skipped when the run was not
    successful, and it is wrapped in ``suppress`` so a store failure does not
    fail a turn whose actual work succeeded.  A sub-agent's artifact is a
    by-product of the turn, not a precondition for it.
    """
    summarized = False
    if len(full_content) > content_limit:
        payload["content"] = await summarize(full_content, role, shaped_task)
        summarized = True

    if context_manager is None or not payload["ok"]:
        return FinalizedResult(payload=payload, summarized=summarized)

    entry_id: Optional[str] = None
    with suppress("LTM write for sub-agent result failed; turn continues"):
        # The id is minted before the write because the entry needs it, but it
        # is only *recorded* after the write succeeds.  Assigning it first left
        # `memory_entry_id` and `artifact_ref` pointing at an entry that was
        # never stored, which a caller would then fail to retrieve.
        candidate_id = new_id()
        context_manager.remember(
            entry_type(
                id=candidate_id,
                content=(
                    f"sub-agent [{role}] task: "
                    f"{shaped_task[:_TASK_EXCERPT_CHARS]}\n\n"
                    f"result: {full_content}"
                ),
                importance=_OBSERVATION_IMPORTANCE,
                category="episodes",
                entity=role,
                memory_type="sub_agent_observation",
                scope=(f"run:{run_id}" if run_id else _UNTRACKED_SCOPE),
                status="active",
                source_session=(context_manager.session_id or ""),
                confidence=_OBSERVATION_CONFIDENCE,
                created_at=now,
                updated_at=now,
            )
        )
        entry_id = candidate_id
        payload["artifact_ref"] = entry_id

    return FinalizedResult(
        payload=payload, summarized=summarized, memory_entry_id=entry_id
    )
