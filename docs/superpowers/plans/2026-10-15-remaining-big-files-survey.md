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
| `agent/scheduler/store.py` | 1534 (was 4086) | `SchedulerStore` | 74 | 2783 | **0 lines** |
| `agent/memory/store.py` | 1009 (was 2835) | `LTMStore` | 67 | 1763 | **0 lines** |
| `agent/core/agent.py` | 1980 (was 2536) | `BaseAgent` | 79 | 1940 | **33 / 301 lines** |
| `agent/memory/context.py` | 1113 (was 2484) | `ContextManager` | 60 | 1563 | **2 / 21 lines** |

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

### `memory/text.py` — **extracted**, 2098 → 1972 lines

The rendering half (`_format_working_state_text` 34, `_shows_text` 30, `_checkpoint_summary`
27, `_clip_working_state_text` 4, `_format_fact_value` 3, `_coerce_consolidation_result` 17):
how working state is *rendered* — given the state, produce the words — which is why the rules
can be read and tested apart from when they run.

One candidate was **excluded by the closure check rather than by taste**: `_merge_artifacts`
looked self-free but reads a free name the bound-name scan missed, so it stays in the class
until someone gives it a home deliberately.

### `memory/query_terms.py` — **extracted**, 1972 → 1932 lines

Three `self`-free predicates over text (`_matched_fact_alias_terms`, `_has_multi_intent_marker`,
`_weighted_term_overlap`, 40 lines): given a query, say what it names.  None of them ever took
`self`, so the signatures crossed over unchanged -- only the call sites lost their `self.`
prefix.

Worth recording: `_merge_artifacts`, which an earlier pass excluded for a free name it did not
have.  The name was its own `*groups` parameter -- the checker did not look at `vararg`/`kwarg`.
A false negative in the *checker* costs the same as one in the metric: a function wrongly
believed uncoupled gets moved, or in this case wrongly believed coupled gets left behind.

### What remains here

1. **The 0-data / 1-call group** (`record_usage_event`, `observe_token_usage`,
   `_is_episode_recall_query`, `_working_state_relevance_score`, …) — cheap, but each needs the
   one collaborator it calls passed in.
2. ~~`_working_state_candidates`, `_working_state_is_complete`, `_merge_artifacts`.~~
   **Done** -- `memory/working_state.py`, 1932 -> 1911 lines.
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

### `core/turn_state.py` — **moved**, 2366 → 2275 lines

The six data holders (`AgentContext` 10, `_ContentFilterRecovery` 37, `_TurnLoopState` 24,
`_TaskLocalContextStack` 13, `SubAgentProgressEvent` 9, `AgentResult` 6) plus
`DEFAULT_SYSTEM_PROMPT`, which moved out of `agent/__init__.py` because `AgentContext`'s field
default reads it.

That second move is the other half of a decision **already recorded** in
`agent/core/turn_loop.py`: it kept `AgentContext` at a function-local import, explaining that
"moving it down instead would mean moving ``DEFAULT_SYSTEM_PROMPT`` out of
``agent/__init__.py`` as well, because the field default reads it -- a larger change than the
cycle it would remove".  The judgement was right *for the goal it had* (removing one local
import).  The goal here is different -- a 2366-line file -- and paying that cost is exactly
what the survey is for.  The prompt now lives with the dataclass that defaults to it, and the
cycle is gone.

Two mechanical notes, the second of which cost a failed run:

* The 36-line prompt literal had to move **with** the class, not be imported from the package
  root: `agent/__init__.py` imports `.core.output` at line 54, well before it defines the
  prompt at 176, so a module-level `from agent import DEFAULT_SYSTEM_PROMPT` in a core module
  can execute against a half-initialised package.  `__init__.py` now takes the name *from*
  `turn_state`.
* **A class's decorator lines precede its `ClassDef.lineno`.**  Deleting a class by its own
  lineno leaves `@dataclass` orphaned behind -- which is a syntax error, not a subtle bug.  The
  method extractions had all used `min(decorator lines + lineno)`; the first class move did not.

### Extraction order (what is left)

1. ~~**`core/post_turn.py`** — `_post_turn_maintenance` (201 lines).~~ **Done.**
2. ~~**`core/turn_state.py`** — the small top-level classes.~~ **Done.**
3. ~~`core/tool_calls.py` — `_execute_regular_tool_calls` (40 lines).~~ **Done.**
4. The remaining `self`-free helpers.

