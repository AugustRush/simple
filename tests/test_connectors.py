"""The connector capability: read-only, off until configured, and honest about both.

What these tests are for
------------------------
The feature is not "an IMAP client exists"; it is the three promises the contract
makes, each of which is cheap to state and easy to break later:

* **Off until configured.**  An unconfigured mailbox registers *no tools at all*.
  A tool that can only answer "not configured" invites the model to try, fail, and
  explain its own failure to the person.
* **Read-only, enforced below the code that could get it wrong.**  The mailbox is
  selected ``readonly=True`` (the server refuses writes) and bodies come back via
  ``BODY.PEEK`` (nothing is marked seen).  The recorded command list is asserted,
  not the intention.
* **No secret in the config file.**  A literal ``password`` is *refused*, not
  honoured -- silently accepting it is how a mailbox password ends up in a
  dotfiles repo.

No network: ``imaplib.IMAP4_SSL`` is replaced by a fake that records every command
and serves a couple of messages.  That is also what makes the read-only assertion
possible at all -- a real server would merely comply.
"""

from __future__ import annotations

import asyncio
import json
from email.message import EmailMessage

import pytest

from agent.connectors import email as email_module
from agent.connectors.email import EmailConnector, EmailSettings
from agent.connectors.plugin import ConnectorPlugin
from agent.kernel.plugin import PluginHost


def _message(subject: str, sender: str, body: str, *, uid: int) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Mon, 26 Oct 2026 09:00:00 +0800"
    message.set_content(body)
    return message.as_bytes()


class _FakeIMAP:
    """Records every command; serves whatever the test put in ``messages``."""

    instances: "list[_FakeIMAP]" = []
    #: Seeded by the test; every connection opens its own instance, because the
    #: connector opens one per call and a test cannot pre-configure "the" instance.
    seed: "dict[str, bytes]" = {}
    fail_login = False

    #: Commands a read-only client must never issue.  Asserted rather than trusted
    #: to the module's own docstring.
    FORBIDDEN = {"store", "copy", "move", "expunge", "append", "delete", "rename"}

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.commands: "list[tuple[str, tuple]]" = []
        self.messages: "dict[str, bytes]" = dict(type(self).seed)
        self.readonly: "bool | None" = None
        _FakeIMAP.instances.append(self)

    # -- the parts the connector calls ------------------------------------
    def login(self, user: str, password: str) -> "tuple[str, list]":
        self.commands.append(("login", (user,)))
        if type(self).fail_login or password != "s3cret":
            raise RuntimeError("authentication failed")
        return ("OK", [b"logged in"])

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> "tuple[str, list]":
        self.commands.append(("select", (mailbox, readonly)))
        self.readonly = readonly
        return ("OK", [b"1"])

    def search(self, charset: "str | None", criteria: str) -> "tuple[str, list]":
        self.commands.append(("search", (criteria,)))
        return ("OK", [b" ".join(uid.encode() for uid in self.messages)])

    def fetch(self, ids: str, spec: str) -> "tuple[str, list]":
        self.commands.append(("fetch", (ids, spec)))
        wanted = [part.strip() for part in str(ids).split(",") if part.strip()]
        rows: list = []
        for uid in wanted:
            raw = self.messages.get(uid)
            if raw is None:
                continue
            rows.append((f"{uid} (BODY[] {{{len(raw)}}}".encode(), raw))
            rows.append(b")")
        return ("OK", rows)

    def logout(self) -> "tuple[str, list]":
        self.commands.append(("logout", ()))
        return ("BYE", [])

    # -- helpers for assertions -------------------------------------------
    def names(self) -> "list[str]":
        return [name for name, _ in self.commands]

    def spec_of(self, command: str) -> str:
        for name, args in self.commands:
            if name == command:
                return str(args[-1])
        raise AssertionError(f"{command} was never issued: {self.names()}")


