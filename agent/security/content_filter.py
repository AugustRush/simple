"""Bayesian content filter for pre-screening tool results before API submission.

Uses character n-gram Naive Bayes to score text as P(risky | features).
Learns from actual API "Content Exists Risk" rejections to improve over time.

Design:
- Features: unique 1-3 character n-grams from text (Chinese-focused)
- Laplace smoothing with strong "safe" prior (most content is not risky)
- Persisted to ~/.agent/content_filter_model.json
- False positives are acceptable (summary is lossy but functional)
- False negatives are handled by the recovery layer in agent.py
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from agent import shared


# Seed is intentionally empty. The classifier starts with uniform priors (score = 0.5
# for all inputs) and learns from actual API "Content Exists Risk" rejections.
# Adding seed n-grams would mean guessing what DeepSeek's filter looks for — wrong
# guesses cause false positives that permanently lose tool output via summarization.
# The cold-start case (first API rejection) is handled by _recover_from_content_filter
# in agent.py, which trains the model so the prevention layer works thereafter.
_SEED_RISKY_NGRAMS: set[str] = set()

# Character classes for feature extraction
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_ALNUM_RE = re.compile(r"[A-Za-z0-9぀-ヿ가-힯]")


def _extract_ngrams(text: str, n: int) -> list[str]:
    """Extract character n-grams, collapsing whitespace runs."""
    cleaned = " ".join(text.split())
    if len(cleaned) < n:
        return []
    return [cleaned[i : i + n] for i in range(len(cleaned) - n + 1)]


def _feature_set(text: str, max_n: int = 3) -> set[str]:
    """Extract unique 1..max_n character n-grams from text.

    Samples from head + tail to handle very long tool results efficiently.
    """
    head_len = min(len(text), 2000)
    tail_start = max(head_len, len(text) - 500)
    sample = text[:head_len] + text[tail_start:]
    features: set[str] = set()
    for n in range(1, max_n + 1):
        features.update(_extract_ngrams(sample, n))
    return features


class ContentFilter:
    """Naive Bayes classifier for detecting content likely to trigger API filters."""

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha  # Laplace smoothing: small alpha = strong "safe" prior
        self._safe_counts: Counter[str] = Counter()
        self._risky_counts: Counter[str] = Counter()
        self._safe_total: int = 0
        self._risky_total: int = 0
        # Seed prior: each seed n-gram gets 1 risky count
        for gram in _SEED_RISKY_NGRAMS:
            self._risky_counts[gram] = 1
            self._risky_total += 1

    # ── scoring ──────────────────────────────────────────────────────────────

    def score(self, text: str) -> float:
        """Return P(risky | text) using log-space Naive Bayes.

        Returns a probability in [0, 1].  Higher = more likely to trigger a
        content filter.  Default return is 0.0 when no features are available.
        """
        features = _feature_set(text)
        if not features:
            return 0.0

        vocab: set[str] = set(self._safe_counts) | set(self._risky_counts) | features
        vocab_size = len(vocab)

        # Log priors: uniform prior when no data, otherwise empirical
        safe_events = self._safe_total + self.alpha * vocab_size
        risky_events = self._risky_total + self.alpha * vocab_size
        total_events = safe_events + risky_events

        log_p_safe = math.log(safe_events / total_events) if total_events > 0 else math.log(0.999)
        log_p_risky = math.log(risky_events / total_events) if total_events > 0 else math.log(0.001)

        for f in features:
            safe_count = self._safe_counts.get(f, 0) + self.alpha
            risky_count = self._risky_counts.get(f, 0) + self.alpha
            log_p_safe += math.log(safe_count / safe_events)
            log_p_risky += math.log(risky_count / risky_events)

        # Convert from log-space: P(risky) = exp(log_p_risky) / (exp(log_p_safe) + exp(log_p_risky))
        # Guard against underflow: when both log-probs are extremely negative,
        # math.exp() returns 0.0 and we fall back to uniform prior.
        max_log = max(log_p_safe, log_p_risky)
        if max_log < -700:  # exp(-700) ≈ 0.0 in double precision
            # Both underflow → uniform prior (0.5 when no data, empirical otherwise)
            if total_events > 0:
                return self._risky_total / total_events
            return 0.5
        if log_p_risky - log_p_safe > 50:
            return 1.0
        if log_p_safe - log_p_risky > 50:
            return 0.0
        e_risky = math.exp(log_p_risky - max_log)
        e_safe = math.exp(log_p_safe - max_log)
        total = e_safe + e_risky
        if total == 0.0:
            return 0.5
        return e_risky / total

    def is_risky(self, text: str, threshold: float = 0.7) -> bool:
        """Convenience: True if score exceeds threshold."""
        return self.score(text) > threshold

    # ── learning ─────────────────────────────────────────────────────────────

    def learn(self, text: str, is_risky: bool) -> None:
        """Update the model with a labeled example."""
        features = _feature_set(text)
        if not features:
            return
        if is_risky:
            for f in features:
                self._risky_counts[f] += 1
                self._risky_total += 1
        else:
            for f in features:
                self._safe_counts[f] += 1
                self._safe_total += 1

    def learn_batch(self, texts: list[str], is_risky: bool) -> None:
        for text in texts:
            self.learn(text, is_risky)

    async def learn_and_persist(
        self,
        risky_texts: list[str],
        *,
        path: "Path | None" = None,
    ) -> None:
        """Atomically update the classifier with risky examples and persist.

        Combines the two steps every caller needs (``learn`` + ``save``)
        and offloads disk I/O to a thread.  Empty input is a no-op so the
        caller can pass a filtered list without a guard.
        """
        if not risky_texts:
            return
        import asyncio

        for text in risky_texts:
            self.learn(text, is_risky=True)
        target = path or default_model_path()
        await asyncio.to_thread(self.save, target)

    # ── persistence ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "safe_counts": dict(self._safe_counts),
            "risky_counts": dict(self._risky_counts),
            "safe_total": self._safe_total,
            "risky_total": self._risky_total,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentFilter":
        inst = cls(alpha=data.get("alpha", 0.1))
        inst._safe_counts = Counter(data.get("safe_counts", {}))
        inst._risky_counts = Counter(data.get("risky_counts", {}))
        inst._safe_total = data.get("safe_total", 0)
        inst._risky_total = data.get("risky_total", 0)
        return inst

    def save(self, path: Path) -> None:
        """Persist durably. Concurrency here is normal, not exotic.

        ``learn_and_persist`` offloads this to a thread while the event loop
        constructs sub-agents that each ``load`` the same path, and a parent and
        its children can persist at once.  An in-place ``write_text`` truncates
        first, so those readers saw an empty file and callers raced each other
        into invalid JSON that then loaded as a blank classifier forever.
        """
        shared._atomic_write_json(path, self.to_dict(), indent=2)

    @classmethod
    def load(cls, path: Path) -> "ContentFilter":
        """Load the classifier, or start fresh if there is nothing to load.

        Absent and unreadable are different events.  No file is the ordinary
        first-run case.  A file that exists but will not parse means learned
        state was lost, which is worth a log line and worth keeping the evidence
        for — silently substituting an empty classifier turns data loss into an
        invisible capability regression.
        """
        if not path.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except Exception:
            logging.getLogger("agent").warning(
                "content filter at %s is unreadable; starting from an empty "
                "classifier and preserving the file for inspection",
                path,
                exc_info=True,
            )
            with contextlib.suppress(OSError):
                path.replace(path.with_name(f"{path.name}.corrupt"))
            return cls()

    # ── statistics ───────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "safe_examples": self._safe_total,
            "risky_examples": self._risky_total,
            "vocab_size": len(set(self._safe_counts) | set(self._risky_counts)),
        }


# ── Tool result summarization ────────────────────────────────────────────────

# Metadata allowlists for built-in file service results.  Bodies (read content,
# listed items, replacement text) are dropped; the fields the agent needs to
# continue safely (revisions, ranges, cursors, statistics) are preserved.
_STRUCTURED_FILE_SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "read_file": (
        "root",
        "path",
        "revision",
        "start_line",
        "end_line",
        "total_lines",
        "next_start_line",
        "size_bytes",
        "returned_bytes",
        "encoding",
        "bom",
        "newline",
    ),
    "write_file": (
        "root",
        "path",
        "mode",
        "old_revision",
        "new_revision",
        "old_size_bytes",
        "new_size_bytes",
        "byte_delta",
    ),
    "edit_file": (
        "root",
        "path",
        "mode",
        "old_revision",
        "new_revision",
        "old_size_bytes",
        "new_size_bytes",
        "byte_delta",
        "replacement_count",
        "replaced_occurrences",
    ),
    "list_files": (
        "root",
        "path",
        "count",
        "truncated",
        "next_cursor",
    ),
}


def _truncate_text(text: str, limit: int = 200) -> str:
    """Truncate text at a word/sentence boundary near limit."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    # Try to break at newline
    boundary = text.rfind("\n", 0, limit)
    if boundary > limit // 2:
        return text[:boundary].rstrip() + "\n...[truncated]"
    # Try to break at sentence end
    for punct in (". ", "。", "！", "？", "\n"):
        boundary = text.rfind(punct, 0, limit)
        if boundary > limit // 2:
            return text[: boundary + len(punct)].rstrip() + "\n...[truncated]"
    return text[:limit].rstrip() + "...[truncated]"


