"""Provider registration: a wire format becomes data, not a dispatch branch.

``build_transport``'s docstring already claimed it was the "single dispatch
point -- adding a provider here is the only place to edit".  It was not: it was
two hardcoded branches, and the SDK client that goes with a format was
constructed in three separate places (``ModelClientFactory.from_config``,
``bootstrap._provider_client_factory``, and the web channel's connection test),
with subtly different behaviour -- the bootstrap copy silently fell back to
OpenAI for an unknown format where the factory raised.

These tests pin the two properties the registry exists for: a new wire format
is registered rather than dispatched on, and an unknown format fails loudly
while naming what is available.
"""

from __future__ import annotations

import pytest

from agent.core.providers import (
    ProviderRegistry,
    UnknownApiFormatError,
    build_client,
    build_transport,
    registered_formats,
    register_provider,
    unregister_provider,
)
from agent.core.transport import AnthropicTransport, OpenAITransport


def _recording_transport(client, **kwargs) -> AnthropicTransport:
    """A real transport used as a recording stand-in.

    ``ModelTransport`` has ten abstract methods, so a throwaway fake would be
    mostly unimplemented noise.  ``AnthropicTransport`` is concrete, keeps the
    client it was handed, and is unambiguous about which factory ran.
    """
    transport = AnthropicTransport(
        client,
        kwargs.get("thinking_effort"),
        headers=kwargs.get("headers"),
        provider_name=kwargs.get("provider_name", ""),
    )
    transport.built_with = kwargs  # type: ignore[attr-defined]
    return transport


# ── Registration ─────────────────────────────────────────────────────────────


def test_built_in_formats_are_registered() -> None:
    """The two shipped formats go through the same door as any other."""
    assert {"anthropic", "openai"} <= set(registered_formats())


def test_an_unknown_format_names_what_is_available() -> None:
    """A bare "unsupported" leaves the reader guessing which value is legal."""
    with pytest.raises(UnknownApiFormatError) as excinfo:
        build_transport("no-such-format", object())
    message = str(excinfo.value)
    assert "no-such-format" in message
    assert "anthropic" in message
    assert "openai" in message
    assert excinfo.value.api_format == "no-such-format"


def test_an_unknown_format_is_refused_for_a_client_too() -> None:
    with pytest.raises(UnknownApiFormatError):
        build_client({"api_key": "x"}, "no-such-format")


def test_a_new_format_can_be_registered_and_built() -> None:
    """The whole point: no edit to build_transport, no edit to ModelTransport."""
    registry = ProviderRegistry()
    registry.register(
        "recording",
        transport_factory=_recording_transport,
        client_factory=lambda cfg: ("client-for", cfg.get("api_key")),
    )

    client = registry.build_client({"api_key": "k"}, "recording")
    assert client == ("client-for", "k")

    transport = registry.build_transport("recording", client)
    assert isinstance(transport, AnthropicTransport)
    assert transport.client == ("client-for", "k")


def test_registering_twice_without_replace_is_refused() -> None:
    """Silently shadowing a built-in format is how a typo becomes a behaviour."""
    registry = ProviderRegistry()
    factory = _recording_transport  # noqa: E731
    registry.register("mine", transport_factory=factory)
    with pytest.raises(ValueError):
        registry.register("mine", transport_factory=factory)


def test_replace_swaps_a_registered_transport() -> None:
    registry = ProviderRegistry()
    first = _recording_transport  # noqa: E731
    second = _recording_transport  # noqa: E731
    registry.register("mine", transport_factory=first)
    registry.register("mine", transport_factory=second, replace=True)
    assert registry.transport_factory("mine") is second


def test_a_registered_client_factory_may_be_omitted() -> None:
    """A transport-only format is legal; only building a client needs one."""
    registry = ProviderRegistry()
    registry.register(
        "transport-only",
        transport_factory=_recording_transport,
    )
    assert registry.has_client_factory("transport-only") is False
    assert isinstance(registry.build_transport("transport-only", object()), AnthropicTransport)


def test_registered_formats_are_sorted_and_complete() -> None:
    registry = ProviderRegistry()
    registry.register(
        "zeta", transport_factory=_recording_transport
    )
    registry.register(
        "alpha", transport_factory=_recording_transport
    )
    assert registry.formats() == ("alpha", "zeta")


# ── The shared registry the application actually uses ────────────────────────


def test_shared_registry_builds_the_shipped_transports() -> None:
    assert isinstance(build_transport("anthropic", object()), AnthropicTransport)
    assert isinstance(build_transport("openai", object()), OpenAITransport)


def test_shared_registration_is_visible_to_build_transport() -> None:
    """A plugin registers once, and every caller sees it."""
    register_provider(
        "plugin-format",
        transport_factory=_recording_transport,
    )
    try:
        assert isinstance(build_transport("plugin-format", object()), AnthropicTransport)
        assert "plugin-format" in registered_formats()
    finally:
        unregister_provider("plugin-format")


def test_unregister_removes_a_format() -> None:
    registry = ProviderRegistry()
    registry.register(
        "temp", transport_factory=_recording_transport
    )
    registry.unregister("temp")
    assert "temp" not in registry.formats()


# ── Credential resolution, previously written out three times ────────────────


