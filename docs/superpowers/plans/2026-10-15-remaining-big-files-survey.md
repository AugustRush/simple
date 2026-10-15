# The four unmeasured big files — survey

`2026-10-09-big-file-survey.md` measured `channels/web.py`, `channels/feishu.py` and
`tools/builtin_tools.py` and gave an extraction order for each.  Four large files were never
measured: `scheduler/store.py`, `memory/store.py`, `core/agent.py`, `memory/context.py`.  This
is the same treatment for them, produced the same way — by measurement, not by reading and
forming an impression.

## How these numbers were produced

For each file: parse with `ast`, take every method of the large class, and count

* **body lines** — `end_lineno - lineno`, so decorators and nested blocks are included;
* **`self` references** — every `self.<name>` in the method, split into **data** (an attribute
  that is not a sibling method) and **sibling calls** (`self.other_method()`).

The extraction cost is predicted by the reference count, not by the line count: a method that
touches no `self.<anything>` can move to a module and become a free function with its
arguments passed, while a method that calls four siblings needs a collaborator designed
first.  This is the metric the first version of this survey got wrong — see "A mistake worth
recording" below.

| file | lines | the big class | methods | method body lines | **`self`/`cls`-free methods** |
|---|---|---|---|---|---|
| `agent/scheduler/store.py` | 4086 | `SchedulerStore` | 86 | 3256 | **11 / 261 lines** |
| `agent/memory/store.py` | 2628 (was 2835) | `LTMStore` | 79 | 2377 | **9 / 212 lines** |
| `agent/core/agent.py` | 2366 (was 2536) | `BaseAgent` | 79 | 1940 | **35 / 358 lines** |
| `agent/memory/context.py` | 2098 (was 2484) | `ContextManager` | 72 | 1756 | **11 / 114 lines** |

81 methods and **1176 lines** can leave their class without designing a single seam.  That is
the headline, and it is why the extraction orders below start there.

## Three mistakes worth recording, all in the same metric

This measurement was wrong twice more before it was right, each time by counting too little:

1. **Only `self._attribute`, not `self.method()`.**  It reported `memory/context.py` as having
   **47 self-free methods and 1007 lines** — because a method whose whole body calls a sibling
   looks "self-free" under that rule while being maximally coupled to its class.  Corrected to
   24 / 563.
2. **Not `cls.`** — and this was the largest error of the three, because the affected code is
   `@classmethod` by the dozen.  `fit_to_budget` looked like a pure function; it calls eight
   `cls._` helpers.  Corrected: `memory/context.py` **16 / 240**, and `memory/store.py`
   19 / 317.  The cluster that actually left the file (below) was 11 methods / 384 lines, not
   the 5 / 317 the order first named.
3. **Signed parameters counted as free names.**  `cls` is bound, so a scan for "free names"
   filters it out — which is how (2) hid behind a check that looked like it covered it.

The lesson is the one this repository keeps meeting in its guards: a metric is a proxy, and
reading it as the thing itself produces a plan that is wrong in a direction nobody notices
until the work is underway.  The extraction order here was re-derived after each correction,
and the third one changed which cluster to move *first*.

---

## 1. `agent/memory/context.py` — 2484 lines

### Shape

`ContextManager` was 2368 of them: 83 methods, 2140 lines of body.  One module-level function
(`enqueue_orphan_staging_recovery`, 54) already sat outside it.  This was the file with the
most extractable material, and the least excuse for its size.

### `memory/fitting.py` — **extracted**, 2484 → 2098 lines

The cluster is closed under itself and is **11 methods / 384 lines**, not the five the first
draft of this order named: fitting needs the eviction notices, the protected indexes, the
message-unit arithmetic and the tool-history repair, and each of those needs another.

| lines | member | what it is |
|---|---|---|
| 113 | `fit_to_budget` | messages + a token budget in, messages out |
| 81 | `_complete_message_units` | message-unit arithmetic |
| 59 | `_repair_tool_history` | repairs a message list's tool pairing |
| 33 | `_protected_indexes` | which indexes a cut may not drop |
| 31 | `_with_notice_before_protected_request` | inserts the eviction notice |
| 25 | `_content_blocks` | the message's blocks, with their invariant enforced |
| 16 | `_strip_eviction_notices` | removes prior notices |
| 9 | `_eviction_notice` | builds the notice |
| 9 | `_anthropic_tool_result_ids` | which tool results a message answers |
| 5 | `_role_sequence` | the roles, in order |
| 3 | `_is_real_user_request` | user, and not only answering tools |

