# Service Kernel Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the hand-written `components` dict into an explicit, typed,
disposable service layer, so that (a) an extension can register anything —
tools, commands, skills, hooks, MCP servers — and have it all unwind on unload,
and (b) any of those contributions can be replaced by config without editing
call sites.

**Architecture:** This is the unfinished half of
`2026-04-28-turn-runtime-boundary.md`. That plan deliberately introduced
`RuntimeComponents` as "the stable seam for later `TurnRunner` extraction" and
left it a thin wrapper over `dict[str, Any]` — `values` plus `require(name)`.
The seam was never widened, so every dependency is still resolved by string name
at runtime and every registration is still undone by a bespoke code path.

The target is not "everything is a plugin". It is the narrower and load-bearing
claim that **registration is an effect**: whatever a provider registers, it
unwinds. The repository already contains one complete instance of the target
shape — `agent/exec/subprocess.py` has a one-method `Protocol`, a
`provider_from(registry)` resolver, and a replaceable
`LocalSubprocessProvider`. Ten of the tasks below are "make the rest of the
codebase look like `exec/`", in an order chosen so that each step is verifiable
without the next.

**Tech Stack:** Python 3.13, asyncio, SQLite (WAL), pytest. This plan changes no
frontend and no user-visible behaviour.

---

## Status

**Tasks 0-6 done.  Task 7 is 10 of 11 extractions done.  Task 8 half done (channels moved; scheduler is an ordering constraint, see below).**
Branch: `refactor/service-kernel`.

Measured after each stage, same command every time
(`.venv/bin/python -m pytest -q -p no:cacheprovider`):

| Stage | Full suite | `agent/core/agent.py` | Headline change |
| --- | --- | --- | --- |
| Baseline | 19 failed / 2641 passed | 4704 | — |
| T0 state ledger | 19 / 2649 | 4704 | 49 pieces of escaping state recorded + guarded |
| T1 service registry | 3 / 2687 | 4704 | scoped, reversible registration |
| T2 provider registry | 3 / 2705 | 4704 | a wire format is registered, not dispatched |
| T3 tool views | 3 / 2730 | 4704 | 3 scopes → 1 (`ToolView` + `derive`) |
| T4 container backdoor | 3 / 2737 | 4703 | `get_context("components")` gone |
| T5 plugin unload | 3 / 2753 | 4703 | 5 unload paths → 1; 2 documented leaks closed |
| T6 tool events | 3 / 2772 | 4703 | executor no longer holds the plugin catalog |
| T7a error classification | 3 / 2803 | 4657 | `llm_errors.py` |
| T7b window arithmetic | 3 / 2818 | 4554 | `token_budget.py` |
| T7c stuck detection | 3 / 2843 | 4265 | `tool_watchdog.py` |
| T7d1-4 text/contract/planning/runners | 3 / 2908 | 4169 | `bounded_text.py`, `output_contract.py`, `spawn_planning.py` |
| T7d5 sub-agent context | 3 / 2926 | 4117 | `subagent_context.py` |
| T7d6 payload boundaries | 3 / 2944 | 4105 | tests only |
| T7d7 metadata inheritance | 3 / 2950 | 4084 | `propagate_sub_metadata` |
| T7d8 run promotion | 3 / 2960 | 4067 | `promote_orchestration_runs` |
| T7d9 sub-agent factory | 3 / 2978 | 4063 | `subagent_factory.py` |
| T7d10 retry loop + budget | 3 / 2997 | 3993 | `subagent_run.py` |
| T7d11 sub-agent preparation | 3 / 3014 | 3986 | `subagent_prepare.py` |
| T7d12 result finalization | **3 / 3036** | **3948** | `subagent_finalize.py` |

The FAILED set is byte-identical to
`docs/superpowers/plans/2026-10-01-baseline-failures.txt` at every stage.  The
19 → 3 drop between T0 and T1 is **not this work**: the run environment's file
policy changed from `workspace-write` to `danger-full-access`, and all 16 sandbox
failures vanished with it.  Both baselines are recorded separately, with that
reason, rather than the number being quietly adjusted.

Command for every column: `.venv/bin/python -m pytest -q -p no:cacheprovider`.

**The 19 → 3 drop is not this work.** The run environment's file policy changed
from `workspace-write` to `danger-full-access` between the Task 0 and Task 1
measurements, and all 16 sandbox failures vanished with it — `sandbox-exec` can
now apply its profile where before every one failed with
`sandbox-exec: sandbox_apply: Operation not permitted`. The baseline file
records both measurements separately, with that reason, rather than adjusting a
number. The three remaining failures are the `test_cli_ui` console-width
probes, unchanged throughout.

Test growth is accounted for exactly: Task 0 added 8
(`tests/test_state_inventory.py`), Task 1 added 22
(`tests/test_service_registry.py`), Task 2 added 18
(`tests/test_provider_registry.py`), Task 3 added 25
(`tests/test_tool_scope.py`). The four files Task 1 names
(`test_service_registry`, `test_user_tools`, `test_subprocess_provider`,
`test_user_tool_isolation`) run 77 passed; the six files Task 2 names
(`test_provider_registry`, `test_provider_settings`, `test_model_routing`,
`test_provider_headers`, `test_subprocess_provider`, `test_thinking_effort`,
`test_usage_telemetry`) run 263 passed, all with no failures.

> Note: `2026-09-22-scheduler-concurrency.md` records the suite at "16 failed"
> under the older sandbox policy. That count is comparable to neither column
> above.

### What Task 1 actually produced

`agent/runtime/services.py` (the primitive) and a `provider_from` that uses it:

- `ServiceRegistry` — flat name → service, refuses silent overwrite, resolves
  with `MissingServiceError` instead of `None`.
- `ServiceScope` — unwinds in reverse order, exactly once, idempotently, and a
  failing teardown does not strand the others.
- `ServiceView` — a read-only dict-like view, so the ~48 existing
  `get_context` readers migrate without changing shape.
- `provider_from` consults the direct `subprocess_provider` key first and an
  attached service registry second, so both old and new injection work.

The property that motivated the task, asserted in
`test_a_scoped_provider_replacement_reverts_on_unload`: a scope replaces the
execution provider, every resolver sees the replacement, and on unload the
original is back — with no call site told either time.

### What Task 2 actually produced

`agent/core/providers.py` (the registry) and one client-construction path:

- `ProviderRegistry` — a wire format maps to a client factory and a transport
  factory; refuses to shadow an existing format unless asked.
- `UnknownApiFormatError` names the format asked for *and* every format that is
  registered, because guessing between those two questions is the slow path.
- The two built-in formats register through the same call an extension uses,
  which is what keeps the extension path honest.
- `resolve_api_key` is now written once. It had been written three times.

