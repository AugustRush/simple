"""The files a task promises, and whether they are there when it ends.

The defect these tests exist for is a missing half of the contract.  A task
could say what *done* meant (``acceptance``) but not what *work product* it
owed, so the path a run was supposed to write existed only in prose -- or,
worse, only inside the ``verify_command`` that checked a file the run had
never been told about.  A chain built that way fails every night against a
path nobody named, and the failure reads as a bug in the agent.

One concept answers it, and only one: ``produces``, a list of paths relative
to the task's workspace.  A product is named by its path and nothing else --
a second name would be a second identity, and two identities drift.  From
that one declaration three readers are served that all used to guess: the run
is told where to write, the downstream step is handed the address, and "the
file is there" becomes a check nobody has to write.

Three things are kept apart here, and collapsing any two is the failure this
prevents:

* what the task *promised* -- read off the task row, edited with it;
* what the run *left* -- measured on disk when the run ended;
* whether those agree, which is the only thing that makes either mean
  anything to the step below.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    Acceptance,
    DeliveryTarget,
    ExecutionResult,
    NewScheduledTask,
    SchedulerService,
    SchedulerStore,
    TriggerSpec,
    Workflow,
    WorkflowStep,
    missing_products,
    normalize_products,
    product_path_problem,
    product_report,
    products_for_handoff,
    products_payload,
    validate_products,
)
from agent.verification import VERDICT_FAILED, VERDICT_NONE, VERDICT_PASSED

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

#: What ``write_product`` puts in a declared file.  The byte count travels
#: into the run's report, so the length is asserted rather than spelled out.
PRODUCT_TEXT = "work product\n"
PRODUCT_BYTES = len(PRODUCT_TEXT.encode("utf-8"))


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp``.

    Overrides pytest's own fixture, which fails in this environment: its
    per-run root under the system temp directory cannot be created when the
    session is sandboxed, so every test that asks for ``tmp_path`` errors out
    before it runs.  ``/tmp`` is writable and just as disposable.
    """
    path = Path("/tmp") / f"simple-products-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def make_store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def clock(at: str = "2026-05-01T11:59:00+00:00") -> TriggerSpec:
    return TriggerSpec.once(at, "UTC")


def make_service(
    store: SchedulerStore,
    *,
    on_run=None,
    self_report: tuple[str, str] | None = None,
    delivery_status: str = "delivered",
) -> SchedulerService:
    """A service whose agent runs succeed, and optionally leave files behind.

    *on_run* is the stand-in for the agent actually doing the work: it is
    handed the task row, so a test can write exactly the file the task
    declared -- or deliberately not write it, which is the whole point of the
    missing-product case.  The acceptance check itself is never faked.
    """

    async def executor(task, run):
        if on_run is not None:
            on_run(task, run)
        verdict, reason = self_report or (VERDICT_NONE, "")
        return ExecutionResult(
            summary=f"ran {task.name}",
            text_output=f"out {task.name}",
            self_report_verdict=verdict,
            self_report_reason=reason,
        )

    async def unused(*args, **kwargs):
        raise AssertionError("system executor should not be called")

    async def delivery(task, run, result):
        return delivery_status

    return SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=unused,
        delivery=delivery,
        poll_seconds=30,
        lease_seconds=300,
    )


def make_task(
    store: SchedulerStore,
    *,
    name: str = "nightly",
    workspace_root: str,
    acceptance: Acceptance | None = None,
    produces: list[str] | None = None,
) -> object:
    return store.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=clock(),
            payload={"prompt": "do the thing"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            workspace_root=workspace_root,
            acceptance=acceptance or Acceptance(),
            produces=list(produces or []),
        ),
        now=NOW,
    )


def make_due(store: SchedulerStore, task_id: str, when: datetime = NOW) -> None:
    """Move a task's next occurrence into the past so the clock can claim it."""
    store._conn.execute(
        "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
        ((when - timedelta(minutes=1)).astimezone(timezone.utc).isoformat(), task_id),
    )
    store._conn.commit()