### `core/tool_calls.py` — **moved**, 2275 → 2235 lines

`_execute_regular_tool_calls` (40 lines) takes a list of calls and an executor and nothing about
the agent that owns them -- which is why its tests already build a fake executor and hand it
calls.  The rule it implements is the interesting part: a batch containing a `shell` call runs
**sequentially**, because a medium-risk shell command blocks on the human consent menu and
nothing else may print while that menu is open.

It stays importable as `from agent.core.agent import _execute_regular_tool_calls` (the tests do
that), so no call site changed.
3. **The remaining `self`-free helpers** (`_clear_context_restart_message`,
   `_build_tool_loop_stuck_response`, `_extract_summary_text`, `consolidation_model`,
   `_protected_turn_messages`).
4. The 0-data / 1-call group (`_runtime_progress_message` 85, `_format_agent_error` 59, …).
5. `__init__` (132 lines, 17 refs) and `register_spawn_capability` (157) last: they are the
   object's identity and are supposed to know everything.

## 2a. A number I got wrong

The commit that moved the entry writers (`a153472`) and this survey both said
`agent/memory/store.py` came out at **990** lines.  It is **1009** (`git show a153472:... | wc -l`).
The pre-move figure (1130) was right, so the delta was over-reported by 19 lines.  The commit
message cannot be amended without rewriting history; this note is the correction.

## 2b. `agent/memory/context.py` — the job/consolidation cluster is **not** mechanically extractable

Fifteen methods (~290 lines: `process_one_job` 107, `enqueue_staging_job` 29,
`should_enqueue_consolidation` 22, `sleep` 18, `next_job`, `pending_jobs`, `stats`,
`mark_activity`, `idle_elapsed`, `on_memory_cleared`, `consume_memory_clear_suppression`,
`should_process_jobs`, `should_sleep`, `should_idle_sleep`, `should_session_end_sleep`) share six
pieces of mutable state: `_jobs`, `_lock`, `_processing_job`, `_needs_consolidation`,
`_last_activity`, `_suppress_next_turn_persistence`.

Two things block a move rather than a redesign:

1. `tests/test_background_memory.py:224` monkeypatches **the instance attribute** —
   `ctx_mgr.process_one_job = fake_process_one_job` **on a real `ContextManager`**, and
   `:455` reads `ctx_mgr._needs_consolidation`.  A moved function called by bare name would never
   see that patch, so the test would keep passing while exercising nothing.
2. The state is the *point* of the cluster (a queue plus a lock).  Passing it in as parameters
   means threading six mutable values through fifteen signatures.

The honest path is a deliberate refactor: a `JobQueue` (or `SessionRuntime`) object that owns the
six values, injected into `ContextManager`, with those two tests rewritten to inject a fake queue.
That is a behaviour-preserving redesign, not an extraction, and it should be done as its own
change with its own verification -- not smuggled in under "move some methods".

## 2c. `agent/tools/builtin_tools.py` — **done**: 2062 → 339, `_register` 1650 → 59

2062 lines, 11 methods, of which `_register` is **1650**.  Measured structure: `_register` is
`r = self.registry` followed by **43 independent `r.register(...)` statements**, each 20-40
lines (name, description, JSON schema, handler).  Nothing in it calls another method of the
class.

So the 84%-in-one-method figure is right, and the reduction is a split, not an extraction —
43 statements grouped by the domain their tool names already spell out (`schedule_*` 8,
`memory_*` 6, `workflow_*` 4, `list_*` 4, then files/shell/audio/context/web/plugin/output
and a few singles).

**What makes it a redesign rather than a move:** the statements are not self-contained.  They
reference the collaborator objects built in `__init__` — `self._file_tools` (7 uses),
`self._schedule_tools` (12), `self._plugin_tools` (8), `self._memory_tools` (6),
`self._web_tools` (3), `self._signal_tools` (3), `self._context_tools` (2) — as sources of
handlers and workspace helpers.  So each extracted `register(r)` needs those passed in as
well as the registry, and the natural shape is a per-cluster module holding
`def register(r, *, file_tools, ...) -> None`, with `_register` reduced to the ordered calls.