**A real defect removed, not just tidied:** client construction lived in three
places (`ModelClientFactory.from_config`, `bootstrap._provider_client_factory`,
and the web channel's connection test) and they had drifted — the bootstrap copy
silently produced an OpenAI client for an unrecognised format where the factory
raised. Both now go through the registry, so an unknown format fails loudly
whichever path reaches it. `tests/test_provider_registry.py` pins that.

**Compatibility:** `build_transport` remains importable from
`agent.core.transport` via a module-level `__getattr__`, because that is where
callers and tests have always reached for it. The deferred lookup is not
cosmetic — `providers` imports the transport classes, so a module-scope import
back would be a cycle.

**Ledger interaction:** the registry is a process-level singleton built by a
constructor call, so the Task 0 scanner cannot see it. It was added to the
ledger's non-scannable notes and assigned to Task 9, which is the mechanism
working as designed rather than an exception to it.

### What Task 3 actually produced

`ToolView` and `ToolRegistry.derive` in `agent/tools/runtime.py`, and
`BaseAgent._create_sub_registry` rewritten as one `derive` call.

The three scopes differed only in **which tools they accept**, so the difference
is now data and the copy is written once:

| Scope | Rule |
| --- | --- |
| whole-process | `ToolView()` — accepts everything |
| per web session | `exclude_source_prefixes=("runtime:spawn",)` |
| per sub-agent | `capabilities_include=<profile bound>`, `capabilities_exclude={"orchestration"}` |

Three semantics were preserved deliberately rather than tidied, because each is
load-bearing and none was documented:

1. **An empty `capabilities_include` admits nothing**, not everything. Judged by
   subset alone it would be a wildcard that reads as the strictest setting.
2. **A tool declaring no capabilities is excluded from a bounded view.** Unknown
   is not the same as safe; the sub-agent profiles depended on this.
3. **`capabilities_exclude` is applied after `capabilities_include`**, so a
   broader inclusion cannot re-admit what a narrower rule removed.

One behaviour was found *while* collapsing them and had to be preserved
explicitly: `derive` clones mutable context containers on the way in, inherited
ones included. The old sub-agent path cloned two named lists by hand
(`shell_blocked_commands`, `shell_allowed_commands`); a child appending to a
list it merely inherited would otherwise have rewritten the parent's. Cloning by
shape rather than by key name covers the next list someone adds.

`fork()` remains, as a thin wrapper over the same call, because callers and four
test files use it.

### What Task 4 actually produced

The `components` backdoor is gone, and both readers now name what they use.

The finding that made this cheap: `PluginCatalog.reload` reads exactly **four**
keys out of the container -- `command_router`, `skill_catalog`, `mcp_client`,
`registry` (every `components.get(` in `agent/plugins/catalog.py`). Both
bootstrap paths now provide those four under their own names, and
`_reload_plugins` rebuilds the four-key dict it passes on. So the plugin manager
now declares four dependencies instead of receiving everything, and the
`user_tools` reader declares two (`user_tool_catalog`, `user_tools_enabled`).

`mcp_client` is only set when it is not `None`; a missing key already read as
`None` through `get_context`, so behaviour is unchanged.

Two things went wrong while writing the guard test, and both are worth
recording because the first version *passed* while checking nothing:

1. It blanked string literals wholesale, which deleted the `"components"` it was
   searching for. A source guard that cannot fail is worse than none.
2. It re-tokenised each file once **per line**, making it quadratic and hanging
   on the largest module (the 4.7k-line `agent.py`).

The guard now blanks comments and multi-line string interiors only, keeps the
opening delimiter so indentation survives, and matches the *call shape*
(`(get|set)_context("components"`) rather than the bare word, because error
text in `agent/tools/files.py` legitimately says "components". Verified by
reintroducing the backdoor: the guard reports the exact file and line.

### What Task 5 actually produced

`agent/plugins/context.py` and a real unload path, so a plugin's contributions
unwind instead of being rebuilt around.

- `PluginContext` is what `setup(ctx)` is handed: `add_slash_command`,
  `add_bundled_skill`, `add_bundled_mcp`, `add_agent_definition`, `add_tool`,
  and `on_unwind` for anything else. Each records its own undo on a
  `ServiceScope` (Task 1), so the catalog never enumerates the kinds.
- `PluginInstall` holds the plugin, its scope, and its unloaded flag.
- `PluginCatalog.unload_plugin(name)` is now **the** unload path: fire
  `on_session_end`, dispose the scope, call `teardown`, remove the assets, drop
  the module cache entry. Synchronous, matching `discover_and_load`, which
  reload calls it from.
- `reload` unloads every loaded plugin before re-discovering. Unloading all of
  them rather than only the removed set is deliberate: re-discovery re-imports
  every module, so each plugin genuinely becomes a new install and the old
  scope must not stay open beside it.

**Both documented leaks are closed**, and the test names them:

| Old limitation (`catalog.py:1328-1334`) | Now |
| --- | --- |
| "Cannot disconnect an MCP stdio process for a removed plugin ... the subprocess stays alive until the agent shuts down" | `teardown()` runs on unload; the test's handle is closed |
| "Plugin `on_session_end` hooks are NOT fired on eviction" | fired on unload, including eviction through `reload` |

`teardown` is not a failure of the scope design. A scope can only undo what was
registered *through* it; a plugin that started a subprocess has to say so
itself. What was missing was a moment to say it in, and that is what unload
provides. The order is fixed and tested: hook, scope, teardown — the hook runs
while the plugin's registrations still exist, and teardown last because it is
most likely to need the plugin whole.

### What Task 6 actually produced

`agent/core/events.py` (two typed events and a bus) and one wiring change in
`agent/core/agent.py`, replacing `RegularToolExecutor(..., plugin_catalog=...)`
with `event_bus=TOOL_EVENTS`.

The direction of the dependency is now one way. The executor publishes typed
events and names nothing about plugins; `PluginCatalog` subscribes in its
constructor and adapts each event to the existing hook chain, so `on_pre_tool`,
`on_post_tool`, and the `plugin.json` command hooks all run exactly as before.
`grep -n plugin agent/tools/executor.py` now returns one comment.

**Both existing semantics are preserved and pinned by tests**, because a
behaviour change smuggled in under a refactor is the failure mode this task is
most exposed to:

| Behaviour | Where it came from | Pinned by |
| --- | --- | --- |
| First refusal wins; later listeners do not run | `catalog.py:1819-1849` | `test_the_first_block_wins_and_later_listeners_do_not_run` |
| `ToolPostExecute` is observational; a return value is ignored | `executor.py:538` | `test_a_post_listener_return_value_is_ignored_by_the_executor` |

A listener refuses by returning a truthy reason, so `None` means "no opinion" —
the only default a listener can have. The refusal reason from the listener that
blocked is what reaches the model, which the short-circuit test asserts
explicitly.

**Defaults are chosen for isolation.** `RegularToolExecutor(event_bus=None)`
publishes nowhere, so an executor built by a test is isolated by construction;
production passes the shared `TOOL_EVENTS` bus explicitly. `PluginCatalog`
defaults to that same bus and unsubscribes in `close()`, which is asserted —
without it a reloaded catalog would gate every tool twice.

**The import cycle simplified as a side effect.** `agent/tools/executor.py` no
longer imports `agent.plugins.catalog` at all, removing one edge from the cycle
that Task 5 had to work around.

### What Task 7 has produced so far (extraction 1 of 4)

Order is by extraction cost, cheapest first, as the plan requires.

**Extraction 1 — error classification and continuation merging.**
`agent/core/llm_errors.py` holds five pure functions lifted out of `BaseAgent`:
`is_llm_retryable`, `is_account_exhausted`, `retryable_llm_classes`,
`is_content_filter_block`, `merge_continuation_text`. `agent/core/agent.py`
went 4703 → **4657** lines, and its methods are now thin delegations.

The reason this mattered is not the line count. Nine test functions in
`tests/test_llm_retry_policy.py` were building a full
`BaseAgent(object(), ToolRegistry(), model=..., api_format=...)` to call methods
that touch no instance state. Those tests now import the functions, and
`tests/test_llm_errors.py` tests the classification directly — no agent, no
registry, no model client — including a parametrised table of the retryable and
non-retryable tokens that would have been tedious to write against an object.

The `_retryable_llm_classes` cache moved from a class attribute to a
module-level `global`, so it is now a ledger entry rather than a hidden cache.
The guard caught that automatically, which is the mechanism working.

> **A delegation is not automatically safe.** `test_the_agent_methods_agree_with_the_functions`
> asserts the agent's methods still call the functions, because the extraction's
> failure mode is a wrapper that silently stops wrapping.

**Extraction 2 — context-window arithmetic.**
`agent/core/token_budget.py` holds `TokenBudget`, which absorbs
`_input_token_budget`, `_estimate_input_tokens`, `_estimate_raw_body_tokens`,
`_payload_overhead`, `_output_room`, `_retrieval_token_budget` and
`_configured_output_cap`. `agent/core/agent.py` is now **4554** lines.

This one is worth more than the line count, because the two bugs it encodes are
easy to reintroduce and were expensive to find:

- **The head is priced by the head's own measurement.** Re-deriving it through
  the body's calibration factor let a *body* change re-price a byte-identical
  *head* mid-turn — measured as 82588 → 68382 tokens, triggering emergency
  compaction the payload never needed.
- **The reserve is not the configured cap.** Letting the cap set the reserve gave
  a provider configured for long answers the smallest usable context.

Both are now asserted directly in `tests/test_token_budget.py`, without an agent.

**Two design points came from tests, not from taste.** `TokenBudget` reads
`context_window`, `output_reserve` and `max_retrieval_tokens` **live** through a
settings object rather than copying them, because `tests/test_consolidation.py`
sets all three on a live agent and the web channel resizes its window per
session — a copied value silently ignores the override. And `_token_budget` is a
**lazy property** rather than an `__init__` assignment, because a test subclass
skips `__init__` entirely to reach the arithmetic. Both were found because the
tests failed, not because they were anticipated.

`_OUTPUT_CAP_MARGIN` moved with the code; the copy left in `agent.py` was
deleted rather than left as a second definition of the same number.

**Extraction 3 — tool-loop stuck detection.**
`agent/core/tool_watchdog.py` holds `ToolLoopWatchdog` and `ToolLoopOutcome`
(re-exported from `agent.core.agent`). Thirteen methods and three constants
became one object with two thresholds. `agent/core/agent.py` is now **4265**
lines — 4703 → 438 overall, a 9% reduction.

**A real reachability defect was found while writing the tests.**
`same_batch_unproductive` cannot fire at the default settings.  Both counters
advance on the same unproductive rounds, but `unproductive_rounds` also counts
rounds belonging to *other* batch signatures, so it is never behind; with one
shared threshold the run-length rule always reports first.  The thresholds are
now separable (`batch_threshold`, defaulting to the old shared value so
behaviour is unchanged), the caveat is documented at the rule, and a test pins
the default unreachability so the docstring stays true.

**Three test expectations I wrote were wrong, and the code was right.**  Each is
now documented at the assertion rather than silently "fixed":
`fatal` is `all(...)` only when the batch is *unproductive* and `any(...)`
otherwise (a mixed batch is fatal); the stop message renders the rule as prose
rather than quoting the internal key at the user; and the occurrence rule needs
its own threshold to be reached at all.

**Extraction 4 — model-facing text helpers.**
`agent/core/bounded_text.py`: `append_named_block`, `bounded_json`,
`bounded_text`, `bounded_structured`.  They were static methods called from three
unrelated concerns (sub-agent prompt building, handoff summaries, output-contract
validation), which is what made them free functions.  The two truncation markers
are asserted to differ, because collapsing them would silently change what a
model sees.

**Extraction 5 — output contracts.**
`agent/core/output_contract.py`.  This one needed a real dependency change:
`_resolve_output_contract_path` read the output directory out of
`self.registry` and the fallback root out of `self.workspace_root`, which is why
the group could not simply be moved.  The validator now takes a `resolve_path`
callable and the agent supplies its own — so a validator's behaviour no longer
depends on what else is running.

Two expectations of mine were wrong here and the code was right, both now
documented at the assertion:

- The contract vocabulary is `format` / `required_keys` / `required_files`, **not
  JSON Schema**, and a `json` contract reads the `<deliverable>` block rather
  than the whole answer.
- An absolute path is **not** honoured unconditionally: one outside the allowed
  roots is refused.  "Absolute wins" is what a reader would assume, so the
  refusal is now a test with its reason attached.

**Extraction 6 — spawn-batch planning.**
`agent/core/spawn_planning.py` holds `SpawnPlanner`: capability-profile choice,
the per-turn agent ceiling, spawn-call→spec construction, the result payload, and
the coordination mode.  Six methods, none of which performed I/O.  Their 19
`self` attributes are declared once in `PlannerDeps` (8 fields) instead of
reached for individually, and the decision rules moved by mechanical rewrite
rather than being restated.

**An init-order defect was caught by the suite, not by me.**  The planner was
first assembled where the token budget is wired — before
`self.max_parallel_agents` is assigned — and **143 tests failed** with
`AttributeError: 'BaseAgent' object has no attribute 'max_parallel_agents'` on
construction.  Moved to after the assignment; all 143 passed without further
change.  Worth recording because it is the whole value of the baseline: a
mis-ordered assignment is invisible in review and loud in the suite.

**Extraction 7 — the runner group, and why it needed no work.**
The three runners (`run_parallel_subtasks`, `run_pipeline_subtasks`,
`run_rendezvous_subtasks`) were **already thin wrappers**: they pass
`self._execute_subtask_spec`, `self._canonicalize_write_scope` and
`self._emit_runtime_progress_event` as injected callables to the free functions
in `agent/orchestration/runtime.py`, which do the scheduling.  There was nothing
to extract — the injection seam the plan proposed already existed.  This is
recorded because "the group needed no work" is a finding, not an omission, and
the remaining two pure functions in it were moved:

- `summarize_subtask_result` — a truncation helper, now next to the payload
  builder it feeds.
- `parse_rendezvous_directive` — ~58 lines of normalizing a model's free-form
  JSON.  It is the one place a text contract is enforced, and it is now tested
  against fences, malformed input, true-like spellings of ``stop``, and unknown
  ids in ``continue_with``.  Those cases were previously reachable only through
  a live rendezvous round.

**Where Task 7 actually stands.**  Seven extractions are done and `agent.py` is
4117 lines (4703 → 4117, -586, a 12% reduction).

**Extraction 8 — the two cheap members of the sub-agent group.**
Measuring the group per-method rather than as a whole changed the plan.  It is
not uniformly coupled:

| Method | Lines | `self.*` |
| --- | --- | --- |
| `_canonicalize_write_scope` | 11 | 2 |
| `_compose_sub_system_prompt` | 28 | 3 |
| `_propagate_sub_metadata` | 28 | 0 |
| `_create_sub_agent` | 27 | 15 |
| `_execute_agent` | **288** | **16** |

So `agent/core/subagent_context.py` takes the two cheapest — path resolution and
prompt composition — and `_execute_agent` now calls two free functions instead of
two methods.  The property those two protect is the one a sub-agent gets wrong
*silently*: its system prompt is built from **its own** registry, never the
parent's active context, and a stub skill catalog is rejected by type before it
reaches the prompt builder.

**Extraction 9 — metadata inheritance.**
`propagate_sub_metadata` moved to `subagent_context.py`, which completes the
cheap half of the group.  What it protects is a whitelist, and the whitelist is a
contract that was buried in a 4k-line class: `skill_catalog`, `required_skills`,
`cancel_token`, `context_manager`, `session_id` travel; per-turn state does not,
because several sub-agents run at once and a wholesale copy would have them share
and overwrite each other's entries.  `required_skills` is copied rather than
shared so a child's append cannot rewrite the parent's list.

**Extraction 10 — promoting run summaries into memory.**
`promote_orchestration_runs` moved to `spawn_planning.py` with the store, clock
and entry class supplied as arguments, so it can be exercised without a store, a
clock, or the memory layer's entry class.  The three filters it enforces are now
each pinned by a test, and each exists because the alternative put something
wrong into memory a later turn reads as *fact*:

- ``result.ok`` — a failed sub-agent's text is not a finding;
- non-empty after stripping — an empty summary would occupy a retrieval slot
  while carrying no information;
- a length bound — this text joins a later turn's context, so unbounded
  promotion competes with the conversation it is meant to inform.

**What is left of Task 7.**  Two members, and they are one decision:

**Only `_execute_agent` remains** (288 lines, 16 attributes) — the single
primitive every runner calls.  With the factory extracted it no longer needs a
parent to build a child, so what is left is genuinely the loop's *policy*: step
ceiling, retry handling, contract validation.  The interface proposal below is
still the open decision; the extraction above is what makes it a smaller one.

**Extraction 11 — the sub-agent factory.**
`agent/core/subagent_factory.py` holds `AgentSettings` (the twelve inherited
configuration values as one frozen contract), `settings_from` and
`build_sub_agent`.  `_create_sub_agent` became a five-line call.  The fifteen
attribute reaches are gone: the builder takes data, so `_execute_agent` no longer
needs a parent to build a child.

Two details are load-bearing and now tested:

- **The transport is inherited, not rebuilt.**  It carries the model → provider
  routing table, so rebuilding one from `client` would pin the sub-agent to the
  *active* provider and a per-turn override naming another provider's model would
  be posted to the wrong endpoint.
- **The snapshot is frozen.**  A sub-agent runs with the limits the turn had when
  it was spawned; a later config edit cannot change a running child's ceiling.

**Extraction 12 — the sub-agent retry loop.**
`agent/core/subagent_run.py` holds `run_with_retries` and `retries_for_profile`.
111 of `_execute_agent`'s 288 lines were this loop, and measuring it first is
what made the extraction safe: it reached exactly four attributes of its parent,
so the rest became arguments -- including the function that performs one attempt,
which is what makes the retry policy testable with a scripted callable instead of
a live model.

**A defect the extraction introduced, and a guard that caught it.**  The first
version accepted `fresh_context` and never called it, so a retry inherited the
failed attempt's conversation: the model would be asked to continue a
conversation it had already answered, and the parent would read two attempts as
one turn.  The test that caught it pinned the *count* of contexts built (one
before the loop, one per retry) -- an assertion that only exists because the
extraction made the loop observable.  Two further splice errors were caught the
same way: an unconditional `return` where the original had a conditional one
(every successful sub-agent reported "no result"), and a stale failure payload
surviving a later success.

**The budget rules are now pinned by tests** rather than inferred from comments:

- `sub_agent_timeout_seconds` bounds the whole subtask, so a retry cannot
  multiply the wall clock it may consume (asserted with a hanging `send`);
- a timeout ends the loop instead of retrying, because it already spent the
  budget it would retry with;
- only `read_only` and `research` retry at all -- an implementation worker may
  have written files a retry cannot roll back;
- partial text from a timed-out attempt is salvaged, and an empty one is omitted
  rather than stored as `""`.

`agent/core/agent.py` is now **3993** lines -- 4703 to 3993, a 15% reduction.

**Extraction 13 — sub-agent preparation.**
`agent/core/subagent_prepare.py` resolves everything about a sub-agent that does
not depend on a model call: the write scope, the canonical output contract, the
contract-shaped task, the plugin role body, the sub-registry, the sub-agent, and
the system prompt.  It is built **once**, before the retry loop, because those are
pure functions of the arguments -- two attempts seeing two registries would mean
two different tool schemas for one subtask.

Two findings from writing the tests:

- **The three normalizations have to agree.**  The write scope feeds both the
  capability profile and the registry view; the output contract is read by both
  the prompt builder and the validator.  If either pair disagrees, one side
  silently sees an empty value -- which is why the preparation resolves each
  once and returns it rather than letting consumers re-derive.
- **The write-scope filter is string-shaped, not value-shaped.**  It is
  `if str(item).strip()`, so `None` survives as the string `"None"`.  Recorded
  and tested rather than narrowed: this is pre-existing behaviour moved verbatim,
  and changing what a tool call may declare is a decision, not an extraction.

**Another splice error, caught the same way as the last one.**  The replacement
removed `_fresh_sub_ctx` along with the block it was replacing, so every spawn
returned `name '_fresh_sub_ctx' is not defined` -- 18 tests red.  Restored.  The
pattern across three rounds is consistent enough to state: **the splice risk is
never the block being replaced, it is the helper defined immediately after it.**

**Extraction 14 — result finalization.**  With this, `_execute_agent` is
orchestration rather than implementation: **288 lines → 167**, and every one of
its remaining `self.*` references is a callable handed to an extracted function.

`agent/core/subagent_finalize.py` splits the last part in two, because only one
half has a side effect: `build_result_payload` is pure, and
`finalize_sub_agent` takes the store.  That separation is what makes the
interesting rules testable without a database.

**A real defect the split exposed.**  The original minted the memory entry's id
*before* the store write, inside the suppression context, so a failed write still
left `memory_entry_id` and `payload["artifact_ref"]` pointing at an entry that
was never stored -- a caller would then fail to retrieve it, with no indication
why.  The id is now only recorded after the write succeeds.  Nothing failed
before the change because no existing test made the store raise; the new
finalizer test does.

**Two behaviours recorded rather than tidied**, both pre-existing and now pinned:

- **The sub-agent's own error overwrites a contract failure.**  A run that both
  failed and missed its contract reports the *cause*; the contract miss is a
  consequence.  The precedence was easy to invert while moving the code.
- **`"(no output)"` is a pre-validation default, not a guarantee.**  A successful
  validation replaces `payload["content"]` with whatever the validator returned,
  so an empty answer with no contract ends up as the empty string.  The marker
  still does its job on the failure path, where nothing overwrites it.

### The one decision left, written down so it can be settled

`_execute_agent` is 288 lines and reaches 16 attributes; `_create_sub_agent` is
27 lines reaching 15.  They are the single primitive every runner calls
(`run_parallel_subtasks`, `run_pipeline_subtasks`, `run_rendezvous_subtasks` all
arrive here), so the interface chosen for them is the interface ralph and the
scheduler would eventually reuse — which is why it is a decision rather than a
move.

**What is actually in the 288 lines**, in order: resolve the scoped context
manager; compose the sub-agent system prompt from the sub-registry; build the
sub-agent; drive up to `max_tool_call_iterations` steps of model-request →
tool-execute → append; summarize the result; run the output-contract validation;
assemble and return a payload dict.  Only three of those need anything the caller
cannot supply as a value: *call the model once*, *execute a tool batch*, and
*report progress*.

**Proposed shape** — a `SubAgentRunner` in `agent/core/subagent_runner.py`:

```python
@dataclass(frozen=True)
class RunnerDeps:
    settings: Any                 # read live: max_* limits, timeouts, retries
    registry: Any                 # for the sub-registry view
    call_model: Callable          # one model request -> (stop_reason, text, tools)
    execute_tools: Callable       # one tool batch -> list[str] of results
    emit: Callable                # progress / sub-agent events
    summarize: Callable           # optional LLM summarize, already on the agent
    build_context: Callable       # sub-registry -> (system_prompt, AgentContext)

class SubAgentRunner:
    async def run(self, **kwargs) -> dict: ...      # what _execute_agent returns
```

`BaseAgent._execute_agent` becomes a one-line delegation, exactly as the ten
previous extractions did, and the loop's *policy* (step ceiling, retry handling,
contract validation) becomes testable with a scripted `call_model` — which is the
part of a sub-agent that is currently hardest to test and most likely to break.

