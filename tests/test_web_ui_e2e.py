"""The page itself, in a real browser -- the one surface no other test touches.

Why this file is separate, and why it skips
-------------------------------------------
Everything else in this suite drives a protocol: the functional layer opens the real
WebSocket and the route contract checks that every endpoint the page calls exists.
Neither can tell you that the page **runs**: that React mounted, that the bundle's
imports resolved, that a click reaches a handler, that the reply appears in the DOM
rather than only on the wire.  That needs a browser, and a browser is a dependency the
core install does not carry.

So: ``playwright`` is an optional extra.  Without it these tests skip, which is the
honest outcome -- a skipped test is visible in the summary, whereas a test that
pretends to check the page while checking an HTTP response is invisible.  To run them::

    uv pip install --python .venv/bin/python playwright
    .venv/bin/python -m playwright install chromium
    .venv/bin/python -m pytest tests/test_web_ui_e2e.py

What is real here
-----------------
The gateway is a **subprocess** launched the way a person launches it
(``python -m agent.cli gateway``), serving the built bundle from
``agent/_builtin/web/dist``.  The provider is the same scripted OpenAI-compatible
server the functional layer uses, so the answer is known in advance and the assertion is
about the page, not about a model.  Playwright drives a headless Chromium.

What is asserted, and why each one is worth the dependency
----------------------------------------------------------
* **No JavaScript errors on load.**  A bundle that throws while mounting renders a
  blank page and answers every request correctly -- which is exactly the failure the
  protocol-level tests cannot see.
* **The answer reaches the DOM.**  The functional layer proves the WebSocket carries
  it; this proves a person would read it.
* **A second route renders.**  Rendering is per-view: a broken import in one view is
  invisible from the start page.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is the optional 'e2e' extra; see this module's docstring",
)

from test_functional_turns import scripted, write_config  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: How long the gateway may take to compose and bind.  Generous on purpose: a cold
#: import of this project is seconds, and a flaky startup would teach people to ignore
#: this test.
STARTUP_TIMEOUT_SECONDS = 90


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_healthy(base_url: str) -> None:
    """Poll /api/health until the gateway answers, or fail with its output."""
    deadline = time.time() + STARTUP_TIMEOUT_SECONDS
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as response:
                if response.status == 200:
                    json.loads(response.read().decode("utf-8"))
                    return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise AssertionError(
        f"gateway did not answer {base_url}/api/health within "
        f"{STARTUP_TIMEOUT_SECONDS}s (last error: {last_error})"
    )


@pytest.fixture(scope="module")
def live_gateway(tmp_path_factory):
    """A real `agent.cli gateway` process (a real gateway, on a real port).

    Module-scoped because starting one costs a composition (~2-4s) and every test here
    is about the *served page*, which is identical for all of them.
    """
    home = tmp_path_factory.mktemp("e2e-home")
    port = _free_port()
    provider = scripted({"text": "pong from the browser"})
    try:
        cfg = write_config(
            home,
            provider,
            channels={"web": {"enabled": True, "host": "127.0.0.1", "port": port}},
        )
        # `write_config` writes the config where the CLI reads it; the subprocess
        # re-reads it from disk, which is what a person's gateway does.
        env = dict(os.environ)
        env["SIMPLE_AGENT_HOME"] = str(home)
        env["SIMPLE_NO_BROWSER"] = "1"
        env.pop("BROWSER", None)
        process = subprocess.Popen(
            [sys.executable, "-m", "agent.cli", "gateway"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_until_healthy(base_url)
        except BaseException:
            process.terminate()
            out, _ = process.communicate(timeout=20)
            raise AssertionError(f"gateway failed to start. Output:\n{out}")
        yield {"base_url": base_url, "home": home}
    finally:
        provider.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture()
def page(browser, live_gateway):
    """A fresh page per test, with console errors collected from the first paint."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on(
        "console",
        lambda message: (
            errors.append(f"{message.type}: {message.text}")
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.errors = errors  # type: ignore[attr-defined]
    try:
        page.goto(live_gateway["base_url"], wait_until="networkidle")
        yield page
    finally:
        context.close()



def open_page(page, label: str) -> None:
    """Go to a page the way a person does: the bar if it is on the bar, else ⋯.

    The sidebar is gone. 线程 / 自动化 / 看板 are on the bar because they are the
    pages somebody moves between while working; everything else is opened from
    the menu. A test that only knew one of the two would pass while the entry it
    was supposed to be testing had moved.
    """
    on_bar = page.locator(".header-right .ant-btn", has_text=label)
    if on_bar.count():
        on_bar.first.click()
        return
    page.locator(".topbar-more").click()
    page.wait_for_selector(".ant-dropdown-menu-item", timeout=10_000)
    page.locator(".ant-dropdown-menu-item", has_text=label).first.click()


def test_the_shell_renders_without_javascript_errors(page):
    """A bundle that throws while mounting still answers every request correctly.

    This is the failure the protocol tests structurally cannot see, and the reason the
    browser is worth its download: ``goto`` succeeds either way, so the assertion is
    that the *page* came up -- a navigation label, the composer -- and that nothing
    logged an error while it did.
    """
    page.wait_for_selector("textarea", timeout=20_000)
    # The shell: a bar with the agent's name on it, and the pages behind ⋯.
    assert page.locator(".topbar-brand").is_visible()
    assert page.locator(".topbar-title").inner_text().strip()
    assert page.locator(".topbar-more").is_visible()
    page.locator(".topbar-more").click()
    page.wait_for_selector(".ant-dropdown-menu-item", timeout=10_000)
    assert page.locator(".ant-dropdown-menu-item", has_text="记忆").count() == 1
    assert page.locator(".ant-dropdown-menu-item", has_text="设置").count() == 1
    # 扩展 and 自主 were pages here; both were content another screen already
    # owns (设置 and 看板). A destination that is not a place you go is what made
    # this app feel bigger than it is.
    assert page.locator(".ant-dropdown-menu-item", has_text="扩展").count() == 0
    assert page.locator(".ant-dropdown-menu-item", has_text="自主").count() == 0
    page.keyboard.press("Escape")
    assert page.errors == [], page.errors


def test_a_message_is_answered_on_the_page(page):
    """The functional layer proves the socket carries the reply; this proves it lands."""
    composer = page.wait_for_selector("textarea", timeout=20_000)
    composer.fill("this is a browser message")
    composer.press("Enter")

    page.wait_for_selector("text=pong from the browser", timeout=30_000)
    assert "this is a browser message" in page.inner_text("body")
    assert page.errors == [], page.errors


def test_the_capability_lists_live_on_the_settings_page(page):
    """扩展 was a page; its two lists are a card on 设置 now.

    That is the fold this change makes: a page whose whole content is switches
    and deletes is configuration, and configuration already had a page. The
    assertion is not "it moved" -- it is that both lists are still reachable and
    still switchable, because a fold that loses the controls is a deletion with
    better manners.
    """
    page.goto(page.url.split("#")[0] + "#/settings", wait_until="networkidle")
    page.wait_for_selector(".capabilities-card", timeout=20_000)

    tabs = page.locator(".capabilities-card .extensions-tabs button")
    assert tabs.count() == 2
    assert "插件" in tabs.nth(0).inner_text() and "技能" in tabs.nth(1).inner_text()

    # Both tabs render something: the count on the tab is the list's whole size,
    # so a tab with a number and an empty body would be the panel lying.
    assert page.locator(".capabilities-card .entity-card, .capabilities-card .page-empty").count() >= 1
    tabs.nth(1).click()
    page.wait_for_timeout(400)
    assert tabs.nth(1).get_attribute("aria-selected") == "true"
    assert page.locator(".capabilities-card .skill-row, .capabilities-card .page-empty").count() >= 1

    assert page.errors == [], page.errors


def test_a_second_route_renders_its_own_view(page):
    """Rendering is per-view, so one broken import is invisible from the start page."""
    page.goto(page.url.split("#")[0] + "#/memory", wait_until="networkidle")
    page.wait_for_selector(".memory-tabs", timeout=20_000)

    assert page.locator(".memory-tabs").first.is_visible()
    assert page.errors == [], page.errors


def test_the_automation_page_is_one_list(page, live_gateway):
    """The merge, seen by a person: one page, one list, two tabs.

    A task is created through the API rather than the form -- this test is about the
    *list*, and driving the form would test the form. A fresh gateway has nothing, so
    the empty state is asserted first, then a row is created and found.
    """
    import json
    import urllib.request

    open_page(page, "自动化")
    page.wait_for_selector(".schedule-tabs", timeout=20_000)

    # Two tabs, not three: what needs a person, and everything that exists.
    tabs = page.locator(".schedule-tabs [role=tab]")
    assert tabs.count() == 2, [tabs.nth(i).inner_text() for i in range(tabs.count())]
    assert "自动化" in tabs.nth(1).inner_text()
    # ...and the landing list says what it is when there is nothing in it.
    page.wait_for_selector("text=还没有自动化", timeout=20_000)

    # Create one task through the API the page itself uses.
    # The same body the task form builds (`scheduleRequestBody` in the frontend):
    # action_type names the payload shape, the trigger fields say when.
    payload = {
        "name": "每天早上看看日志",
        "action_type": "agent_task",
        "trigger_type": "interval",
        "every": 24,
        "unit": "hours",
        # An interval counts from an anchor; the form sends one for the same reason.
        "anchor_at": "2026-11-01T00:00:00+08:00",
        "prompt": "读一遍日志，告诉我有没有异常",
        "workspace_root": str(live_gateway["home"]),
        "context_policy": "stateless",
        "delivery_mode": "standalone",
        "timeout_seconds": 1800,
        "retry_policy": {"max_attempts": 1, "backoff_seconds": 30},
        "selected_skills": [],
        "permission_profile": "inherit",
        "timezone_name": "Asia/Shanghai",
    }
    request = urllib.request.Request(
        f"{live_gateway['base_url']}/api/schedules",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status in (200, 201), response.status
    except urllib.error.HTTPError as exc:  # the body names the field it refused
        raise AssertionError(
            f"creating a task through the API failed: {exc.code} {exc.read().decode()}"
        ) from exc

    page.reload(wait_until="networkidle")
    page.wait_for_selector("text=每天早上看看日志", timeout=20_000)
    # The row says which shape it is, from the unified payload.
    assert page.locator(".schedule-list").first.inner_text().count("每天早上看看日志") == 1
    assert page.errors == [], page.errors


def test_editing_an_automation_is_a_conversation_too(page, live_gateway):
    """A row's edit action opens a conversation seeded with *this* automation.

    The id travels with the sentence: "the task called 每天早上看看日志" stops being a
    unique description the moment there are two, and the agent has to name the row it is
    about to change. Seeded with a template rather than a filled-in request, because the
    person is the one who knows what should change.
    """
    import json
    import urllib.request

    # One automation, created through the API the page itself uses.
    payload = {
        "name": "每晚复盘一下",
        "action_type": "message",
        "message_text": "晚上好",
        "trigger_type": "interval",
        "every": 24,
        "unit": "hours",
        "anchor_at": "2026-11-01T00:00:00+08:00",
        "workspace_root": str(live_gateway["home"]),
        "context_policy": "stateless",
        "delivery_mode": "standalone",
        "timeout_seconds": 1800,
        "retry_policy": {"max_attempts": 1, "backoff_seconds": 30},
        "selected_skills": [],
        "permission_profile": "inherit",
        "timezone_name": "Asia/Shanghai",
    }
    request = urllib.request.Request(
        f"{live_gateway['base_url']}/api/schedules",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        assert response.status in (200, 201), response.status
        created = json.loads(response.read().decode("utf-8"))
    task_id = created["task"]["id"]

    open_page(page, "自动化")
    page.wait_for_selector("text=每晚复盘一下", timeout=20_000)

    page.get_by_role("button", name="在对话里修改 每晚复盘一下").first.click()

    composer = page.wait_for_selector("textarea", timeout=20_000)
    value = composer.input_value()
    assert "每晚复盘一下" in value, value
    assert task_id in value, f"the row's id has to travel: {value!r}"
    assert page.errors == [], page.errors


def test_creating_an_automation_is_a_conversation(page):
    """The create entry is a new session with the first sentence in the box.

    A form asked a person to know the tool's vocabulary -- trigger types, permission
    profiles, whether their request was "one task" or "a chain". The agent knows all of
    that and can ask one question at a time, so the entry opens a conversation and seeds
    the composer with the three things it would ask anyway.
    """
    open_page(page, "自动化")
    page.wait_for_selector(".schedule-tabs", timeout=20_000)

    # One entry, and it says what it does.
    create = page.locator(".schedule-page-head button:has-text('对话新建')")
    assert create.count() == 1, create.count()
    create.first.click()

    # ...a new conversation, on the chat view, with the seed text editable in the box.
    composer = page.wait_for_selector("textarea", timeout=20_000)
    value = composer.input_value()
    assert "帮我建一个自动化任务" in value, value
    assert "什么时候做" in value, value

    assert page.errors == [], page.errors


def test_the_board_shows_what_it_would_do(page, live_gateway):
    """The panel that answers "does anything need me", proposals included.

    Seeded through the store the patrol writes, because the patrol itself runs on
    a ten-minute clock -- waiting for it would make this a slow test of the
    scheduler rather than a test of the screen.

    The proposals used to have a page of their own (自主) that drew the same rows
    this panel draws, one door deeper. What the decision needs is on the card: the
    action, what was observed, and how bad it would be. What the *page* carried --
    which actions it takes on its own -- is the one sentence under the queue.
    """
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

    from agent.autonomy import Proposal
    from agent.autonomy.store import open_proposal_store

    # The gateway's home, so the panel reads the store the test wrote.
    os.environ["SIMPLE_AGENT_HOME"] = str(live_gateway["home"])
    from agent import shared

    shared.AGENT_HOME = live_gateway["home"]
    store = open_proposal_store()
    try:
        for action, tool, reversibility, confidence in (
            ("读一遍失败的运行", "schedule_runs", "reversible", 0.8),
            ("检查卡住的队列", "send_file", "irreversible", 0.55),
        ):
            proposal = Proposal(
                source="e2e-session",
                reason="有一个事实值得看一眼",
                action=action,
                tool=tool,
                reversibility=reversibility,
                confidence=confidence,
            )
            proposal.context["admission"] = {
                "answer": "prepare",
                "reason": "今天自动执行的额度已用完",
                "prepared": {"tool": tool, "arguments": {}},
            }
            store.record(proposal)
    finally:
        store.close()

    page.goto(page.url.split("#")[0], wait_until="networkidle")
    # The 看板 button, and not a route: there is no URL for this panel, because it
    # is a control you open on whatever screen you are on.
    page.locator(".header-board").click()
    page.wait_for_selector(".panel-card-proposal", timeout=20_000)

    # By text, not by position: another test in this module seeds its own
    # proposal into the same gateway home.
    readable = page.locator(".panel-card-proposal").filter(has_text="读一遍失败的运行")
    assert readable.count() == 1, "the proposal this test seeded is not on the panel"
    assert page.locator(".panel-card-proposal").filter(has_text="检查卡住的队列").count() == 1
    assert "有一个事实值得看一眼" in readable.first.inner_text(), (
        "what was observed is what a decision is made on"
    )
    panel = page.inner_text(".panel-body")
    # The reversibility class, because it is the reason the row is here at all.
    assert "只读" in panel and "不可逆" in panel, "the reversibility class must be visible"
    # The one rule the panel exists to state. It used to promise "0 次自动执行";
    # the loop acts now, so what it has to state instead is *which* actions it
    # takes on its own -- and that the rest is never taken.
    assert "只读的动作它自己做" in panel
    assert "永远不会自己做" in panel, "the irreversible line is the promise that remains"
    # Answering on the card is the next test's subject; this one is about the
    # panel saying what is waiting without a second screen.
    assert page.errors == [], page.errors


def test_the_app_opens_the_one_conversation(page):
    """One conversation, and it is already open.

    The app used to open on a welcome screen and turn the first message into a
    new thread. That shape belongs to a chat product: it asks which container a
    sentence goes into. This one has a conversation -- the install remembers it
    and hands it back at boot -- so `#/` *is* that conversation, the address
    names it, and nothing stands in front of it.
    """
    page.goto(page.url.split("#")[0], wait_until="networkidle")
    page.wait_for_selector(".home-room", timeout=20_000)

    assert page.locator(".home-view").count() == 0, (
        "the landing page -- a box with a new-conversation click behind it -- is gone"
    )
    assert "#/t/" in page.url, f"the address names the open thread: {page.url}"
    assert page.locator(".composer-wrap textarea").count() == 1
    # What it did on its own is one line above the conversation, closed by
    # default: while you are talking, that is background.
    strip = page.locator(".self-activity-head")
    assert strip.count() == 1
    assert strip.get_attribute("aria-expanded") == "false"

    # Navigation is one bar plus a menu, not a column.
    assert page.locator(".app-sider").count() == 0, "the sidebar is gone"
    bar = [
        page.locator(".header-right .ant-btn").nth(i).inner_text().strip()
        for i in range(page.locator(".header-right .ant-btn").count())
    ]
    assert {"线程", "自动化", "看板"} <= set(bar), bar
    page.locator(".topbar-more").click()
    page.wait_for_selector(".ant-dropdown-menu-item", timeout=10_000)
    labels = [
        page.locator(".ant-dropdown-menu-item").nth(i).inner_text().strip()
        for i in range(page.locator(".ant-dropdown-menu-item").count())
    ]
    assert "记忆" in labels and "设置" in labels, labels
    assert "会话管理" not in labels, labels
    assert "对话" not in labels, labels
    assert "自动化" not in labels and "线程" not in labels, labels
    page.keyboard.press("Escape")
    assert page.errors == [], page.errors


def test_typing_sends_into_the_same_conversation(page):
    """A message is not a new conversation -- it is the next turn of this one.

    The old flow made the first message *create* a thread, which is why every
    screen had a "new conversation" path. Here the conversation predates the
    first message, so sending must leave the identity alone.
    """
    page.goto(page.url.split("#")[0], wait_until="networkidle")
    page.wait_for_selector(".home-room", timeout=20_000)
    before = page.url

    box = page.wait_for_selector(".composer-wrap textarea", timeout=20_000)
    box.fill("帮我看看昨天的构建")
    box.press("Enter")
    page.wait_for_selector("text=帮我看看昨天的构建", timeout=30_000)
    page.wait_for_timeout(1200)

    assert page.url == before, f"the conversation changed under the message ({before} -> {page.url})"
    assert page.errors == [], page.errors


def test_the_drawer_lists_threads_and_offers_no_way_to_make_one(page):
    """线程 is a list of work, not a set of documents to create.

    It used to open with 新对话, which is the same question the landing page
    asked, one door deeper: which container does this sentence belong to. The
    button is gone -- the conversation already exists -- and what is left is the
    record: the sessions work happened in, with the open one marked.
    """
    page.goto(page.url.split("#")[0], wait_until="networkidle")
    page.wait_for_selector(".home-room", timeout=20_000)

    box = page.wait_for_selector(".composer-wrap textarea", timeout=20_000)
    box.fill("帮我总结当前项目")
    box.press("Enter")
    page.wait_for_selector("text=帮我总结当前项目", timeout=30_000)
    page.wait_for_timeout(2000)

    page.locator(".header-right .ant-btn", has_text="线程").first.click()
    page.wait_for_selector(".session-panel", timeout=10_000)

    assert page.locator(".threads-new").count() == 0, (
        "a conversation is not something the person creates"
    )
    rows = page.locator(".session-item")
    assert rows.count() >= 1, "the thread the turn happened in must be listed"
    assert rows.first.inner_text().strip()
    assert page.locator(".session-item.active").count() == 1, "the open thread is marked"

    assert page.errors == [], page.errors


def test_the_board_answers_a_proposal_where_it_shows_it(page, live_gateway):
    """The card that asks you to decide is the card you decide on.

    A board that shows a blocking proposal and then makes you navigate somewhere
    else to answer it is a page that knows what to do and refuses to do it. (That
    elsewhere was 自主, a page that drew the same rows; it is gone, and this is
    why it could go.)

    Also pins the refresh: answering changes the column, and the page has to be
    told. The first version refreshed only the page the button was on, so the card
    the server had already dropped sat on the board until a reload.
    """
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from agent.autonomy import Proposal
    from agent.autonomy.store import open_proposal_store

    os.environ["SIMPLE_AGENT_HOME"] = str(live_gateway["home"])
    from agent import shared

    shared.AGENT_HOME = live_gateway["home"]
    store = open_proposal_store()
    try:
        proposal = Proposal(
            source="e2e-board",
            reason="有 2 次运行结束得不好，还没有人看过",
            action="读一遍那 2 次失败的运行",
            tool="schedule_runs",
            reversibility="reversible",
            confidence=0.8,
        )
        proposal.context["admission"] = {"answer": "prepare", "reason": "额度已用完"}
        store.record(proposal)
    finally:
        store.close()

    page.goto(page.url.split("#")[0], wait_until="networkidle")
    # The board is behind the header's 看板 button now, closed by default: a 380px
    # rail that appears before anybody asks takes a quarter of the window from the
    # thing they came for.
    page.locator(".header-board").click()
    page.wait_for_selector(".panel-card-proposal", timeout=20_000)

    # The panel opens on one sentence, then draws only the groups that have
    # cards: three headings, two of which report their own emptiness, is the
    # most expensive possible way to say "nothing needs you".
    assert "件等你处理" in page.locator(".panel-summary-state").inner_text()
    titles = [
        page.locator(".panel-group-head").nth(i).inner_text().strip()
        for i in range(page.locator(".panel-group-head").count())
    ]
    # "等你处理" and its count are one heading line, and it leads. Not an exact
    # list: the gateway is shared, so other tests' cards can add groups below.
    assert titles and titles[0].replace("\n", " ").split()[0] == "等你处理", titles

    # By text, not by position: another test in this module seeds its own
    # proposals into the same gateway home, so "the first card" is whichever ran
    # first.
    card = page.locator(".panel-card-proposal").filter(has_text="读一遍那 2 次失败的运行")
    assert card.count() == 1, "the proposal this test seeded is not on the board"
    assert "有 2 次运行结束得不好" in card.first.inner_text(), (
        "the reason is what a decision needs"
    )

    before = page.locator(".panel-card-proposal").count()
    card.first.locator("button").first.click()
    page.wait_for_timeout(2500)
    after = page.locator(".panel-card-proposal").count()
    assert after == before - 1, f"the answered card was not dropped ({before} -> {after})"
    assert page.errors == [], page.errors


def test_a_board_card_opens_its_task(page, live_gateway):
    """The first column is entirely things that need a person, so each card has to
    be a way in.

    A board that shows a failure and cannot open it is a notice board bolted shut:
    the reader learns something is wrong and is then sent to find it themselves.

    Seeded through the scheduler's store, because the e2e gateway's home starts
    empty and waiting ten minutes for its own patrol is not a test of the page.
    """
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from datetime import datetime, timedelta, timezone

    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        TriggerSpec,
        open_store,
    )

    os.environ["SIMPLE_AGENT_HOME"] = str(live_gateway["home"])

    now = datetime.now(timezone.utc)
    # An explicit path, not the module-level default: `open_store()` resolves
    # `shared.SCHEDULER_DB_FILE`, which is computed at import and only rewritten
    # by `_set_agent_home`. Assigning `AGENT_HOME` alone leaves the store pointing
    # at the developer's real home -- which is the kind of mistake that passes
    # locally and fails in CI.
    from pathlib import Path as _Path

    store = open_store(db_path=_Path(live_gateway["home"]) / "tasks" / "scheduler.db")
    try:
        task = store.create_task(
            NewScheduledTask(
                name="看板测试任务",
                kind="agent_prompt",
                trigger=TriggerSpec.once((now - timedelta(minutes=5)).isoformat(), "UTC"),
                payload={"prompt": "检查一次"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )
        claimed = store.claim_task_now(task.id, now=now - timedelta(minutes=5))
        assert claimed is not None
        store.complete_run(
            task.id,
            claimed.run.id,
            finished_at=now - timedelta(minutes=4),
            status="failed",
            error="看板测试：故意失败",
        )
    finally:
        store.close()

    page.goto(page.url.split("#")[0], wait_until="networkidle")
    page.locator(".header-board").click()
    page.wait_for_timeout(600)
    card = page.locator(".panel-card-attention").filter(has_text="看板测试任务")
    card.first.wait_for(timeout=20_000)
    card.first.click()

    # The drawer lives on 自动化, so arriving there is part of opening it.
    page.wait_for_selector(".ant-drawer-open", timeout=20_000)
    assert "#/schedules" in page.url
    # The open drawer only. antd keeps a closed drawer's DOM, so `.ant-drawer-title`
    # also matches the board panel that this click just folded away -- and reading
    # the first one would report the panel's title as the task's.
    title = page.locator(".ant-drawer-open .ant-drawer-title").first.inner_text()
    assert "看板测试任务" in title, title
    assert page.errors == [], page.errors


def test_the_timeline_can_load_older_pages(page, live_gateway):
    """The home timeline is a preview; the history is behind "加载更早".

    It used to offer "全部任务" instead, which sends somebody who asked "what
    happened" into the screen where tasks are *managed*. Different question, so the
    history now continues in place.

    Seeded because the e2e gateway's home starts empty: a first page shorter than
    the limit carries no cursor, which is the correct behaviour and the wrong
    fixture.
    """
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from datetime import datetime, timedelta, timezone
    from pathlib import Path as _Path

    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    os.environ["SIMPLE_AGENT_HOME"] = str(live_gateway["home"])

    now = datetime.now(timezone.utc)
    # Explicit path: `open_store()` resolves a module-level default that is only
    # rewritten by `_set_agent_home`, so it would point at the developer's home.
    store = SchedulerStore(db_path=_Path(live_gateway["home"]) / "tasks" / "scheduler.db")
    try:
        for index in range(11):
            moment = now - timedelta(hours=index + 1)
            task = store.create_task(
                NewScheduledTask(
                    name=f"历史任务 {index}",
                    kind="agent_prompt",
                    trigger=TriggerSpec.once(moment.isoformat(), "UTC"),
                    payload={"prompt": "p"},
                    delivery_mode="standalone",
                    delivery_target=DeliveryTarget.standalone(),
                )
            )
            claimed = store.claim_task_now(task.id, now=moment)
            if claimed is not None:
                store.complete_run(
                    task.id,
                    claimed.run.id,
                    finished_at=moment,
                    status="succeeded",
                    summary=f"第 {index} 次",
                )
    finally:
        store.close()

    page.goto(page.url.split("#")[0], wait_until="networkidle")
    # The record lives inside the conversation's strip now (the landing page that
    # used to hold it open by default is gone), so it has to be opened first.
    page.wait_for_selector(".self-activity-head", timeout=20_000)
    page.locator(".self-activity-head").click()
    page.wait_for_selector(".home-timeline-row", timeout=20_000)
    before = page.locator(".home-timeline-row").count()

    more = page.wait_for_selector(".home-timeline-more", timeout=20_000)
    more.click()
    page.wait_for_timeout(2000)

    after = page.locator(".home-timeline-row").count()
    assert after > before, f"the timeline did not grow ({before} -> {after})"
    assert page.errors == [], page.errors
