"""Changing a task after it exists, and reading back what it now says.

A scheduled task's definition could only be written at the moment of creation.
Everything that made it wrong afterwards -- a verify command naming a file the
step never writes, a criterion that was not what the user meant, an instruction
missing a line -- had exactly one repair: delete the task and build it again.
That takes the run history with it, and it takes the id, which is what every
downstream step subscribed to, so the edges come down too.  The real database
shows the habit: nine steps left behind by workflows that were rebuilt rather
than edited.

These tests hold the replacement to the promises that make it usable, and each
of them is a way an earlier shape failed:

* a field the call does not name keeps the value it had -- the whole reason an
  edit is not a rewrite, and the reason ``schedule_runs`` reports the
  definition in the vocabulary ``schedule_update`` takes;
* what is read can be sent back -- a definition that can be read but not
  written is a description, and a reply that the writer refuses for naming
  unknown fields is worse than one that never showed them;
* the two things that are somebody else's stay theirs: which workflow a task
  is a step of, and the words that asked for it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_scheduler_state(monkeypatch, tmp_path):
    """Every test gets its own scheduler database and output directory."""
    import agent.shared as shared_module

    agent_home = tmp_path / ".agent"
    monkeypatch.setattr(shared_module, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", agent_home / "output")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", agent_home / "tasks")
    monkeypatch.setattr(
        shared_module,
        "SCHEDULER_DB_FILE",
        agent_home / "tasks" / "scheduler.db",
    )


def _soon(hours: int = 2) -> str:
    """A moment in the future: a one-off being created has to have one."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def make_tools(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = BuiltinTools(
        memory=MemoryPalace(base_dir=tmp_path / "memory", context_dir=tmp_path / "context"),
        registry=registry,
        workspace_root=workspace,
    )
    return tools, registry, workspace


def call(registry, name: str, payload: dict) -> dict:
    return json.loads(asyncio.run(registry.call(name, payload)))


def create_task(registry, **overrides) -> dict:
    """An agent task, which is what most of these are about.

    ``action_type`` is spelled out because the tool's default is ``message``,
    and a message task has no instruction to edit.
    """
    payload = {
        "name": "nightly",
        "action_type": "agent_task",
        "prompt": "写一份日报",
        "trigger_type": "once",
        "at": _soon(),
        "timezone_name": "Asia/Shanghai",
        "intent": "用户说每天要一份日报",
    }
    payload.update(overrides)
    return call(registry, "schedule_create", payload)


def control(registry, name: str, payload: dict, intent: str = "照着故障改") -> dict:
    """Call one of the intent-gated tools.

    They all require a stated intent -- "what this does and why", the same
    requirement ``shell`` carries -- so every call here names one, and a test
    that leaves it out is testing the schema rather than the tool.
    """
    return call(registry, name, {**payload, "intent": intent})


def read_definition(registry, task_id: str) -> dict:
    payload = call(registry, "schedule_runs", {"task_id": task_id})
    assert payload["ok"] is True, payload
    return payload["task"]


def refusal(payload: dict) -> str:
    """The message from a failed call, whichever door refused it.

    A schema refusal comes back as ``{"error": {"message": ...}}`` and one
    from the tool itself as ``{"error": "..."}``; which one a caller gets
    depends on where the call stopped -- the input contract or the work.  Both
    are the tool answering, so the assertions read the message without caring.
    """
    assert payload["ok"] is False, payload
    error = payload["error"]
    return str(error.get("message", "")) if isinstance(error, dict) else str(error)


def schedule_update_schema(registry) -> dict:
    """The update tool's published input schema, through the public API."""
    for entry in registry.to_anthropic_format():
        if entry["name"] == "schedule_update":
            return entry["input_schema"]
    raise AssertionError("schedule_update is not registered")


#: The keys ``schedule_runs`` reports but no edit may write.  Named as a set
#: rather than repeated per test, because the point of the split is that it is
#: a short, deliberate list -- every other key in the reply has to be accepted
#: by ``schedule_update``, or the read-back is a call that gets refused.
READ_ONLY_KEYS = {
    "id",
    "workflow_id",
    "step_key",
    "enabled",
    "delivery_mode",
    "delivery_target",
    "request_quote",
}


