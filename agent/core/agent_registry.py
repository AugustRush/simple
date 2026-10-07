"""Sub-agent roles a plugin declares, as a service rather than a catalog dict.

This is the fourth instance of one shape -- after tools, commands and the plugin
catalog's own tree -- and it is the one where the shape earns its keep for a second
reason.  Tools and commands are *contributed* and then read by the agent; this is
**looked up at runtime**: ``subagent_prepare`` asks "what is the body for the role
named ``plugin:github:reviewer``" while a turn is being assembled.  A registry reached
as a service is therefore replaceable in the sense the plan means -- a composition can
substitute where roles come from -- rather than only enumerable.

The key is derived, like a tool's source
---------------------------------------
A role is stored under ``plugin:<plugin>:<agent>``.  Callers pass the two parts and the
key is built here, so the naming convention has one home; the previous form had the
loader and the Python contribution each assembling it, and the runtime consumer parsing
it back.

What is deliberately not here: any notion of what a role *means*.  It is a name, a
description and a body of markdown.  How a body is prepended to a system prompt, and
whether a role may override another, belong to whoever assembles the spawn -- which is
the point of keeping the registry this thin.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AgentRegistry", "ScopedAgentDefs"]


def role_key(plugin: str, agent: str) -> str:
    """The key a declared role is stored under.

    Exported because the runtime consumer has to recognise a role it was handed --
    ``subagent_prepare`` checks ``role.startswith("plugin:")`` -- and a format that two
    modules agree on should be written down in one of them.
    """
    return f"plugin:{plugin}:{agent}"


class AgentRegistry:
    """The sub-agent roles a composition offers, by key."""

    __slots__ = ("_defs",)

    def __init__(self) -> None:
        self._defs: dict[str, dict[str, Any]] = {}

    # -- writing -------------------------------------------------------------

    def add(self, key: str, definition: dict) -> "Any":
        """Register *key*, returning the undo that removes it."""
        self._defs[key] = dict(definition)

        def forget() -> None:
            self._defs.pop(key, None)

        return forget

    def remove(self, key: str) -> None:
        self._defs.pop(key, None)

    def clear(self) -> None:
        self._defs.clear()

    # -- reading -------------------------------------------------------------

    def get(self, key: str) -> dict | None:
        record = self._defs.get(key)
        return dict(record) if record is not None else None

    def all(self) -> list[dict]:
        """Every role, each carrying its own key -- the shape ``/agents`` lists."""
        return [dict(value, key=key) for key, value in self._defs.items()]

    def keys(self) -> tuple[str, ...]:
        return tuple(self._defs)

    # -- snapshot, for a reload that has to roll back ------------------------

    def snapshot(self) -> dict[str, dict]:
        return {key: dict(value) for key, value in self._defs.items()}

    def restore(self, snapshot: dict[str, dict]) -> None:
        self._defs = {key: dict(value) for key, value in snapshot.items()}

    # -- the scoped view -----------------------------------------------------

    def scoped(self, ctx: Any) -> "ScopedAgentDefs":
        return ScopedAgentDefs(self, ctx)


class ScopedAgentDefs:
    """One plugin's handle on the role registry."""

    __slots__ = ("_registry", "_ctx")

    def __init__(self, registry: AgentRegistry, ctx: Any) -> None:
        self._registry = registry
        self._ctx = ctx

    @property
    def owner(self) -> str:
        return str(getattr(self._ctx, "name", "") or "")

    def add(self, name: str, definition: dict) -> str:
        """Contribute one role, removed when this plugin unloads.

        The definition is stamped with its plugin, because the key alone is not enough
        to answer "whose role is this" without parsing -- and parsing a key to recover
        a fact you had when you wrote it is how the two come to disagree.
        """
        key = role_key(self.owner, name)
        undo = self._registry.add(key, {**definition, "plugin": self.owner})
        self._ctx.on_unwind(undo)
        return key
