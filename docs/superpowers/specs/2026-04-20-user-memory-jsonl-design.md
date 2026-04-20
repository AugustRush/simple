# User Memory JSONL Design

## Goal

Keep memory palace categories as internal retrieval/storage logic, but collapse the user-visible memory surface to a single JSONL export file.

## Design

- SQLite remains the durable source of truth for long-term memory.
- Category stats are derived from SQLite queries instead of `_meta.json`.
- Category JSON snapshots and markdown entity projections are no longer maintained on the write path.
- `MemoryPalace.read_index()` now returns the content of `memory/memory.jsonl`.
- `MemoryPalace.export_jsonl()` writes a one-entry-per-line export for user inspection.

## Result

This removes redundant user-visible artifacts while preserving the internal locus model used by consolidation and retrieval.
