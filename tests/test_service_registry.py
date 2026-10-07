"""The service registry and its disposable scopes.

This is the primitive the refactor is built on: a named service can be
provided, resolved, replaced, and -- above all -- registered inside a scope
that unwinds it.  ``agent/exec/subprocess.py`` already had a hand-rolled
version of the resolution half (``provider_from``); these tests pin the
generalization, including the half that did not exist before: nothing in the
tree could previously un-register a single registration.
"""

from __future__ import annotations

import pytest

from agent.exec.subprocess import LocalSubprocessProvider, provider_from
from agent.runtime.services import (
    MissingServiceError,
    ServiceRegistry,
    ServiceScope,
    ServiceView,
)


class _FakeProvider:
    """A stand-in for an injected SubprocessProvider."""

    def __init__(self, tag: str = "fake") -> None:
        self.tag = tag

    async def run(self, request):  # pragma: no cover - shape only
        raise NotImplementedError


# ── Resolution ───────────────────────────────────────────────────────────────


def test_provided_service_resolves() -> None:
    registry = ServiceRegistry()
    provider = _FakeProvider()
    registry.provide("subprocess_provider", provider)
    assert registry.get("subprocess_provider") is provider


def test_missing_service_raises_a_named_error() -> None:
    """A silent None is how a missing dependency becomes a runtime puzzle."""
    registry = ServiceRegistry()
    with pytest.raises(MissingServiceError) as excinfo:
        registry.get("subprocess_provider")
    assert "subprocess_provider" in str(excinfo.value)


def test_missing_service_respects_an_explicit_default() -> None:
    registry = ServiceRegistry()
    assert registry.get("nothing.here", default="fallback") == "fallback"


def test_resolution_reports_availability() -> None:
    registry = ServiceRegistry()
    assert not registry.has("subprocess_provider")
    registry.provide("subprocess_provider", _FakeProvider())
    assert registry.has("subprocess_provider")


# ── Replacement ──────────────────────────────────────────────────────────────


def test_second_provide_without_replace_is_refused() -> None:
    """Overwriting a service silently is how two providers disagree forever."""
    registry = ServiceRegistry()
    registry.provide("thing", "first")
    with pytest.raises(ValueError) as excinfo:
        registry.provide("thing", "second")
    assert "thing" in str(excinfo.value)


def test_replace_swaps_the_value() -> None:
    registry = ServiceRegistry()
    registry.provide("thing", "first")
    registry.provide("thing", "second", replace=True)
    assert registry.get("thing") == "second"


def test_replacing_restores_the_previous_value_on_unwind() -> None:
    """Replacement must be reversible, or a scope cannot own its own override."""
    registry = ServiceRegistry()
    registry.provide("thing", "original")
    with registry.scope() as scope:
        scope.provide("thing", "override", replace=True)
        assert registry.get("thing") == "override"
    assert registry.get("thing") == "original"


# ── Scopes ───────────────────────────────────────────────────────────────────


def test_scope_unwinds_what_it_registered() -> None:
    registry = ServiceRegistry()
    with registry.scope() as scope:
        scope.provide("temp", "value")
        assert registry.get("temp") == "value"
    assert not registry.has("temp")
    with pytest.raises(MissingServiceError):
        registry.get("temp")


def test_scope_unwinds_in_reverse_registration_order() -> None:
    """Last registered is first undone, so a provider can depend on an earlier one."""
    registry = ServiceRegistry()
    order: list[str] = []
    with registry.scope() as scope:
        scope.on_unwind(lambda: order.append("first"))
        scope.on_unwind(lambda: order.append("second"))
    assert order == ["second", "first"]


def test_nested_scope_unwinds_alone() -> None:
    registry = ServiceRegistry()
    with registry.scope() as outer:
        outer.provide("outer", 1)
        with registry.scope() as inner:
            inner.provide("inner", 2)
        assert not registry.has("inner")
        assert registry.get("outer") == 1
    assert not registry.has("outer")


def test_scope_does_not_leak_registrations_to_the_parent() -> None:
    registry = ServiceRegistry()
    scope = registry.scope()
    scope.provide("child.only", "x")
    assert registry.get("child.only") == "x"
    scope.dispose()
    assert not registry.has("child.only")


def test_dispose_is_idempotent() -> None:
    """Unloading runs twice on some paths; the second must be a no-op."""
    registry = ServiceRegistry()
    scope = registry.scope()
    scope.provide("once", 1)
    scope.dispose()
    scope.dispose()
    assert not registry.has("once")


def test_disposing_twice_does_not_undo_a_later_registration() -> None:
    registry = ServiceRegistry()
    first = registry.scope()
    first.provide("name", "from-first")
    first.dispose()
    registry.provide("name", "from-parent")
    first.dispose()
    assert registry.get("name") == "from-parent"


def test_scope_context_manager_returns_the_scope() -> None:
    registry = ServiceRegistry()
    with registry.scope() as scope:
        assert isinstance(scope, ServiceScope)
        scope.provide("inside", True)
        assert registry.get("inside") is True


def test_unwind_handler_that_raises_does_not_block_the_rest() -> None:
    """One broken teardown must not strand every other registration."""
    registry = ServiceRegistry()
    seen: list[str] = []

    def boom() -> None:
        raise RuntimeError("teardown failed")

    with pytest.raises(RuntimeError):
        with registry.scope() as scope:
            scope.on_unwind(lambda: seen.append("ran"))
            scope.on_unwind(boom)
    # The failing handler ran first (reverse order) and the earlier one still ran.
    assert seen == ["ran"]


