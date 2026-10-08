"""The agent's configuration, as one typed value rather than twelve assignments.

Why this exists
---------------
`AgentPlugin` used to write twelve attributes onto the agent it had just built:

```python
agent.max_parallel_agents = max(1, int(cfg.get("orchestration", {}).get(...)))
agent.sub_agent_timeout_seconds = max(1, int(...))
agent.llm_max_retries = max(0, int(cfg.get("llm_max_retries", ...)))
... ten more
```

and `BaseAgent` ended up with about thirty-five instance attributes, two thirds of them
configuration.  The review's phrasing is exact: the container was carrying **both services and
settings**, and the two are not the same kind of thing -- a service is something a composition
chooses and can replace, a setting is a number the operator wrote in a file.

**Settings, not services.**  Nothing here is provided by a plugin, nothing is resolved by name,
and nothing is replaceable at run time.  Keeping them out of the service namespace is what
stops `ServiceRegistry` from being two containers wearing one name.

Defaults are the `shared` constants the composition used, so a `from_config` with no config at
all produces exactly what the old inline code produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["AgentSettings"]


@dataclass(frozen=True)
class AgentSettings:
    """What the configuration says about the agent's own behaviour.

    Frozen because these are read all over the runtime and a value that can change
    underneath a turn makes the failure depend on where in the turn it was read.
    """

    #: Which skill bundle decides how a turn is orchestrated; named in full so a
    #: composition can point at a plugin's bundle (see the planner for why).
    orchestration_policy_skill_id: str = "multi-agent-orchestration"
    #: Caps concurrency, not the total.
    max_parallel_agents: int = 1
    sub_agent_timeout_seconds: int = 1
    sub_agent_retries: int = 0
    #: Bounds total sub-agents per turn.  ``0`` means "derive from max_parallel_agents".
    max_agents_per_turn: int = 0
    #: Steps (model request plus its tools) allowed in one turn.
    max_tool_call_iterations: int = 1
    max_truncation_continuations: int = 0
    llm_max_retries: int = 0
    llm_retry_base_delay: float = 0.1
    result_content_max_chars: int = 0

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "AgentSettings":
        """The settings *cfg* describes, with every clamp the old inline code applied.

        The clamps are the point of having this in one place: `max(1, ...)`,
        `bounded_int(...)` and the two-sided `min(max(...))` were three different idioms for
        "keep this inside its range", spread over forty lines, and a reader had to reconstruct
        the intent for each.
        """
        from agent import shared
        from agent.config import bounded_int

        cfg = cfg or {}
        orch = cfg.get("orchestration", {}) or {}
        defaults = cls()

        return cls(
            orchestration_policy_skill_id=str(
                orch.get("policy_skill_id", defaults.orchestration_policy_skill_id)
            ),
            max_parallel_agents=max(
                1,
                int(
                    orch.get(
                        "max_parallel_agents", shared.DEFAULT_MAX_PARALLEL_AGENTS
                    )
                ),
            ),
            sub_agent_timeout_seconds=max(
                1,
                int(
                    orch.get(
                        "sub_agent_timeout_seconds",
                        shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS,
                    )
                ),
            ),
            sub_agent_retries=max(
                0,
                int(orch.get("sub_agent_retries", shared.DEFAULT_SUB_AGENT_RETRIES)),
            ),
            max_agents_per_turn=max(0, int(orch.get("max_agents_per_turn", 0))),
            # `max_steps` is the preferred spelling; `max_tool_call_iterations` stays
            # accepted because it is a published config key and dropping it would break
            # existing configs.  Both name the same bound.
            max_tool_call_iterations=bounded_int(
                cfg.get(
                    "max_steps",
                    cfg.get("max_tool_call_iterations", shared.MAX_TOOL_CALL_ITERATIONS),
                ),
                default=shared.MAX_TOOL_CALL_ITERATIONS,
                min_value=1,
                max_value=shared.MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS,
            ),
            max_truncation_continuations=bounded_int(
                cfg.get(
                    "max_truncation_continuations",
                    shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
                ),
                default=shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
                min_value=0,
                max_value=shared.MAX_CONFIGURABLE_TRUNCATION_CONTINUATIONS,
            ),
            llm_max_retries=max(
                0, int(cfg.get("llm_max_retries", shared.DEFAULT_LLM_MAX_RETRIES))
            ),
            llm_retry_base_delay=max(
                0.1,
                float(
                    cfg.get("llm_retry_base_delay", shared.DEFAULT_LLM_RETRY_BASE_DELAY)
                ),
            ),
            result_content_max_chars=min(
                shared.MAX_RESULT_CONTENT_CHARS,
                max(
                    shared.MIN_RESULT_CONTENT_CHARS,
                    int(
                        # Under `orchestration`, not at the top level: this is where the
                        # old inline code read it, and a `from_config` that looked
                        # elsewhere would silently stop honouring a configured value.
                        orch.get(
                            "result_content_max_chars",
                            shared.DEFAULT_RESULT_CONTENT_MAX_CHARS,
                        )
                    ),
                ),
            ),
        )

    def apply_to(self, agent: Any) -> Any:
        """Copy these onto *agent*, so every existing reader keeps working.

        Copied rather than exposed as properties on the agent: the runtime reads
        ``agent.max_parallel_agents`` in a dozen places, and turning thirty-five attributes
        into properties is a change to all of them for no gain in behaviour.  What matters
        here is that the *derivation* lives in one typed place; how it reaches the readers is
        an implementation detail this can hide.
        """
        for field_name in self.__dataclass_fields__:
            setattr(agent, field_name, getattr(self, field_name))
        return agent