**The alternative, if that interface is not wanted:** extract only
`_promote_orchestration_runs`-style leaves and leave `_execute_agent` in place,
recording it as a known limit.  That is defensible — it is one method, it is the
most stateful thing in the class, and the file is already 636 lines shorter — but
it means the sub-agent loop stays untestable without a live model, and
`Task 10` (ralph/scheduler as plugins) cannot reuse the primitive.

I have not taken either path: both are interface decisions, and the ten
extractions so far were all behaviour-preserving moves.
- `_create_sub_agent` (15) and `_execute_agent` (**288 lines, 16 attributes**) —
  **one decision, not two**: `_execute_agent` is the single primitive every
  runner calls, and extracting it means agreeing on how a sub-agent is
  constructed and driven.  That is a design decision, and it is the reason the
  plan ordered this group last.  Measured coupling
puts it far above everything extracted so far: it holds `registry`, `client`,
`_transport`, `context_manager`, `plugin_catalog`, `workspace_root`, and a dozen
configuration values, and `_execute_agent` is the single primitive every runner
calls.  Extracting it means agreeing on an interface for constructing and
driving a sub-agent, which is a design decision rather than a move — the plan
ordered it last for exactly this reason, and it has not been forced through.
4169 lines (4703 → 4169, -534, a 11% reduction).  The **runner group** (8 methods,
13 attributes) and the **sub-agent group** (6 methods, **29 attributes**) are
still on `BaseAgent`.  Measured coupling puts the sub-agent group far above
everything else extracted so far, which is why the plan ordered it last and why
it has not been forced through: it holds `registry`, `client`, `_transport`,
`context_manager`, `plugin_catalog`, `workspace_root` and a dozen configuration
values, and forcing it would be the kind of move that produces a wrapper class
rather than a boundary.

