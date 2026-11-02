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


def test_the_shell_renders_without_javascript_errors(page):
    """A bundle that throws while mounting still answers every request correctly.

    This is the failure the protocol tests structurally cannot see, and the reason the
    browser is worth its download: ``goto`` succeeds either way, so the assertion is
    that the *page* came up -- a navigation label, the composer -- and that nothing
    logged an error while it did.
    """
    page.wait_for_selector("textarea", timeout=20_000)
    assert page.locator("text=记忆").first.is_visible()
    assert page.locator("text=扩展").first.is_visible()
    assert page.errors == [], page.errors


def test_a_message_is_answered_on_the_page(page):
    """The functional layer proves the socket carries the reply; this proves it lands."""
    composer = page.wait_for_selector("textarea", timeout=20_000)
    composer.fill("this is a browser message")
    composer.press("Enter")

    page.wait_for_selector("text=pong from the browser", timeout=30_000)
    assert "this is a browser message" in page.inner_text("body")
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

    page.locator(".nav-label", has_text="自动化").first.click()
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


def test_one_create_entry_continues_into_a_chain(page):
    """One button, and the chain is the *next* step rather than a different form.

    The page used to greet you with two creators -- one for a task, one for a chain --
    which asked a person to classify their own request before filling anything in.
    Now there is one, and "改成流程（多步）" continues the same draft into a one-step chain:
    the name, the content and the trigger you already chose move across.
    """
    page.locator(".nav-label", has_text="自动化").first.click()
    page.wait_for_selector(".schedule-tabs", timeout=20_000)

    # One creator in the page head: neither "新建任务" nor "新建流程" is there to choose
    # between any more -- the one button says what it does and the filter says the rest.
    create = page.locator(".schedule-page-head button:has-text('新建')")
    assert create.count() == 1, [create.nth(i).inner_text() for i in range(create.count())]
    create.first.click()
    page.wait_for_selector(".ant-modal-title:has-text('新建任务')", timeout=20_000)

    page.get_by_role("button", name="改成流程（多步）").click()
    page.wait_for_selector(".ant-modal-title:has-text('新建流程')", timeout=20_000)

    assert page.errors == [], page.errors
