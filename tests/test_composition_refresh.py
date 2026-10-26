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
  decides which core plugins are mounted, and mounting is the composition root's act: it
  runs once, and what it produced is what every long-lived holder captured.  Re-mounting
  that in place would rebuild the process's spine under a serving gateway, so a reload
  **reports** ``restart_required`` rather than pretending it applied something.

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
        catalog.set_config_loader(loader)
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
    assert catalog.refresh_composition()["ok"] is True


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
    report = catalog.refresh_composition()

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

    report = catalog.refresh_composition()

    assert report["ok"] is True
    assert catalog.get_loaded_names_for_directory("hello") == set()
