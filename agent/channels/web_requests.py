"""Parsing what a request body says about workflows, triggers and deliveries: the shapes the HTTP layer accepts, and the feishu settings that decide whether a trigger can be delivered. Moved out of WebChannel (see the remaining-big-files survey work order)."""

from __future__ import annotations

from typing import Any, Optional


def _names_a_trigger_field(raw: dict[str, Any]) -> bool:
    """Whether a step body says anything about *when* it runs, other than type.

    Kept separate from the ``trigger_type`` test so the type's own field is not
    double-counted, and so a body that names only an hour is still read as an
    answer.  Emptiness is not an answer: an editor that sends every field it
    knows about, blank ones included, is not asking for anything.
    """
    from agent.scheduler.editing import TRIGGER_BODY_FIELDS

    return any(
        field_name != "trigger_type"
        and field_name != "trigger_from"
        and str(raw.get(field_name) or "").strip()
        for field_name in TRIGGER_BODY_FIELDS
    )


def _resolve_model_override(raw_model: Any) -> str | None:
    """Validate a model selected by the browser.

        The input control sends a model id, not a provider configuration.
        Keep this value scoped to the current turn and only accept ids the
        routing layer can dispatch — every configured provider's models, not
        just the active provider's (RoutingTransport owns the model -> client
        mapping, so a foreign provider's id is a valid per-turn override).
        """
    if raw_model is None:
        return None
    if not isinstance(raw_model, str):
        raise ValueError("model must be a string")
    model = raw_model.strip()
    if not model:
        return None

    cfg: dict[str, Any] = {}
    try:
        from agent.config import load_config
        from agent.core.transport import routable_model_ids

        cfg, _ = load_config()
        configured = routable_model_ids(cfg)
    except Exception:
        configured = set()
    if configured and model not in configured:
        raise ValueError("model is not available in the current configuration")
    if not configured:
        raise ValueError("no configured models are available")
    return model


def _feishu_ready() -> bool:
    feishu = _feishu_channel_config()
    return bool(feishu.get("app_id") and feishu.get("app_secret"))


def _signal_problem(
    name: str,
) -> Optional[str]:
    """Why *name* can never fire, or None when it can.

        A short-lived connection of its own, because the builder deliberately
        knows nothing about persistence: it turns a body into a spec, and the
        caller owns the write.  The check needs to read tasks, so it borrows a
        connection rather than widening the builder's contract for one
        validation.
        """
    from agent.scheduler import open_store

    probe = open_store()
    try:
        return probe.describe_signal_problem(name)
    finally:
        probe.close()


def _trigger_from_body(
    body: dict[str, Any],
    existing: Any = None,
):
    """A ``TriggerSpec`` from a body, filling gaps from what was stored."""
    from agent.scheduler import trigger_from_body

    return trigger_from_body(body, existing, signal_problem=_signal_problem)


def _delivery_from_body(
    body: dict[str, Any],
    existing: Any = None,
):
    """Delivery mode and target, from the body or from what the task had."""
    from agent.scheduler import delivery_from_body

    return delivery_from_body(body, existing, feishu_ready=_feishu_ready)


def _feishu_channel_config() -> dict[str, Any]:
    """The global Feishu channel config, as delivery reads it at run time.

        Delivery resolves credentials from the global config when a run
        finishes, so this is the same source -- asking here what the runtime
        will see then, not a copy that can drift.
        """
    from agent.config import load_config

    cfg, _ = load_config()
    feishu = cfg.get("channels", {}).get("feishu", {})
    return feishu if isinstance(feishu, dict) else {}


def _borrowed_trigger(
    raw: dict[str, Any],
    key: str,
    existing_by_key: dict[str, Any],
) -> Any:
    """The trigger a step asks to take over from another step, if it asks.

        A schedule belongs to the *chain*, not to the step that happens to hold
        it: "每天早上九点" is when the workflow runs, and the entry step's task
        is only where the clock lives, because that is the task that fires.
        So when an edit moves the entry role from one step to another -- which
        is what reordering a chain does -- the schedule has to move with it.

        ``trigger_from`` is how the editor says that without ever holding the
        trigger itself.  It names the step whose schedule moves, and the spec is
        copied from what is stored, so a reorder cannot round-trip somebody's
        weekly clock through a form that only knows how to render it.

        The donor must be a step of the *stored* graph, and the answer is read
        before anything is written, so it does not matter what order the body
        lists the steps in.
        """
    donor_key = str(raw.get("trigger_from", "") or "").strip()
    if not donor_key:
        return None
    if donor_key == key:
        raise ValueError(f"步骤「{key}」不能沿用自己现有的触发方式")
    donor = existing_by_key.get(donor_key)
    if donor is None:
        raise ValueError(
            f"步骤「{key}」想沿用「{donor_key}」的触发方式，"
                "但这个流程里没有这一步"
        )
    if getattr(donor, "trigger", None) is None:
        raise ValueError(
            f"步骤「{key}」想沿用「{donor_key}」的触发方式，"
                f"但「{donor_key}」不是入口步骤，它由上游驱动，没有自己的触发方式"
        )
    return donor.trigger


