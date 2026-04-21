# Memory Projection Shell Removal Design

## Goal

Keep the internal SQLite memory loci model, but remove the remaining file-system "memory palace" shell from the user-visible layer. The only durable text projection should be one chronological JSONL export.

## Design

- SQLite remains the source of truth.
- `memory_items.category` continues to represent internal loci such as `identity`, `projects`, and `tasks`.
- Remove `MemoryIndex` and any chapter-directory initialization under `memory/`.
- `MemoryPalace.read_index()` continues to return the JSONL export content for compatibility.
- `MemoryPalace.export_jsonl()` writes `memory/memory.jsonl` sorted by `updated_at` ascending, then `id` ascending for stable ties.
- CLI and related displays stop treating memory as chapter folders and instead show either:
  - the JSONL export directly, or
  - lightweight structured stats derived from SQLite.

## Non-Goals

- No change to consolidation routing or retention semantics.
- No removal of internal category/locus fields from SQLite.
- No new user-facing file formats besides the existing JSONL export.

## Acceptance Criteria

- No memory chapter directories or `_index.md` files are created.
- User-visible memory export remains a single `memory.jsonl`.
- JSONL export order is chronological by `updated_at`.
- CLI no longer depends on `list_chapters()` to describe memory contents.
