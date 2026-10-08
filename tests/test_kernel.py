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
from agent.core.event_domains import TurnCompleted, TurnStarted


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


def test_replacing_reactivates_dependents_because_a_dependency_is_a_name() -> None:
    """The opposite of what this test first asserted, and the first assertion was wrong.

    It said the incumbent's dependents "declared a dependency on *it*", so a replacement
    -- being a different implementation -- should not inherit the relationship.  That
    reads well and contradicts the kernel's own model: ``inject`` names **services**, the
    graph is keyed by name, and ``has(name)`` is key presence.  A replacement provides the
    same name, so it satisfies the same dependency.

    The cost of the old behaviour was not a wrong answer in a diagnostic; it was that
    replacing a provider silently **deleted everything above it**, with nothing pending
    and no error.  A dependent that wanted to refuse the new implementation could not:
    it was gone.
    """
    host = PluginHost()
    dependent = NeedsLeaf()
    host.load(dependent)
    host.load(Leaf())
    assert dependent.activated

    host.load(Leaf(), replace=True)

    assert "needs_leaf" in host.loaded(), (
        "the dependent must be re-activated: its dependency is the name `leaf`, which the "
        "replacement provides"
    )
    assert dependent.activated, "and it re-reads the service rather than keeping the old one"


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


# ── An async setup is refused, not silently ignored ──────────────────────────


def test_an_async_setup_is_refused_rather_than_silently_doing_nothing() -> None:
    """The kernel calls ``setup`` synchronously, so a coroutine registers nothing.

    This is the worst shape a plugin failure can take: the install reports as loaded,
    ``pending()`` is empty, and the plugin contributes nothing -- the only signal is a
    ``RuntimeWarning`` that most deployments suppress.  Found while making the
    Scheduler a plugin, whose builder is ``async``; it would have produced a scheduler
    that mounted successfully and had no service.

    Refused rather than supported: awaiting here would mean ``load`` is async, which is
    a change to every caller including the composition root.
    """
    host = PluginHost()

    class AsyncSetup:
        name = "async_one"

        async def setup(self, ctx):
            ctx.provide("thing", 42)

    install = host.load(AsyncSetup())
    assert install.error, "the install must carry an error"
    assert "async setup" in install.error
    assert host.services.get("thing", None) is None
    assert host.pending() == (), "not left pending: it will fail the same way again"


def test_an_async_setup_that_registered_first_keeps_none_of_it() -> None:
    """No half-applied state, because it goes through the same disposal as a raise."""
    host = PluginHost()

    class HalfAsync:
        name = "half"

        async def setup(self, ctx):
            ctx.provide("partial", 1)  # never runs, and must not survive if it did

    install = host.load(HalfAsync())
    assert install.error
    assert host.services.get("partial", None) is None


def test_a_raising_setup_and_an_async_one_leave_the_same_state() -> None:
    """One failure path, so the two cannot diverge."""
    class Raises:
        name = "raises"

        def setup(self, ctx):
            ctx.provide("x", 1)
            raise RuntimeError("boom")

    class Async:
        name = "async_one"

        async def setup(self, ctx):
            ctx.provide("x", 1)

    states = []
    for plugin in (Raises(), Async()):
        host = PluginHost()
        install = host.load(plugin)
        states.append(
            (
                bool(install.error),
                host.services.get("x", None),
                host.pending(),
                plugin.name in host.installs,
            )
        )
    assert states[0] == states[1] == (True, None, (), False), states


def test_a_sync_setup_that_returns_something_harmless_is_fine() -> None:
    """Control: the check is on a coroutine, not on a return value.

    A ``setup`` that ends with ``return self.something`` is ordinary, and refusing every
    non-``None`` return would be the wrong rule -- and would break plugins that exist.
    """
    host = PluginHost()

    class Returns:
        name = "returns"

        def setup(self, ctx):
            ctx.provide("thing", 7)
            return "some value"

    install = host.load(Returns())
    assert install.error is None
    assert host.services.get("thing", None) == 7


# ── A subscription to another bus's event is refused ─────────────────────────


def _host_with_domains():
    """A host wired the way a composition wires one: it knows the domain table."""
    from agent.core.event_domains import domain_of

    host = PluginHost()
    host.domain_lookup = domain_of
    return host


