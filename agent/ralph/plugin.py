"""Ralph as a plugin, rather than something the composition root builds unconditionally.

What was there
--------------
``bootstrap`` built a ``RalphService`` inline and put it in the component mapping.  The
service was already *named* -- ``commands/builtin.py`` resolves ``ralph_service`` -- so
the read side was already a service lookup.  What was missing is that the composition
could not choose: every process got a Ralph, its task store, its verifier and its
context factory, whether or not anybody ran ``/ralph``.

As a plugin, a profile that does not mount it has no ``ralph_service``, and ``/ralph``
reports a missing service instead of half-working.  That is the plan's point about
execution pieces being plugins: not that the code moves, but that whether it is present
becomes a decision.

Why the dependency list is long, and why that is honest
-------------------------------------------------------
Seven names, where most plugins declare one or two.  Ralph *is* a composition of the
others -- it drives turns through the agent, verifies against the filesystem, and
builds its iteration context from the system prompt and the skill catalog -- so a short
list would mean it was reaching for those behind the context's back.  Declaring them
means an unmet one leaves Ralph **pending**, which is the right state: a composition
with no workspace cannot verify anything, and a Ralph that activated anyway would fail
at the first check.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.ralph import (
    RalphIterationResult,
    RalphService,
    RalphVerifier,
    open_task_store,
)

__all__ = ["RalphPlugin"]


class RalphPlugin:
    """Provides ``ralph_service``.

    ``name`` is the plugin's identity; it is also what a profile lists to include
    Ralph and what a caller would name to unload it.
    """

    name = "ralph"

    #: Everything Ralph needs to exist.  See the module docstring for why it is long.
    inject = (
        "agent",
        "system_prompt",
        "skill_catalog",
        "context_manager",
        "workspace_root",
        "output_dir",
        "cfg",
    )

    def setup(self, ctx: Any) -> None:
        agent = ctx.get("agent")
        system_prompt = ctx.get("system_prompt")
        skill_catalog = ctx.get("skill_catalog")
        cfg = ctx.get("cfg") or {}

        async def turn_executor(
            iter_ctx: Any,
            prompt: str,
            *,
            cancel_token: Any = None,
            model_override: str | None = None,
        ) -> RalphIterationResult:
            """Run one Ralph iteration through the agent.

            Read from the context at call time rather than captured at setup: a
            composition that replaces its agent should have Ralph drive the new one,
            and a captured reference would keep driving the old.
            """
            result = await ctx.get("agent").send_message(iter_ctx, prompt)
            return RalphIterationResult(
                iteration=1,
                summary=result.content or "",
                tool_calls=tuple(result.tool_calls_made or ()),
                error=result.error,
            )

        def context_factory() -> Any:
            """The context a Ralph iteration runs in."""
            from agent.core.agent import AgentContext

            iter_ctx = AgentContext(system_prompt=system_prompt)
            iter_ctx.metadata["skill_catalog"] = skill_catalog
            return iter_ctx

        # Imported here, not at module scope: agent/__init__ imports this package.
        # The task store's location is process state (a test may point it elsewhere),
        # so it is read at setup rather than captured in a module constant.
        import agent as agent_module

        service = RalphService(
            turn_executor=turn_executor,
            store=open_task_store(agent_module.TASKS_DIR),
            verifier=RalphVerifier(
                workspace_root=ctx.get("workspace_root"),
                output_dir=ctx.get("output_dir"),
                blocked_commands=tuple(cfg.get("shell_blocked_commands", ())),
            ),
            context_factory=context_factory,
            context_manager=ctx.get("context_manager"),
        )
        ctx.provide("ralph_service", service, replace=True)
        # `agent` is read through the context on every iteration, so it is looked up
        # again there; the local is kept only to fail early if it is missing.
        del agent
