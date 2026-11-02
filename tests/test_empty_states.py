"""Every empty state names the next step.

An empty list is the one place where a reader has nothing to act on, so the sentence
there is the whole page. Two of the four pages said "还没有插件" / "还没有会话" and
stopped -- reporting emptiness and leaving the reader to guess how the list ever
fills -- while the memory and automation pages said what to do about it.

This is a source-level check rather than a browser one, on purpose: the empty state
only renders on a gateway that has nothing, and a browser test would have to fight
the data the other tests create. What the rule is about -- the *text* of every
`page-empty` -- is in the source, so that is where it is checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "views"

#: A true empty state starts with this. A "no match" state ("没有匹配的会话") is a
#: different sentence: the list is not empty, the search is too narrow.
EMPTY_PREFIX = "还没有"

#: What counts as naming the next step: the ways a list actually fills on this page.
#:
#: "上面那个框" is on the list because the home page's empty state is *about* the
#: composer above it -- the list has no button of its own, and the box is the only
#: way in. A vocabulary that only accepts buttons would call that state unhelpful.
GUIDANCE = ("对话", "新建", "右上角", "agent", "在左边", "上面")


def _empty_descriptions(source: str) -> list[str]:
    """Every literal string passed as ``description`` to an ``Empty``."""
    found: list[str] = []
    # `description="..."` on one line, and `description={`...`}` / `'...'` blocks.
    for match in re.finditer(r"description=(\"[^\"]*\"|`[^`]*`|'[^']*')", source, flags=re.S):
        found.append(match.group(1)[1:-1])
    return found


def test_every_true_empty_state_says_what_happens_next() -> None:
    offenders: list[str] = []
    checked = 0
    for path in sorted(VIEWS.glob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        if "page-empty" not in source:
            continue
        for text in _empty_descriptions(source):
            if not text.strip().startswith(EMPTY_PREFIX):
                continue  # a "no match" state, not an empty one
            checked += 1
            if not any(word in text for word in GUIDANCE):
                offenders.append(f"{path.name}: {text.strip()}")

    assert checked >= 3, f"only {checked} empty states found; the scan has drifted"
    assert not offenders, (
        "these empty states report emptiness without saying what to do next:\n  "
        + "\n  ".join(offenders)
    )
