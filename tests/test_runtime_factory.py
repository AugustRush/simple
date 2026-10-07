"""Building a runtime is a service, so something inside a composition can ask for it.

Six call sites reach ``agent._build_components_async``.  Five are the composition root
building itself, which is what a root is for.  The sixth is a scheduled run: it may find
that the config on disk changed since the process started, and then needs a runtime built
from *that* config -- a fresh composition, per run.

A scheduled run is not the root.  Importing the root to build one is a plugin reaching
behind the container it was given, which is the coupling the kernel exists to remove, and
it is the last thing blocking the Scheduler from being a plugin (see
``docs/superpowers/plans/2026-10-02-plugin-kernel-status.md``).

The tests here are about the *seam*: that the factory is substitutable, that it carries
the two arguments the existing call sites actually vary, and that building and closing
are the same object's business.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.runtime.factory import RuntimeFactory

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Recorder:
    """A builder that records how it was called and returns a marker."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed: list[dict] = []

    async def build(self, cfg, *, announce=True, resource_home=None):
        self.calls.append(
            {"cfg": cfg, "announce": announce, "resource_home": resource_home}
        )
        return {"built": True, "cfg": cfg}

    async def close(self, components):
        self.closed.append(components)


# ── The seam ─────────────────────────────────────────────────────────────────


def test_building_goes_through_the_substituted_builder() -> None:
    """Substituted at construction, not by patching a module global.

    The point of the service is that a composition can supply a different one.  A factory
    that looked the builder up per call would be substitutable only by monkeypatching,
    which is the arrangement the service replaces.
    """
    import asyncio

    recorder = _Recorder()
    factory = RuntimeFactory(recorder.build, recorder.close)

    async def main():
        return await factory.build({"a": 1})

    assert asyncio.run(main()) == {"built": True, "cfg": {"a": 1}}
    assert recorder.calls == [{"cfg": {"a": 1}, "announce": True, "resource_home": None}]


def test_announce_travels_because_a_run_must_not_reprint_the_header() -> None:
    import asyncio

    recorder = _Recorder()
    factory = RuntimeFactory(recorder.build)
    asyncio.run(factory.build({}, announce=False))
    assert recorder.calls[0]["announce"] is False


def test_resource_home_travels_because_a_gateways_directories_are_global() -> None:
    import asyncio

    recorder = _Recorder()
    factory = RuntimeFactory(recorder.build)
    asyncio.run(factory.build({}, resource_home=Path("/tmp/home")))
    assert recorder.calls[0]["resource_home"] == Path("/tmp/home")


def test_closing_is_the_same_objects_business() -> None:
    """Built by one thing and closed by another is where a leak comes from.

    A caller given the ability to build should not have to import the ability to close;
    the two are one concern, and keeping them on one object is what makes it hard to
    build a runtime that nothing closes.
    """
    import asyncio

    recorder = _Recorder()
    factory = RuntimeFactory(recorder.build, recorder.close)

    async def main():
        components = await factory.build({})
        await factory.close(components)

    asyncio.run(main())
    assert recorder.closed == [{"built": True, "cfg": {}}]


def test_the_default_delegates_to_the_composition_root() -> None:
    """A name for the existing behaviour, not a second implementation of it.

    Asserted by import rather than by calling: calling would build a whole runtime.  What
    matters is that the default is *reached*, so the service cannot drift into being its
    own builder.
    """
    source = (REPO_ROOT / "agent/runtime/factory.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "agent"
        for alias in node.names
    }
    assert "_build_components_async" in imported
    assert "_close_components" in imported


def test_the_scheduler_builds_through_the_factory_rather_than_the_root() -> None:
    """The sixth call site must not import the composition root.

    This is the check that says the seam is actually used: ``agent/scheduler/build.py``
    may reach ``agent`` for config loading, but building and closing a run's runtime must
    go through the factory it was given.
    """
    source = (REPO_ROOT / "agent/scheduler/build.py").read_text(encoding="utf-8")
    assert "factory.build(" in source, "the run's runtime must be built by the factory"
    assert "factory.close(" in source, "and closed by the same one"
    assert "_build_components_async" not in source, (
        "importing the composition root to build a run is what this replaces"
    )
    assert "_close_components" not in source, (
        "and the same for closing it"
    )


def test_the_factory_is_provided_to_the_composition() -> None:
    """A service nothing provides is the same as one that does not exist."""
    source = (REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8")
    assert 'runtime_factory' in source
    tree = ast.parse(source)
    provided = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "provide"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "runtime_factory"
    ]
    assigned = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "runtime_factory"
        and isinstance(node.ctx, ast.Store)
    ]
    assert provided or assigned, "bootstrap must provide runtime_factory"
