"""The scheduler's run parameters come from one resolution.

These were resolved twice in ``cli.py`` -- the interactive path and the ``scheduler``
command -- with the same four key names and the same four defaults typed out at each
site.  They agreed, which is why nobody noticed; the point of the tests below is that
"they agree" becomes a fact rather than a coincidence.

The property that could not be stated before is at the bottom: **both entry points
resolve the same settings.**  With two independent resolutions there was nothing to
compare, and a setting added to one of them would have produced a scheduler that behaved
differently depending on how it was started.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.scheduler.models import DEFAULT_SIGNAL_MAX_DEPTH
from agent.scheduler.settings import RESOLUTION_DEFAULTS, SchedulerSettings

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Defaults ─────────────────────────────────────────────────────────────────


def test_an_empty_config_gives_the_documented_defaults() -> None:
    settings = SchedulerSettings.resolve({})
    assert settings == SchedulerSettings(
        poll_seconds=30.0,
        lease_seconds=300,
        max_concurrent_runs=3,
        signal_max_depth=DEFAULT_SIGNAL_MAX_DEPTH,
    )


def test_the_defaults_are_declared_as_a_set() -> None:
    """Asserted together, because the failure mode was per-setting drift.

    Two sites each spelled out four defaults.  Re-typing them here one at a time would
    reproduce the shape of the bug; naming the set means a change to one is a change to
    the declaration rather than to a line in a function.
    """
    assert RESOLUTION_DEFAULTS == {
        "poll_seconds": 30.0,
        "lease_seconds": 300,
        "max_concurrent_runs": 3,
        "signal_max_depth": DEFAULT_SIGNAL_MAX_DEPTH,
    }


def test_a_missing_cfg_is_the_same_as_an_empty_one() -> None:
    """``cfg`` is optional because a test or an embedder may have none.

    A ``None`` that raised would be a crash in a path that has nothing to do with the
    scheduler's behaviour, and the defaults are a complete answer.
    """
    assert SchedulerSettings.resolve(None) == SchedulerSettings.resolve({})


def test_an_empty_scheduler_section_is_the_same_as_none() -> None:
    assert SchedulerSettings.resolve({"scheduler": {}}) == SchedulerSettings.resolve({})
    assert SchedulerSettings.resolve({"scheduler": None}) == SchedulerSettings.resolve({})


# ── Config beats defaults; argv beats config ─────────────────────────────────


def test_config_beats_the_defaults() -> None:
    settings = SchedulerSettings.resolve(
        {"scheduler": {"poll_seconds": 5, "lease_seconds": 60,
                       "max_concurrent_runs": 9, "signal_max_depth": 2}}
    )
    assert settings == SchedulerSettings(
        poll_seconds=5.0, lease_seconds=60, max_concurrent_runs=9, signal_max_depth=2
    )


def test_the_flags_beat_the_config() -> None:
    """The ``scheduler`` command's two options, and only those two."""
    settings = SchedulerSettings.resolve(
        {"scheduler": {"poll_seconds": 5, "lease_seconds": 60}},
        poll_seconds=1.5,
        lease_seconds=7,
    )
    assert settings.poll_seconds == 1.5
    assert settings.lease_seconds == 7


def test_a_flag_of_none_means_not_given() -> None:
    settings = SchedulerSettings.resolve(
        {"scheduler": {"poll_seconds": 5}}, poll_seconds=None
    )
    assert settings.poll_seconds == 5.0


def test_a_zero_flag_is_honoured_rather_than_treated_as_absent() -> None:
    """``is None``, not ``or``: absent and zero are different statements.

    The form this replaces was ``poll_seconds or sched_cfg.get(...)``, which is
    indistinguishable from correct only while no setting can legitimately be falsy.  The
    CLI's ``min=0.1`` keeps zero out of ``--poll-seconds`` today, but the bug that form
    invites depends on the *value* rather than on whether the user spoke, so a future
    setting that can be zero would silently fall back to config.
    """
    settings = SchedulerSettings.resolve(
        {"scheduler": {"max_concurrent_runs": 9}}, lease_seconds=0
    )
    assert settings.lease_seconds == 0


def test_the_values_are_cast_the_way_the_service_expects_them() -> None:
    """``poll_seconds`` is a float and the other three are ints.

    Config files are JSON, so a hand-written ``300.0`` for ``lease_seconds`` arrives as a
    float; the service multiplies and compares these, and a float lease would reach the
    store as ``300.0``.
    """
    settings = SchedulerSettings.resolve(
        {"scheduler": {"poll_seconds": "12", "lease_seconds": 300.0,
                       "max_concurrent_runs": "4", "signal_max_depth": 2.0}}
    )
    assert isinstance(settings.poll_seconds, float) and settings.poll_seconds == 12.0
    assert isinstance(settings.lease_seconds, int) and settings.lease_seconds == 300
    assert isinstance(settings.max_concurrent_runs, int)
    assert isinstance(settings.signal_max_depth, int)


