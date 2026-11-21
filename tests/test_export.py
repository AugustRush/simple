"""Getting the data out.

Before this, the only verbs were *read it here* and *delete it*. A mistake on the
memory page was unrecoverable and moving to another machine meant starting over,
while "your data is yours" is not a claim you can make with no export.

Two properties matter more than the file list:

* **no secrets**, ever -- a backup that copies API keys into a file somebody
  emails to themselves is worse than no backup;
* **the archive is complete enough to be worth having** -- a JSON export that
  names the images the agent made and does not contain them is the partial backup
  that looks complete.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest


@pytest.fixture()
def home(tmp_path: Path):
    """A miniature agent home: one conversation, one memory entry, one task."""
    from agent.memory.store import LTMStore
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    (tmp_path / "context").mkdir()
    store = LTMStore(tmp_path / "context")
    store.write_conversation_exchange(
        session_id="s1",
        user_content="帮我看一下下载目录",
        assistant_content="好的，一共 12 个文件",
        channel="web",
        message_id="m1",
    )
    store.append_agent_event(
        session_id="s1",
        event_type="tool_started",
        payload={"tool_name": "list_files"},
        turn_id="m1",
    )
    store.set_session_title("s1", "整理下载目录")
    store.close()

    (tmp_path / "tasks").mkdir()
    scheduler = SchedulerStore(db_path=tmp_path / "tasks" / "scheduler.db")
    scheduler.create_task(
        NewScheduledTask(
            name="每周五写周报",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-12-01T09:00:00+00:00", "UTC"),
            payload={"prompt": "写周报"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            workspace_root=str(tmp_path),
        )
    )
    scheduler.close()

    (tmp_path / "autonomy").mkdir()
    (tmp_path / "web" / "sessions" / "s1" / "output").mkdir(parents=True)
    (tmp_path / "web" / "sessions" / "s1" / "output" / "list.txt").write_text(
        "a.txt\nb.txt\n", encoding="utf-8"
    )
    return tmp_path


def _build(home: Path, destination: Path):
    from agent.export import build_archive

    return build_archive(
        context_dir=home / "context",
        tasks_dir=home / "tasks",
        autonomy_dir=home / "autonomy",
        web_sessions_dir=home / "web" / "sessions",
        scheduler_output_dir=home / "output" / "scheduler",
        destination=destination,
    )


def test_the_conversation_and_its_tool_trace_are_in_the_archive(home) -> None:
    path, manifest = _build(home, home / "out.zip")

    with zipfile.ZipFile(path) as archive:
        sessions = json.loads(archive.read("sessions.json"))

    assert manifest["counts"]["sessions"] == 1
    (session,) = sessions
    assert session["title"] == "整理下载目录"
    assert [turn["content"] for turn in session["turns"]] == [
        "帮我看一下下载目录",
        "好的，一共 12 个文件",
    ]
    assert session["events"][0]["event_type"] == "tool_started"
    assert session["events"][0]["payload"]["tool_name"] == "list_files"


def test_the_files_the_agent_made_are_in_the_archive_not_only_named(home) -> None:
    """A JSON that names the images and does not contain them looks complete."""
    path, manifest = _build(home, home / "out.zip")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()

    assert "output/s1/output/list.txt" in names
    assert manifest["counts"]["output_files"] == 1


def test_no_secret_from_the_config_can_appear(home) -> None:
    """Asserted against the real config file, not against a guess.

    The archive is built by *not reading* config.json. This test proves the
    consequence: whatever secrets that file holds, none of their values are in
    the archive.
    """
    import os

    config = Path(os.path.expanduser("~/.agent/config.json"))
    secrets: list[str] = []
    if config.is_file():
        raw = json.loads(config.read_text())
        for name, block in (raw.get("providers") or {}).items():
            key = str((block or {}).get("api_key") or "")
            # The masked form is what the API returns; the literal one is what a
            # real export could leak. Skip masks and empty values.
            if key and "****" not in key and len(key) > 8 and not key.startswith("$"):
                secrets.append(key)
        token = str(raw.get("auth_token") or "")
        if token and len(token) > 8:
            secrets.append(token)

    path, _manifest = _build(home, home / "out.zip")
    with zipfile.ZipFile(path) as archive:
        blob = b"".join(
            archive.read(name) for name in archive.namelist() if name.endswith(".json")
        )

    for secret in secrets:
        assert secret.encode() not in blob, "a credential reached the export"


def test_the_manifest_says_what_is_missing(home) -> None:
    """An archive that silently omits something fails when it is relied on."""
    path, _manifest = _build(home, home / "out.zip")

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    assert manifest["archive_version"] >= 1
    assert manifest["exported_at"]
    joined = " ".join(manifest["excluded"])
    assert "config.json" in joined
    assert "密钥" in joined


def test_a_task_and_its_definition_survive_the_round_trip(home) -> None:
    path, manifest = _build(home, home / "out.zip")

    with zipfile.ZipFile(path) as archive:
        tasks = json.loads(archive.read("tasks.json"))

    assert manifest["counts"]["tasks"] == 1
    assert tasks["tasks"][0]["name"] == "每周五写周报"


def test_a_missing_database_is_an_empty_section_not_a_crash(tmp_path) -> None:
    """A fresh install has no tasks and no proposals; exporting it must work."""
    (tmp_path / "context").mkdir()
    path, manifest = _build(tmp_path, tmp_path / "out.zip")

    assert manifest["counts"]["sessions"] == 0
    assert manifest["counts"]["tasks"] == 0
    with zipfile.ZipFile(path) as archive:
        assert "manifest.json" in archive.namelist()


# ── regressions found by review ─────────────────────────────────────────────


def test_scheduled_run_reports_are_in_the_archive(home) -> None:
    """The docstring promised "the files those runs and conversations produced"
    and only conversations were packed. A run's `.md` report is the result; its
    `artifacts/` working tree is scratch and is excluded on purpose."""
    reports = home / "output" / "scheduler" / "task-1"
    (reports / "run-1").mkdir(parents=True)
    (reports / "run-1.md").write_text("# 周报\n做完了\n", encoding="utf-8")
    (reports / "run-1" / "artifacts").mkdir()
    (reports / "run-1" / "artifacts" / "big.bin").write_bytes(b"x" * 1024)

    path, manifest = _build(home, home / "out.zip")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        body = archive.read("scheduler-output/task-1/run-1.md").decode()

    assert "scheduler-output/task-1/run-1.md" in names
    assert "做完了" in body
    assert manifest["counts"]["scheduler_reports"] == 1
    assert not any("artifacts" in name for name in names), (
        "a run's working tree is not a backup target"
    )
    assert any("artifacts" in item for item in manifest["excluded"])


def test_workflows_survive_and_dangling_task_references_are_cleared(home) -> None:
    import json as _json
    import sqlite3

    source = home
    db = source / "tasks" / "scheduler.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO workflows (id, name, enabled, created_at, updated_at) "
            "VALUES ('w1','流水线',1,'2026-01-01','2026-01-01')"
        )
        task_id = conn.execute("SELECT id FROM scheduled_tasks").fetchone()[0]
        conn.execute(
            "UPDATE scheduled_tasks SET workflow_id='w1' WHERE id=?", (task_id,)
        )
    conn.close()

    path, manifest = _build(source, source / "out.zip")
    assert manifest["counts"]["workflows"] == 1

    from agent.import_data import import_archive

    target = source.parent / "dst"
    for name in ("context", "tasks", "autonomy"):
        (target / name).mkdir(parents=True, exist_ok=True)
    report = import_archive(
        path,
        context_dir=target / "context",
        tasks_dir=target / "tasks",
        autonomy_dir=target / "autonomy",
    )

    assert report["workflows_added"] == 1
    conn = sqlite3.connect(target / "tasks" / "scheduler.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 1
        assert conn.execute("SELECT workflow_id FROM scheduled_tasks").fetchone()[0] == "w1"
    finally:
        conn.close()


def test_an_install_whose_memory_is_somewhere_else_is_refused_not_half_exported(
    tmp_path,
) -> None:
    """`sqlite-fts-memory` keeps entries in `palace-fts.db`; the queries here are
    written against the built-in schema. Measured on that profile:
    `no such table: session_meta`. A backup that silently omits the memory is the
    failure it exists to prevent."""
    from agent.export import ExportUnsupported

    home = tmp_path / "fts"
    (home / "context").mkdir(parents=True)
    (home / "tasks").mkdir()
    (home / "autonomy").mkdir()
    import sqlite3

    conn = sqlite3.connect(home / "context" / "palace.db")
    conn.execute("CREATE TABLE staging_turns (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(ExportUnsupported, match="sqlite-fts-memory"):
        _build(home, home / "out.zip")
