"""Long-term memory as a plugin, so the composition does not assemble it above the catalog.

What was there
--------------
``bootstrap`` built the ``LTMStore``, seeded it from the config's ``assistant_identity``,
built a ``MemoryPalace`` around it, and handed the same store to the ``ContextManager``
and to the web channel's session-store factory.  All of it happened *above* the plugin
catalog, which is what made ``memory`` impossible to provide from a plugin: by the time
the catalog could mount one, its consumers had already been handed the object.

Three consumers, one store
--------------------------
The order is what mattered, not the count.  ``ContextManager`` and the session-store
factory both take the same ``LTMStore`` the palace does, so the store is provided as
``ltm_store`` for them to resolve, and the palace as ``memory``.  Two services rather than
one because they are two things: a store that persists facts and a palace that reads
memory chapters.

The store's lifetime is the plugin's
------------------------------------
``ctx.on_unwind(store.close)``: this plugin opened it.  That is the same rule the scheduler
plugin follows, and the same one an earlier attempt got wrong by asking the *service* to
close a store it had merely been given.

Seeding is a composition writing down what the config declares
--------------------------------------------------------------
The two ``FactAssertion``s come from ``assistant_identity`` -- a name and a role the
operator put in ``config.json``.  That is not memory internals; it is the composition
recording a declaration, which is why it belongs here rather than in the store's own
module.
"""

from __future__ import annotations

from typing import Any

from agent.memory.palace import MemoryPalace
from agent.memory.store import FactAssertion, LTMStore

__all__ = ["MemoryPlugin"]


class MemoryPlugin:
    """Provides ``ltm_store`` and ``memory``."""

    name = "memory"

    #: The config, for the storage and consolidation sections, and the assistant identity
    #: that seeds the store.  The paths come from ``shared`` rather than from config --
    #: they are process state a session name rewrites, and reading them at ``setup`` is
    #: what gets the value in force rather than the one at import.
    inject = ("cfg",)

    def setup(self, ctx: Any) -> None:
        from agent import shared

        cfg = ctx.get("cfg", None) or {}
        ctx_cfg = cfg.get("context", {}) or {}
        storage_cfg = ctx_cfg.get("storage", ctx_cfg)
        cons_cfg = ctx_cfg.get("consolidation", ctx_cfg)
        mem_cfg = cfg.get("memory", {}) or {}

        store = LTMStore(
            context_dir=shared.CONTEXT_DIR,
            max_categories=storage_cfg.get("max_categories", shared.MAX_CATEGORIES),
            memory_dir=shared.MEMORY_DIR,
        )
        # Opened here, closed here.  A store closed by one path and not another is the
        # leak this arrangement exists to prevent.
        ctx.on_unwind(store.close)

        self._seed_identity(store, cfg)

        palace = MemoryPalace(
            tidy_interval=mem_cfg.get(
                "tidy_interval_seconds", shared.MEMORY_TIDY_INTERVAL
            ),
            tidy_threshold=mem_cfg.get(
                "tidy_file_threshold", shared.MEMORY_TIDY_FILE_THRESHOLD
            ),
            base_dir=shared.MEMORY_DIR,
            context_dir=shared.CONTEXT_DIR,
            store=store,
        )

        ctx.provide("ltm_store", store)
        ctx.provide("memory", palace)

    @staticmethod
    def _seed_identity(store: LTMStore, cfg: dict) -> None:
        """Record the operator's declared name and role as facts.

        Keyed deterministically (``bootstrap-assistant-name-<name>``) so a restart
        overwrites rather than accumulating duplicates -- the id carries the value, which
        is what makes it idempotent.
        """
        identity = cfg.get("assistant_identity", {}) or {}
        for field, predicate in (("name", "name"), ("role", "role")):
            value = str(identity.get(field, "") or "").strip()
            if not value:
                continue
            store.add_fact_assertion(
                FactAssertion(
                    id=f"bootstrap-assistant-{predicate}-{_key(value)}",
                    subject="assistant",
                    predicate=predicate,
                    value=value,
                    source_kind="bootstrap",
                    source_id=f"config.assistant_identity.{predicate}",
                )
            )


def _key(value: str) -> str:
    """The value as an id fragment: lower-cased, and spaces become underscores.

    Two different spellings of one role must not become two facts, and the original
    spelling is kept in ``value``.
    """
    return value.lower().replace(" ", "_")