Two preconditions worth stating before starting, because both have bitten this file already:
the registry is a **shared mutable object**, so the split must preserve the *order* of
registration (any later registration can observe earlier ones); and the tool described here
(`scripts/_move_methods.py`) moves methods, not statement runs, so this is either a
hand-written split or a new mode for that tool — a decision to make explicitly rather than by
improvising.

**What it took:** `scripts/_split_registrations.py`, written for this shape (a contiguous run of
`r.register(...)` statements -> a module-level `register_<cluster>(r, *, <collaborators>)`), and
seven batches in descending index order so earlier indices never shift: file/shell/audio (272
string literals, verified identical), memory+context, schedule+workflow (729 lines, 524
literals), plugin/tool authoring, web, signals+step output, clear_context.  Every batch's string
literals were compared as a multiset against the original before it was written.

`_register` is now a readable ordered list: seven calls plus the one conditional registration
(`clean_output`, under `if self._output_dir is not None:`) that stayed because the tool moves
call statements and 23 lines did not justify a new mode.

Two defects came out of the tool itself, both from checking text where the question was "who
reads this": it first passed the collaborators as **bare parameter names** (which `_register`
does not have -- they are `self._file_tools`), which registered nothing at all; and its constant
check counted a constant's **own definition line** as a reader and refused a constant nothing
else touched.  Both are the same lesson as the rest of this work: a guard that answers a
different question than the one asked fails in the direction that looks like success.

## 2d. `BaseAgent`'s orchestration core — measured: it is the irreducible kernel

After the facade tier and the executors came out, `core/agent.py` is 1958 lines and what is left
of the *orchestration* is four methods.  Each was measured for lines, instance writes and test
doubles before deciding; none of the four can be moved by extraction, and the reason differs:

| method | lines | writes instance state | test doubles | verdict |
|---|---|---|---|---|
| `run_rendezvous_subtasks` | 41 | no | none | **moved** (`core/subtask_execution.py`, seams as capabilities) |
| `run_pipeline_subtasks` | 27 | no | 1 instance patch | a **thin delegate** to `orchestration/runtime`; nothing to move |
| `run_parallel_subtasks` | 14 | no | 1 instance patch | same |
| `_execute_agent` | 166 | no | closure of 17 methods / 296 lines | needs an `OrchestrationDeps` surface |
| `_run_tool_uses` | 122 | no | closure of 36 methods / 788 lines | needs the same, plus the tool loop |
| `send_message` | 111 | no | **30 test definitions, 15 patches** | a public seam; moving it breaks them |
| `register_spawn_capability` | 157 | **`workspace_root`, `_base_system_prompt`** | 6 subclass overrides | writes state and builds a `parent = self` closure |

So "it needs 14-18 collaborators" was the wrong summary.  The four that remain are blocked by
three *different* things, and only one of them is cardinality:

* **a public seam** (`send_message`) — 30 test doubles exist because it is the agent's entry point;
* **state ownership** (`register_spawn_capability`) — it initialises two attributes and closes over
  the agent; a facade would have to hand back the values and every caller would have to assign them;
* **one large closure each** (`_execute_agent`, `_run_tool_uses`) — they are the turn's decision
  graph, not a helper.

That is what `LoopDeps.from_services` is for, and its docstring says so: a turn should take
`llm`, `registry` and the rest as services, "with an agent being one thing that happens to hold
them".  The path to these four is therefore not a move but a **second construction route** — an
orchestration surface built from services the way `LoopDeps.from_services` is — and that is a
redesign of the turn's entry, to be judged as such.

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

### `memory/normalize.py` — **extracted**, 1837 → 1827 lines

Five pure functions of their arguments (`normalize_category_name`, `_normalize_content_key`,
`_is_palace_locus`, `_normalize_entity`, `_stable_merge_key`, 33 lines).  They are called all over
the store, so the class keeps a thin wrapper for each and every `self._normalize_entity(...)` call
site survives untouched.  The file shrank by ten lines, because 33 lines of body cost ~40 lines of
wrappers -- the ratio rule again.

The reason to do this small, unglamorous move first is the next one: the fact-ledger cluster's
`_fact_assertions_from_entry` reads `_normalize_entity` and `normalize_category_name`, so **415
lines were blocked on 33**.

The tool grew a capability that this exposed: rewriting `self.sibling(` to `sibling(` only handles
a sibling that is *called*.  A sibling handed over as a value -- `resolve_fact` passes
`key=self._fact_assertion_total_score` -- is a bare attribute reference, and leaving it alone makes
the tool refuse with "still references self".  Both shapes are rewritten now; it is the fifth time
this class of miss has cost a hand-fix.