**One self-inflicted error, and what caught it.**  A scripted block replacement
deleted `_run_tool_uses` (the tool loop body) along with the methods it was
meant to remove.  The integration tests hung — the loop with no watchdog — which
is how it surfaced.  It was restored from `HEAD` and the whole suite re-run.
The lesson recorded here: `git diff --stat` alone did not reveal it, because the
deletion *looked* like the intended one; only the method-inventory diff
(`git show HEAD:… | grep -c '^    def '`) showed a method missing that had not
been moved.

**Still to extract, in order:** token budget (`_input_token_budget`,
`_estimate_input_tokens`, `_payload_overhead`, `_output_room`,
`_retrieval_token_budget`), then the tool-loop watchdog, then orchestration
(`run_parallel_subtasks:1280`, `run_pipeline_subtasks:1296`,
`run_rendezvous_subtasks:1325`, `_execute_agent:4217`) — which lives on
`BaseAgent`, not in `agent/orchestration/`, and is therefore the expensive one.

**One honest limitation remains.** An *async* `on_session_end` cannot be driven
when unload is called from inside a running event loop (`asyncio.run` leaves the
coroutine unawaited). Sync hooks run normally, bounded by their timeout; an
async hook in that situation is skipped with a dim log line, and a plugin that
needs async teardown has `ctx.on_unwind`. Recorded rather than hidden: the
alternative was a synchronous facade that silently did not run the hook.

