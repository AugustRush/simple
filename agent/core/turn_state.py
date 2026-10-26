"""The turn's own state: what a turn carries in, and what it leaves behind.

Moved out of ``agent/core/agent.py`` (see the remaining-big-files survey).  These are the
classes a turn passes around -- the context handed to a model call, the result handed back,
the per-task local stack, the recovery bookkeeping, the loop's own counters.  They were
already top-level; the 2366-line class they sat above is what this file is about.

``DEFAULT_SYSTEM_PROMPT`` moved here from ``agent/__init__.py``, because ``AgentContext``'s
field default reads it and the module that owns the dataclass should own its default.  That is
also the other half of a decision recorded in ``agent/core/turn_loop.py``: it kept
``AgentContext`` at a function-local import "because the field default reads
``DEFAULT_SYSTEM_PROMPT``", and judged moving the prompt "a larger change than the cycle it
would remove".  With the prompt here, that cycle is gone and ``turn_loop`` can import this
module at module scope.

Nothing here imports ``agent/core/agent.py``: the dependency runs one way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from agent import shared

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AgentContext",
    "AgentResult",
    "SubAgentProgressEvent",
    "_TaskLocalContextStack",
    "_ContentFilterRecovery",
    "_TurnLoopState",
]


DEFAULT_SYSTEM_PROMPT = """You are a powerful personal AI agent with tools, memory, and the ability to spawn sub-agents.

## Tools
Your exact tool capabilities are appended later in this prompt. Use only the tools explicitly listed for this agent instance.

## Growing a capability

The tool list appended below is authoritative; use only what it lists. When it lacks
something a task keeps needing — a conversion you have now done twice by hand, a data
source, an output shape you keep reconstructing — you may build it:

- `create_tool` writes a small Python module into the user's tools directory. It is
  probe-imported in a separate process before it can be registered, and it is
  **callable from your next turn, in this same conversation**. Unless the deployment
  trusts that directory wholesale it needs the user's confirmation first: show them the
  code, ask, and never assume agreement.
- `create_skill` records a procedure as a skill — instructions to be followed later. It
  is live in the next turn, and it becomes part of your own instructions, so write it as
  something you would still agree with after reading it again.

Two rules:

1. **A tool earns its place by recurring.** For a one-off, use the shell, the file tools
   or the web tools. A directory of one-use tools is a directory nobody reads.
2. **Never report a capability you did not run.** After creating one, call it once and
   report what came back. "I built a tool for that" is not the same sentence as "it
   worked".

Never create a tool or a skill whose purpose is to widen your own permissions, read
credentials, or act on another person's machine.

## spawn_agent — multi-agent orchestration

Use `spawn_agent` when the task benefits from specialised sub-agents. Two core patterns:
Prefer lead-controlled coordination over free-form sub-agent debate.

### Pattern 1 — Parallel (independent subtasks)
Call `spawn_agent` **multiple times in ONE turn** when subtasks are fully independent.
They run concurrently; you synthesise the results afterward.
Example: "summarise these 3 articles" → spawn 3 summarisers simultaneously.

### Pattern 2 — Pipeline (dependent subtasks)
When role B needs role A's output, spawn **both in ONE turn**: give A a stable `id`,
and give B `depends_on: ["<A's id>"]`. The runtime runs stages in dependency order
and passes only bounded summaries downstream — never full raw histories.

### Pattern 3 — Rendezvous (bounded multi-round)
For important disagreements, spawn the participants in ONE turn with
`coordination_mode: "rendezvous"`. The runtime runs a bounded number of rounds
(default 2), synthesises the disagreements between rounds, and can narrow the
follow-up or stop early.

The key rule: **if role B needs role A's output, express it with `depends_on` — the
runtime will sequence it; never invent results on behalf of another agent.**

### When NOT to use spawn_agent
Answer directly for simple questions, single-domain tasks, and conversational follow-ups.
Default to direct — don't over-orchestrate.

## Memory
Save important facts, decisions, and learnings to memory so they persist across sessions.
"""


class _TurnLoopState:
    """One turn's step-loop state.

    A plain object the caller owns and passes in, rather than attributes on
    ``self``: ``send_message`` may run several turns concurrently on one agent
    instance, and ``self`` is shared between them.  Storing this state on the
    instance let one turn read another's ``trace_status`` and
    ``pending_tool_response``, which a concurrent-session test caught.
    """

    __slots__ = (
        "pending_tool_response",
        "result_text",
        "trace_status",
        "trace_error",
        "steps",
    )

    def __init__(self) -> None:
        self.pending_tool_response: Any = None
        self.result_text = ""
        self.trace_status = "ok"
        self.trace_error: Optional[str] = None
        self.steps = 0


@dataclass
class AgentContext:
    """State for a single agent instance."""

    agent_id: str = field(default_factory=shared._new_id)
    role: str = "assistant"
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tools_enabled: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_id: str
    content: str
    tool_calls_made: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class SubAgentProgressEvent:
    kind: str
    role: Optional[str] = None
    task: Optional[str] = None
    message: str = ""
    completed: int = 0
    total: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _TaskLocalContextStack:
    """Deprecated: kept only as a no-op shim for any third-party caller.

    The runtime no longer maintains a context stack — ``_active_agent_context``
    (a ContextVar) is the single source of truth for the current
    AgentContext.  Sub-agents run in their own BaseAgent instance with
    their own ContextVar, so a per-agent stack was always degenerate
    (at most one item).
    """

    def __bool__(self) -> bool:
        return False


@dataclass
class _ContentFilterRecovery:
    """The rollback state a provider content-filter block needs.

    A provider can reject a request because of what a *tool result* contained.
    Recovering means un-sending the last protocol unit and resending it with
    the offending output summarized.  That requires remembering what was last
    submitted, and carrying the recovered response into the next loop
    iteration so it is consumed instead of issuing a fresh LLM call.

    That is three pieces of state with one lifecycle.  They used to be three
    loose locals threaded through a 500-line function, cleared at four
    separate sites — where forgetting one clears the rollback point and turns
    a recoverable block into a failed turn.  Naming the state machine makes
    the invariant ("submission recorded ⇒ rollback possible") checkable.
    """

    submitted_tool_uses: list[dict] | None = None
    submitted_results: list[str] | None = None
    pending_response: Any | None = None

    def record_submission(
        self, tool_uses: list[dict], results: list[str]
    ) -> None:
        """Note what just went to the provider, so it can be rolled back."""
        self.submitted_tool_uses = list(tool_uses)
        self.submitted_results = list(results)

    def forget_submission(self) -> None:
        """Drop the rollback point — nothing recoverable is outstanding."""
        self.submitted_tool_uses = None
        self.submitted_results = None

    def take_pending_response(self) -> Any | None:
        """Consume a recovered response exactly once."""
        response, self.pending_response = self.pending_response, None
        return response
