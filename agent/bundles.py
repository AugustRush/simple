"""Bundles: named sets of configuration overrides.

The relationship to plugin profiles
-----------------------------------
``agent/plugins/profiles.py`` answers "which plugins load".  A bundle answers the
larger question, "what is this composition": the plugin profile is one of the
things a bundle can set, alongside scheduler timings, channel enablement and
memory behaviour.  Both are *named data* rather than branches, which is the shape
``agent/scheduler/profiles.py`` established for permissions and which the plan
reuses deliberately instead of inventing a second mechanism.

One table, applied in order
---------------------------
A bundle is an ordered list of :class:`~agent.config_patch.PatchEntry`, so a bundle
can compose another by *including its entries* rather than by referencing it:

    BASE_PATCHES = (entry("evolution.enabled", True),)
    HEADLESS = Bundle(..., patches=BASE_PATCHES + (entry("channels.web.enabled", False),))

Textual composition rather than a name reference keeps the result computable
without a resolver pass, and keeps a bundle's effect readable in one place.  The
cost is that a shared fragment is duplicated in the table's source, not in the
data -- which is the right trade for a table this size and would not be for a
hundred rows.

Bundles are opt-in
------------------
``DEFAULT_BUNDLE`` applies nothing.  A bundle that changed behaviour by existing
would mean a configuration that was not asked for, and the way to say "no
composition" has to be available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from agent.config_patch import (
    PatchEntry,
    UnknownConfigPath,
    apply_patch,
    entry,
)

__all__ = [
    "DEFAULT_BUNDLE_NAME",
    "DEFAULT_BUNDLE",
    "BUNDLES",
    "Bundle",
    "KnownBundleError",
    "bundle_names",
    "resolve_bundle",
    "apply_bundle",
]


class KnownBundleError(ValueError):
    """Raised for a bundle *name* that does not exist.

    Loud rather than falling back, for the same reason the plugin-profile
    equivalent is: a run under a deliberately narrowed composition that quietly
    got the full one is worse than a startup failure that names the typo.
    """

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        super().__init__(
            f"unknown bundle {name!r}; known: {', '.join(known) or '(none)'}"
        )
        self.name = name
        self.known = known


@dataclass(frozen=True)
class Bundle:
    """A named composition: the overrides that make it what it is."""

    key: str
    label: str
    summary: str
    patches: tuple[PatchEntry, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Public shape, so a UI lists bundles without hardcoding them."""
        return {
            "key": self.key,
            "label": self.label,
            "summary": self.summary,
            "patches": [
                {"path": patch.path, "value": patch.value} for patch in self.patches
            ],
        }

    def validate(self, reference: Mapping[str, Any]) -> None:
        """Raise if any patch names a path *reference* does not declare.

        The runtime application is deliberately lenient (see :func:`apply_bundle`),
        so this is where a bundle's paths are actually checked.  It exists as a
        method rather than only as a test so that a bundle author has something to
        call: a typo caught by CI is good, and a typo caught before the bundle is
        added to the table is better.
        """
        for patch in self.patches:
            node: Any = reference
            for part in patch.path.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    raise UnknownConfigPath(patch.path, reference)
                node = node[part]


#: A fragment: things every bundle here wants, declared once as data.
#:
#: Every path here is checked against ``DEFAULT_CONFIG`` by
#: ``tests/test_bundles.py``.  That check earned its place immediately: the first
#: version of this table named ``channels.web.enabled`` and
#: ``orchestration.sub_agent_retries``, and *neither exists* -- ``channels``
#: defaults to ``None`` and the retry count is not a declared default at all.
#: ``get`` returned ``None`` for both, so the mistake was invisible.
_COMMON = (
    entry("evolution.enabled", True),
    entry("memory.tidy_interval_seconds", 3600),
)

#: The named compositions.  Two rows, because the mechanism is the point and a
#: second row is what proves the first is not a special case.
BUNDLES: dict[str, Bundle] = {
    "default": Bundle(
        key="default",
        label="默认",
        summary="应用任何 bundle 都不改动配置；与不带 bundle 运行时完全一致。",
        patches=(),
    ),
    "unattended": Bundle(
        key="unattended",
        label="无人值守",
        summary="关掉需要人在场的通道，缩短任务租约，让无人运行时失败得更快。",
        patches=_COMMON + (
            # Poll often: nobody is watching, so a trigger that fires now should
            # not wait a full idle interval to be noticed.  The lease stays above
            # the poll interval, or every run would look abandoned to the next
            # poll and be claimed twice.
            entry("scheduler.poll_seconds", 10),
            entry("scheduler.lease_seconds", 120),
            # Fewer concurrent runs, because there is no human to notice the
            # machine struggling and no interactive turn to protect.
            entry("scheduler.max_concurrent_runs", 1),
            entry("memory.tidy_interval_seconds", 900),
        ),
    ),
}

#: What a composition gets when it names no bundle.  The same object as the table
#: row, so "the default" cannot drift from "the bundle called default".
DEFAULT_BUNDLE_NAME = "default"
DEFAULT_BUNDLE = BUNDLES[DEFAULT_BUNDLE_NAME]


def bundle_names() -> tuple[str, ...]:
    return tuple(BUNDLES)


def resolve_bundle(name: str) -> Bundle:
    try:
        return BUNDLES[name]
    except KeyError:
        raise KnownBundleError(name, bundle_names()) from None


def apply_bundle(
    config: Mapping[str, Any],
    bundle: Optional[Bundle] = None,
) -> dict[str, Any]:
    """*config* with *bundle*'s overrides applied.

    ``bundle=None`` means the default bundle, which applies nothing -- so a caller
    that names no composition gets its configuration back, copied.

    The patches are applied with ``allow_new``, and that is safe *because of the
    test, not despite it*: ``test_every_patch_names_a_declared_path`` asserts every
    path in every bundle against ``DEFAULT_CONFIG``, so a typo fails in CI rather
    than in a run.  Re-checking at apply time would add nothing and would break a
    real case -- ``load_config_for_run``'s *fallback* is a caller's last
    known-good config, which need not contain every section a bundle touches, and
    refusing there would drop the composition exactly when the file was
    unreadable.  The strict check remains the default in
    :func:`agent.config_patch.apply_patch`, where a caller writing an ad-hoc patch
    has no such guard.
    """
    active = bundle if bundle is not None else DEFAULT_BUNDLE
    patches = tuple(
        PatchEntry(path=item.path, value=item.value, allow_new=True)
        for item in active.patches
    )
    return apply_patch(config, patches)
