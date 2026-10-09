"""The scheduler and workflow tools: one graph, read and written.

Why this module exists
----------------------
Twenty-one methods of ``BuiltinTools`` -- a 4854-line class -- are about one
thing: a graph of timed tasks.  ``schedule_create`` / ``list`` / ``runs`` /
``update`` / ``set_enabled`` / ``run`` / ``cancel`` / ``delete`` write and read
its rows; ``workflow_create`` / ``update`` / ``list`` / ``delete`` write and
read the chain those rows belong to; and the ``describe_*`` /
``action_payload`` helpers are the sentences both of them show a person.

They could not be split from each other.  The workflow methods read five of the
scheduler's own helpers (``schedule_store``, ``schedule_target``,
``schedule_trigger``, ``action_payload``, ``workflow_steps``) and eighteen names
from ``agent.scheduler``; the survey measured that and concluded they are one
extraction, not two.  What they do *not* need is the host: the five callables
they used to reach through ``self`` -- the store accessor, the two workspace
roots, and the two response builders -- are injected instead, so this class
depends on five functions rather than on a class with forty tools in it.

Three properties travelled with the code, each of which a reader would
otherwise be tempted to "fix":

- **A creator has to answer for what it creates.**  The executor's
  ``requires_request`` check refuses an unasked creation; these handlers are the
  other half.  ``schedule_create`` refuses a write-granting permission profile
  rather than pinning an unattended write to a directory nobody chose, and the
  maintenance tools take an ``intent`` they do not store -- a task is a row that
  has to explain itself months later, an edit is not.
- **The store is reached through one accessor, and it stays on the host.**
  ``BuiltinTools._schedule_store`` caches it, and three other clusters (signals,
  step output, the host itself) share the same accessor.
- **``normalize_products`` is applied on the way in.**  A step's declared
  products are normalised when written so the handoff reads one shape, which is
  why the same call appears in four places.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from agent import shared
from agent.scheduler.models import LOCAL_TIMEZONE, normalize_products

from .runtime import _active_schedule_target


#: How much of a failed run's text a tool result carries.
#:
#: The reason is what a caller acts on, and the whole of it -- along with the
#: text the run produced -- is on disk at ``output_path``.  A log pasted into
#: the context in full buys nothing and can cost the run its window, so every
#: free-text field here is clipped and says so.
RUN_REASON_CHARS = 700
RUN_SUMMARY_CHARS = 400
RUN_TAIL_CHARS = 400


def _clip(text: Any, limit: int) -> str:
    """*text*, cut to *limit* characters with the cut made visible.

    Silent truncation is worse than none: a reason that ends mid-sentence reads
    as the whole reason, and the caller stops looking.
    """
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + f"…（共 {len(value)} 字，此处截断）"


def _run_outcome(run: Any) -> Optional[dict[str, Any]]:
    """How one run ended, in the three fields a list can carry.

    ``status`` and ``verdict`` travel separately because they are not the same
    question: status says whether the run happened, verdict says whether what
    it produced met the bar the task was given.  A run can end cleanly having
    produced something that does not do the job, and collapsing the two is what
    makes a useless answer read as a success.
    """
    if run is None:
        return None
    finished_at = getattr(run, "finished_at", None)
    return {
        "status": str(getattr(run, "status", "") or ""),
        "verdict": str(getattr(run, "verdict", "") or ""),
        "finished_at": finished_at.isoformat() if finished_at else None,
    }


def _run_detail(run: Any) -> dict[str, Any]:
    """One run, with the reason it ended that way.

    Every field the run row has that answers "why", and nothing that answers
    "what exactly did it write" -- that question is the file itself, at
    ``output_path``.
    """
    started_at = getattr(run, "started_at", None)
    finished_at = getattr(run, "finished_at", None)
    duration_ms = None
    if started_at is not None and finished_at is not None:
        duration_ms = max(0, round((finished_at - started_at).total_seconds() * 1000))
    scheduled_for = getattr(run, "scheduled_for", None)
    output_path = str(getattr(run, "output_path", "") or "").strip()
    output_available = False
    if output_path:
        try:
            output_available = Path(output_path).expanduser().is_file()
        except OSError:
            output_available = False
    verification = getattr(run, "verification", None)
    from agent.verification import verification_payload

    raw_verification = verification_payload(verification)
    verification_dict = None
    if raw_verification is not None:
        verification_dict = {
            "status": str(raw_verification.get("status") or ""),
            "exit_code": raw_verification.get("exit_code"),
            "error": raw_verification.get("error"),
            "stderr_tail": _clip(raw_verification.get("stderr_tail"), RUN_TAIL_CHARS),
            "stdout_tail": _clip(raw_verification.get("stdout_tail"), RUN_TAIL_CHARS),
        }
    from agent.scheduler import run_needs_attention
    from agent.scheduler.models import products_for_handoff, products_payload

    # What this run was told to leave behind, read off its own snapshot -- the
    # same source the runtime measured against, so the declaration reported
    # here is the one the run was actually held to and not the task's current
    # one.  A run from before this existed carries no key and declares nothing,
    # which is the right answer for it.
    #
    # The two halves are separated by ``products_for_handoff`` rather than by
    # filtering the report here: a path that was promised and has no file
    # behind it must read as *missing*, and deriving that from the report alone
    # would let a declaration with no measurement read as "nothing was
    # missing".  Same shape the step below is handed, for the same reason.
    snapshot = dict(getattr(run, "config_snapshot", {}) or {})
    handoff = products_for_handoff(
        snapshot.get("produces"), products_payload(getattr(run, "products", None))
    )

    return {
        "run_id": str(getattr(run, "id", "") or ""),
        "status": str(getattr(run, "status", "") or ""),
        "verdict": str(getattr(run, "verdict", "") or ""),
        "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "duration_ms": duration_ms,
        "attempt": int(getattr(run, "attempt", 1) or 1),
        "trigger_source": str(getattr(run, "trigger_source", "schedule") or "schedule"),
        "missed_count": int(getattr(run, "missed_count", 0) or 0),
        "needs_attention": run_needs_attention(run),
        "acknowledged": bool(getattr(run, "acknowledged_at", None)),
        "error": _clip(getattr(run, "error", ""), RUN_REASON_CHARS),
        "summary": _clip(getattr(run, "summary", ""), RUN_SUMMARY_CHARS),
        "output_path": output_path,
        "output_available": output_available,
        "verification": verification_dict,
        # What the run was asked to leave behind and what was actually there
        # when it ended.  Both, because neither answers the question alone: an
        # empty list with an empty declaration means the task never promised
        # anything, and an empty list with a declaration means the work did not
        # produce it -- and "declared nothing" must not be able to read as
        # "produced nothing".
        "products": handoff["products"],
        "products_missing": handoff["products_missing"],
    }


class ScheduleTools:
    """The scheduler and workflow tools, with the five callables they need."""

    def __init__(
        self,
        schedule_store: Callable[[], Any],
        active_workspace_root: Callable[[], Path],
        chosen_workspace_root: Callable[[], Optional[Path]],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        self._schedule_store = schedule_store
        self._active_workspace_root = active_workspace_root
        self._chosen_workspace_root = chosen_workspace_root
        self._ok = ok
        self._error = error

    def schedule_target(self, delivery_mode: Optional[str] = None):
        from agent.scheduler import DeliveryTarget

        active = _active_schedule_target.get()
        if delivery_mode == "standalone":
            return "standalone", DeliveryTarget.standalone()
        if delivery_mode == "channel" and active:
            return "channel", DeliveryTarget.channel(
                target_type=str(active.get("target_type", "feishu_chat")),
                chat_id=str(active["chat_id"]),
                chat_type=str(active.get("chat_type", "p2p")),
            )
        if active:
            return "channel", DeliveryTarget.channel(
                target_type=str(active.get("target_type", "feishu_chat")),
                chat_id=str(active["chat_id"]),
                chat_type=str(active.get("chat_type", "p2p")),
            )
        return "standalone", DeliveryTarget.standalone()

    def schedule_trigger(
        self,
        *,
        trigger_type: str,
        timezone_name: str,
        at: Optional[str] = None,
        every: Optional[int] = None,
        unit: Optional[str] = None,
        time_of_day: Optional[str] = None,
        day_of_week: Optional[str] = None,
        day_of_month: Optional[int] = None,
        signal_name: Optional[str] = None,
    ):
        """The trigger a flat tool call asks for, in the interface's vocabulary.

        A tool call has one ``at`` because whoever writes one does not
        distinguish "the moment this runs, once" from "the anchor an interval
        counts from"; the interface has two names because a stored interval
        keeps its anchor under ``anchor_at``.  This is where the two meet, and
        it is the only place either is translated -- the rules themselves are
        the builder's, shared with the schedule editor, so a tool cannot come
        to mean something different by "每周三 09:00" than the form does.
        """
        from agent.scheduler import trigger_from_body

        kind = str(trigger_type).strip().lower()
        return trigger_from_body(
            {
                "trigger_type": kind,
                "timezone_name": timezone_name,
                "at": at,
                # Only an interval reads this name, and only it has an anchor
                # to name.
                "anchor_at": at if kind == "interval" else None,
                "every": every,
                "unit": unit,
                "time_of_day": time_of_day,
                "day_of_week": day_of_week,
                "day_of_month": day_of_month,
                "signal_name": signal_name,
            },
            signal_problem=self._schedule_store().describe_signal_problem,
        )

    @staticmethod
    def describe_trigger_when(trigger) -> str:
        """How to say *when* a task runs, in the terms the trigger uses.

        Returns a fragment meant to be followed by the action ("发送消息…",
        "执行任务…"), or an empty string when there is no honest way to name a
        time -- which is not a bug to paper over with "None" but the normal
        case for a task that waits for a signal.
        """
        if trigger.trigger_type == "signal":
            name = str(trigger.payload.get("name", "")).strip()
            return f"收到信号「{name}」时"
        initial = trigger.initial_run_at()
        return f"将在 {initial.isoformat()} " if initial else ""

    def schedule_create(
        self,
        name: str,
        trigger_type: str,
        prompt: str = "",
        action_type: str = "message",
        message_text: Optional[str] = None,
        instruction: Optional[str] = None,
        job_name: Optional[str] = None,
        timezone_name: str = LOCAL_TIMEZONE,
        at: Optional[str] = None,
        every: Optional[int] = None,
        unit: Optional[str] = None,
        time_of_day: Optional[str] = None,
        day_of_week: Optional[str] = None,
        day_of_month: Optional[int] = None,
        signal_name: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        permission_profile: Optional[str] = None,
        workspace_root: Optional[str] = None,
        criteria: Optional[list[str]] = None,
        verify_command: Optional[str] = None,
        produces: Optional[list[str]] = None,
        intent: str = "",
    ) -> dict[str, Any]:
        trigger = self.schedule_trigger(
            trigger_type=trigger_type,
            timezone_name=timezone_name,
            at=at,
            every=every,
            unit=unit,
            time_of_day=time_of_day,
            day_of_week=day_of_week,
            day_of_month=day_of_month,
            signal_name=signal_name,
        )
        resolved_mode, target = self.schedule_target(delivery_mode)
        from agent.scheduler import Acceptance, NewScheduledTask

        # A task woken by a signal is still a scheduled task in every way that
        # matters -- it just has no date to name.  Saying when it runs is
        # therefore a different sentence, not a missing one, and the summary
        # should read as "it will run", not as "it will run at None".
        when_text = self.describe_trigger_when(trigger)
        noun = "信号任务" if trigger.trigger_type == "signal" else "定时任务"
        job_noun = f"系统{noun}"
        task_kind, payload = self.action_payload(
            action_type,
            prompt=prompt,
            message_text=message_text,
            instruction=instruction,
            job_name=job_name,
        )
        if task_kind == "message":
            summary_text = (
                f"已设置好{noun}！{when_text}发送消息“{payload['message_text']}”。"
                if when_text
                else f"已设置好{noun}，会发送消息“{payload['message_text']}”。"
            )
        elif task_kind == "agent_prompt":
            summary_text = (
                f"已设置好{noun}！{when_text}执行任务：{payload['prompt']}"
                if when_text
                else f"已设置好{noun}，会执行任务：{payload['prompt']}"
            )
        else:
            summary_text = (
                f"已设置好{job_noun}！{when_text}执行 {payload['job_name']}。"
                if when_text
                else f"已设置好{job_noun}，会执行 {payload['job_name']}。"
            )

        # The envelope is resolved here rather than left to the dataclass
        # defaults.  Those defaults (``inherit`` plus the process working
        # directory) describe a posture, but not one anybody chose: the task
        # the user asked for in conversation would run under whatever the
        # global config happened to say, writing wherever the gateway
        # happened to be started from.  Naming both explicitly is what makes
        # "which profile is this?" answerable from the task's own definition.
        from agent.scheduler.profiles import (
            PERMISSION_PROFILES,
            resolve_permission_profile,
        )

        requested_profile = str(permission_profile or "").strip()
        if requested_profile and requested_profile not in PERMISSION_PROFILES:
            # Refused, not silently substituted.  A narrower profile would
            # still be an envelope nobody picked, and the caller that asked
            # for it deserves to hear that the name is wrong rather than
            # discover the substitution when the task fails at 3am.
            raise ValueError(
                f"Unsupported permission_profile '{requested_profile}'; "
                "expected one of: " + ", ".join(PERMISSION_PROFILES)
            )
        profile = resolve_permission_profile(requested_profile)

        explicit_workspace = str(workspace_root or "").strip()
        chosen_workspace = (
            Path(explicit_workspace).expanduser().resolve(strict=False)
            if explicit_workspace
            else self._chosen_workspace_root()
        )
        if profile.requires_workspace_root and chosen_workspace is None:
            raise ValueError(
                f"权限策略「{profile.label}」需要显式指定项目文件夹，"
                "不能回落到服务进程的当前目录"
            )
        task_workspace = chosen_workspace or self._active_workspace_root()
        if task_kind == "agent_prompt" and not task_workspace.is_dir():
            raise ValueError(f"项目文件夹不存在：{task_workspace}")

        store = self._schedule_store()
        acceptance = Acceptance(
            criteria=[str(item) for item in (criteria or []) if str(item).strip()],
            verify_command=str(verify_command or "").strip(),
        )
        new_task = NewScheduledTask(
            name=name,
            kind=task_kind,
            trigger=trigger,
            payload=payload,
            delivery_mode=resolved_mode,
            delivery_target=target,
            workspace_root=str(task_workspace),
            permission_profile=profile.key,
            acceptance=acceptance,
            produces=normalize_products(produces),
            # The words that were checked by the executor's quote test, kept on
            # the row so the task can still answer "why am I here" long after
            # the conversation that asked for it is gone.  The check and the
            # record read the same string on purpose: a task saved with a quote
            # nobody verified would be a claim about the past that nothing
            # backs up.
            request_quote=str(intent or "").strip(),
        )
        task = store.find_matching_task(new_task)
        existing = task is not None
        if task is None:
            task = store.create_task(new_task)
        return self._ok(
            task={
                "id": task.id,
                "name": task.name,
                "kind": task.kind,
                "delivery_mode": task.delivery_mode,
                "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                # Echoed back so the caller can confirm the envelope that was
                # actually stored instead of assuming the defaults held.
                "permission_profile": task.permission_profile,
                "workspace_root": task.workspace_root,
                "acceptance": task.acceptance.to_dict(),
                # Echoed for the same reason the envelope is, and it carries
                # more: the caller that wrote these paths needs to see the
                # stored list, because that list is what the run will be told
                # to write and what a downstream step will be handed.
                "produces": list(task.produces),
                "db_path": str(shared.SCHEDULER_DB_FILE),
                "existing": existing,
            },
            summary_text=(
                f"{summary_text}\n运行权限：{profile.label}；项目文件夹：{task_workspace}"
                f"{self.describe_products(task.produces)}"
                f"{self.describe_acceptance(task.acceptance)}"
            ),
        )

    @staticmethod
    def describe_products(produces: Any) -> str:
        """What this task promises to leave behind, in one line for the user.

        Said out loud for the same reason the criterion is, and with one more:
        these paths are what the run will be *told* to write, so a declaration
        the caller cannot see is a run writing files somewhere nobody asked
        for -- or, when it is wrong, a task that fails every night against a
        path that was never going to exist.
        """
        declared = normalize_products(produces)
        if not declared:
            return ""
        return "\n这次运行要产出的文件：" + "、".join(declared)

    @staticmethod
    def describe_acceptance(acceptance: Any) -> str:
        """What this task will be judged by, in one line for the user.

        Said out loud rather than stored quietly, because a criterion nobody
        was told about is indistinguishable from no criterion -- and the
        difference decides whether a failed run looks like a bug.
        """
        criteria = list(getattr(acceptance, "criteria", []) or [])
        command = str(getattr(acceptance, "verify_command", "") or "").strip()
        if not criteria and not command:
            return ""
        parts = []
        if criteria:
            parts.append("；".join(str(item) for item in criteria))
        if command:
            parts.append(f"验收命令：{command}")
        return "\n判定成功的依据：" + "；".join(parts)

    @staticmethod
    def action_payload(
        action_type: Optional[str],
        *,
        prompt: str = "",
        message_text: Optional[str] = None,
        instruction: Optional[str] = None,
        job_name: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        """Translate a tool's action vocabulary into a task kind and payload.

        One definition, because two callers need it: creating a single
        scheduled task, and creating each step of a workflow.  A workflow whose
        steps understood ``action_type`` differently from a standalone task
        would be a second dialect of the same language.

        The content field names the action when ``action_type`` is left out:
        a step carrying ``instruction`` is an agent task, one carrying
        ``message_text`` is a message.  A workflow step has no ``prompt`` to
        fall back on, so defaulting to message regardless refused the obvious
        spelling -- ``instruction`` alone -- with a complaint about a field its
        author never used, and the chain was never built.
        """
        normalized = str(action_type or "").strip().lower()
        if not normalized and not str(message_text or "").strip():
            if str(instruction or "").strip():
                normalized = "agent_task"
            elif str(job_name or "").strip():
                normalized = "system_job"
        normalized = normalized or "message"
        if normalized == "message":
            text = str(message_text or prompt).strip()
            if not text:
                raise ValueError("`message_text` is required for message actions")
            return "message", {"message_text": text}
        if normalized == "agent_task":
            text = str(instruction or prompt).strip()
            if not text:
                raise ValueError("`instruction` is required for agent_task actions")
            return "agent_prompt", {"prompt": text}
        if normalized == "system_job":
            text = str(job_name or "").strip()
            if not text:
                raise ValueError("`job_name` is required for system_job actions")
            return "system_job", {"job_name": text}
        raise ValueError(f"Unsupported action_type '{action_type}'")

    def schedule_list(self) -> dict[str, Any]:
        """Every task, with how its last run ended.

        The outcome travels with the task because a scheduled task is created
        in one conversation and then runs in none.  Without it this list cannot
        tell a task that has been succeeding every night from one that has
        failed every night since the day it was made, and the two answers are
        identical -- which is how a chain the agent built can be broken from
        the first night and never once be noticed.

        ``workflow_id``/``step_key`` are here for the same reason: from the
        task table a step and a standalone task look alike, and the difference
        is what says who owns the definition (a step cannot be deleted or
        retriggered on its own) and where to look when it stops.
        """
        store = self._schedule_store()
        tasks = store.list_tasks()
        return self._ok(
            count=len(tasks),
            items=[
                {
                    "id": task.id,
                    "name": task.name,
                    "kind": task.kind,
                    "delivery_mode": task.delivery_mode,
                    "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                    "enabled": task.enabled,
                    "workflow_id": str(getattr(task, "workflow_id", "") or ""),
                    "step_key": str(getattr(task, "step_key", "") or ""),
                    "produces": list(getattr(task, "produces", []) or []),
                    "last_run": _run_outcome(store.latest_run(task.id)),
                }
                for task in tasks
            ],
        )

    def schedule_runs(self, task_id: str, limit: int = 10) -> dict[str, Any]:
        """What a task's runs actually did, newest first.

        The other half of ``schedule_create``.  An unattended run happens when
        nobody is watching, and where it ended up exists in exactly one place:
        the run history.  ``schedule_list`` can only say the task exists, so
        without this tool the agent builds work it can never observe -- it
        cannot tell a chain that ran cleanly from one that has never once
        completed, cannot say why a step stopped, and cannot fix either.

        Addressed by task id, which the two list tools hand out.  The run's
        ``error`` is where the reason is, and it is truncated: the whole of it,
        and the text the run produced, are on disk at ``output_path`` -- read
        that for anything longer than a reason.

        ``acceptance`` is echoed because it is usually the answer.  The most
        common way a run fails is that the criterion it was given cannot be
        satisfied -- a verify command naming a file that step never writes --
        and that is a defect in the definition rather than in the work, so it
        has to be fixable by editing the task rather than by retrying it; the
        whole definition is returned here so that edit can be written against
        what the task actually says.
        """
        wanted = str(task_id or "").strip()
        if not wanted:
            return self._error(
                "task_id 不能为空；用 schedule_list 或 workflow_list 里的 id。"
            )
        store = self._schedule_store()
        task = store.get_task(wanted)
        if task is None:
            return self._error(
                f"没有 id 为「{wanted}」的任务。用 schedule_list 看现有任务。"
            )

        runs = store.list_runs(task.id)
        kept = list(reversed(runs))[: max(1, min(int(limit or 10), 50))]
        from agent.scheduler import task_definition_payload

        return self._ok(
            task={
                "id": task.id,
                # The whole definition, not a summary of it.  An edit has to
                # know what it is editing, and until this was a full definition
                # there was no way to read back the prompt, the trigger or the
                # permission posture of a task one had created -- so "fix it"
                # meant "rewrite it blind", and rewriting blind is how a field
                # nobody mentioned gets erased.  It is exactly the vocabulary
                # ``schedule_update`` accepts, which is the other half of the
                # point: everything above this line can be sent straight back,
                # one field changed or none.
                **task_definition_payload(task),
                # Everything below is readable and not writable, and each line
                # names who owns it instead.  They are here because a diagnosis
                # needs them -- "is this thing even on", "does it message
                # anyone", "which chain is it part of" are the first questions
                # about a task that is misbehaving -- and outside the payload
                # because that function is defined as the set an edit may set,
                # and a key in it that the schema then rejects as unknown is
                # worse than one that is obviously out of reach.
                "workflow_id": str(getattr(task, "workflow_id", "") or ""),
                "step_key": str(getattr(task, "step_key", "") or ""),
                "enabled": bool(task.enabled),
                "delivery_mode": str(getattr(task, "delivery_mode", "") or ""),
                "delivery_target": (
                    {
                        "target_type": str(task.delivery_target.target_type),
                        "payload": dict(task.delivery_target.payload or {}),
                    }
                    if getattr(task, "delivery_target", None) is not None
                    and str(getattr(task, "delivery_mode", "")) == "channel"
                    else None
                ),
                "request_quote": str(getattr(task, "request_quote", "") or ""),
            },
            run_count=len(runs),
            returned=len(kept),
            runs=[_run_detail(run) for run in kept],
            note=(
                f"这个任务共有 {len(runs)} 次运行，只返回最近 {len(kept)} 次。"
                if len(runs) > len(kept)
                else ""
            ),
        )

    def schedule_delete(self, task_id: str, intent: str = "") -> dict[str, Any]:
        """Delete a task, and its run history with it.

        ``intent`` is declared, not read: the executor refuses the call without
        it, and this function never inspects it.  Same shape as
        ``schedule_cancel``, and declared at registration rather than here.

        Which tasks may be deleted here is the store's answer, not this
        tool's: a running task, and a step of a workflow that still exists,
        are refused with a reason.  Asking the store rather than checking
        first is what keeps the rule in one place -- the interface refuses
        the same two cases, and a step left behind by a deleted workflow is
        deletable from both.

        Deleting is not editing, and reaching for this to fix a definition is
        the mistake worth naming: it takes the run history with it, and the id
        goes with that -- which is the id every downstream step is subscribed
        to, so the edges come down too.  A wrong definition is
        ``schedule_update``'s to correct.
        """
        store = self._schedule_store()
        store.delete_task(task_id)
        return self._ok(task_id=task_id, deleted=True)

    def schedule_edit_context(self, existing: Any = None):
        """What building a definition needs, from an unattended tool call.

        The signal check is here because a tool call can name a signal, and a
        subscription that matches nothing is a task that never runs -- checked
        by the same rule the editor uses, so the two doors cannot disagree
        about which names are real.

        The skill catalogue is deliberately absent: re-validating a value that
        is merely being carried over would make an unrelated edit fail the day
        somebody retires a skill, and a task pinned to a skill that no longer
        exists is exactly the kind of thing an edit has to be able to fix.

        *existing* is what makes the model check safe to have here.  A model id
        that is newly named is checked against the routing table, which is the
        same table the interface checks against -- a task pinned to a foreign
        provider's group fails at 3am with the provider's 400, naming a model
        nobody chose, and that refusal belongs where somebody is looking.  A
        model id that is the one already on the row is not re-checked, so
        reading a definition and sending it back does not fail just because
        that model has since been retired.
        """
        from agent.scheduler import EditContext

        stored_model = getattr(existing, "model_override", None)

        def validate_model(raw_model: Any) -> Optional[str]:
            model = str(raw_model).strip() if raw_model is not None else ""
            if not model or model == stored_model:
                # Clearing it, or carrying the very value the row already has.
                return model or None
            try:
                from agent.config import load_config
                from agent.core.transport import routable_model_ids

                cfg, _ = load_config()
                configured = routable_model_ids(cfg)
            except Exception:
                # A configuration that cannot be read is not evidence that the
                # id is wrong; refusing on it would block edits for a reason
                # the caller cannot act on.
                return model
            if configured and model not in configured:
                raise ValueError(
                    f"模型「{model}」不在当前配置的模型里，不能固定给它。"
                    "可用的是当前 provider 分组里的模型 id。"
                )
            return model

        return EditContext(
            chosen_workspace_root=self._chosen_workspace_root(),
            fallback_workspace_root=self._active_workspace_root(),
            signal_problem=self._schedule_store().describe_signal_problem,
            model_validator=validate_model,
        )

    def schedule_update(
        self, task_id: str, intent: str = "", **patch: Any
    ) -> dict[str, Any]:
        """Change part of an existing task and leave the rest of it alone.

        The verb this toolset was missing.  Everything an unattended task is --
        its prompt, its criterion, the files it declares, its permission
        posture -- could be written only at the moment of creation, so a task
        whose *definition* was wrong could not be corrected at all.  The only
        move was to delete it and build it again, which throws away the run
        history that says what it did; the real scheduler shows the habit, in
        nine steps left behind by workflows that were rebuilt rather than
        edited.  Meanwhile ten of the seventeen runs waiting for a person were
        waiting because a definition was wrong -- a verify command naming a
        file that step never writes -- and nothing could write the fix.

        Partial on purpose.  A field the call does not name keeps the value the
        task already has, and the whole resulting definition comes back, so a
        caller can see what it now says instead of assuming.  ``schedule_runs``
        returns the definition in this same vocabulary, so what is read there
        can be handed straight back here.

        Three things it will not do, and each is somebody else's: which
        workflow a task is a step of belongs to the graph (edit the workflow),
        who asked for it belongs to the past (it is not a setting), and whether
        it is switched on belongs to ``schedule_set_enabled`` -- so the one
        thing a person reaches for at 3am has exactly one name.

        Deleting still exists, and does something else.
        """
        from agent.scheduler import (
            describe_edit,
            mirror_step_edit,
            step_owns_no_trigger,
            task_definition_payload,
            task_from_body,
        )

        wanted = str(task_id or "").strip()
        if not wanted:
            return self._error(
                "task_id 不能为空；用 schedule_list 或 schedule_runs 里的 id。"
            )
        store = self._schedule_store()
        existing = store.get_task(wanted)
        if existing is None:
            return self._error(
                f"没有 id 为「{wanted}」的任务。用 schedule_list 看现有任务。"
            )
        body = dict(patch)
        if not body:
            return self._error(
                "这次调用没有写任何要改的字段。要改哪个就写哪个：name、prompt、"
                "criteria、verify_command、produces、trigger_type 等；"
                "只想开关任务用 schedule_set_enabled。"
            )
        # The flat tool vocabulary has one `at`, which for an interval is the
        # instant it counts from; the builder wants the interface's own name for
        # that.  Mapped here, the same way ``schedule_create`` maps it, so the
        # two tools cannot come to mean different things by `at`.
        if (
            str(body.get("trigger_type") or "").strip().lower() == "interval"
            and "at" in body
        ):
            body["anchor_at"] = body.pop("at")

        try:
            spec = task_from_body(
                body,
                existing,
                context=self.schedule_edit_context(existing),
                # A step that waits on upstreams does not own its trigger: the
                # graph says it waits for them.  The store answers which steps
                # those are, so this tool and the schedule editor cannot
                # disagree about it.
                keep_trigger=step_owns_no_trigger(store, existing),
            )
            updated = store.update_task(wanted, spec)
            if updated is not None:
                # The task row is what was just written; the graph is what the
                # next save of its workflow rebuilds the task from.  Without
                # this the edit survives exactly until somebody moves an edge.
                mirror_step_edit(store, updated)
        except (KeyError, ValueError, TypeError) as exc:
            return self._error(str(exc))
        if updated is None:
            return self._error(f"任务「{wanted}」在保存的过程中已经不存在了。")
        changed = describe_edit(existing, updated)
        return self._ok(
            task={"id": updated.id, **task_definition_payload(updated)},
            changed=changed,
            note=(
                "这次调用没有改变任何字段——写进去的值和原来一样。"
                if not changed
                else ""
            ),
        )

    def schedule_set_enabled(
        self, task_id: str, enabled: bool, intent: str = ""
    ) -> dict[str, Any]:
        """Switch one task on or off.

        The refusal is the store's, and it is the one worth knowing: a step of
        a workflow that still exists cannot be switched on its own, because its
        switch is rewritten from the workflow's every time that workflow is
        saved -- so flipping it here would be a promise the next save breaks.
        Pausing the workflow is the switch that holds, and the message says so.

        Its own tool rather than a field on ``schedule_update`` because this is
        the switch somebody reaches for while something is going wrong, and it
        should be one name and one call rather than an argument to a bigger
        one.
        """
        store = self._schedule_store()
        wanted = str(task_id or "").strip()
        if not wanted:
            return self._error("task_id 不能为空；用 schedule_list 里的 id。")
        task = store.get_task(wanted)
        if task is None:
            return self._error(
                f"没有 id 为「{wanted}」的任务。用 schedule_list 看现有任务。"
            )
        try:
            store.set_enabled(wanted, bool(enabled))
        except ValueError as exc:
            return self._error(str(exc))
        after = store.get_task(wanted) or task
        return self._ok(
            task_id=wanted,
            name=after.name,
            enabled=bool(after.enabled),
            workflow_id=str(getattr(after, "workflow_id", "") or ""),
            step_key=str(getattr(after, "step_key", "") or ""),
            note=(
                "已启用：到时间它就会跑。"
                if after.enabled
                else "已停用：它不会再被触发，已经在跑或排队的运行不受影响。"
            ),
        )

    def schedule_run(
        self, task_id: str, run_id: str = "", intent: str = ""
    ) -> dict[str, Any]:
        """Start a run now: this task as it is, or that run's snapshot again.

        Two questions with one verb, and the difference is the whole answer.
        With no ``run_id`` the task runs as it is *now*, which is how a fix is
        confirmed -- ``schedule_update`` and then this.  With a ``run_id`` it
        re-runs the snapshot that run was started with, so the outcome is about
        the work rather than about the definition: a flake gets another try
        unchanged, and a fix cannot be mistaken for one.

        A task that is already running is refused rather than queued.  The run
        in flight owns the row, so a second one would either wait invisibly or
        collide with it, and both are worse than being told.
        """
        store = self._schedule_store()
        wanted = str(task_id or "").strip()
        if not wanted:
            return self._error("task_id 不能为空；用 schedule_list 里的 id。")
        source = str(run_id or "").strip()
        try:
            claimed = (
                store.claim_retry(wanted, source) if source else store.claim_task_now(wanted)
            )
        except ValueError as exc:
            return self._error(str(exc))
        if claimed is None:
            task = store.get_task(wanted)
            if task is None:
                return self._error(
                    f"没有 id 为「{wanted}」的任务。用 schedule_list 看现有任务。"
                )
            if task.active_run_id:
                return self._error(
                    f"「{task.name}」正在运行（运行 {task.active_run_id}）。"
                    "要停掉它用 schedule_cancel；要看它跑成什么样用 schedule_runs。"
                )
            return self._error(
                f"没找到可重跑的运行「{source}」，或者它还在运行中。"
                "用 schedule_runs 看这个任务有哪些运行、各自是什么状态。"
            )
        return self._ok(
            task_id=claimed.task.id,
            run_id=claimed.run.id,
            trigger_source=claimed.run.trigger_source,
            attempt=claimed.run.attempt,
            note=(
                "已经排上队了。无人值守的运行要几分钟到几小时，"
                "用 schedule_runs 查这个任务就能看到结果。"
            ),
        )

    def schedule_cancel(
        self, task_id: str, run_id: str = "", intent: str = ""
    ) -> dict[str, Any]:
        """Ask a run that is in flight to stop.

        A request, not a kill.  The run notices at its next checkpoint and stops
        itself, so the answer says *asked* rather than *stopped* -- reporting a
        completed cancellation would be describing something this side of the
        process cannot see.

        With no ``run_id`` it means the task's current run, which is the only
        one it can mean: a task holds one run at a time.  Saying "the one that
        is running" rather than making the caller look it up first matters when
        it is already an hour into a timeout.
        """
        store = self._schedule_store()
        wanted = str(task_id or "").strip()
        if not wanted:
            return self._error("task_id 不能为空；用 schedule_list 里的 id。")
        task = store.get_task(wanted)
        if task is None:
            return self._error(
                f"没有 id 为「{wanted}」的任务。用 schedule_list 看现有任务。"
            )
        source = str(run_id or "").strip() or str(task.active_run_id or "")
        if not source:
            return self._error(
                f"「{task.name}」现在没有在运行，没有可取消的东西。"
                "要停掉它以后的运行，用 schedule_set_enabled(enabled=false)。"
            )
        if not store.request_cancel(wanted, source):
            return self._error(
                f"运行「{source}」不在运行中，或者不属于这个任务。"
                "用 schedule_runs 看它现在的状态。"
            )
        return self._ok(
            task_id=wanted,
            run_id=source,
            cancel_requested=True,
            note="已经请求取消：它会在下一个检查点停下。用 schedule_runs 看它最后停在哪。",
        )

    def workflow_steps(
        self,
        raw_steps: list[dict[str, Any]],
        *,
        existing: Any = None,
        chosen_workspace: Optional[Path] = None,
        fallback_workspace: Optional[Path] = None,
    ) -> list[Any]:
        """The steps a tool call describes, given the graph they replace.

        One builder for creating a chain and for editing one, because the two
        are the same sentence with a different starting point.  A step whose
        ``key`` matches one already in the stored graph keeps every field the
        call does not mention -- which is what makes an edit an edit.  Without
        it, renaming a step would reset the criterion that decides whether the
        rest of the chain runs at all, and every field a tool call has no word
        for -- a retry policy, a skill list, a timeout -- would be written back
        as its default by the same save.

        The dialect is the tools' own: flat trigger fields, and
        ``instruction`` / ``message_text`` for content.  It is not the graph
        editor's, because the caller here is a model writing a tool call.  Both
        dialects still reach the same rules about what a trigger means and what
        a criterion is.
        """
        from agent.scheduler import Acceptance, WorkflowStep
        from agent.scheduler.profiles import resolve_permission_profile

        fallback = fallback_workspace or self._active_workspace_root()
        chosen = (
            chosen_workspace
            if chosen_workspace is not None
            else self._chosen_workspace_root()
        )
        existing_by_key = {
            str(getattr(step, "key", "")).strip(): step
            for step in (getattr(existing, "steps", None) or [])
        }

        built: list[WorkflowStep] = []
        for index, raw in enumerate(raw_steps, start=1):
            key = str(raw.get("key") or "").strip()
            if not key:
                raise ValueError(f"第 {index} 个步骤缺少 key")
            label = f"步骤「{key}」"
            previous = existing_by_key.get(key)

            def kept(field_name: str, fallback_value: Any) -> Any:
                """The value for *field_name*: what the call said, else what was."""
                if field_name in raw and raw.get(field_name) is not None:
                    return raw.get(field_name)
                if previous is not None:
                    return getattr(previous, field_name, fallback_value)
                return fallback_value

            asked_action = str(raw.get("action_type") or "").strip()
            names_content = any(
                str(raw.get(field_name) or "").strip()
                for field_name in ("instruction", "message_text", "job_name")
            )
            if previous is not None and not names_content and not asked_action:
                kind, payload = previous.kind, dict(previous.payload)
            else:
                kind, payload = self.action_payload(
                    asked_action or None,
                    instruction=raw.get("instruction"),
                    message_text=raw.get("message_text"),
                    job_name=raw.get("job_name"),
                )

            depends_on = [
                str(item).strip()
                for item in (kept("depends_on", None) or [])
                if str(item).strip()
            ]
            # Only an entry step carries a trigger.  A dependent step's trigger
            # *is* its upstreams, and giving it both would be two answers to
            # "when does this run".  Refused rather than ignored: dropping a
            # `trigger_type` the caller wrote down would answer "run this daily
            # at 10" with a step that runs at no particular time, and say
            # nothing about having done so.
            trigger_fields = {
                "trigger_type": raw.get("trigger_type"),
                "signal_name": raw.get("signal_name"),
                "at": raw.get("at"),
                "every": raw.get("every"),
                "unit": raw.get("unit"),
                "time_of_day": raw.get("time_of_day"),
                "day_of_week": raw.get("day_of_week"),
                "day_of_month": raw.get("day_of_month"),
            }
            asked_when = [
                field_name
                for field_name, value in trigger_fields.items()
                if value is not None and str(value).strip()
            ]
            if depends_on and asked_when:
                raise ValueError(
                    f"{label}既有上游（{'、'.join(depends_on)}），又写了"
                    f"{'、'.join(asked_when)}。它的触发方式由上游决定，"
                    "不能再另外指定；否则「什么时候运行」会有两个答案。"
                    "如果这一步真的需要按时间运行，把它改成没有上游的入口步骤。"
                )
            trigger = None
            if not depends_on:
                asked_type = str(raw.get("trigger_type") or "").strip()
                # A step that was driven by its upstreams has no trigger of its
                # own to keep: what it carries is the fan-in, written by the
                # graph, and it means nothing once the upstreams are gone.
                keeper = (
                    previous.trigger
                    if previous is not None and previous.is_entry()
                    else None
                )
                names_when = bool(
                    asked_when
                    or str(raw.get("timezone_name") or "").strip()
                )
                if not names_when:
                    if keeper is None:
                        raise ValueError(
                            f"{label}没有上游，必须指定触发方式（trigger_type）"
                        )
                    trigger = keeper
                elif asked_type:
                    trigger = self.schedule_trigger(
                        trigger_type=asked_type,
                        timezone_name=str(
                            raw.get("timezone_name") or LOCAL_TIMEZONE
                        ),
                        at=raw.get("at"),
                        every=raw.get("every"),
                        unit=raw.get("unit"),
                        time_of_day=raw.get("time_of_day"),
                        day_of_week=raw.get("day_of_week"),
                        day_of_month=raw.get("day_of_month"),
                        signal_name=raw.get("signal_name"),
                    )
                elif keeper is not None:
                    # No type named: the stored one is what the other fields are
                    # answering about, so moving a step's daily report an hour
                    # does not oblige the caller to restate that it is daily.
                    from agent.scheduler import trigger_from_body

                    trigger = trigger_from_body(
                        dict(raw, trigger_type=keeper.trigger_type),
                        keeper,
                        signal_problem=self._schedule_store().describe_signal_problem,
                    )
                else:
                    raise ValueError(
                        f"{label}没有上游，必须指定触发方式（trigger_type）"
                    )

            profile = resolve_permission_profile(
                str(kept("permission_profile", "inherit") or "inherit")
            )
            own_workspace = str(raw.get("workspace_root") or "").strip()
            if own_workspace:
                step_workspace = str(
                    Path(own_workspace).expanduser().resolve(strict=False)
                )
            elif previous is not None and "workspace_root" not in raw:
                # Kept exactly, empty string included.  An empty one means "as
                # the entry step", and materialising the inherited folder into
                # it would turn a step that follows the chain's folder into one
                # pinned to wherever it happened to be edited.
                step_workspace = str(previous.workspace_root or "")
            else:
                step_workspace = str(chosen if chosen is not None else fallback)
            if profile.requires_workspace_root and not step_workspace:
                # Same refusal, and for the same reason, as ``schedule_create``:
                # a write-granting step pinned to the process working directory
                # would write somewhere nobody can derive from the graph.
                raise ValueError(
                    f"{label}使用了权限策略「{profile.label}」，需要显式指定项目文件夹，"
                    "不能回落到服务进程的当前目录"
                )

            if previous is not None and "delivery_mode" not in raw:
                mode, target = previous.delivery_mode, previous.delivery_target
            else:
                mode, target = self.schedule_target(raw.get("delivery_mode"))

            # Carried from the stored step when the call has no word for them.
            # A tool call cannot express a retry policy, a skill list or a
            # timeout, and omitting them would not mean "unchanged" -- the step
            # would be rebuilt from the dataclass default, so editing a step's
            # criterion would quietly reset how hard the chain tries.
            carried: dict[str, Any] = {}
            if previous is not None:
                carried = {
                    "context_policy": previous.context_policy,
                    "model_override": previous.model_override,
                    "timeout_seconds": int(previous.timeout_seconds),
                    "selected_skills": list(previous.selected_skills),
                    "retry_policy": dict(previous.retry_policy),
                }

            if "criteria" in raw or "verify_command" in raw:
                acceptance = Acceptance(
                    criteria=[
                        str(item)
                        for item in (raw.get("criteria") or [])
                        if str(item).strip()
                    ],
                    verify_command=str(raw.get("verify_command") or "").strip(),
                )
            else:
                acceptance = getattr(previous, "acceptance", None) or Acceptance()

            built.append(
                WorkflowStep(
                    key=key,
                    name=str(kept("name", "") or key or label).strip(),
                    kind=kind,
                    payload=payload,
                    depends_on=depends_on,
                    trigger=trigger,
                    workspace_root=step_workspace,
                    permission_profile=profile.key,
                    acceptance=acceptance,
                    produces=(
                        normalize_products(raw.get("produces"))
                        if "produces" in raw
                        else list(getattr(previous, "produces", None) or [])
                    ),
                    delivery_mode=mode,
                    delivery_target=target,
                    **carried,
                )
            )
        return built

    def workflow_create(
        self,
        name: str,
        steps: list[dict[str, Any]],
        description: str = "",
        workspace_root: Optional[str] = None,
        intent: str = "",
    ) -> dict[str, Any]:
        """Create a chain of tasks whose order is the graph, not a clock.

        The whole reason this exists as one tool rather than as "call
        schedule_create a few times" is the edges.  Two tasks that happen to
        subscribe to each other's signals look identical, from the signals
        alone, to two tasks that were meant to be one job -- and the
        difference is exactly what a failure has to know: when a step fails,
        the steps below it must not run, and only the graph can say which
        those are.  Building the chain here records that.

        Every refusal comes from the same validator the store uses on write,
        so a graph rejected here is rejected before any row exists.  A
        workflow row with no tasks behind it is the failure mode this tool is
        shaped to make impossible: it looks like a plan and runs nothing.
        """
        from agent.scheduler import Workflow

        raw_steps = [item for item in (steps or []) if isinstance(item, dict)]
        if not raw_steps:
            raise ValueError("`steps` 至少要有一个步骤")

        workflow = Workflow(
            name=str(name or "").strip(),
            description=str(description or "").strip(),
            steps=self.workflow_steps(raw_steps),
            # Checked by the executor's quote test before this ran; stored on the
            # workflow row so every step under it can say which sentence in a
            # conversation put the whole chain there.
            request_quote=str(intent or "").strip(),
        )
        store = self._schedule_store()
        created = store.create_workflow(workflow)
        tasks = store.step_tasks(created.id)
        return self._ok(
            workflow={
                "id": created.id,
                "name": created.name,
                "description": created.description,
                "enabled": created.enabled,
                "steps": [
                    {
                        "key": step.key,
                        "name": step.name,
                        "kind": step.kind,
                        "depends_on": list(step.depends_on),
                        "trigger": (
                            json.loads(step.trigger.to_json())
                            if step.trigger is not None
                            else None
                        ),
                        "task_id": tasks[step.key].id if step.key in tasks else None,
                        "workspace_root": step.workspace_root,
                        "permission_profile": step.permission_profile,
                        "acceptance": step.acceptance.to_dict(),
                        "produces": list(step.produces),
                    }
                    for step in created.steps
                ],
                "db_path": str(shared.SCHEDULER_DB_FILE),
            },
            summary_text=self.describe_workflow(created),
        )

    @staticmethod
    def describe_workflow(workflow: Any) -> str:
        """The chain, in the order it will run, as a sentence.

        Written out rather than summarised as a count, because the two things
        that go wrong with a graph -- a step in the wrong place, and a step
        whose criterion was not what the person meant -- are both visible in
        the sequence and invisible in "created 3 steps".
        """
        lines = [f"已创建流程「{workflow.name}」，共 {len(workflow.steps)} 个步骤："]
        for step in workflow.steps:
            upstreams = "、".join(str(item) for item in step.depends_on)
            when = f"在 {upstreams} 成功后运行" if upstreams else "按自身触发方式运行"
            lines.append(f"- {step.key}（{step.name}）：{when}")
            declared = normalize_products(getattr(step, "produces", None))
            if declared:
                # Said here for the same reason the criterion below is: these
                # paths are what the step will be *told* to write, so a
                # declaration the caller cannot see is a step that fails every
                # night against a path it was never asked about.
                lines.append(f"  产出文件：{'、'.join(declared)}")
            criteria = list(getattr(step.acceptance, "criteria", []) or [])
            command = str(getattr(step.acceptance, "verify_command", "") or "").strip()
            if criteria or command:
                judged = "；".join(str(item) for item in criteria)
                if command:
                    judged = f"{judged}；验收命令：{command}" if judged else f"验收命令：{command}"
                lines.append(f"  判定成功的依据：{judged}")
        return "\n".join(lines)

    def workflow_list(self) -> dict[str, Any]:
        """Every chain, with each step's task and how its last run ended.

        The graph alone is a drawing.  What a caller needs after building one --
        or after being told a chain exists -- is which step is where it is
        supposed to be: a step that has never run, a step whose last run failed,
        and a step that is waiting on an upstream all look identical from the
        shape of the graph, and the difference is the whole of the answer to
        "is this working".

        ``task_id`` travels with each step because the run history is keyed by
        it: without it, "why did step three stop" is a question with no way to
        ask it.
        """
        store = self._schedule_store()
        workflows = store.list_workflows()
        items = []
        for workflow in workflows:
            step_tasks = store.step_tasks(workflow.id)
            steps = []
            for step in workflow.steps:
                task = step_tasks.get(step.key)
                steps.append(
                    {
                        "key": step.key,
                        "name": step.name,
                        "depends_on": list(step.depends_on),
                        "enabled": bool(task.enabled) if task is not None else False,
                        "task_id": task.id if task is not None else "",
                        "produces": list(getattr(step, "produces", []) or []),
                        "last_run": _run_outcome(
                            store.latest_run(task.id) if task is not None else None
                        ),
                    }
                )
            items.append(
                {
                    "id": workflow.id,
                    "name": workflow.name,
                    "description": workflow.description,
                    "enabled": workflow.enabled,
                    "steps": steps,
                }
            )
        return self._ok(count=len(items), items=items)

    def workflow_delete(self, workflow_id: str, intent: str = "") -> dict[str, Any]:
        """Stop a workflow, keeping the record that it ran.

        ``intent`` is declared, not read -- see ``_schedule_delete``.

        The tasks behind it are disabled rather than deleted, which is what the
        store does; this only says so, because "deleted" would suggest the run
        history went with it and somebody reading the log later would not know
        to look for it.  Nothing else refers to the graph afterwards, so the
        leftovers are ordinary tasks from that moment on: they show up in the
        task list and schedule_delete removes them.

        Reaching for this in order to *change* a chain is the mistake worth
        naming, and the real scheduler shows what it costs: nine steps left
        behind by workflows that were rebuilt rather than edited, each of them
        disabled but still there, and their run history stranded under step
        keys their successor does not use.  ``workflow_update`` changes the
        chain in place.
        """
        store = self._schedule_store()
        disabled = store.delete_workflow(workflow_id)
        return self._ok(
            workflow_id=workflow_id,
            deleted=True,
            disabled_task_ids=disabled,
        )

    def workflow_update(
        self,
        workflow_id: str,
        intent: str = "",
        name: Optional[str] = None,
        description: Optional[str] = None,
        enabled: Optional[bool] = None,
        steps: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Change a chain in place, keeping its steps' ids and run history.

        Add a step, remove one, move one, reword one -- without the chain
        becoming a different chain.  Steps are matched to the stored graph by
        ``key``, and that is the whole contract: a key that is already there is
        the same step and keeps everything this call does not mention; a key
        that is not there is a new step, and gets a new task; a stored key that
        is missing from the list is *removed*, and its task is switched off
        rather than deleted, so the record of what it did survives.

        The ids matter more than they look.  A step's task id appears in the
        signal names its downstream steps subscribe to, so rebuilding the chain
        instead of editing it -- which is what there was to do before this tool
        existed -- silently cuts every edge: the tasks that were there carry on
        subscribed to ids nothing emits, and the new ones subscribe to ids that
        have never run.  Keeping the key keeps the id, and keeping the id keeps
        the edges.

        ``steps`` may be left out entirely, which is how a rename or a pause is
        said without restating the whole graph.  When it is given it is the
        whole list, in the order the chain should run.  Same step language as
        ``workflow_create``: ``key``, ``depends_on``, ``instruction`` or
        ``message_text``, flat trigger fields on the entry step, and
        ``criteria`` / ``verify_command`` / ``produces`` per step.
        """
        from agent.scheduler import Workflow, WorkflowStep, validate_workflow_graph

        wanted = str(workflow_id or "").strip()
        if not wanted:
            return self._error(
                "workflow_id 不能为空；用 workflow_list 里的 id。"
            )
        store = self._schedule_store()
        existing = store.get_workflow(wanted)
        if existing is None:
            return self._error(
                f"没有 id 为「{wanted}」的流程。用 workflow_list 看现有流程。"
            )
        if name is None and description is None and enabled is None and steps is None:
            return self._error(
                "这次调用没有写任何要改的字段。可以改 name、description、"
                "enabled，或者给 steps 一整份新的步骤列表。"
            )

        if steps is None:
            built = list(existing.steps)
        else:
            raw_steps = [item for item in steps if isinstance(item, dict)]
            if not raw_steps:
                return self._error(
                    "steps 不能是空的：流程至少要有一个步骤。"
                    "如果要停掉整条流程，用 schedule_set_enabled 或 workflow_delete。"
                )
            try:
                built = self.workflow_steps(raw_steps, existing=existing)
            except (KeyError, ValueError, TypeError) as exc:
                return self._error(str(exc))

        graph = Workflow(
            name=str(name if name is not None else existing.name).strip(),
            description=str(
                description if description is not None else existing.description
            ).strip(),
            enabled=bool(enabled if enabled is not None else existing.enabled),
            steps=built,
            id=existing.id,
        )
        try:
            # Checked here as well as in the store, so a cycle comes back naming
            # the ring rather than as a failure from inside the write.
            validate_workflow_graph(graph.steps)
            updated = store.update_workflow(wanted, graph)
        except (KeyError, ValueError, TypeError) as exc:
            return self._error(str(exc))
        if updated is None:
            return self._error(f"流程「{wanted}」在保存的过程中已经不存在了。")

        before_keys = {str(step.key).strip() for step in existing.steps}
        after_keys = {str(step.key).strip() for step in updated.steps}
        added = sorted(after_keys - before_keys)
        removed = sorted(before_keys - after_keys)
        # ``task_id`` travels with each step for the same reason it does from
        # ``workflow_create``: it is what the run history is keyed by, and here
        # it is also the evidence for the one claim this tool makes -- that
        # editing a chain kept its steps' ids, and with them every edge.  A
        # reply that only listed keys would leave the caller to take that on
        # faith, at exactly the moment it is worth checking.
        step_tasks = store.step_tasks(updated.id)
        # Named rather than counted, because the two things that go wrong with
        # an edited graph are a step in the wrong place and a step that quietly
        # stopped existing -- and both are visible in the names and invisible in
        # "updated 4 steps".
        return self._ok(
            workflow={
                "id": updated.id,
                "name": updated.name,
                "description": updated.description,
                "enabled": updated.enabled,
                "steps": [
                    {
                        "key": step.key,
                        "name": step.name,
                        "depends_on": list(step.depends_on),
                        "produces": list(getattr(step, "produces", []) or []),
                        "task_id": (
                            step_tasks[step.key].id if step.key in step_tasks else None
                        ),
                    }
                    for step in updated.steps
                ],
                "db_path": str(shared.SCHEDULER_DB_FILE),
            },
            added_steps=added,
            removed_steps=removed,
            summary_text=self.describe_workflow(updated),
        )