class _Subscriber:
    name = "subscriber"

    def __init__(self) -> None:
        self.seen: list = []

    def setup(self, ctx):
        self.ctx = ctx


def test_subscribing_to_a_domain_event_through_the_context_is_refused() -> None:
    """``ctx.on(TurnCompleted, ...)`` reads correctly and does nothing.

    A domain's events are published on **that domain's bus**.  The context's bus is for
    events a composition defines itself, and in the shipped composition the two happen to
    be the same object -- so this call *would* work there, and would stop working the
    moment a composition had its own bus.  A listener that is never called is
    indistinguishable from a plugin that works, which is why the refusal is worth more
    than the permissiveness.

    ``Domain.attach(ctx, handler)`` goes to the domain's bus in every configuration, and
    that is what the message points at.
    """
    host = _host_with_domains()
    plugin = _Subscriber()
    host.load(plugin)
    assert host.pending() == (), "the plugin activated"

    with pytest.raises(ValueError) as caught:
        plugin.ctx.on(TurnCompleted, lambda event: None)

    message = str(caught.value)
    assert "turn" in message and "Domain.attach" in message, message


def test_the_domain_form_is_accepted_and_reachable() -> None:
    """Control: the refusal must point at something that works."""
    from agent.core.event_domains import TURN
    from agent.core.events import TURN_EVENTS

    host = _host_with_domains()
    plugin = _Subscriber()
    host.load(plugin)
    plugin.ctx.on_unwind(TURN.attach(plugin.ctx, plugin.seen.append))

    asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
    assert len(plugin.seen) == 1, "the domain form reaches the domain's bus"


def test_an_event_of_the_composition_is_accepted() -> None:
    """Control: the rule is about the domain table, not about every event.

    A plugin defining its own event and subscribing with ``ctx.on`` is the case the
    context's bus exists for, and refusing it would make the context useless.
    """
    host = _host_with_domains()
    plugin = _Subscriber()
    host.load(plugin)

    class _Mine:
        pass

    unsub = plugin.ctx.on(_Mine, plugin.seen.append)
    assert callable(unsub)
    asyncio.run(plugin.ctx.emit(_Mine()))
    assert len(plugin.seen) == 1


def test_without_a_wired_table_the_context_accepts_anything() -> None:
    """The kernel does not know the domains, so it cannot refuse on its own.

    A host with no ``domain_lookup`` is a bare kernel -- used in the kernel's own tests
    and by anything embedding it -- and it must keep working without the domain table.
    """
    host = PluginHost()
    plugin = _Subscriber()
    host.load(plugin)
    assert plugin.ctx.on(TurnCompleted, lambda event: None) is not None


# ── replacement is an act, not a collision ────────────────────────────────────


class _Provider:
    """Provides ``thing``, with a value a test can recognise."""

    def __init__(self, value: object, *, extra: tuple[str, ...] = ()) -> None:
        self.name = "provider"
        self.inject = ("cfg",)
        self._value = value
        self._extra = extra

    def setup(self, ctx) -> None:
        ctx.provide("thing", self._value)
        for name in self._extra:
            ctx.provide(name, self._value)


class _Consumer:
    name = "consumer"
    inject = ("thing",)

    def __init__(self) -> None:
        self.seen = None
        self.activated = 0

    def setup(self, ctx) -> None:
        self.seen = ctx.get("thing")
        self.activated += 1


class _DeepConsumer:
    name = "deep"
    inject = ("middle",)

    def setup(self, ctx) -> None:
        ctx.provide("deep_done", True)


class _Middle:
    name = "middle"
    inject = ("thing",)

    def setup(self, ctx) -> None:
        ctx.provide("middle", True)


def _host(mapping=None):
    from agent.kernel.service import ServiceRegistry

    mapping = mapping if mapping is not None else {"cfg": {}}
    return PluginHost(services=ServiceRegistry.adopt(mapping)), mapping


