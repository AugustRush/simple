"""The kernel: plugins composed, depended on, provided for, observed, and undone.

The plan's sentence is the specification -- *let plugins be composed, depend on each
other, provide services, listen to events, and be unloaded or replaced* -- so these
tests are written against that sentence rather than against the implementation.

Three properties are load-bearing and each has its own group:

- **registrations are effects.**  Everything a plugin registers is undone by
  disposing its scope, so there is no per-kind teardown to keep in sync and "who
  unregisters this?" is not a question the design permits.
- **composition is a plugin loading a plugin.**  The cascade is a consequence of
  the disposal rule, so a child that outlives its parent is not a bug that can
  happen -- it is a state the scope model cannot represent.
- **dependencies wait rather than fail.**  A plugin whose service is absent is
  pending, holds nothing, and activates when the service appears.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agent.kernel.context import Context
from agent.kernel.event import EventBus
from agent.kernel.plugin import Install, MissingNameError, PluginHost
from agent.kernel.service import MissingServiceError


# ── Test plugins ─────────────────────────────────────────────────────────────


class Leaf:
    """Provides one service and subscribes to one event."""

    name = "leaf"
    inject: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.setups = 0
        self.teardowns = 0
        self.seen: list = []

    def setup(self, ctx):
        self.setups += 1
        self.ctx = ctx
        ctx.provide("leaf.value", 42)
        ctx.on(Ping, self.seen.append)

    def teardown(self):
        self.teardowns += 1


@dataclass(frozen=True)
class Ping:
    n: int = 1


@dataclass(frozen=True)
class Pong:
    n: int = 1


class NeedsLeaf:
    name = "needs_leaf"
    inject = ("leaf.value",)

    def __init__(self) -> None:
        self.activated = False
        self.value = None

    def setup(self, ctx):
        self.activated = True
        self.value = ctx.get("leaf.value")


class Parent:
    name = "parent"
    inject: tuple[str, ...] = ()

    def __init__(self, child) -> None:
        self.child = child
        self.ctx = None

    def setup(self, ctx):
        self.ctx = ctx
        ctx.plugin(self.child)


# ── Lifecycle ────────────────────────────────────────────────────────────────


def test_a_plugin_activates_and_its_setup_runs_once() -> None:
    host = PluginHost()
    plugin = Leaf()
    host.load(plugin)
    assert plugin.setups == 1
    assert host.loaded() == ("leaf",)


def test_a_plugin_without_a_name_is_refused() -> None:
    """Identity is not optional: an unnamed plugin cannot be unloaded or depended on."""
    class Anonymous:
        def setup(self, ctx):
            pass

    with pytest.raises(MissingNameError):
        PluginHost().load(Anonymous())


def test_the_same_name_cannot_be_loaded_twice() -> None:
    host = PluginHost()
    host.load(Leaf())
    with pytest.raises(ValueError, match="already loaded"):
        host.load(Leaf())


def test_a_plugin_without_setup_is_fine() -> None:
    """The protocol asks for a name and an optional setup; a marker plugin is legal."""
    class Marker:
        name = "marker"

    host = PluginHost()
    host.load(Marker())
    assert host.loaded() == ("marker",)


def test_a_failed_setup_is_not_left_loaded() -> None:
    class Broken:
        name = "broken"

        def setup(self, ctx):
            ctx.provide("broken.partial", 1)
            raise RuntimeError("setup exploded")

    host = PluginHost()
    install = host.load(Broken())
    assert install.error
    assert host.loaded() == ()
    assert not host.services.has("broken.partial"), "its registrations were undone"


# ── Services: provide, get, replace ──────────────────────────────────────────


def test_a_provided_service_is_resolvable() -> None:
    host = PluginHost()
    host.load(Leaf())
    assert host.services.get("leaf.value") == 42


def test_a_missing_service_raises_without_a_default() -> None:
    """A silent ``None`` becomes an ``AttributeError`` three frames away."""
    host = PluginHost()
    with pytest.raises(MissingServiceError):
        host.services.get("nothing")


def test_providing_twice_is_refused_unless_replacing() -> None:
    """Two plugins claiming one name is a configuration error, not a race."""
    host = PluginHost()
    host.load(Leaf())

    class Thief:
        name = "thief"

        def setup(self, ctx):
            ctx.provide("leaf.value", 99)

    install = host.load(Thief())
    assert install.error, "the second provider failed rather than winning silently"
    assert host.services.get("leaf.value") == 42


def test_a_plugin_can_replace_a_service_explicitly() -> None:
    """Clause ③ of the plan: replacement is a first-class act, not an accident."""
    host = PluginHost()
    host.load(Leaf())

    class Replacement:
        name = "replacement"
        inject = ("leaf.value",)

        def setup(self, ctx):
            ctx.provide("leaf.value", 7, replace=True)

    host.load(Replacement())
    assert host.services.get("leaf.value") == 7


# ── Dependencies ─────────────────────────────────────────────────────────────


def test_a_plugin_waits_for_a_service_that_does_not_exist_yet() -> None:
    """Load order stops mattering: the dependent is reached first and waits."""
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)

    assert dependent.activated is False
    assert host.loaded() == ()
    assert host.pending() == ("needs_leaf",)
    assert host.waiting_for() == {"needs_leaf": ("leaf.value",)}


def test_the_dependent_activates_when_the_service_appears() -> None:
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)
    host.load(Leaf())

    assert dependent.activated is True
    assert dependent.value == 42
    assert set(host.loaded()) == {"leaf", "needs_leaf"}
    assert host.pending() == ()


def test_a_pending_plugin_has_registered_nothing() -> None:
    """There is no half-loaded state to reason about: setup has not run."""
    host = PluginHost()
    host.load(NeedsLeaf())
    assert host.services.names() == ()


def test_a_pending_plugin_does_not_activate_on_an_unrelated_load() -> None:
    """Only a plugin that *provided* something can unblock a waiter."""
    host = PluginHost()

    class Unrelated:
        name = "unrelated"

        def setup(self, ctx):
            ctx.provide("unrelated.thing", 1)

    host.load(NeedsLeaf())
    host.load(Unrelated())
    assert host.pending() == ("needs_leaf",)


def test_a_service_satisfies_a_dependency_supplied_later_in_a_chain() -> None:
    """The flush is iterative: an activated plugin may unblock another."""
    host = PluginHost()
    host.load(NeedsLeaf())

    class Middle:
        name = "middle"
        inject = ("leaf.value",)

        def setup(self, ctx):
            ctx.provide("middle.thing", ctx.get("leaf.value") + 1)

    class Top:
        name = "top"
        inject = ("middle.thing",)

        def setup(self, ctx):
            self.value = ctx.get("middle.thing")

    host.load(Top())
    host.load(Middle())
    host.load(Leaf())
    assert host.pending() == ()
    assert "top" in host.loaded()


# ── Events ───────────────────────────────────────────────────────────────────


def test_a_subscription_is_delivered() -> None:
    host = PluginHost()
    plugin = Leaf()
    host.load(plugin)
    asyncio.run(host.events.emit(Ping()))
    assert plugin.seen == [Ping()]


def test_a_subscription_does_not_see_another_type() -> None:
    host = PluginHost()
    plugin = Leaf()
    host.load(plugin)
    asyncio.run(host.events.emit(Pong()))
    assert plugin.seen == [], "the type is the channel"


def test_ctx_emit_delivers() -> None:
    host = PluginHost()
    plugin = Leaf()
    install = host.load(plugin)
    asyncio.run(host.installs[install.name].plugin.ctx.emit(Ping(n=5)))
    assert plugin.seen == [Ping(n=5)]


def test_a_raising_listener_does_not_stop_the_next() -> None:
    """Mounting a plugin must not break the thing it observes."""
    failures: list = []
    bus = EventBus(on_error=lambda event, exc: failures.append(exc))
    seen: list = []
    bus.on(Ping, lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.on(Ping, seen.append)
    asyncio.run(bus.emit(Ping()))
    assert len(seen) == 1
    assert failures and isinstance(failures[0], RuntimeError)


def test_blocking_delivery_stops_at_the_first_refusal() -> None:
    bus = EventBus()
    later: list = []
    bus.on(Ping, lambda event: "refused")
    bus.on(Ping, seen := later.append)
    assert asyncio.run(bus.emit_blocking(Ping())) == "refused"
    assert later == [], "a gate a later listener can argue with is not a gate"


def test_plain_emit_does_not_stop_at_a_truthy_result() -> None:
    """The two modes are different on purpose; only a declared domain blocks."""
    bus = EventBus()
    later: list = []
    bus.on(Ping, lambda event: "truthy")
    bus.on(Ping, later.append)
    asyncio.run(bus.emit(Ping()))
    assert len(later) == 1


# ── Registrations are effects ────────────────────────────────────────────────


def test_unloading_removes_the_service() -> None:
    host = PluginHost()
    host.load(Leaf())
    host.unload("leaf")
    assert not host.services.has("leaf.value")


def test_unloading_removes_the_subscription() -> None:
    """A listener left behind is called for ever, raising into the isolation handler."""
    host = PluginHost()
    plugin = Leaf()
    host.load(plugin)
    host.unload("leaf")
    asyncio.run(host.events.emit(Ping()))
    assert plugin.seen == []
    assert not host.events.has_listeners(Ping)


def test_unloading_runs_teardown_once() -> None:
    host = PluginHost()
    plugin = Leaf()
    host.load(plugin)
    host.unload("leaf")
    assert plugin.teardowns == 1


def test_unloading_twice_is_harmless() -> None:
    host = PluginHost()
    host.load(Leaf())
    host.unload("leaf")
    host.unload("leaf")  # must not raise


def test_unloading_an_unknown_plugin_raises() -> None:
    with pytest.raises(KeyError, match="no such plugin"):
        PluginHost().unload("ghost")


def test_a_failed_teardown_does_not_strand_the_rest() -> None:
    class BadTeardown:
        name = "bad"

        def setup(self, ctx):
            ctx.provide("bad.thing", 1)

        def teardown(self):
            raise RuntimeError("teardown exploded")

    host = PluginHost()
    host.load(BadTeardown())
    host.unload("bad")
    assert not host.services.has("bad.thing"), "the scope was still disposed"


# ── Composition: a plugin loads a plugin ─────────────────────────────────────


def test_a_plugin_can_load_another() -> None:
    host = PluginHost()
    parent = Parent(Leaf())
    host.load(parent)
    assert set(host.loaded()) == {"parent", "leaf"}


def test_unloading_the_parent_unloads_the_child() -> None:
    """The cascade is the disposal rule, not a special case bolted on."""
    host = PluginHost()
    parent = Parent(Leaf())
    host.load(parent)
    host.unload("parent")
    assert host.loaded() == ()
    assert not host.services.has("leaf.value")


def test_a_child_does_not_outlive_its_parent() -> None:
    """The state is unrepresentable rather than merely handled."""
    host = PluginHost()
    host.load(Parent(Leaf()))
    assert host.installs["leaf"].parent == "parent"


def test_unloading_a_child_leaves_the_parent() -> None:
    host = PluginHost()
    host.load(Parent(Leaf()))
    host.unload("leaf")
    assert host.loaded() == ("parent",)


# ── Replacement of a provider takes its dependents down ──────────────────────


def test_unloading_a_provider_unloads_its_dependents() -> None:
    """Otherwise a dependent holds a service whose owner is gone."""
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)
    host.load(Leaf())
    assert dependent.activated

    host.unload("leaf")
    assert "needs_leaf" not in host.loaded()
    assert not host.services.has("leaf.value")


def test_an_independent_plugin_survives_that_unload() -> None:
    host = PluginHost()

    class Independent:
        name = "independent"

        def setup(self, ctx):
            ctx.provide("independent.thing", 1)

    host.load(Independent())
    host.load(Leaf())
    host.unload("leaf")
    assert "independent" in host.loaded()


# ── Injectable attributes ────────────────────────────────────────────────────


def test_an_injected_name_is_reachable_as_an_attribute() -> None:
    """The alternative is ``plugin.runtime.agent.memory.tool_registry``.

    The shortcut works for names that are valid identifiers, which is the shape a
    service is normally given.
    """
    host = PluginHost()

    class Provider:
        name = "provider"

        def setup(self, ctx):
            ctx.provide("tools", "the tool registry")

    class Consumer:
        name = "consumer"
        inject = ("tools",)

        def setup(self, ctx):
            self.tools = ctx.tools

    consumer = Consumer()
    host.load(consumer)
    host.load(Provider())
    assert consumer.tools == "the tool registry"


def test_a_dotted_injected_name_is_reached_with_get() -> None:
    """A name that is not an identifier cannot be an attribute, and that is fine.

    ``inject = ("leaf.value",)`` is a legitimate service name; the shortcut is a
    convenience for identifiers, not the only way in.  Asserted so the limitation is
    a decision rather than a surprise.
    """
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)
    host.load(Leaf())
    assert dependent.value == 42
    ctx = host.context_for(host.installs["needs_leaf"])
    with pytest.raises(AttributeError):
        ctx.__getattr__("leaf")


def test_an_undeclared_name_is_not_an_attribute() -> None:
    """Reaching for a service you did not declare is what ``inject`` makes visible."""
    host = PluginHost()
    install = host.load(Leaf())
    ctx = host.installs[install.name].plugin.ctx
    with pytest.raises(AttributeError, match="inject"):
        ctx.something_undeclared


def test_a_service_cannot_shadow_a_real_method() -> None:
    """``provide("get", ...)`` must not replace ``Context.get``."""
    host = PluginHost()

    class Shadow:
        name = "shadow"

        def setup(self, ctx):
            ctx.provide("get", "not the method")
            self.result = ctx.get("get")

    plugin = Shadow()
    host.load(plugin)
    assert plugin.result == "not the method"


# ── The whole sentence, in one test ──────────────────────────────────────────


def test_composed_depended_provided_observed_and_replaced() -> None:
    """Everything the kernel is for, exercised as one scenario."""
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)          # depends, so it waits
    assert host.pending() == ("needs_leaf",)

    host.load(Leaf())             # provides, so the waiter activates
    assert dependent.activated and dependent.value == 42

    asyncio.run(host.events.emit(Ping()))
    assert host.installs["leaf"].plugin.seen == [Ping()]

    class Better:
        name = "better"
        inject = ("leaf.value",)

        def setup(self, ctx):
            ctx.provide("leaf.value", 100, replace=True)

    host.load(Better())           # replaced
    assert host.services.get("leaf.value") == 100

    host.unload("better")
    assert host.services.get("leaf.value") == 42, "the replaced value came back"


def test_an_install_carries_what_a_diagnostic_needs() -> None:
    host = PluginHost()
    install = host.load(Leaf())
    assert isinstance(install, Install)
    assert install.name == "leaf"
    assert install.pending is False
    assert install.parent is None


# ── Prerequisites for the catalog migration ──────────────────────────────────


def test_an_install_caches_the_context_it_was_activated_with() -> None:
    """The catalog reads ``install.ctx``; caching it makes the kernel's record a
    drop-in for the catalog's own, rather than something it has to rebuild."""
    host = PluginHost()
    install = host.load(Leaf())
    assert install.ctx is not None
    assert install.ctx.get("leaf.value") == 42


