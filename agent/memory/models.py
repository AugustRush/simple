"""Value types exchanged across the memory subsystem.

Plain dataclasses and one exception — no behaviour beyond field defaults, so
every other memory module can depend on this without cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent import shared

from ._helpers import _now

class ContextLimitError(RuntimeError):
    """Raised when complete provider context cannot fit its input budget."""

@dataclass
class LTMEntry:
    """A single long-term memory entry with importance scoring."""

    id: str
    content: str
    importance: float  # 0.0 – 1.0
    category: str
    created_at: str
    updated_at: str
    entity: str = ""
    memory_type: str = "fact"
    scope: str = "global"
    status: str = "active"
    source_session: str = ""
    confidence: float = 1.0

    def decay(self, factor: float = shared.DECAY_FACTOR) -> None:
        self.importance = max(0.0, self.importance * factor)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "importance": self.importance,
            "category": self.category,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "entity": self.entity,
            "memory_type": self.memory_type,
            "scope": self.scope,
            "status": self.status,
            "source_session": self.source_session,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LTMEntry":
        return cls(**d)


@dataclass(frozen=True)
class ConversationTurn:
    """A durable event-log row for one plain-text conversation message."""

    id: int
    session_id: str
    role: str
    content: str
    channel: str = ""
    message_id: str = ""
    reply_to_id: str = ""
    metadata: dict[str, Any] | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "channel": self.channel,
            "message_id": self.message_id,
            "reply_to_id": self.reply_to_id,
            "metadata": self.metadata or {},
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ConversationWriteResult:
    """Rows created while journaling one user/assistant exchange."""

    user_created: bool = False
    assistant_created: bool = False
    first_assistant_for_user: bool = False

    @property
    def changed(self) -> bool:
        return self.user_created or self.assistant_created


@dataclass(frozen=True)
class SessionWorkingState:
    """Durable, model-readable working context for one channel session."""

    session_id: str
    state: dict[str, Any]
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AgentRuntimeEvent:
    """Append-only runtime fact for one channel session."""

    id: int
    session_id: str
    event_type: str
    payload: dict[str, Any]
    turn_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass
class FactAssertion:
    """An append-only normalized fact claim derived from some evidence source."""

    id: str
    subject: str
    predicate: str
    value: Any
    value_type: str = ""
    scope: str = "global"
    source_kind: str = "manual_write"
    source_id: str = ""
    source_session: str = ""
    channel: str = ""
    confidence: float = 1.0
    status: str = "active"
    valid_from: str = ""
    valid_to: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class ResolvedFact:
    """The current best belief for a canonical fact key."""

    fact_key: str
    subject: str
    predicate: str
    value: Any
    value_type: str = ""
    scope: str = "global"
    winning_assertion_id: str = ""
    resolution_reason: str = "resolved"
    confidence: float = 1.0
    resolved_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass(frozen=True)
class QueryPlan:
    """A lightweight plan for exact-fact versus freeform retrieval."""

    query_type: str = "freeform_context"
    scope: str = "global"
    target_subjects: tuple[str, ...] = ()
    target_predicates: tuple[str, ...] = ()
    lexical_terms: tuple[str, ...] = ()
    allow_freeform_fallback: bool = True


@dataclass
class LTMCategory:
    """Metadata for a long-term memory category."""

    name: str
    entry_count: int = 0
    avg_importance: float = 0.0
    last_updated: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entry_count": self.entry_count,
            "avg_importance": self.avg_importance,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LTMCategory":
        return cls(**d)


@dataclass
class ConsolidationResult:
    success: bool
    compressed_messages: list[dict]
    stored_entries: int = 0