`fit_to_budget` stays on the class as a one-line `@classmethod` delegate: the `ContextService`
protocol declares it, and three test modules call `ContextManager.fit_to_budget(...)`.  The
other ten are reached as `fitting.<name>` from the class.  The two class constants moved with
them (`_EVICTION_SENTINEL`, `_COMPACTION_LOW_WATER`) and the three test references to them were
repointed rather than aliased, so the names have one home.

### What remains here

1. **`memory/text.py`** — the rendering half (`_format_working_state_text`, `_shows_text`,
   `_checkpoint_summary`, the other `_format*` helpers; 0 data refs, ≤1 sibling call each).
2. **The 0-data / 1-call group** (`record_usage_event`, `observe_token_usage`,
   `_is_episode_recall_query`, `_working_state_relevance_score`, …) — cheap, but each needs the
   one collaborator it calls passed in.
3. Only then the coupled core (`_project_working_state_from_event` 140 lines / 5 refs,
   `process_one_job` 107/5, `retrieve_implicit_context` 73).

## 2. `agent/core/agent.py` — 2536 lines

### Shape

`BaseAgent` was 2181 of them: 79 methods, 1940 lines.  Six small top-level things share the
file — `AgentContext`, `AgentResult`, `SubAgentProgressEvent`, `_TaskLocalContextStack`,
`_ContentFilterRecovery`, `_TurnLoopState`, `_execute_regular_tool_calls` — the largest of them
35 lines.

### `core/post_turn.py` — **extracted**, 2536 → 2366 lines

`_post_turn_maintenance` alone was **201 lines** of the `self`-free cluster, and it was already
a `@staticmethod`: staging, compaction and consolidation after a turn, taking everything it
needs as keyword arguments and reading nothing off the instance.  Verified faithful by diff
before committing: the moved body is byte-identical apart from the three intended
replacements (the function's name, its constant's local alias, and
`agent_module._with_task_context` → `shared._with_task_context`, which is the same function the
package root re-exports).

It stays callable as `BaseAgent._post_turn_maintenance` — the turn loop, the CLI and several
tests call it there — through a one-line staticmethod delegate.  Note the *service* that used
to sit in front of it was removed earlier as an unenforceable extension point; that decision
was about the name, and did not keep the code in the class.

### Extraction order

1. ~~**`core/post_turn.py`** — `_post_turn_maintenance` (201 lines).~~ **Done.**
2. **`core/turn_state.py`** — the small top-level classes (`AgentContext`, `AgentResult`,
   `_TurnLoopState`, `_TaskLocalContextStack`, `_ContentFilterRecovery`), which are data
   holders for the turn loop.
3. **The remaining `self`-free helpers** (`_clear_context_restart_message`,
   `_build_tool_loop_stuck_response`, `_extract_summary_text`, `consolidation_model`,
   `_protected_turn_messages`).
4. The 0-data / 1-call group (`_runtime_progress_message` 85, `_format_agent_error` 59, …).
5. `__init__` (132 lines, 17 refs) and `register_spawn_capability` (157) last: they are the
   object's identity and are supposed to know everything.

## 3. `agent/memory/store.py` — 2835 lines

### Shape

`LTMStore` is the whole file but for a dataclass: 89 methods, 2578 lines of body.  It is the
SQLite layer, so most methods are one query plus one mapper.

### `memory/rows.py` — **extracted**, 2835 → 2738 lines

The six `_row_to_*` mappers (89 lines) went first, as the order named.  Two things the move
surfaced, both worth recording:

* Two of the six were `@staticmethod` and four were plain methods taking `self` they never
  used; the extract keeps one signature per function (the row), and the two *callable
  references* — `self._row_to_fact_assertion` passed into a list rather than called — are the
  ones the first rewrite missed, because it only matched calls.
* The module is imported as `_row_mappers`, not `rows`: this file has **22 local variables
  named `rows`** (a cursor fetch is the most natural name in the world here), and an import
  called `rows` is shadowed by the first of them, which turned six mappers into attribute
  lookups on a list.  The survey cannot warn about that kind of thing; the first full run can.

