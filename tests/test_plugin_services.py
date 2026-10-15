"""A plugin provides a service; another plugin resolves it; unloading takes it back.

This is the difference between the contribution surface being *enumerated* and
being *declarative*.  ``PluginContext`` used to offer exactly five ``add_*``
methods -- a slash command, a skill root, an MCP config, an agent definition, a
tool -- so a plugin could only fill slots the catalog already knew about.  A
plugin could not hand anything *to other plugins*, which is what makes a plugin
tree a tree rather than a list.

The registration primitive already existed (``ServiceScope``, P1); what was
missing was a way for a plugin to reach it, and a way for a reader to resolve
what another plugin provided.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write_plugin(plugin_dir: Path, code: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")


PROVIDER = '''
    class Greeter:
        def greet(self):
            return "hello from the provider"


    def register():
        class Provider:
            name = "provider"

            def setup(self, ctx):
                self.ctx = ctx
                ctx.provide("greeter", Greeter())

            def teardown(self):
                self.torn_down = True

        return Provider()
'''

CONSUMER = '''
    def register():
        class Consumer:
            name = "consumer"
            inject = ("greeter",)

            def setup(self, ctx):
                self.ctx = ctx
                self.seen = ctx.get("greeter", default=None)
                ctx.on_unwind(lambda: None)

        return Consumer()
'''

#: Declares nothing and reads ``greeter`` anyway.  This is the case the enforced
#: contract refuses: the read is a dependency the kernel cannot see, so it is
#: refused at setup rather than allowed to hold a name whose owner it cannot be
#: told about when that owner is replaced or unloaded.
CONSUMER_WITHOUT_DECLARATION = '''
    def register():
        class Early:
            name = "consumer"

            def setup(self, ctx):
                self.seen = ctx.get("greeter", default="absent")

        return Early()
'''


@pytest.fixture()
def plugins(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    _write_plugin(root / "aaa-provider", PROVIDER)
    _write_plugin(root / "zzz-consumer", CONSUMER)
    return root


def _load(root: Path):
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=root)
    loaded = catalog.discover_and_load()
    return catalog, loaded


def _plugin(catalog, name):
    plugin, _meta = catalog._plugins[name]
    return plugin


# ── A plugin can provide ─────────────────────────────────────────────────────


def test_a_plugin_can_provide_a_service(plugins: Path) -> None:
    catalog, loaded = _load(plugins)
    try:
        assert "provider" in loaded
        assert catalog.resolve_service("greeter") is not None
    finally:
        catalog.close()


def test_another_plugin_can_resolve_it(plugins: Path) -> None:
    """The point: a plugin hands something to other plugins, not only to tools."""
    catalog, _ = _load(plugins)
    try:
        consumer = _plugin(catalog, "consumer")
        assert consumer.seen is not None
        assert consumer.seen.greet() == "hello from the provider"
    finally:
        catalog.close()


def test_a_plugin_providing_nothing_resolves_to_absent(plugins: Path) -> None:
    catalog, _ = _load(plugins)
    try:
        assert catalog.resolve_service("nothing.here", default="absent") == "absent"
    finally:
        catalog.close()


def test_resolving_a_missing_service_without_a_default_raises(plugins: Path) -> None:
    """A silent None is how a missing dependency becomes a puzzle three frames away."""
    from agent.kernel.service import MissingServiceError

    catalog, _ = _load(plugins)
    try:
        with pytest.raises(MissingServiceError):
            catalog.resolve_service("nothing.here")
    finally:
        catalog.close()


# ── Unloading takes it back ──────────────────────────────────────────────────


def test_unloading_the_provider_removes_the_service(plugins: Path) -> None:
    """Registration is an effect: the provider's scope owns it."""
    catalog, _ = _load(plugins)
    try:
        assert catalog.resolve_service("greeter") is not None
        catalog.unload_plugin("provider")
        assert catalog.resolve_service("greeter", default=None) is None
    finally:
        catalog.close()