def summarize_tool_result(
    tool_name: str,
    raw_result: str,
    *,
    include_preview: bool = True,
) -> str:
    """Create a safe, brief summary of a tool result.

    Strips potentially risky text content while preserving factual metadata
    the agent needs to continue working.
    """
    raw = str(raw_result or "")
    try:
        data = json.loads(raw)
    except Exception:
        data = None

    if isinstance(data, dict):
        # Structured file-service envelopes get metadata-preserving summaries
        # before any generic truncation applies.
        if data.get("ok") is False:
            err = data.get("error", "unknown error")
            summary: dict[str, Any] = {"ok": False, "summarized": True}
            if isinstance(err, dict):
                # Preserve the stable error code/details/retryability envelope.
                summary["error"] = dict(err)
            else:
                summary["error"] = err
            return json.dumps(summary, ensure_ascii=False)
        if data.get("ok") is True and tool_name in _STRUCTURED_FILE_SUMMARY_FIELDS:
            summary = {"ok": True, "summarized": True}
            for field in _STRUCTURED_FILE_SUMMARY_FIELDS[tool_name]:
                if field in data:
                    summary[field] = data[field]
            return json.dumps(summary, ensure_ascii=False)

    # Per-tool summarization strategies
    def preview_payload(raw_text: str, limit: int) -> dict[str, Any]:
        if include_preview:
            return {"preview": _truncate_text(raw_text, limit)}
        return {"preview_omitted": True}

    if tool_name in ("read_file", "memory_read", "read_skill_file"):
        # Extract filename + size stats; strip content
        lines = raw.split("\n")
        return json.dumps({
            "ok": True,
            "summarized": True,
            "lines": len(lines),
            "size_bytes": len(raw),
            **preview_payload(raw, 500),
        })

    if tool_name == "shell":
        # Keep exit status + preview of stdout; strip full output
        lines = raw.split("\n")
        head = "\n".join(lines[:10])
        return json.dumps({
            "ok": True,
            "summarized": True,
            "output_lines": len(lines),
            **preview_payload(head, 500),
        })

    if tool_name in ("web_fetch", "web_search", "tavily_search"):
        return json.dumps({
            "ok": True,
            "summarized": True,
            "size_bytes": len(raw),
            **preview_payload(raw, 500),
        })

    if tool_name in ("list_files", "memory_index", "memory_search"):
        # These are usually safe (file paths, metadata) — keep as-is if short
        if len(raw) <= 2000:
            return raw
        return json.dumps({
            "ok": True,
            "summarized": True,
            "count": raw.count("\n") + 1,
            **preview_payload(raw, 500),
        })

    # Generic: keep if short, truncate if long
    if len(raw) <= 1000:
        return raw
    return json.dumps({
        "ok": True,
        "summarized": True,
        "size_bytes": len(raw),
        **preview_payload(raw, 300),
    })


# ── Message-level operations ────────────────────────────────────────────────


def summarize_tool_results(
    tool_calls: list[dict],
    results: list[str],
) -> list[str]:
    """Summarize a batch of tool results, one per call."""
    return [
        summarize_tool_result(tc["name"], res)
        for tc, res in zip(tool_calls, results)
    ]


def filter_tool_results(
    content_filter: ContentFilter,
    tool_calls: list[dict],
    results: list[str],
    *,
    threshold: float = 0.7,
) -> tuple[list[str], list[int]]:
    """Screen tool results through the content filter.

    Returns:
        (filtered_results, risky_indices)
        filtered_results: list of result strings, summarized where risky
        risky_indices: indices of results that were flagged
    """
    filtered: list[str] = []
    risky: list[int] = []
    for i, (tc, res) in enumerate(zip(tool_calls, results)):
        if content_filter.is_risky(res, threshold=threshold):
            filtered.append(
                summarize_tool_result(tc["name"], res, include_preview=False)
            )
            risky.append(i)
        else:
            filtered.append(res)
    return filtered, risky


# ── Default model path ──────────────────────────────────────────────────────

def default_model_path() -> Path:
    from agent.shared import AGENT_HOME

    return AGENT_HOME / "content_filter_model.json"
