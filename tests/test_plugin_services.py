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

            def setup(self, ctx):
                self.ctx = ctx
                self.seen = ctx.resolve("greeter", default=None)
                ctx.on_unwind(lambda: None)

        return Consumer()
'''

CONSUMER_BEFORE_PROVIDER = '''
    def register():
        class Early:
            name = "consumer"

            def setup(self, ctx):
                self.seen = ctx.resolve("greeter", default="absent")

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
    from agent.runtime.services import MissingServiceError

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


def test_the_consumer_is_untouched_by_the_provider_unloading(plugins: Path) -> None:
    catalog, _ = _load(plugins)
    try:
        catalog.unload_plugin("provider")
        assert "consumer" in catalog._plugins
    finally:
        catalog.close()


# ── Ordering, stated rather than assumed ─────────────────────────────────────


def test_a_consumer_loaded_before_the_provider_does_not_see_it(tmp_path: Path) -> None:
    """Plugins load in name order, and there is no re-resolution after the fact.

    Recorded rather than fixed: deferred activation -- a plugin that waits for a
    service and starts when it appears -- is the ``inject`` mechanism, which this
    task does not yet build.  Until then the ordering rule is real and a plugin
    author has to know it, so it is asserted here instead of left implicit.
    """
    root = tmp_path / "plugins"
    _write_plugin(root / "aaa-consumer", CONSUMER_BEFORE_PROVIDER)
    _write_plugin(root / "zzz-provider", PROVIDER)

    catalog, loaded = _load(root)
    try:
        assert loaded == ["consumer", "provider"], "loaded in directory order"
        assert _plugin(catalog, "consumer").seen == "absent"
    finally:
        catalog.close()


# ── A provided service survives a replacement declaration ────────────────────


def test_providing_twice_without_replace_is_refused(plugins: Path) -> None:
    """Two plugins claiming one name is a configuration error, not a race."""
    from agent.runtime.services import ServiceRegistry

    registry = ServiceRegistry()
    registry.provide("mine", object())
    with pytest.raises(ValueError):
        registry.provide("mine", object())
