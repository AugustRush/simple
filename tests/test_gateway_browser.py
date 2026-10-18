"""The gateway opens the web UI — and knows when not to.

Two things are being tested, and they are separate on purpose:

* the address.  ``public_url`` is the *reader's* address, not the bind address:
  a wildcard bind is not a URL, and printing ``http://0.0.0.0:8787`` sends the
  browser to something that only works by accident.
* the decision.  A gateway started by a service, a CI job or an agent's
  background command has no one in front of it; opening a window there is a
  surprise, so the three opt-outs are checked without a browser in sight.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.channels.web import WebChannel, WebConfig
from agent.cli import (
    _gateway_web_channel,
    _open_browser_when_ready,
    _should_open_browser,
)


# ── the address ─────────────────────────────────────────────────────────────


def test_the_public_url_is_the_address_a_browser_can_use():
    channel = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=8787))

    assert channel.public_url == "http://127.0.0.1:8787/"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", ""])
def test_a_wildcard_bind_is_reported_as_localhost(host):
    channel = WebChannel(WebConfig(enabled=True, host=host, port=9000))

    assert channel.public_url == "http://127.0.0.1:9000/"


def test_a_disabled_channel_has_no_public_url():
    channel = WebChannel(WebConfig(enabled=False, host="127.0.0.1", port=8787))

    assert channel.public_url == ""


def test_the_gateway_finds_the_web_channel_among_the_others():
    web = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=8787))
    other = object()

    assert _gateway_web_channel([other, web]) is web
    assert _gateway_web_channel([other]) is None


# ── the decision ────────────────────────────────────────────────────────────


def test_an_interactive_gateway_opens_the_ui():
    assert _should_open_browser(no_open=False, interactive=True) is True


@pytest.mark.parametrize(
    ("no_open", "interactive"),
    [
        (True, True),   # the flag
        (False, False),  # no terminal: a service, CI, or an agent's job
    ],
)
def test_the_opt_outs_suppress_the_browser(no_open, interactive):
    assert _should_open_browser(no_open=no_open, interactive=interactive) is False


def test_the_environment_can_suppress_the_browser(monkeypatch):
    monkeypatch.setenv("SIMPLE_NO_BROWSER", "1")

    assert _should_open_browser(no_open=False, interactive=True) is False


# ── readiness ───────────────────────────────────────────────────────────────


def test_a_server_that_is_not_started_yet_is_not_ready():
    channel = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=8787))
    channel._server = SimpleNamespace(started=False)
    channel._server_task = None

    assert asyncio.run(channel.wait_ready(timeout=0.15)) is False


def test_a_started_server_is_ready():
    channel = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=8787))
    channel._server = SimpleNamespace(started=True)

    assert asyncio.run(channel.wait_ready(timeout=0.15)) is True


def test_a_server_that_failed_to_bind_is_never_ready():
    """A connect would succeed against whatever holds the port; the server's own
    task being finished is what says this gateway never got it."""
    channel = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=8787))
    channel._server = SimpleNamespace(started=False)

    async def scenario():
        channel._server_task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)
        return await channel.wait_ready(timeout=0.5)

    assert asyncio.run(scenario()) is False


# ── opening ─────────────────────────────────────────────────────────────────


def test_the_browser_opens_only_after_the_channel_is_ready(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("agent.cli.webbrowser.open", lambda url: opened.append(url) or True)

    async def ready():
        return True

    asyncio.run(_open_browser_when_ready("http://127.0.0.1:8787/", ready))

    assert opened == ["http://127.0.0.1:8787/"]


def test_a_channel_that_never_gets_ready_does_not_open_a_browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("agent.cli.webbrowser.open", lambda url: opened.append(url) or True)

    async def ready():
        return False

    asyncio.run(_open_browser_when_ready("http://127.0.0.1:8787/", ready))

    assert opened == []


def test_a_readiness_check_that_raises_does_not_open_a_browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("agent.cli.webbrowser.open", lambda url: opened.append(url) or True)

    async def ready():
        raise RuntimeError("server died")

    asyncio.run(_open_browser_when_ready("http://127.0.0.1:8787/", ready))

    assert opened == []
