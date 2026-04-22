---
name: Multi-Agent Orchestration
description: Decide when to answer directly, when to run parallel subtasks, when to use a dependency pipeline, and when to do a lead-controlled rendezvous.
user-invocable: false
disable-model-invocation: false
---

# Multi-Agent Orchestration

Use this skill when a task might benefit from structured multi-agent coordination.

## Core Rule

`spawn_agent` is the only public delegation primitive.
Do not invent or rely on new public tools such as `team_run`, `review_team`, or similar wrappers.

## Choose One Mode

### direct

Use direct execution when:
- The task is simple
- The task is single-domain
- The user just wants a straightforward answer

### parallel

Use parallel subtasks when:
- Subtasks are independent
- Multiple perspectives materially improve the outcome
- The tasks do not share the same write scope

### pipeline

Use a dependency pipeline when:
- One subtask depends on another subtask's result
- The work naturally decomposes into ordered stages

Only pass the necessary summary from one stage to the next.
Do not pass full raw histories unless strictly required.

### rendezvous

Use rendezvous when:
- A first independent round is useful
- You need a second pass on disagreements or competing conclusions
- You want controlled coordination without free-form agent-to-agent chat

Rendezvous means:
1. Run one independent round
2. Summarize the important differences as the lead
3. Run one follow-up round using the lead summary
4. Synthesize the final answer

Default to at most 2 rounds.

## Do Not Over-Orchestrate

Do not use multi-agent orchestration when:
- The user asked a direct question
- The subtasks are too small to justify overhead
- Multiple agents would edit the same files without an explicit write boundary
- The task can be solved correctly by a single agent without loss of quality

## Lead Responsibilities

When you orchestrate:
- define clear roles
- define clear task boundaries
- keep subtasks small and independent
- summarize results yourself
- merge duplicates
- surface disagreements explicitly
- produce the final answer yourself
