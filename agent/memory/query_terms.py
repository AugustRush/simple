"""Reading a query: which aliases it mentions, whether it asks for two things, how much it overlaps.

Moved out of ``ContextManager`` (see the remaining-big-files survey).  All three are
``self``-free predicates over text: given a query, say what it names.  They are the part of
retrieval that can be reasoned about without a store, a session or an embedding.
"""

from __future__ import annotations

from typing import Any

from ._helpers import _lexical_terms

__all__ = ['_matched_fact_alias_terms', '_has_multi_intent_marker', '_weighted_term_overlap']


@staticmethod
def _matched_fact_alias_terms(
    lowered_query: str,
    aliases: dict[str, tuple[str, ...]],
) -> tuple[list[str], set[str]]:
    targets: list[str] = []
    matched_terms: set[str] = set()
    for key, variants in aliases.items():
        hits = [variant for variant in variants if variant in lowered_query]
        if not hits:
            continue
        targets.append(key)
        for hit in hits:
            terms = _lexical_terms(hit)
            if terms:
                matched_terms.update(terms)
            else:
                matched_terms.add(hit)
    return targets, matched_terms


@staticmethod
def _has_multi_intent_marker(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(
        marker in lowered
        for marker in (
            " and ",
            " also ",
            " plus ",
            "另外",
            "还有",
            "以及",
            "顺便",
            "并且",
        )
    )


@staticmethod
def _weighted_term_overlap(
    query_terms: set[str],
    value: Any,
    *,
    weight: float,
) -> float:
    terms = set(_lexical_terms(str(value or "")))
    if not terms:
        return 0.0
    return len(query_terms & terms) * weight