def test_replacing_a_provider_brings_its_dependents_back() -> None:
    """Swapping a provider used to delete everything above it.

    ``load(replace=True)`` unloaded the incumbent, which correctly took its dependents
    down -- they held a service whose owner was about to be gone -- and then left them
    down.  So replacing the provider of ``thing`` left ``consumer`` permanently absent,
    with no error and nothing pending: the dependent had been *unloaded*, not left
    waiting.  That is deletion wearing replacement's name.
    """
    host, mapping = _host()
    host.load(_Provider("first"))
    consumer = _Consumer()
    host.load(consumer)
    assert host.loaded() == ("provider", "consumer")

    host.load(_Provider("second"), replace=True)

    assert host.loaded() == ("provider", "consumer"), (
        "the dependent must come back, not be lost"
    )
    assert mapping["thing"] == "second"
    assert consumer.seen == "second", "the replacement's value must reach the dependent"
    assert consumer.activated == 2, "the dependent is re-activated, not merely remembered"


def test_a_replacement_that_does_not_satisfy_leaves_the_dependent_pending() -> None:
    """The honest failure: visible, and naming what is missing.

    A dependent that cannot be satisfied must not silently vanish, and must not pretend
    to be active.  It stays in ``pending`` and ``waiting_for`` says why.
    """
    host, mapping = _host()
    host.load(_Provider("first"))
    host.load(_Consumer())

    class _Insufficient:
        name = "provider"
        inject = ("cfg",)

        def setup(self, ctx) -> None:
            pass  # provides nothing

    host.load(_Insufficient(), replace=True)

    assert host.loaded() == ("provider",)
    assert host.pending() == ("consumer",)
    assert host.waiting_for() == {"consumer": ("thing",)}
    assert "thing" not in mapping, "the old value must not survive its provider"


def test_a_plain_unload_still_takes_dependents_down() -> None:
    """Control: the restore is specific to replacement, not a weakening of unload.

    An unload with no replacement really does mean the service is gone, so leaving the
    dependents active would leave them holding a dead reference.
    """
    host, mapping = _host()
    host.load(_Provider("first"))
    host.load(_Consumer())

    host.unload("provider")

    assert host.loaded() == ()
    assert host.pending() == ()
    assert "thing" not in mapping


def test_replacement_restores_a_chain_not_only_direct_dependents() -> None:
    """A subtree comes back, not just the first level.

    ``consumer`` injects ``thing``; ``middle`` injects ``thing`` and provides
    ``middle``; ``deep`` injects ``middle``.  Everything above the replaced provider
    went down with it, so everything above it has to come back.
    """
    host, mapping = _host()
    host.load(_Provider("first"))
    host.load(_MidCfg := _Middle())  # noqa: F841 - named for readability
    host.load(_DeepConsumer())

    host.load(_Provider("second"), replace=True)

    assert host.loaded() == ("provider", "middle", "deep")
    assert mapping["middle"] is True
    assert mapping["deep_done"] is True


def test_replacement_keeps_the_activation_order_of_the_restored_plugins() -> None:
    """Providers first, so a dependent does not briefly wait for one it needs.

    Re-loading in the wrong order would still end up correct -- ``_flush_pending``
    resolves it -- but it would log a plugin flapping through ``pending``, and the
    order is free to get right here.
    """
    host, _ = _host()
    host.load(_Provider("first"))
    host.load(_Middle())
    host.load(_DeepConsumer())

    host.load(_Provider("second"), replace=True)

    order = list(host.loaded())
    assert order.index("provider") < order.index("middle") < order.index("deep")


# ── the third state: refused ─────────────────────────────────────────────────


class _Boom:
    name = "boom"

    def setup(self, ctx) -> None:
        raise RuntimeError("setup exploded")


class _AsyncBoom:
    name = "async_boom"

    async def setup(self, ctx) -> None:  # pragma: no cover - refused, never awaited
        ...


class _Child:
    name = "child"
    inject = ("never_provided",)

    def setup(self, ctx) -> None:
        ctx.provide("child_up", True)


class _ChildLoader:
    name = "loader"

    def setup(self, ctx) -> None:
        ctx.provide("loaded_marker", True)
        ctx.plugin(_Child())


class _OtherLoader:
    name = "loader"

    def setup(self, ctx) -> None:
        ctx.provide("loaded_marker", "replaced")


