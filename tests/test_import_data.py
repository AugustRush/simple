"""Putting an export back, without destroying what is already here.

"Restore" means replacing, which is the right shape for a disaster and the wrong
shape for what people actually do: "I deleted a conversation by mistake", "I moved
to a new machine". A restore for the second case deletes whatever the new machine
has accumulated, which is the same accident that made the export necessary.

So import **adds and never deletes**, and reports what it skipped.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest


def _home(base: Path, *, with_data: bool = True) -> Path:
    from agent.memory.store import LTMStore

    (base / "context").mkdir(parents=True, exist_ok=True)
    (base / "tasks").mkdir(exist_ok=True)
    (base / "autonomy").mkdir(exist_ok=True)
    if with_data:
        store = LTMStore(base / "context")
        store.write_conversation_exchange(
            session_id="s1",
            user_content="问题一",
            assistant_content="回答一",
            channel="web",
            message_id="m1",
        )
        store.append_agent_event(
            session_id="s1",
            event_type="tool_started",
            payload={"tool_name": "list_files"},
            turn_id="m1",
        )
        store.set_session_title("s1", "我的对话")
        store.close()
        (base / "web" / "sessions" / "s1" / "output").mkdir(parents=True)
        (base / "web" / "sessions" / "s1" / "output" / "a.txt").write_text("hello")
    return base


def _export(home: Path, destination: Path):
    from agent.export import build_archive

    return build_archive(
        context_dir=home / "context",
        tasks_dir=home / "tasks",
        autonomy_dir=home / "autonomy",
        web_sessions_dir=home / "web" / "sessions",
        destination=destination,
    )


def _import(home: Path, archive: Path):
    from agent.import_data import import_archive

    return import_archive(
        archive,
        context_dir=home / "context",
        tasks_dir=home / "tasks",
        autonomy_dir=home / "autonomy",
        web_sessions_dir=home / "web" / "sessions",
    )


def _turns(home: Path, session: str = "s1") -> list[str]:
    from agent.memory.store import LTMStore

    store = LTMStore(home / "context")
    try:
        return [
            row.content
            for row in store.recent_conversation_turns(session_id=session, limit=50)
        ]
    finally:
        store.close()


def test_a_deleted_conversation_comes_back(tmp_path) -> None:
    """The case that made this necessary: a mistake on the memory page."""
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")

    target = _home(tmp_path / "dst")
    conn = sqlite3.connect(target / "context" / "palace.db")
    conn.execute("DELETE FROM conversation_turns")
    conn.execute("DELETE FROM agent_events")
    conn.commit()
    conn.close()
    shutil.rmtree(target / "web" / "sessions" / "s1" / "output")

    report = _import(target, archive)

    assert report["sessions_added"] == 1
    assert _turns(target) == ["问题一", "回答一"]
    assert (target / "web" / "sessions" / "s1" / "output" / "a.txt").read_text() == "hello"


def test_importing_never_touches_what_is_already_there(tmp_path) -> None:
    """A conversation whose id is present is skipped, not merged into.

    Merging two timelines at the same positions would break the transcript's
    only ordering, which the paging cursor depends on.
    """
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")

    target = _home(tmp_path / "dst")
    # The target already has the same session with an extra turn of its own.
    from agent.memory.store import LTMStore

    store = LTMStore(target / "context")
    store.write_conversation_exchange(
        session_id="s1",
        user_content="这台机器上的新问题",
        assistant_content="新回答",
        channel="web",
        message_id="m2",
    )
    store.close()
    before = _turns(target)

    report = _import(target, archive)

    assert report["sessions_skipped"] == 1
    assert report["sessions_added"] == 0
    assert _turns(target) == before, "an existing conversation must be left alone"


def test_the_sequence_counter_is_lifted_past_what_was_imported(tmp_path) -> None:
    """Otherwise the next turn written shares a position with an imported one.

    The transcript's ordering is the only thing the paging cursor and the display
    agree on; two rows at one position is a broken conversation, not a shorter
    one.
    """
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")

    target = _home(tmp_path / "dst", with_data=False)
    _import(target, archive)

    from agent.memory.store import LTMStore

    store = LTMStore(target / "context")
    try:
        store.write_conversation_exchange(
            session_id="s1",
            user_content="导入之后的新一轮",
            assistant_content="回答",
            channel="web",
            message_id="m3",
        )
        rows = store.recent_conversation_turns(session_id="s1", limit=50)
    finally:
        store.close()

    seqs = [int(row.seq) for row in rows]
    assert len(seqs) == len(set(seqs)), f"duplicate positions: {seqs}"


def test_a_newer_archive_is_refused_rather_than_half_read(tmp_path) -> None:
    """The format is versioned for exactly this moment."""
    from agent.import_data import ArchiveError

    archive = tmp_path / "future.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "manifest.json",
            json.dumps({"archive_version": 99, "exported_at": "2030-01-01"}),
        )
        handle.writestr("sessions.json", "[]")

    with pytest.raises(ArchiveError, match="v99"):
        _import(_home(tmp_path / "dst", with_data=False), archive)


def test_something_that_is_not_an_export_is_refused(tmp_path) -> None:
    from agent.import_data import ArchiveError

    not_an_export = tmp_path / "random.zip"
    with zipfile.ZipFile(not_an_export, "w") as handle:
        handle.writestr("hello.txt", "hi")

    with pytest.raises(ArchiveError, match="manifest"):
        _import(_home(tmp_path / "dst", with_data=False), not_an_export)

    not_a_zip = tmp_path / "notes.txt"
    not_a_zip.write_text("hello")
    with pytest.raises(ArchiveError, match="ZIP"):
        _import(_home(tmp_path / "dst2", with_data=False), not_a_zip)


def test_a_malformed_section_leaves_nothing_behind(tmp_path) -> None:
    """Validated before anything is written: half an import is worse than none."""
    from agent.import_data import ArchiveError

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("manifest.json", json.dumps({"archive_version": 1}))
        handle.writestr("sessions.json", '{"not": "a list"}')
        handle.writestr("memory.json", "[]")

    target = _home(tmp_path / "dst", with_data=False)
    with pytest.raises(ArchiveError, match="列表"):
        _import(target, archive)

    assert _turns(target) == []


def test_importing_the_same_archive_twice_is_idempotent(tmp_path) -> None:
    """A double-click must not double the conversation."""
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")

    target = _home(tmp_path / "dst", with_data=False)
    first = _import(target, archive)
    second = _import(target, archive)

    assert first["sessions_added"] == 1
    assert second["sessions_added"] == 0
    assert second["sessions_skipped"] == 1
    assert _turns(target) == ["问题一", "回答一"]


# ── regressions found by review ─────────────────────────────────────────────


def test_an_archive_with_memory_imports_instead_of_crashing(tmp_path) -> None:
    """Measured: `TypeError: tuple indices must be integers or slices, not str`.

    `fts._sync_fts_row` reads `row["status"]` to decide how to index an entry,
    and the import opened a bare connection -- which returns tuples. Every
    archive containing one memory entry died, taking the session import in the
    same transaction with it.
    """
    import sqlite3

    from agent.memory.models import LTMEntry
    from agent.memory.store import LTMStore

    source = _home(tmp_path / "src", with_data=False)
    store = LTMStore(source / "context")
    store.add_entry(
        LTMEntry(
            id="m1",
            content="记住的事",
            importance=0.5,
            category="concepts",
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )
    )
    store.close()
    archive, manifest = _export(source, tmp_path / "a.zip")
    assert manifest["counts"]["memory_entries"] == 1

    target = _home(tmp_path / "dst", with_data=False)
    report = _import(target, archive)

    assert report["memory_added"] == 1
    conn = sqlite3.connect(target / "context" / "palace.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        # And the index retrieval reads: a row without one is a memory the agent
        # will never find.
        assert conn.execute("SELECT COUNT(*) FROM memory_items_fts").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_archive_cannot_write_outside_the_sessions_directory(tmp_path) -> None:
    """`relative_to` is lexical: `/a/b/../../../x` passes it against `/a/b`.

    Measured: an entry named `output/s1/output/../../../pwned.txt` was written
    outside `web/sessions`. A malicious archive could drop files anywhere the
    gateway can write.
    """
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("manifest.json", json.dumps({"archive_version": 1}))
        handle.writestr("sessions.json", "[]")
        handle.writestr("memory.json", "[]")
        handle.writestr("output/s1/output/../../../pwned.txt", "ESCAPED")

    target = _home(tmp_path / "dst", with_data=False)
    report = _import(target, archive)

    assert report["output_copied"] == 0
    assert report["output_skipped"] >= 1
    assert not list((tmp_path / "dst").rglob("pwned.txt")), "wrote outside the root"


def _task_home(base: Path):
    """A home with one scheduled task, made through the scheduler's own API.

    Not hand-written INSERTs: `scheduled_tasks` has a dozen NOT NULL columns and
    a hand-written row breaks on the next one somebody adds.
    """
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    home = _home(base, with_data=False)
    scheduler = SchedulerStore(db_path=home / "tasks" / "scheduler.db")
    scheduler.create_task(
        NewScheduledTask(
            name="周报",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-12-01T09:00:00+00:00", "UTC"),
            payload={"prompt": "写周报"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            workspace_root=str(home),
        )
    )
    scheduler.close()
    return home


def test_a_task_exported_while_running_is_not_left_unrunnable(tmp_path) -> None:
    """A task carries `active_run_id`; if that run is not in the archive the
    imported task points at nothing. `claim_due_tasks` requires the column to be
    NULL and stale-run recovery needs a run row to join, so it never runs again.
    """
    import sqlite3

    source = _task_home(tmp_path / "src")
    conn = sqlite3.connect(source / "tasks" / "scheduler.db")
    with conn:
        task_id = conn.execute("SELECT id FROM scheduled_tasks").fetchone()[0]
        conn.execute(
            "UPDATE scheduled_tasks SET active_run_id='run-gone', "
            "lease_until='2030-01-01 00:00:00' WHERE id=?",
            (task_id,),
        )
    conn.close()

    archive, _manifest = _export(source, tmp_path / "a.zip")
    target = _home(tmp_path / "dst", with_data=False)
    _import(target, archive)

    conn = sqlite3.connect(target / "tasks" / "scheduler.db")
    try:
        row = conn.execute(
            "SELECT active_run_id, lease_until FROM scheduled_tasks"
        ).fetchone()
    finally:
        conn.close()
    assert row == (None, None), "a dangling claim must be cleared or the task never runs"


def test_task_run_history_survives_the_round_trip(tmp_path) -> None:
    """The export listed runs and the manifest counted them; the import dropped
    them, so a restored task showed no history at all."""
    import sqlite3

    source = _task_home(tmp_path / "src")
    conn = sqlite3.connect(source / "tasks" / "scheduler.db")
    with conn:
        task_id = conn.execute("SELECT id FROM scheduled_tasks").fetchone()[0]
        conn.execute(
            "INSERT INTO scheduled_task_runs (id, task_id, scheduled_for, started_at, "
            " status, created_at, updated_at) "
            "VALUES ('r1', ?, '2026-01-01', '2026-01-01', 'succeeded', "
            "        '2026-01-01', '2026-01-01')",
            (task_id,),
        )
    conn.close()

    archive, manifest = _export(source, tmp_path / "a.zip")
    assert manifest["counts"]["task_runs"] == 1

    target = _home(tmp_path / "dst", with_data=False)
    report = _import(target, archive)

    assert report["task_runs_added"] == 1
    conn = sqlite3.connect(target / "tasks" / "scheduler.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_task_runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_tool_trace_of_a_replaced_turn_survives(tmp_path) -> None:
    """`restore_branch` reads `agent_events_archive` to bring a version back with
    the calls that produced it. Exporting only the turns left it mute: the turn
    came back, the tool calls that made it did not.
    """
    import sqlite3

    source = _home(tmp_path / "src")
    conn = sqlite3.connect(source / "context" / "palace.db")
    with conn:
        conn.execute(
            "INSERT INTO conversation_turns_archive (session_id, role, content, "
            " created_at, seq, turn_index, branch_id, archived_at) "
            "VALUES ('s1','user','被换下的一问','2026-01-01 00:00:00', 9, 1, "
            "        'branch-1', '2026-01-01 00:00:01')"
        )
        conn.execute(
            "INSERT INTO agent_events_archive (session_id, event_type, payload_json, "
            " turn_id, created_at, seq, branch_id, archived_at) "
            "VALUES ('s1','tool_started','{\"tool_name\": \"list_files\"}', 'm1', "
            "        '2026-01-01 00:00:00', 8, 'branch-1', '2026-01-01 00:00:01')"
        )
    conn.close()

    archive, manifest = _export(source, tmp_path / "a.zip")
    assert manifest["counts"]["replaced_versions"] == 1
    assert manifest["counts"]["replaced_events"] == 1

    target = _home(tmp_path / "dst", with_data=False)
    _import(target, archive)

    conn = sqlite3.connect(target / "context" / "palace.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_turns_archive"
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT event_type, payload_json, branch_id FROM agent_events_archive"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the replaced turn's tool trace was dropped"
    assert row[0] == "tool_started"
    assert "list_files" in row[1]
    assert row[2] == "branch-1"


def test_the_sequence_counter_covers_the_versions_kept_aside(tmp_path) -> None:
    """Measured: active turns at seq 1,2 and kept-aside versions at 3,4 imported
    with next_seq=3, after which restoring a version produced two rows at seq 3
    and two at seq 4 -- the transcript stopped being one order."""
    import sqlite3

    from agent.memory.store import LTMStore

    source = _home(tmp_path / "src", with_data=False)
    store = LTMStore(source / "context")
    for index in range(1, 5):
        store.write_conversation_exchange(
            session_id="s1",
            user_content=f"问题{index}",
            assistant_content=f"回答{index}",
            channel="web",
            message_id=f"m{index}",
        )
    store.close()

    # Keep the last two turns aside, leaving two active.
    store = LTMStore(source / "context")
    rows = store.recent_conversation_turns(session_id="s1", limit=10)
    users = [row for row in rows if row.role == "user"]
    store.archive_tail_from(session_id="s1", from_seq=users[2].seq)
    store.close()

    archive, _manifest = _export(source, tmp_path / "a.zip")
    target = _home(tmp_path / "dst", with_data=False)
    _import(target, archive)

    conn = sqlite3.connect(target / "context" / "palace.db")
    try:
        highest = max(
            conn.execute("SELECT COALESCE(MAX(seq),0) FROM conversation_turns_archive").fetchone()[0],
            conn.execute("SELECT COALESCE(MAX(seq),0) FROM conversation_turns").fetchone()[0],
        )
        next_seq = conn.execute(
            "SELECT next_seq FROM transcript_seq_counters WHERE session_id='s1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert next_seq > highest, f"next_seq={next_seq} would reuse a position <= {highest}"


def test_a_failing_section_is_reported_and_the_rest_still_lands(tmp_path) -> None:
    """Partial is a real outcome, not a server fault.

    Measured before this: a valid archive with one malformed task wrote the
    conversations, then raised -- the caller got a 500 and had no way to know
    half of it had landed. Now the sections are isolated, the report says which
    one failed, and `complete` is false.
    """
    import sqlite3

    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")

    # Rebuild the archive with a task row that violates a NOT NULL constraint.
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(broken, "w") as out:
        for name in original.namelist():
            if name == "tasks.json":
                out.writestr(
                    name,
                    json.dumps({"tasks": [{"id": "t1", "name": "x", "kind": None}]}),
                )
            else:
                out.writestr(name, original.read(name))

    target = _home(tmp_path / "dst", with_data=False)
    report = _import(target, broken)

    assert report["complete"] is False
    assert report["sections"]["tasks"]["ok"] is False
    assert report["sections"]["tasks"]["error"]
    # The conversations are the part somebody actually misses; they still landed.
    assert report["sections"]["conversations"]["ok"] is True
    assert _turns(target) == ["问题一", "回答一"]


def test_a_complete_import_says_so(tmp_path) -> None:
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")

    report = _import(_home(tmp_path / "dst", with_data=False), archive)

    assert report["complete"] is True
    assert all(section["ok"] for section in report["sections"].values())


def test_re_running_a_partial_import_converges(tmp_path) -> None:
    """The documented recovery path: everything is deduped by id, so importing
    the same archive again finishes what the first attempt missed instead of
    doubling it."""
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")
    target = _home(tmp_path / "dst", with_data=False)

    first = _import(target, archive)
    second = _import(target, archive)

    assert first["sessions_added"] == 1
    assert second["sessions_added"] == 0
    assert second["sessions_skipped"] == 1
    assert _turns(target) == ["问题一", "回答一"]


def test_a_half_written_output_file_is_not_treated_as_finished(tmp_path) -> None:
    """The one failure a retry could not repair: a truncated file left at the
    destination, which `exists()` then skips forever. Files are written through
    a `.part` and renamed, so an interrupted copy leaves a `.part` -- never a
    destination file that looks finished.
    """
    source = _home(tmp_path / "src")
    archive, _manifest = _export(source, tmp_path / "a.zip")
    target = _home(tmp_path / "dst", with_data=False)

    # Simulate a crash mid-copy of `a.txt`: only the `.part` exists.
    output = target / "web" / "sessions" / "s1" / "output"
    output.mkdir(parents=True)
    (output / "a.txt.part").write_text("hel")

    report = _import(target, archive)

    assert report["output_copied"] == 1
    assert (output / "a.txt").read_text() == "hello"
    assert not (output / "a.txt.part").exists(), "the interrupted copy was left behind"
