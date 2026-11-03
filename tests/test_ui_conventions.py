"""UI conventions that a screenshot cannot catch.

These read the stylesheet as text, which is the right level for rules that are
about *tokens* rather than rendering: whether a colour is used for the job its
contrast was measured for, and whether an interactive element can be seen at all
by somebody who is not using a mouse.

The alternative -- rendering each element and measuring pixels -- is what found
the bug below, and it is the wrong shape for a guard: it needs a browser, a
server and a fixture per rule, so it would run rarely and rot. A source rule runs
with the rest of the suite and fails on the line that introduced the problem.
"""

from __future__ import annotations

from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "frontend" / "src" / "styles"


def _rules_mentioning(needle: str) -> list[tuple[str, str]]:
    """(file, line) for every line of CSS that mentions *needle*."""
    hits: list[tuple[str, str]] = []
    for path in sorted(STYLES.glob("*.css")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if needle in line:
                hits.append((f"{path.name}:{number}", line.strip()))
    return hits


def test_no_focus_ring_is_drawn_with_the_border_token() -> None:
    """The focus ring must not use ``--primary-border``.

    Measured on the rendered page: ``--primary-border`` is 26% white, and as a
    focus indicator it composites to 2.36:1 against the page -- under the 3:1
    WCAG requires for non-text controls. It is a fine *border*; it is an invisible
    *focus ring*, and the bug is invisible precisely to the people who do not use
    a mouse.

    The replacement, ``--focus-ring``, measures 11.35:1 in the dark theme. This
    test exists so the faint one cannot come back one rule at a time.
    """
    offenders = [
        (where, line)
        for where, line in _rules_mentioning("var(--primary-border)")
        if "outline" in line or "stroke" in line
    ]

    assert not offenders, (
        "these rules draw a focus indicator with the border token, which measures "
        f"2.36:1 against the page (minimum is 3:1): {offenders}"
    )


def test_the_focus_token_exists_in_both_themes() -> None:
    """A token defined only for dark mode leaves one theme with no focus ring."""
    tokens = (STYLES / "01-theme-tokens.css").read_text()

    assert tokens.count("--focus-ring:") == 2, (
        "every theme block must define --focus-ring"
    )
