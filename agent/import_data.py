"""Putting an export back, without ever destroying what is already here.

Why this is a merge and not a restore
-------------------------------------
"Restore" means replacing: the archive becomes the truth and everything else is
discarded. That is the right shape for a disaster, and the wrong shape for the
thing people actually do -- "I deleted a conversation by mistake", "I moved to a
new machine and want my history". A restore for the second case deletes whatever
the new machine has accumulated since, which is the same class of accident that
made the export necessary.

So: **import adds, and never deletes.** A session whose id is already here is
skipped and reported as skipped. Memory entries, tasks, proposals and goals are
added only when their id is absent. Output files are copied only when the target
does not exist. The report says, per section, what was added and what was left
alone -- an import that silently skipped half an archive is a restore that fails
later.

What it refuses
---------------
* An archive whose ``archive_version`` is newer than this code understands. The
  format is versioned for exactly this moment; guessing at unknown fields is how
  a restore writes half a conversation.
* Anything that is not a ZIP with a ``manifest.json``.
* A ``sessions.json`` that is not a list. Validated before anything is written,
  so a malformed archive leaves nothing behind rather than half of it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["import_archive", "ArchiveError", "SUPPORTED_VERSION"]


class ArchiveError(ValueError):
    """The archive cannot be imported, and why."""


#: The highest ``archive_version`` this code can read. Bumped when the shape
#: changes; a newer archive is refused rather than half-read.
SUPPORTED_VERSION = 1

#: Sections in the archive, and the table each one fills.
_SESSION_TURN_COLUMNS = (
    "session_id",
    "role",
    "content",
    "channel",
    "message_id",
    "reply_to_id",
    "metadata_json",
    "created_at",
    "seq",
)


def _read_json(archive: zipfile.ZipFile, name: str, default: Any) -> Any:
    try:
        raw = archive.read(name)
    except KeyError:
        return default
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ArchiveError(f"{name} 不是合法的 JSON：{exc}") from None


def _bump_seq_counter(conn: sqlite3.Connection, session_id: str, highest: int) -> None:
    """Make sure the session's next position is past everything imported.

    Without this the imported turns and the next turn written by the agent share
    a position, and the transcript's ordering -- which is the only ordering the
    paging cursor and the display agree on -- stops being a total order.
    """
    if highest <= 0:
        return
    conn.execute(
        """
        INSERT INTO transcript_seq_counters (session_id, next_seq)
        VALUES (?, ?)
        ON CONFLICT(session_id) DO UPDATE SET next_seq = MAX(next_seq, ?)
        """,
        (session_id, highest + 1, highest + 1),
    )


def _import_sessions(conn: sqlite3.Connection, sessions: list[dict[str, Any]]) -> dict[str, int]:
    added = skipped = turns_added = events_added = 0
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            continue
        existing = conn.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if existing:
            # Present means present: merging into a live conversation would put
            # two different timelines at the same positions.
            skipped += 1
            continue
        highest = 0
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            row = {
                "session_id": session_id,
                "role": str(turn.get("role") or "user"),
                "content": str(turn.get("content") or ""),
                "channel": str(turn.get("channel") or ""),
                "message_id": str(turn.get("message_id") or ""),
                "reply_to_id": str(turn.get("reply_to_id") or ""),
                "metadata_json": json.dumps(
                    turn.get("metadata") or {}, ensure_ascii=False, sort_keys=True
                ),
                "created_at": str(turn.get("created_at") or ""),
                "seq": int(turn.get("seq") or 0),
            }
            highest = max(highest, row["seq"])
            conn.execute(
                f"INSERT INTO conversation_turns ({', '.join(_SESSION_TURN_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _SESSION_TURN_COLUMNS)})",
                tuple(row[column] for column in _SESSION_TURN_COLUMNS),
            )
            turns_added += 1
        for event in session.get("events") or []:
            if not isinstance(event, dict):
                continue
            seq = int(event.get("seq") or 0)
            highest = max(highest, seq)
            conn.execute(
                "INSERT INTO agent_events "
                "(session_id, event_type, payload_json, turn_id, created_at, seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    str(event.get("event_type") or ""),
                    json.dumps(event.get("payload") or {}, ensure_ascii=False),
                    str(event.get("turn_id") or ""),
                    str(event.get("created_at") or ""),
                    seq,
                ),
            )
            events_added += 1
        for row in session.get("replaced_versions") or []:
            if not isinstance(row, dict):
                continue
            conn.execute(
                "INSERT INTO conversation_turns_archive "
                "(session_id, role, content, channel, message_id, reply_to_id, "
                " metadata_json, created_at, seq, turn_index, branch_id, "
                " archived_at, archived_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    str(row.get("role") or "user"),
                    str(row.get("content") or ""),
                    str(row.get("channel") or ""),
                    str(row.get("message_id") or ""),
                    str(row.get("reply_to_id") or ""),
                    json.dumps(row.get("metadata") or {}, ensure_ascii=False),
                    str(row.get("created_at") or ""),
                    int(row.get("seq") or 0),
                    int(row.get("turn_index") or 0),
                    str(row.get("branch_id") or ""),
                    str(row.get("archived_at") or ""),
                    str(row.get("archived_by") or "import"),
                ),
            )
        title = str(session.get("title") or "").strip()
        if title:
            conn.execute(
                "INSERT INTO session_meta (session_id, title, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO NOTHING",
                (session_id, title, datetime.now(timezone.utc).isoformat()),
            )
        _bump_seq_counter(conn, session_id, highest)
        added += 1
    return {
        "sessions_added": added,
        "sessions_skipped": skipped,
        "turns_added": turns_added,
        "events_added": events_added,
    }


def _import_memory(conn: sqlite3.Connection, entries: list[dict[str, Any]]) -> dict[str, int]:
    from agent.memory import fts as fts_module

    added = skipped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            continue
        if conn.execute(
            "SELECT 1 FROM memory_items WHERE id = ?", (entry_id,)
        ).fetchone():
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO memory_items "
            "(id, content, importance, category, entity, memory_type, scope, status, "
            " source_session, confidence, pinned, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                str(entry.get("content") or ""),
                float(entry.get("importance") or 0.5),
                str(entry.get("category") or "concepts"),
                str(entry.get("entity") or ""),
                str(entry.get("memory_type") or "fact"),
                str(entry.get("scope") or "global"),
                str(entry.get("status") or "active"),
                str(entry.get("source_session") or ""),
                float(entry.get("confidence") or 1.0),
                int(entry.get("pinned") or 0),
                str(entry.get("created_at") or ""),
                str(entry.get("updated_at") or ""),
            ),
        )
        # The index is what retrieval reads; a row without one is a memory the
        # agent will never find.
        fts_module._sync_fts_row(conn, entry_id)
        added += 1
    return {"memory_added": added, "memory_skipped": skipped}


def _import_autonomy(path: Path, payload: dict[str, Any]) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {"proposals_added": 0, "goals_added": 0}
    added = goals = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    from agent.autonomy.store import ProposalStore

    ProposalStore(path).close()
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            for proposal in payload.get("proposals") or []:
                if not isinstance(proposal, dict):
                    continue
                proposal_id = str(proposal.get("id") or "").strip()
                if not proposal_id:
                    continue
                if conn.execute(
                    "SELECT 1 FROM proposals WHERE id = ?", (proposal_id,)
                ).fetchone():
                    continue
                columns = [key for key in proposal if key != "id"]
                conn.execute(
                    f"INSERT INTO proposals (id, {', '.join(columns)}) "
                    f"VALUES (?, {', '.join('?' for _ in columns)})",
                    (proposal_id, *[proposal[column] for column in columns]),
                )
                added += 1
            for goal in payload.get("goals") or []:
                if not isinstance(goal, dict):
                    continue
                goal_id = str(goal.get("id") or "").strip()
                if not goal_id:
                    continue
                if conn.execute(
                    "SELECT 1 FROM goals WHERE id = ?", (goal_id,)
                ).fetchone():
                    continue
                columns = [key for key in goal if key != "id"]
                conn.execute(
                    f"INSERT INTO goals (id, {', '.join(columns)}) "
                    f"VALUES (?, {', '.join('?' for _ in columns)})",
                    (goal_id, *[goal[column] for column in columns]),
                )
                goals += 1
    finally:
        conn.close()
    return {"proposals_added": added, "goals_added": goals}


def _import_output(
    archive: zipfile.ZipFile, names: list[str], web_sessions_dir: Path
) -> dict[str, int]:
    """Copy the agent's own output back, never overwriting what is there."""
    copied = skipped = 0
    for name in names:
        if not name.startswith("output/") or name.endswith("/"):
            continue
        relative = name[len("output/") :]
        # `output/<session>/output/<file>` in the archive; the session directory
        # is the one under the web root.
        parts = Path(relative).parts
        if len(parts) < 2:
            continue
        target = web_sessions_dir.joinpath(*parts)
        try:
            target.relative_to(web_sessions_dir)
        except ValueError:
            # A path that escapes the directory is not a file this archive is
            # allowed to write.
            skipped += 1
            continue
        if target.exists():
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, target.open("wb") as sink:
            shutil.copyfileobj(source, sink)
        copied += 1
    return {"output_copied": copied, "output_skipped": skipped}


