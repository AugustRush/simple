# Service Kernel Refactor — closing report

**Scope:** the refactor reviewed and executed across 24 working rounds on branch
`refactor/service-kernel`, from commit `56d48be` (baseline) to the head at the
time of writing.

**Result:** 30 commits, 3062 tests passing, and a failure set byte-identical to
the baseline at every single step.  `agent/core/agent.py` went from 4704 to 3948
lines, and twenty-two new test modules were added.

---

## The evidence, in one place

| Measure | Baseline | Now |
| --- | --- | --- |
| `agent/core/agent.py` | 4704 lines | **3948** (−756, −16.1%) |
| `_execute_agent` (the sub-agent primitive) | 288 lines | **167** |
| `agent/core/` modules | 6 | **20** |
| Test modules added | — | **22** |
| Full suite | 19 failed / 2641 passed | **3 failed / 3062 passed** |
| FAILED set vs baseline | — | **byte-identical, all 24 rounds** |

The 19 → 3 drop is **not this work**: the run environment's file policy changed
from `workspace-write` to `danger-full-access` and all 16 sandbox failures
vanished with it.  Both baselines are recorded separately in
`2026-10-01-baseline-failures.txt`, with that reason, rather than the number
being quietly adjusted.

---

## What each stage delivered

### P0 — a frozen state layer

49 pieces of module-level mutable state that escape the service layer, recorded
in `agent/runtime/state_ledger.py` and enforced by
`tests/test_state_inventory.py`.  The scanner sees three shapes (module-level
`ContextVar`, `global`-rebound names, containers mutated in place); shapes it
cannot verify — instance attributes, import-time-only bindings, a function-local
`ContextVar` — are recorded as notes rather than entries, so the guard never
asserts something it did not check.  Verified by introducing a probe global: the
guard reports it by name and line.

### P1 — the registration primitive

`agent/runtime/services.py`: `ServiceRegistry`, `ServiceScope`, `ServiceView`.
The design point is that a scope records **undo actions** rather than owning
storage, because the contexts it has to wrap are not scope-private —
`ToolRegistry._context` is a shared dict that forks and the tool-call path both
read.  An undo action can restore a previous value, so a scope can override an
existing service and hand the original back on the way out.

### P2 — providers register instead of being dispatched

`agent/core/providers.py`.  `transport.py`'s own docstring claimed adding a
provider was "a single new `ModelTransport` subclass and zero edits"; the factory
was two hardcoded branches.  **Client construction lived in three places and had
already drifted** — the bootstrap copy silently produced an OpenAI client for an
unrecognised format where the factory raised.  One registry now, and
`resolve_api_key` written once instead of three times.

### P3 — three tool scopes become one

`ToolView` + `ToolRegistry.derive`.  The three mechanisms differed only in which
tools they accept, so the difference became data.  Three semantics were preserved
deliberately, each load-bearing and previously undocumented: an empty
`capabilities_include` admits nothing; a tool declaring no capabilities is
excluded from a bounded view; exclusion is applied after inclusion.

### P4 — the container backdoor closed

`get_context("components")` handed every tool the whole bootstrap result.  Only
**four keys** were ever read from it.  Both readers now declare what they use,
and a source-level guard fails if the path reopens — verified by reintroducing it.

### P5 — unload became one path, and two leaks closed

`PluginContext` + `PluginCatalog.unload_plugin`.  Before: five code paths, and
two limitations the catalog documented about itself — a removed plugin's MCP
subprocess could not be disconnected, and `on_session_end` did not fire on
eviction.  Both now have tests.  Recorded honestly: an **async** `on_session_end`
cannot be driven from inside a running event loop, so that case is skipped with a
log line rather than silently not running.

### P6 — the executor stopped knowing about plugins

`agent/core/events.py`.  `grep -n plugin agent/tools/executor.py` now returns one
comment.  Two existing semantics were pinned rather than tidied: the first
refusal wins and later listeners do not run; `ToolPostExecute` is observational
and its return value is ignored.

### P7 — `BaseAgent` split, in 14 extractions

Ordered by extraction cost, cheapest first, measured rather than assumed:

| Module | What moved | `agent.py` after |
| --- | --- | --- |
| `llm_errors.py` | retryability and error classification | 4657 |
| `token_budget.py` | the window arithmetic | 4554 |
| `tool_watchdog.py` | stuck detection | 4265 |
| `bounded_text.py` | model-facing text bounding | 4248 |
| `output_contract.py` | contract rendering and validation | 4248 |
| `spawn_planning.py` | batch planning, promotion, rendezvous parsing | 4169 |
| `subagent_context.py` | prompt composition, write scope, metadata | 4084 |
| `subagent_factory.py` | settings snapshot and construction | 4063 |
| `subagent_run.py` | the retry loop and its budget | 3993 |
| `subagent_prepare.py` | everything before a model call | 3986 |
| `subagent_finalize.py` | the payload and the memory write | **3948** |