def test_two_resolutions_with_the_same_inputs_are_equal() -> None:
    """A value, so a test can compare two entry points.  The duplication made this
    unstatable: two code paths produced two tuples with nothing to compare."""
    config = {"scheduler": {"poll_seconds": 5, "lease_seconds": 60}}
    assert SchedulerSettings.resolve(config) == SchedulerSettings.resolve(config)
    assert SchedulerSettings.resolve(config) != SchedulerSettings.resolve(
        config, poll_seconds=1
    )


# ── The property the duplication made unassertable ───────────────────────────


def test_no_call_site_resolves_the_settings_itself() -> None:
    """``cli.py`` must have no second reading of the scheduler's config keys.

    This is the check that would have caught the original shape.  Two sites reading
    ``cfg["scheduler"]`` with their own defaults look correct in isolation and are only
    wrong relative to each other, so the assertion is on the *absence* of a second
    resolution rather than on the behaviour of either one.

    ``cli.py`` still builds the settings object for each entry point -- that is the
    shared call -- but it must not reach for the keys directly.
    """
    source = (REPO_ROOT / "agent/cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        # `sched_cfg.get("poll_seconds")` / `cfg.get("scheduler", {}).get(...)`
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "get" or not node.args:
                continue
            key = node.args[0]
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if key.value in RESOLUTION_DEFAULTS:
                offenders.append(f"line {node.lineno}: .get({key.value!r})")

    assert not offenders, (
        "the scheduler's settings are resolved in agent/scheduler/settings.py; "
        "cli.py reads the raw keys again at:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_would_notice_a_second_resolution() -> None:
    """Control: an assertion on absence passes trivially if the scan matches nothing.

    Paired with the check above, which is the wrong shape to leave unpaired -- a rule
    that stops matching reports a clean tree.
    """
    probe = ast.parse('cfg.get("scheduler", {}).get("poll_seconds", 30)\n')
    found = []
    for node in ast.walk(probe):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant) and key.value in RESOLUTION_DEFAULTS:
                    found.append(key.value)
    assert found, "the scan must notice a direct read of a scheduler setting key"


def test_both_entry_points_resolve_the_same_settings() -> None:
    """Both call the one constructor, so the same config yields the same settings.

    The interactive path passes no flags; the ``scheduler`` command passes the two it
    has.  Given the same config and no flags, they must agree -- which, with the
    resolution inlined at each site, was true by inspection and by nothing else.
    """
    config = {
        "scheduler": {
            "poll_seconds": 7,
            "lease_seconds": 45,
            "max_concurrent_runs": 2,
            "signal_max_depth": 3,
        }
    }
    interactive = SchedulerSettings.resolve(config)
    command = SchedulerSettings.resolve(config, poll_seconds=None, lease_seconds=None)
    assert interactive == command


@pytest.mark.parametrize("key", sorted(RESOLUTION_DEFAULTS))
def test_every_setting_is_reachable_from_config(key: str) -> None:
    """Each key actually travels, rather than being read from the defaults forever."""
    section = {"poll_seconds": 1.0, "lease_seconds": 2,
               "max_concurrent_runs": 3, "signal_max_depth": 4}
    settings = SchedulerSettings.resolve({"scheduler": {key: section[key]}})
    assert getattr(settings, key) == section[key]


def test_cli_can_actually_reach_the_resolution() -> None:
    """The call sites must be able to see the name.

    Written after getting this wrong: the import was added by matching a line of source
    text, the line was in a different file, and the replacement therefore did nothing
    and said nothing.  Every test in this file passed -- they import the resolution
    directly and never touch ``cli.py`` -- and the failure appeared only in the full
    suite, as a ``NameError`` at the call site.

    A source edit that matches nothing is indistinguishable from one that worked, so the
    check is on the *import being present* rather than on the text of an edit.
    """
    tree = ast.parse((REPO_ROOT / "agent" / "cli.py").read_text(encoding="utf-8"))
    imported = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "agent.scheduler.settings"
        and any(alias.name == "SchedulerSettings" for alias in node.names)
    ]
    assert imported, "cli.py must import SchedulerSettings; its call sites use the name"

    # And the call sites exist, so the import is not vestigial.
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "SchedulerSettings"
    ]
    assert len(calls) >= 2, (
        f"both entry points resolve through the one constructor; found {len(calls)}"
    )
