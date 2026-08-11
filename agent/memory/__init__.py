"""Memory subsystem exports.

The subsystem is layered bottom-up, and each module may only import from the
ones above it in this list:

``_helpers``      constants and pure functions, depends on nothing
``models``        dataclasses exchanged between the layers
``staging``       append-only buffer of raw conversation turns
``store``         SQLite-backed long-term memory
``retrieval``     BM25-lite scoring over stored entries
``consolidation`` LLM-driven compaction of context into long-term memory
``palace``        user-facing facade over the store
``context``       per-turn orchestration of the above
``worker``        background thread that drains queued memory jobs

``system`` is a re-export facade kept for callers that still import the old
single-module path; nothing here should import from it.
"""

from ._helpers import normalize_memory_chapter
from .consolidation import ConsolidationEngine
from .context import ContextManager
from .models import (
    AgentRuntimeEvent,
    ContextLimitError,
    ConversationTurn,
    FactAssertion,
    LTMCategory,
    LTMEntry,
    ResolvedFact,
    SessionWorkingState,
)
from .palace import MemoryPalace
from .retrieval import LocalRetriever
from .staging import StagingBuffer
from .store import LTMStore
from .worker import BackgroundMemoryWorker

__all__ = [
    "BackgroundMemoryWorker",
    "AgentRuntimeEvent",
    "ConsolidationEngine",
    "ContextLimitError",
    "ContextManager",
    "ConversationTurn",
    "FactAssertion",
    "LTMCategory",
    "LTMEntry",
    "LTMStore",
    "LocalRetriever",
    "MemoryPalace",
    "ResolvedFact",
    "SessionWorkingState",
    "StagingBuffer",
    "normalize_memory_chapter",
]
