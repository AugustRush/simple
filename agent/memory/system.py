"""Backwards-compatible façade for the memory subsystem.

This module used to hold the whole subsystem in one 5000-line file.  It was
split into layered modules (``_helpers`` -> ``models`` -> ``store``/``staging``/
``retrieval`` -> ``consolidation``/``palace`` -> ``context`` -> ``worker``), and
the re-exports below are exactly the names callers still reach through this
path.  New code should import from the specific module instead.

Note for patching: ``monkeypatch.setattr("agent.memory.system.foo", ...)``
rebinds only this module's alias, not the definition the other modules call.
Patch the owning module (e.g. ``agent.memory.store.foo``) instead.
"""

from __future__ import annotations

from ._helpers import SQLITE_BUSY_TIMEOUT_MS, normalize_memory_chapter
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
from .context import ContextManager, enqueue_orphan_staging_recovery
from .worker import BackgroundMemoryWorker, BackgroundMemoryWorkerPool, PooledMemoryWorkerHandle
from .palace import MemoryPalace

__all__ = [
    "AgentRuntimeEvent",
    "BackgroundMemoryWorker",
    "BackgroundMemoryWorkerPool",
    "PooledMemoryWorkerHandle",
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