# ─── what is read can be sent back ──────────────────────────────────────────


def test_a_definition_read_back_can_be_sent_straight_in(tmp_path):
    """The edit is written against what the task says, not against memory."""
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(
        registry,
        criteria=["正文不少于 500 字"],
        verify_command="test -f 07_正文.md",
        produces=["07_正文.md"],
    )
    task_id = created["task"]["id"]

    task = read_definition(registry, task_id)
    echo = {key: value for key, value in task.items() if key not in READ_ONLY_KEYS}
    echo["task_id"] = task_id
    echo["intent"] = "原样写回"

    back = call(registry, "schedule_update", echo)

    assert back["ok"] is True, back
    # Nothing moved, and the reply says so rather than claiming a change.
    assert back["changed"] == []


def test_every_key_the_read_back_reports_is_one_the_writer_accepts(tmp_path):
    """A key with no home in the schema turns the whole read-back into a refusal."""
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task = read_definition(registry, created["task"]["id"])

    properties = set(schedule_update_schema(registry)["properties"])
    writable = set(task) - READ_ONLY_KEYS
    assert writable  # the schema is not only the read-only extras
    assert writable <= properties
    # And the extras really are outside: if one ever becomes writable it should
    # move out of this set, not linger in both.
    assert not (READ_ONLY_KEYS - {"id"}) & properties


def test_a_field_the_call_does_not_name_keeps_its_value(tmp_path):
    """The half-wipe this pins is invisible when it happens.

    ``acceptance`` is two fields, and rebuilding the pair whenever either was
    mentioned meant "fix the verify command" also cleared the criteria -- a
    task that lost the thing it is judged by goes on reporting success.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(
        registry,
        criteria=["正文不少于 500 字", "引用了原文档"],
        verify_command="test -f 07_正文.md",
        produces=["07_正文.md"],
    )
    task_id = created["task"]["id"]

    fixed = call(
        registry,
        "schedule_update",
        {
            "task_id": task_id,
            "intent": "verify_command 少写了一层目录，它实际写在 report/ 下",
            "verify_command": "test -f report/07_正文.md",
        },
    )

    assert fixed["ok"] is True, fixed
    assert fixed["task"]["verify_command"] == "test -f report/07_正文.md"
    assert fixed["task"]["criteria"] == ["正文不少于 500 字", "引用了原文档"]
    assert fixed["task"]["produces"] == ["07_正文.md"]
    assert fixed["task"]["prompt"] == "写一份日报"
    # The reply names the one thing that moved, not the field it shares a row
    # with.
    assert fixed["changed"] == ["判定依据"]


def test_an_edit_says_which_fields_it_actually_moved(tmp_path):
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)

    edited = call(
        registry,
        "schedule_update",
        {
            "task_id": created["task"]["id"],
            "intent": "名字和超时都要改",
            "name": "日报",
            "timeout_seconds": 600,
        },
    )

    assert edited["ok"] is True, edited
    assert set(edited["changed"]) == {"名称", "超时"}


def test_an_edit_that_names_nothing_is_refused_with_the_fields_to_name(tmp_path):
    """Silently doing nothing would look like an edit that had worked."""
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)

    message = refusal(
        call(
            registry,
            "schedule_update",
            {"task_id": created["task"]["id"], "intent": "改一下"},
        )
    )

    assert "name" in message
    assert "schedule_set_enabled" in message


def test_an_edit_of_an_unknown_task_says_where_to_look(tmp_path):
    _tools, registry, _workspace = make_tools(tmp_path)

    message = refusal(
        call(registry, "schedule_update", {"task_id": "nope", "intent": "改"})
    )

    assert "schedule_list" in message


# ─── the two things that are somebody else's ────────────────────────────────


def test_membership_and_the_quote_cannot_be_written_by_a_body(tmp_path):
    """Which chain a task belongs to, and who asked for it, are not settings.

    A body that could set membership could detach a step from the graph that
    explains it; a body that could set the quote could rewrite the record of
    who asked, which is the one thing that record is for.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    workflow = call(
        registry,
        "workflow_create",
        {
            "name": "日报链",
            "intent": "用户说每天要一份日报",
            "steps": [
                {
                    "key": "collect",
                    "name": "收集",
                    "action_type": "agent_task",
                    "instruction": "收集数据",
                    "trigger_type": "once",
                    "at": _soon(),
                },
                {
                    "key": "publish",
                    "name": "发布",
                    "action_type": "message",
                    "message_text": "日报好了",
                    "depends_on": ["collect"],
                },
            ],
        },
    )
    assert workflow["ok"] is True, workflow
    workflow_id = workflow["workflow"]["id"]
    step_id = workflow["workflow"]["steps"][0]["task_id"]

    before = read_definition(registry, step_id)
    assert before["request_quote"]  # the workflow was created with one

    message = refusal(
        call(
            registry,
            "schedule_update",
            {
                "task_id": step_id,
                "intent": "想把它拆出来、还想改掉出处，两样都不该成功",
                "workflow_id": "",
                "step_key": "",
                "request_quote": "我编的",
            },
        )
    )
    assert "unknown field" in message

    after = read_definition(registry, step_id)
    assert after["workflow_id"] == before["workflow_id"] == workflow_id
    assert after["step_key"] == "collect"
    assert after["request_quote"] == before["request_quote"]


