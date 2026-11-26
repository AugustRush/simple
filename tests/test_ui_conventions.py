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


def test_the_theme_is_written_where_the_stylesheets_look() -> None:
    """Every theme rule selects `html[data-theme]`; the entry point wrote `#root`.

    So the first paint applied nothing: a light-theme reader got one frame of the
    dark `:root` tokens on every reload, then React's first effect flipped it.
    Measured by reading the selectors and the entry point, because the symptom is
    a single frame nobody can catch reliably in a screenshot.
    """
    entry = (STYLES.parent / "main.tsx").read_text()
    theme_module = (STYLES.parent / "lib" / "theme.ts").read_text()

    assert "#root" not in entry.split("ReactDOM")[0], (
        "the entry point must not theme #root: no rule selects on it"
    )
    # `applyTheme` writes it on `document.documentElement`; the assertion is on
    # the element the rules select, not on one particular spelling of it.
    assert "document.documentElement" in theme_module
    assert "html.dataset.theme = theme" in theme_module
    # And the selectors really are html-scoped, so the fix is aimed at the right
    # element rather than at a convention that changed.
    scoped = sum(
        line.count("html[data-theme")
        for path in STYLES.glob("*.css")
        for line in path.read_text().splitlines()
    )
    assert scoped >= 10, scoped


def test_theme_has_three_modes_and_a_switch_that_cannot_flash() -> None:
    """`system` is a rule for choosing a theme, not a third theme."""
    theme_module = (STYLES.parent / "lib" / "theme.ts").read_text()

    assert "'light' | 'dark' | 'system'" in theme_module
    assert "prefers-color-scheme: dark" in theme_module
    assert "watchSystemTheme" in theme_module
    # The flash fix: transitions are suppressed for the frame that changes them.
    assert "theme-switching" in theme_module
    tokens = (STYLES / "01-theme-tokens.css").read_text()
    assert "html.theme-switching" in tokens


def test_every_control_height_comes_from_the_scale() -> None:
    """Nine heights (42/40/38/34/32/30/28/25/22) for five jobs, decided by
    whichever partial won the cascade.

    A control measured its own way is how a row of button + input + select ends
    up three different heights, and how the same control changes height when a
    later stylesheet loads. The scale lives in 01-theme-tokens.css; every other
    partial names a step.
    """
    import re

    tokens = (STYLES / "01-theme-tokens.css").read_text(encoding="utf-8")
    declared = {
        name: int(value)
        for name, value in re.findall(r"(--control-[\w-]+):\s*(\d+)px", tokens)
    }
    assert declared == {
        "--control-lg": 44,
        "--control-md": 40,
        "--control-sm": 36,
        "--control-xs": 32,
        "--control-2xs": 28,
    }, declared

    control_markers = (
        ".ant-btn",
        ".ant-input",
        ".ant-select",
        ".ant-menu-item",
        ".send-button",
        ".segmented-control",
        ".workspace-search",
        ".deliver-mode-button",
        ".permission-button",
        ".workspace-picker",
        ".brand-action",
        ".header-menu",
    )
    offenders: list[str] = []
    for path in sorted(STYLES.glob("*.css")):
        if path.name == "01-theme-tokens.css":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
            selector = " ".join(match.group(1).split())
            if not any(marker in selector for marker in control_markers):
                continue
            if ".workspace-status-dot" in selector:
                continue  # a dot, not a control
            if ".composer" in selector and ".ant-input" in selector:
                # The composer's textarea. Its min-height is the *empty* height
                # and it grows with what is typed -- not a control size, and
                # pinning it to the scale would stop it growing.
                continue
            body = match.group(2)
            literal = re.search(r"(?<!-)(min-)?height:\s*(\d+)px", body)
            if literal:
                line = text[: match.start()].count("\n") + 1
                offenders.append(
                    f"{path.name}:{line} {selector[-48:]} = {literal.group(2)}px"
                )

    assert not offenders, "control heights outside the scale:\n  " + "\n  ".join(offenders)


