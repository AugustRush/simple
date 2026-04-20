# SQLite Staging Checkpoint Design

## Goal

Move the default conversation staging path from per-session JSONL files into `palace.db`, while preserving the existing `StagingBuffer` API and legacy JSONL compatibility.

## Scope

This phase only changes staging persistence. It does not remove memory projections, rewrite long-term memory storage, or delete orphan JSONL recovery.

## Design

`StagingBuffer(context_dir=..., session_id=...)` now uses a `staging_turns` SQLite table in `<context_dir>/palace.db`.

`StagingBuffer(path=...)` keeps the old JSONL backend. This preserves tests, explicit file-based callers, and orphan recovery for staging files left by older versions.

`ContextManager.enqueue_staging_job()` records staging backend metadata so background workers can reconstruct the right buffer type. SQLite-backed staging jobs include `context_dir` and `session_id`; JSONL-backed jobs continue to include `staging_path`.

## Validation

- Default staging creates `palace.db` and no JSONL file.
- Default staging survives process-style reconstruction by `session_id`.
- Background consolidation can reconstruct and drain a non-primary SQLite-backed staging buffer.
- Existing JSONL-specific tests continue to pass.