### What remains here

Done, in order: `rows.py`, `migrations.py`, `facts.py` (the four writers/scorers), `usage.py`,
`queries.py`, `schema.py`, `fts.py`, `journal.py`, `append_conversation_turn`, `normalize.py`.
Left:

1. **`facts.py` — complete** (all twelve fact-ledger methods; 1761 -> 1561 lines) and
   **`conversations.py`** (`write_conversation_exchange` 201 + `_json_object`; 1561 -> 1388).
   The writer taught the tool a sixth connection shape: `conn = self._connect()` with no `with`,
   which never commits -- the *opposite* of the delegating case, and the wrapper must pass the
   connection without opening a `with` of its own.  Two more tool defects came out of it (a bare
   acquisition that was detected but not replaced, and refusals that did not say what they saw).
2. ~~**Extending `facts.py`** — **seven of twelve are in** (`read_fact_assertions`,
   `read_resolved_facts`, `has_conflicted_fact`, `retract_facts`, `_read_facts`,
   `_fact_assertions_from_entry`, `_fact_assertion_total_score`; 1795 -> 1761 lines).  Five are
   left, and they are **not mechanical**:
   * `resolve_fact` (117 lines) takes an **optional** `_conn` and only opens one when it is absent.
     It also writes (INSERT/UPDATE/DELETE), so the distinction matters: with `_conn` the caller owns
     the transaction and nothing is committed on exit (`contextlib.nullcontext`), without it the
     `with self._connect()` block commits. `--conn-expr` cannot express that -- it would open
     eagerly and ignore `_conn`. The honest shape is a transaction-agnostic function plus a wrapper
     that decides:
     ```python
     def resolve_fact(self, subject, predicate, scope="global", *, _conn=None):
         if _conn is not None:
             return facts.resolve_fact(_conn, subject, predicate, scope=scope)
         with self._connect() as conn:
             return facts.resolve_fact(conn, subject, predicate, scope=scope)
     ```
   * `add_fact_assertion`, `set_identity`, `_derive_missing_identity_facts`,
     `_repair_assistant_identity_facts` (145 lines together) move once `resolve_fact` has, because
     they call it.
   And one test has to move with them: `tests/test_fact_store.py` patches the **instance**
   `store.resolve_fact` to prove `add_fact_assertion` rolls back.  After the move the internal call
   resolves in `facts.py`, so the patch must target `agent.memory.facts.resolve_fact` -- otherwise
   the test keeps passing while intercepting nothing.
2. **`memory/entries.py`** then `memory/search.py` (`search_entries` 99).
3. **`memory/conversations.py`** (`write_conversation_exchange` 202 + `_json_object`).
4. **`memory/maintenance.py`** (`apply_retention`, `apply_decay`, `clear_persistent_memory`,
   `maintenance_snapshot`) -- **blocked** on the cache seam: seven methods call
   `_sync_after_mutation`, which writes `self._category_stats_cache`, and `apply_decay` /
   `clear_persistent_memory` write that cache directly.  They need an `invalidate_category_stats()`
   collaborator first (section 6).

## 4. `agent/scheduler/store.py` — 4086 lines

### Shape

`SchedulerStore` is 3514 of them: 86 methods, 3256 lines.  The file also had **14 top-level
`_migrate_to_*` functions totalling 259 lines** — already outside the class, and the cheapest
thing in this whole survey.

### `scheduler/migrations.py` — **moved**, 4086 → 3828 lines

The 14 migrations went first, exactly as the order named: they were top-level functions needing
only `sqlite3`, so this was a *move*, not an extraction.  The store imports the names back into
its own namespace, because the migration **table** — version number to function — is what says
what order they run in, and that belongs to the store.  No call sites changed.

### `scheduler/rows.py` — **extracted**, 3828 → 3575 lines

Eight of the measured cluster went (`_task_from_row` 29, `_run_from_row` 25, `_step_task_spec`
55, `_step_workspaces` 45, `_identity_values` 36, `_attention_clause`, `_check_products` 15,
`_check_acceptance` 14).  Three are **deliberately not there**:

* `_live_workflow_step` — the survey counted it `self`-free; it calls `self.get_workflow`.  The
  metric was wrong, and reading the moved code against its class caught it.