def test_the_antd_scale_is_the_same_scale() -> None:
    """A control's height must not depend on whether it happens to be an antd one.

    `controlHeight` alone left SM and LG at antd's defaults, so a compact button
    beside a token-sized one disagreed by a step.
    """
    import re

    app = (STYLES.parent / "App.tsx").read_text(encoding="utf-8")

    assert re.search(r"controlHeight:\s*40\b", app)
    assert re.search(r"controlHeightSM:\s*32\b", app)
    assert re.search(r"controlHeightLG:\s*44\b", app)


def test_settings_is_one_page_not_tabs() -> None:
    """Three tabs of 451 / 924 / 744px were three places to re-orient.

    Every switch changed what was on screen, so finding a setting meant
    remembering which tab held it and then scrolling to a different offset. One
    page means one scroll and one reading order; the escape hatch at the bottom
    is collapsed instead of occupying a third of the page.
    """
    settings = (STYLES.parent / "views" / "settings.tsx").read_text(encoding="utf-8")

    assert "settings-tabs" not in settings, "the tab strip is back"
    assert "settingsTab" not in settings, "a panel switch is back"
    assert settings.count('className="settings-grid"') == 1
    # The JSON escape hatch is collapsed, not deleted: its textarea stays
    # mounted so a half-typed edit survives closing the panel.
    assert "Collapse" in settings
    assert "defaultActiveKey" in settings


def test_capabilities_is_a_destination_and_theme_is_not_a_submenu() -> None:
    """Both used to be two clicks deep. 能力 is a view of its own (its two lists
    are taller than every settings panel combined); 主题's three answers are a
    group, because hiding three items made the most-changed setting in the app
    a detour."""
    app = (STYLES.parent / "App.tsx").read_text(encoding="utf-8")
    route = (STYLES.parent / "lib" / "route.ts").read_text(encoding="utf-8")
    shell = (STYLES.parent / "hooks" / "useShell.tsx").read_text(encoding="utf-8")

    assert "'capabilities'" in route, "能力 is not a view"
    assert "view === 'capabilities'" in app
    # The menu entry comes from `navItems`, not written twice in the menu
    # literal -- which is what made the first attempt render 能力 twice.
    assert "{ key: 'capabilities'" in shell
    assert app.count("key: 'capabilities'") == 0
    assert "type: 'group'" in app
    assert "label: '主题'" in app


def test_the_save_bar_covers_what_scrolls_under_it() -> None:
    """A sticky bar that only covers its own width reads as a rendering glitch.

    antd's `Affix` writes the wrapper's measured width **inline** on the fixed
    child (`width: 305.98px`), so the opaque background covered the buttons only
    and the provider rows showed through either side. An inline declaration is
    outranked by exactly one thing, which is why this rule is `!important`.
    """
    polish = (STYLES / "16-polish.css").read_text(encoding="utf-8")
    section = polish.split(".settings-affix .ant-affix")[1][:400]

    assert "width: auto !important" in section
    assert "background: var(--bg)" in section
    # The wrapper itself must not be restyled: it holds the fixed child, and
    # `display: block` on it collapsed the bar entirely (measured: top -1497).
    wrapper = polish.split(".settings-affix {")[1][:160] if ".settings-affix {" in polish else ""
    assert "display: block" not in wrapper


def test_every_settings_row_uses_one_geometry() -> None:
    """The 模型与频道 card held three row designs at once, and they ended their
    controls at different x positions (measured: switches at 701 and 1165, the
    select at 899, the button at 1097). Three shapes in one card is what "no
    consistency" looks like to a reader.

    Two class names remain (`settings-pref` from the older rows, `settings-row`
    from the channel block) because rewriting four rows of markup is a bigger
    change than the alignment is worth -- so the guard is that *both* are given
    the same geometry: same gap, same control column, same hairline.
    """
    polish = (STYLES / "16-polish.css").read_text(encoding="utf-8")

    # Both row classes get the same gap and the same control column.
    for cls in (".settings-row {", ".settings-pref {"):
        block = polish.split(cls)[1][:400]
        assert "gap: 24px" in block, f"{cls} has its own gap"
        assert "min-height: 56px" in block, f"{cls} has its own row height"

    # The control column is what makes the right edges agree.
    assert polish.count("min-width: 320px") >= 2
    # ...and the divider is the same token in both, not --border in one and
    # --border-subtle in the other.
    assert polish.count("border-top: 1px solid var(--border-subtle)") >= 3


