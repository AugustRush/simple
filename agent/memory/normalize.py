"""Normalising the values the store keys on.

Category names, content keys, entity names, and the merge key that decides whether two entries
are the same thing.  Moved out of ``LTMStore`` (see the remaining-big-files survey); every one is
a pure function of its arguments, and every one is called from elsewhere in the store, so the
class keeps a thin wrapper for each -- that is what lets ``self._normalize_entity(...)`` keep
working at its many call sites.
"""

from __future__ import annotations

import re
from typing import Optional

from agent import shared

from .models import LTMEntry

__all__ = [
    "normalize_category_name",
    "_normalize_content_key",
    "_is_palace_locus",
    "_normalize_entity",
    "_stable_merge_key",
]


def normalize_category_name(name: str) -> str:
    """Collapse external category input into a safe, stable storage key."""
    normalized = re.sub(r"[\\/]+", " ", str(name).strip().lower())
    normalized = normalized.replace("..", " ")
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("._")
    return normalized or "general"


def _is_palace_locus(
    category: str,
) -> bool:
    return normalize_category_name(category) in shared.PALACE_LOCI


def _normalize_entity(
    entity: str,
    category: str,
) -> str:
    raw = str(entity).strip()
    if raw:
        return normalize_category_name(raw)
    if _is_palace_locus(category):
        return (
            "user"
            if normalize_category_name(category) == "identity"
            else "general"
        )
    return normalize_category_name(category)


def _normalize_content_key(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content.strip().lower())
    return normalized


def _stable_merge_key(
    entry: LTMEntry,
) -> Optional[str]:
    category = normalize_category_name(entry.category)
    entity = _normalize_entity(entry.entity, category)
    memory_type = (entry.memory_type or "fact").strip().lower()

    if category in {"episodes", "archive", "concepts"}:
        return None
    if category == "tasks":
        normalized_content = _normalize_content_key(entry.content)
        return f"{category}|{entity}|{memory_type}|{normalized_content}"
    if category not in {"identity", "projects", "people", "procedures"}:
        return None

    normalized_content = _normalize_content_key(entry.content)
    return f"{category}|{entity}|{memory_type}|{normalized_content}"