def test_a_setup_that_raises_is_reported_not_only_logged() -> None:
    """``loaded`` and ``pending`` were the only two states, and a refusal was neither.

    A plugin whose ``setup`` raised registered nothing, so it appeared in no list at all
    and the only trace was a log line -- which is not an API.  A composition root that
    wanted to say "these did not load" had to parse output it does not own.
    """
    host, _ = _host()
    host.load(_Boom())

    assert host.loaded() == ()
    assert host.pending() == ()
    assert host.failed() == {"boom": "setup exploded"}
    assert [install.name for install in host.failures()] == ["boom"]


def test_an_async_setup_is_reported_the_same_way() -> None:
    """Both refusals go through one path, so neither can be the quiet one.

    The async form is refused on purpose -- the kernel calls ``setup`` synchronously and
    awaiting would make ``load`` async -- and the point of refusing it is that its
    *absence of effect* is otherwise invisible.
    """
    host, _ = _host()
    host.load(_AsyncBoom())

    assert list(host.failed()) == ["async_boom"]
    assert "async setup" in host.failed()["async_boom"]


def test_a_later_success_clears_the_record_of_an_earlier_failure() -> None:
    """A report that kept naming a fixed plugin is one people learn to ignore."""
    host, _ = _host()
    host.load(_Boom())
    assert "boom" in host.failed()

    class _Fixed:
        name = "boom"

        def setup(self, ctx) -> None:
            ctx.provide("boom_ok", True)

    host.load(_Fixed(), replace=True)
    assert host.failed() == {}
    assert "boom_ok" in host.services._services


def test_unload_all_clears_the_failure_record_too() -> None:
    """``unload_all`` means the tree is empty; a failure list would describe a gone tree."""
    host, _ = _host()
    host.load(_Boom())
    host.unload_all()
    assert host.failed() == {}


def test_a_plugin_that_loads_a_child_from_setup_is_recorded_as_its_parent() -> None:
    """The link was dropped, and the consequence was a child that could not be restored.

    ``self._installs[name] = install`` happens **after** ``setup`` returns, so during its
    own setup a plugin is not in ``_installs`` -- and the child it loaded got no link.
    Disposal still worked (scopes nest), which is why nothing looked wrong; what broke
    was everything that *walks* the tree, ``replace``'s capture included.
    """
    host, _ = _host()
    host.load(_ChildLoader())

    assert host._installs["loader"].children == ["child"]
    assert host.pending() == ("child",)


def test_replacing_a_parent_keeps_a_pending_child() -> None:
    """Measured before the fix: the pending child vanished from both lists.

    ``unload`` takes a child down whether it is active or pending -- its loop tests
    ``_installs`` *and* ``_pending`` -- so a capture that read only ``_installs`` dropped
    every pending child, and it was never restored.
    """
    host, _ = _host()
    host.load(_ChildLoader())
    assert host.pending() == ("child",)

    host.load(_OtherLoader(), replace=True)

    assert host.loaded() == ("loader",)
    assert host.pending() == ("child",), "the pending child must survive its parent's swap"
    assert host.waiting_for() == {"child": ("never_provided",)}


def test_a_child_loaded_during_setup_is_still_disposed_with_its_parent() -> None:
    """Control: the scope nesting that made the missing link invisible still works."""
    host, _ = _host()
    host.load(_ChildLoader())
    assert "loaded_marker" in host.services._services

    host.unload("loader")

    assert "loaded_marker" not in host.services._services
    assert host.pending() == ()


# ── awaitable teardown ───────────────────────────────────────────────────────


class _AsyncTeardown:
    """Registers both halves, so the two lifetimes can be told apart."""

    name = "async_teardown"

    def setup(self, ctx) -> None:
        ctx.provide("async_marker", True)
        ctx.on_unwind(lambda: self.sync_ran.append(True))
        ctx.on_unwind_async(self._aclose)

    sync_ran: list = []

    async def _aclose(self) -> None:
        self.closed = True

    closed = False


class _AstopPlugin:
    name = "astop_plugin"

    def __init__(self) -> None:
        self.stopped = False

    def setup(self, ctx) -> None:
        ctx.provide("astop_marker", True)

    async def astop(self) -> None:
        self.stopped = True