def test_a_settings_card_insets_its_content_once() -> None:
    """The provider rows sat at 261..1179 inside a card body of 241..1199, so
    the list was inset an extra 20px and stopped short of everything above it.
    Every card's content now starts and ends on the body's own edges."""
    polish = (STYLES / "16-polish.css").read_text(encoding="utf-8")
    # Anchored on the selector, not on the prose: the comment above it contains
    # an apostrophe and a typographic quote, and a guard that depends on the
    # wording fails when the wording improves.
    # Both rules that target a provider row inside a card body.
    rules = [
        block for block in polish.split("}") if ".provider-row" in block
    ]
    assert any("margin-inline: 0" in block for block in rules)
    assert any("border-color: var(--border-subtle)" in block for block in rules)
    assert any("padding: 12px 14px" in block for block in rules)


def test_a_settings_row_stacks_below_the_two_column_width() -> None:
    """The control column is 320px. At 390px the row is 316px wide, so the fixed
    column was wider than its row: the text collapsed to zero and the control
    only stayed visible because flex happened to wrap it. Between 640 and 900px
    it breaks visibly -- wide enough not to wrap, too narrow for both columns.
    """
    polish = (STYLES / "16-polish.css").read_text(encoding="utf-8")

    assert "@media (max-width: 720px)" in polish
    section = polish.split("@media (max-width: 720px)")[1][:600]
    assert ".settings-pref," in section and ".settings-row {" in section
    assert "flex-direction: column" in section
    assert "min-width: 0" in section, "the fixed control column was not released"


def test_the_composer_is_wider_than_the_transcript() -> None:
    """The box you type in is wider than the column you read, on purpose: it is
    the one thing on the page you put something *into*, and a box that frames
    the conversation reads as the place to type.

    Measured: 900 vs 820, so it overhangs 40px on each side. The cap lives on
    `.composer-stage`, not `.composer` -- setting `.composer { width }` changed
    nothing, because the grid track was already 820.
    """
    polish = (STYLES / "16-polish.css").read_text(encoding="utf-8")

    assert "--chat-column: 820px" in polish
    assert "--chat-column-wide: 900px" in polish
    section = polish.split(".composer-stage {")[1][:300]
    assert "width: min(var(--chat-column-wide), 100%)" in section
    # The notices stay on the narrow column: a notice as wide as the box reads
    # as part of the box.
    assert ".message-queue-banner," in section


def test_the_board_is_a_page_and_the_bar_carries_the_glance() -> None:
    """The board was a 400px drawer over the conversation: it covered the
    composer, and its rows had to be shortened to fit one narrow column.

    A page gives it the reading width every other page has. What the bar keeps
    is the glance -- how much is running, waiting, queued -- because most of the
    time the question is a look, not a visit.
    """
    route = (STYLES.parent / "lib" / "route.ts").read_text(encoding="utf-8")
    app = (STYLES.parent / "App.tsx").read_text(encoding="utf-8")
    panel = (STYLES.parent / "views" / "status_panel.tsx").read_text(encoding="utf-8")

    assert "'board'" in route, "看板 is not a view"
    assert "view === 'board'" in app
    assert "renderStatusPanel" in app
    # No drawer left: a panel that covers the composer is not where you read a
    # list.
    assert "board-drawer" not in app
    assert "Drawer" not in app.split("from 'antd'")[0]
    # Idle is a state, not an absence -- a strip that vanishes reads as broken.
    assert "is-idle" in panel
