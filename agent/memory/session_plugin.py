"""The session log as a plugin, so the agent can be wired before it is provided.

Why this is a plugin rather than a line in the composition root
--------------------------------------------------------------
``ContextManager`` is Phase 2's Session *service* -- that part was done long ago.  What
was not is where it is **built**: ``bootstrap`` assembled it above the catalog, which is
why ``agent.context_manager`` had to be assigned *after* the mount and could not be
assigned by the plugin that owns the agent.

Making it a plugin removes that ordering: the composition now resolves
``context_manager`` like any other service, and `AgentPlugin` can have it in hand when it
builds the agent.

What it takes, and what it does not
-----------------------------------
``ltm_store`` for the store, ``workspace_root`` for the memory scope, and ``cfg`` for the
consolidation settings.  Everything else -- the retriever, the idle window, the staging
thresholds, the token estimator -- is a default plus a config key, so nothing here reaches
for session or process state the composition cannot supply.
"""

from __future__ import annotations

from typing import Any

from agent.memory.context import ContextManager
from agent.memory.system import ConsolidationEngine, LocalRetriever
from agent.pathing import project_memory_scope

__all__ = ["SessionPlugin"]


class SessionPlugin:
    """Provides ``context_manager``."""

    name = "session"

    #: The store it reads and writes, the workspace whose memory scope it is scoped to,
    #: and the config holding the consolidation settings.
    inject = ("ltm_store", "workspace_root", "cfg")

    def setup(self, ctx: Any) -> None:
        from agent import shared

        cfg = ctx.get("cfg", None) or {}
        ctx_cfg = cfg.get("context", {}) or {}
        storage_cfg = ctx_cfg.get("storage", ctx_cfg)
        cons_cfg = ctx_cfg.get("consolidation", ctx_cfg)
        store = ctx.get("ltm_store")

        manager = ContextManager(
            store=store,
            retriever=LocalRetriever(),
            consolidation=ConsolidationEngine(
                store=store,
                max_categories=storage_cfg.get("max_categories", shared.MAX_CATEGORIES),
                decay_factor=storage_cfg.get("decay_factor", shared.DECAY_FACTOR),
                sleep_token_ratio=cons_cfg.get(
                    "token_ratio", shared.SLEEP_TOKEN_RATIO
                ),
                keep_last_messages=cons_cfg.get("keep_last_messages", 6),
                max_source_tokens=cons_cfg.get(
                    "max_source_tokens", shared.CONSOLIDATION_MAX_SOURCE_TOKENS
                ),
                max_chunks_per_run=cons_cfg.get("max_chunks_per_run", 2),
                output_tokens=cons_cfg.get("output_tokens", 512),
                chars_per_token=cons_cfg.get("token_estimation", {}).get(
                    "chars_per_token", float(shared.CHARS_PER_TOKEN)
                ),
                cjk_chars_per_token=cons_cfg.get("token_estimation", {}).get(
                    "cjk_chars_per_token", 1.0
                ),
            ),
            idle_seconds=cons_cfg.get("idle_seconds", 300),
            min_messages=cons_cfg.get("min_messages", 4),
            staging_turn_threshold=cons_cfg.get(
                "staging_turn_threshold", shared.STAGING_TURN_THRESHOLD
            ),
            staging_token_threshold=cons_cfg.get(
                "staging_token_threshold", shared.STAGING_TOKEN_THRESHOLD
            ),
            route_keywords=ctx_cfg.get("route_keywords"),
            project_scope=project_memory_scope(ctx.get("workspace_root")),
        )
        ctx.provide("context_manager", manager)