* `workflow_blocked_steps` — carries the store's own `@_synchronized`; locking is the store's.
* `_join_arrivals` — reads ids the caller holds.

Two lessons, both paid for in failed runs:

1. **Same body ≠ same contract.**  The store's `_dt` and `models.parse_datetime` were three
   identical lines except `_dt` returned `None` for an empty value.  Aliasing one as the other
   turned `None` into `fromisoformat("None")`.  The fix is a *named* home for the distinction:
   `models.optional_datetime`, with a docstring saying why it is not `parse_datetime`.
2. **A test that monkeypatches an instance method is a consumer of the class.**  Two workflow
   tests inject a mid-build failure by patching `store._step_task_spec`; after the move the
   call resolves in the importing module's namespace, so the patch target had to become
   `agent.scheduler.store._step_task_spec`.  Three test references to the moved names were
   repointed to `rows` for the same reason.

### The rest of the cluster — **moved**, 3575 → 3544 lines

Four more went into the same `scheduler/rows.py` (`_validate_graph` 15, `_emission_from_row` 13,
`_workflow_from_row` 8, `_emission_workflow_run_id` 1).  Three of them spelled `self` in their
signature and never used it, which the extraction asserted against before dropping it -- the
first attempt failed on that assertion and wrote nothing, which is the right failure mode for a
mechanical move.

