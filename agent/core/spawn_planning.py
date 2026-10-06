"""Turning a batch of spawn_agent calls into an execution plan.

Why this module exists
----------------------
Six methods on ``BaseAgent`` decided everything about a spawn batch *before* any
sub-agent ran: which capability profile each one gets, how many may run in one
turn, what the spec is, what the result payload looks like, and whether the batch
is ``parallel``, ``pipeline``, ``rendezvous`` or ``direct``.

None of them perform I/O -- no ``await``, no transport, no registry read -- so
all of it is planning, and all of it was reachable only through an agent.  The
group was measured at 19 ``self`` attributes; the configuration and helper ones
are now declared once in :class:`PlannerDeps` instead of reached for
individually.

What is deliberately unchanged
------------------------------
The decision rules themselves, moved by mechanical rewrite rather than
rewritten: the implementation-work vocabulary, the profile fallback order
(``explicit`` → ``full`` for direct mode → ``implementation`` → ``read_only``),
the per-turn ceiling of ``max(8, max_parallel_agents * 4)``, and the coordination
error messages.  Behaviour changes smuggled into a move are the failure mode
this refactor keeps finding, so the logic below is the original text with
attribute access redirected.

The coordination rules, stated once
-----------------------------------
An explicit ``coordination_mode`` wins; two different ones are an error rather
than a silent winner; an unsupported one is an error that names it.  Failing
loudly is the point: a batch that quietly ran as ``parallel`` when one subtask
declared ``pipeline`` would drop the ordering the caller asked for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from agent.orchestration.planner import OrchestrationDecision
from agent.orchestration.runtime import (
    RendezvousDirective,
    SubtaskResult,
    SubtaskSpec,
)

__all__ = [
    "MAX_PROMOTED_SUMMARY_CHARS",
    "PlannerDeps",
    "SpawnPlanner",
    "promote_orchestration_runs",
]

#: Modes a batch may explicitly declare.
SUPPORTED_COORDINATION_MODES = frozenset({"parallel", "pipeline", "rendezvous"})

#: Vocabulary that makes a task "implementation work" and therefore earns the
#: ``implementation`` capability profile rather than ``read_only``.  Both
#: languages, because tasks are written by the user in theirs.
_IMPLEMENTATION_TOKENS = (
    "implement",
    "implementation",
    "engineer",
    "developer",
    "coder",
    "patch",
    "fix",
    "edit",
    "modify",
    "refactor",
    "code",
    "file",
    "实现",
    "修复",
    "修改",
    "重构",
    "编码",
)


@dataclass(frozen=True)
class PlannerDeps:
    """Everything the planner needs that it cannot compute itself.

    ``settings`` is read live rather than copied, for the same reason the token
    budget reads its window live: these values are assigned on a live agent in
    more than one place.
    """

    settings: Any
    max_parallel_agents: int
    string_list: Callable[[Any], list[str]]
    normalize_output_contract: Callable[[Any], dict[str, Any]]
    mapping_dict: Callable[[Any], dict[str, Any]]
    bounded_text: Callable[[Any, int], str]
    bounded_structured: Callable[[Any], Any]
    result_content_limit: Callable[[], int]


class SpawnPlanner:
    """Planning for one agent's spawn batches.  See the module docstring."""

    __slots__ = ("_d",)

    def __init__(self, deps: PlannerDeps) -> None:
        self._d = deps

    # -- capability profiles -------------------------------------------------

    @staticmethod
    def looks_like_implementation_work(role: str, task: str) -> bool:
        text = f"{role} {task}".lower()
        return any(token in text for token in _IMPLEMENTATION_TOKENS)

    def default_capability_profile(
        self,
        role: str,
        task: str,
        explicit: str,
        *,
        orchestration_mode: str,
    ) -> str:
        """The profile a subtask runs under when it did not name one.

        Order matters: an explicit request always wins, ``direct`` batches get
        the full toolset because there is no coordination to protect, and only
        then does the task's wording decide between implementation and read-only.
        """
        if explicit:
            return explicit.strip().lower()
        if orchestration_mode == "direct":
            return "full"
        if self.looks_like_implementation_work(role, task):
            return "implementation"
        return "read_only"

    # -- sizing --------------------------------------------------------------

    def max_agents_per_turn(self) -> int:
        """Total sub-agents one turn may run, independent of concurrency."""
        configured = int(getattr(self._d.settings, "max_agents_per_turn", 0) or 0)
        if configured > 0:
            return configured
        return max(8, int(self._d.max_parallel_agents) * 4)

    # -- spec construction ---------------------------------------------------

    def spawn_tool_use_to_spec(
        self,
        tool_use: dict[str, Any],
        *,
        index: int,
        orchestration_decision: OrchestrationDecision,
    ) -> SubtaskSpec:
        tool_input = tool_use.get("input", {})
        role = str(tool_input.get("role", "assistant") or "assistant")
        task = str(tool_input.get("task", "") or "")
        spec_id = str(
            tool_input.get("id")
            or tool_use.get("id")
            or f"spawn-{index}"
        )
        depends_on = self._d.string_list(tool_input.get("depends_on"))
        explicit_profile = str(tool_input.get("capability_profile", "") or "").strip()
        return SubtaskSpec(
            id=spec_id,
            role=role,
            task=task,
            depends_on=depends_on,
            expected_output=str(tool_input.get("expected_output", "") or ""),
            system_suffix=str(tool_input.get("system_suffix", "") or ""),
            output_contract=self._d.normalize_output_contract(
                self._d.mapping_dict(tool_input.get("output_contract"))
            ),
            write_scope=self._d.string_list(tool_input.get("write_scope")),
            capability_profile=self.default_capability_profile(
                role,
                task,
                explicit_profile,
                orchestration_mode=orchestration_decision.mode,
            ),
            early_exit=bool(tool_input.get("early_exit", False)),
            timeout_seconds=float(tool_input.get("timeout_seconds", 0) or 0),
            continue_on_failure=bool(tool_input.get("continue_on_failure", False)),
        )

    def spawn_result_payload(
        self,
        spec: SubtaskSpec,
        result: SubtaskResult,
        *,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """The JSON a completed sub-agent hands back to the parent."""
        content_limit = self._d.result_content_limit()
        payload: dict[str, Any] = {
            "ok": result.ok,
            "role": self._d.bounded_text(spec.role, 200),
            "task": self._d.bounded_text(spec.task, 1000),
            "content": self._d.bounded_text(
                result.content or "(no output)",
                content_limit,
            ),
            "tool_calls_made": result.tool_calls_made[:100],
        }
        if result.structured_content is not None:
            payload["structured_content"] = self._d.bounded_structured(
                result.structured_content
            )
        if result.error:
            payload["error"] = self._d.bounded_text(result.error, 1000)
        if result.artifact_ref:
            payload["artifact_ref"] = result.artifact_ref
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def summarize_subtask_result(content: str, limit: int = 400) -> str:
        text = str(content or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def parse_rendezvous_directive(
        resp_text: str,
        *,
        known_ids: set[str],
    ) -> RendezvousDirective | None:
        """Parse and normalize a rendezvous synthesis response.

        The model's output is free-form, so every field is validated and
        normalized instead of being trusted verbatim: ``stop`` only becomes
        True for an explicit true-like value, ``continue_with`` is filtered to
        known subtask ids (falling back to "keep everyone" when the selection
        is empty or unknown), and anything malformed returns None so the
        caller uses its deterministic fallback.
        """
        text = str(resp_text or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3].strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        summary = str(data.get("summary", "") or "")
        raw_stop = data.get("stop")
        if isinstance(raw_stop, bool):
            stop = raw_stop
        elif isinstance(raw_stop, str):
            stop = str(raw_stop).strip().lower() in {"true", "yes", "1"}
        elif isinstance(raw_stop, (int, float)):
            stop = bool(raw_stop)
        else:
            stop = False
        raw_continue = data.get("continue_with")
        if raw_continue is None:
            continue_with = None
        elif isinstance(raw_continue, str):
            continue_with = [raw_continue] if raw_continue in known_ids else None
        elif isinstance(raw_continue, list):
            selected = [
                str(item)
                for item in raw_continue
                if str(item or "").strip()
            ]
            known = [item for item in selected if item in known_ids]
            continue_with = known or None
        else:
            continue_with = None
        return RendezvousDirective(
            summary=summary,
            stop=stop,
            continue_with=continue_with,
        )

    # -- coordination mode ---------------------------------------------------

    def execution_mode_from_spawn_calls(
        self,
        spawn_calls: list[tuple[int, dict]],
    ) -> str:
        """Which runner the batch should use.

        Raises on an unsupported or conflicting explicit mode rather than
        picking one: a batch that silently ran as ``parallel`` when a subtask
        declared ``pipeline`` would drop the ordering the caller asked for.
        """
        explicit_modes = {
            str(tu.get("input", {}).get("coordination_mode", "") or "").strip().lower()
            for _, tu in spawn_calls
        }
        explicit_modes.discard("")
        unsupported_modes = explicit_modes - SUPPORTED_COORDINATION_MODES
        if unsupported_modes:
            raise ValueError(
                "unsupported coordination_mode: "
                + ", ".join(sorted(unsupported_modes))
            )
        if len(explicit_modes) > 1:
            raise ValueError(
                "conflicting coordination_mode values: "
                + ", ".join(sorted(explicit_modes))
            )
        if explicit_modes:
            return next(iter(explicit_modes))
        if any(
            self._d.string_list(tu.get("input", {}).get("depends_on"))
            for _, tu in spawn_calls
        ):
            return "pipeline"
        if len(spawn_calls) > 1:
            return "parallel"
        return "direct"

# ── Promoting run summaries into session memory ──────────────────────────────

#: Longest summary promoted into session memory.  Bounded because this text
#: becomes part of a later turn's context, and an unbounded promotion would
#: push out the conversation it is meant to inform.
MAX_PROMOTED_SUMMARY_CHARS = 1600


def promote_orchestration_runs(
    context_manager: Any,
    runs: list[dict],
    *,
    now: str,
    new_id: Callable[[], str],
    entry_type: type,
) -> int:
    """Write bounded, successful run summaries into session memory.

    Returns how many entries were written, so a caller can tell "nothing to
    promote" from "promotion ran and found nothing eligible" without reading
    the store back.

    Three filters, each of which exists for a reason found the hard way:

    - **``result.ok``** -- a failed sub-agent's text is not a finding, and
      promoting it would put a former error into the permanent memory a later
      turn reads as fact.
    - **non-empty after stripping** -- an empty summary carries no information
      and would occupy a slot that retrieval ranks.
    - **a length bound** -- this text joins a later turn's context, so an
      unbounded promotion competes with the conversation it is meant to inform.

    ``now``, ``new_id`` and ``entry_type`` are arguments rather than imports so
    the promotion can be exercised without a store, a clock, or the memory
    layer's entry class.
    """
    if not context_manager:
        return 0
    session_id = context_manager.staging.session_id
    written = 0
    for run in runs:
        run_id = str(run.get("run_id") or "")
        for result in run.get("results", []):
            if not isinstance(result, SubtaskResult) or not result.ok:
                continue
            summary = str(result.summary or result.content or "").strip()
            if not summary:
                continue
            summary = summary[:MAX_PROMOTED_SUMMARY_CHARS]
            context_manager.store.add_entry(
                entry_type(
                    id=new_id(),
                    content=(
                        f"orchestration run {run_id} [{result.id}] summary:\n"
                        f"{summary}"
                    ),
                    importance=0.45,
                    category="episodes",
                    entity="orchestration",
                    memory_type="orchestration_summary",
                    scope=f"session:{session_id}",
                    source_session=session_id,
                    confidence=0.6,
                    created_at=now,
                    updated_at=now,
                )
            )
            written += 1
    return written
