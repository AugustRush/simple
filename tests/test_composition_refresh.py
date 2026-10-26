"""What a reload does with the *config*, and what it says it cannot do.

The gap this closes
-------------------
A reload re-read the plugin *directories* and never the `plugins` **section**, so the one
thing an operator (or the page's on/off switch) actually writes was the one thing a reload
ignored.  The web toggle worked around it by assigning ``catalog._plugin_config`` by hand --
a private poke from another layer, which is what a missing seam looks like from outside.

What a reload can and cannot apply
----------------------------------
* Which plugins load -- an entry's ``enabled`` flag, or the profile's entries -- is read by
  discovery for every plugin, so refreshing it here makes the change live.  That is the
  whole of the on/off switch.
* The composition *itself* -- the profile's core selection and its service replacements --
  decides which core plugins are mounted.  A **plugin reload** cannot change that (the
  services it would replace are held by everything that captured them), so it reports
  ``restart_required`` instead of pretending it applied something.  Applying it is
  ``PluginCatalog.recompose`` -- a whole-composition operation the caller owes an idle
  process to; ``tests/test_functional_turns.py`` proves it against a live gateway.

Two rules the tests below pin, because "reload" is an operation people run on a live
system: it never raises for a config problem, and a config it could not parse leaves the
running composition alone.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.plugins.catalog import PluginCatalog


_PLUGIN_SOURCE = '''
class Hello:
    name = "hello"

    def setup(self, ctx):
        ctx.provide("hello.marker", True)


def register():
    return Hello()
'''


def _catalog(tmp_path, *, plugin_config=None, loader=None) -> PluginCatalog:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir()
    plugin_dir = user / "hello"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(_PLUGIN_SOURCE)
    catalog = PluginCatalog(
        builtin_dir=builtin,
        user_dir=user,
        plugin_config=plugin_config or {},
    )
    if loader is not None:
        catalog.bind_composition(config_loader=loader)
    return catalog


def test_a_reload_picks_up_a_plugin_switched_off_in_the_config(tmp_path):
    """The half a reload used to ignore: the file, not the directory."""
    answer = {"plugins": {}}
    catalog = _catalog(tmp_path, loader=lambda: answer["plugins"])
    assert catalog.discover_and_load() == ["hello"]

    # The operator (or the page's switch) writes `enabled: false` and reloads.
    answer["plugins"] = {"hello": {"enabled": False}}
    result = asyncio.run(catalog.reload({}))

    assert result["removed_plugins"] == ["hello"], result
    assert catalog.get_loaded_names_for_directory("hello") == set()
    assert catalog.catch_up()["ok"] is True


def test_the_reload_reports_the_composition_and_what_is_still_owed(tmp_path):
    """`ok: true` plus silence reads as "everything applied", which is not the truth."""
    answer = {"plugins": {}}
    catalog = _catalog(tmp_path, loader=lambda: answer["plugins"])
    catalog.discover_and_load()

    answer["plugins"] = {"_profile": "sqlite-fts-memory"}
    result = asyncio.run(catalog.reload({}))

    assert result["ok"] is True
    assert result["composition"]["profile"] == "sqlite-fts-memory"
    assert result["restart_required"], "a core swap needs a restart and must say so"
    owed = " ".join(result["restart_required"])
    assert "sqlite-fts-memory" in owed, owed
    assert "memory" in owed, owed


def test_a_profile_only_change_without_core_swap_needs_no_restart(tmp_path):
    """Control: the default profile differs from the default profile in nothing."""
    answer = {"plugins": {}}
    catalog = _catalog(tmp_path, loader=lambda: answer["plugins"])
    catalog.discover_and_load()

    answer["plugins"] = {"_profile": "default"}
    result = asyncio.run(catalog.reload({}))

    assert result["composition"]["profile"] == "default"
    assert result["restart_required"] == []


def test_an_unknown_profile_name_is_reported_and_the_running_one_is_kept(tmp_path):
    """A live gateway must not go down for a typo in a file it just re-read.

    Construction still *raises* for an unknown profile -- a run under a narrowed
    composition must not quietly get the default set -- but a reload has a second job:
    the process is already serving, so the answer is a sentence, not a traceback.
    """
    answer = {"plugins": {}}
    catalog = _catalog(tmp_path, loader=lambda: answer["plugins"])
    catalog.discover_and_load()
    before = catalog.core_plugin_selection()

    answer["plugins"] = {"_profile": "no-such-profile"}
    report = catalog.catch_up()

    assert report["ok"] is False
    assert "no-such-profile" in report["error"]
    assert catalog.core_plugin_selection() == before, "the running profile is untouched"


def test_a_config_that_cannot_be_read_does_not_fail_the_reload(tmp_path):
    def broken():
        raise RuntimeError("disk went away")

    catalog = _catalog(tmp_path, loader=broken)
    catalog.discover_and_load()

    result = asyncio.run(catalog.reload({}))

    # The reload itself still runs: the plugins on disk are what it can read.
    assert result["ok"] is True
    assert result["composition"]["ok"] is False
    assert "disk went away" in result["composition"]["error"]


def test_without_a_loader_the_construction_time_answer_is_kept(tmp_path):
    """A bare catalog (a test, an embedder) has no config to re-read, and that is fine."""
    catalog = _catalog(tmp_path, plugin_config={"hello": {"enabled": False}})
    catalog.discover_and_load()

    report = catalog.catch_up()

    assert report["ok"] is True
    assert catalog.get_loaded_names_for_directory("hello") == set()


# ── the single wiring call, and recompose being safe to call ────────────────


def test_binding_mounts_and_reports_what_it_mounted(tmp_path):
    """One call instead of three, and the record is the mount's own return value.

    The three calls it replaced were ``set_config_loader``,
    ``set_composition_mounter`` and ``note_core_mounts`` -- and the third could describe
    a mount that never happened, because nothing tied it to the second.  Here the names
    come back from the act itself.
    """
    catalog = _catalog(tmp_path)
    mounted: list[str] = []

    def mount_core() -> list[str]:
        mounted.append("mounted")
        return ["registry", "memory", "session"]

    names = catalog.bind_composition(
        config_loader=lambda: {"_profile": "core"},
        mount_core=mount_core,
    )

    assert names == ("registry", "memory", "session")
    assert mounted == ["mounted"], "binding is the first mount, not a setter"


def test_a_reader_without_a_mounter_can_still_report(tmp_path):
    """An embedder may want the config re-read without giving the catalog the ability to
    rebuild the process.  That is a legitimate binding; only ``recompose`` is refused."""
    catalog = _catalog(tmp_path)
    assert catalog.bind_composition(config_loader=lambda: {}) == ()

    report = catalog.catch_up(apply=False)
    assert report["ok"] is True

    result = asyncio.run(catalog.recompose())
    assert result["ok"] is False
    assert "mounter" in result["error"]


def test_recompose_with_nothing_owed_does_not_remount(tmp_path):
    """Safe to call, not merely correct when called: an unchanged composition is a no-op.

    Without the guard, a page that says "apply" on an unchanged config would stop and
    restart every core service -- after the caller had already dropped its session
    runtimes -- to arrive back where it started.
    """
    catalog = _catalog(tmp_path)
    mounts: list[str] = []

    def mount_core() -> list[str]:
        mounts.append("mount")
        return ["registry"]

    catalog.bind_composition(config_loader=lambda: {"_profile": "core"}, mount_core=mount_core)
    assert mounts == ["mount"]

    result = asyncio.run(catalog.recompose())

    assert result["ok"] is True
    assert result["needs"] == "nothing", result
    assert mounts == ["mount"], "a no-op recompose must not touch the mounted tree"
