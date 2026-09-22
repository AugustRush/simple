"""Building a task definition out of a partial edit.

One rule, in one place.  Three callers used to hold a copy each -- the
schedule interface, the workflow interface, and the agent's own tools -- and
two of them had already drifted: only the interface checked a name's length
and whether a skill could be attached, so the same definition was acceptable
through one door and refused through another.

The rule is that **a field the body does not mention keeps the value it
already had**.  That is not a new convention here; it is the one the workflow
builder has used all along, written down at ``_workflow_step_from_body``: a
graph editor that edits the graph should not be able to erase a step's model
by not mentioning it.  The task builder was the exception, and the exception
cost real things.  ``_update_task_in_transaction`` rewrites every column from
the spec it is handed, so a field the builder left out of that spec was not
*skipped* -- it was written back as the dataclass default.  Editing a task's
name through the interface cleared the files it had declared it would produce,
and the run went on failing against a promise its own row no longer contained.

So :func:`task_from_body` is *total*: every field a task can hold is either
named by the body or read off the row.  :func:`task_definition_payload` is its
inverse, and the two round-trip in both directions::

    task_from_body({}, task) == task
    task_from_body(task_definition_payload(task), task) == task

The first says an edit that names nothing changes nothing.  The second says
the field list is complete -- a field the payload forgets is a field an edit
can silently reset, and the only way to notice is to have written the pair
down next to each other and checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agent.scheduler.models import (
    LOCAL_TIMEZONE,
    MAX_RETRY_ATTEMPTS,
    MAX_RETRY_BACKOFF_SECONDS,
    SIGNAL_MODE_ALL,
    SIGNAL_MODE_ANY,
    SIGNAL_MODES,
    Acceptance,
    DeliveryTarget,
    NewScheduledTask,
    TriggerSpec,
    acceptance_payload,
    normalize_products,
    resolve_timezone_name,
)
from agent.scheduler.profiles import PERMISSION_PROFILES, resolve_permission_profile

#: The body keys that describe *when* a task runs.  A body naming none of them
#: is not an answer about the trigger, and the stored spec is kept verbatim --
#: not rebuilt into an equivalent one.  The store decides whether a schedule
#: survives an edit by comparing the serialised trigger, so a rebuild that
#: happens to mean the same thing still reads as "the time changed" and is
#: answered by moving the next occurrence into the future, swallowing one that
#: was already due.
TRIGGER_BODY_FIELDS = (
    "trigger_type",
    "timezone_name",
    "trigger_from",
    "at",
    "every",
    "unit",
    "anchor_at",
    "time_of_day",
    "day_of_week",
    "day_of_month",
    "signal_name",
    "signal_names",
    "signal_mode",
)

CONTEXT_POLICIES = ("stateless", "task_history", "shared_memory")

#: Which payload key carries the content for each kind of task.
PAYLOAD_KEY = {
    "message": "message_text",
    "agent_prompt": "prompt",
    "system_job": "job_name",
}

#: ``action_type`` is the interface's word for a kind; the stored column uses
#: the kind's own name.  Two vocabularies, mapped once, here.
ACTION_KINDS = {
    "message": "message",
    "agent_task": "agent_prompt",
    "system_job": "system_job",
}

MAX_CONTENT_LENGTH = {"agent_prompt": 6000, "message": 2000}


@dataclass(frozen=True)
class EditContext:
    """What building a definition needs that only the caller knows.

    Every entry is optional and every one has a permissive default, so a
    caller that has none of them -- a test, mostly -- can build a definition
    without standing up a session, a skill catalogue or a Feishu app.
    """

    #: Where a task goes when it names no folder: the folder the person chose
    #: for this session.
    chosen_workspace_root: Optional[Path] = None
    #: Where it goes when there is not even that.
    fallback_workspace_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    #: ``.get(skill_id)`` -> bundle with ``user_invocable``.
    skill_catalog: Any = None
    #: Model id -> itself, or raises for an id no provider group owns.
    model_validator: Optional[Callable[[Any], Optional[str]]] = None
    #: Whether "deliver to Feishu" is configured at all.
    feishu_ready: Optional[Callable[[], bool]] = None
    #: Signal name -> why it can never fire, or None when it can.
    signal_problem: Optional[Callable[[str], Optional[str]]] = None


def trigger_to_body(trigger: Optional[TriggerSpec]) -> dict[str, Any]:
    """A stored trigger, in the vocabulary the body is written in.

    Not ``to_json``: that is the column's shape, and this is the interface's.
    The two differ in exactly one place, and it is why this exists rather than
    the payload being handed out as-is.

    A signal subscription has two stored forms -- a single ``name``, and the
    ``names``/``mode`` list a workflow's fan-in writes.  The list form is used
    even when there is only *one* upstream, so the two cannot be told apart by
    counting, and a decoder that tried would read a one-upstream step as a
    single-name subscription and rebuild it that way -- which the store reads
    as "the trigger changed" and answers by recomputing the schedule, and which
    would drop every other upstream the moment there were two.  So the shape is
    reported as found, and there is a key for each.
    """
    if trigger is None:
        return {}
    payload = dict(trigger.payload or {})
    body: dict[str, Any] = {
        "trigger_type": trigger.trigger_type,
        "timezone_name": str(payload.get("timezone_name") or LOCAL_TIMEZONE),
    }
    if trigger.trigger_type == "once":
        body["at"] = str(payload.get("at") or "")
    elif trigger.trigger_type == "interval":
        body["every"] = int(payload.get("every") or 1)
        body["unit"] = str(payload.get("unit") or "days")
        body["anchor_at"] = str(payload.get("anchor_at") or "")
    elif trigger.trigger_type in {"daily", "weekdays"}:
        body["time_of_day"] = str(payload.get("time_of_day") or "")
    elif trigger.trigger_type == "weekly":
        body["day_of_week"] = str(payload.get("day_of_week") or "")
        body["time_of_day"] = str(payload.get("time_of_day") or "")
    elif trigger.trigger_type == "monthly":
        body["day_of_month"] = int(payload.get("day_of_month") or 1)
        body["time_of_day"] = str(payload.get("time_of_day") or "")
    elif trigger.trigger_type == "signal":
        listed = payload.get("names")
        if isinstance(listed, (list, tuple)):
            body["signal_names"] = [str(item) for item in listed]
            body["signal_mode"] = str(payload.get("mode") or SIGNAL_MODE_ANY)
        else:
            body["signal_name"] = str(payload.get("name") or "")
    return body


def trigger_from_body(
    body: dict[str, Any],
    existing: Any = None,
    *,
    signal_problem: Optional[Callable[[str], Optional[str]]] = None,
) -> TriggerSpec:
    """A ``TriggerSpec`` from the body, filling gaps from what the task had.

    Shared by the schedule interface, the workflow interface and the agent's
    tools.  A second copy of these rules would be free to disagree with this
    one about what "每周三 09:00" means, and the disagreement would show up as
    a task that fires on the wrong day rather than as an error.

    A field the body omits falls back to the stored trigger's own field, so
    changing only the hour of a weekly report does not oblige the caller to
    restate the day.
    """
    stored = getattr(existing, "trigger", None)
    stored_payload = dict(getattr(stored, "payload", {}) or {})

    def named(field_name: str) -> bool:
        """Whether the body itself supplies this field, rather than falling back.

        The difference decides what may be validated: a value the caller just
        wrote can be refused for being wrong, one carried over from the stored
        row cannot -- the row is the record of what was already accepted.
        """
        return field_name in body and body.get(field_name) not in (None, "")

    def answered(field_name: str, fallback: Any) -> Any:
        if named(field_name):
            return body.get(field_name)
        if stored_payload.get(field_name) not in (None, ""):
            return stored_payload.get(field_name)
        return fallback

    trigger_type = (
        str(answered("trigger_type", getattr(stored, "trigger_type", "once")))
        .strip()
        .lower()
    )
    # An omitted zone means the machine's own, not UTC: the browser sends its
    # zone explicitly, so this only decides the case where nothing did.
    timezone_name = (
        str(answered("timezone_name", LOCAL_TIMEZONE)).strip() or LOCAL_TIMEZONE
    )

    if trigger_type == "once":
        at = str(answered("at", "")).strip()
        if not at:
            raise ValueError("once 触发方式需要 `at`（要运行的时刻）")
        trigger = TriggerSpec.once(at, timezone_name)
        # Only a moment this caller just supplied has to be in the future.  The
        # stored one must not be: a one-off that has already fired still sits on
        # its task, and checking it here would refuse every later edit to that
        # task -- renaming it, switching it off, correcting its products.  That
        # is how "get the definition wrong and delete it" becomes the only way
        # to fix anything, which is exactly what these tools exist to end.
        if named("at") and trigger.initial_run_at() <= datetime.now(timezone.utc):
            raise ValueError("执行时间必须晚于当前时间")
    elif trigger_type == "interval":
        # The interface calls the anchor `anchor_at`; the tools' flat
        # vocabulary has only `at` and means the same instant by it.  Both are
        # read here so neither caller has to translate.
        anchor = str(answered("anchor_at", "") or answered("at", "")).strip()
        if not anchor:
            raise ValueError("interval 触发方式需要 `anchor_at`（起算时刻）")
        every = int(answered("every", 1))
        if every < 1:
            raise ValueError("重复间隔必须大于 0")
        trigger = TriggerSpec.interval(
            every, str(answered("unit", "days")), anchor, timezone_name
        )
    elif trigger_type == "daily":
        trigger = TriggerSpec.daily(str(answered("time_of_day", "")), timezone_name)
    elif trigger_type == "weekly":
        trigger = TriggerSpec.weekly(
            str(answered("day_of_week", "")),
            str(answered("time_of_day", "")),
            timezone_name,
        )
    elif trigger_type == "weekdays":
        trigger = TriggerSpec.weekdays(str(answered("time_of_day", "")), timezone_name)
    elif trigger_type == "monthly":
        trigger = TriggerSpec.monthly(
            int(answered("day_of_month", 1)),
            str(answered("time_of_day", "")),
            timezone_name,
        )
    elif trigger_type == "signal":
        # Which of the two stored shapes this ends up as is decided by which
        # one it *came* from, not by how many names there are: a workflow's
        # fan-in writes the list form even for a single upstream, so counting
        # would silently rewrite every dependent step's trigger into the
        # single-name form -- briefly identical in meaning, and then wrong the
        # moment a second upstream is added.
        names: Optional[list[str]] = None
        mode = SIGNAL_MODE_ALL
        single_form = False
        if "signal_name" in body and str(body.get("signal_name") or "").strip():
            # An explicit single name is how a caller says "just this one",
            # which is the only way to leave the list form behind.
            names = [str(body["signal_name"]).strip()]
            single_form = True
        elif isinstance(body.get("signal_names"), list):
            names = [
                str(item).strip() for item in body["signal_names"] if str(item).strip()
            ]
            mode = str(
                body.get("signal_mode") or stored_payload.get("mode") or SIGNAL_MODE_ALL
            )
        elif isinstance(stored_payload.get("names"), list):
            names = [
                str(item).strip()
                for item in stored_payload["names"]
                if str(item).strip()
            ]
            mode = str(
                body.get("signal_mode") or stored_payload.get("mode") or SIGNAL_MODE_ALL
            )
        else:
            single = str(stored_payload.get("name") or "").strip()
            if single:
                names = [single]
                single_form = True
        if not names:
            raise ValueError("signal 触发方式需要 `signal_name`（要等待的信号）")
        if mode not in SIGNAL_MODES:
            raise ValueError(f"不支持的 signal_mode「{mode}」")
        # Subscribing is exact-name matching, so a typo is not a near miss --
        # it is a task that never runs.  What can be checked is checked; what
        # cannot is allowed, because a new name is how a new emitter is paired
        # and refusing it would make the two sides have to be created in a
        # particular order.
        if signal_problem is not None:
            for name in names:
                problem = signal_problem(name)
                if problem:
                    raise ValueError(f"signal_name「{name}」无效：{problem}")
        if single_form:
            trigger = TriggerSpec.signal(names[0])
        elif mode == SIGNAL_MODE_ALL:
            trigger = TriggerSpec.signal_all(names)
        else:
            trigger = TriggerSpec("signal", {"names": names, "mode": mode})
    else:
        raise ValueError(f"不支持的触发方式（trigger_type）：{trigger_type}")

    # Evaluated once so an unsatisfiable spec is refused while the caller can
    # still fix it, rather than at the moment it is first due.
    trigger.instantiate().next_after(datetime.now(timezone.utc))
    return trigger


def delivery_from_body(
    body: dict[str, Any],
    existing: Any = None,
    *,
    feishu_ready: Optional[Callable[[], bool]] = None,
) -> tuple[str, DeliveryTarget]:
    """Delivery mode and target, from the body or from what the task had.

    "发到飞书" is the one channel the runtime can deliver to, and it reads the
    global Feishu app credentials -- per-task credentials are not a thing this
    interface offers.  Checking them here is the difference between a form that
    says what it does and one that accepts a task whose every run fails at 3am
    with a RuntimeError nobody typed.
    """
    delivery_mode = str(
        body.get("delivery_mode", getattr(existing, "delivery_mode", "standalone"))
        or "standalone"
    )
    if delivery_mode not in {"standalone", "channel"}:
        raise ValueError("不支持的投递方式")
    if delivery_mode == "standalone":
        return "standalone", DeliveryTarget.standalone()

    raw_target = body.get("delivery_target")
    if isinstance(raw_target, dict) and raw_target:
        target_type = str(raw_target.get("target_type", "")).strip()
        payload = raw_target.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("delivery_target.payload 必须是对象")
        if target_type != "feishu_chat":
            given = target_type or "（空）"
            raise ValueError(f"暂不支持的投递渠道：{given}")
        chat_id = str(payload.get("chat_id", "")).strip()
        if not chat_id:
            raise ValueError("发到飞书需要选择一个会话")
        target = DeliveryTarget(
            target_type="feishu_chat",
            payload={
                "chat_id": chat_id,
                # Every chat the picker offers comes from the bot's chat list,
                # so a chat_id addresses it; receive_id_type is written down
                # because the pre-picker heuristic guessed open_id for anything
                # not marked "group" and would have misread these.
                "chat_type": str(payload.get("chat_type", "group") or "group"),
                "receive_id_type": "chat_id",
            },
        )
    else:
        # Nothing chosen in this body: keep what the task already had, which is
        # the only honest answer for an edit that did not touch delivery.  A
        # task that never had one is asked to pick.
        target = getattr(existing, "delivery_target", None)
        if target is None or target.target_type != "feishu_chat":
            raise ValueError("发到飞书需要选择一个会话")
    if (
        feishu_ready is not None
        and not feishu_ready()
        and _is_new_promise(existing, target)
    ):
        raise ValueError("发到飞书需要先在设置里填好飞书应用（app_id / app_secret）")
    return "channel", target


def _is_new_promise(existing: Any, target: DeliveryTarget) -> bool:
    """Whether delivering here is a promise this save is making for the first time.

    The credential check exists to stop a *save* from creating a task whose
    every run will fail at 3am.  A save that resolves to the delivery the row
    already had is not making a promise -- the promise was made already -- so
    refusing it would only block fixing something else, at the moment the task
    is broken and the credentials are gone.  That is exactly the moment an
    agent reaches for the editor.
    """
    if str(getattr(existing, "delivery_mode", "") or "") != "channel":
        return True
    stored = getattr(existing, "delivery_target", None)
    if stored is None:
        return True
    to_json = getattr(stored, "to_json", None)
    stored_key = to_json() if callable(to_json) else str(stored)
    return stored_key != target.to_json()


def _content_from_body(kind: str, body: dict[str, Any]) -> Optional[str]:
    """The content this body names for *kind*, or None when it names none.

    ``None`` and ``""`` are different answers: ``""`` is a body that said "this
    task no longer has content" and is refused below, while ``None`` is a body
    that said nothing about content at all and means the stored one is kept.
    """
    if kind == "message":
        for field_name in ("message_text", "prompt"):
            if field_name in body:
                return str(body.get(field_name) or "").strip()
        return None
    if kind == "system_job":
        if "job_name" in body:
            return str(body.get("job_name") or "").strip()
        return None
    for field_name in ("prompt", "instruction"):
        if field_name in body:
            return str(body.get(field_name) or "").strip()
    return None


def _infer_kind(body: dict[str, Any]) -> Optional[str]:
    """Which kind a body that names no ``action_type`` is asking for.

    Mirrors the inference the tools already use when creating a step, so the
    two doors read the same body the same way.  ``message_text`` wins because
    it is the one field that cannot also be read as an agent instruction.
    """
    if "message_text" in body:
        return "message"
    if "instruction" in body:
        return "agent_prompt"
    if "job_name" in body:
        return "system_job"
    if "prompt" in body:
        return "agent_prompt"
    return None


def _resolve_action(body: dict[str, Any], existing: Any) -> tuple[str, dict[str, Any]]:
    """Kind and payload, from the body or from what the task had."""
    existing_kind = str(getattr(existing, "kind", "") or "")
    existing_payload = dict(getattr(existing, "payload", {}) or {})
    asked = str(body.get("action_type", "") or "").strip().lower()

    if asked:
        kind = ACTION_KINDS.get(asked)
        if kind is None:
            raise ValueError("不支持的任务类型")
        content = _content_from_body(kind, body)
        if content is None and existing_kind == kind:
            content = str(next(iter(existing_payload.values()), "") or "").strip()
        if not content:
            raise ValueError("任务内容不能为空")
    else:
        kind = existing_kind or (_infer_kind(body) or "")
        if not kind:
            raise ValueError("新建任务需要 `action_type` 或内容字段")
        content = _content_from_body(kind, body)
        if content is None:
            if not existing_kind:
                raise ValueError("任务内容不能为空")
            # Nothing named: keep the stored kind and payload exactly.
            return existing_kind, dict(existing_payload)
        if not content:
            raise ValueError("任务内容不能为空")

    limit = MAX_CONTENT_LENGTH.get(kind)
    if limit is not None and len(content) > limit:
        raise ValueError(f"任务内容不能超过 {limit} 个字符")
    return kind, {PAYLOAD_KEY[kind]: content}


def task_from_body(
    body: dict[str, Any],
    existing: Any = None,
    *,
    context: Optional[EditContext] = None,
    keep_trigger: bool = False,
    request_quote: str = "",
) -> NewScheduledTask:
    """The whole task a body leaves behind, given the task it edits.

    ``existing`` is the row being edited, or None when one is being created.
    ``keep_trigger`` is for a step of a workflow that has upstreams: its
    trigger *is* its upstreams, so the graph owns that field and a task
    editor must not be able to overwrite it.
    """
    ctx = context or EditContext()

    def answered(field_name: str, fallback: Any) -> Any:
        """The value for *field_name*: what the body said, else what was."""
        if field_name in body:
            return body.get(field_name)
        if existing is not None:
            return getattr(existing, field_name, fallback)
        return fallback

    name = str(answered("name", "") or "").strip()
    if not name:
        raise ValueError("任务名称不能为空")
    if len(name) > 80:
        raise ValueError("任务名称不能超过 80 个字符")

    if keep_trigger:
        asked_type = str(body.get("trigger_type", "") or "").strip().lower()
        if asked_type and asked_type != existing.trigger.trigger_type:
            raise ValueError("这一步的触发方式由它上游的步骤决定，不能在这里改成别的")
        trigger = existing.trigger
    elif any(field_name in body for field_name in TRIGGER_BODY_FIELDS):
        trigger = trigger_from_body(body, existing, signal_problem=ctx.signal_problem)
    elif existing is not None:
        trigger = existing.trigger
    else:
        raise ValueError("新建任务需要 `trigger_type`")

    task_kind, payload = _resolve_action(body, existing)

    requested_profile = (
        str(answered("permission_profile", "inherit") or "inherit").strip() or "inherit"
    )
    if requested_profile not in PERMISSION_PROFILES:
        # Named, and with the alternatives, because two audiences read this:
        # the browser shows it to a person who picked from a list, and a tool
        # call hands it back to a model that has to try again.  A message that
        # only said "no" would leave both guessing at the spelling.
        raise ValueError(
            f"不支持的权限策略「{requested_profile}」，"
            "可选：" + "、".join(PERMISSION_PROFILES)
        )
    profile = resolve_permission_profile(requested_profile)

    # Resolved before the fallback chain on purpose.  A profile that grants
    # writes needs a directory a *person* chose: falling back to the gateway
    # process's working directory would make the task write somewhere nobody
    # can predict from its definition.
    explicit_workspace = str(answered("workspace_root", "") or "").strip()
    chosen_workspace = (
        Path(explicit_workspace).expanduser().resolve(strict=False)
        if explicit_workspace
        else ctx.chosen_workspace_root
    )
    if profile.requires_workspace_root and chosen_workspace is None:
        raise ValueError(
            f"权限策略「{profile.label}」需要显式指定项目文件夹，"
            "不能回落到服务进程的当前目录"
        )
    workspace = chosen_workspace or ctx.fallback_workspace_root
    if task_kind == "agent_prompt" and not workspace.is_dir():
        raise ValueError(f"项目文件夹不存在：{workspace}")

    context_policy = str(answered("context_policy", "stateless") or "stateless")
    if context_policy not in CONTEXT_POLICIES:
        raise ValueError("不支持的上下文策略")

    timeout_seconds = int(answered("timeout_seconds", 1800))
    if timeout_seconds < 10 or timeout_seconds > 604800:
        raise ValueError("超时时间必须在 10 秒到 7 天之间")

    raw_retry = answered("retry_policy", None)
    if raw_retry is not None and not isinstance(raw_retry, dict):
        raise ValueError("retry_policy 必须是对象")
    retry = dict(raw_retry or {})
    try:
        # No ``or`` fallbacks: ``0 or 1`` is ``1``, and a max_attempts of 0
        # would slip through the range check below as the no-retry default
        # instead of being refused.
        max_attempts = int(retry.get("max_attempts", 1))
        backoff_seconds = int(retry.get("backoff_seconds", 30))
    except (TypeError, ValueError):
        raise ValueError("retry_policy 必须是整数")
    if max_attempts < 1 or max_attempts > MAX_RETRY_ATTEMPTS:
        raise ValueError(f"最大尝试次数必须在 1 到 {MAX_RETRY_ATTEMPTS} 之间")
    if backoff_seconds < 0 or backoff_seconds > MAX_RETRY_BACKOFF_SECONDS:
        raise ValueError(f"重试间隔必须在 0 到 {MAX_RETRY_BACKOFF_SECONDS} 秒之间")

    raw_skills = answered("selected_skills", None)
    if raw_skills is not None and not isinstance(raw_skills, list):
        raise ValueError("selected_skills must be a list")
    selected_skills = list(
        dict.fromkeys(
            str(item).strip() for item in (raw_skills or []) if str(item).strip()
        )
    )
    if ctx.skill_catalog is not None:
        for skill_id in selected_skills:
            bundle = ctx.skill_catalog.get(skill_id)
            if bundle is None or not getattr(bundle, "user_invocable", False):
                raise ValueError(f"技能不可用：{skill_id}")

    # Validated only when the body names it.  Re-validating a stored value
    # would make an unrelated edit fail the day a provider's model list
    # changes, which would take away the only way to fix a task whose model
    # has gone away.
    model_override = answered("model_override", None)
    if "model_override" in body and ctx.model_validator is not None:
        model_override = ctx.model_validator(model_override)

    # Both halves of the contract, each kept unless the body names it.  Sending
    # the field explicitly -- an empty list included -- is an answer, and it is
    # how a criterion is removed on purpose rather than by omission.
    # Each half is its own field, so naming one is an answer about one.  Both
    # halves were rebuilt together before, which meant "fix the verify_command"
    # also cleared the criteria -- the same invisible loss the task editor was
    # rebuilt to stop, and the worst version of it: the criteria are what the
    # run is judged by, so a task that lost them keeps reporting success.
    stored_acceptance = getattr(existing, "acceptance", None) or Acceptance()
    acceptance = Acceptance(
        criteria=(
            [str(item) for item in (body.get("criteria") or []) if str(item).strip()]
            if "criteria" in body
            else list(stored_acceptance.criteria)
        ),
        verify_command=(
            str(body.get("verify_command") or "").strip()
            if "verify_command" in body
            else str(stored_acceptance.verify_command)
        ),
    )

    if "produces" in body:
        produces = normalize_products(body.get("produces"))
    elif existing is not None:
        produces = list(getattr(existing, "produces", []) or [])
    else:
        produces = []

    delivery_mode, delivery_target = delivery_from_body(
        body, existing, feishu_ready=ctx.feishu_ready
    )

    for field_name, supported in (
        ("overlap_policy", "forbid_overlap"),
        ("missed_run_policy", "coalesce"),
    ):
        if field_name in body and str(body.get(field_name) or supported) != supported:
            raise ValueError(f"{field_name} 暂不支持自定义；当前固定为 {supported}")

    return NewScheduledTask(
        name=name,
        kind=task_kind,
        trigger=trigger,
        payload=payload,
        delivery_mode=delivery_mode,
        delivery_target=delivery_target,
        model_override=model_override,
        enabled=bool(answered("enabled", True)),
        workspace_root=str(workspace),
        context_policy=context_policy,
        timeout_seconds=timeout_seconds,
        retry_policy={
            "max_attempts": max_attempts,
            "backoff_seconds": backoff_seconds,
        },
        selected_skills=selected_skills,
        permission_profile=profile.key,
        acceptance=acceptance,
        produces=produces,
        # Membership is inherited, never taken from the body.  Which workflow a
        # task is a step of is decided by materialising that workflow, so a
        # request that could set it could also detach a step from the chain it
        # is part of -- and a detached step keeps running while the graph that
        # explains it stops mentioning it.
        workflow_id=str(getattr(existing, "workflow_id", "") or ""),
        step_key=str(getattr(existing, "step_key", "") or ""),
        # A quote is evidence about the past, not a setting: no body can
        # rewrite who asked for this task, and a body that carries one is
        # ignored rather than believed.  Only a creation supplies it.
        request_quote=(
            str(request_quote or "").strip()
            or str(getattr(existing, "request_quote", "") or "")
        ),
    )


def task_definition_payload(task: Any) -> dict[str, Any]:
    """A task's whole definition, in the vocabulary :func:`task_from_body` reads.

    The inverse of the builder, and deliberately next to it: a field this
    forgets is a field an edit can silently reset, and keeping the pair
    adjacent is what makes that visible.  ``tests/test_scheduler_editing.py``
    pins both halves of the promise -- that feeding this straight back through
    the builder gives the task it came from, and that every key here is a key
    the agent's ``schedule_update`` accepts.  The second is not decoration: a
    definition that can be read but not written back is a description, not an
    edit, and a ``schedule_runs`` reply that ``schedule_update`` rejects as
    having unknown fields is worse than one that omits them.

    Three things are absent, each because it belongs to somebody else:

    * ``workflow_id`` / ``step_key`` -- inherited from the graph, which decides
      them by materialising the workflow.  A body that could set them could
      detach a step from the chain that explains it.
    * ``enabled`` -- ``schedule_set_enabled``'s, and that path refuses to flip
      the switch of a step of a live workflow, because the next save of that
      workflow rewrites it.  A second door here would not refuse, so the
      switch would have a way round its own guard.
    * ``request_quote`` -- evidence about the past rather than a setting; no
      body can rewrite who asked for a task.

    ``acceptance`` is flattened into its two fields rather than nested: the
    builder reads ``criteria`` and ``verify_command``, so a payload carrying
    the nested object would come back as a body that mentions neither -- and
    the pair that decides whether a run counts would be the one thing an edit
    could never move.
    """
    acceptance = acceptance_payload(getattr(task, "acceptance", None))
    return {
        "name": task.name,
        "action_type": {
            "message": "message",
            "agent_prompt": "agent_task",
            "system_job": "system_job",
        }.get(task.kind, "agent_task"),
        **(
            {"message_text": str((task.payload or {}).get("message_text") or "")}
            if task.kind == "message"
            else {"job_name": str((task.payload or {}).get("job_name") or "")}
            if task.kind == "system_job"
            else {"prompt": str((task.payload or {}).get("prompt") or "")}
        ),
        **trigger_to_body(task.trigger),
        "permission_profile": task.permission_profile,
        "workspace_root": task.workspace_root,
        "context_policy": task.context_policy,
        "timeout_seconds": int(task.timeout_seconds),
        "retry_policy": dict(task.retry_policy or {}),
        "selected_skills": list(task.selected_skills or []),
        "model_override": task.model_override,
        "criteria": list(acceptance["criteria"]),
        "verify_command": str(acceptance["verify_command"]),
        "produces": list(getattr(task, "produces", []) or []),
    }


def step_owns_no_trigger(store: Any, task: Any) -> bool:
    """True when *task* is a workflow step whose timing comes from upstreams.

    Answered from the graph, not from the task: a fan-in trigger and a
    hand-picked signal both report ``trigger_type`` "signal", and only the
    graph knows which steps have upstreams.
    """
    if not getattr(task, "workflow_id", "") or not getattr(task, "step_key", ""):
        return False
    workflow = store.get_workflow(task.workflow_id)
    if workflow is None:
        return False
    step = workflow.step(task.step_key)
    return step is not None and bool(step.depends_on)


def mirror_step_edit(store: Any, task: Any) -> None:
    """Copy an edited step task back into the graph it belongs to.

    A step is edited through the ordinary task path -- it is an ordinary task,
    with its own run history and its own switches -- but what it is *stored* as
    is a step of a graph, and the next save of that workflow rebuilds the task
    from the graph.  Without this, changing a step's prompt would survive
    exactly until somebody moved an edge.

    The graph is not re-materialised: the task in hand was written a moment ago
    and is the newer of the two, so rebuilding it from the step copied from it
    would be a round trip that can only lose something.

    Every field of :class:`WorkflowStep` is copied, and that is the whole point
    of writing it out here rather than in each caller.  A field left out is not
    merely uncopied -- the step is rebuilt from the dataclass default, so a
    rename would reset the retry policy, and an edit to a step's schedule would
    drop the files it promised to produce.
    """
    if not getattr(task, "workflow_id", "") or not getattr(task, "step_key", ""):
        return
    workflow = store.get_workflow(task.workflow_id)
    if workflow is None:
        return
    from agent.scheduler.models import Workflow, WorkflowStep

    steps = []
    matched = False
    for step in workflow.steps:
        if str(step.key).strip() != str(task.step_key).strip():
            steps.append(step)
            continue
        matched = True
        steps.append(
            WorkflowStep(
                key=step.key,
                name=task.name,
                kind=task.kind,
                payload=dict(task.payload),
                # A task cannot express an edge, so it must not be able to
                # break or invent one.
                depends_on=list(step.depends_on),
                # Nor a trigger, when it has upstreams: the upstreams *are* its
                # trigger, and this is the one field where the task row and the
                # graph would otherwise disagree.
                trigger=None if step.depends_on else task.trigger,
                workspace_root=task.workspace_root,
                permission_profile=task.permission_profile,
                context_policy=task.context_policy,
                model_override=task.model_override,
                timeout_seconds=int(task.timeout_seconds),
                selected_skills=list(task.selected_skills),
                acceptance=task.acceptance,
                produces=list(getattr(task, "produces", []) or []),
                retry_policy=dict(task.retry_policy),
                delivery_mode=task.delivery_mode,
                delivery_target=task.delivery_target,
            )
        )
    if not matched:
        return
    store.update_workflow(
        workflow.id,
        Workflow(
            name=workflow.name,
            steps=steps,
            id=workflow.id,
            description=workflow.description,
            enabled=workflow.enabled,
        ),
        materialize=False,
    )


def describe_edit(before: Any, after: Any) -> list[str]:
    """Which fields an edit actually changed, in the reader's own words.

    Said out loud because the failure this whole module exists to prevent is
    silent: a field rewritten to its default looks exactly like a field nobody
    touched, and the only way to tell is to compare.  An empty list is the
    answer "this changed nothing", which is a real answer and worth printing.
    """
    fields = (
        ("name", "名称"),
        ("kind", "类型"),
        ("payload", "内容"),
        ("enabled", "启用"),
        ("trigger", "触发方式"),
        ("workspace_root", "项目文件夹"),
        ("permission_profile", "权限策略"),
        ("context_policy", "上下文策略"),
        ("timeout_seconds", "超时"),
        ("retry_policy", "重试策略"),
        ("selected_skills", "技能"),
        ("model_override", "模型"),
        ("delivery_mode", "投递方式"),
        ("delivery_target", "投递目标"),
        ("acceptance", "判定依据"),
        ("produces", "产出文件"),
    )
    changed: list[str] = []
    for attribute, label in fields:
        old = getattr(before, attribute, None)
        new = getattr(after, attribute, None)
        old_key = old.to_json() if hasattr(old, "to_json") else old
        new_key = new.to_json() if hasattr(new, "to_json") else new
        if isinstance(old_key, list):
            old_key = list(old_key)
        if isinstance(new_key, list):
            new_key = list(new_key)
        if old_key != new_key:
            changed.append(label)
    return changed


__all__ = [
    "ACTION_KINDS",
    "CONTEXT_POLICIES",
    "EditContext",
    "PAYLOAD_KEY",
    "TRIGGER_BODY_FIELDS",
    "delivery_from_body",
    "describe_edit",
    "mirror_step_edit",
    "step_owns_no_trigger",
    "task_definition_payload",
    "task_from_body",
    "trigger_from_body",
    "trigger_to_body",
]
