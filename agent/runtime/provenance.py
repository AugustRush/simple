"""Where a capability came from, and what it can run.

Two questions the plugin model could not answer, and the shape of the answer.

The design that asked for this
------------------------------
``docs/superpowers/specs/2026-10-12-capability-provenance-and-adapter-translators-design.md``
measured the state of trust in this tree and found that "a capability is a name bound to an
object": nothing on it says who declared it, what it will execute, or whether anyone
approved it.  Its first migration step was to pass ``provenance=`` at every ``provide``
site.

That step is wrong, and this module is the corrected shape.  At ``provide`` time the kernel
**already knows the declaring plugin** -- ``Context.name`` is ``install.name`` -- and the
plugin's source, kind and directory are facts the loader has.  Passing them again at each of
the thirty call sites would be a hand-maintained mirror of a record that already exists,
which is exactly the defect the service ledger was cured of: *a fact the source can derive
does not belong in a declaration.*  So provenance attaches to the **plugin**, once, where it
is discovered; a capability's provenance is then derived (``who_provides`` -> plugin ->
provenance).

What is deliberately absent
---------------------------
``digest`` is empty for now.  It is the step that makes approval durable ("an edit is not
the same approval"), and it needs the ``Execution`` list and the ledger before it means
anything: a digest of a record that is never compared against an approval is a decoration.
Empty is honest; a placeholder hash would not be.

``Execution`` is defined here because it is the *other* half of the invariant and the shape
the format adapters will fill, not because anything produces it yet.  What a plugin can run
is the ecosystem's answer -- ``executable_files``, ``hooks_files``, ``mcp_files`` are
already declared per format -- so the producers belong to the adapters, and this module owns
only the type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Execution", "Provenance", "CORE", "PLUGIN", "IMPORT"]

#: The three provenances a capability can have.  ``core`` is shipped with the agent,
#: ``plugin`` arrived from a directory a human put there, ``import`` from another
#: application's profile -- nothing produces ``import`` yet; the DSH import does, and this
#: is the vocabulary it will use.
CORE = "core"
PLUGIN = "plugin"
IMPORT = "import"


@dataclass(frozen=True)
class Provenance:
    """One plugin's answer to "where did this come from".

    Frozen because it is a *record of a decision made earlier* -- the moment a plugin was
    discovered, installed or imported -- and a mutable one would let the answer drift from
    the thing it describes.
    """

    #: A string a human can act on: a path they can open, or ``core:<name>`` for a plugin
    #: that ships with the agent.  Not an opaque id -- the point of provenance is that a
    #: person can go and look.
    source: str
    kind: str = PLUGIN
    #: The install that loaded this one, or ``""`` for a root.  A diagnostic asks "why is
    #: this plugin present", and for a nested install the answer is another plugin.
    declared_by: str = ""
    #: Of what will run, so that editing a hook command is not the same approval.
    #: Empty until the digest step: a digest nothing compares is a decoration.
    digest: str = ""

    @property
    def is_core(self) -> bool:
        """Whether this came with the agent, and is not a plugin's to replace."""
        return self.kind == CORE


@dataclass(frozen=True)
class Execution:
    """One thing a capability can actually run.

    ``kind`` names the mechanism, not the ecosystem: two formats that both spawn a process
    over stdio produce the same kind, because the human being asked is answering the same
    question.  ``payload`` is exactly what would run -- the command, its arguments, its
    environment -- so a rendering of a list of these is a truthful answer to "what will this
    do if I say yes".
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        """One line for a human: the mechanism, then the concrete thing it runs."""
        command = self.payload.get("command") or self.payload.get("path") or ""
        if isinstance(command, (list, tuple)):
            command = " ".join(str(part) for part in command)
        return f"{self.kind}: {command}" if command else self.kind
