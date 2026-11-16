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
VIEWS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "views"


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


def test_the_checked_switch_does_not_paint_a_white_knob_on_a_light_track() -> None:
    """The knob has to differ from the track it sits on.

    ``--primary`` is near-white in the dark theme, so antd's checked switch is a
    light track -- and antd draws the knob white in both themes. Measured on the
    rendered page: 1.72:1 between knob and track. An enabled automation then read
    as an empty pale pill, which is the one thing a switch exists to answer.

    The dark theme therefore repaints the knob with the page colour (10.97:1 on
    the same track). This guard is *about that override existing*, because the
    visible symptom is a colour, not a missing element.
    """
    tokens = (STYLES / "01-theme-tokens.css").read_text()

    assert "ant-switch-checked .ant-switch-handle::before" in tokens, (
        "the dark theme must repaint the checked switch's knob; antd's white knob "
        "on the near-white track measures 1.72:1"
    )


def test_the_automation_status_is_not_an_antd_tag() -> None:
    """执行成功 and 执行失败 must not be rendered as antd ``Tag``s.

    10-secondary-glass.css paints every ``.ant-tag`` with the monochrome palette
    *and marks the declaration important*, which is how 执行失败 and 执行成功
    ended up as the same grey word in the same grey pill. The first fix was to
    out-rank it with ``!important``; the row now renders the status as a plain
    ``span`` whose colour comes from the shared ``.status-*`` rules, so the global
    override has nothing to win. This guard is about that shape: putting the
    ``Tag`` back would silently reintroduce the bug, because the override it
    loses to is two files away.
    """
    automation = (VIEWS / "automation.tsx").read_text()
    assert "schedule-status-tag" in automation, "the status class should still exist"
    for line in automation.splitlines():
        if "schedule-status-tag" not in line:
            continue
        assert "<Tag" not in line and "</Tag>" not in line, (
            "the status must stay a span: as a `.ant-tag` it is flattened to the "
            f"global grey by an !important rule elsewhere -- {line.strip()}"
        )




def test_buttocks_use_the_project_focus_ring_not_antds_double_border() -> None:
    """antd's own focus ring reads as a second border on a solid primary.

    Measured on the memory toolbar: `outline: 3px solid rgb(82,82,83)` at a 1px
    offset around the dark 新增记忆 pill, left on screen after a *mouse* click
    (antd hands focus back to a modal's trigger, and Chromium counts that as
    :focus-visible). Every hand-written control here rings at 2px of
    --focus-ring with a 2px offset, so the antd ones follow the same rule.
    """
    offenders = [
        (where, line)
        for where, line in _rules_mentioning(".ant-btn:focus-visible")
    ]

    assert offenders, "the antd focus ring must be overridden, not inherited"

    text = (STYLES / "15-shell.css").read_text()
    keyboard = text[text.index("html[data-input='keyboard'] .ant-btn:focus-visible") :][:400]
    assert "2px solid var(--focus-ring)" in keyboard, keyboard
    assert "outline-offset: 2px" in keyboard, keyboard
    # Only the keyboard draws it: a pointer click that leaves focus behind (antd
    # does this when a modal closes) must not leave a ring on a solid button.
    pointer = text[text.index("html[data-input='pointer'] .ant-btn:focus-visible") :][:400]
    assert "outline: none" in pointer, pointer


def test_the_delivery_choice_is_offered_only_while_a_turn_is_running() -> None:
    """插话 / 等回复后 differ only mid-turn; idle, both start a turn at once.

    A control offering a distinction that does not apply teaches nothing, so the
    chip is gated on `isStreaming`. This guard is about that gate existing.
    """
    chat = (VIEWS / "chat.tsx").read_text()
    assert "deliver-mode-button" in chat, "the mode chip should exist"
    gate = chat.index("{isStreaming && (")
    chip = chat.index("deliver-mode-button")
    assert gate < chip, "the chip must sit inside the isStreaming gate"


def test_a_deferred_message_is_not_labelled_as_an_interjection() -> None:
    """The two mailboxes are different promises and the row must say which.

    `isInTurnUserMessage` is true for both (queued OR interjection), so the row
    used to print 插话 for a message the person had asked to hold until the
    answer finished -- claiming it was interrupting the answer it was waiting
    for.
    """
    rows = (STYLES.parent / "components" / "MessageList.tsx").read_text()
    assert "const deferred = !item.interjection" in rows, (
        "the row must distinguish a deferred message from an interjection"
    )
    assert "{deferred ? '排队' : '插话'}" in rows
    assert "{deferred ? '等本轮结束' : '待读'}" in rows


def test_the_composer_cannot_be_stretched_by_its_own_footer() -> None:
    """Measured at 375px: the mode chip grew `.composer` from 355px to 465px.

    `.composer-wrap` is a grid, so its track was sized by the widest child's
    max-content -- the composer grew to fit its footer and pushed the send
    button off-screen. `min-width: 0` hands sizing back to the container.
    """
    composer = (STYLES / "04-composer.css").read_text()
    # Wide enough to span the block's own comment: the note explaining the
    # measurement sits above the declarations it justifies.
    block = composer[composer.index(".composer {") :][:700]
    assert "min-width: 0" in block, block
    assert "max-width: 100%" in block, block
    # And the chip collapses like the workspace/permission chips already do.
    narrow = composer[composer.index("@media (max-width: 640px)") :][:400]
    assert ".deliver-mode-button.ant-btn" in narrow
    assert ".deliver-mode-label { display: none; }" in narrow