def test_a_pending_install_has_no_context_yet() -> None:
    """No setup ran, so there is no context to have been handed one."""
    host = PluginHost()
    install = host.load(NeedsLeaf())
    assert install.pending is True
    assert install.ctx is None


def test_the_context_exposes_the_bus_for_domain_subscription() -> None:
    """A domain is a set of event types kept *outside* the kernel.

    The kernel therefore cannot offer ``ctx.on_domain("turn", ...)`` -- it would
    have to know what a turn is.  Handing over the bus lets the domain table do
    the subscribing, which is where that vocabulary belongs.
    """
    from agent.core.event_domains import TURN, TurnStarted, TurnFinished

    host = PluginHost()
    install = host.load(Leaf())
    ctx = install.ctx

    seen: list = []
    undo = TURN.subscribe(ctx.bus, lambda event: seen.append(event))
    try:
        asyncio.run(ctx.emit(TurnStarted("a", True, False)))
        asyncio.run(ctx.emit(TurnFinished("a", "ok", 1)))
        assert len(seen) == 2
    finally:
        undo()


def test_a_domain_subscription_through_the_context_is_still_unwound() -> None:
    """The bus is handed over, and ``ctx.on_unwind`` keeps the removal reversible.

    Written first against ``ctx.scope().on_unwind(...)``, which does nothing: a
    nested scope nobody disposes is a silent no-op.  The plugin's own lifetime is
    what it means, so the kernel offers one call for that.

    A plugin that subscribes through the domain table must register the undo on
    its own scope, or unloading leaves a listener behind.  Asserted at the bus so
    the failure is visible rather than silent.
    """
    from agent.core.event_domains import TURN, TurnStarted

    host = PluginHost()

    class Subscriber:
        name = "subscriber"

        def setup(self, ctx):
            self.undo = TURN.subscribe(ctx.bus, lambda event: None)
            ctx.on_unwind(self.undo)

    host.load(Subscriber())
    assert host.events.has_listeners(TurnStarted)
    host.unload("subscriber")
    assert not host.events.has_listeners(TurnStarted), "the scope owned the removal"


