# Scheduler Design

## Problem

The runtime has long-lived agent capabilities, session-scoped memory staging,
and durable task-like state for Ralph loops, but it has no general scheduler.
That means the system cannot reliably execute user-defined or system-defined
work at a future time, survive process restarts, or keep an audit trail of
scheduled executions.

## Goals

1. Add persistent scheduled-task support that survives process restarts.
2. Support two task families through one scheduler core:
   - user-facing `agent_prompt` tasks
   - internal `system_job` maintenance tasks
3. Support two delivery modes in the first version:
   - `standalone`: write run output to task history and output files
   - `channel`: deliver the result to a stable channel target
4. Keep the time model structured and explicit: `once`, `interval`, `daily`,
   `weekly`.
5. Make scheduling deterministic, testable, and recoverable.

## Non-Goals

- Supporting raw cron expressions in the first version.
- Recovering ephemeral CLI terminal sessions as delivery targets.
- Building a GUI or chat-native schedule editor in the first version.
- Distributed scheduling across multiple machines.

## First-Principles Model

The scheduler must be built from five durable primitives:

1. **Task intent** — what should run, where results go, and under what trigger.
2. **Next-run calculation** — a deterministic function from `(trigger, now)` to
   the next eligible instant.
3. **Claiming** — an atomic way to turn “due task” into “running task run”.
4. **Run history** — a durable record of scheduled time, start/end, status, and
   outputs.
5. **Delivery** — a separate concern from execution, with stable targets rather
   than in-memory sessions.

Anything else in the design must serve one of those primitives.

## Architecture

Introduce a new `agent.scheduler` subsystem with four focused units:

- `agent.scheduler.models`
  - task, run, trigger, and delivery dataclasses/enums
- `agent.scheduler.store`
  - SQLite-backed persistence for tasks and task runs
- `agent.scheduler.runtime`
  - trigger calculation, due-task claiming, stale-run recovery, dispatch loop
- `agent.scheduler.delivery`
  - delivery adapters for `standalone` and `feishu_chat`

The scheduler is a separate long-lived process started through `simple scheduler`.
Interactive CLI and gateway flows remain clients of the scheduling subsystem,
not the scheduling truth source.

## Trigger Model

The first version uses a structured trigger payload instead of cron text.

Supported trigger types:

- `once`
  - fields: `at`, `timezone`
- `interval`
  - fields: `every`, `unit`, `anchor_at`, `timezone`
- `daily`
  - fields: `time_of_day`, `timezone`
- `weekly`
  - fields: `day_of_week`, `time_of_day`, `timezone`

Each trigger implements the same contract:

- `next_after(now_utc) -> datetime | None`
- `advance_from(scheduled_for_utc, now_utc) -> datetime | None`

`advance_from` is responsible for “coalesce once” behavior after downtime:
if multiple windows were missed, the scheduler runs one catch-up execution and
then advances to the first trigger time after `now`.

## Persistent Data Model

Store scheduler state in a dedicated SQLite database under `~/.agent/tasks/`.

### Task

- `id`
- `name`
- `kind` — `agent_prompt | system_job`
- `enabled`
- `trigger_type`
- `trigger_payload`
- `timezone`
- `delivery_mode` — `standalone | channel`
- `delivery_target`
- `payload`
- `model_override`
- `overlap_policy` — first version: `forbid_overlap`
- `missed_run_policy` — first version: `coalesce`
- `next_run_at`
- `lease_until`
- `active_run_id`
- `last_run_at`
- `last_success_at`
- `created_at`
- `updated_at`

### TaskRun

- `id`
- `task_id`
- `scheduled_for`
- `started_at`
- `finished_at`
- `status` — `running | succeeded | failed | interrupted`
- `summary`
- `error`
- `output_path`
- `delivery_status`
- `created_at`
- `updated_at`

## Claiming and Recovery

### Claiming

When a task becomes due, the scheduler performs one SQLite transaction:

1. Select a due task whose lease is free or expired.
2. Create a `TaskRun(status=running, scheduled_for=task.next_run_at)`.
3. Advance `task.next_run_at` to the next logical trigger time.
4. Set `task.lease_until` and `task.active_run_id`.

Advancing on claim prevents duplicate execution while the scheduler is healthy.

### Recovery

On startup and periodically, the scheduler recovers stale runs:

1. Find tasks with `lease_until < now` and `active_run_id` still set.
2. Mark the associated run as `interrupted`.
3. Clear the task lease and active run.
4. Roll back `task.next_run_at` to the interrupted run’s `scheduled_for`.

That makes missed claimed runs eligible for replay after crashes without losing
the original scheduled instant.

## Execution Model

### `agent_prompt`

Runs a fresh `AgentContext` against a durable payload:

- `prompt`
- optional `model_override`
- optional `system_suffix`

The run must not depend on any live interactive session memory. It may use the
standard runtime components and tools, but the task definition itself must be
self-sufficient.

### `system_job`

Runs a named internal handler with structured parameters. The first version
should include at least:

- `memory_tidy`

This allows the scheduler to support user-visible jobs and maintenance jobs
through the same claiming and history pipeline.

## Delivery Model

### Standalone

- Persist run summary and output path
- Write generated artifacts under a per-run output directory
- Do not require any live channel process

### Channel

The first version supports stable Feishu chat delivery:

- target type: `feishu_chat`
- fields: `chat_id`, `chat_type`

Delivery is intentionally based on stable identifiers, not in-memory
conversation contexts, so it remains valid across process restarts.

## CLI Surface

Add a new `schedule` command group:

- `simple schedule once`
- `simple schedule interval`
- `simple schedule daily`
- `simple schedule weekly`
- `simple schedule list`
- `simple schedule show <id>`
- `simple schedule pause <id>`
- `simple schedule resume <id>`
- `simple schedule delete <id>`
- `simple schedule run-now <id>`

Add a long-running service command:

- `simple scheduler`

## Testing Strategy

The first version must have focused tests for:

1. trigger calculation across time zones
2. due-task claiming and stale-run recovery
3. overlap prevention
4. catch-up/coalesce behavior after downtime
5. standalone run persistence
6. Feishu channel delivery without a live session
7. CLI schedule creation and listing

## Risks

1. Time-zone semantics can become inconsistent if trigger logic mixes local and
   UTC arithmetic.
2. Delivery logic can accidentally depend on live channel/session state if not
   forced through durable targets.
3. Scheduler loops that reuse agent components must avoid leaking mutable
   per-run state such as `AgentContext.messages`.

## Decision

Build a dedicated persistent scheduler subsystem with structured triggers,
durable run history, restart recovery, and separate delivery adapters. Start
with CLI-first task management, support `standalone` and `feishu_chat`
delivery, and keep cron support as a future input-layer extension rather than
the system’s truth source.
