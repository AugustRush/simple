"""The shape of a provider request, described the way a prefix cache sees it.

A provider's prompt cache bills a request by how much of its *front* it has
already stored: the cached part costs a fraction of the uncached part, so a
request's price is decided by where its content first diverges from something
the provider has seen before.  That makes a request three parts with very
different properties:

* the **head** -- the system prompt, then the tool schemas -- is the only part
  every request of a session can share.  Anything that varies per request, put
  here, therefore costs the most, and costs it on every request.
* the **body** -- the messages -- is naturally append-only, and that is what
  lets the head's sharing survive.  Its *first* message is the cheapest place
  to notice that something rewrote it, because a drop or an insert at the front
  is exactly what breaks the prefix.
* everything after that is per-request content and cannot be shared anyway.

These helpers exist so a recorded provider call can say which part changed.
They are fingerprints rather than lengths because the question is "is this the
same as last time", and a length answers it wrongly for any edit that preserves
size -- which is precisely the edit a shallow review misses.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(payload: str) -> str:
    """A short, process-stable digest.

    ``blake2b`` rather than :func:`hash`: the value is written to a database and
    read back by a later process, and ``hash`` is salted per process, so it
    would compare unequal to itself across runs and report a change that never
    happened.
    """
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


def _render(value: Any) -> str:
    """Serialise for fingerprinting, order-insensitively and stably.

    ``sort_keys`` makes two dictionaries that differ only in insertion order
    fingerprint the same, which is right: the provider's cache matches tokens,
    and a re-serialisation that reorders keys without changing the tokens'
    order cannot cost a hit.  ``default=str`` is a last resort for a value that
    is not JSON -- a tool schema built by hand could carry one -- and such a
    value is the one thing that can make this fingerprint unstable, because
    ``str`` of an arbitrary object includes its address.  Everything the agent
    actually sends comes from ``ToolRegistry.to_anthropic_format()``, which
    yields plain JSON.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def head_fingerprint(system_prompt: str, tools: list[dict[str, Any]]) -> str:
    """Fingerprint the part of a request that must not change within a session.

    Covers the system prompt and the tool schemas *together*, because both sit
    ahead of the messages: a change to either invalidates everything after it,
    so a fingerprint covering only one would report "unchanged" for a request
    that had just lost its whole prefix.
    """
    return _digest(system_prompt + "\x00" + _render(tools))


def body_front_fingerprint(messages: list[dict[str, Any]]) -> str:
    """Fingerprint the first message of the body, where a rewrite shows up.

    Empty string for an empty body rather than the digest of nothing, so a
    caller reading the column can tell "no messages" from "some messages".
    """
    if not messages:
        return ""
    first = messages[0]
    return _digest(_render([first.get("role"), first.get("content")]))


def describe_payload(
    system_prompt: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    compacted: bool = False,
) -> dict[str, Any]:
    """The cache-relevant shape of one provider request, as recorded metadata.

    ``compacted`` is the caller's answer to "did preparing this call rewrite the
    body", and it is the one fact the fingerprints cannot recover on their own:
    a body can be rewritten to something whose front happens to fingerprint the
    same, and that call still paid full price for its history.
    """
    return {
        "head_fingerprint": head_fingerprint(system_prompt, tools),
        "body_front_fingerprint": body_front_fingerprint(messages),
        "body_messages": len(messages),
        "body_compacted": bool(compacted),
    }


__all__ = ["body_front_fingerprint", "describe_payload", "head_fingerprint"]
