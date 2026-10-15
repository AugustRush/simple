"""The scheduler as a plugin: present only if a composition mounts it.

The gateway used to build the scheduler inline, so every gateway process had one whether
or not the operator wanted it, and the two commands that run one each repeated the same
five steps.  Mounted, "this composition has no scheduler" is a state it can be in, and
`/schedule` reports a missing service instead of half-working.

These tests are about the plugin's own assembly -- what it requires, what it provides,
what it owns.  The gateway's wiring is `tests/test_agent_integration.py`'s.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.kernel.plugin import PluginHost
from agent.scheduler.plugin import SchedulerPlugin
from agent.scheduler.settings import SchedulerSettings

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Memory:
    def force_tidy(self) -> None:
        return None


def _host(**overrides):
    host = PluginHost()
    values = {
        "memory": _Memory(),
        "runtime_factory": object(),
        "cfg": {},
    }
    values.update(overrides)
    for name, value in values.items():
        if value is not None:
            host.services.provide(name, value)
    return host


# ── It is a plugin, so absence is a state ────────────────────────────────────


def test_the_scheduler_is_absent_until_it_is_mounted() -> None:
    host = _host()
    assert host.services.get("scheduler", None) is None
    host.load(SchedulerPlugin())
    assert type(host.services.get("scheduler", None)).__name__ == "SchedulerService"


def test_unloading_takes_the_service_away() -> None:
    host = _host()
    host.load(SchedulerPlugin())
    host.unload("scheduler")
    assert host.services.get("scheduler", None) is None


def test_a_composition_without_memory_leaves_it_pending() -> None:
    """``memory`` is the one name with no fallback.

    The builder read it as a hard subscript, so a run without it raised a ``KeyError``
    inside the run.  Declared in ``inject``, the absence is a *pending* plugin -- visible
    at composition time, and named.
    """
    host = _host(memory=None)
    host.load(SchedulerPlugin())
    assert host.pending() == ("scheduler",)
    assert host.services.get("scheduler", None) is None
    assert "memory" in host.waiting_for()["scheduler"]


def test_a_composition_without_a_runtime_factory_leaves_it_pending() -> None:
    """A run that finds a changed config builds its own runtime.

    The factory's default would reach for the composition root, which is exactly what
    this plugin exists to avoid -- so it is required rather than defaulted.
    """
    host = _host(runtime_factory=None)
    host.load(SchedulerPlugin())
    assert host.pending() == ("scheduler",)


def test_it_declares_the_two_things_it_cannot_work_without() -> None:
    assert set(SchedulerPlugin.inject) == {"memory", "runtime_factory", "cfg"}
    assert SchedulerPlugin.name == "scheduler"


def test_it_declares_what_it_reads_without_requiring() -> None:
    """Every tolerated read is named, so the replace/unload cascade can see it.

    A settings override, the output directory, the workspace root, the tool
    registry and a named bundle's loader are all optional for the *builder* -- it
    falls back to its own default.  They are not optional for the *declaration*:
    an undeclared read is a dependency the kernel cannot see, which is how a
    replaced service leaves its reader holding the old one.
    """
    assert set(SchedulerPlugin.optional) == {
        "scheduler_settings",
        "output_dir",
        "workspace_root",
        "registry",
        "scheduler_config_loader",
    }


# ── What it owns ─────────────────────────────────────────────────────────────


def test_the_plugin_owns_the_store_it_created(monkeypatch) -> None:
    """Ownership follows creation, and here the plugin is the one that creates it.

    An earlier attempt gave this ownership to the service -- 'close the store you were
    built with' -- and the suite overturned it: a caller may legitimately read the store
    after the service stops, because the service was *given* the store.  The plugin is
    not given it; the plugin opens it.  So the plugin closes it.
    """
    import agent.scheduler.plugin as plugin_module

    store = _Store()
    monkeypatch.setattr(plugin_module, "scheduler_store", lambda: store)

    host = _host()
    host.load(SchedulerPlugin())
    assert store.closed == 0, "open while the plugin lives"

    host.unload("scheduler")
    assert store.closed == 1, "closed exactly once, by the scope that opened it"


def test_the_builder_is_handed_the_plugins_own_store(monkeypatch) -> None:
    """Not opened twice.

    The builder used to call ``scheduler_store()`` itself.  Left as it was, this plugin
    would open one store and the service would use another -- two connections to one
    file, and the scope's ``close`` would reach the one nothing used while the service's
    stayed open.  Asserted through the argument rather than by counting opens, because
    counting cannot tell which of the two was closed.
    """
    import agent.scheduler.plugin as plugin_module
    from agent.scheduler import build as build_module

    store = _Store()
    monkeypatch.setattr(plugin_module, "scheduler_store", lambda: store)

    seen = {}
    real = build_module.build_scheduler_service

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(plugin_module, "build_scheduler_service", spy)
    host = _host()
    host.load(SchedulerPlugin())
    assert seen.get("store") is store, "the builder must use the plugin's store"


# ── The two things it cannot read from a context ─────────────────────────────


def test_settings_come_from_the_composition_when_argv_had_anything_to_say() -> None:
    """Config *and* argv decide the run parameters; a plugin has only a context.

    So the caller resolves the two and puts the result in -- which is what the gateway
    does -- and the plugin reads one name.
    """
    provided = SchedulerSettings(
        poll_seconds=7.5, lease_seconds=90, max_concurrent_runs=4, signal_max_depth=2
    )
    host = _host(scheduler_settings=provided, cfg={"scheduler": {"poll_seconds": 1}})
    host.load(SchedulerPlugin())
    service = host.services.get("scheduler", None)
    assert service.poll_seconds == 7.5
    assert service.lease_seconds == 90


def test_settings_fall_back_to_the_config_when_nothing_was_provided() -> None:
    host = _host(cfg={"scheduler": {"poll_seconds": 3, "lease_seconds": 40}})
    host.load(SchedulerPlugin())
    service = host.services.get("scheduler", None)
    assert service.poll_seconds == 3.0
    assert service.lease_seconds == 40


def test_it_does_not_import_the_cli() -> None:
    """A plugin reaching for the command layer is the coupling the kernel removes.

    The two parameters it cannot get from a context are passed *in*; nothing here reads
    ``sys.argv`` or imports ``agent.cli``, and this asserts the absence because that is
    the shape a shortcut would take.
    """
    tree = ast.parse((REPO_ROOT / "agent/scheduler/plugin.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "agent.cli" not in imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "argv"]
