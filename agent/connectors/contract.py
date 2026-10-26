"""What a connector is: one outside service this agent may read, and nothing else.

Why this exists, next to MCP
----------------------------
The agent already reaches third-party services through MCP, and that is the right
answer for anything that ships a server.  It is the wrong answer for the two things
a *personal* agent needs most -- the owner's mail and their calendar -- because
neither has an official server, and the usual workaround is a shell command with a
password pasted into it.  A connector is that other shape: implemented here, small,
and honest about what it will not do.

The three rules, each a decision rather than an omission
--------------------------------------------------------
**Read-only.**  A connector can search and read.  It has no way to send, move,
delete, label or reply: those are exactly the actions where "the agent did it" needs
a person in the loop, and an interface that cannot express them cannot be talked
into them by a clever turn or a prompt injection.  Adding a write verb later is a
deliberate change with an approval in front of it, not a parameter.

**Off until configured.**  An unconfigured connector registers no tools at all.  A
tool the model can see but that can only answer "not configured" is worse than no
tool: it invites the model to try, fail, and then explain its own failure to the
person, which reads as a broken deployment rather than as a switch nobody flipped.

**Credentials come from the environment, never from the config file.**  The config
file is what people paste into issue reports and commit to dotfiles repos.  A
connector names the *variable* to read, and a literal secret in the config is
refused with a reason rather than used quietly.

What a connector is not
-----------------------
Not a plugin (it is not mounted or unloaded independently -- the composition owns
one connector plugin that fans out), and not a channel (it does not deliver
anything).  It is a read surface with a status line, so "why can't it see my mail"
has an answer that is a sentence rather than a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["Connector", "ConnectorStatus"]


@dataclass(frozen=True)
class ConnectorStatus:
    """What a connector can say about itself without touching the network.

    ``available`` is about *configuration*, not reachability: a mailbox whose host is
    down is still available (and the tool's own error says so); one with no password
    is not, and no retry will change that.
    """

    name: str
    kind: str
    available: bool
    #: Why not, when not -- one sentence, addressed to the person who configured it.
    reason: str = ""
    #: Non-secret facts worth showing: mailbox, host, window size.
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@runtime_checkable
class Connector(Protocol):
    """One read-only outside service.

    ``search`` and ``read`` are deliberately the whole surface.  Both take and
    return plain data (dicts and lists) so a tool can serialise them unchanged, and
    both are synchronous: the tool layer runs them on a worker thread, which is what
    keeps a slow IMAP server from blocking the turn's event loop.
    """

    name: str
    kind: str

    def status(self) -> ConnectorStatus:
        """Whether this connector is configured, and why not when it is not."""

    def search(self, query: str, limit: int = 10) -> "list[dict[str, Any]]":
        """Find items matching *query*, newest first.  Empty query means "recent"."""

    def read(self, item_id: str) -> "dict[str, Any]":
        """One item in full, addressed by whatever ``search`` returned as its id."""
