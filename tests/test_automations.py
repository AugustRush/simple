"""自动化 has one thing in it, not two.

The premise, and where it is written down
-----------------------------------------
A chain ("workflow") is a graph of **tasks**: ``WorkflowStep`` is documented as "one step
of a workflow: a task, plus what has to finish before it runs", a chain run is anchored to
a task run (``workflow_runs.root_task_id``), and the task payload already carries
``workflow_id`` / ``step_key``.  ``tests/test_scheduler_workflows.py`` opens with the same
sentence: *"A step is a task that already exists."*

What was still split was the presentation -- two list endpoints, two tool families, two
page tabs -- which is what makes an operator ask "task or workflow?" when the honest answer
is "the same thing with one step or several".

These tests pin the read model that answers the list question in one shape, and in
particular the invariant that makes it safe to show: **a step is never its own row**. A
step listed beside its chain is the same automation counted twice, and a page that adds up
its rows would then disagree with the badge -- the exact failure the attention snapshot's
"add up the rows and check them" invitation exists to catch.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    DeliveryTarget,
    NewScheduledTask,
    SchedulerStore,
    TriggerSpec,
    Workflow,
    WorkflowStep,
)
from agent.scheduler.automations import (
    AUTOMATION_KIND_CHAIN,
    AUTOMATION_KIND_TASK,
    automation_rows,
)


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp`` (the sandbox cannot use pytest's)."""
    path = Path("/tmp") / f"simple-automations-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def make_task(store: SchedulerStore, name: str, *, at: str = "2026-05-01T11:59:00+00:00"):
    return store.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=TriggerSpec.once(at, "UTC"),
            payload={"prompt": f"do {name}"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )


def make_chain(store: SchedulerStore, name: str = "chain") -> Workflow:
    return store.create_workflow(
        Workflow(
            name=name,
            steps=[
                WorkflowStep(
                    key="collect",
                    name="Collect",
                    kind="agent_prompt",
                    payload={"prompt": "collect"},
                    trigger=TriggerSpec.once("2026-05-01T12:00:00+00:00", "UTC"),
                ),
                WorkflowStep(
                    key="analyze",
                    name="Analyze",
                    kind="agent_prompt",
                    payload={"prompt": "analyze"},
                    depends_on=["collect"],
                ),
                WorkflowStep(
                    key="publish",
                    name="Publish",
                    kind="agent_prompt",
                    payload={"prompt": "publish"},
                    depends_on=["collect"],
                ),
            ],
        )
    )


# ── one shape ───────────────────────────────────────────────────────────────


def test_a_plain_task_is_a_chain_of_one_step(tmp_path) -> None:
    """The same shape, so a list column never has to branch on the kind."""
    store = make_store(tmp_path)
    try:
        make_task(store, "lonely")
        (row,) = automation_rows(store)

        assert row["kind"] == AUTOMATION_KIND_TASK
        assert row["step_count"] == 1
        assert row["task_id"] and not row["workflow_id"]
        assert row["trigger_type"] == "once"
        assert row["next_run_at"] == "2026-05-01T11:59:00+00:00"
    finally:
        store.close()


def test_a_chain_is_one_row_with_its_steps_counted(tmp_path) -> None:
    store = make_store(tmp_path)
    try:
        make_chain(store)
        (row,) = automation_rows(store)

        assert row["kind"] == AUTOMATION_KIND_CHAIN
        assert row["step_count"] == 3
        assert row["workflow_id"]
        # The entry step's clock is the chain's clock: "when does this next start".
        assert row["trigger_type"] == "once"
        assert row["next_run_at"] == "2026-05-01T12:00:00+00:00"
        # ...and the entry step's task id is what a detail panel opens.
        assert row["task_id"]
    finally:
        store.close()


def test_a_step_is_never_its_own_row(tmp_path) -> None:
    """The invariant that makes the list safe to add up.

    Three steps plus one standalone task is **two** rows, not four. A step shown beside its
    chain is the same automation counted twice, and the attention number would then be a
    number the page cannot reproduce from its rows.
    """
    store = make_store(tmp_path)
    try:
        make_chain(store)
        make_task(store, "lonely")

        rows = automation_rows(store)

        assert len(rows) == 2
        assert len(store.list_tasks()) == 4, "the steps really are tasks in the store"
        kinds = sorted(row["kind"] for row in rows)
        assert kinds == [AUTOMATION_KIND_CHAIN, AUTOMATION_KIND_TASK]
    finally:
        store.close()