def test_a_steps_trigger_belongs_to_the_graph(tmp_path):
    """Its timing *is* its upstreams, so retyping it here would break the edge."""
    _tools, registry, _workspace = make_tools(tmp_path)
    workflow = call(
        registry,
        "workflow_create",
        {
            "name": "两段链",
            "intent": "用户说要一条两段链",
            "steps": [
                {
                    "key": "first",
                    "name": "第一步",
                    "action_type": "message",
                    "message_text": "第一步",
                    "trigger_type": "once",
                    "at": _soon(),
                },
                {
                    "key": "second",
                    "name": "第二步",
                    "action_type": "message",
                    "message_text": "第二步",
                    "depends_on": ["first"],
                },
            ],
        },
    )
    second_id = workflow["workflow"]["steps"][1]["task_id"]

    message = refusal(
        call(
            registry,
            "schedule_update",
            {
                "task_id": second_id,
                "intent": "改成每天早上跑",
                "trigger_type": "daily",
                "time_of_day": "09:00",
            },
        )
    )
    assert "上游" in message
    # Nothing was written on the way to the refusal.
    assert read_definition(registry, second_id)["trigger_type"] == "signal"


def test_editing_a_step_reaches_the_workflow_that_rebuilds_it(tmp_path):
    """Otherwise the edit survives exactly until somebody moves an edge.

    Saving a workflow rewrites every one of its step tasks from the graph, so a
    step's row and the graph's own copy of it have to move together.
    """
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_tools(tmp_path)
    workflow = call(
        registry,
        "workflow_create",
        {
            "name": "单步链",
            "intent": "用户要一条链",
            "steps": [
                {
                    "key": "only",
                    "name": "唯一一步",
                    "action_type": "agent_task",
                    "instruction": "原来的指令",
                    "trigger_type": "once",
                    "at": _soon(),
                }
            ],
        },
    )
    workflow_id = workflow["workflow"]["id"]
    step_id = workflow["workflow"]["steps"][0]["task_id"]

    edited = call(
        registry,
        "schedule_update",
        {
            "task_id": step_id,
            "intent": "指令少写了一行，补上",
            "prompt": "改过的指令",
        },
    )
    assert edited["ok"] is True, edited

    store = SchedulerStore(db_path=Path(workflow["workflow"]["db_path"]))
    try:
        stored = next(w for w in store.list_workflows() if w.id == workflow_id)
        assert stored.steps[0].payload.get("prompt") == "改过的指令"
    finally:
        store.close()


