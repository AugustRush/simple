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

**Tasks 0 and 1 implemented 2026-10-01.** Tasks 2-10 not started.
Branch: `refactor/service-kernel`.

| | Baseline | After Task 0 | After Task 1 |
| --- | --- | --- | --- |
| Full suite | 19 failed / 2641 passed | 19 failed / 2649 passed | **3 failed / 2687 passed** / 1 skipped |
| FAILED set | see baseline file | byte-identical | byte-identical (see note) |
| Module-level mutable state | undocumented | **49 entries**, guard-tested | unchanged |
| Replaceable services | 1 hand-rolled (`exec/`) | 1 | **general mechanism + scoped, reversible replacement** |

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
(`tests/test_service_registry.py`). The four files Task 1 names
(`test_service_registry`, `test_user_tools`, `test_subprocess_provider`,
`test_user_tool_isolation`) run 77 passed with no failures.

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

- [ ] **Step 1: Write the failing tests**

A provider registered under a new `api_format` is selectable by
`active_provider` without editing `build_transport`; a transport registered by
an extension replaces the built-in for that format; an unknown format still
raises, now naming the registered formats.

- [ ] **Step 2: Implement registration**

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

- [ ] **Step 3: Verify**

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

- [ ] **Step 1: Write the failing tests**

The same three isolation cases, expressed through one API: a process-wide
registry; a session view excluding a source prefix; a capability-filtered child
view that cannot spawn. Plus: disposing a view does not disturb the parent, and
the parent sees no child tools.

- [ ] **Step 2: Implement views on top of `ServiceScope`**

`ctx.tools.view(...)` returns a child backed by `ServiceScope`. `fork()` and
`_create_sub_registry` become thin wrappers so existing call sites keep working
in this task; delete the wrappers in Task 5 once a plugin exercises the new
path.

- [ ] **Step 3: Verify**

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

- [ ] **Step 1: Write the failing test**

A test asserting `get_context("components")` returns nothing for any registered
tool context, so the backdoor cannot be reintroduced silently.

- [ ] **Step 2: Replace both readers with declared dependencies**

Each reader names the services it needs; the bootstrap provides exactly those.
Remove the `set_context("components", ...)` line.

- [ ] **Step 3: Verify**

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

- [ ] **Step 1: Write the failing tests**

One plugin contributes: a tool, a slash command, a bundled skill, a hook, and an
MCP server. Assert that after unload: the tool name is absent from
`ctx.tools.schemas()`, the command name is absent from the router, the skill is
absent from the catalog, the hook no longer fires, and the MCP subprocess
handle is closed. Assert `on_session_end` **does** fire on eviction.

- [ ] **Step 2: Give `AgentPlugin` a lifecycle**

Add a `setup(ctx)` / `teardown()` pair. Everything registered in `setup` goes
through a `ServiceScope`; `teardown` is the escape hatch for what a scope cannot
express (a subprocess handle). Default implementations must keep the existing
duck-typed hooks working, since plugins are loaded from user directories and
cannot be broken by this change.

- [ ] **Step 3: Make reload a scope unwind**

`reload()` becomes: unwind the old scopes, then re-discover. Keep the
`sys.modules` clear (`:1360-1361`) — Python module-level state is real and
clearing it is correct; the scope handles everything registered *through* the
plugin.

- [ ] **Step 4: Verify**

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

- [ ] **Step 1: Write the failing test**

The executor observes a typed `ToolPreExecute` object and blocks on it, with no
`PluginCatalog` in scope.

- [ ] **Step 2: Define the two event types and dispatch them**

`ToolPreExecute` and `ToolPostExecute` as dataclasses. `RuntimeEvent` stays as
it is (`agent/core/output.py:1075-1087`) for lifecycle facts.

- [ ] **Step 3: Verify**

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

## Task 9: One place decides what loads

Finding 2: schedulers and channels are built outside bootstrap, and the other
switches are in four different shapes. Until this is one function, "profile"
cannot mean anything, so this task is the prerequisite for any profile work and
does not itself introduce profiles.

**Files:**
- Modify: `agent/bootstrap.py:424-1002`
- Modify: `agent/cli.py:1024-1033`, `:1539-1559`
- Modify: `agent/channels/base.py:719-754`
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