# ── Scoped services: a mutation through ctx is reversible by construction ────


class _Registry:
    """A toy service whose mutations are reversible.

    The convention the kernel relies on: a mutating method returns the undo, and
    ``scoped(ctx)`` hands back a view bound to one plugin's lifetime.  The
    service itself stays a plain object -- the kernel never proxies it, because a
    transparent proxy would break ``is`` and ``isinstance`` for every service that
    did not need the behaviour.
    """

    def __init__(self) -> None:
        self.items: list[str] = []

    def scoped(self, ctx):
        return _ScopedRegistry(self, ctx)

    def add(self, item: str) -> None:
        self.items.append(item)

    def remove(self, item: str) -> None:
        if item in self.items:
            self.items.remove(item)


class _ScopedRegistry:
    def __init__(self, registry: _Registry, ctx) -> None:
        self._registry = registry
        self._ctx = ctx

    def register(self, item: str) -> str:
        """Adds, and registers its own undo on the owning plugin's lifetime."""
        self._registry.add(item)
        self._ctx.on_unwind(lambda: self._registry.remove(item))
        return item


class _UsesRegistry:
    name = "uses_registry"
    inject = ("things",)

    def setup(self, ctx):
        self.ctx = ctx
        ctx.things.register("mine")


class _ProvidesThings:
    name = "things_provider"

    def __init__(self, registry: _Registry) -> None:
        self._registry = registry

    def setup(self, ctx):
        # The service is provided under the name the consumer injects.
        pass


