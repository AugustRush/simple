"""A plugin that never loaded is not the same as a plugin that is not installed.

``list_plugins`` answered only the *loaded* ones, so a plugin waiting for a
service and a plugin whose ``setup`` raised were both simply absent from the
capabilities page -- and absent reads exactly like "never installed". The three
states need different fixes, so they have to be distinguishable.

The kernel also logged nothing on the two transitions a person actually looks
for: "did my plugin come up" and "is it waiting". A successful composition and a
composition missing a plugin both had nothing to say.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent.kernel.plugin import PluginHost


class _Needs:
    def __init__(self, name: str, inject: tuple[str, ...]) -> None:
        self.name = name
        self.inject = inject

    def setup(self, ctx) -> None:
        pass


class _Breaks:
    name = "breaks"

    def setup(self, ctx) -> None:
        raise RuntimeError("boom")


class _Fine:
    name = "fine"

    def setup(self, ctx) -> None:
        pass


def test_a_pending_plugin_says_what_it_waits_for(caplog) -> None:
    host = PluginHost()
    caplog.set_level(logging.INFO)
    host.load(_Needs("waits", ("nobody_provides_this",)))

    assert host.pending() == ("waits",)
    assert "waiting for" in caplog.text
    assert "nobody_provides_this" in caplog.text


def test_an_activated_plugin_says_so(caplog) -> None:
    """The transition a person looks for, and the one that was silent."""
    host = PluginHost()
    caplog.set_level(logging.INFO)
    host.load(_Fine())

    assert "active" in caplog.text
    assert "fine" in caplog.text


def test_an_unloaded_plugin_says_so(caplog) -> None:
    host = PluginHost()
    host.load(_Fine())
    caplog.set_level(logging.INFO)
    host.unload("fine")

    assert "unloaded" in caplog.text


def test_the_catalog_reports_the_state_of_every_plugin_it_knows(tmp_path) -> None:
    """Loaded, waiting and failed are three answers, not two."""
    from agent.plugins.catalog import PluginCatalog

    catalog = PluginCatalog(
        builtin_dir=tmp_path / "builtin", user_dir=tmp_path / "user"
    )
    catalog.mount(_Fine())
    catalog.mount(_Needs("waits", ("absent_service",)))
    catalog.mount(_Breaks())

    states = catalog.plugin_states()

    assert states["fine"]["state"] == "active"
    assert states["waits"]["state"] == "pending"
    assert states["waits"]["missing"] == ["absent_service"]
    assert states["breaks"]["state"] == "failed"
    assert "boom" in states["breaks"]["error"]


def test_the_payload_separates_what_never_loaded() -> None:
    """A pending plugin has no version or description -- drawing it as a card
    would invent facts it does not have."""
    source = (
        Path(__file__).resolve().parents[1] / "agent" / "channels" / "web.py"
    ).read_text(encoding="utf-8")
    body = source[source.index("async def _plugins(") :][:3000]

    assert '"unavailable"' in body
    assert '"state"' in body
    assert '"missing"' in body