**A cycle had to be broken twice.** `agent.plugins.context` needs
`agent.runtime.services`, but `agent.runtime` imports `contracts`, which imports
the skill catalog, which imports the tool registry, which imports
`agent.plugins.catalog`. Both imports are therefore function-local, with the
reason written where a reader will hit it.

### What Task 0 actually found

The scanner sees three shapes (module-level `ContextVar`, `global`-rebound
names, and containers mutated in place) and currently finds **49** of them:

| Group | Count | Note |
| --- | --- | --- |
| `ContextVar` | 11 | turn-scoped implicit arguments |
| Session-keyed tables | 8 | 6 shell authorisation + 2 plugin approval |
| `_set_agent_home` path constants | 19 | one `global` rewrite; two writers (`shared.py:101-133`, `agent/__init__.py:126-135`) |
| Other rebound machinery | 6 | sync tool pool, plugin install lock, system-prompt cache ×2, two path-derived |
| Interactive CLI state | 5 | process level by construction, one terminal per process |

Four shapes are **deliberately outside** the ledger because the scanner cannot
verify them; they are recorded as notes in `agent/runtime/state_ledger.py`
rather than as entries, so the guard never asserts something it did not check:
instance attributes (`ToolRegistry._context_override`,
`agent/tools/runtime.py:388`), import-time-only bindings never rebound or
mutated (`CONSOLE`, `_SYNC_TOOL_EXECUTOR_LOCK`, `_IN_PROCESS_LOCKS_GUARD`,
`_ATTACHMENT_BATCH_CAPACITY_LOCK`), and a function-local `ContextVar`
(`runtime_event_buffer`, `agent/channels/base.py:562`).

**Consequence for the rest of the plan:** Task 4 and Task 9 are the two that
must move process-scoped state, so they are the expensive ones; the turn-scoped
`ContextVar` group is a mechanical threading exercise (Tasks 6-8); and the five
interactive-CLI entries are expected to survive the refactor with their
justifications, not to converge.

---

## Background: what was actually measured

Three read-only investigations were run against the tree before this plan was
written. Only the findings that change the plan are recorded here.

**1. The combination step is procedural, not deficient in abstraction.**
`bootstrap._build_components_async` (`agent/bootstrap.py:424-1002`) constructs
everything in ~21 ordered steps and drops the result into a plain dict
(`:902-914`). There is no DI container; `RuntimeComponents.require`
(`agent/runtime/contracts.py:109-125`) is a name lookup with a type assertion.
The real container is that dict, and it is reachable from tools via
`registry.set_context("components", components)` (`agent/bootstrap.py:990`) —
read back at `agent/tools/builtin_tools.py:2675` and
`agent/tools/user_tools.py:613`.

**2. Load switches are scattered, and two subsystems are not in bootstrap at
all.** `memory` is unconditional (`:528-596`); `evolution` checks `enabled`
(`:839-844`); MCP checks list non-empty (`:704`, `:992`); `user_tools`'s
`enabled` only selects an approval mode (`:809-821`); `plugins` and `skills`
load then filter per-entry. **`SchedulerService` is built in
`agent/cli.py:1024-1033`, and channels in `agent/channels/base.py:719-754`** —
so "which profile is this" cannot be answered in one place today.

**3. Plugin teardown does not exist.** `AgentPlugin`
(`agent/plugins/catalog.py:46-102`) declares no `unregister`/`teardown`/
`dispose`/`close`. Unload is `reload()` → clear
`sys.modules["_agent_plugin_<name>"]` (`:1360-1361`) → full re-discovery
(`:1364`). The catalog documents its own leaks: a removed plugin's MCP stdio
subprocess cannot be disconnected, and `on_session_end` does not fire on
eviction (`:1328-1334`).

**4. Tools are already scoped — three incompatible ways.**
Process-wide (`agent/bootstrap.py:510`), per web session via
`fork(exclude_source_prefixes=("runtime:spawn",))` (`:212-222`,
`agent/tools/runtime.py:566-593`), and per sub-agent via a freshly built
registry filtered by capability (`agent/core/agent.py:4030-4077`).

**5. Plugins cannot register tools at all.** `agent/plugins/catalog.py` never
calls `register`; the only three `registry` references in the file are MCP
teardown during reload (`:1416`, `:1434`, `:1439-1440`). Tool providers are a
disjoint mechanism: `UserToolCatalog` (`agent/tools/runtime.py:823-881`) and
`MCPClient` (`:604+`).

**6. A durable event log already exists and is explicitly named.**
`ConversationTurn`'s docstring is "A durable event-log row for one plain-text
conversation message" (`agent/memory/models.py:61`); both tables share one
`seq` timeline; all three writers are INSERT-only
(`agent/memory/store.py:1106-1124`, `:1192-1200`, `:1836-1852`); and
`session_working_state` is a derived projection, not a source
(`agent/memory/context.py:1831`). Task 7 therefore wires the existing log to
listeners rather than introducing a second event model.

**7. State escapes the registration channel entirely.** 13 `ContextVar`
declarations and ~20 module-level mutable globals, including two `global`
rewrite paths: `shared._set_agent_home()` rewrites 18 path constants
(`agent/shared.py:101-133`), and `agent/__init__.py:_AgentModule._FORWARDED`
writes 60+ constants back into `shared` (`:311-376`). This is why Task 0 exists
first: an effect-based unload cannot be honest while registrations can be
bypassed.

---

## Scope decisions (read before starting)

These are the parts of the original proposal this plan deliberately does **not**
do, with the measured reason:

- **No `Capability` as a first-class abstraction.** `capabilities` is already a
  `frozenset[str]` tag set on `ToolDef` (`agent/tools/runtime.py:130-138`) that
  the executor checks; it is orthogonal to services and stays that way.
- **No second event taxonomy.** See finding 6.
- **No `ctx.events` domain vocabulary up front.** `_CC_EVENT_MAP`
  (`agent/plugins/catalog.py:188-230`) already declares ~29 event names of which
  exactly 6 can fire. Task 7 types only the emit sites that exist.
- **No per-turn dynamic tool registration.** `ContextAssembler.select_tools`
  documents that the tool block must be byte-stable for the life of the agent
  instance or the provider prefix cache is repriced
  (`agent/core/context_assembler.py:36-64`). Task 4 unifies three *instance*-level
  scopes; it does not add a per-turn one.

---

## Task 0: Freeze the state layer

The rest of the plan assumes "registration is an effect". That assumption is
false while a provider can mutate `shared.AGENT_HOME` or add a module-level
`ContextVar` and have it survive unload. This task adds no features; it makes
the later ones auditable.

**Files:**
- Create: `docs/superpowers/plans/2026-10-01-service-state-inventory.md`
- Create: `tests/test_state_inventory.py`
- Test: `tests/test_state_inventory.py`

- [x] **Step 1: Write the inventory**

For each of the 13 `ContextVar` declarations and each module-level mutable
global, record: **name · path:line · owner (module) · scope (process /
agent / turn / session) · convergence target (Task N or "stays") · reason if it
stays**. Seed rows, all verified:

| Symbol | Path:line | Scope | Target |
| --- | --- | --- | --- |
| `_active_sink` | `agent/core/output.py:1070` | turn | Task 7 (explicit param) |
| `_active_event_collector` | `agent/core/output.py:1112` | turn | Task 7 |
| `_active_assistant_text` | `agent/core/output.py:1119` | turn | Task 7 |
| `_active_tool_progress` | `agent/tools/executor.py:54` | turn | Task 6 |
| `_active_agent_context` | `agent/core/agent.py:88` | turn | Task 7 (stays as ctx) |
| `_active_orchestration_runs` | `agent/core/agent.py:91` | turn | Task 8 |
| `_active_schedule_target` | `agent/tools/runtime.py:26` | turn | Task 7 |
| `_active_signal_context` | `agent/tools/runtime.py:35` | turn | stays (scheduler) |
| `_active_run_self_report` | `agent/tools/runtime.py:62` | turn | stays (documented `copy_context` reason, `:39-56`) |
| `ToolRegistry._context_override` | `agent/tools/runtime.py:388-390` | call | Task 4 |
| `_active_cancel_token` | `agent/shared.py:399` | turn | Task 7 |
| `_active_session_id` | `agent/shared.py:407` | session | Task 4 |
| `runtime_event_buffer` | `agent/channels/base.py:562` | runner | Task 7 |

Module-level globals to record likewise: `shared.py:135-150`, `:256`, `:267`,
`:269`, `:510` (`CONSOLE`), `:421` (`_INSTANCE_ID`); `agent/__init__.py:123`
(`TASKS_DIR`) and `:311-376` (`_FORWARDED`); `config.py:1152-1153`
(`_system_prompt_cache_*`); `tools/runtime.py:79-80` (`_SYNC_TOOL_EXECUTOR`);
`tools/builtin_tools.py:309-317` (`_PLUGIN_INSTALL_LOCK`);
`tools/files.py:225`, `:1881` (`_IN_PROCESS_LOCKS`);
`security/shell.py:230-239` (7 names);
`security/plugin_approval.py:19-20`; `cli.py:86`, `:167`, `:1039`, `:1040`,
`:1142`; `channels/feishu.py:85`.

- [x] **Step 2: Write the guard test**

`tests/test_state_inventory.py` must fail when a module-level assignment appears
that is not in the inventory. Use `ast` over `agent/**.py`: collect module-level
`Assign`/`AnnAssign` targets whose value is a call, and module-level
`ContextVar(...)` calls, then assert the name is a key of the inventory dict
parsed from the markdown file. Allow an explicit `ALLOWED_NEW` inside the test
to be edited only together with the inventory (the review cost is the point).

- [x] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_state_inventory.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no   # must still be 19 failed
```

---

## Task 1: Generalize the one pattern that already works

`agent/exec/subprocess.py` is the only place where Service + `Protocol` +
replaceable implementation are all present: `SubprocessProvider`
(`:155`, one method), `LocalSubprocessProvider` (`:166`), `provider_from(registry)`
(`:306`) resolving registry context key `subprocess_provider` (`:300-312`).
Promote that shape into one resolver so the remaining tasks have a single
idiom.

**Files:**
- Create: `agent/runtime/services.py`
- Modify: `agent/exec/subprocess.py:300-312`
- Modify: `agent/tools/runtime.py:557-564` (`set_context`/`get_context` delegate)
- Test: `tests/test_service_registry.py`

- [x] **Step 1: Write the failing tests**

Cover: a registered provider is returned; an unregistered name raises a named
error rather than returning `None`; registering twice without `replace=True`
raises; a provider registered inside a disposable scope disappears when the
scope exits.

- [x] **Step 2: Implement the resolver**

`agent/runtime/services.py`, no imports from `agent.tools`, `agent.memory`, or
`agent.core`:

```python
class ServiceScope:
    """A disposable registration scope. Exiting it unwinds what it registered."""
    def __enter__(self) -> "ServiceScope": ...
    def __exit__(self, *exc: object) -> None: ...

class ServiceRegistry:
    def provide(self, name: str, value: Any, *, replace: bool = False) -> None: ...
    def get(self, name: str) -> Any: ...
    def scope(self) -> ServiceScope: ...
```

Unwinding is reverse-registration-order, and is the only mechanism Task 2 and
Task 8 may use.

- [x] **Step 3: Re-express `provider_from` on top of it**

`provider_from(registry)` keeps its signature and its
`subprocess_provider` key, so both call sites
(`agent/tools/builtin_tools.py:3339-3340`, `agent/tools/user_tool_runner.py:239-240`)
are untouched. The registry context dict becomes a delegating view so the other
~48 `get_context` readers keep working unchanged.

- [x] **Step 4: Verify**

```bash
.venv/bin/python -m pytest tests/test_service_registry.py tests/test_user_tools.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 2: Provider registration entry point

`build_transport` hardcodes two wire formats
(`agent/core/transport.py:1090-1102`) and `ModelClientFactory.from_config`
hardcodes two SDKs (`agent/config.py:417-441`). Everything around them is
already data: the model→provider table (`transport.py:1369-1394`), the
per-provider model id sets (`:1266-1291`), the client cache key (`:1437`), and
`PROVIDER_FIELDS` (`config.py:229-313`), whose docstring already says it is the
single description shared by validation, the settings form, and the CLI wizard.

`ModelTransport` is a 9-abstract-method ABC with 5 more hooks
(`transport.py:88-383`). Do **not** widen or split it in this task; register an
id, a client factory, and a transport factory, and leave the ABC shape alone.

**Files:**
- Modify: `agent/core/transport.py:1066-1102` (`build_transport`)
- Modify: `agent/config.py:352-463` (`ModelClientFactory`)
- Modify: `agent/bootstrap.py:717-732`
- Test: `tests/test_provider_registry.py`

- [x] **Step 1: Write the failing tests**

A provider registered under a new `api_format` is selectable by
`active_provider` without editing `build_transport`; a transport registered by
an extension replaces the built-in for that format; an unknown format still
raises, now naming the registered formats.

- [x] **Step 2: Implement registration**

```python
def register_provider(
    api_format: str,
    *,
    transport_factory: Callable[[Any, dict], ModelTransport],
    client_factory: Callable[[dict], Any],
) -> None: ...
```

Built-ins (`anthropic`, `openai`) register through the same call, so the
hardcoded branches are deleted rather than kept beside the table.

- [x] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_provider_registry.py tests/test_provider_settings.py tests/test_model_routing.py tests/test_provider_headers.py tests/test_subprocess_provider.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 3: `ctx.tools` — one service, three scopes unified

Three mechanisms currently express tool-set isolation (finding 4). The target is
one API that can express all three, keeping `register`'s existing signature and
`source` semantics (`agent/tools/runtime.py:398-444`) unchanged — `source` stays
as the underlying namespace, since `_DEFAULT_TOOL_CAPABILITIES` is keyed by
`(source, name)` (`:296-382`).

**Files:**
- Modify: `agent/tools/runtime.py:293-600` (`ToolRegistry`)
- Modify: `agent/bootstrap.py:212-222`, `:510-518`
- Modify: `agent/core/agent.py:4030-4077` (`_create_sub_registry`)
- Test: `tests/test_tool_scope.py`

- [x] **Step 1: Write the failing tests**

The same three isolation cases, expressed through one API: a process-wide
registry; a session view excluding a source prefix; a capability-filtered child
view that cannot spawn. Plus: disposing a view does not disturb the parent, and
the parent sees no child tools.

- [x] **Step 2: Implement views on top of `ServiceScope`**

`ctx.tools.view(...)` returns a child backed by `ServiceScope`. `fork()` and
`_create_sub_registry` become thin wrappers so existing call sites keep working
in this task; delete the wrappers in Task 5 once a plugin exercises the new
path.

- [x] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_tool_executor.py tests/test_builtin_tools.py tests/test_skill_tools.py tests/test_user_tool_isolation.py tests/test_orchestration.py tests/test_web_channel.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 4: Cut the `components` backdoor

Two readers reach the whole container through a tool registry
(`agent/tools/builtin_tools.py:2675`, `agent/tools/user_tools.py:613`), which
means no service boundary is enforceable on the tool side while that path
exists. Replacing it with declared injection is the point of the task; adding a
`ctx` object without removing the path would only add a layer.

