"""The out-of-page channel: one desktop notification, and nothing that can break a run.

What is worth pinning here
--------------------------
* **The message is data, not code.** The AppleScript source is a constant and the
  text arrives as ``argv``; building the script by concatenation is how a task
  name or an error message becomes executable AppleScript. The injection test is
  the reason this file exists.
* **The population is the badge's population.** A notification for every
  successful run is noise, and noise is how the one that mattered gets dismissed.
  The rule is ``should_notify_run``, which is ``ATTENTION_STATUSES`` plus the
  missed-occurrence note -- the same rule the SQL counts.
* **Silence where there is no notification centre.** A headless Linux gateway
  gets ``False`` and no subprocess; it does not get a logged error per run.
* **A failure here cannot fail a run.** Every path answers ``False``.

The real notification is never posted by this suite: the subprocess call is
replaced in every test that could reach it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.channels import desktop_notify


class _FakeProcess:
    def __init__(self, returncode: int = 0, hang: bool = False) -> None:
        self.returncode = returncode
        self.killed = False
        self._hang = hang

    async def wait(self) -> int:
        if self._hang:
            await asyncio.sleep(3600)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _darwin(monkeypatch, process: _FakeProcess | None = None):
    """Force the macOS branch and record every argv the notifier would spawn."""
    calls: list[tuple] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(argv)
        return process or _FakeProcess()

    monkeypatch.setattr(desktop_notify, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(desktop_notify.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(desktop_notify.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_the_notification_population_is_failures_only():
    """A banner is for something to act on; the badge may count more than that.

    Measured on the machine this runs on: after the gateway had been down, every
    task carried a "6 次触发在停机期间被跳过" note, and the badge's rule fired a
    banner for each one -- eight tasks, eight interruptions, all saying the
    scheduler had not been running while it was not running. The note belongs on
    the badge (a number you can ignore) and in the run's record, not on a screen
    that takes the whole display.
    """
    from agent.scheduler.models import ATTENTION_STATUSES

    for status in ATTENTION_STATUSES:
        assert desktop_notify.should_notify_run(status) is True, status

    assert desktop_notify.should_notify_run("succeeded") is False
    assert desktop_notify.should_notify_run("cancelled") is False
    # The rule that caused the burst, pinned so it cannot come back quietly.
    assert desktop_notify.should_notify_run("succeeded", missed_count=6) is False


def test_notifications_are_on_unless_turned_off():
    assert desktop_notify.desktop_notifications_enabled({}) is True
    assert desktop_notify.desktop_notifications_enabled({"notifications": {}}) is True
    assert (
        desktop_notify.desktop_notifications_enabled(
            {"notifications": {"desktop": False}}
        )
        is False
    )
    # A section that is not an object keeps the default rather than reading as
    # "enabled because truthy".
    assert desktop_notify.desktop_notifications_enabled({"notifications": "yes"}) is True
    # A *string* that says no means no: `"false"` is truthy in Python, and the
    # one thing this switch must not do is ignore an operator who wrote it out.
    for word in ("false", "no", "off", "0", "FALSE", " none "):
        assert (
            desktop_notify.desktop_notifications_enabled(
                {"notifications": {"desktop": word}}
            )
            is False
        ), word
    assert (
        desktop_notify.desktop_notifications_enabled(
            {"notifications": {"desktop": "true"}}
        )
        is True
    )


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_platform_without_a_notification_centre_is_silent(monkeypatch, platform):
    monkeypatch.setattr(desktop_notify, "sys", SimpleNamespace(platform=platform))
    spawns: list[tuple] = []

    async def fake_exec(*argv, **kwargs):  # pragma: no cover - must not be reached
        spawns.append(argv)
        return _FakeProcess()

    monkeypatch.setattr(desktop_notify.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(desktop_notify.notify("Simple Agent", "hi")) is False
    assert spawns == []


def test_no_osascript_means_no_notification(monkeypatch):
    monkeypatch.setattr(desktop_notify, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(desktop_notify.shutil, "which", lambda name: None)
    assert asyncio.run(desktop_notify.notify("Simple Agent", "hi")) is False


def test_the_text_is_passed_as_an_argument_not_welded_into_the_script(monkeypatch):
    """The injection test.

    A task name or an error message containing a quote is ordinary; a quote that
    ends the string literal in the AppleScript source is an injected command. The
    script is a constant and the text is one ``argv`` item, so the second cannot
    happen -- and this test would fail the moment somebody went back to
    ``-e f'display notification "{text}"'``.
    """
    calls = _darwin(monkeypatch)
    hostile = 'report "done" & (do shell script "rm -rf ~/x")'

    assert asyncio.run(desktop_notify.notify("Simple Agent", hostile)) is True
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "/usr/bin/osascript"
    assert argv[1] == "-e"
    assert argv[2] == desktop_notify._SCRIPT
    assert hostile not in desktop_notify._SCRIPT, "the script must be a constant"
    assert "rm -rf" not in "".join(argv[:3]), "the text may only travel as an argument"
    assert argv[3] == hostile, "the message arrives verbatim as one argument"
    assert argv[4] == "Simple Agent"


def test_layout_whitespace_is_collapsed_because_a_banner_is_one_line(monkeypatch):
    calls = _darwin(monkeypatch)
    asyncio.run(desktop_notify.notify("Simple Agent", "line one\n\n  line two"))
    assert calls[0][3] == "line one line two"


def test_a_hanging_osascript_is_killed_and_reported_as_no_notification(monkeypatch):
    process = _FakeProcess(hang=True)
    calls = _darwin(monkeypatch, process)
    assert (
        asyncio.run(desktop_notify.notify("Simple Agent", "hi", timeout=0.01)) is False
    )
    assert process.killed is True, "a run must not wait forever on a permission prompt"
    assert len(calls) == 1


def test_an_osascript_that_exits_nonzero_is_a_failure_not_an_exception(monkeypatch):
    _darwin(monkeypatch, _FakeProcess(returncode=1))
    assert asyncio.run(desktop_notify.notify("Simple Agent", "hi")) is False


def test_a_run_that_needs_nobody_sends_nothing(monkeypatch):
    calls = _darwin(monkeypatch)
    sent = asyncio.run(
        desktop_notify.notify_run_needs_attention(
            task_name="每日简报", status="succeeded", summary="all good"
        )
    )
    assert sent is False
    assert calls == []


def test_a_failed_run_says_what_failed(monkeypatch):
    calls = _darwin(monkeypatch)
    sent = asyncio.run(
        desktop_notify.notify_run_needs_attention(
            task_name="每日简报",
            status="failed",
            summary="wrote 3 of 5 files",
            error="TimeoutError: the model call took 600s\nsecond line ignored",
        )
    )
    assert sent is True
    message = calls[0][3]
    assert "每日简报" in message
    assert "TimeoutError" in message
    # The first line only: a banner is one line, and the second one is the trace.
    assert "second line ignored" not in message


def test_a_successful_run_after_a_gap_does_not_interrupt(monkeypatch):
    """The gap is history, and the run succeeded: there is nothing to do."""
    calls = _darwin(monkeypatch)
    sent = asyncio.run(
        desktop_notify.notify_run_needs_attention(
            task_name="每日简报",
            status="succeeded",
            summary="report is ready",
            missed_count=4,
        )
    )
    assert sent is False
    assert calls == [], "no banner, and no subprocess either"


def test_disabled_means_disabled(monkeypatch):
    calls = _darwin(monkeypatch)
    sent = asyncio.run(
        desktop_notify.notify_run_needs_attention(
            task_name="每日简报", status="failed", error="boom", enabled=False
        )
    )
    assert sent is False
    assert calls == []


# ── proving, from the settings page, that a failure would reach you ─────────


def _notify_test_request():
    """A minimal request object: the handler reads nothing but auth headers."""

    class _Request:
        headers: dict[str, str] = {}

    return _Request()


def test_the_test_button_reports_every_path_separately(monkeypatch):
    """\"The banner failed but the chat message went\" is the answer that helps.

    A single boolean would throw away which path to fix.
    """
    import asyncio

    from agent.channels import web_notify

    sent: list[tuple[str, str]] = []

    async def fake_desktop(title, message, **_kw):
        sent.append(("desktop", message))
        return True

    async def fake_feishu(chat_id, text):
        sent.append(("feishu", chat_id))
        return {"ok": True, "detail": "已发到选定的会话，去飞书看一眼"}

    monkeypatch.setattr(
        "agent.channels.desktop_notify.notify", fake_desktop, raising=False
    )
    monkeypatch.setattr(web_notify, "_send_feishu_test", fake_feishu)
    monkeypatch.setattr(
        web_notify,
        "_feishu_section",
        lambda cfg: {"notify_chat_id": "oc-1"},
    )
    monkeypatch.setattr(
        "agent.config.load_config",
        lambda: ({"notifications": {"desktop": True}, "channels": {}}, ""),
    )

    response = asyncio.run(
        web_notify._notify_test(
            _notify_test_request(), authorized=lambda _r: True
        )
    )
    import json

    body = json.loads(bytes(response.body).decode())

    assert body["ok"] is True
    by_channel = {item["channel"]: item for item in body["channels"]}
    assert by_channel["desktop"]["ok"] is True
    assert by_channel["feishu"]["ok"] is True
    assert {name for name, _ in sent} == {"desktop", "feishu"}


def test_an_unconfigured_chat_is_reported_as_skipped_not_failed(monkeypatch):
    """\"Nothing is set up\" and \"it is set up and broken\" are different answers."""
    import asyncio
    import json

    from agent.channels import web_notify

    async def fake_desktop(title, message, **_kw):
        return True

    monkeypatch.setattr(
        "agent.channels.desktop_notify.notify", fake_desktop, raising=False
    )
    monkeypatch.setattr(web_notify, "_feishu_section", lambda cfg: {})
    monkeypatch.setattr(
        "agent.config.load_config",
        lambda: ({"notifications": {"desktop": True}}, ""),
    )

    response = asyncio.run(
        web_notify._notify_test(
            _notify_test_request(), authorized=lambda _r: True
        )
    )
    body = json.loads(bytes(response.body).decode())
    by_channel = {item["channel"]: item for item in body["channels"]}

    assert by_channel["feishu"]["skipped"] is True
    assert by_channel["feishu"]["ok"] is False
    assert "还没选" in by_channel["feishu"]["detail"]


def test_turning_desktop_off_is_said_rather_than_attempted(monkeypatch):
    """A disabled channel is skipped, not silently reported as working."""
    import asyncio
    import json

    from agent.channels import web_notify

    attempted: list[str] = []

    async def fake_desktop(title, message, **_kw):
        attempted.append(message)
        return True

    monkeypatch.setattr(
        "agent.channels.desktop_notify.notify", fake_desktop, raising=False
    )
    monkeypatch.setattr(web_notify, "_feishu_section", lambda cfg: {})
    monkeypatch.setattr(
        "agent.config.load_config",
        lambda: ({"notifications": {"desktop": False}}, ""),
    )

    response = asyncio.run(
        web_notify._notify_test(
            _notify_test_request(), authorized=lambda _r: True
        )
    )
    body = json.loads(bytes(response.body).decode())
    by_channel = {item["channel"]: item for item in body["channels"]}

    assert attempted == [], "a disabled channel must not be attempted"
    assert by_channel["desktop"]["skipped"] is True
    assert body["ok"] is False, "nothing left, so nothing reached the person"
