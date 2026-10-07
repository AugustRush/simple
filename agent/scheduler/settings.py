"""The scheduler's run parameters, resolved in one place.

Why this module exists
----------------------
Four settings -- how often to poll, how long a lease lasts, how many runs may overlap,
how deep a signal chain may go -- were resolved at **two** places in ``cli.py``: the
interactive path and the ``scheduler`` command.  Both read ``cfg["scheduler"]`` with the
same four key names and the same four defaults, and the second additionally applied two
command-line flags.

They agree today.  That is the problem: they agree because someone wrote the same
defaults twice, and the next setting added to one of them would be a scheduler that
behaves differently depending on how it was started -- with no test that could tell the
difference, because each site is exercised by its own path.

So the resolution is a value with one constructor, and the command-line flags are
*parameters to it* rather than a second code path.  ``SchedulerSettings.resolve(cfg,
poll_seconds=..., lease_seconds=...)`` is that constructor, and it is the only place
that knows a key name or a default.

Only the flags that exist are parameters
----------------------------------------
``scheduler`` accepts ``--poll-seconds`` and ``--lease-seconds`` and nothing else, so
only those two can override.  Adding a flag means adding a parameter here, which is the
point: today the two override sites are load-bearing and invisible, and a new flag
applied at one of them would silently do nothing at the other.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from agent.scheduler.models import DEFAULT_SIGNAL_MAX_DEPTH

__all__ = ["SchedulerSettings", "RESOLUTION_DEFAULTS"]

#: What a setting falls back to when neither config nor argv supplies it.
#:
#: Named so a test can assert the defaults *as a set* rather than one at a time: the two
#: call sites this replaces agreed on all four, and re-typing them here is what would let
#: them drift again.
RESOLUTION_DEFAULTS: dict[str, Any] = {
    "poll_seconds": 30.0,
    "lease_seconds": 300,
    "max_concurrent_runs": 3,
    "signal_max_depth": DEFAULT_SIGNAL_MAX_DEPTH,
}


class SchedulerSettings(NamedTuple):
    """What the scheduler was told to do, with argv beats config.

    A ``NamedTuple`` rather than a dataclass because the two call sites pass these
    straight into ``_build_scheduler_service(...)`` as keyword arguments, and a tuple is
    also how a test compares them: two runs with the same settings are equal, which is
    the property the duplication made unassertable.
    """

    poll_seconds: float
    lease_seconds: int
    max_concurrent_runs: int
    signal_max_depth: int

    @classmethod
    def resolve(
        cls,
        cfg: dict | None,
        *,
        poll_seconds: float | None = None,
        lease_seconds: int | None = None,
    ) -> "SchedulerSettings":
        """Resolve the settings from *cfg* and the two flags that can override it.

        A flag of ``None`` means "not given", and applies the config value.  A flag of
        ``0`` means "given as zero" and wins -- which is why the test is ``is None``
        rather than ``or``: ``poll_seconds=0`` is rejected by the CLI's ``min=0.1``, but
        the falsy form would treat any future zero-valued setting as absent, and that
        bug is silent and depends on the value rather than on whether the user spoke.
        """
        section = (cfg or {}).get("scheduler", {}) or {}

        def choose(flag: Any, key: str, cast: Any) -> Any:
            if flag is not None:
                return cast(flag)
            return cast(section.get(key, RESOLUTION_DEFAULTS[key]))

        return cls(
            poll_seconds=choose(poll_seconds, "poll_seconds", float),
            lease_seconds=choose(lease_seconds, "lease_seconds", int),
            max_concurrent_runs=int(
                section.get(
                    "max_concurrent_runs", RESOLUTION_DEFAULTS["max_concurrent_runs"]
                )
            ),
            signal_max_depth=int(
                section.get(
                    "signal_max_depth", RESOLUTION_DEFAULTS["signal_max_depth"]
                )
            ),
        )