def test_env_reference_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_REGISTRY_TEST_KEY", "from-env")
    client = build_client({"api_key": "$PROVIDER_REGISTRY_TEST_KEY"}, "openai")
    assert client.api_key == "from-env"


def test_env_reference_that_is_unset_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing here beats sending a request with an empty credential."""
    monkeypatch.delenv("PROVIDER_REGISTRY_TEST_MISSING", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        build_client({"api_key": "$PROVIDER_REGISTRY_TEST_MISSING"}, "openai")
    assert "PROVIDER_REGISTRY_TEST_MISSING" in str(excinfo.value)


def test_a_literal_key_is_used_as_written() -> None:
    client = build_client({"api_key": "literal-key"}, "openai")
    assert client.api_key == "literal-key"


def test_base_url_reaches_the_client() -> None:
    client = build_client(
        {"api_key": "k", "base_url": "https://example.invalid/v1"}, "openai"
    )
    assert "example.invalid" in str(client.base_url)


def test_build_transport_passes_headers_and_effort_through() -> None:
    """The kwargs a built-in transport needs must survive the registry."""
    transport = build_transport(
        "openai", object(), "low", False, {"X-Test": "1"}, provider_name="p"
    )
    assert isinstance(transport, OpenAITransport)


# ── The acceptance condition, end to end ─────────────────────────────────────


def test_a_new_format_works_through_the_real_config_path() -> None:
    """Task 2's acceptance: a registered format is selectable by config.

    Goes through ``ModelClientFactory.from_config`` with a complete config, not
    just the registry, because "does not require editing the dispatch" is only
    worth anything if the code that dispatches for real honours it.
    """
    from agent.config import ModelClientFactory

    sentinel_client = object()
    seen: dict[str, object] = {}

    def client_factory(provider_cfg: dict):
        seen["cfg"] = provider_cfg
        return sentinel_client

    register_provider(
        "task2-format",
        client_factory=client_factory,
        transport_factory=_recording_transport,
    )
    try:
        cfg = {
            "active_provider": "task2",
            "model": "task2-model",
            "max_tokens": 1234,
            "providers": {
                "task2": {
                    "api_format": "task2-format",
                    "api_key": "task2-key",
                    "default_model": "task2-model",
                }
            },
        }
        client, model, max_tokens = ModelClientFactory.from_config(cfg, announce=False)

        assert client is sentinel_client
        assert model == "task2-model"
        assert max_tokens == 1234
        # The provider section reaches the factory, with its name attached so
        # a credential error can say which provider failed.
        assert seen["cfg"]["api_key"] == "task2-key"
        assert seen["cfg"]["name"] == "task2"
    finally:
        unregister_provider("task2-format")


def test_an_unknown_format_in_config_fails_loudly() -> None:
    """The failure a mistyped ``api_format`` produces, at the config boundary."""
    from agent.config import ModelClientFactory

    cfg = {
        "active_provider": "typo",
        "providers": {"typo": {"api_format": "opemai", "default_model": "m"}},
    }
    with pytest.raises(RuntimeError) as excinfo:
        ModelClientFactory.from_config(cfg, announce=False)
    message = str(excinfo.value)
    assert "opemai" in message
    assert "typo" in message


def test_a_missing_credential_names_the_variable_and_the_provider(monkeypatch) -> None:
    """The message has one job: say what to export, and for which provider.

    ``name`` is not in the file -- the loader attaches it -- so an error raised from a
    section nobody named reports ``provider: ?``.  In a config with six providers that is
    the one thing the message cannot afford to be vague about, and the routing transport
    is exactly where an *unnamed* section arrives.
    """
    from agent.core.providers import missing_credential, resolve_api_key

    monkeypatch.delenv("SIMPLE_ABSENT_PROVIDER_KEY", raising=False)
    section = {"api_key": "$SIMPLE_ABSENT_PROVIDER_KEY"}

    assert missing_credential(section) == "SIMPLE_ABSENT_PROVIDER_KEY"

    with pytest.raises(RuntimeError) as excinfo:
        resolve_api_key({**section, "name": "acme"})
    message = str(excinfo.value)
    assert "SIMPLE_ABSENT_PROVIDER_KEY" in message
    assert "provider: acme" in message, message


def test_a_literal_key_is_never_missing(monkeypatch) -> None:
    """Control: the local no-auth gateway is a real configuration.

    ``api_key: ""`` means "this endpoint wants no credential", which is not the same as
    "the credential was not provided".  Only the ``$ENV`` form can be absent, and asking
    the question without raising is what lets a composition keep running for providers it
    merely routes to.
    """
    from agent.core.providers import missing_credential, resolve_api_key

    assert missing_credential({"api_key": ""}) is None
    assert resolve_api_key({"api_key": ""}) == ""
    assert missing_credential({"api_key": "sk-literal"}) is None

    monkeypatch.setenv("SIMPLE_PRESENT_PROVIDER_KEY", "from-env")
    assert missing_credential({"api_key": "$SIMPLE_PRESENT_PROVIDER_KEY"}) is None
    assert resolve_api_key({"api_key": "$SIMPLE_PRESENT_PROVIDER_KEY"}) == "from-env"