@pytest.fixture
def fake_imap(monkeypatch) -> "type[_FakeIMAP]":
    _FakeIMAP.instances = []
    _FakeIMAP.seed = {}
    _FakeIMAP.fail_login = False
    monkeypatch.setattr(email_module.imaplib, "IMAP4_SSL", _FakeIMAP)
    return _FakeIMAP


def _email_section(**overrides) -> dict:
    """A configured mailbox, spelled the way each test wants it."""
    section = {
        "host": "imap.example.com",
        "port": 993,
        "user": "me@example.com",
        "password_env": "TEST_MAIL_PASSWORD",
        "mailbox": "INBOX",
    }
    section.update(overrides)
    return section


def _cfg(**overrides) -> dict:
    return {"connectors": {"email": _email_section(**overrides)}}


def _configured(monkeypatch, *, password: str = "s3cret", **overrides) -> EmailConnector:
    monkeypatch.setenv("TEST_MAIL_PASSWORD", password)
    return EmailConnector(EmailSettings.from_config(_cfg(**overrides)))


# ── the plugin: what it provides and what it registers ───────────────────────


def _load_plugin(cfg: dict):
    """Mount the connector plugin the way the composition does, in a bare host."""
    import agent as agent_module
    from agent.kernel.service import ServiceRegistry

    registry = agent_module.ToolRegistry(console=None)
    mapping = {"cfg": cfg, "registry": registry}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(ConnectorPlugin())
    return host, registry, mapping


def test_an_unconfigured_mailbox_registers_no_tools_at_all(monkeypatch):
    """The whole reason this is a plugin: "exists only when configured" needs a place."""
    monkeypatch.delenv("TEST_MAIL_PASSWORD", raising=False)
    host, registry, mapping = _load_plugin({"connectors": {}})

    assert "email_search" not in registry.list_tools()
    assert "email_read" not in registry.list_tools()
    # ...but the surface exists, so the reason is askable.
    connectors = mapping["connectors"]
    [status] = connectors.statuses()
    assert status["available"] is False
    assert "TEST_MAIL_PASSWORD" in status["reason"] or "EMAIL_PASSWORD" in status["reason"]
    assert host.loaded() == ("connectors",)


def test_a_configured_mailbox_registers_two_read_only_tools(monkeypatch):
    connector = _configured(monkeypatch)
    assert connector.status().available is True

    host, registry, _ = _load_plugin(_cfg())
    assert {"email_search", "email_read"} <= set(registry.list_tools())
    # The capability declaration is what a read-only permission profile filters on.
    assert registry.tool_capabilities("email_search") == frozenset({"read"})
    assert registry.tool_capabilities("email_read") == frozenset({"read"})
    # Nothing write-shaped is declared, because nothing write-shaped exists.
    for name in registry.list_tools():
        assert registry.tool_capabilities(name) <= {"read"}


def test_a_password_in_the_config_file_is_refused_not_honoured(monkeypatch, fake_imap):
    """The obvious thing to try must not be the thing that leaks a password."""
    connector = EmailConnector(
        EmailSettings.from_config(
            {
                "connectors": {
                    "email": {
                        "host": "imap.example.com",
                        "user": "me@example.com",
                        "password": "hunter2",
                    }
                }
            }
        )
    )
    status = connector.status()
    assert status.available is False
    assert "environment" in status.reason.lower()
    assert fake_imap.instances == [], "a refused configuration must not connect"

    with pytest.raises(RuntimeError, match="not configured"):
        connector.search("anything")


# ── reading: the three read-only promises ────────────────────────────────────


def test_search_matches_locally_and_returns_newest_first(monkeypatch, fake_imap):
    """One batched header fetch, then a substring match here -- not IMAP SEARCH."""
    connector = _configured(monkeypatch)
    fake_imap.seed = {
        "1": _message("第三周周报", "小王 <wang@example.com>", "进度", uid=1),
        "2": _message("Re: 周报", "li@example.com", "收到", uid=2),
        "3": _message("发票", "billing@example.com", "账单", uid=3),
    }

    items = connector.search("周报", limit=5)
    client = fake_imap.instances[0]

    assert [item["subject"] for item in items] == ["Re: 周报", "第三周周报"]
    assert items[0]["uid"] == "2", "newest uid first"
    # One search and one batched fetch: the window costs one round trip.
    assert client.names().count("search") == 1
    assert client.names().count("fetch") == 1