def import_archive(
    archive_path: Path,
    *,
    context_dir: Path,
    tasks_dir: Path,
    autonomy_dir: Path,
    web_sessions_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Merge one archive into this install; return what happened per section."""
    path = Path(archive_path)
    if not path.is_file():
        raise ArchiveError("找不到这个文件")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"不是一个 ZIP 文件：{exc}") from None

    with archive:
        manifest = _read_json(archive, "manifest.json", None)
        if not isinstance(manifest, dict):
            raise ArchiveError("这个 ZIP 里没有 manifest.json，不像是一次导出")
        version = int(manifest.get("archive_version") or 0)
        if version > SUPPORTED_VERSION:
            raise ArchiveError(
                f"这个归档是更新版本（v{version}）导出的，当前程序只认到 "
                f"v{SUPPORTED_VERSION}。升级之后再导入，别让我猜新字段的意思。"
            )

        sessions = _read_json(archive, "sessions.json", [])
        memory = _read_json(archive, "memory.json", [])
        tasks = _read_json(archive, "tasks.json", {})
        autonomy = _read_json(archive, "autonomy.json", {})
        # Validated before anything is written: a malformed archive must leave
        # nothing behind rather than half of it.
        if not isinstance(sessions, list):
            raise ArchiveError("sessions.json 不是列表")
        if not isinstance(memory, list):
            raise ArchiveError("memory.json 不是列表")

        report: dict[str, Any] = {}
        # The store owns its schema; this module owns only the writes. Opening
        # and closing each store first is what makes importing into a *fresh*
        # install work -- measured: a raw connection to a home that had never run
        # the agent found no `conversation_turns` table at all.
        from agent.memory.store import LTMStore

        Path(context_dir).mkdir(parents=True, exist_ok=True)
        LTMStore(context_dir).close()
        palace = Path(context_dir) / "palace.db"
        conn = sqlite3.connect(str(palace))
        try:
            with conn:
                report.update(_import_sessions(conn, sessions))
                report.update(_import_memory(conn, memory))
        finally:
            conn.close()

        from agent.scheduler.importing import import_tasks

        report.update(import_tasks(Path(tasks_dir) / "scheduler.db", tasks))
        report.update(_import_autonomy(Path(autonomy_dir) / "proposals.db", autonomy))
        if web_sessions_dir is not None:
            report.update(
                _import_output(archive, archive.namelist(), Path(web_sessions_dir))
            )

    report["archive_version"] = version
    report["exported_at"] = str(manifest.get("exported_at") or "")
    return report