Measuring per-method rather than per-group repeatedly changed the plan: the
runner group turned out to need **no** work (it already passed injected
callables), and the sub-agent group was four cheap methods plus one expensive one
rather than six uniform ones.

### P8 — composition, and an honest half

`build_gateway_channels` moved into `agent/bootstrap.py`, the composition module,
with a source guard against a second construction site (verified by introducing
one).  The scheduler's construction is an **ordering** constraint — it needs
`AgentCore`, created at `bootstrap.py:964` — not a scattered decision, and
`test_the_scheduler_is_not_here_and_that_is_recorded` fails the day that changes,
so the note cannot go stale.

### P9 — pluginization, at the layer the code can express

| Piece | State |
| --- | --- |
| `SchedulerStore` construction, 22 sites | one factory, `open_store(db_path=None)`, guarded |
| `RalphTaskStore` construction, 3 sites | one factory, `open_task_store(tasks_dir=None)`, guarded |
| `SchedulerService` recognising its store by concrete class | removed — the store answers for itself |
| Mounting either service as a profile row | **requires the Profile layer, which does not exist** |

The thread-affinity change is worth reading in full: I had escalated it twice as
a design decision needing a choice between two designs.  Measuring first showed
it was one class test in four lines, and the right answer was neither option —
**put the knowledge in the store that has it**.  One wart is preserved on purpose:
the check is by identity, not `isinstance`, so a subclass inherits its parent's
connection exactly as before.  Narrowing it would be a behaviour change wearing a
refactor's clothes.

---

## Behaviours preserved rather than tidied

Each of these was found while moving code, and each would have been easy to
"fix" silently:

- The first `on_pre_tool` refusal wins; later listeners do not run.
- `ToolPostExecute`'s return value is discarded.
- A sub-agent's own error **overwrites** a contract failure.
- `"(no output)"` is a pre-validation default, not a guarantee.
- The write-scope filter is string-shaped: `None` survives as `"None"`.
- `_calling_in_thread` uses identity, so a `SchedulerStore` subclass keeps the
  parent's connection.
- `merge_continuation_text`'s overlap search is bounded at 64 characters, so a
  longer restatement is only partly trimmed.

## Defects the work exposed

Not introduced — exposed by making previously unreachable paths testable:

- Client construction had drifted across three sites (P2).
- `same_batch_unproductive` is **unreachable** at the default thresholds: both
  counters advance on the same rounds, and the run-length one counts rounds from
  other batches too, so it is never behind.  The thresholds are now separable and
  the caveat is documented at the rule.
- A sub-agent's memory id was minted before the store write, so a failed write
  still left `artifact_ref` pointing at an entry that was never stored (P7).

## Mistakes I made, and what caught them

Recorded because the pattern is the useful part: **the splice risk was never the
block being replaced — it was the definition immediately after it.**

- Deleted `_run_tool_uses` along with the methods being moved; the integration
  tests hung, which is how it surfaced.
- Turned a conditional `return` into an unconditional one; every successful
  sub-agent reported "no result" (13 tests red).
- Deleted `_fresh_sub_ctx`, so every spawn raised `NameError` (18 tests red).
- `fresh_context` was accepted and never called, so a retry inherited the failed
  attempt's conversation.  Caught by a test asserting the *count* of contexts.
- Replaced `SchedulerStore.__init__`'s definition line, dropping the class body
  into module scope.
- Forgot an import after switching to `open_task_store` (34 tests red).
- Wrote a state guard that passed while checking nothing, because it blanked the
  very string it searched for.
- Made a source guard quadratic by re-tokenising per line; it hung.

Every one was caught by running the full suite and comparing the FAILED set
against the baseline, which is why that comparison is in this report as a
standing measurement rather than a footnote.

---

## What is not done

**The Profile/Bundle layer.**  This is the one thing the objective's Task 10
implies and the code cannot yet express: a plugin as a *row* with an `id`, a
`config` and an `inject` list, and a profile that selects rows.  P9 delivered
everything a plugin needs *from these two services* — a factory with an explicit
location, and no concrete-type inspection — but "what a plugin is" is a new
functional layer, not a refactor of an existing one.

Two starting points already exist in the tree and should be reused rather than
reinvented: `agent/scheduler/profiles.py:75` (`PERMISSION_PROFILES`) is a
config-override table that already documents why it exists — "adding a profile
meant adding a branch, and two places to keep in sync" — and Task 8's evidence
that composition needs one entry point.

Everything else in the objective is delivered, measured, and guarded.