def _host_with_registry():
    host = PluginHost()
    registry = _Registry()
    host.services.provide("things", registry)
    return host, registry


def test_a_mutating_service_returns_a_per_plugin_view() -> None:
    host, registry = _host_with_registry()
    install = host.load(_UsesRegistry())
    assert registry.items == ["mine"]
    assert install.ctx.things is install.ctx.things, "the view is cached"


def test_a_registration_through_the_view_is_undone_on_unload() -> None:
    """The whole point: the plugin never names a scope, and the entry still goes."""
    host, registry = _host_with_registry()
    host.load(_UsesRegistry())
    assert registry.items == ["mine"]

    host.unload("uses_registry")
    assert registry.items == [], "the undo was recorded on the plugin's lifetime"


def test_two_plugins_using_one_service_do_not_revoke_each_other() -> None:
    """Each view records on its own plugin's scope, so unloads are independent."""
    host, registry = _host_with_registry()

    class Second:
        name = "second"
        inject = ("things",)

        def setup(self, ctx):
            ctx.things.register("theirs")

    host.load(_UsesRegistry())
    host.load(Second())
    assert sorted(registry.items) == ["mine", "theirs"]

    host.unload("uses_registry")
    assert registry.items == ["theirs"], "only its own entry went"


def test_a_service_without_scoped_keeps_its_identity() -> None:
    """A plain service is handed over as-is, so ``is`` and ``isinstance`` work."""
    host = PluginHost()
    host.services.provide("plain", registry := {"a": 1})
    install = host.load(Leaf())
    assert install.ctx.get("plain") is registry


