# Durable Conversation History Design

## Goal

Make the agent able to answer history questions such as "刚才我们聊了什么" from durable evidence instead of relying on semantic memory summaries. The design separates three data types that have different jobs:

- Working context: bounded prompt messages used for the current model call.
- Semantic memory: extracted facts, preferences, tasks, and summaries.
- Event history: append-only user/assistant turns with time and session metadata.

## Design

SQLite remains the durable source of truth. Add a `conversation_turns` table beside `memory_items` and `staging_turns`. Each row stores one plain-text user or assistant event with `session_id`, `role`, `content`, `channel`, `created_at`, optional `message_id`, optional `reply_to_id`, and JSON metadata.

`conversation_turns` is source-of-truth event history. `episodes` remain derived summaries for coarse recall and retention, but they are not used as a substitute for exact or recent conversation recall.

Retrieval uses evidence type rather than keyword-specific behavior:

- Event-recall queries prefer `conversation_turns` and can include semantic memory as a secondary section.
- Semantic-recall queries prefer `memory_items`.
- Mixed queries may include both sections.

The existing `context_retrieve` tool remains the user-facing retrieval API, but its implementation asks the `ContextManager` for combined context. This avoids exposing a second tool until product usage proves it is needed.

User-visible export can stay as one JSONL memory file. Full turn history is internal durable evidence, not part of the user memory palace abstraction.

## Non-Goals

- No vector store, embeddings, or new ranking service.
- No message-level UI.
- No case-by-case hardcoded answers for specific Chinese or English phrases.
- No replacement of semantic memory extraction.

## Acceptance Criteria

- Completing a turn records both user and assistant plain-text messages in `conversation_turns`.
- Staging can be cleared after consolidation without deleting conversation history.
- `context_retrieve("刚才我们聊了什么")` returns actual recent turns when available.
- Semantic memory retrieval still works for preference/project/task questions.
