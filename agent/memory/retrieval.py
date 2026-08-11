"""BM25-lite retrieval over long-term memory entries."""

from __future__ import annotations

import math

from agent import shared

from ._helpers import _lexical_terms
from .models import LTMEntry

class LocalRetriever:
    """BM25-lite retrieval with importance boosting. Pure stdlib, no external deps."""

    K1: float = 1.5
    B: float = 0.75

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """Language-aware tokenizer for lexical recall and reranking."""
        return _lexical_terms(text)

    def score(
        self, query: str, entries: list[LTMEntry]
    ) -> list[tuple[LTMEntry, float]]:
        """Score entries against query using BM25-lite + importance boost."""
        if not entries:
            return []
        query_terms = self.tokenize(query)
        if not query_terms:
            return [(e, e.importance) for e in entries]

        N = len(entries)
        df: dict[str, int] = {}
        tokenized: list[list[str]] = []

        for entry in entries:
            tokens = self.tokenize(
                f"{entry.content} {entry.entity} {entry.category} {entry.memory_type}"
            )
            tokenized.append(tokens)
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        avg_dl = sum(len(t) for t in tokenized) / N if N else 1.0

        scored: list[tuple[LTMEntry, float]] = []
        for i, entry in enumerate(entries):
            tokens = tokenized[i]
            dl = len(tokens)
            tf_map: dict[str, int] = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            bm25 = 0.0
            for term in query_terms:
                if term not in tf_map:
                    continue
                idf = math.log(
                    (N - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1
                )
                tf = tf_map[term]
                tf_norm = (
                    tf
                    * (self.K1 + 1)
                    / (tf + self.K1 * (1 - self.B + self.B * dl / avg_dl))
                )
                bm25 += idf * tf_norm

            # Importance acts as a multiplicative boost
            scored.append((entry, bm25 * (1.0 + entry.importance)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def retrieve(
        self, query: str, entries: list[LTMEntry], top_k: int = shared.RETRIEVAL_TOP_K
    ) -> list[LTMEntry]:
        """Return top-K most relevant entries (score > 0 only)."""
        scored = self.score(query, entries)
        return [entry for entry, s in scored[:top_k] if s > 0]