def test_the_scoped_view_is_rebuilt_when_the_service_is_replaced() -> None:
    """Otherwise a plugin would keep mutating a service that is no longer installed."""
    host = PluginHost()
    first = _Registry()
    host.services.provide("things", first)
    install = host.load(_UsesRegistry())
    assert first.items == ["mine"]

    second = _Registry()
    host.services.provide("things", second, replace=True)
    install.ctx.things.register("mine")
    assert second.items == ["mine"], "the new service was used"
    assert first.items == ["mine"], "and the old one was not touched again"


# ── Replacement is a first-class act ─────────────────────────────────────────


def test_replacing_a_plugin_unloads_the_incumbent_first() -> None:
    """Not overwriting: half of one plugin and half of another is not a state to
    reason about, so the incumbent's registrations are unwound before the new
    one's are made."""
    host = PluginHost()
    host.load(Leaf())
    assert host.services.get("leaf.value") == 42

    class Better:
        name = "leaf"

        def setup(self, ctx):
            ctx.provide("leaf.value", 99)

    host.load(Better(), replace=True)
    assert host.services.get("leaf.value") == 99
    assert host.loaded() == ("leaf",), "one leaf, not two"


def test_a_plain_duplicate_is_still_refused() -> None:
    """Replacement is asked for; it is not the default."""
    host = PluginHost()
    host.load(Leaf())
    with pytest.raises(ValueError, match="already loaded"):
        host.load(Leaf())