def _workflow_step_from_body(
    raw: dict[str, Any],
    index: int,
    existing: Any = None,
    borrowed_trigger: Any = None,
):
    """One step of a workflow, from its JSON object.

        A step with upstreams may not carry a trigger, and a step without them
        must.  Both are refused here with a sentence about the step, and again
        by the graph check -- the first so the message names the step, the
        second so no other caller can get past it.

        A field the body *omits* keeps the value the step already had, and only
        a field that is present is taken as an answer.  The alternative is that
        every edit of a graph has to resend everything the editor never shows
        -- the entry step's schedule, a step's model, its skill list -- because
        leaving one out would silently reset it.  A graph editor that edits the
        graph should not be able to erase a step's model by not mentioning it.
        Sending the field explicitly, empty string included, still means what it
        says.

        Where a step's own schedule is concerned, an explicit ``trigger_type``
        is an answer, an inherited one from ``borrowed_trigger`` is the next
        best thing, and only then does the step keep what it had.
        """
    from agent.scheduler import (
        MAX_RETRY_ATTEMPTS,
        MAX_RETRY_BACKOFF_SECONDS,
        WorkflowStep,
    )

    def answered(field_name: str, fallback: Any) -> Any:
        """The value for *field_name*: what the body said, else what was."""
        if field_name in raw:
            return raw.get(field_name)
        if existing is not None:
            return getattr(existing, field_name, fallback)
        return fallback

    key = str(raw.get("key", "")).strip()
    if not key:
        raise ValueError(f"第 {index + 1} 个步骤缺少 key")
    if len(key) > 40:
        raise ValueError(f"步骤 key「{key}」不能超过 40 个字符")
    if any(ch.isspace() for ch in key):
        raise ValueError(f"步骤 key「{key}」不能包含空格")
    given_name = str(answered("name", "") or "").strip()
    payload = answered("payload", None)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError(f"步骤「{key}」的 payload 必须是对象")
    depends_on = [
        str(item).strip()
        for item in (answered("depends_on", None) or [])
        if str(item).strip()
    ]
    trigger = None
    given_trigger = str(raw.get("trigger_type", "")).strip()
    if depends_on:
        if given_trigger:
            raise ValueError(
                f"步骤「{key}」有上游，不能另外再指定时间或信号触发"
            )
        if borrowed_trigger is not None:
            raise ValueError(
                f"步骤「{key}」有上游，不能沿用别的步骤的触发方式；"
                    "触发方式跟着入口步骤走"
            )
    elif given_trigger:
        trigger = _trigger_from_body(raw, existing)
    elif _names_a_trigger_field(raw):
        # A body that names no ``trigger_type`` but does name, say, the hour
        # is still an answer about when this runs; the stored trigger's own
        # type is what it is answering about.  Read as "unset" it was
        # silently ignored, and a step whose schedule was never moved
        # looked exactly like one that was.
        trigger = _trigger_from_body(raw, existing)
    elif borrowed_trigger is not None:
        trigger = borrowed_trigger
    elif existing is not None and existing.trigger is not None:
        trigger = existing.trigger
    else:
        raise ValueError(
            f"步骤「{key}」没有上游，必须指定触发方式（trigger_type）"
        )
    delivery_mode, delivery_target = _delivery_from_body(raw, existing)
    skills = answered("selected_skills", None)
    if skills is not None and not isinstance(skills, list):
        raise ValueError(f"步骤「{key}」的 selected_skills 必须是数组")
    # Checked against the same bounds a task's is, and refused rather than
    # clamped: a number that comes back different from the one that was
    # sent is a silent disagreement about what this step will do when it
    # fails.  The store clamps instead, because a graph written straight to
    # it has nobody left to tell.
    raw_retry = answered("retry_policy", None)
    if raw_retry is not None and not isinstance(raw_retry, dict):
        raise ValueError(f"步骤「{key}」的 retry_policy 必须是对象")
    retry = dict(raw_retry or {})
    try:
        # No ``or`` fallbacks here: ``0 or 1`` is ``1``, and a
        # max_attempts of 0 sent by the editor would slip through the
        # range check below as the no-retry default instead of being
        # refused.  The task-level check reads its fields the same way.
        max_attempts = int(retry.get("max_attempts", 1))
        backoff_seconds = int(retry.get("backoff_seconds", 30))
    except (TypeError, ValueError):
        raise ValueError(f"步骤「{key}」的 retry_policy 必须是整数")
    if max_attempts < 1 or max_attempts > MAX_RETRY_ATTEMPTS:
        raise ValueError(
            f"步骤「{key}」的最大尝试次数必须在 1 到 {MAX_RETRY_ATTEMPTS} 之间"
        )
    if backoff_seconds < 0 or backoff_seconds > MAX_RETRY_BACKOFF_SECONDS:
        raise ValueError(
            f"步骤「{key}」的重试间隔必须在 0 到 {MAX_RETRY_BACKOFF_SECONDS} 秒之间"
        )
    return WorkflowStep(
        key=key,
        name=given_name or key,
        kind=str(answered("kind", "agent_prompt") or "agent_prompt").strip(),
        payload=dict(payload),
        depends_on=depends_on,
        trigger=trigger,
        workspace_root=str(answered("workspace_root", "") or "").strip(),
        permission_profile=str(
            answered("permission_profile", "inherit") or "inherit"
        ),
        context_policy=str(answered("context_policy", "stateless") or "stateless"),
        # Validated exactly like a task's: a step is a task, and an id no
        # provider group owns can only reach the wrong endpoint at run
        # time -- where nothing re-checks it.  Accepting it here would put
        # the failure a scheduled run away from the request that caused it.
        model_override=_resolve_model_override(
            answered("model_override", None)
        ),
        timeout_seconds=int(answered("timeout_seconds", 1800) or 1800),
        selected_skills=[
            str(item).strip()
            for item in (skills or [])
            if str(item).strip()
        ],
        retry_policy={
            "max_attempts": max_attempts,
            "backoff_seconds": backoff_seconds,
        },
        delivery_mode=delivery_mode,
        delivery_target=delivery_target,
    )


