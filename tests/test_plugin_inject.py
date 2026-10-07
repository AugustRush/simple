"""``inject``: a plugin declares what it needs and waits until it exists.

Before this, a plugin that needed something another plugin provided had to be
loaded *after* it.  That rule was real and undocumented, and the previous round
recorded it as a test rather than fixing it:

    test_a_consumer_loaded_before_the_provider_does_not_see_it

That test now goes the other way.  ``inject`` is the mechanism the original
proposal called for -- a plugin stays pending until its services exist, and
activates when they appear, which is what makes load order stop mattering.

The counterpart is deactivation: when a provider unloads, the plugins that
declared a dependency on it must stop too, or they are left holding a service
whose owner is gone.
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
            return "hello"


    def register():
        class Provider:
            name = "provider"

            def setup(self, ctx):
                ctx.provide("greeter", Greeter())

        return Provider()
'''

#: Loads *before* the provider by directory name, and still gets the service.
INJECTING = '''
    def register():
        class Consumer:
            name = "consumer"
            inject = ("greeter",)

            def __init__(self):
                self.activated = False
                self.greeter = None

            def setup(self, ctx):
                self.activated = True
                self.greeter = ctx.get("greeter")

        return Consumer()
'''

NEEDS_TWO = '''
    def register():
        class Two:
            name = "two"
            inject = ("greeter", "counter")

            def setup(self, ctx):
                self.saw = (ctx.get("greeter"), ctx.get("counter"))

        return Two()
'''

COUNTER = '''
    def register():
        class Counter:
            name = "counter"

            def setup(self, ctx):
                ctx.provide("counter", object())

        return Counter()
'''

#: A plugin whose setup raises, to check a failed activation is not retried.
BROKEN_SETUP = '''
    def register():
        class Broken:
            name = "broken"
            inject = ("greeter",)

            def setup(self, ctx):
                raise RuntimeError("setup exploded")

        return Broken()
'''


def _root(tmp_path: Path, **plugins: str) -> Path:
    root = tmp_path / "plugins"
    for name, code in plugins.items():
        _write_plugin(root / name.replace("_", "-"), code)
    return root


def _load(root: Path):
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=root)
    loaded = catalog.discover_and_load()
    return catalog, loaded


def _plugin(catalog, name):
    return catalog._plugins[name][0]


# ── The ordering rule is gone ────────────────────────────────────────────────


def test_a_plugin_that_injects_activates_after_its_provider(tmp_path: Path) -> None:
    """Load order no longer matters -- this is the whole point.

    ``aaa-consumer`` sorts before ``zzz-provider``, so the consumer is reached
    first and must wait.
    """
    root = _root(tmp_path, aaa_consumer=INJECTING, zzz_provider=PROVIDER)
    catalog, loaded = _load(root)
    try:
        # Activation order, not directory order: the consumer is reached first
        # but cannot load until the provider offers ``greeter``.
        assert sorted(loaded) == ["consumer", "provider"], "both are loaded"
        consumer = _plugin(catalog, "consumer")
        assert consumer.activated is True, "it must not be left pending"
        assert consumer.greeter.greet() == "hello"
    finally:
        catalog.close()


def test_a_plugin_injecting_nothing_still_activates_immediately(tmp_path: Path) -> None:
    root = _root(tmp_path, plain=PROVIDER)
    catalog, _ = _load(root)
    try:
        assert "provider" in catalog._plugins
    finally:
        catalog.close()


def test_a_plugin_waits_for_all_of_its_dependencies(tmp_path: Path) -> None:
    """Half-satisfied is not satisfied."""
    root = _root(
        tmp_path,
        aaa_two=NEEDS_TWO,
        mmm_provider=PROVIDER,  # provides greeter
        zzz_counter=COUNTER,  # provides counter, last
    )
    catalog, _ = _load(root)
    try:
        two = _plugin(catalog, "two")
        assert two.saw[0].greet() == "hello"
        assert two.saw[1] is not None
    finally:
        catalog.close()


# ── Still pending is a state, not a failure ──────────────────────────────────


def test_a_plugin_whose_dependency_never_appears_stays_pending(tmp_path: Path) -> None:
    """Not an error: the composition simply does not supply it.

    A plugin that is pending has run no ``setup`` and registered nothing, so
    leaving it out is the honest outcome -- and it must be *visible*, so the
    catalog reports it.
    """
    root = _root(tmp_path, aaa_consumer=INJECTING)  # no provider at all
    catalog, loaded = _load(root)
    try:
        assert loaded == [], "nothing activated, so nothing is loaded"
        assert catalog.pending_plugins() == ["consumer"]
        assert "consumer" not in catalog._plugins
    finally:
        catalog.close()


def test_a_pending_plugin_registers_nothing(tmp_path: Path) -> None:
    """Its ``setup`` has not run, so it cannot have contributed anything."""
    root = _root(tmp_path, aaa_consumer=INJECTING)
    catalog, _ = _load(root)
    try:
        assert catalog.get_slash_commands() == {}
    finally:
        catalog.close()


def test_a_failed_setup_after_activation_is_not_retried(tmp_path: Path) -> None:
    """An exploding ``setup`` fails once, and is reported rather than looping."""
    root = _root(tmp_path, aaa_broken=BROKEN_SETUP, zzz_provider=PROVIDER)
    catalog, loaded = _load(root)
    try:
        assert "broken" not in loaded
        assert "broken" not in catalog.pending_plugins(), "not retried forever"
    finally:
        catalog.close()


# ── Deactivation ─────────────────────────────────────────────────────────────


def test_unloading_a_provider_deactivates_its_dependents(tmp_path: Path) -> None:
    """Otherwise a dependent holds a service whose owner is gone.

    The dependent's registrations belong to a scope that is still open, so the
    only honest end state is that it deactivates too.
    """
    root = _root(tmp_path, aaa_consumer=INJECTING, zzz_provider=PROVIDER)
    catalog, _ = _load(root)
    try:
        assert _plugin(catalog, "consumer").activated is True

        catalog.unload_plugin("provider")

        assert "consumer" not in catalog._plugins, "the dependent went with it"
        assert catalog.resolve_service("greeter", default=None) is None
    finally:
        catalog.close()


def test_an_independent_plugin_survives_that_unload(tmp_path: Path) -> None:
    root = _root(
        tmp_path,
        aaa_consumer=INJECTING,
        mmm_independent=COUNTER,
        zzz_provider=PROVIDER,
    )
    catalog, _ = _load(root)
    try:
        catalog.unload_plugin("provider")
        assert "counter" in catalog._plugins, "it injected nothing"
    finally:
        catalog.close()


def test_injecting_a_missing_service_is_reported_not_silently_skipped(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, aaa_consumer=INJECTING)
    catalog, _ = _load(root)
    try:
        report = catalog.pending_report()
        assert "consumer" in report
        assert "greeter" in report["consumer"], "it names what it is waiting for"
    finally:
        catalog.close()
