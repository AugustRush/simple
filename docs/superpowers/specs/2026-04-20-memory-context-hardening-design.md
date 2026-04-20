# Memory Context Hardening Design

## Goal

Fix three correctness gaps in the memory/context pipeline:

1. consolidation failures must remain retryable
2. multiple open tasks for the same entity must coexist
3. recent unconsolidated context must remain available after working-memory compaction

## Scope

This design intentionally avoids the larger staging-to-SQLite rewrite. It hardens the current architecture with minimal surface-area changes in `agent/memory/system.py` and targeted test updates.

## Proposed Changes

### 1. Explicit consolidation success semantics

`ConsolidationEngine.consolidate()` should report whether extraction/persistence succeeded. Callers must only clear staging state and dirty flags after a confirmed success. Logged failures without state preservation are not acceptable because they silently drop retry intent.

### 2. Task storage as a set, not a singleton

`tasks` entries should no longer deduplicate solely on `(category, entity, memory_type)`. Distinct task contents for the same entity must be stored independently, while status updates for the same logical task should still upsert deterministically.

### 3. Recent unconsolidated context survives compaction

Implicit retrieval should include a bounded summary of recent staged turns when the live message window has been compacted or when explicit episodic recall is requested. This closes the gap between front-end compaction and background consolidation.

### 4. Retrieval routing becomes soft bias, not hard filter

Category routing should influence ranking rather than act as the primary exclusion gate. Retrieval should search broadly first, then prefer routed categories when multiple candidates are relevant.

## Non-Goals

- replace staging JSONL with SQLite
- remove markdown/json projections
- redesign memory schema beyond task deduplication semantics

## Validation

Add regression tests for:

- failed consolidation keeps staged turns and retry signal intact
- distinct tasks under one entity coexist
- compaction-triggered sessions still inject recent unconsolidated context
- soft-biased retrieval still returns relevant items outside routed categories
