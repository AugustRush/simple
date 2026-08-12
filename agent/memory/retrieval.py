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
    """BM25-lite relevance ranking. Pure stdlib, no external deps.

    Deliberately *only* relevance.  Scores used to be multiplied by
    ``(1 + entry.importance)``, which conflated two different questions:

    - **ranking** — given this query, which memory is most relevant?
    - **retention** — with limited space, which memory should I forget?

    Importance answers the second well and the first not at all, and mixing
    them made ranking worse.  Measured on the eval set: ablating the boost
    improved MRR (0.587 -> 0.609), and a permutation test over 120 random
    reassignments of the same importance values put the real assignment at
    the 3rd percentile — p ~ 0.97 that a random shuffle ranks at least as
    well.  It was not noise; it was worse than noise.

    The mechanism is visible in any real store: importance anti-correlates
    almost perfectly with document length (self_identity 0.95 / 30 chars,
    notes 0.80 / 222, session summaries 0.48 / 458, sub-agent observations
    0.31 / 2850).  BM25 *already* applies a length prior through its ``dl /
    avg_dl`` normalization, so multiplying by importance double-counted it —
    short entries were promoted twice for the same reason, pushing longer
    but genuinely relevant entries down.

    Importance is still carried on the entry and still drives decay and
    eviction, which is the job it is actually good at.
    """

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
        """Score entries against *query* by relevance alone.

        *corpus* supplies store-wide document frequencies.  Without it the
        frequencies fall back to the candidate list, which only ranks
        *within* an already-filtered set and should not be relied on when the
        candidates came from a query-driven prefilter.
        """
        if not entries:
            return []
        query_terms = self.tokenize(query)
        if not query_terms:
            # No query means no relevance signal exists; fall back to the
            # retention prior, which is what importance is for.
            return sorted(
                ((e, e.importance) for e in entries),
                key=lambda item: item[1],
                reverse=True,
            )

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

            scored.append((entry, bm25))

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
