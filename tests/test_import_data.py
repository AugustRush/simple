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