def test_unloading_the_provider_takes_the_dependent_consumer_with_it(plugins: Path) -> None:
    """A declared dependency is a real edge, so the cascade follows it.

    This is the *reason* to declare: ``inject`` is what ``unload``/``replace`` walk
    to find the plugins holding a service that is going away.  Before the
    declaration was enforced, the consumer below had no edge and was left holding
    ``greeter`` -- which is the failure this asserts against.
    """
    catalog, _ = _load(plugins)
    try:
        assert "consumer" in catalog._plugins
        catalog.unload_plugin("provider")
        assert "consumer" not in catalog._plugins, (
            "the consumer declared the dependency, so it goes with its provider"
        )
    finally:
        catalog.close()


# ── Ordering, stated rather than assumed ─────────────────────────────────────


def test_reading_without_declaring_is_refused(tmp_path: Path) -> None:
    """The contract is enforced, not merely documented.

    ``ctx.resolve`` at setup time used to be eager and forgiving: a plugin that
    declared nothing read whatever happened to exist, got the absent default, and
    held a service nobody could take away from it.  The declaration is now what
    makes the dependency graph complete, so an undeclared read is a *refusal*, and
    the refusal names the plugin and the fix.
    """
    root = tmp_path / "plugins"
    _write_plugin(root / "aaa-consumer", CONSUMER_WITHOUT_DECLARATION)
    _write_plugin(root / "zzz-provider", PROVIDER)

    catalog, loaded = _load(root)
    try:
        assert loaded == ["provider"], "the consumer was refused, so it never loaded"
        failures = catalog.failed_report()
        assert "consumer" in failures
        assert "without declaring it" in failures["consumer"]
    finally:
        catalog.close()


# ── A provided service survives a replacement declaration ────────────────────


def test_providing_twice_without_replace_is_refused(plugins: Path) -> None:
    """Two plugins claiming one name is a configuration error, not a race."""
    from agent.kernel.service import ServiceRegistry

    registry = ServiceRegistry()
    registry.provide("mine", object())
    with pytest.raises(ValueError):
        registry.provide("mine", object())


# ── The model adapter is a service ───────────────────────────────────────────


class _Adapter:
    """A stand-in model transport: the seam already existed as an ABC."""

    api_format = "anthropic"

    def endpoint_for(self, model):
        return f"endpoint:{model}"

    async def create(self, **kwargs):
        return "answered by the composition's adapter"

    def parse_response(self, *args, **kwargs):
        return ("end_turn", "text", [])

    def has_incomplete_tool_calls(self, *args, **kwargs):
        return False


class _Own:
    api_format = "anthropic"

    def endpoint_for(self, model):
        return "the agent's own"


def test_an_agent_uses_the_adapter_its_composition_named() -> None:
    """``llm`` is the plan's model plugin seam.

    ``ModelTransport`` was already an ABC with three implementations, so the
    *interface* seam existed.  What did not is that a composition could choose one
    without editing the code that builds the agent -- which is the difference
    between a dependency that is injected and one that is *named*.
    """
    from agent.core.agent import BaseAgent

    replacement = _Adapter()
    # `services` is the seam and it wins over the argument; what changed is its *width*.
    # It used to be handed the whole component mapping, which made "what does the agent
    # resolve by name" unanswerable; `bootstrap` now passes exactly the one name this
    # class looks up.
    agent = BaseAgent(
        client=object(),
        registry=object(),
        model="m",
        api_format="anthropic",
        transport=_Own(),
        services={"llm": replacement},
    )
    assert agent._transport is replacement
    assert agent._transport.endpoint_for("m") == "endpoint:m"


def test_an_agent_without_a_composition_keeps_the_adapter_it_was_given() -> None:
    """The one escape hatch, and it is explicit: an agent built outside a
    composition has no service to resolve and is not silently given one."""
    from agent.core.agent import BaseAgent

    own = _Own()
    agent = BaseAgent(
        client=object(), registry=object(), model="m", transport=own,
    )
    assert agent._transport is own
