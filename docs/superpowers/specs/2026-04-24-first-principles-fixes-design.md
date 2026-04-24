# First Principles Fixes Design

## Goal

Fix the confirmed correctness and safety issues from review with minimal surface area, while leaving large-scale refactors out of scope.

## Design

- Treat SQLite as the source of truth for memory state and make JSONL export an explicit or on-demand projection, not a side effect of every write.
- Fix the `evolve` CLI command by using the same qualified component-shutdown path as the rest of the CLI, so cleanup is correct regardless of branch taken.
- Narrow legacy cleanup to files that this system can positively identify as old generated artifacts instead of deleting by broad extension patterns.
- Make skill updates preserve existing instructions unless the caller provides a non-empty replacement or explicitly opts into clearing them.
- Add a minimal schema-version migration entrypoint to `SchedulerStore` so future schema evolution has a stable mechanism.
- Apply only no-risk cleanup alongside these fixes, such as removing duplicate constants and dead one-line assignments.

## Non-Goals

- No splitting of large files or architectural rewrites.
- No orchestration-planner redesign.
- No broad flaky-test cleanup sweep outside tests touched by these fixes.

## Acceptance Criteria

- `evolve` cleanup path no longer raises `NameError`.
- Memory writes no longer force a full JSONL rewrite on every call.
- Reading/exporting the memory index still produces a valid `memory.jsonl`.
- Legacy cleanup no longer deletes arbitrary `.json` or `.md` files under agent-managed directories.
- `update_skill` no longer wipes instructions when passed an empty string unintentionally.
- `SchedulerStore` records and upgrades schema version through a dedicated migration path.