def test_an_empty_store_answers_an_empty_list(tmp_path) -> None:
    """Not an error, not a null: a page with nothing to show has a list of nothing."""
    store = make_store(tmp_path)
    try:
        assert automation_rows(store) == []
    finally:
        store.close()


# ── the numbers the page checks against the badge ───────────────────────────


def test_a_chain_s_attention_is_the_sum_of_its_steps(tmp_path) -> None:
    """A chain's badge is its steps' badge: the person is being asked about the chain."""
    store = make_store(tmp_path)
    try:
        make_chain(store)
        tasks = store.step_tasks(store.list_workflows()[0].id)
        snapshot = store.attention_snapshot()
        counts = snapshot["counts"]

        (row,) = automation_rows(store, snapshot)

        assert row["attention"] == sum(int(counts.get(task.id, 0)) for task in tasks.values())
    finally:
        store.close()


def test_the_rows_add_up_to_the_snapshot(tmp_path) -> None:
    """The invitation the snapshot makes, honoured: `sum(rows) == total`."""
    store = make_store(tmp_path)
    try:
        make_chain(store)
        make_task(store, "lonely")
        snapshot = store.attention_snapshot()

        rows = automation_rows(store, snapshot)

        assert sum(row["attention"] for row in rows) == snapshot["total"]
    finally:
        store.close()


# ── the route ───────────────────────────────────────────────────────────────


def test_the_route_serves_the_list_and_the_badge_from_one_snapshot(tmp_path, monkeypatch) -> None:
    """The handler answers with the rows *and* the number, from one snapshot.

    Called directly rather than over HTTP: what is worth pinning is that the endpoint
    exists and that its body carries both halves from the same read. The two existing
    endpoints keep their own editors; this one is the list.
    """
    import asyncio
    import json

    from agent.channels.web import WebChannel, WebConfig

    store = make_store(tmp_path)
    make_chain(store)
    make_task(store, "lonely")

    channel = WebChannel(WebConfig())
    monkeypatch.setattr(channel, "_authorized", lambda request: True)
    # The handler imports `open_store` inside the function, so this is what it resolves.
    monkeypatch.setattr("agent.scheduler.open_store", lambda *args, **kwargs: store)
    try:
        response = asyncio.run(channel._automations(object()))
        body = json.loads(response.body)
    finally:
        store.close()

    assert {row["kind"] for row in body["automations"]} == {"task", "chain"}
    assert body["unseen_attention"] == 0
    assert "permission_profiles" in body, "the editor's vocabulary travels with the list"


def test_the_patrol_s_own_errands_are_not_automations(tmp_path) -> None:
    """A task the agent created to do its own reading is machinery, not something you set up.

    Measured on a fresh install: five of the eight rows in 自动化 were one-off
    errands the patrol had created for itself, and had already finished. The
    result of each is on the proposal that started it, so the row said nothing
    the 自主 page did not already say better.
    """
    store = make_store(tmp_path)
    try:
        make_task(store, "每天早上看日志")
        store.create_task(
            NewScheduledTask(
                name="读一遍那 2 次失败的运行",
                kind="agent_prompt",
                trigger=TriggerSpec.once("2026-05-01T11:58:00+00:00", "UTC"),
                payload={
                    "prompt": "read the failures",
                    "origin": "autonomy",
                    "proposal_id": "p1",
                },
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )

        rows = automation_rows(store)

        assert [row["name"] for row in rows] == ["每天早上看日志"]
        assert len(store.list_tasks()) == 2, "it is still a task in the store"
    finally:
        store.close()


def test_own_errand_ids_reads_one_definition_of_own_errand(tmp_path) -> None:
    """The list and the patrol must not disagree about which tasks are the agent's."""
    from agent.scheduler.automations import own_errand_task_ids

    store = make_store(tmp_path)
    try:
        mine = make_task(store, "每天看日志")
        theirs = store.create_task(
            NewScheduledTask(
                name="干一件事",
                kind="agent_prompt",
                trigger=TriggerSpec.once("2026-05-01T11:58:00+00:00", "UTC"),
                payload={"prompt": "x", "origin": "autonomy"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )

        hidden = own_errand_task_ids(store)

        assert hidden == {theirs.id}
        assert mine.id not in hidden
    finally:
        store.close()