async def run_to_terminal(service: SchedulerService, now: datetime = NOW) -> None:
    for hop in range(3):
        await service.run_once(now=now + timedelta(seconds=30 * hop))


def write_product(*names: str):
    """An *on_run* that writes exactly ``names`` into the task's workspace."""

    def hook(task, run):
        for name in names:
            target = Path(task.workspace_root) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(PRODUCT_TEXT, encoding="utf-8")

    return hook


# ── 1. A product is a path inside the workspace, and nothing else ───────────


def test_a_product_is_cleaned_into_the_one_shape_the_store_writes():
    """Deduplicated and trimmed, so two spellings of one path are one promise.

    The path *is* the identity.  A duplicated entry would make "which of these
    did the run leave" answerable twice with different answers, which is the
    same class of defect as giving a product a separate name.
    """
    assert normalize_products([" out/report.md ", "out/report.md", "", None]) == [
        "out/report.md"
    ]
    # A non-list arrives in the shape "nothing was declared", not as a crash.
    assert normalize_products(None) == []
    assert normalize_products("out/report.md") == []


def test_a_product_outside_the_workspace_is_refused_where_the_task_is_written(
    tmp_path,
):
    """Refused while somebody is looking at it, not discovered at 3am.

    An absolute path would make the scheduler vouch for a file it has no
    business reaching, and would hand the downstream run a location the
    declaring task's own permission profile never allowed it to write.
    """
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError) as caught:
            make_task(store, workspace_root=str(tmp_path), produces=["/etc/passwd"])
        # The path is quoted, so the author can see which one to fix.
        assert "/etc/passwd" in str(caught.value)
        assert "绝对路径" in str(caught.value)
        # And nothing was written.
        assert store.list_tasks() == []
    finally:
        store.close()


def test_a_product_that_walks_out_with_dotdot_is_refused(tmp_path):
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError) as caught:
            make_task(store, workspace_root=str(tmp_path), produces=["../elsewhere.md"])
        assert ".." in str(caught.value)
        assert store.list_tasks() == []
    finally:
        store.close()


def test_a_directory_is_not_a_product(tmp_path):
    """An empty directory would satisfy an existence check while containing
    nothing, so "the folder is there" must not pass for "the work is there"."""
    assert product_path_problem("out/") is not None
    assert product_path_problem("out") is None

    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            make_task(store, workspace_root=str(tmp_path), produces=["out/"])
    finally:
        store.close()


def test_too_many_products_are_refused(tmp_path):
    """A cap, because a declaration nobody can read is a declaration nobody
    checks -- and because the list travels into every downstream prompt."""
    from agent.scheduler import MAX_PRODUCTS

    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError) as caught:
            make_task(
                store,
                workspace_root=str(tmp_path),
                produces=[f"out/{i}.md" for i in range(MAX_PRODUCTS + 1)],
            )
        assert str(MAX_PRODUCTS) in str(caught.value)
    finally:
        store.close()


def test_an_overlong_product_path_is_refused(tmp_path):
    from agent.scheduler import MAX_PRODUCT_PATH_CHARS

    too_long = "out/" + ("x" * MAX_PRODUCT_PATH_CHARS) + ".md"
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            make_task(store, workspace_root=str(tmp_path), produces=[too_long])
    finally:
        store.close()


def test_validate_products_is_the_one_gate_both_callers_use(tmp_path):
    """Written and edited go through the same answer, or a task could be
    repaired into a state it could never have been created in."""
    validate_products(["out/report.md"], label="任务")
    with pytest.raises(ValueError) as caught:
        validate_products(["/abs.md"], label="流程步骤")
    # The label names *which* declaration is wrong, because a workflow has
    # many and "a product path is wrong" does not say where to look.
    assert "流程步骤" in str(caught.value)


# ── 2. The report is measured when the run ends ─────────────────────────────


