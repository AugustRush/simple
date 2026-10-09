"""Reading an upstream step's output, under two ceilings.

Why this module exists
----------------------
``read_step_output`` is the tool a workflow step uses to see what the step it
stands on produced.  It is a lookup into *this workflow's own history* rather
than a file reader with a narrower name, and that is its whole safety property:
only ``output_path`` values the scheduler recorded are ever read, so a path it
was not told about is not reachable through it.

It was a method pair on ``BuiltinTools``, a 5042-line class, alongside the
workflow tools that create the graph it reads.  The graph-walking helper
(``_workflow_ancestors``) and the three bounds moved with it, because nothing
else read them except the schema that quotes two of them back to the model.

The two ceilings are the part worth stating, because both exist to stop a read
from costing the run the context it needs to act on what it read:

- ``READ_STEP_OUTPUT_MAX_CHARS`` is the static ceiling, and
  ``READ_STEP_OUTPUT_DEFAULT_CHARS`` the comfortable read.
- ``READ_STEP_OUTPUT_BUDGET_SHARE`` divides *this run's* input budget, published
  by the agent core before every provider call.  A flat 40000 was three quarters
  of a 53841-token budget in one call, so a run could walk into the limit and
  lose everything it had done -- and the failure mode is a provider error that
  says nothing about the tool that caused it.

Addressing is by *step key* because that is the name the graph gives a step and
the only one a model can be expected to know, and the workflow is taken from the
run being served rather than from an argument, so a step cannot reach outside
its own graph by naming one.  Reading is restricted to ancestors: a step beside
it has not run for this round and a step below it has not run at all, so reading
either hands back an earlier round's text as if it were this round's work.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Callable


#: How much of an upstream step's output ``read_step_output`` hands back.
#:
#: Bounded because the tool exists to feed a model's context, and a step's
#: output is unbounded: a run that reads three upstream reports whole can spend
#: its entire budget before it starts working.  The default is a comfortable
#: read; the ceiling is what a caller who has asked for "all of it" gets, with
#: the path returned alongside so the rest is one more call away -- and with
#: ``READ_STEP_OUTPUT_BUDGET_SHARE`` applied on top, so "all of it" cannot mean
#: "all of the window".
READ_STEP_OUTPUT_DEFAULT_CHARS = 8000
READ_STEP_OUTPUT_MAX_CHARS = 40000
#: The largest share of a run's input budget one read may spend.
#:
#: A flat character ceiling cannot know whether it is asking for a tenth of the
#: budget or three quarters of it, because the cost of a read is measured in the
#: units the wall is: CJK text is about one token per character, so 40000
#: characters of Chinese is 40000 tokens.  A quarter is a read that cannot by
#: itself exhaust the window, and a run that wants more reads it in pieces.
READ_STEP_OUTPUT_BUDGET_SHARE = 4


def _workflow_ancestors(steps, key: str) -> set[str]:
    """Every step ``key`` transitively stands on.

    Read from the authored graph rather than from the triggers, because the
    question is about the shape of the chain: a step may read what it stands
    on, and nothing else.  A step beside it has not run for this round, and a
    step below it has not run at all -- reading either returns an earlier
    round's text, which is exactly the confusion the "not your own step" rule
    exists to prevent, one edge further out.
    """
    depends = {
        str(step.key).strip(): [str(item).strip() for item in (step.depends_on or [])]
        for step in steps
    }
    seen: set[str] = set()
    pending = list(depends.get(str(key).strip(), []))
    while pending:
        current = pending.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        pending.extend(depends.get(current, []))
    return seen


class StepOutputTools:
    """The step-output tool, with the scheduler accessor and responders it needs."""

    def __init__(
        self,
        schedule_store: Callable[[], Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        self._schedule_store = schedule_store
        self._ok = ok
        self._error = error


    def _limit(self, requested: Any) -> tuple[int, str]:
        """How much of an upstream's output one read may bring in, and why.

        The ceiling comes from this run's input budget, which the agent core
        publishes before every provider call, rather than from a constant: the
        flat 40000 was three quarters of a 53841-token budget in a single call,
        so a run could walk into the limit and lose everything it had done --
        the failure mode being a provider error that says nothing about the
        tool that caused it.

        Returns the limit and a sentence explaining it when the budget is what
        bound, because a caller told "you got less than you asked for" with no
        reason will simply ask again.
        """
        budget = 0
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            if active_ctx is not None:
                metadata = getattr(active_ctx, "metadata", {}) or {}
                budget = int(metadata.get("_last_input_token_budget", 0) or 0)
        ceiling = READ_STEP_OUTPUT_MAX_CHARS
        if budget > 0:
            ceiling = min(ceiling, max(1, budget // READ_STEP_OUTPUT_BUDGET_SHARE))
        wanted = int(requested or 0) or READ_STEP_OUTPUT_DEFAULT_CHARS
        limit = max(1, min(wanted, ceiling))
        if budget > 0 and wanted > ceiling:
            return limit, (
                f"本次运行的输入预算约 {budget} tokens，单次读取最多用掉其中 "
                f"{budget // READ_STEP_OUTPUT_BUDGET_SHARE} tokens，"
                f"所以这里最多返回 {limit} 个字符。"
            )
        return limit, ""

    def read(
        self, step: str, max_chars: int = READ_STEP_OUTPUT_DEFAULT_CHARS
    ) -> dict[str, Any]:
        """The latest successful output of a step this one stands on.

        Addressed by *step key* because that is the name the graph gives a
        step and the only one a model can be expected to know: task ids and run
        ids are internal, and a step that has to look them up is a step that
        will look them up wrong.  The workflow is taken from the run being
        served, not from an argument, so a step cannot reach outside its own
        graph by naming one.

        Restricted to ancestors of the current step, not to the whole graph.
        A step beside it has not run for this round and a step below it has not
        run at all, so reading either hands back an earlier round's text as if
        it were this round's work -- which is the failure the tool exists to
        make impossible rather than to make unlikely.

        Only ``output_path`` values the scheduler recorded are read.  That is
        the tool's whole safety property: it is not a file reader with a
        narrower name, it is a lookup into this workflow's own history, and a
        path it was not told about is not reachable through it.

        How much comes back is capped by this run's input budget as well as by
        ``max_chars`` (see ``_step_output_limit``), so asking for everything
        cannot cost the run the context it needs to act on what it read.
        """
        wanted = str(step or "").strip()
        if not wanted:
            return self._error("step 不能为空；用上游步骤的 key，例如 step2")

        context: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            if active_ctx is not None:
                context = dict(getattr(active_ctx, "metadata", {}) or {})
        workflow_id = str(context.get("scheduler_workflow_id", "") or "").strip()
        if not workflow_id:
            return self._error(
                "当前运行不属于任何流程，read_step_output 只能读同一条流程里的步骤。"
                "单独的任务请用 read_file 或 shell 读它的产物路径。"
            )

        store = self._schedule_store()
        steps = store.step_tasks(workflow_id)
        target = steps.get(wanted)
        if target is None:
            available = "、".join(sorted(steps)) or "（无）"
            return self._error(
                f"流程里没有步骤「{wanted}」。可选的步骤：{available}"
            )
        if wanted == str(context.get("scheduler_step_key", "") or "").strip():
            # Reading your own output would return the previous round's text
            # as if it were the upstream's, which is the kind of answer that
            # looks right and is not.
            return self._error(
                f"「{wanted}」就是当前这一步，读它只会拿到自己上一轮的结果。"
            )
        workflow = store.get_workflow(workflow_id)
        current_key = str(context.get("scheduler_step_key", "") or "").strip()
        ancestors = (
            _workflow_ancestors(workflow.steps, current_key)
            if workflow is not None
            else set()
        )
        if wanted not in ancestors:
            # Same reasoning as the line above, one edge further out: a step
            # that has not run yet, or ran in an earlier round, has nothing
            # about *this* round to hand over.
            known = "、".join(sorted(ancestors)) or "（无）"
            return self._error(
                f"「{wanted}」不是当前这一步的上游，读它只会拿到它上一轮的文本。"
                f"当前步骤的上游：{known}"
            )

        latest = None
        for run in store.list_runs(target.id):
            if run.status == "succeeded" and str(run.output_path or "").strip():
                latest = run
        if latest is None:
            return self._error(
                f"步骤「{wanted}」还没有成功过，所以没有产物可读。"
            )

        path = Path(str(latest.output_path)).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self._error(f"读取步骤「{wanted}」的输出失败：{exc}")

        limit, budget_note = self._limit(max_chars)
        truncated = len(text) > limit
        notes: list[str] = []
        if truncated:
            notes.append(f"只返回了前 {limit} 个字符，共 {len(text)} 个。")
        if budget_note:
            notes.append(budget_note)
        if notes:
            notes.append("需要其余内容就读上面的 output_path。")
        return self._ok(
            step=wanted,
            task_id=target.id,
            task_name=target.name,
            run_id=latest.id,
            finished_at=latest.finished_at.isoformat() if latest.finished_at else None,
            summary=latest.summary,
            output_path=str(path),
            text=text[:limit],
            truncated=truncated,
            total_chars=len(text),
            # Said out loud rather than left to be discovered: a step that
            # believes it read the whole report and got two thirds of it will
            # act on the two thirds.
            note=" ".join(notes),
        )
