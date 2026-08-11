"""Backwards-compatible façade for the memory subsystem.

This module used to hold the whole subsystem in one 5000-line file.  It was
split into layered modules (``_helpers`` -> ``models`` -> ``store``/``staging``/
``retrieval`` -> ``consolidation``/``palace`` -> ``context`` -> ``worker``); the
re-exports below keep every ``agent.memory.system.X`` import and monkeypatch
target working.  New code should import from the specific module instead.

Note for patching: ``monkeypatch.setattr("agent.memory.system.foo", ...)`` now
rebinds only this module's alias, not the definition the other modules call.
Patch the owning module (e.g. ``agent.memory.store.foo``) instead.
"""

from __future__ import annotations

from ._helpers import (
    SQLITE_BUSY_TIMEOUT_MS,
    _ASSISTANT_IDENTITY_ENTITIES,
    _FACT_QUERY_PREDICATE_ALIASES,
    _FACT_QUERY_SUBJECT_ALIASES,
    _FACT_SOURCE_PRECEDENCE,
    _IDENTITY_NOTE_MAX_CHARS,
    _IDENTITY_PREDICATES,
    _IDENTITY_SUBJECTS,
    _RECENCY_GOVERNED_FACTS,
    _RETIRED_INFERENCE_SOURCE_KINDS,
    _RUN_SCRATCH_MAX_ACTIVE,
    _RUN_SCRATCH_RETENTION_DAYS,
    _dump_fact_value,
    _emit_consolidation,
    _fact_key,
    _fact_value_type,
    _identity_note_value,
    _lexical_terms,
    _load_fact_value,
    _new_id,
    _normalize_fact_part,
    _now,
    normalize_memory_chapter,
)
from .models import (
    AgentRuntimeEvent,
    ConsolidationResult,
    ContextLimitError,
    ConversationTurn,
    ConversationWriteResult,
    FactAssertion,
    LTMCategory,
    LTMEntry,
    QueryPlan,
    ResolvedFact,
    SessionWorkingState,
)
from .staging import StagingBuffer
from .store import LTMStore
from .retrieval import LocalRetriever
from .consolidation import ConsolidationEngine
from .context import ContextManager
from .worker import BackgroundMemoryWorker
from .palace import MemoryPalace

__all__ = [
    "AgentRuntimeEvent",
    "BackgroundMemoryWorker",
    "ConsolidationEngine",
    "ConsolidationResult",
    "ContextLimitError",
    "ContextManager",
    "ConversationTurn",
    "ConversationWriteResult",
    "FactAssertion",
    "LTMCategory",
    "LTMEntry",
    "LTMStore",
    "LocalRetriever",
    "MemoryPalace",
    "QueryPlan",
    "ResolvedFact",
    "SQLITE_BUSY_TIMEOUT_MS",
    "SessionWorkingState",
    "StagingBuffer",
    "normalize_memory_chapter",
]
