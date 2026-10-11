"""``llm`` is provided by a plugin, and this is what that has to keep true.

The model adapter is the seam every other participant declares -- the agent loop
injects it, sub-agents inherit it -- but it was built by the composition root and
dropped into the component mapping, so "replace the model adapter" meant editing
``bootstrap``.  These tests pin the three properties that make the move real
rather than cosmetic:

* the plugin builds the routing transport and provides it, together with the
  process-lifetime client cache the per-web-session rebuilds share;
* the real composition resolves ``llm`` from that plugin, and the agent it
  injects into holds the same object;
* a profile can name another plugin for ``llm`` -- which drops this one -- so the
  seam is usable by a deployment, not just present.
"""

from __future__ import annotations

import pytest

from agent.core.transport import RoutingTransport
from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry
from agent.plugins.core import core_plugins
from agent.core.model_plugin import ModelPlugin

REPLACEMENT = {"llm": "acme.model"}


def _cfg() -> dict:
    """A config with one usable provider and one that cannot be built.

    The unreachable provider is deliberate: "the composition comes up and reports
    the provider it cannot reach" is behaviour that used to live in
    ``bootstrap``'s startup output and moved into the plugin with the transport.
    """
    from test_agent_integration import _minimal_cfg

    cfg = _minimal_cfg()
    cfg["providers"]["gone"] = {
        "api_format": "no-such-format",
        "default_model": "gone-model",
        "max_tokens": 10,
    }
    return cfg


def _mount(mapping: dict) -> PluginHost:
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(ModelPlugin())
    return host


def test_the_plugin_provides_the_transport_and_the_shared_cache() -> None:
    mapping: dict = {"cfg": _cfg(), "client": object(), "api_format": "openai"}

    host = _mount(mapping)

    assert host.pending() == (), "the composition provides everything it injects"
    assert isinstance(mapping["llm"], RoutingTransport)
    assert isinstance(mapping["provider_client_cache"], dict)
    assert "gone" in mapping["llm"].unavailable, "and it reports what it could not build"


def test_it_reports_an_unreachable_provider_by_name(capsys) -> None:
    mapping: dict = {"cfg": _cfg(), "client": object(), "api_format": "openai"}

    _mount(mapping)

    out = capsys.readouterr().out
    assert "Provider 'gone' unavailable" in out
    assert "no-such-format" in out


def test_the_cache_it_provides_is_the_one_reused_by_a_later_transport() -> None:
    """The point of providing the cache: a rebuild must not make a second client.

    A transport built against the same cache and the same provider configuration
    reuses the client instead of opening another connection pool -- which is what
    the per-web-session rebuilds rely on.  The cache key includes the credential,
    so a config edit yields a fresh client rather than a stale one.
    """
    from agent.core.transport import build_routing_transport

    mapping: dict = {"cfg": _cfg(), "client": object(), "api_format": "openai"}
    _mount(mapping)
    cache = mapping["provider_client_cache"]
    made: list[str] = []

    def factory(provider_cfg: dict, api_format: str) -> object:
        made.append(str(provider_cfg.get("api_key")))
        return object()

    cfg = {
        "active_provider": "fake",
        "providers": {
            "fake": {"api_format": "openai"},
            "other": {
                "api_format": "openai",
                "api_key": "k1",
                "models": ["other-model"],
            },
        },
    }
    build_routing_transport(cfg, "openai", None, factory, client_cache=cache)
    build_routing_transport(cfg, "openai", None, factory, client_cache=cache)

    assert made == ["k1"], "the second transport reused the cached client"


def test_the_real_composition_gets_llm_from_the_plugin(monkeypatch, tmp_path) -> None:
    import asyncio

    import agent as agent_module
    from agent.runtime.contracts import resolve

    cfg = _cfg()
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg, announce=True: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = asyncio.run(agent_module._build_components_async(cfg, announce=False))

    llm = resolve(components, "llm")
    assert isinstance(llm, RoutingTransport)
    assert components["plugin_catalog"].who_provides("llm") == "model"
    assert isinstance(components["provider_client_cache"], dict)
    # The name is not decoration: the agent that declares the seam holds this
    # object.  ``endpoint_for`` is the agent's public read of its transport, and it
    # answers from the routing table only if the two are the same object.
    assert components["agent"].endpoint_for("fake-model") == llm.endpoint_for("fake-model")
    components["plugin_catalog"].close()


def test_a_profile_can_name_another_plugin_for_llm() -> None:
    names = [plugin.name for plugin in core_plugins(replacements=REPLACEMENT)]

    assert "model" not in names, "the plugin it displaces goes"
    assert "agent" in names, "and everything that injects llm stays"

    from agent.plugins.profiles import PROFILES

    assert "llm" not in {
        service
        for profile in PROFILES.values()
        for service in profile.service_replacements
    }, "the shipped profiles do not replace it; the mechanism is what is being pinned"
