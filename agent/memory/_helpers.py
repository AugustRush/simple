"""Shared constants and small pure helpers for the memory subsystem.

The bottom layer: this module imports nothing else from ``agent.memory``."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from agent.lexical import lexical_terms

# How long a SQLite connection waits for a competing writer before giving up.
# WAL mode allows concurrent readers but only one writer; the background
# memory worker and the synchronous-tool pool both write to palace.db, so
# without this they would surface transient "database is locked" errors.
SQLITE_BUSY_TIMEOUT_MS = 5000


_FACT_SOURCE_PRECEDENCE = {
    "identity_directive": 0,
    "user_statement": 0,
    "direct_user": 0,
    "correction": 1,
    "bootstrap": 2,
    "manual_write": 3,
    "conversation_turn": 4,
    "consolidation_extract": 5,
    "summary_extract": 6,
}
_RUN_SCRATCH_RETENTION_DAYS = 7
_RUN_SCRATCH_MAX_ACTIVE = 500
_FACT_QUERY_SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "assistant": (
        "assistant",
        "agent",
        "bot",
        "you",
        "your",
        "你",
        "你自己",
        "你的",
        "助手",
        "机器人",
    ),
    "user": (
        "user",
        "the user",
        "me",
        "my",
        "我",
        "我的",
        "用户",
    ),
}
_FACT_QUERY_PREDICATE_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "your name",
        "my name",
        "名字",
        "叫什么",
        "叫啥",
        "称呼",
    ),
    "role": (
        "role",
        "who are you",
        "what are you",
        "身份",
        "角色",
        "你是谁",
        "你是什么",
    ),
}
# Identity is a setting, so the newest statement of it wins outright and
# source authority only breaks ties.  Everywhere else a trusted source still
# outranks a fresher guess.
_IDENTITY_PREDICATES: tuple[str, ...] = ("name", "role", "identity_note")
_IDENTITY_SUBJECTS: frozenset[str] = frozenset({"assistant", "user"})
_RECENCY_GOVERNED_FACTS: frozenset[tuple[str, str]] = frozenset(
    (subject, predicate)
    for subject in _IDENTITY_SUBJECTS
    for predicate in _IDENTITY_PREDICATES
)
# Source kinds retired with the pattern-matching identity extractor.  Rows they
# left behind are not evidence any more, because the code that judged them no
# longer exists — see LTMStore._retract_inferred_identity_facts.
_RETIRED_INFERENCE_SOURCE_KINDS: tuple[str, ...] = ("user_statement", "conversation_turn")
_ASSISTANT_IDENTITY_ENTITIES: frozenset[str] = frozenset(
    {"assistant", "assistant_identity", "self", "agent"}
)
_IDENTITY_NOTE_MAX_CHARS = 600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _emit_consolidation(phase: str, **fields: Any) -> None:
    """Emit a consolidation lifecycle event into the active EventCollector."""
    try:
        from agent.core.output import _active_event_collector
        collector = _active_event_collector.get()
        if collector is not None:
            collector.emit(f"consolidation_{phase}", **fields)
    except Exception:
        pass  # never let event emission break maintenance


def normalize_memory_chapter(chapter: str, aliases: dict[str, str]) -> str:
    chapter = str(chapter).strip().lower()
    return aliases.get(chapter, chapter)


def _normalize_fact_part(value: str, default: str = "") -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized or default


def _fact_key(subject: str, predicate: str, scope: str) -> str:
    return json.dumps(
        [
            _normalize_fact_part(subject),
            _normalize_fact_part(predicate),
            _normalize_fact_part(scope, "global"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _fact_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (dict, list)):
        return "json"
    return "string"


def _dump_fact_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_fact_value(value_json: str) -> Any:
    return json.loads(value_json)


def _identity_note_value(text: str) -> str:
    """The prose of an identity entry, fit to sit in a system prompt.

    Kept verbatim apart from a length bound: tone and persona live in the exact
    wording, and this module has no business deciding which parts of what the
    user wrote are the "real" identity.
    """
    note = str(text or "").strip()
    if len(note) > _IDENTITY_NOTE_MAX_CHARS:
        note = note[:_IDENTITY_NOTE_MAX_CHARS].rstrip() + "…"
    return note


def _lexical_terms(text: str) -> list[str]:
    return lexical_terms(text)
