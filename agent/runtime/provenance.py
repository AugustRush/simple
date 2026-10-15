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

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Execution",
    "Provenance",
    "CORE",
    "PLUGIN",
    "IMPORT",
    "digest_of",
    "content_digest",
]

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
        subject = self._subject()
        return f"{self.kind}: {subject}" if subject else self.kind

    def command_summary(self) -> str:
        """The concrete commands this would run, or ``""`` when the files do not say.

        Separate from :meth:`describe` because the two callers want different text around
        the same fact: a diagnostic wants the mechanism, and the installer's confirmation
        already renders the *ecosystem's* name for the marker in the user's language.  What
        it was missing is this -- the command itself.
        """
        commands = self._commands()
        if not commands:
            return ""
        return self._with_remainder(_render_command(commands[0]), len(commands) - 1)

    def _subject(self) -> str:
        # Commands first: the thing that runs is the answer, and the file it lives in is
        # only where to go and look.  Then the marker, which is relative and meaningful in
        # the plugin's own terms; the absolute path last, because it is the least useful to
        # a person deciding.
        return (
            self.command_summary()
            or str(self.payload.get("marker") or "")
            or str(self.payload.get("path") or "")
        )

    def _commands(self) -> list[str]:
        """Every concrete command this execution would run, best effort.

        ``command`` (one) as well as ``commands`` (several) and ``servers``: the three
        shapes a producer reasonably writes, and dropping the singular one was a silent
        regression when this was refactored -- ``describe()`` returned a bare kind for a
        payload that named its command.
        """
        payload = self.payload
        if payload.get("command"):
            # With its arguments: ``npx`` alone is not what runs, and a person being asked
            # about an MCP server is answering about ``npx -y some-server``.  A command that
            # already arrives as a list keeps its own parts and the arguments follow it --
            # wrapping it would render the list's repr as the command.
            command = payload["command"]
            args = list(payload.get("args") or ())
            parts = list(command) + args if isinstance(command, (list, tuple)) else [command, *args]
            return [_render_command(parts)]
        if payload.get("commands"):
            return [str(item) for item in payload["commands"]]
        found: list[str] = []
        for server in payload.get("servers") or ():
            if not isinstance(server, dict):
                continue
            command = server.get("command")
            if not command:
                continue
            args = server.get("args") or ()
            if isinstance(command, (list, tuple)):
                found.append(" ".join(str(part) for part in command))
            else:
                found.append(" ".join(str(part) for part in (command, *args)))
        return found

    @staticmethod
    def _with_remainder(subject: str, remaining: int) -> str:
        return subject if remaining <= 0 else f"{subject} (+{remaining} more)"


def _render_command(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


# ── the digest: an edit is not the same approval ─────────────────────────────


def content_digest(data: bytes | str) -> str:
    """A short, stable digest of the bytes something will run.

    SHA-256 truncated to 16 hex characters.  Short because this ends up inside a *rule
    string* a human may be shown and that gets written to a ledger, and 64 bits is far more
    than enough to make an accidental collision impossible while keeping the line readable.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def digest_of(executions: "tuple[Execution, ...] | list[Execution]") -> str:
    """The digest of everything adopting this would run, or ``""`` when nothing runs.

    Two deliberate properties:

    * **Content, not location.**  The absolute path is *not* hashed.  Moving a plugin
      directory does not change what it runs, and a digest that changed on a move would
      re-ask a question whose answer was already given -- teaching the human to approve
      without reading, which is how a consent gate stops working.
    * **Contents, not just names.**  Each item's own digest is over the bytes of the file
      (or the canonical JSON of the declaration).  A digest over marker *names* would say
      that an edited ``__init__.py`` is the plugin that was approved, which for arbitrary
      code is the whole risk.

    ``""`` when there is nothing to run: the caller's "no executables" case, where a digest
    would be a number with no question behind it.
    """
    if not executions:
        return ""
    # The **whole payload, minus the path**, rather than a ``digest`` field the producer may
    # or may not have set.  The first version read only ``payload["digest"]``, so a producer
    # that built its executions directly -- the DSH import, whose servers are declarations
    # rather than files -- produced a digest over *names alone*, which is the hole this
    # docstring warns about, arrived at by omission instead of by choice.  Hashing the
    # payload makes content-sensitivity the default and leaves nothing for a caller to
    # forget; ``path`` is dropped because it is the one field that names a *location*.
    parts = [
        {
            "kind": item.kind,
            "content": content_digest(
                json.dumps(
                    {key: value for key, value in item.payload.items() if key != "path"},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            ),
        }
        for item in executions
    ]
    canonical = json.dumps(
        sorted(parts, key=lambda part: (part["kind"], part["content"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return content_digest(canonical)
