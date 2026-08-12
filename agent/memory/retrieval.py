"""BM25-lite retrieval over long-term memory entries."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Mapping, Optional

from agent import shared

from ._helpers import _lexical_terms
from .models import LTMEntry


@dataclass(frozen=True)
class CorpusStats:
    """Document frequencies measured over the whole store, not the candidates.

    IDF answers "how surprising is this term in the corpus".  Measured over a
    candidate list that was *selected by matching the query*, the answer is
    always "not surprising at all" — df ≈ N for exactly the query terms — so
    the ranking is driven by whatever incidental words vary between
    candidates.  Passing real corpus statistics is what makes the re-rank a
    re-rank rather than noise.
    """

    total_documents: int
    document_frequencies: Mapping[str, int] = field(default_factory=dict)

    def idf(self, term: str, *, fallback_n: int) -> float:
        n = self.total_documents or fallback_n
        df = self.document_frequencies.get(term, 0)
        return math.log((n - df + 0.5) / (df + 0.5) + 1)


class LocalRetriever:
    """BM25-lite retrieval with importance boosting. Pure stdlib, no external deps."""

    K1: float = 1.5
    B: float = 0.75

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """Language-aware tokenizer for lexical recall and reranking."""
        return _lexical_terms(text)

    def score(
        self,
        query: str,
        entries: list[LTMEntry],
        corpus: Optional[CorpusStats] = None,
    ) -> list[tuple[LTMEntry, float]]:
        """Score entries against query using BM25-lite + importance boost.

        *corpus* supplies store-wide document frequencies.  Without it the
        frequencies fall back to the candidate list, which only ranks
        *within* an already-filtered set and should not be relied on when the
        candidates came from a query-driven prefilter.
        """
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

        def _idf(term: str) -> float:
            if corpus is not None:
                return corpus.idf(term, fallback_n=N)
            local_df = df.get(term, 0)
            return math.log((N - local_df + 0.5) / (local_df + 0.5) + 1)

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
                tf = tf_map[term]
                tf_norm = (
                    tf
                    * (self.K1 + 1)
                    / (tf + self.K1 * (1 - self.B + self.B * dl / avg_dl))
                )
                bm25 += _idf(term) * tf_norm

            # Importance acts as a multiplicative boost
            scored.append((entry, bm25 * (1.0 + entry.importance)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def retrieve(
        self,
        query: str,
        entries: list[LTMEntry],
        top_k: int = shared.RETRIEVAL_TOP_K,
        corpus: Optional[CorpusStats] = None,
    ) -> list[LTMEntry]:
        """Return top-K most relevant entries (score > 0 only)."""
        scored = self.score(query, entries, corpus)
        return [entry for entry, s in scored[:top_k] if s > 0]
