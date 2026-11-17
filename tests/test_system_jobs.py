"""Scheduled maintenance as a contribution, not a branch in the builder.

``build_scheduler_service`` had one supported system job:

    if job_name == "memory_tidy":
        memory_palace.force_tidy()
        await memory_palace.tidy()
        ...
    raise RuntimeError(f"Unsupported system job: {job_name}")

So a plugin could not add periodic maintenance -- reindex, rotate, refresh,
prune. The scheduler already had the trigger, the run row, the history and the
failure report; the only missing piece was that the *body* had to be written in
the builder.

These tests are from the outside: a plugin contributes a job through the same
``ctx`` shape every other capability uses, and a run executes it.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.scheduler.system_jobs import SystemJobRegistry


class _PluginCtx:
    def __init__(self, name: str) -> None:
        self.name = name
        self._undos: list = []

    def on_unwind(self, undo) -> None:
        self._undos.append(undo)

    def unload(self) -> None:
        for undo in reversed(self._undos):
            undo()
        self._undos.clear()


def _executor(registry: SystemJobRegistry):
    """The builder's executor, reduced to the lookup it now is."""

    async def run(task, run):
        payload = dict(getattr(task, "payload", None) or {})
        job_name = str(payload.get("job_name", "")).strip()
        job = registry.job_for(job_name)
        if job is None:
            available = ", ".join(registry.names()) or "(none registered)"
            raise RuntimeError(
                f"Unsupported system job: {job_name!r}; this install has: {available}"
            )
        return await job(task=task, run=run, payload=payload, cfg={})

    return run


class _Task:
    def __init__(self, job_name: str) -> None:
        self.payload = {"job_name": job_name}


def test_a_plugin_can_add_a_job_a_run_then_executes() -> None:
    """The whole point: a maintenance job the builder has never heard of."""
    ran: list[str] = []

    async def reindex(*, task, run, payload, cfg):
        ran.append("reindex")
        return {"summary": "reindexed"}

    registry = SystemJobRegistry()
    registry.scoped(_PluginCtx("search")).add("reindex", reindex)

    result = asyncio.run(_executor(registry)(_Task("reindex"), None))

    assert result == {"summary": "reindexed"}
    assert ran == ["reindex"]


def test_an_unknown_job_names_what_is_available() -> None:
    registry = SystemJobRegistry()

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_executor(registry)(_Task("rotate"), None))

    message = str(excinfo.value)
    assert "rotate" in message
    assert "this install has" in message, message


def test_unloading_a_plugin_removes_its_job_and_nothing_else() -> None:
    async def one(*, task, run, payload, cfg):
        return {"summary": "one"}

    async def two(*, task, run, payload, cfg):
        return {"summary": "two"}

    registry = SystemJobRegistry()
    first, second = _PluginCtx("a"), _PluginCtx("b")
    registry.scoped(first).add("alpha", one)
    registry.scoped(second).add("beta", two)

    first.unload()

    assert registry.names() == ("beta",)


def test_a_job_that_raises_is_what_the_run_records_as_failed() -> None:
    """A job must not swallow its error: the run's retry policy and its failure
    classification both read what comes out of here."""

    async def broken(*, task, run, payload, cfg):
        raise RuntimeError("index is corrupt")

    registry = SystemJobRegistry()
    registry.scoped(_PluginCtx("search")).add("reindex", broken)

    with pytest.raises(RuntimeError, match="index is corrupt"):
        asyncio.run(_executor(registry)(_Task("reindex"), None))


def test_two_plugins_claiming_one_job_name_is_refused() -> None:
    async def first(*, task, run, payload, cfg):
        return {"summary": "first"}

    async def second(*, task, run, payload, cfg):
        return {"summary": "second"}

    registry = SystemJobRegistry()
    registry.scoped(_PluginCtx("a")).add("prune", first)

    with pytest.raises(ValueError) as excinfo:
        registry.scoped(_PluginCtx("b")).add("prune", second)

    assert "already provided by a" in str(excinfo.value)


def test_a_replacement_is_restored_when_its_plugin_unloads() -> None:
    """The same undo-by-identity rule the delivery registry learned the hard way."""

    async def builtin(*, task, run, payload, cfg):
        return {"summary": "builtin"}

    async def replacement(*, task, run, payload, cfg):
        return {"summary": "replacement"}

    registry = SystemJobRegistry()
    registry.register("tidy", builtin)
    ctx = _PluginCtx("replacer")
    registry.scoped(ctx).add("tidy", replacement, replace=True)

    assert registry.job_for("tidy") is replacement
    ctx.unload()

    assert registry.job_for("tidy") is builtin
