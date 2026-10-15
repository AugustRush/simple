"""Slash commands as a capability, owned by one registry.

Why this exists
---------------
A slash command is stored in three parallel dicts: the handler, its metadata for
``/help``, and the owning plugin's name (so a conflict can name who holds the name).
Two code paths wrote all three by hand -- the plugin's Python contribution
(``register_slash_commands``) and the declarative ``commands/*.md`` loader -- and each
also wrote its own undo.  Six writes and two undos for one concept, in two files.

That is the same shape the tool registry had before :mod:`agent.tools.scoped`: the
registration and the removal are separate statements, so a path that adds without
recording the removal is one edit away.  Here both live in :meth:`CommandRegistry.add`,
and a caller gets the undo back rather than being trusted to write it.

The undo is returned, not registered
------------------------------------
``add`` returns a callable and does not decide where it goes.  The two callers want
different things: a plugin's own contribution belongs to its lifecycle (so the kernel's
``ctx`` records it), while the declarative loader runs *during* a plugin's setup and
hands its undos to the same scope.  Returning the undo keeps the registry ignorant of
both and means there is still exactly one place that knows what "remove a command"
means.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.kernel.service import ScopedContribution

__all__ = ["CommandRegistry", "ScopedCommands"]


class CommandRegistry:
    """The commands a composition offers, and who offered each one.

    One storage rather than three: ``Command`` records keep the handler, the metadata
    and the owner together, so they cannot disagree about which keys exist -- which
    the parallel dicts could, and did: the owner map was written in one place and the
    handler in another.
    """

    __slots__ = ("_commands",)

    def __init__(self) -> None:
        self._commands: dict[str, dict[str, Any]] = {}

    # -- writing -------------------------------------------------------------

    def add(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        metadata: dict | None = None,
        owner: str = "",
    ) -> Callable[[], None]:
        """Register *name*, returning the undo that removes it.

        The metadata is passed in rather than computed here, because what goes in it
        differs per source: a Python contribution gets a description parsed from the
        handler's docstring, a markdown file gets the file's front matter.  Computing
        it here would mean this class knowing both conventions.

        The undo removes the record **only while it is still this one**.  A name-keyed
        undo -- what this used to return -- let the first contributor's unload remove
        the second contributor's command: A registers ``x``, B overwrites ``x``, A
        unloads, and B's command and owner record are gone.  Identity is what makes
        two contributors touching one name independent, and it is the rule the
        channel, MCP-server and agent registries already follow.
        """
        record = {
            "handler": handler,
            "metadata": dict(metadata or {}),
            "owner": owner,
        }
        self._commands[name] = record

        def forget() -> None:
            if self._commands.get(name) is record:
                self._commands.pop(name, None)

        return forget

    def remove(self, name: str) -> None:
        self._commands.pop(name, None)

    def clear(self) -> None:
        self._commands.clear()

    # -- reading -------------------------------------------------------------

    def get(self, name: str) -> Callable[..., Any] | None:
        record = self._commands.get(name)
        return record["handler"] if record is not None else None

    def has(self, name: str) -> bool:
        return name in self._commands

    def owner(self, name: str) -> str:
        record = self._commands.get(name)
        return record["owner"] if record is not None else ""

    def names(self) -> tuple[str, ...]:
        return tuple(self._commands)

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return {name: record["handler"] for name, record in self._commands.items()}

    def metadata(self) -> dict[str, dict[str, Any]]:
        return {name: dict(record["metadata"]) for name, record in self._commands.items()}

    def owners(self) -> dict[str, str]:
        return {name: record["owner"] for name, record in self._commands.items()}

    # -- snapshot, for a reload that has to roll back ------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """A copy that :meth:`restore` can put back.

        Deep enough that a later ``add`` cannot mutate the snapshot's records: a
        reload that fails has to restore the state as it was, and a shallow copy would
        restore the same dict objects the failed attempt had already written into.
        """
        return {
            name: {
                "handler": record["handler"],
                "metadata": dict(record["metadata"]),
                "owner": record["owner"],
            }
            for name, record in self._commands.items()
        }

    def restore(self, snapshot: dict[str, dict[str, Any]]) -> None:
        self._commands = {
            name: {
                "handler": record["handler"],
                "metadata": dict(record["metadata"]),
                "owner": record["owner"],
            }
            for name, record in snapshot.items()
        }

    # -- the scoped view -----------------------------------------------------

    def scoped(self, ctx: Any) -> "ScopedCommands":
        """A view bound to *ctx*'s lifetime.  Called by the kernel."""
        return ScopedCommands(self, ctx)


class ScopedCommands(ScopedContribution):
    """One plugin's handle on the command registry.

    Reached as ``ctx.<service>`` when the registry is provided under a name the plugin
    injects.  Every ``add`` records its own removal on the plugin's lifetime.
    """

    __slots__ = ()

    def add(
        self, name: str, handler: Callable[..., Any], *, metadata: dict | None = None
    ) -> str:
        """Contribute one command, removed when this plugin unloads.

        Returns the name, so a caller can log it without repeating the string.
        """
        self._contribute(
            self._registry.add(name, handler, metadata=metadata, owner=self.owner)
        )
        return name
