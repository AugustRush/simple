"""The connector plugin: build what is configured, and register only what works.

Why a plugin rather than a few more built-in tools
--------------------------------------------------
The tools a connector contributes depend on **configuration**: a deployment with no
mailbox configured should not offer `email_search` at all.  Built-in tools are
registered unconditionally by `ToolsPlugin`, so "this tool exists only when a
credential does" has no place to be expressed there.  A plugin is that place: it
receives the config at setup, decides what to contribute, and its registrations are
undone with its scope like any other contribution.

Why an unconfigured connector still provides ``connectors``
-----------------------------------------------------------
So that "why can't the agent see my mail" has an answer.  The service holds every
connector *including* the ones that are not usable, each with a status and a reason;
a surface that only appears when it works cannot tell anyone why it is missing.

The tools are registered with ``capabilities=("read",)`` and nothing else -- see
:mod:`agent.connectors.contract` for why there is no write verb to declare.

Credentials never reach the model: the tool results carry mail, the status carries
host/user/mailbox, and the password lives only in the environment of the process.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from agent import shared

from .contract import Connector, ConnectorStatus
from .email import EmailConnector, EmailSettings

__all__ = ["ConnectorPlugin", "ConnectorRegistry"]

#: The busiest a tool result is allowed to be.  The connector itself caps bodies; this
#: caps the *list*, so "search my mail" cannot return a mailbox.
_MAX_SEARCH_RESULTS = 20


class ConnectorRegistry:
    """Every connector this deployment knows about, usable or not."""

    def __init__(self, connectors: "list[Connector]") -> None:
        self._connectors = list(connectors)

    def all(self) -> "list[Connector]":
        return list(self._connectors)

    def get(self, name: str) -> Any:
        return next(
            (connector for connector in self._connectors if connector.name == name),
            None,
        )

    def statuses(self) -> "list[dict[str, Any]]":
        """One row per connector, for a page, a CLI, or a diagnostic."""
        return [connector.status().to_dict() for connector in self._connectors]


def _search_tool(connector: Connector) -> "Callable[..., Any]":
    async def email_search(query: str = "", limit: int = 10) -> dict[str, Any]:
        # The IMAP work is blocking; `to_thread` is what keeps one slow mail server
        # from freezing a turn's event loop (and every other session on it).
        try:
            items = await asyncio.to_thread(
                connector.search, query, min(int(limit or 10), _MAX_SEARCH_RESULTS)
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "count": len(items), "items": items}

    return email_search


def _read_tool(connector: Connector) -> "Callable[..., Any]":
    async def email_read(item_id: str) -> dict[str, Any]:
        try:
            item = await asyncio.to_thread(connector.read, item_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "item": item}

    return email_read


def _email_tool_specs(connector: Connector) -> "list[tuple]":
    """The two tools, with model-facing descriptions.

    The descriptions say what the tool *cannot* do, not only what it does: a model
    that knows it cannot send is a model that will not promise to.
    """
    settings = getattr(connector, "settings", None)
    mailbox = getattr(settings, "mailbox", "INBOX")
    window = getattr(settings, "search_window", 60)
    return [
        (
            "email_search",
            (
                f"Search the owner's mailbox ({mailbox}) by substring over the most "
                f"recent {window} messages' subject and sender, newest first. "
                "Read-only: there is no way to send, reply, move, delete or mark "
                "anything. An empty query means the newest messages."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Substring to look for in subject or sender; empty for recent",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"How many results (1-{_MAX_SEARCH_RESULTS}, default 10)",
                        "default": 10,
                    },
                },
            },
            _search_tool(connector),
        ),
        (
            "email_read",
            (
                "Read one message in full by the uid that email_search returned. "
                "Read-only; long bodies are cut and the reply says so with "
                "`truncated: true`."
            ),
            {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "The uid from email_search",
                    }
                },
                "required": ["item_id"],
            },
            _read_tool(connector),
        ),
    ]


class ConnectorPlugin:
    """Provides ``connectors``; registers the read tools a configuration enables."""

    name = "connectors"

    #: ``cfg`` says what is configured and ``registry`` is where the tools go.
    #: Both required: a connector plugin that ran without a registry would silently
    #: contribute nothing, which is the failure mode this plugin exists to expose.
    inject = ("cfg", "registry")

    def setup(self, ctx: Any) -> None:
        cfg = ctx.get("cfg", None) or {}
        connectors: "list[Connector]" = [
            EmailConnector(EmailSettings.from_config(cfg))
        ]
        ctx.provide("connectors", ConnectorRegistry(connectors))

        registry = ctx.get("registry")
        for connector in connectors:
            status = connector.status()
            if not status.available:
                # Not an error -- this is the ordinary state of a deployment that has
                # connected nothing.  Said once, at startup, in one line, because the
                # alternative is a person wondering why the agent cannot see their
                # mail and finding no answer anywhere.
                shared.CONSOLE.print(
                    f"[dim]connector {connector.name}: {status.reason}[/dim]"
                )
                continue
            for name, description, parameters, fn in _email_tool_specs(connector):
                registry.register(
                    name,
                    description,
                    parameters,
                    fn,
                    source="connector",
                    capabilities=("read",),
                )
