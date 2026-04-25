# Memory Facts And Retrieval Design

## Goal

Improve memory recall quality and answer accuracy by separating raw evidence from derived beliefs, making exact facts durable and queryable without relying on free-form text search, and keeping prompt injection relevance-first.

## Current Implementation Scope

This branch implements the first slice of the design:

- durable `fact_assertions` and `resolved_facts` tables in SQLite
- precedence-based fact resolution with explicit conflict preservation
- deterministic assistant identity bootstrap from config
- synchronous assistant-name capture for high-precision direct turns
- resolved-fact retrieval before free-form memory fallback
- conflict-aware implicit injection that prefers silence over guessing

Broader canonical fact extraction for user preferences, project state, and task state remains future work.

## Problem

The current memory system has improved storage durability, but it still mixes multiple jobs into one semantic layer:

- `conversation_turns` stores raw event history.
- `memory_items` stores free-form extracted memories, task items, notes, and session summaries.
- Retrieval still depends heavily on lexical recall over text content.

This causes four structural problems:

1. Exact facts and current beliefs are not modeled separately from raw evidence.
2. Stable facts such as assistant identity still depend too much on delayed consolidation.
3. Conflicting facts cannot be resolved deterministically without overwriting history.
4. Retrieval quality is limited by keyword matching and ad hoc heuristics instead of a clear truth model.

## Design Principles

1. Raw evidence and current belief are different objects and must be stored separately.
2. Exact facts should be queryable as structured data before any free-form search runs.
3. Query planning should bias retrieval, not hard-gate it.
4. Implicit prompt injection should prefer high-confidence resolved facts over loose summaries.
5. Conflicts should be preserved as evidence, not hidden by destructive upserts.
6. Evaluation must measure retrieval quality, not just latency.

## Memory Model

The system should distinguish four layers:

1. `raw evidence`
   - immutable conversation events, manual writes, deterministic bootstrap facts
2. `fact assertions`
   - normalized claims extracted from evidence
3. `resolved facts`
   - current best belief for a `(subject, predicate, scope)` key
4. `freeform memory`
   - summaries, concepts, procedures, and explanatory context that are not clean canonical facts

This replaces the previous mental model where a single table implicitly acted as both evidence and belief.

## Durable Storage

SQLite remains the durable source of truth.

### Existing Tables

- `conversation_turns`
  - append-only event history
- `memory_items`
  - retained for `episodes`, `concepts`, `procedures`, long-form project notes, and user-authored notes

### New Tables

#### `fact_assertions`

Append-only normalized claims derived from raw evidence or deterministic system state.

Required fields:

- `id`
- `subject`
- `predicate`
- `value_json`
- `value_type`
- `scope`
- `source_kind`
- `source_id`
- `source_session`
- `channel`
- `confidence`
- `status`
- `valid_from`
- `valid_to`
- `created_at`
- `updated_at`

Notes:

- `source_kind` examples: `conversation_turn`, `manual_write`, `bootstrap`, `consolidation_extract`
- `status` examples: `active`, `superseded`, `conflicted`, `archived`
- `value_json` keeps the schema extensible while still allowing typed values

#### `resolved_facts`

Materialized current belief per fact key.

Required fields:

- `fact_key`
- `subject`
- `predicate`
- `value_json`
- `value_type`
- `scope`
- `winning_assertion_id`
- `resolution_reason`
- `confidence`
- `resolved_at`
- `updated_at`

`fact_key` is defined as:

- `(subject, predicate, scope)`

`resolved_facts` is a derived table. It may be rebuilt from `fact_assertions`.

## Canonical Fact Schema

Canonical facts must not be represented as arbitrary prose first.

Minimum canonical shape:

- `subject`
- `predicate`
- `value`
- `value_type`
- `scope`

Examples:

- `subject=assistant`, `predicate=name`, `value="Afu"`, `scope=global`
- `subject=user`, `predicate=response_style`, `value="concise"`, `scope=global`
- `subject=project/auth_migration`, `predicate=status`, `value="done"`, `scope=workspace`

The schema must also support:

- time-bounded truth via `valid_from` and `valid_to`
- negation and correction via multiple assertions on the same key
- source-specific trust via `source_kind`

## Write Paths

### 1. Synchronous Exact-Fact Capture

Some fact classes are too important to defer to background consolidation.

These should be captured synchronously:

- assistant identity
- explicit user identity and stable preferences
- explicit task state changes
- explicit project state changes when phrased as direct statements

Synchronous capture should produce:

- one `fact_assertions` row
- one `resolved_facts` update if resolution is deterministic

This path is narrow by design. It should only fire for high-precision fact classes.

### 2. Deterministic Bootstrap Facts

Stable assistant identity should not rely on conversational memory extraction.

At startup, bootstrap facts should be materialized from deterministic system state such as:

- configured assistant name
- configured assistant role
- stable system prompt metadata when explicitly declared

Bootstrap facts write into `fact_assertions` with `source_kind=bootstrap` and may populate `resolved_facts` immediately.

### 3. Background Consolidation Extraction

Consolidation still extracts durable memory from staged turns, but it now has two outputs:

- canonical `fact_assertions`
- freeform `memory_items`

Consolidation should not write directly to `resolved_facts` without going through assertion resolution.

## Conflict Resolution

Conflicts are resolved over `fact_assertions`, not by replacing rows in place.