# ─── a moment that has passed ───────────────────────────────────────────────


def test_a_one_off_that_already_fired_can_still_be_edited(tmp_path):
    """Refusing this is what leaves delete-and-recreate as the only repair.

    A one-off keeps its moment after it fires, so checking the stored moment
    against the clock refuses every later edit to that task -- renaming it,
    switching it off, correcting its products -- for a reason that has nothing
    to do with the edit.
    """
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    db_path = Path(created["task"]["db_path"])

    store = SchedulerStore(db_path=db_path)
    try:
        # A one-off whose moment has come and gone, which is what any task
        # created earlier than today looks like.
        fired = store.create_task(
            NewScheduledTask(
                name="早就跑过的任务",
                kind="message",
                trigger=TriggerSpec.once("2020-01-01T00:00:00+00:00", "UTC"),
                payload={"message_text": "早就发过了"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )
    finally:
        store.close()

    renamed = call(
        registry,
        "schedule_update",
        {"task_id": fired.id, "intent": "换个名字", "name": "日报（已跑过一次）"},
    )

    assert renamed["ok"] is True, renamed
    assert renamed["task"]["name"] == "日报（已跑过一次）"


def test_a_one_off_in_the_past_cannot_be_created(tmp_path):
    """A moment that has passed is a task that can never run.

    Silent when it happens -- the task exists, looks right, and simply never
    fires -- which is why the refusal is worth having.  The interface has
    always refused it; the tool path did not, so the same sentence from a
    person and from the agent meant two different things.
    """
    _tools, registry, _workspace = make_tools(tmp_path)

    message = refusal(create_task(registry, at="2026-04-20T10:00:00+08:00"))

    assert "晚于当前时间" in message


def test_a_one_off_can_be_moved_to_a_new_moment(tmp_path):
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    target = _soon(hours=5)

    moved = call(
        registry,
        "schedule_update",
        {"task_id": created["task"]["id"], "intent": "往后挪两小时", "at": target},
    )

    assert moved["ok"] is True, moved
    assert datetime.fromisoformat(moved["task"]["at"]) == datetime.fromisoformat(target)
    assert "触发方式" in moved["changed"]


# ─── the trigger round trip ─────────────────────────────────────────────────


def test_a_fan_in_trigger_survives_the_round_trip():
    """A dependent step's trigger is a list, even when it has one upstream.

    The list form is what the graph writes, and it is what lets a second
    upstream be added later without rewriting every dependent step.  A
    ``to_body``/``from_body`` pair that collapsed it to the single-name form
    whenever the list happened to be short would rewrite every dependent step
    in a chain -- identical in meaning at the moment it happens, and wrong the
    moment a second upstream is added.
    """
    from agent.scheduler import TriggerSpec, trigger_from_body, trigger_to_body

    fan_in = TriggerSpec.signal_all(["collect", "analyze"])
    round_tripped = trigger_from_body(trigger_to_body(fan_in))
    assert round_tripped.payload == fan_in.payload
    assert round_tripped.payload["mode"] == "all"

    single = TriggerSpec.signal("collect")
    back = trigger_from_body(trigger_to_body(single))
    assert back.payload == single.payload
    # The single-name form is not a one-element list: it is a different row,
    # and a caller has to get back the shape it asked for.
    assert "names" not in back.payload


def test_a_stored_fan_in_survives_an_edit_that_does_not_touch_it(tmp_path):
    """The same guarantee through the whole read-and-write path."""
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_tools(tmp_path)
    workflow = call(
        registry,
        "workflow_create",
        {
            "name": "汇合链",
            "intent": "用户要两条支线汇合",
            "steps": [
                {
                    "key": "a",
                    "name": "A",
                    "action_type": "message",
                    "message_text": "a",
                    "trigger_type": "once",
                    "at": _soon(),
                },
                {
                    "key": "b",
                    "name": "B",
                    "action_type": "message",
                    "message_text": "b",
                    "trigger_type": "once",
                    "at": _soon(hours=3),
                },
                {
                    "key": "join",
                    "name": "汇合",
                    "action_type": "agent_task",
                    "instruction": "汇总",
                    "depends_on": ["a", "b"],
                },
            ],
        },
    )
    assert workflow["ok"] is True, workflow
    by_key = {step["key"]: step["task_id"] for step in workflow["workflow"]["steps"]}
    join_id = by_key["join"]

    edited = call(
        registry,
        "schedule_update",
        {"task_id": join_id, "intent": "指令加一句话", "prompt": "汇总并归档"},
    )
    assert edited["ok"] is True, edited

    from agent.scheduler import RUN_SUCCESS_STATUS, task_signal_name

    store = SchedulerStore(db_path=Path(workflow["workflow"]["db_path"]))
    try:
        trigger = store.get_task(join_id).trigger
    finally:
        store.close()
    # The two upstreams' success signals, and the "all" that says both are
    # needed -- an edge is a signal name, not a step key.
    assert set(trigger.payload.get("names") or []) == {
        task_signal_name(by_key["a"], RUN_SUCCESS_STATUS),
        task_signal_name(by_key["b"], RUN_SUCCESS_STATUS),
    }
    assert trigger.payload.get("mode") == "all"


# ─── the switch ─────────────────────────────────────────────────────────────


def test_the_switch_has_one_tool_and_reports_where_it_ended(tmp_path):
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task_id = created["task"]["id"]

    off = control(registry, "schedule_set_enabled", {"task_id": task_id, "enabled": False})
    assert off["ok"] is True, off
    assert off["enabled"] is False
    assert read_definition(registry, task_id)["enabled"] is False

    on = control(registry, "schedule_set_enabled", {"task_id": task_id, "enabled": True})
    assert on["ok"] is True, on
    assert on["enabled"] is True


def test_a_live_workflows_step_refuses_its_own_switch(tmp_path):
    """Its switch is rewritten from the workflow's every time that is saved.

    Flipping it here would be a promise the next save breaks, so the refusal
    names the switch that does hold.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    workflow = call(
        registry,
        "workflow_create",
        {
            "name": "单步链",
            "intent": "用户要一条链",
            "steps": [
                {
                    "key": "only",
                    "name": "唯一一步",
                    "action_type": "message",
                    "message_text": "只做一件事",
                    "trigger_type": "once",
                    "at": _soon(),
                }
            ],
        },
    )
    step_id = workflow["workflow"]["steps"][0]["task_id"]

    message = refusal(
        control(registry, "schedule_set_enabled", {"task_id": step_id, "enabled": False})
    )

    assert "流程" in message


# ─── running one now, and stopping it ───────────────────────────────────────


def test_running_a_task_now_claims_a_run_and_says_where_to_look(tmp_path):
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)

    result = control(registry, "schedule_run", {"task_id": created["task"]["id"]})

    assert result["ok"] is True, result
    assert result["run_id"]
    assert result["trigger_source"] == "manual"
    assert "schedule_runs" in result["note"]


def test_a_disabled_task_can_still_be_run_by_hand(tmp_path):
    """"Turn it off, fix it, run it once to see" is the loop this exists for.

    The switch is about what happens on its own, not about being testable.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task_id = created["task"]["id"]
    control(registry, "schedule_set_enabled", {"task_id": task_id, "enabled": False})

    result = control(registry, "schedule_run", {"task_id": task_id})

    assert result["ok"] is True, result


def test_running_a_task_that_is_already_running_is_refused(tmp_path):
    """A second run would either wait invisibly or collide with the first."""
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task_id = created["task"]["id"]
    first = control(registry, "schedule_run", {"task_id": task_id})

    message = refusal(control(registry, "schedule_run", {"task_id": task_id}))

    assert "正在运行" in message
    assert "schedule_cancel" in message
    assert first["run_id"] in message


def test_rerunning_a_run_repeats_its_snapshot_not_the_current_definition(tmp_path):
    """So an outcome is about the work, and a fix cannot be mistaken for a flake.

    The distinction is the whole reason both spellings exist: one answers "is
    this definition right now", the other "was that run unlucky".
    """
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task_id = created["task"]["id"]
    first = control(registry, "schedule_run", {"task_id": task_id})

    store = SchedulerStore(db_path=Path(created["task"]["db_path"]))
    try:
        store.complete_run(
            task_id,
            first["run_id"],
            finished_at=datetime.now(timezone.utc),
            status="failed",
            summary="",
            error="炸了",
        )
    finally:
        store.close()

    again = control(
        registry, "schedule_run", {"task_id": task_id, "run_id": first["run_id"]}
    )

    assert again["ok"] is True, again
    assert again["run_id"] != first["run_id"]
    assert again["trigger_source"] == "retry_snapshot"


def test_cancelling_the_run_in_flight_asks_rather_than_kills(tmp_path):
    """The answer says *asked*: the run stops at its next checkpoint.

    Reporting a completed cancellation would describe something this side of
    the process cannot see.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task_id = created["task"]["id"]
    run = control(registry, "schedule_run", {"task_id": task_id})

    result = control(registry, "schedule_cancel", {"task_id": task_id})

    assert result["ok"] is True, result
    assert result["run_id"] == run["run_id"]
    assert result["cancel_requested"] is True
    assert "请求取消" in result["note"]


def test_cancelling_nothing_names_the_switch_that_would_help(tmp_path):
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)

    message = refusal(
        control(registry, "schedule_cancel", {"task_id": created["task"]["id"]})
    )

    assert "schedule_set_enabled" in message


# ─── editing a workflow ─────────────────────────────────────────────────────


def _two_step_chain(registry, first_text: str = "第一步") -> dict:
    return call(
        registry,
        "workflow_create",
        {
            "name": "两段链",
            "intent": "用户要一条两段链",
            "steps": [
                {
                    "key": "first",
                    "name": "第一步",
                    "action_type": "message",
                    "message_text": first_text,
                    "trigger_type": "once",
                    "at": _soon(),
                },
                {
                    "key": "second",
                    "name": "第二步",
                    "action_type": "agent_task",
                    "instruction": "第二步",
                    "depends_on": ["first"],
                },
            ],
        },
    )


def test_updating_a_workflow_keeps_the_task_ids_of_the_steps_it_keeps(tmp_path):
    """The id is what the steps below are subscribed to.

    Rebuilding a chain instead of editing it gives every step a new task id,
    which silently disconnects every edge -- the failure that made editing a
    workflow, rather than recreating it, the only safe way to change one.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    workflow = _two_step_chain(registry)["workflow"]
    ids_before = {step["key"]: step["task_id"] for step in workflow["steps"]}

    updated = call(
        registry,
        "workflow_update",
        {
            "workflow_id": workflow["id"],
            "intent": "第一步的文案改了",
            "steps": [
                {
                    "key": "first",
                    "name": "第一步",
                    "action_type": "message",
                    "message_text": "改过的第一步",
                    "trigger_type": "once",
                    "at": _soon(),
                },
                {
                    "key": "second",
                    "name": "第二步",
                    "action_type": "agent_task",
                    "instruction": "第二步",
                    "depends_on": ["first"],
                },
            ],
        },
    )

    assert updated["ok"] is True, updated
    assert updated["removed_steps"] == []
    ids_after = {step["key"]: step["task_id"] for step in updated["workflow"]["steps"]}
    assert ids_after == ids_before


def test_a_removed_step_is_disabled_rather_than_deleted(tmp_path):
    """Its run history is the only record of what it did."""
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_tools(tmp_path)
    created = call(
        registry,
        "workflow_create",
        {
            "name": "两步链",
            "intent": "用户要一条两步链",
            "steps": [
                {
                    "key": "keep",
                    "name": "保留",
                    "action_type": "message",
                    "message_text": "留下",
                    "trigger_type": "once",
                    "at": _soon(),
                },
                {
                    "key": "drop",
                    "name": "去掉",
                    "action_type": "message",
                    "message_text": "去掉",
                    "trigger_type": "once",
                    "at": _soon(hours=3),
                },
            ],
        },
    )
    workflow = created["workflow"]
    dropped_id = next(s["task_id"] for s in workflow["steps"] if s["key"] == "drop")

    updated = call(
        registry,
        "workflow_update",
        {
            "workflow_id": workflow["id"],
            "intent": "去掉第二步",
            "steps": [
                {
                    "key": "keep",
                    "name": "保留",
                    "action_type": "message",
                    "message_text": "留下",
                    "trigger_type": "once",
                    "at": _soon(),
                }
            ],
        },
    )

    assert updated["ok"] is True, updated
    assert updated["removed_steps"] == ["drop"]
    store = SchedulerStore(db_path=Path(workflow["db_path"]))
    try:
        leftover = store.get_task(dropped_id)
        assert leftover is not None
        assert leftover.enabled is False
    finally:
        store.close()


# ─── the model a task runs on ───────────────────────────────────────────────


def _patch_routing(monkeypatch, *models: str) -> None:
    """Make the routing table say exactly these models exist."""
    import agent.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda *a, **k: ({"providers": {}, "model": models[0] if models else ""}, None),
    )


def test_a_model_no_provider_group_owns_is_refused_where_somebody_can_see_it(
    tmp_path, monkeypatch
):
    """"Pinned to a foreign group's model" fails at 3am, naming a model nobody chose."""
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    _patch_routing(monkeypatch, "good-model")

    message = refusal(
        call(
            registry,
            "schedule_update",
            {
                "task_id": created["task"]["id"],
                "intent": "换模型",
                "model_override": "ghost",
            },
        )
    )

    assert "ghost" in message


def test_a_carried_model_is_not_re_validated(tmp_path, monkeypatch):
    """A retired model id must still be editable *off* the task it is pinned to.

    Re-checking a value that is merely being carried over would refuse every
    edit to such a task -- including the edit that removes it.
    """
    _tools, registry, _workspace = make_tools(tmp_path)
    created = create_task(registry)
    task_id = created["task"]["id"]

    _patch_routing(monkeypatch, "good-model")
    # Pin it while the model is still routable, then retire it.
    pinned = call(
        registry,
        "schedule_update",
        {
            "task_id": task_id,
            "intent": "固定用这个模型",
            "model_override": "good-model",
        },
    )
    assert pinned["ok"] is True, pinned
    assert pinned["task"]["model_override"] == "good-model"

    _patch_routing(monkeypatch, "some-other-model")

    # Carrying it back unchanged is not a new promise.
    renamed = call(
        registry,
        "schedule_update",
        {
            "task_id": task_id,
            "intent": "改个名字",
            "name": "日报",
            "model_override": "good-model",
        },
    )
    assert renamed["ok"] is True, renamed

    cleared = call(
        registry,
        "schedule_update",
        {"task_id": task_id, "intent": "别再固定模型了", "model_override": None},
    )
    assert cleared["ok"] is True, cleared
    assert cleared["task"]["model_override"] is None


# ─── registration ───────────────────────────────────────────────────────────


def test_the_edit_and_control_tools_declare_what_they_do(tmp_path):
    """``requires_intent``, not ``requires_request``, and a real write.

    The asker's own sentence is hardest to point at mid-repair -- they asked
    why it broke, not for a particular field to change -- so gating these on
    the user's words would leave delete-and-recreate as the only repair.
    Declaring "what this does and why" keeps them countable without blocking
    the fix.
    """
    _tools, registry, _workspace = make_tools(tmp_path)

    for name in (
        "schedule_update",
        "schedule_set_enabled",
        "schedule_run",
        "schedule_cancel",
        "workflow_update",
    ):
        assert name in registry.list_tools()
        assert registry.tool_capabilities(name) == frozenset(
            {"state_write", "requires_intent"}
        ), name

    # The reads stayed reads.
    assert registry.tool_capabilities("schedule_runs") == frozenset({"read"})
    assert registry.tool_capabilities("workflow_list") == frozenset({"read"})