### `memory/migrations.py` — **extracted**, 2738 → 2628 lines

The four schema steps (`_ensure_transcript_indexes` 15, `_add_missing_columns` 21,
`_backfill_transcript_seq` 50, `_next_transcript_seq` 19) take a connection and nothing else.
`_ensure_schema` (the only caller) keeps its sequence; the on-disk format does not change.

### What remains here (9 methods / ~212 lines measured)

1. **`memory/facts.py`** — `_insert_fact_assertion` (30), `_fact_assertion_core_score` (22),
   `_retract_inferred_identity_facts` (26), `_turn_keys` (13).
3. The 0-data / 1-call readers (`corpus_stats` 105, `list_session_ids` 47, `usage_summary` 35,
   `append_usage_event` 34) — each needs the connection passed.
4. The write paths (`write_conversation_exchange` 201, `apply_retention` 86) last.
2. **`memory/migrations.py`** — `_add_missing_columns`, `_backfill_transcript_seq`,
   `_next_transcript_seq`, `_ensure_fts_index`-adjacent schema work.
3. **`memory/facts.py`** — `_insert_fact_assertion`, `_fact_assertion_core_score`,
   `_retract_inferred_identity_facts`, `_read_facts`.
4. The 0-data / 1-call readers (`corpus_stats` 105, `usage_summary` 35, `list_session_ids` 47,
   `append_usage_event` 34) — each needs the connection passed, so after the pure ones.
5. The write paths (`write_conversation_exchange` 201, `apply_retention` 86) last.

## 4. `agent/scheduler/store.py` — 4086 lines

### Shape

`SchedulerStore` is 3514 of them: 86 methods, 3256 lines.  The file also has **14 top-level
`_migrate_to_*` functions totalling 259 lines** — already outside the class, and the cheapest
thing in this whole survey.

### The `self`-free cluster (11 methods, 261 lines) — measured

`_step_task_spec` (55), `_step_workspaces` (45), `_identity_values` (36), `_task_from_row` (29),
`_run_from_row` (25), `_attention_clause` (20), `_check_products` (15), `_check_acceptance`
(14), and three more.

### Extraction order

1. **`scheduler/migrations.py`** — the 14 `_migrate_to_*` functions (259 lines).  They are
   top-level already: this is a *move*, and the only question is which of them the class still
   calls (measured: `_ensure_schema` is the one, 135 lines, 2 refs).
2. **`scheduler/rows.py`** — `_task_from_row`, `_run_from_row`, and the step/identity helpers
   (`_step_task_spec`, `_step_workspaces`, `_identity_values`, `_check_products`,
   `_check_acceptance`, `_live_workflow_step`, `workflow_blocked_steps`).
3. **The cheapest coupled methods** — `_upstream_reports_in_transaction` (64 lines, 1 ref),
   `_insert_emission` (52, 1), `renew_lease` (35, 1), `disable_duplicate_enabled_tasks` (32, 1),
   `_join_arrivals` (28, 1).  Each needs the connection passed and nothing else.
4. The transaction core (`claim_due_tasks` 169, `_claim_task_in_transaction` 91,
   `_update_task_in_transaction` 90, `_skip_blocked_steps_in_transaction` 132) — these are the
   store's reason to exist, and they move last or not at all.
5. `deliver_signals` (184) and `complete_run` (164) last: they orchestrate the rest.

---

## What this does not cover

* **Whether each cluster is a *good* module.**  This survey measures coupling, which predicts
  what is *cheap*; a cheap move that produces an incoherent module is still a bad move, and
  that judgement is made per extraction, against the module's own docstring.
* **The other big files.**  `channels/web.py`, `channels/feishu.py` and `tools/builtin_tools.py`
  were measured in the 2026-10-09 survey; part of its order has since been overtaken (see the
  note in the delivery document's backlog).  `channels/web_session_paths.py` and
  `channels/web_provider_config.py` are the two clusters extracted from the first of them since.
* **Runtime behaviour.**  These numbers say nothing about which code is exercised; a cluster
  with no tests is cheap to move and expensive to move *safely*, and the extraction order above
  assumes the existing suite is the safety net it has been for every extraction so far.
