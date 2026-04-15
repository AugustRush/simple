# Multi-Agent Progress Routing Design

## Problem

In Feishu sessions, multi-agent work triggered by `spawn_agent` currently reports progress with direct `CONSOLE.print(...)` calls inside `agent.py`. That means sub-agent status is bound to the CLI transport instead of the active conversation sink.

This creates two UX failures:

1. Feishu users do not see sub-agent progress while work is running.
2. Tool hints and sub-agent status fragment into CLI-only output instead of a single process-oriented Feishu surface.

## First-Principles Rule

Agents should produce semantic events, not transport-specific UI.

- The agent core decides **what happened**.
- The active `OutputSink` decides **how to show it**.

`spawn_agent` should therefore emit structured progress events upward, and the current sink should render them for CLI, Feishu, or any future channel.

## Design

### 1. Add Structured Progress Events

Define a `SubAgentProgressEvent` dataclass in `agent.py` with fields such as:

- `kind`: `batch_started | batch_progress | agent_started | agent_finished | agent_failed`
- `role`
- `task`
- `message`
- `completed`
- `total`

Add `OutputSink.on_subagent_event(event)` as a new no-op interface method.

### 2. Route Events Through the Active Sink

Add a small helper on `BaseAgent`, e.g. `_emit_subagent_event(event)`, which:

- reads `_active_sink`
- calls `sink.on_subagent_event(event)` when a sink exists
- falls back to CLI-only rendering only when no sink is active

This keeps the core independent of Feishu specifics while still preserving local CLI usability.

### 3. Replace Direct CLI Printing in `spawn_agent`

Update `register_spawn_capability()` and `_run_tool_uses()` so spawn orchestration emits events instead of `CONSOLE.print(...)`.

Behavior:

- when a spawn batch starts, emit `batch_started`
- when each sub-agent acquires a worker slot, emit `agent_started`
- when each sub-agent succeeds, emit `agent_finished`
- when each sub-agent fails or times out, emit `agent_failed`
- while the batch is still running, emit periodic `batch_progress` heartbeats such as `1/3 completed`

This provides at least one visible progress update even when no sub-agent finishes quickly.

### 4. CLI Rendering

`CliOutputSink.on_subagent_event()` renders compact status lines equivalent to the old console output.

This preserves local ergonomics without hardcoding CLI rendering inside `spawn_agent`.

### 5. Feishu Rendering

`FeishuOutputSink` gains a second streaming buffer dedicated to process progress.

Rules:

- sub-agent events append to a **process streaming card**
- tool hints emitted while the process card is active are appended into that same card
- the normal assistant answer continues to use the existing response card/path
- when the turn completes, finalize the process card first, then send the final answer as its own card/message

This yields:

1. One live-updating “process” card for multi-agent progress
2. One separate final answer card/message for the parent agent conclusion

### 6. Edge Cases

- If no sink is active, fallback console rendering still works
- If Feishu CardKit fails, process events fall back to a plain text/status send path
- If the batch has only one sub-agent, the same event model still applies
- If tools run during a progress-active phase, their hints are merged into the process card instead of creating separate messages

## Files

- `agent.py`
  - add `SubAgentProgressEvent`
  - add `OutputSink.on_subagent_event`
  - emit structured events from spawn orchestration
- `channels/feishu.py`
  - add process-card state and rendering
  - merge tool hints into process card while active
- `tests/test_agent_integration.py`
  - verify spawn events are routed to sink
  - verify heartbeat/progress events fire for multi-agent batches
- `tests/test_feishu_channel.py`
  - verify sub-agent events schedule process-card updates
  - verify final turn closes process card separately from final answer
- `tests/test_channel_layer.py`
  - verify output sink interface tolerates new event method