**Files:**
- Modify: `agent/tools/builtin_tools.py:2675`
- Modify: `agent/tools/user_tools.py:613`
- Modify: `agent/bootstrap.py:989-991`
- Test: `tests/test_service_registry.py` (extend)

- [x] **Step 1: Write the failing test**

A test asserting `get_context("components")` returns nothing for any registered
tool context, so the backdoor cannot be reintroduced silently.

- [x] **Step 2: Replace both readers with declared dependencies**

Each reader names the services it needs; the bootstrap provides exactly those.
Remove the `set_context("components", ...)` line.

- [x] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_builtin_tools.py tests/test_user_tools.py tests/test_plugin_catalog.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 5: First real plugin, with unload as the acceptance test

This is the task that decides whether the plan works. Finding 3 says plugin
teardown does not exist; finding 5 says plugins cannot even register a tool. The
acceptance condition is therefore behavioural, not structural: **load a plugin
that contributes all five kinds of thing, unload it, and observe that all five
are gone.** Today that takes five bespoke code paths, and MCP is documented as
impossible.

**Files:**
- Create: `agent/runtime/plugin.py`
- Modify: `agent/plugins/catalog.py:46-102` (declare lifecycle), `:1315-1455`
  (reload), `:1328-1334` (delete the two documented leaks)
- Test: `tests/test_plugin_lifecycle.py`

- [x] **Step 1: Write the failing tests**

One plugin contributes: a tool, a slash command, a bundled skill, a hook, and an
MCP server. Assert that after unload: the tool name is absent from
`ctx.tools.schemas()`, the command name is absent from the router, the skill is
absent from the catalog, the hook no longer fires, and the MCP subprocess
handle is closed. Assert `on_session_end` **does** fire on eviction.

- [x] **Step 2: Give `AgentPlugin` a lifecycle**

Add a `setup(ctx)` / `teardown()` pair. Everything registered in `setup` goes
through a `ServiceScope`; `teardown` is the escape hatch for what a scope cannot
express (a subprocess handle). Default implementations must keep the existing
duck-typed hooks working, since plugins are loaded from user directories and
cannot be broken by this change.

- [x] **Step 3: Make reload a scope unwind**

`reload()` becomes: unwind the old scopes, then re-discover. Keep the
`sys.modules` clear (`:1360-1361`) — Python module-level state is real and
clearing it is correct; the scope handles everything registered *through* the
plugin.

- [x] **Step 4: Verify**

```bash
.venv/bin/python -m pytest tests/test_plugin_catalog.py tests/test_plugin_lifecycle.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 6: Executor emits events instead of holding the catalog

`RegularToolExecutor(self.registry, plugin_catalog=self.plugin_catalog)`
(`agent/core/agent.py:2741-2744`) is a hard dependency from the tool-execution
layer onto the plugin machinery, and it is where `fire_pre_tool`/`fire_post_tool`
are invoked (`agent/tools/executor.py:413-416`, `:537-540`). Converting this is
the smallest honest test of typed events, because the boundary is one class.

Note two behaviours to preserve exactly, both currently surprising:
`on_pre_tool` short-circuits on the first `block` and discards the rest
(`agent/plugins/catalog.py:1819-1849`), and `on_post_tool`'s return value is
already computed and then dropped at `executor.py:538`. Decide and record which
one the typed version keeps — do not silently "fix" either.

**Files:**
- Modify: `agent/tools/executor.py:407-541`
- Modify: `agent/core/agent.py:2741-2744`
- Modify: `agent/plugins/catalog.py:1822-1894`
- Test: `tests/test_tool_executor.py` (extend)

- [x] **Step 1: Write the failing test**

The executor observes a typed `ToolPreExecute` object and blocks on it, with no
`PluginCatalog` in scope.

- [x] **Step 2: Define the two event types and dispatch them**

`ToolPreExecute` and `ToolPostExecute` as dataclasses. `RuntimeEvent` stays as
it is (`agent/core/output.py:1075-1087`) for lifecycle facts.

- [x] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_tool_executor.py tests/test_plugin_catalog.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 7: Wire the existing durable log to listeners

Finding 6: the durable log exists, is append-only, and is explicitly named. This
task adds no event store. It routes the turn-scoped `EventCollector` and the
already-persisted `AgentRuntimeEvent` (`agent/memory/models.py:132-155`, written
via `ContextManager.record_runtime_event`, `agent/memory/context.py:1959-1985`)
to subscribers, replacing the constructor-parameter `event_hook` chain
(`agent/commands/coordinator.py:82`, `:89`; `agent/channels/base.py:599-609`,
`:657-663`) that is the only "bus" today.

**Files:**
- Modify: `agent/runtime/services.py` (add subscription to the resolver)
- Modify: `agent/core/output.py` (per-turn `EventCollector` wiring)
- Modify: `agent/channels/base.py:565-597` (`_record_runtime_event`)
- Test: `tests/test_runtime_contracts.py` (extend)

- [ ] **Step 1: Write the failing test**

A subscriber registered through a scope receives a turn-scoped event and stops
receiving after the scope exits.
- [ ] **Step 2: Implement, then delete the `event_hook` constructor plumbing**
- [ ] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_runtime_contracts.py tests/test_channel_layer.py tests/test_web_channel.py tests/test_usage_telemetry.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 8: Split `BaseAgent`

`agent/core/agent.py` is 4704 lines, 8 classes, ~109 methods on `BaseAgent`,
carrying four unrelated jobs. It is the largest single risk in the repository
and the plugin work does not shrink it. Order by extraction cost, cheapest
first:

1. **LLM retry / continuation** — `_with_llm_retry:2315`, `_is_llm_retryable:2182`,
   `_continue_truncated_response:1981`, `_recover_from_content_filter:2228`,
   `_format_agent_error:2078`, `_is_account_exhausted:2142`. No shared state.
2. **Token budget** — `_input_token_budget:1514`, `_estimate_input_tokens:1535`,
   `_payload_overhead:1462`, `_output_room:1497`, `_retrieval_token_budget:1413`.
   Pure functions over `ctx`.
3. **Tool-loop watchdog** — `_check_tool_loop_stuck:2858`,
   `_build_tool_loop_stuck_response:2598`, and the signature helpers
   `:2379-2573`.
4. **Orchestration** — `run_parallel_subtasks:1280`, `run_pipeline_subtasks:1296`,
   `run_rendezvous_subtasks:1325`, `_execute_agent:4217`. Note these live on
   `BaseAgent`, not in `agent/orchestration/`, which holds only pure functions
   (`agent/orchestration/runtime.py:242/:343/:530`) — so this is the expensive
   one and it goes last.

**Files:**
- Create: `agent/core/llm_retry.py`, `agent/core/token_budget.py`,
  `agent/core/tool_watchdog.py`, `agent/core/orchestration_driver.py`
- Modify: `agent/core/agent.py`
- Test: `tests/test_agent_decomposition.py` (extend)

- [ ] **Step 1: One extraction per commit, tests green between each**
- [ ] **Step 2: After each extraction, assert `agent/core/agent.py` shrank**

```bash
wc -l agent/core/agent.py
```

- [ ] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_agent_decomposition.py tests/test_agent_integration.py tests/test_orchestration.py tests/test_core_defect_regressions.py tests/test_llm_retry_policy.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 8 status: half done, half is an ordering constraint

**Channels — done.** `build_gateway_channels` now lives in `agent/bootstrap.py`,
the composition module, and `agent/channels/base.py` re-exports the old name so
`agent/__init__`, `cli` and four test modules are unaffected.  It was already the
single decision point (one function holding the `web` and `feishu` switches);
what changed is the layer it sits in, and that a test now prevents a second
construction site from appearing:

- `test_only_the_builder_constructs_a_channel` reads every module under `agent/`
  and fails on a `WebChannel(...)`/`FeishuChannel(...)`/`CliChannel(...)` call
  anywhere else.  Verified by introducing one in `cli.py`: it reported
  `agent/cli.py: WebChannel(...)`.
- `test_the_builder_itself_does_construct_channels` is the control that keeps
  the guard from being vacuous.

**Scheduler — not done, and it is an ordering constraint rather than a scattered
decision.** `SchedulerService` needs an `agent_executor` built on `AgentCore`,
which `_build_components_async` creates at `:964` — near the end.  So the
scheduler cannot be constructed where the rest of composition happens, and moving
it would mean either constructing `AgentCore` earlier (a real behaviour change to
startup ordering) or giving the scheduler a late-bound executor.

`test_the_scheduler_is_not_here_and_that_is_recorded` fails the day a
`build_scheduler_service` entry point appears, so this note cannot go stale
silently.  Closing Task 8 as "channels done, scheduler constrained" is a
decision for the plan's owner; the evidence for it is now in the suite.

---

## Task 10, first slice: the store construction seam (done)

The plan said Task 10 needs a store interface first.  Measuring the *construction*
side rather than the interface side changed the picture: this was the same debt
Task 4 removed for the component container, one layer down.

**`SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)` was written out in
twenty-two places** -- twenty in `agent/channels/web.py`, one in `agent/cli.py`,
one in `agent/tools/builtin_tools.py` -- and every one resolved the same
module-level global, which `_set_agent_home` rewrites at runtime.  Twenty-two
readers of one mutable global means the database a caller talks to is a property
of the *process*, not of the caller, and there is no seam at which a test or
another composition can point elsewhere.

That is now `agent/scheduler/open_store(db_path=None)`, which is also the first
store interface Task 10 needs: an explicit path parameter, and exactly one place
that names the concrete class.  The guard is source-level, because a
construction in a branch the tests do not exercise is precisely the case to
catch:

- `test_only_the_scheduler_package_constructs_the_store` — fails on any
  `SchedulerStore(...)` outside `agent/scheduler`.  Verified by reintroducing one
  in `builtin_tools.py`; it reported the module.
- `test_no_module_resolves_the_scheduler_path_directly` — fails on a
  `shared.SCHEDULER_DB_FILE` read outside the package, with one narrow exemption:
  a tool that *reports* which database it used (a `"db_path": str(...)` display
  value) is telling the user where their data went, not driving scheduler
  behaviour.  The exemption is a regex on that shape so it cannot absorb a
  construction.

**Still open, and unchanged by this:** `SchedulerService._store_call` tests
`type(self.store) is SchedulerStore` to decide thread affinity.  See the note
below — that is a different question (what the scheduler may *assume* about a
store) from where the store *comes from*, which is what this slice settled.

## Task 10, second slice: the Ralph task store seam

The same shape as the scheduler store, one layer down and much smaller:
`RalphTaskStore` was constructed in three modules, and two of them were
convenience wrappers in `agent/__init__.py` that each built a throwaway instance
to save or load a single task, reading a module-level `TASKS_DIR` that
`_set_agent_home` rewrites.

Three sites is a tidy-up, not a defect -- but the *shape* is what matters for
Task 10, so it is recorded the same way: a plugin cannot replace a store it does
not construct, and a caller cannot point elsewhere while the path comes from a
global.

`agent.ralph.open_task_store(tasks_dir=None)` is now the only construction site,
with the same guard pattern as the scheduler's (source-level, verified by
reintroducing a construction in `bootstrap.py`).

**One design point, decided explicitly rather than by accident:** the wrappers
pass `TASKS_DIR` in rather than letting the factory resolve its own default.
`agent.TASKS_DIR` is the *live* value `_set_agent_home` repoints, and the existing
wrapper test monkeypatches it; a factory reading its own global would silently
ignore that and break a contract the tests already rely on.  The factory's own
default (the shared scheduler directory) is still exercised, for callers that
have no opinion.

## Task 10: ralph and scheduler as plugins — the boundary, measured

Task 10 needs a store interface before either service can become a plugin.  The
measurement below was taken so the next attempt does not have to re-derive it.

| Service | Methods | Store surface it actually uses |
| --- | --- | --- |
| `RalphService` | 9 | `list_tasks`, `load`, `save` — three methods, no node types |
| `SchedulerService` | 26 | `db_path`, plus it **constructs `SchedulerStore` itself** |

`RalphService` is the easy half and could be given a `Protocol` today.

**`SchedulerService` contains the real decision**, at
`agent/scheduler/runtime.py:82`:

```python
async def _store_call(self, method_name, *args, **kwargs):
    if type(self.store) is not SchedulerStore:          # concrete-class test
        ...  asyncio.to_thread(getattr(self.store, method_name), ...)
    else:
        def invoke():
            thread_store = SchedulerStore(db_path=self.store.db_path)  # built here
            ...
