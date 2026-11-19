"""Everything the person said and everything the agent kept, as one archive.

Why this exists
---------------
There was no way to get the data out. The memory page could delete a session and
the settings page could clear all memory, and neither had a counterpart: a
mistake there was unrecoverable, and moving to another machine meant starting
over. "Your data is yours" is not a feature you can claim while the only verbs
are *read it here* and *delete it*.

What goes in
------------
* **conversations** -- every session's turns, the tool events that produced them,
  and the replaced versions a rewind set aside;
* **memory** -- the durable entries the agent carries between conversations;
* **scheduled work** -- task definitions and their run history;
* **autonomy** -- the proposals the patrol raised and the goals the agent set;
* **output** -- the files those runs and conversations produced.

What deliberately does not
--------------------------
* ``config.json``. It holds API keys and the web auth token. A backup that
  quietly copies credentials into a file somebody emails to themselves is worse
  than no backup, and there is no "just the harmless half" of a config file that
  is worth the risk.
* machine telemetry -- usage counters, patrol ticks, sequence counters, FTS
  shadow tables. They describe this install's operation, not the person's work,
  and they are all derivable or meaningless elsewhere.

The manifest says both lists out loud, because an archive that silently omits
something is a backup that fails at the moment somebody relies on it.

Why a ZIP and not one JSON
--------------------------
The conversations reference images and files the agent produced. A JSON export
would name them and not contain them, which is the partial backup that looks
complete. ``zipfile`` is in the standard library, so this costs no dependency.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["build_archive", "ARCHIVE_VERSION", "EXCLUDED"]

#: Bumped when the shape changes in a way an importer would have to notice.
ARCHIVE_VERSION = 1

#: Named in the manifest so the reader knows what a restore would not bring back.
EXCLUDED = (
    "config.json（含 API 密钥与访问令牌）",
    "用量统计、巡逻记录、序号计数器、全文索引等机器运行数据",
    "本机通知与日志",
)

#: How many output files to carry. The point is "the files the agent made for
#: me", not "every temporary file the sandbox ever wrote".
_MAX_OUTPUT_FILES = 2000
_MAX_OUTPUT_BYTES = 512 * 1024 * 1024


def _iso(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    columns = [item[0] for item in cursor.description or ()]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _read_sessions(conn: Any) -> list[dict[str, Any]]:
    """Every conversation, with its turns, its tool trace and its set-aside versions."""
    titles = {
        str(row["session_id"]): str(row["title"] or "")
        for row in conn.execute("SELECT session_id, title FROM session_meta")
    }
    sessions: dict[str, dict[str, Any]] = {}

    def bucket(session_id: str) -> dict[str, Any]:
        key = str(session_id or "")
        if key not in sessions:
            sessions[key] = {
                "session_id": key,
                "title": titles.get(key, ""),
                "turns": [],
                "events": [],
                "replaced_versions": [],
            }
        return sessions[key]

    for row in _rows(
        conn,
        "SELECT session_id, role, content, channel, message_id, reply_to_id, "
        "metadata_json, created_at, seq FROM conversation_turns ORDER BY session_id, seq, id",
    ):
        entry = bucket(row.pop("session_id"))
        try:
            row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        except ValueError:
            row["metadata"] = {}
        entry["turns"].append(row)

    for row in _rows(
        conn,
        "SELECT session_id, event_type, payload_json, turn_id, created_at, seq "
        "FROM agent_events ORDER BY session_id, seq, id",
    ):
        entry = bucket(row.pop("session_id"))
        try:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        except ValueError:
            row["payload"] = {}
        entry["events"].append(row)

    # The versions a rewind set aside. They are part of the record: a turn the
    # person edited is a thing they said, and an export that dropped it would
    # lose exactly the history this feature exists to keep.
    for row in _rows(
        conn,
        "SELECT session_id, role, content, channel, message_id, reply_to_id, "
        "metadata_json, created_at, seq, turn_index, branch_id, archived_at, archived_by "
        "FROM conversation_turns_archive ORDER BY session_id, seq, id",
    ):
        entry = bucket(row.pop("session_id"))
        try:
            row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        except ValueError:
            row["metadata"] = {}
        entry["replaced_versions"].append(row)

    return [sessions[key] for key in sorted(sessions)]


def _read_memory(conn: Any) -> list[dict[str, Any]]:
    try:
        rows = _rows(
            conn,
            "SELECT id, content, importance, category, entity, memory_type, scope, "
            "status, source_session, confidence, pinned, created_at, updated_at "
            "FROM memory_items ORDER BY category, created_at",
        )
    except Exception:  # noqa: BLE001 - a store without memory is an empty list
        return []
    return rows


def _read_tasks(conn: Any) -> dict[str, Any]:
    try:
        tasks = _rows(conn, "SELECT * FROM scheduled_tasks")
        runs = _rows(conn, "SELECT * FROM scheduled_task_runs")
    except Exception:  # noqa: BLE001
        return {"tasks": [], "runs": []}
    return {"tasks": tasks, "runs": runs}


def _read_autonomy(conn: Any) -> dict[str, Any]:
    proposals: list[dict[str, Any]] = []
    goals: list[dict[str, Any]] = []
    try:
        proposals = _rows(conn, "SELECT * FROM proposals")
    except Exception:  # noqa: BLE001
        pass
    try:
        goals = _rows(conn, "SELECT * FROM goals")
    except Exception:  # noqa: BLE001
        pass
    return {"proposals": proposals, "goals": goals}


def _output_files(session_root: Path) -> list[Path]:
    """The files the agent produced, bounded.

    A cap rather than "everything": an export that walks a 50GB output directory
    is one nobody waits for. The manifest records how many were left out, so a
    truncated archive says so.
    """
    found: list[Path] = []
    total = 0
    if not session_root.is_dir():
        return []
    for path in sorted(session_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if len(found) >= _MAX_OUTPUT_FILES or total + size > _MAX_OUTPUT_BYTES:
            break
        total += size
        found.append(path)
    return found


def build_archive(
    *,
    context_dir: Path,
    tasks_dir: Path,
    autonomy_dir: Path,
    web_sessions_dir: Optional[Path] = None,
    destination: Optional[Path] = None,
) -> tuple[Path, dict[str, Any]]:
    """Write the archive; return its path and the manifest.

    Written to a temp file and renamed at the end: a half-written archive that
    looks like a finished one is the failure mode a backup cannot have.
    """
    import sqlite3

    moment = datetime.now(timezone.utc)
    name = f"simple-agent-export-{moment.strftime('%Y%m%d-%H%M%S')}.zip"
    target = destination or Path(tempfile.gettempdir()) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".part")

    sessions: list[dict[str, Any]] = []
    memory: list[dict[str, Any]] = []
    tasks: dict[str, Any] = {"tasks": [], "runs": []}
    autonomy: dict[str, Any] = {"proposals": [], "goals": []}

    palace = Path(context_dir) / "palace.db"
    if palace.is_file():
        conn = sqlite3.connect(f"file:{palace}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            sessions = _read_sessions(conn)
            memory = _read_memory(conn)
        finally:
            conn.close()

    scheduler = Path(tasks_dir) / "scheduler.db"
    if scheduler.is_file():
        conn = sqlite3.connect(f"file:{scheduler}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tasks = _read_tasks(conn)
        finally:
            conn.close()

    proposals = Path(autonomy_dir) / "proposals.db"
    if proposals.is_file():
        conn = sqlite3.connect(f"file:{proposals}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            autonomy = _read_autonomy(conn)
        finally:
            conn.close()

    included_output: list[str] = []
    omitted_output = 0
    with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sessions.json", json.dumps(sessions, ensure_ascii=False, indent=1))
        archive.writestr("memory.json", json.dumps(memory, ensure_ascii=False, indent=1))
        archive.writestr("tasks.json", json.dumps(tasks, ensure_ascii=False, indent=1, default=str))
        archive.writestr("autonomy.json", json.dumps(autonomy, ensure_ascii=False, indent=1))

        if web_sessions_dir is not None and Path(web_sessions_dir).is_dir():
            root = Path(web_sessions_dir)
            for session_dir in sorted(root.iterdir()):
                if not session_dir.is_dir():
                    continue
                output = session_dir / "output"
                files = _output_files(output)
                if not files:
                    continue
                for path in files:
                    try:
                        relative = path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    try:
                        archive.write(path, arcname=f"output/{relative}")
                        included_output.append(f"output/{relative}")
                    except OSError:
                        omitted_output += 1
                # Counted per directory rather than globally: a session with more
                # than the cap is the one whose remainder is missing.
                try:
                    omitted_output += max(0, sum(1 for p in output.rglob("*") if p.is_file()) - len(files))
                except OSError:
                    pass

        manifest = {
            "archive_version": ARCHIVE_VERSION,
            "exported_at": _iso(moment),
            "counts": {
                "sessions": len(sessions),
                "turns": sum(len(item["turns"]) for item in sessions),
                "replaced_versions": sum(len(item["replaced_versions"]) for item in sessions),
                "memory_entries": len(memory),
                "tasks": len(tasks.get("tasks") or []),
                "task_runs": len(tasks.get("runs") or []),
                "proposals": len(autonomy.get("proposals") or []),
                "goals": len(autonomy.get("goals") or []),
                "output_files": len(included_output),
            },
            "excluded": list(EXCLUDED),
            "output_files_omitted": omitted_output,
            "files": [
                "sessions.json",
                "memory.json",
                "tasks.json",
                "autonomy.json",
                *included_output,
            ],
        }
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))

    os.replace(staging, target)
    return target, manifest