# ── The existing injection idiom ─────────────────────────────────────────────


def test_provider_from_falls_back_to_the_local_provider() -> None:
    """No registry, or an empty one, still yields something runnable."""

    class _Bare:
        def get_context(self, key, default=None):
            return default

    assert isinstance(provider_from(None), LocalSubprocessProvider)
    assert isinstance(provider_from(_Bare()), LocalSubprocessProvider)


def test_provider_from_prefers_an_injected_provider() -> None:
    injected = _FakeProvider("injected")

    class _WithContext:
        def get_context(self, key, default=None):
            return injected if key == "subprocess_provider" else default

    assert provider_from(_WithContext()) is injected


def test_provider_from_reads_a_service_view() -> None:
    """The registry context becomes a view backed by a ServiceRegistry."""
    registry = ServiceRegistry()
    injected = _FakeProvider("scoped")
    registry.provide("subprocess_provider", injected)
    view = ServiceView(registry)

    class _Holder:
        def get_context(self, key, default=None):
            return view.get(key, default)

    assert provider_from(_Holder()) is injected


def test_service_view_mirrors_the_registry() -> None:
    registry = ServiceRegistry()
    view = ServiceView(registry)
    registry.provide("mirrored", 7)
    assert view["mirrored"] == 7
    assert view.get("missing", "d") == "d"
    assert "mirrored" in view
    assert dict(view) == {"mirrored": 7}
    assert len(view) == 1


def test_service_view_get_does_not_raise_for_a_missing_key() -> None:
    """``dict.get`` semantics, because the ~48 existing readers rely on them."""
    view = ServiceView(ServiceRegistry())
    assert view.get("nothing") is None
    with pytest.raises(MissingServiceError):
        view["nothing"]


# ── The end-to-end property: a provider replacement is reversible ────────────


class _ServicesOnlyHolder:
    """A registry-context reader that exposes only the service registry.

    Models the caller that has migrated to services and no longer has a direct
    ``subprocess_provider`` entry -- the case the second lookup exists for.
    """

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    def get_context(self, key, default=None):
        if key == "services":
            return ServiceView(self._services)
        return default


def test_provider_from_reaches_a_service_registry_when_the_key_is_absent() -> None:
    services = ServiceRegistry()
    injected = _FakeProvider("via-services")
    services.provide("subprocess_provider", injected)
    assert provider_from(_ServicesOnlyHolder(services)) is injected


def test_a_scoped_provider_replacement_reverts_on_unload() -> None:
    """The point of this task, stated as behaviour.

    A plugin provides its own execution provider; everything resolving one gets
    the plugin's; when the scope closes the original is back.  No call site is
    told either time, which is what makes replacement a property of the
    registry rather than a convention every caller has to honour.
    """
    services = ServiceRegistry()
    original = _FakeProvider("original")
    replacement = _FakeProvider("replacement")
    services.provide("subprocess_provider", original)
    holder = _ServicesOnlyHolder(services)

    assert provider_from(holder) is original

    with services.scope() as scope:
        scope.provide("subprocess_provider", replacement, replace=True)
        assert provider_from(holder) is replacement

    assert provider_from(holder) is original


# ── The registry as the runtime's container ──────────────────────────────────


def test_a_registry_can_adopt_a_caller_s_own_dict() -> None:
    """So a mapping built by bootstrap and a registry are the *same* storage.

    Copying would mean a caller's later assignment to the mapping is invisible to
    the registry -- which matters because bootstrap assigns directly into the
    component dict while it is being assembled.
    """
    storage = {"agent": "the-agent"}
    registry = ServiceRegistry.adopt(storage)
    assert registry.get("agent") == "the-agent"

    storage["late"] = "added later"
    assert registry.get("late") == "added later", "the registry must see the write"

    registry.provide("through.registry", 1, replace=False)
    assert storage["through.registry"] == 1, "and the mapping must see the registry"


def test_adopt_does_not_copy() -> None:
    storage: dict = {}
    ServiceRegistry.adopt(storage)
    assert storage == {}, "adopt must not seed the caller's dict"


# ── The registry as a component container ────────────────────────────────────


def test_resolve_reads_a_registry() -> None:
    """The kernel's registry is a legitimate container, not only a stand-in for one.

    The agent loop holds its composition as a registry now, so ``resolve`` and
    ``require_service`` have to accept it -- otherwise every read inside a plugin
    would have to go through a second shape for no reason.
    """
    from agent.kernel.service import ServiceRegistry, resolve

    registry = ServiceRegistry()
    registry.provide("agent", "the agent")
    assert resolve(registry, "agent", None) == "the agent"
    assert resolve(registry, "absent", None) is None


def test_require_service_reads_a_registry_and_raises_for_a_missing_name() -> None:
    from agent.kernel.service import (
        MissingServiceError,
        ServiceRegistry,
        require_service,
    )

    registry = ServiceRegistry()
    registry.provide("agent", "the agent")
    assert require_service(registry, "agent") == "the agent"
    with pytest.raises(MissingServiceError):
        require_service(registry, "absent")