def test_replacing_takes_the_incumbents_dependents_with_it() -> None:
    """The incumbent's dependents declared a dependency on *it*; a replacement is a
    different implementation and does not inherit that relationship."""
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)
    host.load(Leaf())
    assert dependent.activated

    host.load(Leaf(), replace=True)
    assert "needs_leaf" not in host.loaded()


def test_replacing_a_pending_plugin_works_too() -> None:
    host = PluginHost()
    host.load(NeedsLeaf())
    assert host.pending() == ("needs_leaf",)
    host.load(NeedsLeaf(), replace=True)
    assert host.pending() == ("needs_leaf",), "still one waiter, not two"


# ── A service that is also a plugin, and a composition that mounted none ─────


def test_a_mounted_plugin_can_be_replaced_by_name() -> None:
    """The value of "the agent loop is a plugin": a composition replaces it by
    providing ``agent_loop``, not by editing the runtime.

    Both plugins are named ``agent_loop`` -- the name is the *seam*, so a
    replacement necessarily has the same one.  Which is also why replacement
    unloads the incumbent rather than shadowing it: there is exactly one plugin by
    that name at a time, and no way to ask which of two live ones is in effect.
    """

    class Stock:
        name = "agent_loop"
        inject = ("agent",)

        def setup(self, ctx):
            ctx.provide("agent_loop", "stock loop")

    class Better:
        name = "agent_loop"
        inject = ("agent",)

        def setup(self, ctx):
            ctx.provide("agent_loop", "better loop")

    host = PluginHost()
    host.services.provide("agent", "an agent")
    host.load(Stock())
    assert host.services.get("agent_loop") == "stock loop"

    host.load(Better(), replace=True)
    assert host.services.get("agent_loop") == "better loop"
    assert host.loaded() == ("agent_loop",), "one loop, not two"

    host.unload("agent_loop")
    assert not host.services.has("agent_loop"), "and it goes with its plugin"


def test_a_plugin_injecting_an_absent_service_never_activates() -> None:
    """So a composition with no agent has no loop, rather than one that fails on
    the first turn -- a misconfiguration moved from startup into a user's message.
    """
    host = PluginHost()

    class Loop:
        name = "agent_loop"
        inject = ("agent",)

        def setup(self, ctx):
            ctx.provide("agent_loop", "stock loop")

    host.load(Loop())
    assert host.pending() == ("agent_loop",)
    assert host.services.get("agent_loop", None) is None
    assert host.waiting_for() == {"agent_loop": ("agent",)}