```

This is not scattered state; it is one trade-off expressed as a type test.  A
SQLite connection cannot be used from another thread, so when the store *is* a
`SchedulerStore` every call gets a thread-owned connection, and any other store is
simply called in a worker thread.  A `Protocol` therefore has to answer a
question the code currently answers by identity:

> **Does a non-SQLite store own its own thread-safety, or must the scheduler
> provide it?**

Getting that wrong does not fail loudly -- it removes the connection isolation
that the `type(...) is` branch exists to provide, and the symptom would be
intermittent SQLite errors under concurrent runs.

**Both halves or neither.**  Giving `RalphService` a `Protocol` while
`SchedulerService` keeps testing its store by concrete class would leave Task 10
looking done and behaving half-done, which is worse than leaving it visibly
outstanding.

**What would settle it.**  Either (a) the scheduler owns threading for every
store, in which case the `Protocol` needs a `db_path`-like handle or a
`thread_local()` method, or (b) stores declare their own thread-affinity, in
which case the `Protocol` needs something like `is_thread_affine` and
`SchedulerStore` returns True.  (b) is closer to what the code does today and
keeps the SQLite knowledge inside the SQLite store; (a) is closer to what a plugin
author would expect.  That is the decision, and it is the plan owner's.

---

## Task 9: One place decides what loads

Finding 2: schedulers and channels are built outside bootstrap, and the other
switches are in four different shapes. Until this is one function, "profile"
cannot mean anything, so this task is the prerequisite for any profile work and
does not itself introduce profiles.

This task also owns the six session-keyed shell authorisation tables
(`agent/security/shell.py:231-240`: allowlist, pending tokens, pending session
scope, auto-approve, permission levels, sandbox modes). They are session state
held in module-level dicts, so per-session isolation cannot be honest until
they become a service; the state ledger assigns them here rather than to a task
of their own, because they have to move the same time as the layout does.

**Files:**
- Modify: `agent/bootstrap.py:424-1002`
- Modify: `agent/cli.py:1024-1033`, `:1539-1559`
- Modify: `agent/channels/base.py:719-754`
- Modify: `agent/security/shell.py:231-240` (six session tables → one service)
- Test: `tests/test_agent_integration.py` (extend)

- [ ] **Step 1: Write a failing test** asserting a single call returns the full
  component set including scheduler and channels for a given config.
- [ ] **Step 2: Move both constructions behind that call**; leave
  `agent/scheduler/profiles.py:75` (`PERMISSION_PROFILES`) as the existing
  precedent for config-override tables and note it in the plan for the eventual
  profile work.
- [ ] **Step 3: Verify**

```bash
.venv/bin/python -m pytest tests/test_agent_integration.py tests/test_channel_layer.py tests/test_scheduler.py -q
.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no
```

---

## Task 10 (deferred): ralph and scheduler as plugins

Not scheduled. `RalphService` is bound to `RalphTaskStore` and `RalphVerifier`
(`agent/bootstrap.py:952-962`) and `SchedulerService` to `SchedulerStore` plus a
SQLite/disk layout (`agent/cli.py:1024-1033`), so both need a store interface
extracted first. Recorded here so that the ordering is explicit rather than
implied: by the measured evidence these are the last two, not peers of the
others.

---

## Final verification

- [ ] `git diff --check`
- [ ] `.venv/bin/python -m pytest -q -p no:cacheprovider --tb=no` → FAILED set
      identical to `2026-10-01-baseline-failures.txt`; passed count increased by
      exactly the tests added
- [ ] Every row of the Task 0 inventory is either converged or carries a written
      reason
- [ ] `grep -rn "get_context(\"components\")" agent/` returns nothing
- [ ] `wc -l agent/core/agent.py` is materially below 4704
