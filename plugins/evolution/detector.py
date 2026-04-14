"""CorrectionDetector — identifies user corrections in conversation turns.

Uses lightweight heuristics (regex patterns) to classify whether a user
message is correcting the agent's previous response.  No LLM calls are
made here, keeping detection latency at zero.

Patterns are split into HIGH and LOW confidence tiers.  A single HIGH match
is sufficient; LOW-tier patterns require at least TWO distinct matches to
reduce false positives (e.g. "actually" alone is ambiguous).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ── HIGH confidence: a single match is strong enough ──────────────────────────
_HIGH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(that'?s?\s*(not\s*right|wrong|incorrect|not\s*what\s*i\s*(meant|asked|wanted)))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(no[,.]?\s+(that|you|it|this)\s+(is|are|was|were)\s+)", re.IGNORECASE
    ),
    re.compile(r"\b(you\s+misunderstood|not\s+what\s+i\s+said)\b", re.IGNORECASE),
    re.compile(r"^(no[,!.]|nope[,!.]|wrong[,!.]|incorrect[,!.])\s", re.IGNORECASE),
    # Chinese: high-confidence correction phrases
    re.compile(r"(不是这样|说错了|理解错了|搞错了|不是我要的)", re.IGNORECASE),
]

# ── LOW confidence: individually ambiguous, need ≥2 to fire ───────────────────
_LOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bactually\b", re.IGNORECASE),
    re.compile(r"\bwait\b", re.IGNORECASE),
    re.compile(r"\bhold\s+on\b", re.IGNORECASE),
    re.compile(r"\bI\s+meant\b", re.IGNORECASE),
    # Chinese: individually ambiguous — one pattern per phrase for distinct counting.
    re.compile(r"不对", re.IGNORECASE),
    re.compile(r"我的意思是", re.IGNORECASE),
    re.compile(r"其实", re.IGNORECASE),
    re.compile(r"等等", re.IGNORECASE),
    re.compile(r"重新来", re.IGNORECASE),
]

# Minimum response length (in characters) to consider a correction meaningful.
# Kept low so Chinese responses (semantic content in fewer chars) are included.
_MIN_PREV_RESPONSE_LEN = 5


@dataclass
class CorrectionSignal:
    """Result of correction detection with a confidence score."""

    is_correction: bool
    confidence: float = 0.0  # 0.0 … 1.0
    matched_patterns: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.matched_patterns is None:
            self.matched_patterns = []


class CorrectionDetector:
    """Detects user-correction signals from conversation turn pairs."""

    def detect(self, user_input: str, prev_response: str) -> CorrectionSignal:
        """Analyse *user_input* for correction signals.

        Returns a CorrectionSignal with confidence.
        """
        if not prev_response or len(prev_response) < _MIN_PREV_RESPONSE_LEN:
            return CorrectionSignal(is_correction=False, confidence=0.0)
        if not user_input.strip():
            return CorrectionSignal(is_correction=False, confidence=0.0)

        matched: list[str] = []

        # Check HIGH-confidence patterns (any single match → correction).
        for pat in _HIGH_PATTERNS:
            if pat.search(user_input):
                matched.append(pat.pattern)
        if matched:
            return CorrectionSignal(
                is_correction=True,
                confidence=0.9,
                matched_patterns=matched,
            )

        # Check LOW-confidence patterns (need ≥ 2 distinct matches).
        for pat in _LOW_PATTERNS:
            if pat.search(user_input):
                matched.append(pat.pattern)
        if len(matched) >= 2:
            return CorrectionSignal(
                is_correction=True,
                confidence=0.5,
                matched_patterns=matched,
            )

        return CorrectionSignal(is_correction=False, confidence=0.0)

    # Convenience wrapper for backward compatibility.
    def is_correction(self, user_input: str, prev_response: str) -> bool:
        return self.detect(user_input, prev_response).is_correction