def test_a_product_report_names_what_is_there_and_how_big(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.md").write_text("hello", encoding="utf-8")

    report = product_report(str(tmp_path), ["out/report.md", "out/missing.md"])

    assert report == [
        {
            "path": "out/report.md",
            "absolute": str(tmp_path / "out" / "report.md"),
            "exists": True,
            "bytes": 5,
        },
        {
            "path": "out/missing.md",
            "absolute": str(tmp_path / "out" / "missing.md"),
            "exists": False,
            "bytes": 0,
        },
    ]
    assert missing_products(report) == ["out/missing.md"]


def test_a_directory_at_the_declared_path_does_not_count_as_produced(tmp_path):
    """``is_file`` rather than ``exists``: a folder named like the artifact is
    the shape a run leaves when it failed to write the file."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.md").mkdir()

    report = product_report(str(tmp_path), ["out/report.md"])

    assert report[0]["exists"] is False
    assert missing_products(report) == ["out/report.md"]


def test_a_product_report_is_defensive_against_a_corrupt_column():
    """It rides beside a run that already happened; a blob nobody can parse
    must not make that run's history unreadable.  "Nothing recorded" is the
    honest reading, and is exactly what a pre-column run really has."""
    assert products_payload(None) == []
    assert products_payload("not a list") == []
    assert products_payload([{"path": ""}, "junk", {"nope": 1}]) == []
    # A well-formed entry survives, with its types coerced.
    assert products_payload([{"path": "a.md", "exists": 1, "bytes": "7"}]) == [
        {"path": "a.md", "absolute": "", "exists": True, "bytes": 7}
    ]


# ── 3. A promised file that is not there is a failure ───────────────────────


def test_a_declared_product_that_was_written_passes_and_is_recorded(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path), produces=["out/report.md"])
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store, on_run=write_product("out/report.md"))))

        run = store.list_runs(task.id)[0]
        assert run.status == "succeeded"
        assert run.verdict == VERDICT_PASSED
        assert run.error == ""
        # The measurement is on the row, not recomputed when somebody asks:
        # the file may be overwritten or deleted afterwards, and "was it there
        # when the work finished" is the only version answerable later.
        assert run.products == [
            {
                "path": "out/report.md",
                "absolute": str(
                    Path(task.workspace_root) / "out" / "report.md"
                ),
                "exists": True,
                "bytes": PRODUCT_BYTES,
            }
        ]
        assert missing_products(run.products) == []
    finally:
        store.close()


def test_a_declared_product_that_was_not_written_fails_the_run(tmp_path):
    """The strongest of the three sources, and the only one that is neither a
    claim nor a command: a file that was promised and is absent."""
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path), produces=["out/report.md"])
        make_due(store, task.id)

        # Delivered fine, reported fine, wrote nothing.
        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == "failed"
        assert run.verdict == VERDICT_FAILED
        # The reason names the path, because "a product is missing" does not
        # say which one and there can be many.
        assert "声明的产物没有产出" in run.error
        assert "out/report.md" in run.error
        assert missing_products(run.products) == ["out/report.md"]
    finally:
        store.close()


def test_a_missing_product_fails_the_run_even_when_the_check_passed(tmp_path):
    """A check that passes is evidence about the narrow thing it tests, not
    that the job is done.  The two sources are combined, not chosen between."""
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(criteria=["always true"], verify_command="true"),
            produces=["out/report.md"],
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == "failed"
        assert run.verdict == VERDICT_FAILED
        # Both survivors: the check's own result is still recorded as what it
        # was, and the missing product is named alongside it.
        assert run.verification is not None
        assert run.verification.exit_code == 0
        assert "out/report.md" in run.error
    finally:
        store.close()


def test_a_run_that_declared_nothing_is_unaffected(tmp_path):
    """The no-regression case, and it is the majority case.

    Most scheduled tasks have no products.  "Declared nothing" must not read
    as "produced nothing", which is why the report is an empty list and the
    verdict stays unjudged rather than being judged against zero files.
    """
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == "succeeded"
        assert run.verdict == VERDICT_NONE
        assert run.products == []
        assert run.error == ""
    finally:
        store.close()


def test_the_product_declaration_is_part_of_a_task_s_identity(tmp_path):
    """Otherwise "produce this file" is answered with the existing task that
    produces nothing -- and reported as done."""
    store = make_store(tmp_path)
    try:
        plain = make_task(store, name="same", workspace_root=str(tmp_path))

        def probe(produces):
            return store.find_matching_task(
                NewScheduledTask(
                    name="same",
                    kind="agent_prompt",
                    trigger=clock(),
                    payload={"prompt": "do the thing"},
                    delivery_mode="standalone",
                    delivery_target=DeliveryTarget.standalone(),
                    workspace_root=str(tmp_path),
                    acceptance=Acceptance(),
                    produces=produces,
                )
            )

        assert probe([]) is not None and probe([]).id == plain.id
        assert probe(["out/report.md"]) is None
    finally:
        store.close()


def test_the_sweep_and_the_create_path_agree_about_what_a_duplicate_is(tmp_path):
    """The same pair of rows has to get the same verdict from both.

    ``find_matching_task`` decides whether a create is a repeat of something
    already there; ``disable_duplicate_enabled_tasks`` runs on every tick and
    switches the later of two identical tasks off.  They compared two
    hand-written column lists, and the sweep's was missing the two halves of
    the contract -- so two tasks disagreeing about what "done" means, or about
    which files they leave behind, were distinct to creation and duplicates to
    the sweep.  The later one was switched off, which is the opposite of what
    the create path had just promised.
    """
    store = make_store(tmp_path)
    try:
        first = make_task(
            store, name="same", workspace_root=str(tmp_path), produces=["out/a.md"]
        )
        second = make_task(
            store, name="same", workspace_root=str(tmp_path), produces=["out/b.md"]
        )

        # Two different promises are two tasks, so nothing is switched off.
        assert store.disable_duplicate_enabled_tasks() == 0
        assert {task.id for task in store.list_tasks() if task.enabled} == {
            first.id,
            second.id,
        }

        # A third making the *same* promise as the first is a duplicate, and
        # one of that pair is switched off.  Which one is the store's call --
        # ordering against an equal creation time is arbitrary by design -- so
        # the assertion is about the pair, not about a particular row.
        third = make_task(
            store, name="same", workspace_root=str(tmp_path), produces=["out/a.md"]
        )
        assert store.disable_duplicate_enabled_tasks() == 1
        enabled = {task.id: task for task in store.list_tasks() if task.enabled}
        assert second.id in enabled
        assert len({first.id, third.id} & set(enabled)) == 1
        # And what survived is one task per promise, which is the property the
        # create path relies on.
        assert sorted(
            tuple(task.produces) for task in enabled.values()
        ) == [("out/a.md",), ("out/b.md",)]
    finally:
        store.close()


def test_the_two_readers_share_one_column_list():
    """Pinned as a property, because a second list is how this broke.

    A column added to one list and not the other is invisible in both: each
    function is internally consistent, and they disagree only about the rows
    they both look at.
    """
    from agent.scheduler.store import SchedulerStore, _TASK_IDENTITY_COLUMNS

    assert "acceptance_json" in _TASK_IDENTITY_COLUMNS
    assert "produces_json" in _TASK_IDENTITY_COLUMNS
    assert "enabled" not in _TASK_IDENTITY_COLUMNS

    # The values line up with the columns, so the zip in each caller pairs the
    # right value with the right name.
    spec = NewScheduledTask(
        name="same",
        kind="agent_prompt",
        trigger=clock(),
        payload={"prompt": "do the thing"},
        delivery_mode="standalone",
        delivery_target=DeliveryTarget.standalone(),
    )
    assert len(SchedulerStore._identity_values(spec)) == len(_TASK_IDENTITY_COLUMNS)


def test_the_product_report_survives_reopening_the_database(tmp_path):
    """It is stored, not derived at read time."""
    store = make_store(tmp_path)
    db_path = store.db_path
    try:
        task = make_task(store, workspace_root=str(tmp_path), produces=["out/report.md"])
        make_due(store, task.id)
        asyncio.run(run_to_terminal(make_service(store, on_run=write_product("out/report.md"))))
        run_id = store.list_runs(task.id)[0].id
    finally:
        store.close()

    reopened = SchedulerStore(db_path=db_path)
    try:
        run = reopened.get_run(task.id, run_id)
        assert missing_products(run.products) == []
        assert run.products[0]["bytes"] == PRODUCT_BYTES
        # And the declaration the run was held to, read back from the task.
        assert reopened.get_task(task.id).produces == ["out/report.md"]
    finally:
        reopened.close()


def test_a_task_s_products_round_trip_through_create_and_read(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(
            store, workspace_root=str(tmp_path), produces=["a.md", "out/b.md"]
        )
        assert task.produces == ["a.md", "out/b.md"]
        assert store.get_task(task.id).produces == ["a.md", "out/b.md"]
    finally:
        store.close()


def test_editing_a_task_s_products_is_checked_by_the_same_gate(tmp_path):
    """Otherwise a task could be repaired into a state it could never have
    been created in, and the failure would surface at 3am."""
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path), produces=["a.md"])

        def edited(produces):
            return NewScheduledTask(
                name=task.name,
                kind=task.kind,
                trigger=clock(),
                payload={"prompt": "do the thing"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
                workspace_root=str(tmp_path),
                acceptance=Acceptance(),
                produces=produces,
            )

        with pytest.raises(ValueError):
            store.update_task(task.id, edited(["/etc/passwd"]))
        with pytest.raises(ValueError):
            store.update_task(task.id, edited(["../out.md"]))
        # And the good declaration is still what is stored, because the
        # refusal happened before anything was written.
        assert store.get_task(task.id).produces == ["a.md"]

        # A repair to a *valid* declaration is allowed, which is what keeps
        # the gate from being a wall.
        store.update_task(task.id, edited(["a.md", "b.md"]))
        assert store.get_task(task.id).produces == ["a.md", "b.md"]
    finally:
        store.close()


# ── 4. An old database upgrades without inventing anything ──────────────────


def test_an_old_database_gains_the_columns_and_backfills_nothing(tmp_path):
    """An empty product list means "nothing was promised", which is also true
    of every run recorded before this existed -- so the migration rewrites no
    history and leaves both columns at their default."""
    store = make_store(tmp_path)
    db_path = store.db_path
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        make_due(store, task.id)
        asyncio.run(run_to_terminal(make_service(store)))
        run_id = store.list_runs(task.id)[0].id
        task_id = task.id
    finally:
        store.close()

    # Rewind the schema version and drop the two columns, which is what a
    # database written before this feature looks like on disk.
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE scheduled_tasks SET produces_json = '[]'")
        conn.execute("UPDATE scheduled_task_runs SET products_json = '[]'")
        conn.execute("PRAGMA user_version = 12")
        conn.commit()
    finally:
        conn.close()

    reopened = SchedulerStore(db_path=db_path)
    try:
        assert reopened.get_task(task_id).produces == []
        run = reopened.get_run(task_id, run_id)
        assert run.products == []
        # The run's other history is untouched -- the migration added columns,
        # it did not rewrite the row.
        assert run.status == "succeeded"
    finally:
        reopened.close()


# ── 5. What is handed to the step below ─────────────────────────────────────


def test_a_product_that_is_there_is_handed_over_as_an_address():
    report = [
        {
            "path": "out/report.md",
            "absolute": "/w/out/report.md",
            "exists": True,
            "bytes": 12,
        }
    ]
    handoff = products_for_handoff(["out/report.md"], report)

    assert handoff == {
        "products": [
            {"path": "out/report.md", "absolute": "/w/out/report.md", "bytes": 12}
        ],
        "products_missing": [],
    }


def test_a_promised_product_with_no_file_is_named_not_handed_over():
    """Handing over a path with nothing behind it is how a step reads an older
    round's file and believes it is this one's."""
    report = [
        {
            "path": "out/report.md",
            "absolute": "/w/out/report.md",
            "exists": False,
            "bytes": 0,
        }
    ]
    handoff = products_for_handoff(["out/report.md"], report)

    assert handoff["products"] == []
    assert handoff["products_missing"] == ["out/report.md"]


def test_a_promised_product_with_no_record_is_reported_missing():
    """A declaration with no measurement is not evidence that the file exists."""
    handoff = products_for_handoff(["a.md"], None)
    assert handoff["products"] == []
    assert handoff["products_missing"] == ["a.md"]


def test_the_address_is_carried_verbatim_rather_than_re_derived():
    """Resolved by the run that wrote it, so a step whose folder changed since
    still points at the file that was actually written."""
    report = [
        {
            "path": "out/report.md",
            "absolute": "/somewhere/else/entirely/out/report.md",
            "exists": True,
            "bytes": 3,
        }
    ]
    handoff = products_for_handoff(["out/report.md"], report)
    assert handoff["products"][0]["absolute"] == (
        "/somewhere/else/entirely/out/report.md"
    )


def test_the_report_payload_carries_both_halves_and_omits_what_is_empty():
    from agent.scheduler.store import _run_report_payload

    present = _run_report_payload(
        task_id="t1",
        task_name="nightly",
        run_id="r1",
        status="succeeded",
        declares=["a.md"],
        products=[
            {"path": "a.md", "absolute": "/w/a.md", "exists": True, "bytes": 4}
        ],
    )
    assert present["products"] == [
        {"path": "a.md", "absolute": "/w/a.md", "bytes": 4}
    ]
    assert "products_missing" not in present

    absent = _run_report_payload(
        task_id="t1",
        task_name="nightly",
        run_id="r2",
        status="failed",
        declares=["a.md"],
        products=[
            {"path": "a.md", "absolute": "/w/a.md", "exists": False, "bytes": 0}
        ],
    )
    assert "products" not in absent
    assert absent["products_missing"] == ["a.md"]

    # A run that declared nothing grows neither key.
    silent = _run_report_payload(
        task_id="t1", task_name="nightly", run_id="r3", status="succeeded"
    )
    assert "products" not in silent and "products_missing" not in silent


# ── 6. Both paths that build the report agree ───────────────────────────────


def test_the_emission_and_the_claim_hand_a_step_the_same_picture(tmp_path):
    """The whole point of the handoff is that a step is told the same thing
    however it started.  A step woken by a signal and one started by hand read
    the same block, so the block has to be built the same way both times.

    Driven end to end: ``collect`` declares and writes a file, ``analyze``
    depends on it, and the run ``analyze`` is given carries collect's product
    as an address.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="linear",
                steps=[
                    WorkflowStep(
                        key="collect",
                        name="collect",
                        kind="agent_prompt",
                        payload={"prompt": "collect"},
                        trigger=clock(),
                        workspace_root=str(tmp_path),
                        produces=["notes.md"],
                    ),
                    WorkflowStep(
                        key="analyze",
                        name="analyze",
                        kind="agent_prompt",
                        payload={"prompt": "analyze"},
                        depends_on=["collect"],
                        workspace_root=str(tmp_path),
                        produces=["summary.md"],
                    ),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        asyncio.run(
            run_to_terminal(
                make_service(store, on_run=write_product("notes.md", "summary.md"))
            )
        )

        analyze_run = store.list_runs(tasks["analyze"].id)[0]
        signals = analyze_run.config_snapshot["signals"]
        payload = next(
            entry["payload"] for entry in signals if entry["payload"]["step_key"] == "collect"
        )
        assert payload["products"] == [
            {
                "path": "notes.md",
                "absolute": str(Path(tmp_path).resolve() / "notes.md"),
                "bytes": PRODUCT_BYTES,
            }
        ]
        assert "products_missing" not in payload
    finally:
        store.close()


def test_a_step_that_promised_a_file_and_did_not_write_it_stops_the_chain(tmp_path):
    """The same rule that makes acceptance load-bearing, applied to products:
    a step whose product is absent must not pass its work down the chain."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="linear",
                steps=[
                    WorkflowStep(
                        key="collect",
                        name="collect",
                        kind="agent_prompt",
                        payload={"prompt": "collect"},
                        trigger=clock(),
                        workspace_root=str(tmp_path),
                        produces=["notes.md"],
                    ),
                    WorkflowStep(
                        key="analyze",
                        name="analyze",
                        kind="agent_prompt",
                        payload={"prompt": "analyze"},
                        depends_on=["collect"],
                        workspace_root=str(tmp_path),
                    ),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        # Writes nothing.
        asyncio.run(run_to_terminal(make_service(store)))

        collect_run = store.list_runs(tasks["collect"].id)[0]
        assert collect_run.status == "failed"
        assert collect_run.verdict == VERDICT_FAILED
        assert missing_products(collect_run.products) == ["notes.md"]
        assert [run.status for run in store.list_runs(tasks["analyze"].id)] == ["skipped"]
    finally:
        store.close()


def test_a_workflow_step_s_products_round_trip_through_the_store(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="chain",
                steps=[
                    WorkflowStep(
                        key="only",
                        name="only",
                        kind="agent_prompt",
                        payload={"prompt": "go"},
                        trigger=clock(),
                        workspace_root=str(tmp_path),
                        produces=["out/thing.md"],
                    )
                ],
            )
        )
        step = store.list_workflows()[0].steps[0]
        assert step.produces == ["out/thing.md"]
        assert workflow.steps[0].produces == ["out/thing.md"]
    finally:
        store.close()


# ── 7. What the run is told ─────────────────────────────────────────────────


def test_the_run_is_told_both_halves_of_its_contract(tmp_path):
    """The third defect, and the one that was invisible.

    The criterion was written by whoever commissioned the task, stored on it,
    and used to judge the run afterwards -- and never once shown to the run.
    A task whose success turned on one sentence spent its whole budget aiming
    at a target it had not been given.  The products did not exist anywhere at
    all.
    """
    from agent.cli import _describe_run_contract

    contract = _describe_run_contract(
        acceptance=Acceptance(criteria=["the report exists"], verify_command="true"),
        produces=["out/report.md"],
        workspace=tmp_path,
    )

    # The product, resolved absolute, resolved once so the run and the
    # downstream step cannot disagree about where it goes.
    assert str(tmp_path / "out" / "report.md") in contract
    assert "out/report.md" in contract
    # And the criterion the run will be judged by.
    assert "the report exists" in contract
    assert "true" in contract


def test_an_ordinary_run_does_not_grow_a_section_about_a_contract_it_lacks(
    tmp_path,
):
    from agent.cli import _describe_run_contract

    assert (
        _describe_run_contract(
            acceptance=Acceptance(), produces=[], workspace=tmp_path
        )
        == ""
    )


def test_the_prompt_names_a_promised_file_that_was_not_produced():
    """A path that was promised and is not there is the one thing the upstream
    block must not stay quiet about: the step below would go looking, find an
    older round's file, and believe it is this round's."""
    from agent.cli import _describe_upstream_results

    snapshot = {
        "signals": [
            {
                "name": "task:t1:succeeded",
                "payload": {
                    "task_id": "t1",
                    "task_name": "collect",
                    "step_key": "collect",
                    "status": "succeeded",
                    "products": [
                        {
                            "path": "notes.md",
                            "absolute": "/w/notes.md",
                            "bytes": 40,
                        }
                    ],
                    "products_missing": ["summary.md"],
                },
            }
        ]
    }

    text = _describe_upstream_results(snapshot)

    assert "produced 「notes.md」: /w/notes.md (40 bytes)" in text
    assert "declared but NOT produced: summary.md" in text