**This file's measured cluster is now empty.**  What remains in `SchedulerStore` is the
transactional core -- `create_workflow`, `create_task`, `apply_retention`,
`workflow_blocked_steps` (which carries the store's own `@_synchronized`) -- and those need a
collaborator design, not a move.

### The store moves at all now — 3544 → 3538, and what that buys

`SchedulerStore` was, until this week, the one big class `scripts/_move_methods.py` could not
touch: 66 methods read `self._conn` (they run inside a transaction the *caller* owns) and 43
carry `@_synchronized`, whose own wrapper is `with self._lock` -- so every method has a hidden
`self._lock` dependency, and **zero** methods call `self._connect()`.  Both facts were verified
directly rather than taken from a report.

Two capabilities closed that gap, and the first cluster through was the four smallest readers
(`get_task`, `list_tasks`, `get_run`, `list_runs` → `scheduler/reads.py`):

* `--conn-expr self._conn`: the body's connection expression becomes the function's first
  parameter, and the class wrapper passes *its own* `self._conn` instead of opening one;
* decorators stay on the **wrapper**, so `@_synchronized` still holds the lock for the whole
  call.  A module function wearing that decorator would be called as `method(self, ...)` with
  `self` bound to the connection.

A note on what this buys, since the four readers only shrank the file by six lines: the four
bodies total ~36 lines and their wrappers cost ~30.  **Extraction pays in where the logic lives,
and only pays in line count when a body is much larger than its wrapper.**  The remaining
scheduler work should be chosen by that ratio.

By that ratio the next cluster was the ten transaction helpers, and it paid: **3538 → 3307**.
They all run inside a transaction the caller owns, so `--conn-expr` was exactly the mechanism
they needed, and the class keeps a one-line wrapper each (with `@_synchronized` where it had one).
Two things had to happen first, and both came out of the tool refusing rather than guessing:

* `_iso`, `_new_id` and `UTC` were defined in **both** `store.py` and `models.py` (or only in
  `store.py`).  They moved to `models.py` -- the module that already owns `UTC` and
  `optional_datetime` -- and `store.py` imports them, with its 90 call sites untouched.
* The tool learned to refuse deleting a module-level constant that **non-moved code still reads**.
  It had been about to delete `UTC` from `store.py` because only the moved methods read it *in
  that file* -- while 24 other *lines* (36 references) still read it.  `compile()` cannot see that (it is a runtime name
  lookup) and the unbound-name check only inspects the generated module, so it would have been a
  `NameError` at call time.  The dry run now runs every check the real run does, which is how the
  refusal became visible before anything was written.

**One method is deliberately left behind**: `_upstream_reports_in_transaction` reads the
module-level `_run_report_payload` (59 lines, also used by `_emit_run_signal`).  The tool cannot
carry it (it is a function, not a method) or re-import it (that would cycle), so those two report
helpers need a home of their own -- `scheduler/reports.py` -- before that method, or
`_emit_run_signal`, can move.
3. **The cheapest coupled methods** — `_upstream_reports_in_transaction` (64 lines, 1 ref),
   `_insert_emission` (52, 1), `renew_lease` (35, 1), `disable_duplicate_enabled_tasks` (32, 1),
   `_join_arrivals` (28, 1).  Each needs the connection passed and nothing else.
4. The transaction core (`claim_due_tasks` 169, `_claim_task_in_transaction` 91,
   `_update_task_in_transaction` 90, `_skip_blocked_steps_in_transaction` 132) — these are the
   store's reason to exist, and they move last or not at all.
5. `deliver_signals` (184) and `complete_run` (164) last: they orchestrate the rest.

---

## 5. The three files the 2026-10-09 survey covered — re-measured

That survey's order for `channels/web.py`, `channels/feishu.py` and `tools/builtin_tools.py` is
**stale**: named helpers no longer exist, and one file's plan is now spent.  Fresh measurements
(2026-10-15) below supersede it.  All three were measured the same way as the four above, but
with an independent cross-check against `symtable`, which agreed with the AST analysis on 98 of
101 scopes for `web.py`.

### `agent/tools/builtin_tools.py` — the plan is spent, and that is the finding

2062 lines, one class, **zero top-level functions** and **zero decorators**.  `_register` is
**1650 lines** of 44 `r.register(...)` calls and `__init__` is 81 lines: together **84%** of the
file.  Strictly extractable material is **96 lines**:

| lines | name | refs | what it is |
|---|---|---|---|
| 1 | `_ok` | ∅ | the envelope every tool answers in |
| 1 | `_error` | ∅ | the same, for failures |
| 20 | `_current_time` | `_ok`, `_error` | one timezone-aware tool handler |
| 74 | `_try_interactive_confirmation` | ∅ | the shell consent gate |

Plus five attribute-coupled accessors (28 lines) that read the registry's context keys.  The
honest conclusion is that this file's remaining reduction is *not* extraction: it is
per-cluster self-registration, which the module docstring already defers.

Traps found: **`SchedulerStore` is undefined in the module** (a return annotation at the
`_schedule_store` accessor, silent only because `from __future__ import annotations` makes it
unresolved until someone calls `typing.get_type_hints`); and moving the four context accessors
**requires editing the `CONTEXT_KEYS` ledger**, because `tests/test_context_keys.py` walks the
package for `get_context("...")` reads and asserts the ledger matches in both directions.

### `agent/channels/web.py` — 60 candidates, and the traps are all about identity

3256 lines; 97 methods; one `@property`, six `@staticmethod`s, **no `@classmethod` and no `cls.`
anywhere**.  Every self/cls reference a candidate has points at *methods of the same class* --
no candidate touches `_config`, `_sessions` or `_handler` directly.  Thirteen are truly
self-free; 40 more have their only call site as `self.X` handed to `Route(...)`.

The proposed order (cheapest first) is `web_run_artifacts` (already a module function) →
`web_assets` → `web_feishu_ops` → `web_provider_validation` → `web_attachments` →
`web_session_files` → `web_trigger_body` → `web_workflow_body` → `web_schedule_body` →
`web_router` (the ~1200-line route table, which needs a small state object first).

The traps are identity, not structure, and each one turns a passing test into a test that
passes while testing nothing:

* **A patched constant whose only reader is the candidate.**  `tests/test_web_channel.py` does
  `monkeypatch.setattr(web_module, "_ARTIFACT_WALK_LIMIT", 4)` and then calls
  `web_module._list_run_artifacts(...)`.  A function reads *its own* module's globals, so moving
  both to a new module silently defeats the patch.  The constants must travel with the reader
  **and the test must be repointed**.
* **Method monkeypatched by attribute through the class.**
  `monkeypatch.setattr(WebChannel, "_list_feishu_chats", staticmethod(...))` (and
  `_send_feishu_text`) only intercepts because the call is `self._list_feishu_chats`.  Turning
  them into plain module functions without a class-level alias makes those tests vacuous.
* **Bound-method callbacks are load-bearing.**  `_signal_problem` and `_feishu_ready` are handed
  to the scheduler builders as *methods*; extraction must pass plain functions, which changes
  callback identity.
* **Lazy imports must stay lazy.**  The `_feishu_*` helpers and the provider validators import
  `lark_oapi` / `agent.config` *inside the body*; that is precisely what makes
  `monkeypatch.setattr(config_module, "load_config", ...)` work.  Hoisting them to module scope
  in the new module breaks those tests.
* **A name defined twice**: `_web_session_upload_dir` is re-defined at web.py:153, shadowing the
  import at web.py:47, so the module attribute is the local copy while `_web_session_dir` is
  imported only to serve it.  Deleting the "duplicate" as obviously dead changes the live binding.

### `agent/channels/feishu.py` — 1958 lines, and the same identity traps in a heavier dose

Candidates cluster into: connection helpers (`_ws_reconnect_delay` plus its two constants,
`build_feishu_client` — already module-level and already imported by `web.py` and
`scheduler/delivery.py`, so those import paths must keep working), inbound ingest
(`_is_bot_mentioned`, `_report_processing_failure`, `_message_attachment_specs`,
`_download_message_attachments`, `_extract_message_attachments`), the send paths
(`_send_file_async`, `_send_plain_async`, `_send_tool_hint_async`, `_send_response_async`,
`_add_reaction`) and the outcome handlers.

Traps, worse here than in `web.py`:

* **Eleven methods are monkeypatched by attribute name** across ~60 test sites
  (`_add_reaction`, `_send_response_async`, `_send_file_async`, `_send_tool_hint_async`,
  `_send_plain_async`, `_download_message_attachments`, `_download_message_resource`,
  `_send_file_sync`, `_upload_file_sync`, `_do_send`).  Each must remain a class attribute, or
  the test stops intercepting.
* **Module constants patched by string path** (`agent.channels.feishu.LARK_AVAILABLE`,
  `_ATTACHMENT_CANCEL_GRACE_SECONDS`, `threading.Thread.start`), and two constants
  (`FEISHU_WS_RECONNECT_*`) whose only reader is a candidate and which tests import *by name* —
  so they must move **and** be re-exported.
* **A name that exists twice**: `_interaction_log` is both a module-level function and a method
  on `FeishuOutputSink`; `self._interaction_log` is the method, the bare calls are the function.
* **`getattr(sink, "defer_temporary_attachment_cleanup")`** in `coordinator.py` makes that name
  load-bearing even though the body is five lines.
* The `on_tool_end`/`on_info`/`on_status`/`on_error` overrides look self-free only because they
  are thin; they are `OutputSink` contract members, and several test hits for other modules are
  the *base* sink's methods, not these.

---

## 6. Corrections from an independent measurement

A second, independent pass over the three god classes (2026-10-15, read-only, cross-checked
against `symtable`) found five things this survey got wrong or left out.  They reshape the plan,
so they are recorded here rather than silently acted on.

1. **A reference count cannot tell a wrapper from a body.**  Twelve of `LTMStore`'s methods that
   section 3 lists as remaining are *already* thin delegates over `schema/`, `queries/`,
   `usage/` and `facts/`: `_ensure_schema`, `_ensure_fts_index`, `append_conversation_turn`,
   `recent_conversation_turns`, `transcript_window`, `_rows_at_seqs`, `list_session_ids`,
   `search_conversation_turns`, `append_usage_event`, `usage_summary`, `corpus_stats`,
   `_fact_assertion_total_score`.  The same is true of `ContextManager.fit_to_budget`.  A
   `self.X` count finds them because a wrapper still says `self._connect` -- the count measures
   coupling, and a delegate is coupled by construction.  Every "remaining" list here needs
   re-checking against the current file before it is acted on.
2. **`apply_retention` is not in `scheduler/store.py`.**  It exists in `memory/store.py` and
   `backends/sqlite_fts.py`.  A task specification that named it for the scheduler file was
   simply wrong.
3. **`@_synchronized` is an invisible dependency.**  **43** methods of `SchedulerStore` carry it,
   and the decorator's wrapper is `with self._lock: return method(self, *args)`.  Each therefore
   needs `self._lock`, and the decorator must stay on the class wrapper -- a module function
   decorated with it would be called as `method(self, ...)` with `self` bound to the connection.
4. **`scheduler/store.py` spells the connection `self._conn`, not `self._connect()`.**  Its
   methods do not open the connection; they are *called inside* a transaction the caller owns.
   Combined with (3) this means `scripts/_move_methods.py` currently **cannot move a single
   method out of that file** -- it refuses every one.  Before any scheduler work the tool needs a
   connection-expression option and wrapper-side decorator retention.  By contrast the 14
   non-decorated `*_in_transaction` helpers have no lock problem and are the right first move.
5. **The cache seam is one designed collaborator, not nine small moves.**  Only five methods
   write `self._category_stats_cache` directly (`__init__`, `_meta`, `_sync_after_mutation`,
   `apply_decay`, `clear_persistent_memory`), and only `_meta` reads it.  The real seam is
   `_sync_after_mutation`: **seven** methods call it (`_remove_category`, `write_entries`,
   `add_entry`, `add_entries`, `upsert_manual_note`, `apply_retention`, `merge_categories`), and it
   writes instance state, so it can never be a free function.  Those seven need an
   `invalidate_category_stats()` callable (or the moved function returns the touched categories and
   the class wrapper invalidates).  Until that exists, the write path and the maintenance group are
   not mechanical moves.  (An earlier version of this note said "nine methods assign or clear
   `self._category_stats_cache`"; the count and the mechanism were both wrong -- corrected by
   counting, not by memory.)

One more trap worth its own line, because it inverts a rule this survey states:
**`_TASK_IDENTITY_COLUMNS` is imported by `tests/test_scheduler_products.py` *from
`agent.scheduler.store`*, while only two moving methods read it.**  "A constant travels with its
only reader" is right about *reading*, and wrong about *naming*: a name that outsiders import has
to be re-exported from its old module even after its reader leaves.

---

## 7. Audit: did any extraction change a string?

Every extraction in this campaign claimed its moved bodies were faithful.  Those checks were
**whitespace-insensitive**, which made them blind to one thing the tool was doing: the line-level
dedent also stripped the leading whitespace of lines *inside* multi-line literals, and that
whitespace is part of the string's **value**.  The claim was therefore stronger than the check.

A value-level audit over all eighteen extracted modules (compare the sorted multiset of every
string literal per class-qualified function, against the pre-move revision) found:

* **zero real content changes** -- no moved literal lost, gained or altered a token;
* **whitespace-only differences in every module that contained a multi-line string** (7 of
  `journal.py`'s 12, 5 of `fitting.py`'s 12, 6 of `queries.py`'s 7, and so on): the interior
  indentation of SQL and of moved docstrings was four spaces shallower than the author wrote.

The tool now leaves every string-occupied line untouched, **including the closing line**, whose
leading whitespace is also part of the value.  `journal.py` -- re-extracted after the fix -- is
byte-exact: 12 of 12 functions identical, no whitespace differences at all.  The modules
extracted before the fix still carry the four-space difference inside their multi-line literals;
that is left as it is rather than re-running fifteen extractions to restore whitespace that no
test asserts and no reader is misled by -- but the deviation is recorded here instead of being
claimed away, and the modules moved from now on are exact.

Three instrument errors were made and corrected while doing this audit, which is why its
conclusion is stated in terms of a method rather than a boolean:

1. comparing against `HEAD` for an extraction that was **already committed** (so the "original"
   was the wrapper the move had just created);
2. comparing string lists **positionally** when `ast.walk` visits them in a different order once
   the `with` statement is gone -- the fix is a multiset comparison;
3. keying by function name alone, which matched `_TurnLoopState.__init__` against
   `BaseAgent.__init__` and reported a difference that a class-qualified comparison showed to be
   byte-identical.

---

## What this does not cover

* **Whether each cluster is a *good* module.**  This survey measures coupling, which predicts
  what is *cheap*; a cheap move that produces an incoherent module is still a bad move, and
  that judgement is made per extraction, against the module's own docstring.
* **The other big files.**  `channels/web.py`, `channels/feishu.py` and `tools/builtin_tools.py`
  are re-measured above; `channels/web_session_paths.py` and `channels/web_provider_config.py`
  are the two clusters extracted from the first of them before that measurement.
* **Runtime behaviour.**  These numbers say nothing about which code is exercised; a cluster
  with no tests is cheap to move and expensive to move *safely*, and the extraction order above
  assumes the existing suite is the safety net it has been for every extraction so far.
* **The mechanical transformation itself.**  `scripts/_move_methods.py` performs it and refuses
  when it cannot prove the result; the rules it encodes are in
  [`2026-10-15-subagent-work-order.md`](2026-10-15-subagent-work-order.md).

