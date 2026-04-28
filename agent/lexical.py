from __future__ import annotations

import re
from typing import Mapping

LATIN_TOKEN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9_]*\b")
CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def count_cjk_chars(text: str) -> int:
    return len(CJK_CHAR_RE.findall(str(text or "")))


def cjk_ngrams(text: str, min_n: int = 2, max_n: int = 3) -> list[str]:
    terms: list[str] = []
    clean = str(text or "").strip()
    if not clean:
        return terms
    for run in CJK_RUN_RE.findall(clean):
        if len(run) < min_n:
            terms.append(run)
            continue
        upper = min(max_n, len(run))
        for n in range(min_n, upper + 1):
            for i in range(0, len(run) - n + 1):
                terms.append(run[i : i + n])
    return terms


def lexical_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in LATIN_TOKEN_RE.findall(str(text or "").lower()):
        if token not in seen:
            terms.append(token)
            seen.add(token)
    for token in cjk_ngrams(text):
        if token not in seen:
            terms.append(token)
            seen.add(token)
    return terms


def keyword_terms(
    text: str,
    *,
    stopwords: set[str] | frozenset[str] | None = None,
    aliases: Mapping[str, str] | None = None,
    latin_min_len: int = 3,
) -> set[str]:
    stopwords = stopwords or set()
    aliases = aliases or {}
    terms: set[str] = set()
    raw = str(text or "")
    lowered = raw.lower()

    for token in LATIN_TOKEN_RE.findall(lowered):
        if len(token) < latin_min_len or token in stopwords:
            continue
        terms.add(aliases.get(token, token))

    for run in CJK_RUN_RE.findall(raw):
        for phrase, canonical in aliases.items():
            if CJK_RUN_RE.search(phrase) and phrase in run:
                terms.add(canonical)
        for gram in cjk_ngrams(run):
            if gram in stopwords:
                continue
            terms.add(aliases.get(gram, gram))

    return terms
