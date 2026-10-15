"""The model adapter as a plugin: it provides ``llm``.

Why this is a plugin
--------------------
``llm`` is the seam every other participant declares: the agent loop injects it,
sub-agents inherit it, and swapping it is how a deployment changes which provider
answers.  It was nevertheless built by ``bootstrap`` and dropped into the
component mapping, which made "replace the model adapter" a patch to the
composition root rather than a plugin that provides the name.

What it builds
--------------
A ``RoutingTransport``: the layer that routes each call to the provider owning
the requested model, so the UI's model list can offer every configured provider
while the active one stays the default.  Two things travel with it and are
provided as well, because they are process-lifetime and shared with the per-web-
session rebuilds:

``provider_client_cache``
    One SDK client per provider configuration for the whole process.  SDK clients
    own connection pools; rebuilding one per session leaks a pool per session per
    provider.  Web sessions rebuild their transport whenever a session runtime is
    created, so they resolve this cache rather than making their own.

It reads three names that the composition provides before any plugin mounts --
``cfg``, ``client`` and ``api_format`` -- so it is activated by the kernel as soon
as it is mounted.  ``client`` in particular is the client the composition already
built for the active provider: the transport reuses it for the default path, so
the routed and unrouted paths share one connection pool.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ModelPlugin"]


class ModelPlugin:
    """Provides ``llm`` (the routing transport) and ``provider_client_cache``."""

    name = "model"

    #: The client and the wire format the composition already chose, plus the
    #: config the rest of the routing table is read from.
    inject = ("cfg", "client", "api_format")

    #: A hand-built mapping may omit it; absence means "trust the inherited client".
    optional = ("client_cache_key",)

    def setup(self, ctx: Any) -> None:
        from agent import shared
        from agent.core.providers import provider_client_factory
        from agent.core.transport import build_routing_transport

        cfg = ctx.get("cfg", None) or {}
        client = ctx.get("client")
        api_format = ctx.get("api_format")
        # ``None`` when the composition never recorded it (a hand-built mapping),
        # which means "trust the inherited client" -- the same behaviour as before
        # this became a plugin.
        default_client_cache_key = ctx.get("client_cache_key", None) or None

        client_cache: dict = {}
        transport = build_routing_transport(
            cfg,
            api_format,
            client,
            client_factory=provider_client_factory,
            client_cache=client_cache,
            default_client_cache_key=default_client_cache_key,
        )

        # Say which providers could not be built, at the one moment the user is
        # looking at startup output.  The composition proceeds without them (a
        # provider it merely routes to must not be a startup prerequisite -- see
        # ``build_routing_transport``), so without this the first sign would be a
        # call that fails, or a model that answers from the wrong endpoint.
        for name, reason in sorted(transport.unavailable.items()):
            shared.CONSOLE.print(
                f"[yellow]Provider '{name}' unavailable: {reason}[/yellow] "
                "[dim]— its models stay unroutable until this is fixed[/dim]"
            )

        ctx.provide("provider_client_cache", client_cache)
        ctx.provide("llm", transport)
