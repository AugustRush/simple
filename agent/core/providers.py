"""Provider registration: wire formats as data rather than dispatch branches.

Why this module exists
----------------------
``agent/core/transport.py`` already states the target in its own docstring:
every ``if api_format == "anthropic" else openai`` belongs behind the
``ModelTransport`` interface, "so adding a new provider format is a single new
``ModelTransport`` subclass and zero edits to the agent loop".  That held for
the agent loop and not for the factory: ``build_transport`` was two hardcoded
branches, and the SDK client that goes with a format was built in three
separate places --

- ``ModelClientFactory.from_config`` (``agent/config.py``),
- ``bootstrap._provider_client_factory`` (``agent/bootstrap.py``), and
- the web channel's connection test (``agent/channels/web.py``),

-- with behaviour that had already drifted: the bootstrap copy fell through to
OpenAI for an unrecognised format where the factory raised.

So this module is the missing half of that boundary.  A format registers a
client factory and a transport factory; the two built-in formats register
through the same call an extension would use, which is what keeps the
extension path honest.  Nothing here imports the agent loop, the session, or
the tool registry: a provider is a leaf.

Scope
-----
The registry is process-wide, deliberately, because a wire format is a
property of the process rather than of a session -- the same reasoning that
keeps ``_DEFAULT_PROVIDER`` in ``agent/exec/subprocess.py`` process-wide.  It
is recorded in ``agent/runtime/state_ledger.py`` for that reason, and Task 9
is where process-wide layout state moves behind configuration.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from agent.core.transport import (
    AnthropicTransport,
    ModelTransport,
    OpenAITransport,
)

__all__ = [
    "ProviderRegistry",
    "UnknownApiFormatError",
    "build_client",
    "build_transport",
    "register_provider",
    "registered_formats",
    "unregister_provider",
]

#: Builds one SDK client from a provider config section.
ClientFactory = Callable[[dict], Any]
#: Builds one transport from a client plus the keyword arguments
#: :func:`build_transport` accepts.  Declared with ``**kwargs`` rather than the
#: five names because a format is entitled to ignore the ones that are not its
#: own -- ``stream_usage`` reaches only OpenAI, for instance.
TransportFactory = Callable[..., ModelTransport]


class UnknownApiFormatError(ValueError):
    """Raised when no provider is registered for a wire format.

    Carries the format and the alternatives, because the two questions a
    reader has -- "what did I ask for" and "what may I ask for" -- are both
    answerable here and guessing between them is the slow path.
    """

    def __init__(self, api_format: str, available: tuple[str, ...]) -> None:
        known = ", ".join(available) if available else "(none registered)"
        super().__init__(
            f"unsupported api_format: {api_format!r}; registered formats: {known}"
        )
        self.api_format = api_format
        self.available = available


def resolve_api_key(provider_cfg: dict) -> str:
    """The credential a provider config names, with ``$ENV`` indirection.

    Written once because it was written three times: ``"$NAME"`` reads the
    environment and a missing value is an error rather than an empty string,
    since an unauthenticated request is a worse failure than a refused start.
    Anything else is the literal key, including the empty string that a
    local, no-auth gateway legitimately uses.
    """
    raw = str(provider_cfg.get("api_key", "") or "")
    if not raw.startswith("$"):
        return raw
    env_name = raw[1:]
    value = os.environ.get(env_name, "")
    if not value:
        raise RuntimeError(
            f"API key env var {env_name!r} not set "
            f"(provider: {provider_cfg.get('name') or '?'}). "
            f"Run: export {env_name}=..."
        )
    return value


def _anthropic_client(provider_cfg: dict) -> Any:
    import anthropic

    kwargs: dict[str, Any] = {"api_key": resolve_api_key(provider_cfg)}
    base_url = provider_cfg.get("base_url")
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.AsyncAnthropic(**kwargs)


def _openai_client(provider_cfg: dict) -> Any:
    try:
        import openai as openai_lib
    except ImportError:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "openai package not installed. Run: pip install openai"
        ) from None
    kwargs: dict[str, Any] = {"api_key": resolve_api_key(provider_cfg)}
    base_url = provider_cfg.get("base_url")
    if base_url:
        kwargs["base_url"] = base_url
    return openai_lib.AsyncOpenAI(**kwargs)


class ProviderRegistry:
    """A wire format -> (client, transport) map.

    Usable standalone for tests and for a caller that wants its own set of
    formats; :data:`SHARED` is the instance the application uses.
    """

    __slots__ = ("_transports", "_clients")

    def __init__(self) -> None:
        self._transports: dict[str, TransportFactory] = {}
        self._clients: dict[str, ClientFactory] = {}

    # -- registration -----------------------------------------------------

    def register(
        self,
        api_format: str,
        *,
        transport_factory: TransportFactory,
        client_factory: Optional[ClientFactory] = None,
        replace: bool = False,
    ) -> None:
        """Register one wire format.

        Refuses to shadow an existing format unless *replace* is set: a second
        registration under the same name is either a typo or a deliberate
        override, and those deserve different answers.
        """
        if api_format in self._transports and not replace:
            raise ValueError(
                f"provider format {api_format!r} is already registered; "
                "pass replace=True to override it"
            )
        self._transports[api_format] = transport_factory
        if client_factory is not None:
            self._clients[api_format] = client_factory

    def unregister(self, api_format: str) -> None:
        """Remove a format.  Absent is not an error; unload paths may retry."""
        self._transports.pop(api_format, None)
        self._clients.pop(api_format, None)

    # -- inspection -------------------------------------------------------

    def formats(self) -> tuple[str, ...]:
        return tuple(sorted(self._transports))

    def has_client_factory(self, api_format: str) -> bool:
        return api_format in self._clients

    def transport_factory(self, api_format: str) -> TransportFactory:
        try:
            return self._transports[api_format]
        except KeyError:
            raise UnknownApiFormatError(api_format, self.formats()) from None

    # -- construction -----------------------------------------------------

    def build_client(self, provider_cfg: dict, api_format: str) -> Any:
        try:
            factory = self._clients[api_format]
        except KeyError:
            if api_format not in self._transports:
                raise UnknownApiFormatError(api_format, self.formats()) from None
            raise ValueError(
                f"provider format {api_format!r} has no client factory registered"
            ) from None
        return factory(provider_cfg)

    def build_transport(
        self,
        api_format: str,
        client: Any,
        thinking_effort: Any = None,
        stream_usage: Any = True,
        headers: Optional[dict[str, str]] = None,
        provider_name: str = "",
    ) -> ModelTransport:
        factory = self.transport_factory(api_format)
        return factory(
            client,
            thinking_effort=thinking_effort,
            stream_usage=stream_usage,
            headers=headers,
            provider_name=provider_name,
        )


#: The registry the application uses.  See the module docstring for why this is
#: process-wide and where it is recorded.
SHARED = ProviderRegistry()

SHARED.register(
    "anthropic",
    client_factory=_anthropic_client,
    transport_factory=lambda client, **kw: AnthropicTransport(
        client, kw["thinking_effort"], headers=kw["headers"],
        provider_name=kw["provider_name"],
    ),
)
SHARED.register(
    "openai",
    client_factory=_openai_client,
    transport_factory=lambda client, **kw: OpenAITransport(
        client, kw["thinking_effort"], kw["stream_usage"],
        headers=kw["headers"], provider_name=kw["provider_name"],
    ),
)


# ── Module-level facade over SHARED ──────────────────────────────────────────
# Callers keep the call shape they had when these were hardcoded branches.


def register_provider(
    api_format: str,
    *,
    transport_factory: TransportFactory,
    client_factory: Optional[ClientFactory] = None,
    replace: bool = False,
) -> None:
    """Register a wire format with the application's registry."""
    SHARED.register(
        api_format,
        transport_factory=transport_factory,
        client_factory=client_factory,
        replace=replace,
    )


def unregister_provider(api_format: str) -> None:
    """Remove a wire format from the application's registry."""
    SHARED.unregister(api_format)


def registered_formats() -> tuple[str, ...]:
    return SHARED.formats()


def build_transport(
    api_format: str,
    client: Any,
    thinking_effort: Any = None,
    stream_usage: Any = True,
    headers: Optional[dict[str, str]] = None,
    provider_name: str = "",
) -> ModelTransport:
    """Build the transport for *api_format*.

    The signature is the one ``build_transport`` had in
    ``agent/core/transport.py``, so existing callers are untouched; the body
    is now a lookup instead of a branch.
    """
    return SHARED.build_transport(
        api_format,
        client,
        thinking_effort,
        stream_usage,
        headers,
        provider_name,
    )


def build_client(provider_cfg: dict, api_format: str) -> Any:
    """Build the SDK client for *api_format*."""
    return SHARED.build_client(provider_cfg, api_format)