def _workflow_from_body(
    body: dict[str, Any],
    existing: Any = None,
):
    from agent.scheduler import Workflow, validate_workflow_graph

    name = str(body.get("name", getattr(existing, "name", ""))).strip()
    if not name:
        raise ValueError("workflow 名称不能为空")
    if len(name) > 60:
        raise ValueError("workflow 名称不能超过 60 个字符")
    description = str(
        body.get("description", getattr(existing, "description", "")) or ""
    ).strip()
    raw_steps = body.get("steps")
    if raw_steps is None and existing is not None:
        raw_steps = [item.to_dict() for item in existing.steps]
    if not isinstance(raw_steps, list):
        raise ValueError("steps 必须是数组")
    if not raw_steps:
        raise ValueError("workflow 至少要有一个步骤")
    if len(raw_steps) > 20:
        raise ValueError("一个 workflow 最多 20 个步骤")
    steps = []
    existing_by_key = {
        str(item.key).strip(): item
        for item in (getattr(existing, "steps", None) or [])
    }
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个步骤必须是对象")
        key = str(raw.get("key", "")).strip()
        steps.append(
            _workflow_step_from_body(
                raw,
                index,
                existing_by_key.get(key),
                # Resolved against the stored graph rather than against the
                # body, so a step may name a donor that this same request is
                # about to give upstreams to: what moves is the schedule
                # that is there now.
                borrowed_trigger=_borrowed_trigger(
                    raw, key, existing_by_key
                ),
            )
        )
    # Checked here as well as in the store so a cycle comes back as a 400
    # carrying the ring, rather than as a failure from inside the write.
    validate_workflow_graph(steps)
    return Workflow(
        name=name,
        description=description,
        enabled=bool(body.get("enabled", getattr(existing, "enabled", True))),
        steps=steps,
    )