### Resolution Unit

Facts compete only within the same:

- `subject`
- `predicate`
- `scope`

### Precedence

Default precedence should follow evidence semantics, not tool path labels:

1. direct user evidence
2. explicit correction evidence
3. deterministic bootstrap facts
4. manual structured writes
5. extracted inference from conversation
6. summarization-derived claims

Recency only breaks ties within the same evidence class.

### Outcomes

Resolution may produce one of three outcomes:

1. `resolved`
   - exactly one winning assertion updates `resolved_facts`
2. `superseded`
   - an older assertion is retained in `fact_assertions` but marked non-current
3. `conflicted`
   - no safe winner exists; no implicit injection should use this fact

The resolver must never silently discard losing evidence.

## Query Planning

Query understanding should produce soft constraints, not a hard route.

The planner output should contain:

- `query_type`
  - `event_recall`
  - `fact_lookup`
  - `freeform_context`
  - `mixed`
- `scope`
  - `current_session`
  - `same_channel`
  - `global`
- `target_subjects`
- `target_predicates`
- `preferred_sources`
- `preferred_loci`
- `lexical_terms`
- `allow_freeform_fallback`

Examples:

- `What is your name?`
  - `query_type=fact_lookup`
  - `target_subjects=["assistant"]`
  - `target_predicates=["name"]`
- `What did we just discuss?`
  - `query_type=event_recall`
  - `scope=current_session`
- `What did we decide about retries last time?`
  - `query_type=mixed`
  - `scope=current_session` with fallback to global memory
  - `target_predicates=["decision"]`
  - `lexical_terms=["retries"]`

## Retrieval Pipeline

Retrieval should become a staged pipeline.

### 1. Candidate Generation

Query the right source types separately:

- `event_recall`
  - `conversation_turns` only
  - session and channel constraints are hard filters
- `fact_lookup`
  - `resolved_facts` first
  - `fact_assertions` only when conflict or provenance is requested
- `freeform_context`
  - `memory_items` via lexical retrieval
- `mixed`
  - structured fact candidates plus freeform support plus scoped event evidence

### 2. Reranking

Combine source-specific candidates with a structured score:

`final_score = lexical_score + field_match_bonus + exact_subject_bonus + exact_predicate_bonus + confidence_bonus + importance_bonus + recency_bonus - conflict_penalty`

Rules:

- resolved canonical facts outrank freeform summaries when both answer the same question
- scoped event hits outrank global event hits
- conflicted facts are excluded from implicit context
- zero-score candidates should not be injected implicitly

### 3. Assembly

#### Implicit Prompt Injection

Only include:

- high-confidence resolved facts
- small amounts of strongly relevant freeform context
- scoped recent unconsolidated context when current working memory no longer shows it

Do not include:

- unresolved conflicts
- zero-hit fallbacks
- unrelated high-importance memories

#### Explicit Retrieval

`context_retrieve` may include multiple sections:

- `Conversation History`
- `Resolved Facts`
- `Supporting Context`
- `Conflicting Facts`

## Freeform Memory Admission

Freeform memory remains necessary, but it should be explicit why an item is not canonical.

Acceptable freeform classes:

- concepts
- procedures
- explanatory project notes
- session summaries
- user-authored notes

If a candidate memory can be cleanly represented as a canonical fact, it should not be stored only as freeform text.

## Retention

Retention policy should diverge by layer:

- `conversation_turns`
  - durable evidence; no semantic pruning on the write path
- `fact_assertions`
  - retain as durable provenance unless explicitly archived
- `resolved_facts`
  - one row per fact key; always current materialization
- `memory_items`
  - keep current locus-aware retention, especially for `episodes`

## Migration

Migration should be phased.

### Phase 1

- add `fact_assertions`
- add `resolved_facts`
- keep current `memory_items` behavior unchanged
- add deterministic bootstrap for assistant identity

### Phase 2

- route exact-fact queries to `resolved_facts`
- keep freeform retrieval as fallback
- expose conflict sections in explicit retrieval

### Phase 3

- upgrade consolidation to emit canonical assertions and freeform memories separately
- add higher-precision synchronous capture for selected user and task facts

## Evaluation

Quality work on memory retrieval requires a gold evaluation set, not only latency benchmarks.

### Required Eval Categories

- assistant identity after restart
- user preference recall
- task status after correction
- project state after update
- current-session event recall
- cross-session isolation
- Chinese substring and paraphrase queries
- mixed queries combining facts and events
- conflict cases with no safe implicit winner

### Required Metrics

- exact-answer accuracy
- top-1 fact recall
- false-positive implicit injection rate
- cross-session leakage rate
- retrieval latency by source type

## Non-Goals

- No vector store or embeddings in the first implementation phase.
- No replacement of `conversation_turns` as durable evidence.
- No UI for browsing fact conflicts in this phase.
- No large file/module refactor outside the memory subsystem.

## Acceptance Criteria

- Assistant identity is recoverable after restart without relying on free-form text search.
- Exact fact queries read from `resolved_facts` before freeform memory search.
- Conflicting fact assertions are preserved as evidence and do not silently overwrite one another.
- Event-recall queries never leak unrelated sessions when scoped to the current session.
- Implicit prompt injection excludes zero-hit and conflicted fallbacks.
- A committed eval set exists for the required categories above.