def test_an_awaitable_teardown_is_claimed_and_visible_rather_than_dropped() -> None:
    """The reason ``on_unwind_async`` is a separate call with a separate report.

    ``unload`` is synchronous and stays that way -- making it async would change every
    caller including the composition root -- so it cannot await a pool close.  It claims
    the handler and *says so*: dropping it would release nothing and report nothing, which
    is the failure mode this class exists to prevent.
    """
    import asyncio

    host, _ = _host()
    plugin = _AsyncTeardown()
    plugin.sync_ran = []
    host.load(plugin)

    host.unload("async_teardown")

    assert plugin.sync_ran == [True], "the synchronous half still runs on unload"
    assert host.pending_async_disposals() == ("async_teardown",)
    assert plugin.closed is False, "the awaitable half has not run yet"

    asyncio.run(host.adispose())

    assert plugin.closed is True
    assert host.pending_async_disposals() == ()


def test_aunload_awaits_for_one_plugin() -> None:
    import asyncio

    host, _ = _host()
    plugin = _AsyncTeardown()
    plugin.sync_ran = []
    host.load(plugin)

    asyncio.run(host.aunload("async_teardown"))

    assert plugin.closed is True
    assert host.pending_async_disposals() == ()


def test_astop_is_awaited_like_a_registered_teardown() -> None:
    """Both spellings, one lifetime rule.

    A plugin may declare ``async def astop`` instead of registering a handler; the claim
    and the await are the same path, so neither spelling can be the one that leaks.
    """
    import asyncio

    host, _ = _host()
    plugin = _AstopPlugin()
    host.load(plugin)

    host.unload("astop_plugin")
    assert plugin.stopped is False
    assert host.pending_async_disposals() == ("astop_plugin",)

    asyncio.run(host.adispose())
    assert plugin.stopped is True


def test_a_synchronous_only_plugin_reports_nothing_pending() -> None:
    """Control: the report names only plugins that actually needed awaiting."""
    host, _ = _host()
    host.load(_ChildLoader())

    host.unload("loader")

    assert host.pending_async_disposals() == ()


def test_the_async_setup_refusal_names_the_supported_alternative() -> None:
    """A refusal that only says no is one somebody works around badly."""
    host, _ = _host()
    host.load(_AsyncBoom())

    error = host.failed()["async_boom"]
    assert "astop" in error and "on_unwind_async" in error


# ── why a plugin is pending, and who re-checks it ─────────────────────────────


class _WaitsForX:
    name = "waits_for_x"
    inject = ("x",)

    def setup(self, ctx) -> None:
        ctx.provide("waits_for_x_up", True)


def test_waiting_for_reports_the_reason_it_went_pending() -> None:
    """Not the difference at report time, which can be empty while it is still pending.

    The old form recomputed the unmet set when asked.  A composition that wrote its
    services straight into the mapping -- which is what the composition root does for the
    values it computes itself -- produced the report

        ralph: pending, waiting for: ()

    "pending, and waiting for nothing": a sentence that sends somebody looking in the wrong
    place, at the one moment the report exists for.
    """
    host, _ = _host()
    host.load(_WaitsForX())
    assert host.waiting_for() == {"waits_for_x": ("x",)}

    # The dependency appears by a route that runs no `setup`, so nothing re-checks.
    host.services._services["x"] = 1

    assert host.pending() == ("waits_for_x",)
    assert host.waiting_for() == {"waits_for_x": ("x",)}, (
        "the recorded reason, not the current difference"
    )


def test_flush_pending_activates_a_waiter_whose_service_appeared_quietly() -> None:
    """The public re-check, so the composition does not rely on a later mount.

    Measured before the fix: removing the `TurnRunner` mount left `ralph` pending forever,
    with `system_prompt` present the whole time -- activation had been happening as a side
    effect of an unrelated later `mount`, and nothing said so.
    """
    host, _ = _host()
    host.load(_WaitsForX())
    host.services._services["x"] = 1

    assert host.flush_pending() == ("waits_for_x",)
    assert host.loaded() == ("waits_for_x",)
    assert host.pending() == ()
    assert "waits_for_x_up" in host.services._services


def test_flush_pending_is_empty_when_nothing_can_activate() -> None:
    """Control: it reports what it activated, so it cannot be mistaken for a no-op."""
    host, _ = _host()
    host.load(_WaitsForX())
    assert host.flush_pending() == ()
    assert host.pending() == ("waits_for_x",)
