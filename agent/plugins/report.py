"""One answer to "what is this, and what would it run" -- for every adoption path.

Why a report
-------------
`install_plugin`, a future `import_dsh_profile`, and a future npm source each ask the same
question of a directory of somebody else's files, and each was going to answer it its own
way.  The design that asked for this
(`docs/superpowers/specs/2026-10-12-capability-provenance-and-adapter-translators-design.md`)
puts it better than a summary can: three producers of a report, one consumer of the
confirmation, instead of three special cases.

Where each field comes from
---------------------------
* ``would_run`` -- the **format adapters**, because what counts as code is the ecosystem's
  answer (`formats.executions`), and not the installer's.
* ``inert`` -- translations nothing dispatches.  Reported rather than refused: an inert hook
  is not a reason to say no, it is a reason not to let somebody believe it will work.
* ``approved`` -- the *in-process* answer today (``plugin_approval``), which is what the
  interactive round trip needs.  A durable one, keyed by a digest of what would run, is the
  next step; until then this field says what is true and no more.
* ``unsupported`` -- recognised and not adopted, with the reason.  Empty for a directory
  today: nothing yet refuses a part of a plugin rather than all of it.

What the report is *not*: a permission.  It describes; the caller decides and asks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent.runtime.provenance import CORE, PLUGIN, Execution, Provenance

__all__ = ["CapabilityReport", "capability_report"]


@dataclass(frozen=True)
class CapabilityReport:
    """What adopting *source* would do, in the terms a person decides in."""

    source: Provenance
    would_run: tuple[Execution, ...] = ()
    #: Declarations that were understood and will do nothing -- a hook event this runtime
    #: has no dispatcher for, most often.
    inert: tuple[str, ...] = ()
    #: From the approval record, by digest once that exists.  Today: the in-process answer.
    approved: bool = False
    unsupported: tuple[str, ...] = ()

    @property
    def executes(self) -> bool:
        """Whether adopting this means running somebody else's code.

        The question the installer gates on, asked of the *report* rather than of a string
        that was built separately: two answers to one question is how a gate ends up
        gating on something the human was not shown.
        """
        return bool(self.would_run)

    def reason(self) -> str:
        """The one-line account shown in the confirmation, or ``""`` when nothing runs.

        Names every executable, and the concrete command when the plugin's own files say
        it.  The version this replaced returned the first marker it found, so a plugin with
        an entry point and two MCP servers asked about one of three.
        """
        parts = [item.describe() for item in self.would_run]
        if self.inert:
            parts.append("inert: " + ", ".join(self.inert))
        return "; ".join(parts)

    def summary(self) -> dict:
        """A JSON-able view, for a tool result or a log line."""
        return {
            "source": self.source.source,
            "kind": self.source.kind,
            "would_run": [
                {"kind": item.kind, **item.payload} for item in self.would_run
            ],
            "inert": list(self.inert),
            "approved": self.approved,
            "unsupported": list(self.unsupported),
        }


def capability_report(
    root: Path,
    *,
    source: str = "",
    kind: str = PLUGIN,
    approved: bool = False,
    inert: tuple[str, ...] = (),
    unsupported: tuple[str, ...] = (),
) -> CapabilityReport:
    """Build the report for a plugin directory.

    *source* is the string a human was shown when they chose this -- a URL, a path, or
    ``core:<name>`` -- not an internal identifier: the report exists so that a person can
    act on it, and a provenance nobody can go and look at is not one.
    """
    from agent.plugins import formats as plugin_formats

    return CapabilityReport(
        source=Provenance(source=source, kind=kind),
        would_run=plugin_formats.executions(root),
        inert=tuple(inert),
        approved=approved,
        unsupported=tuple(unsupported),
    )
