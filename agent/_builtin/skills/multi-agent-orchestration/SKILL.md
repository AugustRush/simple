---
name: Multi-Agent Orchestration
description: Decide orchestration mode
user-invocable: false
disable-model-invocation: true
planner-policy: orchestration
default-mode: direct
parallel-keywords: ["分别", "各自", "parallel", "multiple perspectives"]
pipeline-leading-keywords: ["先", "first"]
pipeline-followup-keywords: ["再", "然后", "then"]
pipeline-keywords: ["分阶段", "step by step"]
rendezvous-keywords: ["辩论", "debate", "正反", "多轮"]
max-rendezvous-rounds: 2
---

# Multi-Agent Orchestration

Policy for coordinating specialised sub-agents through the `spawn_agent`
primitive. This skill is planner-only: it is never activated by the model and
does not add tools.

## When to orchestrate

- **parallel**: independent subtasks that each produce a bounded deliverable.
- **pipeline**: a subtask must consume an upstream subtask's output; encode it
  with `id` + `depends_on` so the runtime sequences the stages.
- **rendezvous**: one bounded round of independent analysis followed by a lead
  synthesis and at most one follow-up round on key disagreements
  (`coordination_mode: "rendezvous"`).

## When NOT to orchestrate

- Simple questions and single-domain tasks: answer directly.
- Any orchestration that can be degraded to a single agent run without loss.
- Free-form agent-to-agent chat: keep coordination lead-controlled and bounded.

## Safety rules

1. Subtasks must be independent, clearly scoped, and converge to a bounded output.
2. Concurrent subtasks with write intent must declare disjoint `write_scope`;
   `implementation` subtasks always require an explicit `write_scope`.
3. Rendezvous is bounded (default max 2 rounds).
4. Wording cues are advisory; the explicit `depends_on` / `coordination_mode`
   graph is authoritative.