def test_the_mailbox_is_read_only_and_bodies_are_never_marked_seen(monkeypatch, fake_imap):
    """Read-only is enforced by the server and by PEEK, not by good intentions."""
    connector = _configured(monkeypatch)
    fake_imap.seed = {"7": _message("hi", "a@example.com", "body", uid=7)}

    connector.search("", limit=1)
    connector.read("7")

    # One connection per call, so the assertions run across both.
    clients = fake_imap.instances
    assert len(clients) == 2, "search and read each open their own connection"
    for client in clients:
        assert ("select", ("INBOX", True)) in client.commands, "select must be readonly"
        assert client.readonly is True
        assert "PEEK" in client.spec_of("fetch")
        assert not set(client.names()) & client.FORBIDDEN, client.names()
        assert "logout" in client.names(), "a leaked socket keeps the selection alive"


def test_read_decodes_the_body_and_cuts_a_long_one(monkeypatch, fake_imap):
    connector = _configured(monkeypatch)
    fake_imap.seed = {
        "9": _message("长信", "a@example.com", "字" * (email_module.MAX_BODY_CHARS + 500), uid=9)
    }

    item = connector.read("9")
    client = fake_imap.instances[0]

    assert item["subject"] == "长信"
    assert item["truncated"] is True
    assert len(item["body"]) == email_module.MAX_BODY_CHARS
    assert "PEEK" in client.spec_of("fetch"), "reading must not mark the message seen"


def test_read_refuses_an_id_it_did_not_hand_out(monkeypatch):
    connector = _configured(monkeypatch)
    with pytest.raises(ValueError):
        connector.read("INBOX/1")


def test_a_failing_login_is_reported_and_the_connection_is_closed(monkeypatch, fake_imap):
    connector = _configured(monkeypatch, password="wrong")
    fake_imap.fail_login = True

    with pytest.raises(RuntimeError):
        connector.search("x")

    [client] = fake_imap.instances
    assert "logout" in client.names(), "a failed login must still release the socket"


# ── the tools the model actually sees ────────────────────────────────────────


def _tool_json(registry, name: str, args: dict) -> dict:
    raw = asyncio.run(registry.call(name, args))
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def test_the_tools_answer_with_one_json_object(monkeypatch, fake_imap):
    connector = _configured(monkeypatch)
    fake_imap.seed = {"4": _message("周报", "a@example.com", "正文", uid=4)}
    _, registry, _ = _load_plugin(_cfg())

    found = _tool_json(registry, "email_search", {"query": "周报", "limit": 3})
    assert found["ok"] is True and found["count"] == 1

    read = _tool_json(registry, "email_read", {"item_id": "4"})
    assert read["ok"] is True and read["item"]["body"].strip() == "正文"


def test_the_tools_cap_what_they_return(monkeypatch, fake_imap):
    """A model asking for 500 results gets the cap, and the reply is still one object."""
    connector = _configured(monkeypatch)
    fake_imap.seed = {
        str(uid): _message(f"m{uid}", "a@example.com", "b", uid=uid)
        for uid in range(1, 31)
    }
    _, registry, _ = _load_plugin(_cfg())

    payload = _tool_json(registry, "email_search", {"query": "", "limit": 500})

    assert payload["ok"] is True
    assert 1 <= payload["count"] <= 20, "the list is capped before it reaches the model"


def test_a_tool_error_is_data_not_an_exception(monkeypatch, fake_imap):
    """A provider that is down must read as one object the model can act on."""
    connector = _configured(monkeypatch)
    fake_imap.fail_login = True
    _, registry, _ = _load_plugin(_cfg())

    payload = _tool_json(registry, "email_search", {"query": "x"})

    assert payload["ok"] is False
    assert "authentication failed" in payload["error"]
