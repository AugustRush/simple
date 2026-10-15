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

| file | lines | the big class | methods | method body lines | **`self`-free methods** |
|---|---|---|---|---|---|
| `agent/scheduler/store.py` | 4086 | `SchedulerStore` | 86 | 3256 | **11 / 261 lines** |
| `agent/memory/store.py` | 2835 | `LTMStore` | 89 | 2578 | **20 / 320 lines** |
| `agent/core/agent.py` | 2536 | `BaseAgent` | 79 | 1940 | **35 / 358 lines** |
| `agent/memory/context.py` | 2484 | `ContextManager` | 83 | 2140 | **24 / 563 lines** |

90 methods and **1502 lines** can leave their class without designing a single seam.  That is
the headline, and it is why the extraction orders below start there.

## A mistake worth recording

The first version of this measurement counted only `self._attribute` and ignored
`self.method()`.  It reported `memory/context.py` as having **47 self-free methods and 1007
lines** — nearly double the truth — because a method whose whole body is a call to a sibling
looks "self-free" under that rule while being maximally coupled to its class.  The corrected
numbers are in the table above (24 / 563).

It is the same failure this repository keeps finding in its guards: a metric that measures a
*proxy* and is read as the thing itself.  Recording it here because the extraction order
depends on the metric, and an order derived from the wrong one would have sent the work at the
most coupled methods first.

---

## 1. `agent/memory/context.py` — 2484 lines

### Shape

`ContextManager` is 2368 of them: 83 methods, 2140 lines of body.  One module-level function
(`enqueue_orphan_staging_recovery`, 54) already sits outside it.  This is the file with the
most extractable material, and the least excuse for its size.

### The `self`-free cluster (24 methods, 563 lines) — measured

| lines | method | what it is |
|---|---|---|
| 113 | `fit_to_budget` | pure: messages + a token budget in, messages out |
| 81 | `_complete_message_units` | pure: message-unit arithmetic |
| 59 | `_repair_tool_history` | pure: repairs a message list's tool pairing |
| 34 | `_format_working_state_text` | pure: renders working state |
| 33 | `_protected_indexes` | pure: which indexes may not be dropped |
| 31 | `_with_notice_before_protected_request` | pure: inserts a notice |
| 30 | `_shows_text` | pure |
| 27 | `_checkpoint_summary` | pure |

### Extraction order

1. **`memory/fitting.py`** — `fit_to_budget`, `_complete_message_units`, `_repair_tool_history`,
   `_protected_indexes`, `_with_notice_before_protected_request` (~317 lines).  The single
   biggest pure cluster in the four files, and the one with the most test coverage: the budget
   rules are already asserted directly.
2. **`memory/text.py`** — the rendering half (`_format_working_state_text`, `_shows_text`,
   `_checkpoint_summary`, and the other `_format*` helpers).
3. **The 0-data / 1-call group** (`record_usage_event`, `observe_token_usage`,
   `_is_episode_recall_query`, `_working_state_relevance_score`, …) — cheap, but each needs the
   one collaborator it calls passed in, so they go after the pure ones.
4. Only then the coupled core (`_project_working_state_from_event` 140/5 refs,
   `process_one_job` 107/5, `retrieve_implicit_context` 73).

## 2. `agent/core/agent.py` — 2536 lines

### Shape

`BaseAgent` is 2181 of them: 79 methods, 1940 lines.  Six small top-level things share the
file — `AgentContext`, `AgentResult`, `SubAgentProgressEvent`, `_TaskLocalContextStack`,
`_ContentFilterRecovery`, `_TurnLoopState`, `_execute_regular_tool_calls` — the largest of them
35 lines.

### The `self`-free cluster (35 methods, 358 lines) — measured

`_post_turn_maintenance` alone is **201 of those 358 lines**, and it is already a
`@staticmethod`: staging, compaction and consolidation after a turn, taking everything it needs
as arguments and returning nothing.

### Extraction order

1. **`core/post_turn.py`** — `_post_turn_maintenance` (201 lines).  A pure function in all but
   its syntax; the only reason it is a method is history.  Note the seam that used to sit in
   front of it (`post_turn_maintenance` as a *service*) was removed as an unenforceable
   extension point — that decision was about the *name*, and does not keep the *code* here.
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

### The `self`-free cluster (20 methods, 320 lines) — measured

| lines | method | what it is |
|---|---|---|
| 50 | `_backfill_transcript_seq` | a migration step |
| 30 | `_insert_fact_assertion` | row writer |
| 26 | `_retract_inferred_identity_facts` | row writer |
| 22 | `_fact_assertion_core_score` | scoring a row |
| 21 | `_add_missing_columns` | a migration step |
| 19 | `_next_transcript_seq` | pure arithmetic over a value |
| 18 | `_row_to_fact_assertion` | row → object |
| 18 | `_row_to_conversation_turn` | row → object |

### Extraction order

1. **`memory/rows.py`** — the `_row_to_*` mappers (6 methods).  Pure functions of a row; they
   are also the ones a second backend would want.
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
