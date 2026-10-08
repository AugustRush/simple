# Plugin Kernel rewrite — status and measured blockers

The architecture this follows is the one-picture version: **the core only composes
plugins** (Context / Plugin / Service / Event / Scope / Lifecycle), and everything
else — agent, model, tools, session, memory, scheduler, channels, Ralph,
orchestration — is a plugin. Capability is downgraded to "what a Service exposes",
not a universal abstraction.

Compatibility with the old interfaces was explicitly not required, so where a
rename or a signature change was the right shape it was made and the tests were
migrated in the same commit. Each such change is called out below, because "I
changed the interface on purpose" and "I broke it" have to stay distinguishable.

## Where each phase stands

| Phase | State | Evidence |
|---|---|---|
| 0 Baseline | done | 3 accepted failures, byte-identical across every commit; see `2026-10-01-baseline-failures.txt` |
| 1 Plugin Kernel | **done** | `agent/kernel/` — Context / Plugin / Service / Event / Scope / Lifecycle, 61 tests in `tests/test_kernel.py`; the boundary itself is asserted in `tests/test_kernel_boundary.py` |
| 2 Service Injection | **done** | ToolRegistry ✅ `registry`, Session ✅ `context_manager`, Model ✅ `llm` |
| 3 Agent Loop Plugin | **done** -- and `agent` itself is now a plugin | `agent_loop` is a plugin with declared `inject`; the loop **body** is in `agent/core/turn_loop.py`. `BaseAgent` keeps the facades tests reach |
| 4 Capability Layer | **done** | tools / commands / sub-agent roles are each a service with `scoped(ctx)`; `PluginAssets` -- a contribution service no plugin called -- was deleted |
| -- Orchestration | **not a plugin: per-call state, pinned** | policy comes from `ctx.metadata["skill_catalog"]` per turn (four writers); a composition can name a plugin's policy via `orchestration.policy_skill_id` |
| 5 Execution Plugins | **2 of 4 done, 2 have no separable boundary** | Ralph ✅ and Scheduler ✅ mounted as plugins; Orchestration / Workflow measured, see below |
| -- Agent | **done** | `agent/core/agent_plugin.py` provides `agent`; the class is an argument, so `bootstrap.BaseAgent` is still the substitution point |
| -- Memory | **done** | `agent/memory/plugin.py` provides `ltm_store` and `memory`; the ordering chain that blocked it is gone |
| -- Session log | **done as a plugin** | `agent/memory/session_plugin.py` provides `context_manager`, which `bootstrap` used to assemble above the catalog |
| 6 Channel Plugins | **web + feishu done; CLI is not a channel** | `channels` is a capability service any plugin can contribute to; `CliChannel` is a stub that raises |
| 7 Profile / Bundle / patch | **done** | bundles were already patches; a profile's effect is now `config_patches`, applied by the same `apply_patch`, and validated against `DEFAULT_CONFIG` like a bundle's |

Commits, oldest first:

```
9b5beda  Phase 1  Plugin Kernel
1c052c0  Phase 2a catalog's host adopts the runtime components
d680eb3  Phase 2b Install.ctx / ctx.bus / ctx.on_unwind
6342e02  Phase 2c scoped service view
56cb051  Phase 2d PluginAssets service
491da1d  Phase 2e declarative loaders return their undos
955a51f  Phase 2f catalog delegates its lifecycle to the kernel host
24e8de1  Phase 3a the agent loop is a plugin (agent_loop)
f128e88  status doc: the measured blockers
<this>   Phase 2g the model adapter is a service (llm)
```

## What the kernel is, and what it deliberately is not

`agent/kernel/` is six concepts and no privileged participants. There is no
`Agent`, `Model`, `Tool` or `Session` type in it, because the kernel would have to
know their names to privilege them and not knowing them is the point.

- **Registrations are effects.** Everything a plugin registers goes on its
  `ServiceScope`; disposing the scope *is* unloading. There is no per-kind teardown
  to keep in sync, so "who unregisters this?" cannot be asked.
- **Composition is a plugin loading a plugin.** `ctx.plugin(child)` records the
  child's install on the parent's scope, so the cascade is a consequence of the
  disposal rule rather than a feature layered on top of it.
- **Dependencies wait rather than fail.** A plugin whose `inject` is unmet is
  pending and holds *no* registrations — there is no half-loaded state.
- **A domain is a set of event types, kept outside the kernel.** The kernel cannot
  offer `ctx.on_domain("turn", …)` because it would have to know what a turn is;
  `Domain.attach(ctx, listener)` is the call, and the domain table owns the answer
  to "which bus" (there are three, one per domain).

## The Model adapter is a service (`llm`)

Two obstacles were measured, an attempt was reverted, and the third attempt
resolved both.  What changed between them is worth recording, because the first
attempt failed for a reason that looked mechanical and was not.

**Obstacle 1 — the mapping did not exist when the agent was built.** `bootstrap`
constructed the global `BaseAgent` around line 800 and assembled the `components`
mapping around line 973; the session root had the same shape.  The first attempt
tried to hand the mapping over afterwards, which needs a post-hoc bind — a second
way for a participant to receive its composition.

The fix was to **create the mapping first and populate it as the rest is built**,
which is what a composition root is for.  Every value the mapping needed already
existed before the agent; the two that did not (`evolution`, `user_tools_enabled`)
are added to the same dict in place, at the point they are built.  So this was a
*move of one dict literal*, not a reordering of the root's logic — which is why it
was safe, and why the earlier attempt was not.

**Obstacle 2 — the adapter was assigned directly in 8+ test sites.** The first
attempt made `_transport` a property resolving the container on every access, which
removes the setter and broke those sites (17 failures).

The fix was to resolve **once, at construction**, and keep `_transport` a plain
attribute.  Replacing the adapter *after* an agent is built is not something a
composition does — it builds both — so a per-call lookup was resolution on the hot
path of every request for a value that cannot change.  Composition-time resolution
is enough for the plan's requirement, and it left the 8 test sites alone.

That is the general lesson: "the container wins, resolved on every access" is not
the same requirement as "the container decides", and only the second was needed.

Measured fallout of the *first* attempt: 24 failures from `components` being
undefined at the construction site, 17 from the missing setter.  The second attempt
had none.

## The loop body moved, and how it was verified

`_run_turn_loop` was the last and largest piece: 422 lines, 124 statements, 33 names
from outside itself. It is now `agent/core/turn_loop.py: run_turn_loop(deps, agent,
ctx, ...)`, and `send_message` calls it.

Three properties made the move safe rather than lucky:

**The dependency surface was already declared and guarded.** By the time the body
moved, every one of the nine `self.X` reads it still had mapped onto a `LoopDeps`
field or a free function. The struct had been built and checked over the preceding
rounds precisely so this step would be mechanical.

**The verification was statement-by-statement, not "the tests pass".** All 124
statements were compared to the original after normalising the intended renames, and
**exactly two differ**: the signature, and `deps = LoopDeps.of(self)` -- removed
because the function now receives `deps`. The `try`/`finally` block came across
byte-identical, which is the part that matters: it restores three contextvars and
clears the turn's markers, and an earlier extraction in this session lost a line from
a similar block without any test noticing.

**The check that made it safe is now a test.** Three assertions: no delegating copy
on `BaseAgent`; `send_message` actually calls the moved function (a body left behind
is invisible from the method list -- the name is simply absent and the loop is dead
code); and the moved function contains the `try`, so the teardown cannot be left in
the caller.

### What is now where

```
agent/core/turn_loop.py    1,590 lines  the loop, its helpers, LoopDeps
agent/core/agent.py        2,530 lines  BaseAgent: construction, facades, tools,
                                        sub-agents, the streaming entry point
```

`BaseAgent` shrank from 3,920 to 2,530 lines. What remains is not the loop: it is
the agent's identity and the facades the suite reaches — five of them, each one
statement, each asserted to be one statement.

### A measurement bug worth remembering

The first version of that scan printed only the attributes starting with `_` and
labelled an empty set "(nothing)". It reported `_inject_pending_interjections` as
having no coupling; the function in fact ends with `self.registry.set_context(...)`.

The output could not distinguish "no dependencies" from "dependencies exist but none
were shown" — and the plan was briefly to extract a function on the strength of it.
Any scan used to justify a refactor has to print the whole set it is testing for,
or it is a validation that cannot fail.

### A second false positive, from naming a service `llm`

Naming the model adapter `llm` made the scanner report
`agent/core/event_domains.py` as a *writer* of it, because the domain table keeps
`{"turn": TURN_EVENTS, "llm": LLM_EVENTS}` and the rule was "every dict literal is a
component mapping". It is a domain-to-bus lookup table.

Excluded by the shape of its values — all `*_EVENTS` bus references, which a
component mapping never has. The alternative was to list `event_domains.py` as an
owner of the model adapter, which would have made the ledger lie about the code,
and a ledger that lies is worse than one that omits: it tells the next reader that
the domain table owns provider dispatch.

## Phase 5, measured: two of the four are not separable

The plan lists Workflow / Ralph / Orchestration / Scheduler as execution plugins.  Measured
one at a time, they are four different things:

| Piece | What it is now | Verdict |
|---|---|---|
| **Ralph** | a service `commands/builtin.py` already resolved, built **inline and unconditionally** by `bootstrap` | **was a plugin waiting to happen** -- now `agent/ralph/plugin.py`, `inject` names all seven dependencies, a composition without it has no `ralph_service` |
| **Scheduler** | a service built by `_build_scheduler_service` in `cli.py`, parameters from `cfg["scheduler"]` **and CLI flags** (`poll_seconds or sched_cfg.get(...)`) | separable, but its parameters have two sources -- config and argv -- so a plugin needs the *effective* values at setup, not the ones on disk |
| **Orchestration** | a **function library plus a planner**: `run_parallel_subtasks`, `run_pipeline_subtasks`, `run_rendezvous_round`, `validate_subtask_specs`, `OrchestrationPlanner`, and a `contracts` type module, all imported at module scope by `agent/core/agent.py` | **not a component**: it is the loop's own implementation. A plugin boundary here means the loop resolving its planner and runners through the container -- ~30 call sites in `agent.py` alone |
| **Workflow** | a data model (`Workflow`, `WorkflowStep`, `validate_workflow_graph`) plus persistence **on the scheduler store** (`store.list_workflows()`), read by a tool and by the web channel | **not a component**: splitting it out means splitting the scheduler store, which invents a boundary where none exists |

Two consequences worth stating plainly.

**Ralph was the only one where being a plugin changes what a composition can be.** It was
unconditional, so making it a plugin removed something that was always there. The Scheduler
is already conditional -- the interactive path starts it only if configured -- so the same
move there is an identity change rather than a capability change.

**Orchestration and Workflow are part of the agent loop and the scheduler respectively, not
peers of them.** Forcing the plugin shape would produce a plugin whose only member is the
thing it was carved out of, and the carvature would be visible in every caller. That is the
plan's table reading as a taxonomy where the code has a composition; the taxonomy should
give way.

## The kernel calls `setup` synchronously, and an `async def setup` is now refused

Found while starting the Scheduler, whose builder (`_build_scheduler_service`) is
`async`.  The host called `setup(ctx)` without awaiting or checking, so a plugin
author writing `async def setup` got:

    loaded       : True
    provided     : None
    in installs  : True
    ...and one RuntimeWarning, which most deployments suppress

A plugin that mounts, contributes nothing and reports no error is the worst shape this
can take -- there is no symptom to search for.  It is now refused with a message naming
the plugin, and the refusal goes through the **same disposal path as a raising setup**,
so the two cannot leave different states.

Refused rather than supported: awaiting in the host would make `load` async, which
changes every caller including the composition root.  When the Scheduler becomes a
plugin it will either make its builder synchronous or start a task from a synchronous
setup.

## The scheduler's builder moved out of the CLI

`_build_scheduler_service` was 294 lines in `cli.py`.  Measured by statement, it is
**six statements of construction** (a store, a delivery, the service, the return) and
**107 of the two per-run executors**.  That ratio is the reason it is a module rather
than a plugin: what a composition cares about is the six, and the rest is what a run
*does*, which is not a composition-time concern at all.

It is now `agent/scheduler/build.py`.  Two helpers moved first, because the moved code
needs them and a scheduler module importing the CLI would be a cycle:

- `scheduler_store()` -> `agent/scheduler/store.py`
- `agent_core_for_components()` -> `agent/runtime/contracts.py`

The remaining composition-root operations (`_build_components_async`,
`_close_components`, `load_config_for_run`) are reached through a **function-local
`import agent`** inside the executor -- the same cycle-break used for the turn loop.

The move was checked by statement, not by "the tests pass": **187 -> 187**, with exactly
two differences -- the signature (`components` became required last round) and the added
local import.  The two describing helpers came across at 19 -> 19 and 56 -> 56 with zero
differences.

## Why the Scheduler cannot become a plugin the way Ralph did

Three measurements, one of them from a change I made and then reverted.

**The store is injected, not owned.**  The obvious move was "the service closes the
store it was built with", which removes a `close()` from two `finally` blocks.  It was
wrong, and the suite said so:

    await service.shutdown()
    runs = store.list_runs(task.id)      # sqlite3.ProgrammingError: closed database

A caller may legitimately read the store after the service stops -- the tests do, and so
would a command reporting what a run recorded.  The service was *given* the store; two
call sites closing two stores they each created is not duplicated ownership, it is two
owners.  Reverted.

**The run executor needs the whole mapping, and a context resolves names.**  A run may
build a fresh runtime (`_build_components_async(run_cfg, ...)`) when the config changed
on disk, and otherwise uses the composition mapping as-is.  A plugin's `ctx` resolves
individual services, not the mapping.  So the plugin is blocked on a choice: a mapping
accessor in the kernel -- the escape hatch the plan argues against -- or a
**runtime-factory service** that answers "build me a runtime for this config".  The
second is the shape the plan wants; it does not exist yet.

**`shutdown()` is async and `dispose()` is sync.**  So a plugin could register
`store.close` on its scope but not `await service.shutdown()`, splitting teardown
between the plugin and the caller -- which is what the current code already does, only
with the store added.

Conclusion: the Scheduler needed a runtime-factory service before it could be a plugin.
That now exists -- `agent/runtime/factory.py`, provided as `runtime_factory`, and
`agent/scheduler/build.py` builds and closes a run's runtime through it rather than
importing the composition root.  The mapping branch remains: a run with no durable ids
still uses the composition's own mapping, which is the historical behaviour for
lightweight executors.

## The Scheduler is a plugin

`agent/scheduler/plugin.py`.  The gateway mounts it instead of building the service
inline, so a gateway composition can now be one without a scheduler.

Three things had to be settled first, and each was its own piece of work: the run
parameters resolved in one place (`agent/scheduler/settings.py`), a synchronous builder
in its own module (`agent/scheduler/build.py`), and a runtime-factory service so a run
can build a runtime without importing the composition root (`agent/runtime/factory.py`).

**The store belongs to the plugin, and the plugin is not the service.**  An earlier
attempt made `SchedulerService.shutdown` close the store and the suite overturned it: the
service is *given* the store, so a caller may read it after the service stops.  The
plugin *opens* it, so the plugin closes it -- registered on its own scope at the moment
of creation, which is how every other registration in this kernel works and why this
half can be scope-owned while the service's async `shutdown` cannot.

**One thing the wiring had to fix:** the builder used to call `scheduler_store()` itself.
With the plugin opening one too, there would have been two connections to one file and
the scope's `close` would have reached the one nothing used.  The builder now takes the
store it was given, and the plugin asserts that it does.

**One path is still not a plugin:** the `scheduler` command builds its service directly.
It runs nothing but the scheduler, so the "a composition can omit it" gain does not apply
there, but the two paths now differ and that is worth knowing.

## Phase 6, measured: one of the three is not a channel

| Piece | What it is now | Verdict |
|---|---|---|
| **Web** | a branch in `build_gateway_channels`: read `channels.web.enabled`, import, filter the config against the dataclass, construct | now `agent/channels/plugin.py`, registering on the `channels` service |
| **Feishu** | the same, second branch | now the same plugin, registering the same way |
| **CLI** | **not a channel.** `CliChannel` is a stub whose `start` raises `NotImplementedError` ("not yet wired into ChannelRunner"); the interactive path is `_interactive_loop` | out of scope: an entry point, not a transport |

The gap that mattered was not the line count of two branches.  **A plugin could contribute
a tool, a command, a sub-agent role, an MCP config and a skill root -- and could not
contribute a channel**, because the gateway's set was a branch per built-in in the
composition root.  A third party's transport had to be added there.

`channels` is now a capability service like the others: `ChannelRegistry` with
`scoped(ctx)`, provided by the composition, and web and feishu are its first users rather
than special cases in it.  `tests/test_channel_composition.py` proves the point with a
third plugin that has no branch anywhere.

**The switch stayed in the config.**  Mounting a channel plugin only when its section says
`enabled` would have put the decision back in `build_gateway_channels`; the plugins are
mounted unconditionally and contribute nothing when off.

**One guard moved rather than being relaxed.**  `test_only_the_builder_constructs_a_channel`
asserted that only `agent/bootstrap.py` calls `WebChannel(...)`.  The composition root no
longer constructs a channel at all, so the single module is now
`agent/channels/plugin.py` -- still one module, still asserted, plus a second assertion
that the composition actually mounts the plugins it reads.

## Phase 7: patch semantics were two mechanisms, and now they are one

`agent/config_patch.py` holds `PatchEntry` and `apply_patch` -- a dotted path, a value,
and a deep copy so the input is untouched.  Measured against it:

| Piece | Was | Now |
|---|---|---|
| **Bundle** | already patches (`Bundle.patches`, `apply_bundle`) | unchanged |
| **Profile** | four hand-written assignments in `apply_profile_to_config`: copy a section, copy a sub-section, assign two keys, copy another section, assign two more | `PermissionProfile.config_patches` -- the same four as data, applied by `apply_patch` |

They were the same concept in two forms, and that had a cost beyond taste: bundles have
`test_every_patch_names_a_declared_path`, which exists because **two invented paths
slipped through a first draft** -- `channels.web.enabled` and
`orchestration.sub_agent_retries`, neither of which exists, and a guessed path is
invisible because assigning to a key that was never there succeeds.  Profiles had no such
check, because a hand-written assignment is not a path to check.

Now they do, and it is the same check.  `apply_patch`'s deep copy is also strictly
stronger than what the profile path promised: it copies sections the profile does not
touch, where the hand-written version copied only the two it did.

`allow_new=True`, for the reason bundles use it: the scheduler's run path can be handed a
last-known-good config from anywhere, and refusing an absent section there would drop the
posture exactly when the config was unreadable.

## There are two plugin protocols, and the one real plugin uses the old one

Measured while working out how to make `memory` a plugin, and it is the larger finding.

| Protocol | Shape | Who uses it |
|---|---|---|
| **Kernel** (`agent/kernel/`) | `name` / `inject` / `setup(ctx)` | Ralph, Scheduler, the channel plugins, everything Phase 2-6 produced |
| **Catalog hooks** (`AgentPlugin`) | `on_session_start(components)`, `on_turn_end(event)`, 16 names in all | **`EvolutionPlugin` -- the only real plugin in the tree** |

The ratio is the finding: **16 hook names, 9 dispatch sites, and three implementations in
production** -- `on_session_start`, `on_turn_end`, `on_session_end`, all three in
`evolution`.  Nine more live in tests as doubles for the protocol itself.

And `agent/plugins/catalog.py` already records the problem in the `AgentPlugin` docstring:

    **Open question, recorded here because this is where it matters:** a plugin is
    handed the whole mapping, which means it can reach any ...

That is the same coupling the kernel was built to remove, in the module that loads
plugins.  It is the reason `memory` cannot simply become a plugin either: a hook plugin
is handed a mapping, so the question "which services does it need" has never been asked.

**Only one of the three had a kernel counterpart, and that was the finding.**  `on_session_start(components)` is `setup(ctx)`, and `on_session_end(event)` is
a subscription.  `on_turn_end(event)` needed the user's input against the agent's answer --
the hook's `TurnEvent` carries `user_input`, `agent_response`, `tool_calls` -- and the
kernel's turn domain had **only** `TurnFinished(agent_id, status, steps)`: an *outcome*,
for a log.  Three fields, and none of them is text.

So the migration was blocked on **vocabulary, not mechanism**: Phase 1 built event
subscription, and the turn domain did not have a type a real plugin could use.  That is
now `TurnCompleted(agent_id, user_input, response, tool_calls)`, emitted on the turn bus.
Each field has a consumer -- `EvolutionPlugin` reads exactly those four -- and it is
declared in `TURN.event_types`, so `TURN.attach(ctx, ...)` delivers it.

The ordering is the whole correctness of `user_input`: it is read from the turn's request
in the context metadata, which is cleared two statements later so nothing can claim to be
a request that has already been answered.  Emit after that clear and **every turn reports
an empty prompt, silently** -- no error, no missing field, just a plugin that never learns.
The order is asserted by line number rather than left to the comment beside it.

`on_session_end` was missing for the same reason: `EvolutionPlugin` scores a session by
reading its transcript and the tools used, and the domain table covered a **turn** --
tools, the loop, the model.  A turn finishes many times per session; a session finishes
once, and nothing announced it.  That is now `SessionFinished` on a fourth domain,
`session`, emitted from `PluginCatalog.fire_session_end` -- the single place both
protocols are notified, so the two callers of that method (`cli` and the channel layer)
cannot drift into one emitting and one not.

`test_the_known_domains_are_exposed` asserts the domain set as a closed list, and it
failed when `session` was added -- which is the intended cost, and how a fourth domain
announces itself.

`EvolutionPlugin` also turned out to hold the component mapping in `self._components` and
**never read it**: assigned twice, read zero times.  Its need for the whole composition
was conventional, not real.

The declarative `hooks_config` (external commands from `hooks.json`) is a *different*
mechanism and stays.

So the capstone is: migrate `EvolutionPlugin` to the kernel protocol, then delete the
hook protocol -- 16 names and 9 dispatch sites with no remaining user.  After that there
is one plugin protocol and no plugin receives a component mapping.

**The migration is done for the plugin** (`agent/_builtin/plugins/evolution/__init__.py`):
`setup(ctx)` with `inject = ("evolution",)`, `TURN.attach` and `SESSION.attach`.  It needed
two pieces of vocabulary that did not exist -- `TurnCompleted` and a `session` domain --
and finding that was most of the work.

It also removed a **silent bug**.  The old handler read `components.get("evolution")`,
found `None` (which meant *disabled*), and **built an engine anyway** from the agent, the
model and the memory.  `evolution.enabled: false` was defeated by the plugin supposed to
honour it.  The fallback existed because `on_session_start` fires with a *partial* mapping
during discovery, so the plugin could not tell "not configured" from "not built yet";
`inject` answers that, and the three states are now distinct -- absent is *pending*,
present-as-`None` is *active with no engine*, present is *active*.

Two consequences worth naming:

- **`self._components` was dead** -- assigned twice, read zero times.  Its need for the
  whole composition was conventional.
- **The interactive loop's catalog had no components**, so the plugin's `inject` could not
  be satisfied from it.  That is *why* the hook protocol handed the mapping to
  `on_session_start`: it was a workaround for a host that could not resolve services for
  its plugins.  The loop now passes its mapping.

What remains of the hook protocol: the dispatch code and its tests.  No plugin implements
a hook any more, so it can be deleted -- which is the next step, and now a deletion rather
than a migration.

## What is still not a plugin

The seven phases are done.  Measured against the objective's list rather than the phase
names, two entries remain services rather than plugins, and they are the same shape:

**Agent** is built by `bootstrap` as a local and put in the component mapping.  Memory
was in the same position and is now a plugin -- `agent/memory/plugin.py`.

It took three steps, and the ordering was the whole of it:

    LTMStore -> EvolutionEngine  -> catalog     the engine moved into its plugin
    LTMStore -> BuiltinTools     -> catalog     the tools block moved below the mount
    LTMStore -> ContextManager   -> catalog     the manager moved below the mount too

Only then could `MemoryPlugin` provide `ltm_store` and `memory`, with `ContextManager` and
the web channel's session-store factory resolving the store rather than closing over a
local.  `bootstrap` still owns the `ContextManager` -- Phase 2 made Session a *service* --
but it now takes the store from the composition instead of building it.

**What is left is `agent`**, which is the same story with more of it and is also where
`BaseAgent`'s 2,530 lines live.  No phase asked for it; the objective's list did.

**Agent** is the same story with more of it, and it is also where `BaseAgent`'s 2,530
lines live.  No phase asked for this one; the objective's list did, so it is recorded
here rather than quietly dropped.

The other departures are measured as inapplicable rather than undone: Orchestration and
Workflow have no separable boundary, and CLI is not a channel.

## Two buses, and the subscription that reads correctly

A domain publishes on **its own bus** (`TOOL_EVENTS`, `TURN_EVENTS`, ...), and `ctx.bus` is
the bus for events a composition defines itself.  In the shipped composition those two are
the same object, so `ctx.on(TurnCompleted, handler)` **works there** -- and would stop
working the moment a composition had its own bus.  A listener that is never called is
indistinguishable from a plugin that works.

`Context.on` first refused a domain's event type and named `Domain.attach`, which goes to
the domain's bus in every configuration.  The kernel does not import the domain table to
find out -- a domain is vocabulary kept outside the kernel by design -- so the composition
wired `host.domain_lookup = event_domains.domain_of`, and a bare host accepted anything.

The refusal was narrower than it looked and was almost written wrong twice: `_domain_lookup`
had to be added to `Context.__slots__` (without it every plugin failed at construction,
which reads as "the guard broke a hundred tests" rather than as a missing slot), and the
rule was *not* "domain events are never delivered on the context's bus" -- they are,
whenever the two happen to coincide.

**Superseded.**  The refusal was right about the danger and wrong about the remedy: the
composition *is* the thing that knows where a type is published, so `Context.on` now
**asks** rather than refuses, and the obvious call is the correct one:

```python
ctx.on(ToolPreExecute, self.gate)     # one type -- routed to the tool bus
TOOL.attach(ctx, self.watch)          # the whole domain, one handler
```

`host.event_bus_lookup = event_domains.bus_for_type` replaces `domain_lookup`; the
`ValueError` and its message are gone, because a guard that has to teach you the right bus
is a worse answer than placing the subscription on it.  The failure mode is closed by
construction instead of by a message.  See "The third half" below for why the message was
not enough anyway.

## The engine moved into `EvolutionPlugin`

`bootstrap` stopped constructing `EvolutionEngine` above the catalog; the plugin builds
the one it uses, reading `cfg` for the enabled switch -- the shape the channel plugins
have.  That removes the last consumer of `memory` above the catalog (`memory` L596 ->
engine L909 -> catalog L932), which was what blocked `memory` from being a plugin.

The plugin **honours an engine the composition already provided** and builds one only when
nobody did and the config says so.  Substitution is the reason: a test injecting a fake, or
a composition preferring its own, is a decision the plugin does not get to overrule -- and
it is also the difference between this and the bug it replaces, which built an engine after
being handed a `None` that meant *disabled*.

**A note on how the previous round went.**  This was written once, appeared to break
`test_interactive_loop_compaction_keeps_latest_system_prompt`, and was set aside with a
patch file and a note saying the cause was not understood.  It was not the change: the
test's own edit -- adding `cfg` to its components -- had **dropped the `"evolution":
_FakeEvolution()` line** from the dict being replaced.  So the plugin had nothing to use,
`enabled: False` told it not to build one, and `/evolve` returned "engine is not available".

The diagnosis that mattered took one print, placed **before** the early return rather than
after it: two rounds of bisecting files could not see a mistake in a diff I had written
myself, and a single `print(self._engine)` could.

## The ordering constraint on `memory` is gone

The chain was `memory` (L596) -> `EvolutionEngine` (L909) -> catalog (L932), so anything
mounted by the catalog provided `memory` too late.  The engine moved into its plugin last
round; the second consumer was `BuiltinTools`, and that block now runs **after**
`discover_and_load()`.

Measured before moving, because a reorder is only safe if nothing in between reads what
moved: nothing between the block and the mount reads a tool from the registry
(`list_tools`, `tool_capabilities`, `tools_with_capability`, `tool_source`,
`to_anthropic_format`, `call` -- none), and the catalog registers no tools of its own, so
the builtins-versus-plugins registration order does not change.  The registry context the
block sets is read by tools **at call time**, which is why it could travel with them.

Verified as a reordering: **134 -> 134** top-level statements in
`_build_components_async`, **identical as a multiset**.

What is left before `MemoryPlugin` is small and named: the component-mapping literal
still refers to the `memory` local, and the moved block still reads that local rather than
the service.  Both become `require_service(components, "memory")`, and then the
construction itself can move into the plugin.

## Open: `AgentPlugin`, and what the measurement says about it

The last item on the objective's list, and the measurement is done -- it is reproducible,
but not at one moment.

**The construction is `cfg`-derived, all of it.**  `BaseAgent(...)` takes ten arguments
and eleven tuning assignments follow it (`max_parallel_agents`,
`sub_agent_timeout_seconds`, `sub_agent_retries`, `max_agents_per_turn`,
`max_tool_call_iterations`, `max_truncation_continuations`, `llm_max_retries`,
`llm_retry_base_delay`, `result_content_max_chars`, ...).  Every one reads `cfg` (or
`orch_cfg`) with a `shared.*` default.  Three of the constructor's arguments --
`supports_vision`, `context_window`, `output_reserve` -- are pure functions of `cfg`, and
the rest are already services or mapping entries.

**Three assignments are not construction; they are wiring, and they happen at three
different times:**

| assignment | readers | when |
|---|---|---|
| `agent.content_filter` | 8 | before the catalog |
| `agent.plugin_catalog` | 3 | **after** the catalog is built |
| `agent.context_manager` | 15 | **after** the mount -- it was moved there when `MemoryPlugin` needed `ltm_store` |

Measured on the *reads*, not the writes: an earlier round removed a parameter because its
attribute was stored and never read, and the parameter itself was used twenty lines below.
All three of these are live.

**Shape (b) was chosen and is done**: `agent/memory/session_plugin.py` provides
`context_manager`, mounted after `MemoryPlugin` (whose `ltm_store` it injects) and before
anything that reads either.  `bootstrap` no longer assembles it, so the ordering obstacle
is gone: an `AgentPlugin` mounted after these two has `context_manager` in hand during
`setup`, and `agent.context_manager = require_service(...)` is one line the composition
still owns -- because it is wiring an already-built agent, not constructing one.

`project_memory_scope` moved from `bootstrap` to `agent/pathing.py` along with it: the
plugin needs it and a plugin cannot import the composition root.  It was already a pure
function of one path.

**And there are two agents.**  `bootstrap` builds one per process and a second per session
(inside the session-components factory, rebuilt when the provider changes).  A plugin
provides one, so the process-wide one is what `AgentPlugin` would own, and the session path
keeps building its own -- which is right, because a session's agent is not a
composition-time thing.

## `AgentPlugin` -- done

`agent/core/agent_plugin.py` provides `agent`.  It was attempted once and reverted because
three failures were not understood; the next round understood them, and all three were
consequences of the move rather than objections to it:

- **`supports_vision` came back undefined** because the plugin imported `BaseAgent` at
  module load, so the tests' patch of `bootstrap.BaseAgent` no longer decided anything.
  The plugin now **takes the class as an argument** and `bootstrap` hands it its own
  module-level name -- so the substitution point that existed before still exists, which
  matters beyond tests: "which agent" is a decision the composition is supposed to make.
- **The session rebuild test saw `model is None`** because a blanket removal of
  `"agent": agent` from the composition root took the *session's* entry with it: the global
  one had to go and this one has the same text.  Restored, and it belongs: a session builds
  its own agent against the config on disk.
- **The ledger and the closed-set table** disagreed, and the honest answer was that
  `agent` has **two writers** -- the plugin for the process, `bootstrap` for a session.
  The ledger's `owners` carries both, and the key stays in `_COMPONENT_KEYS` rather than
  moving to `_PLUGIN_PROVIDED`, which means "not written by the composition at all".

Four things had to move with it: `project_memory_scope`, `active_context_window`,
`active_output_reserve` and `bounded_int` out of `bootstrap` (a plugin cannot import the
composition root, and all four were already pure functions of one config or one path);
`max_tokens` into the mapping *literal* rather than the post-mount update, because the
plugin waited on it; and `register_spawn_capability` below the mounts, because it resolves
the agent.

## The last item: Orchestration -- measured, and it is not a service

The objective lists Orchestration among the things to turn into a plugin.  It is not one,
and the reason is **not** size or boundaries: **its input is per-call state**.

`plan_orchestration` (in the agent-loop plugin) builds an `OrchestrationPlanner` on every
turn from `ctx.metadata["skill_catalog"]`, and that key is written by **four different
driving paths**:

| path | where |
|---|---|
| the runtime's per-turn context | `agent/runtime/contracts.py:833` |
| Ralph's loop | `agent/ralph/plugin.py:93` |
| the CLI loop | `agent/cli.py:883` |
| a channel session | `agent/channels/base.py:646` |

A composition-time `orchestration` service would be built against **one** of them, and the
other three would be silently ignored.  That is a behaviour change, not a refactor -- and
it is exactly the failure shape this refactor has been avoiding all along ("a plugin that
imports its own answer cannot be told").

The other part, `SpawnPlanner`, is constructed in `BaseAgent.__init__` with
`PlannerDeps(settings=self, ...)`: the agent handing *itself* in.  Its dependency points
**into** the agent, so a plugin providing it would have to be built after the agent and the
agent re-wired afterwards -- the reverse of the ordering rule every other move followed.

`tests/test_orchestration_seam.py` pins both claims, so they are falsifiable rather than
assertions in a document: it fails if the planner stops reading the call's metadata, or if
the four writers stop being four.

**And one correction to the paragraph above.**  It said the policy is composable because the
skill catalog is a service plugins contribute to.  Half true, and the half that was wrong
matters:

- A plugin's bundle is namespaced `plugin:<bundle>` and
  `SkillCatalog._rebuild_aliases` **deliberately** gives plugin skills no short alias, so
  one plugin cannot hijack a name another one means.
- `from_skill_catalog` looked the policy up by the **bare** id.

So a plugin's bundle **never** decided orchestration -- correct as a default (that decision
applies to every turn in the process) and wrong as a dead end (a composition had no way to
choose one deliberately).  Two additions, neither changing the default:

- `from_skill_catalog(catalog, policy_skill_id=...)`, so the lookup id is a parameter.
- `orchestration.policy_skill_id` in config, reaching the planner through the agent and
  `LoopDeps` -- the same route every other config-derived knob takes.

A composition that wants a plugin's policy now names it **in full**
(`plugin:multi-agent-orchestration`), which is a decision rather than a takeover.

## The kernel's boundary now has a test

"No privileged core" has been in this document and the README since Phase 1, and **it had no
guard**.  A documented property with no test is one that becomes false quietly, and this is
the property the whole design rests on: the moment the kernel can name `Agent`, that name is
privileged, because the kernel is what everything else is composed *by*.

Two rules, because they fail differently:

- **Structural and exact**: the kernel imports nothing from the rest of the project.  Only
  `agent.kernel`, the stdlib and `typing`.  It was already true; nothing enforced it.
- **Vocabulary**: no identifier in the kernel names a domain concept.

**The vocabulary rule was written wrong first, and the control caught it.**  The initial
version compared whole identifiers, so `ToolRegistryProxy` passed -- a compound name neither
equals nor starts with `tool_`.  That is the spelling a domain concept would actually arrive
in.  It now splits underscores *and* CamelCase, and the control asserts all four forms.

Two false positives had to be resolved by **provenance rather than by loosening the rule**:
`runtime_checkable` (a `typing` decorator) and `RuntimeError` (a builtin) both split into
`runtime`, and neither is this project's runtime.  A name imported from a non-`agent` module,
or one that `hasattr(builtins, ...)`, is not this project's vocabulary -- dropping it keeps
the rule strict for real names.

## Hot swap: replacement used to delete what it replaced

`load(plugin, replace=True)` unloaded the incumbent, which correctly took its dependents
down -- they held a service whose owner was about to be gone -- **and then left them down**.

Measured, before it was fixed:

```
load MemoryPlugin + SessionPlugin   -> loaded ('memory', 'session')
load OtherMemory(replace=True)      -> loaded ('memory',)
                                       'context_manager' gone, nothing pending, no error
```

So swapping the provider of `ltm_store` permanently removed `context_manager`. That is not
replacement, it is deletion with replacement's name -- and the dependent could not even
refuse the new implementation, because it was gone.

The fix is small and the kernel already had everything needed: `Install` keeps `plugin` and
`parent`, so the dependents are captured **before** the unload, in activation order, and
re-loaded after the replacement is in.  A replacement that does not satisfy them leaves them
**pending**, which `waiting_for` reports by name -- visible, not silent.

`PluginCatalog` already loads discovered plugins with `replace=True`, so reloading a plugin
from disk goes through this same path.

**A note on the baseline.**  One full-suite run in this round reported a fourth failure,
`test_ralph.py::test_verifier_escalates_for_live_process_group_after_leader_exits[caller]`.
It did not reproduce: the same tree then ran three times clean, the test passes 3/3 alone and
in its own file, and the failure is in process-group signal handling, which this change does
not touch.  Recorded here rather than silently ignored, because a baseline that is compared
by diff has to say when it was seen once.

## The built-in tool set is a plugin

`BuiltinTools` was the largest implementation in the project and the last thing `bootstrap`
constructed **directly** -- `builtin_tools_cls(memory, registry, ...)` at the composition
root -- so every tool it registered lived for the life of the process.  It is now
`agent/tools/plugin.py`, mounted like the rest.

Measured after the move, because a plugin whose effect is registrations is only a plugin if
the registrations are undoable:

```
load ToolsPlugin()      -> 44 tools, source='builtin', capabilities('read_file') = {'read'}
unload('tools')         -> 0 tools
```

**Two things had to be true for that, and both were already-found bugs of the same shape.**

1. **`ScopedTools` was still "almost a registry".**  It refused `authorizer=` (which
   `BuiltinTools` passes for file mutations) -- the second time this happened, after
   `source=`.  Rather than add the third keyword when it arrives, the view now forwards the
   registry's whole keyword set, and `tests/test_tool_capabilities.py` **compares the two
   signatures**: a new parameter on `ToolRegistry.register` fails that test rather than
   loading a plugin that then cannot register.

2. **`get_context` had to be forwarded, read-only.**  Tool implementations take the registry
   and read the composition's shared context (`output_dir`, `file_access_policy`, ...).
   `set_context` is deliberately **not** forwarded: it is how the composition configures the
   process, and a view that forwarded writes would make "one plugin's handle" a way to reach
   into every other plugin.

## Hot reload kept old code, and nothing said so

The reload path was already tested for a plugin **appearing** and one **disappearing**, and
for a changed **markdown command**.  Changed **Python** was not covered, and it was broken:

```
'v1' -> 'v2'          (same size, same clock tick)  ->  the plugin still provided 'v1'
'v1' -> 'v2-longer'   (different size)              ->  the new value
```

Python validates a `.pyc` against the source's **mtime and size**, so an edit that keeps the
size and lands in the same tick is invisible: the reload re-imported, re-ran `setup`,
re-provided the service -- and provided the old value.  Nothing errored, nothing was pending,
and the plugin object was genuinely new, which is why the first reading of it looked like a
scope problem rather than a bytecode one.

The fix drops the cached bytecode before importing a plugin
(`importlib.util.cache_from_source` -> `unlink`), unconditionally rather than only on reload:
the window is narrow but a hot-reload feature that silently keeps old code is worse than one
that never claimed to reload.

**The lesson is about which case the test used.**  The first version of the test wrote `'v1'`
then `'v2'` -- and failed for the bytecode reason -- while a second test whose new code
happened to be a different length passed, which made the failure look like it was about
*which service* was provided.  Both tests were right about the reload and wrong about the
cache; only the same-length case could tell them apart.

## Channels: the composition is hot-swappable, the implementation is not refactored

Measured, because "channels are just a wrapper" (an earlier note in this document) is true of
one half and false of the other:

```
load WebChannelPlugin + FeishuChannelPlugin  -> 2 channels, owners ('channel.web', 'channel.feishu')
unload('channel.web')                        -> 1 channel, owner ('channel.feishu',)
```

So a channel plugin's contribution **is** undone by unloading it -- `ctx.channels.add` returns
an undo and the scoped view registers it on the plugin's scope.  That is the hot-swappable
half, and `tests/test_channel_composition.py` pins it by identity of the survivor rather than
by count.

What is *not* refactored is `agent/channels/web.py` (4,183 lines) and `feishu.py` (2,587):
they are unchanged by the whole refactor and are still single large classes.  Composable and
modular are not the same claim, and this document should not blur them.

**And "replaceable" for a channel is a scenario, so it is asserted as one**:
`test_a_composition_can_replace_the_built_in_web_channel` composes with `channels.web`
disabled, contributes a substitute from a plugin, and asserts that the substitute is what is
live -- then unloads the substitute and watches it go.  No edit to `plugin.py` or `web.py`
is involved, which is the test of whether the seam is real.

That is a **better** seam than a constructor argument, which is why the channel
implementation is deliberately **not** passed in by name: `web.py` needs starlette/uvicorn,
and importing it at module scope would make the whole catalog fail on a machine without them
instead of skipping one channel with a report.  `test_the_built_in_channel_plugin_mounts_
without_its_optional_dependency` pins that, because "make it a constructor argument" is the
obvious-looking change that would break it.

## What "replaceable and hot-swappable" now means, and how it is checked

| property | how it is asserted |
|---|---|
| a plugin's services are removed when it unloads | `tests/test_*_plugin.py` -- each asserts the name is gone from the mapping |
| replacing a provider **restores its dependents** | `tests/test_kernel.py`, `tests/test_hot_swap.py` |
| the tool set goes away with its plugin | `tests/test_tools_plugin.py` -- 44 tools, then 0 |
| a channel goes away with its plugin | `tests/test_channel_composition.py` -- by identity of the survivor |
| the built-in **web channel can be replaced** without editing core | `test_a_composition_can_replace_the_built_in_web_channel` |
| a changed **plugin file** is picked up on reload | `test_reloading_picks_up_a_same_length_edit` |
| the class a plugin constructs is a substitution point | `test_the_class_is_a_substitution_point`, `test_it_takes_the_class_as_an_argument` |

**An audit of the "almost replaceable" pattern**, done rather than assumed: every plugin's
`setup` was scanned for implementation classes bound by an import.  What remains is
`CommandResult` and `AgentContext` -- **types**, not implementations -- and the two channel
implementations, which are deliberate and pinned with their reason.

**What was deliberately not done.**  "The implementation inside the plugin" for channels
would mean moving `agent/channels/web.py`'s 4,183 lines into `agent/channels/plugin.py`.
The property it is meant to buy -- a composition serving its own transport on the web slot --
is already achievable without editing either file, and is now asserted as a scenario.  A
registry any plugin can contribute to is a better seam than a constructor argument, which is
why the opposite-looking change (passing the class in by name) is actively wrong here: it
would break the optional-dependency behaviour.  Recorded because declining a literal
instruction is a decision, not an omission.

## Review findings, verified: the refused state and the parent link

An external review raised twelve points.  Two of them are P0 and both were **reproduced
before being fixed**; a third was found while verifying them.

**1. A plugin whose `setup` raises was invisible.**  `loaded()` and `pending()` were the only
two states, and a refusal is neither -- `_fail` set `install.error`, logged a warning and
dropped the install.  Measured: `load()` returned an install with the error, and **no API
reported it at all**.  Now `failed()` and `failures()` exist, `unload` reaches a failed
install, a later success clears the record, and the composition root reports what did not
load.

**2. `pending_report()` had no caller in production.**  So even the state that *was*
diagnosable was never diagnosed.  It is now printed by the composition root alongside
failures, and both are recorded in the mapping (`plugin_failures`, `plugin_pending`) for the
gateway and the CLI.

**3. Found while verifying the review's first P0, and worse than what it described.**  The
review said `_capture_dependents` misses *pending dependents*.  The real defect was one level
down: **a child loaded from a plugin's own `setup` never got a parent link at all**, because
`self._installs[name] = install` happens *after* `setup` returns.  Measured: a parent's
`children` was `[]` after `ctx.plugin(Child())`.

Disposal still worked -- scopes nest, so the child's registrations were unwound by the
parent's scope either way -- which is exactly why nothing looked wrong.  What broke was
everything that *walks* the tree: `replace`'s capture.  A **pending** child present before a
`replace=True` vanished from both `loaded()` and `pending()`.

Both halves are fixed: `_activating` tracks installs whose setup is running (so the link is
recorded), and the capture reads `_installs` **and** `_pending`.  A pending child is now
restored and still reported as waiting for what it wanted.

**A placement mistake worth recording.**  The report was first placed immediately after the
mounts, where it announced `Plugin 'ralph' is waiting for: system_prompt` -- for a plugin that
was loaded by the time the composition returned, because `system_prompt` is written into the
mapping below the mounts.  A warning that is usually wrong is one people learn to skip, so the
report now runs at the **end** of the composition, where the state has settled.  That this was
visible at all is the new report doing its job.

## Review finding 3: awaitable teardown

The review's third P0: the lifecycle had `setup`/`teardown`/scope-unwind and nothing that
could await, so "close the pool", "wait for the subprocess" and "flush over the network" had
no home.  That makes a system replaceable and unloadable but not fully hot-pluggable.

**What was not done, and why.**  `async def setup` is still refused.  Supporting it means
`load` becomes async, which changes every caller including the composition root -- and it is
not the common need: teardown is.  The refusal message now **names the alternative** rather
than only saying no, because a refusal that gives no route is one somebody works around badly.

**What was added**, in the two spellings a plugin might want:

- `ctx.on_unwind_async(handler)` -- registered, like `on_unwind`.
- `async def astop()` -- declared, like `teardown`.

Both are claimed by the synchronous `unload` and **kept under the plugin's name**, then
awaited by `host.aunload(name)`, `host.aunload_all()` or `host.adispose()`.  The claim is the
part that matters: `unload` cannot await, so dropping the handler would release nothing and
report nothing, which is the failure this design exists to prevent.
`pending_async_disposals()` names whatever is still waiting, so an unawaited teardown is
visible rather than assumed.

```
host.unload("x")             -> ('x',)   pending_async_disposals: claimed, not run
host.services.has("thing")   -> False    the synchronous half is complete
await host.adispose()        -> ()       and now the awaitable half has run
```

## Review finding 5, in its concrete form: the composition depended on luck

The review's P1 "composition root is still a giant manual wiring" has a symptom that can be
measured, and it was worse than "verbose".  `_build_components_async` writes the values it
computes itself straight into the mapping --

```python
_partial_components.update({"system_prompt": system_prompt, "mcp_client": ..., ...})
```

-- and a direct write runs no `setup`, so **nothing re-checks the waiters**.  Activation was
happening as a side effect of a *later, unrelated* mount:

```
mount(TurnRunner()) removed  ->  loaded ['agent', 'memory', 'session', 'tools']
                                 pending ('ralph',)
                                 waiting_for {'ralph': ()}
```

**"Pending, and waiting for nothing."**  That is a diagnostic that misleads at the one moment
it exists for, and it was caused by a second defect: `waiting_for` derived the unmet set at
*report* time, so once a dependency existed by any route the reason vanished while the
pending state did not.

Both are fixed: the reason is **recorded when a plugin goes pending** (`Install.missing`), and
`flush_pending()` is public so the composition says when it has finished writing.  With the
`TurnRunner` mount removed, `ralph` now loads correctly.

The deeper point is the review's: this ordering wants to be declared rather than sequenced.
`flush_pending()` makes the *current* form correct instead of accidental; it does not remove
the manual ordering, and declaring the composition is still open work.

**A flaky family, recorded.**  Under full-suite load, `tests/test_ralph.py` has now produced
two different failures across this session --
`test_verifier_escalates_for_live_process_group_after_leader_exits[caller]` earlier and
`test_verifier_caller_cancellation_propagates_after_cleanup` now.  Both pass 3/3 in isolation
and inside their own file, both pass on a re-run of the full suite, and both exercise
process-group signals and cancellation.  Not diagnosed, and not dismissed: a baseline
compared by diff has to say when it saw something once.

## Review finding 4: the buses belong to a composition

The four domain buses were module-level singletons.  Their docstrings gave a real
justification -- "the executor is built in several places (the turn loop, the scheduler,
tests) and a per-instance bus would mean a plugin's gate applied to whichever construction
path happened to pass one" -- which is an argument for **one bus per composition** and was
implemented as one per *process*.  The cost was measured in this suite: a leaked
`PreToolUse: deny` gate from one test won another test's `emit_blocking`.

`default_buses()` produces a fresh set; `bootstrap` provides them under
`event_bus.<domain>`; `Domain.attach` **resolves through the context**; and `BaseAgent` takes
`event_bus` / `llm_event_bus` / `tool_event_bus` at construction, defaulting to the process
buses so an embedder that provides nothing keeps the behaviour it had.

**Both halves had to move together, and that is not a detail.**  Resolving only the
subscribing half -- `Domain.attach` reading a composition bus while `BaseAgent` kept
publishing to `TURN_EVENTS` -- would have produced the failure this codebase warns about
most: a listener that is never called, indistinguishable from a plugin that works.  The test
asserts independence in both directions for that reason.

**Two of my own mistakes in that test, both worth recording.**  It first emitted a bare
`object()`, which reaches nobody because the bus dispatches by exact event class; and then it
shared one listener between the two compositions, which made the negative assertion test the
opposite of what it meant -- composition B's bus *should* reach composition B's listener.
Along the way `EventBus.emit` turned out to be a **coroutine**, so calling it without awaiting
returned an object that did nothing, and the first version "passed" the negative half for
entirely the wrong reason.

## The ordering constraints in the composition root were stale

The previous section ended by saying the manual ordering was still open work.  Measuring it
changed the picture, and in the composition's favour:

**All 24 orders** of memory / session / agent / tools produce the same complete composition.
Activation is by dependency **name**, so a plugin whose dependency is absent stays pending and
activates when a provider appears -- the order genuinely does not matter.

Which means the comments recording constraints -- "After Memory: it injects `ltm_store`",
"Last of the three composition-owned plugins" -- described something that had **already
stopped being true**.  A reader who believes them preserves an order that carries no meaning,
and a composition that looks fragile is one nobody dares reorder.

The comments now say what is measured, and `tests/test_composition_order.py` pins it: all 24
orders, plus a control that the tool set actually registered rather than merely loading.

**This is the precondition for declaring the composition rather than sequencing it** -- a
list that can be reordered freely is a list that can become data -- and it is why the
remaining work is now smaller than the review estimated.

## Review finding P2-8: the re-export is gone

`agent/runtime/services.py` was a re-export of six names from `agent.kernel.service`, and its
own docstring said it would be "deleted once nothing imports it".  **Eighteen modules imported
it** -- so "nothing" was a long way off, and the review's suggestion (pin the list and stop the
growth) would have frozen that layer in place.

The names never changed, so this was mechanical: eighteen files now import
`agent.kernel.service` directly and the re-export is deleted.  A structural guard parses every
module for an import of the old path -- **parses, not searches**, because the first version of
the guard matched its own source: the assertion text names the module it checks for, and a
scan that cannot tell a mention from an import reports itself.

**And one process failure, the exact one this session had already written down.**  To verify the
guard bit, the probe reverted `agent/cli.py` to the old import and then ran
`git checkout -- agent/cli.py` to undo it -- which restored it to **HEAD**, where the migration
does not exist, and so to an import of a module that is now deleted.  Sixty-two tests failed.

The rule was already in this session's notes: probe with `cp`, never `git checkout` on a file
with uncommitted work.  The guard itself was fine; the verification of it was the mistake, and
it cost a full-suite run to notice.

## Review finding 6: one path writes durable history

Durable session history was appended from **three** layers -- `AgentCore` for
`turn_finished`, a channel for the runtime events it drained, and `ContextManager` holding
both halves -- and the sequence "append the fact, then derive the projection" was written out
at each.

**The cost was not line count.**  It was that a caller had to know *which context manager
belonged to which session*, and the chain for finding out had a step that is wrong by
construction.  The channel did this:

```python
ctx_mgr = getattr(state, "context_manager", None)
if ctx_mgr is None:
    runtime = session_components.get(event.session_id, components)
    ctx_mgr = resolve(runtime, "context_manager") if isinstance(runtime, dict) else None
```

with a comment explaining that the fallback "sends tool events to an unrelated session and
makes traces disappear after a browser refresh".  **A chain whose last step has a known wrong
answer is a chain nobody can check**; the comment was the reason it looked safe.

`agent/memory/session_log.py` owns the sequence; `ContextManager.record_runtime_event`
delegates to it, so there is one implementation rather than two; and the channel resolves the
log through the session's own state -- **one hop, no chain**, so "which session" is answered by
the lookup instead of by an ordering.

**What it does not own, said plainly**: the projection itself stays with the context manager
that holds the clipping helpers and the working-state shape, and is handed in as a callback.
This unifies *where a fact is written*, which is what was scattered, rather than moving the
projection to a new home for its own sake.

**A dead condition I wrote and removed.**  The first version of the `session_log` property
rebuilt the log when `log.session_id != staging.session_id` -- which is always false, because
both sides read the same callable.  A condition that can never be true suggests a protection
that is not there, so it is gone; reading the id through a callable is what makes rebuilding
unnecessary, and the docstring now says that instead.

## Review finding 7: the composition's own plugins are data

The profile mechanism resolved, and the review's description was accurate: it decides
enable/disable/config for the plugins **found on disk**.  What it could not touch is the six
the composition root mounts itself -- Ralph, Memory, the session log, the agent, the built-in
tools and the turn runner -- which were five literal `mount(...)` calls in `bootstrap`.  So
"this deployment has no memory plugin" was a question a profile **could not ask**.

`agent/plugins/core.py` declares them: name, why it is core rather than discovered, and a
factory.  Two need an argument -- the agent and tool classes -- and they arrive in a
`CoreContext` that carries **only** those, on the same reasoning as `BaseAgent`'s one-name
`services` mapping: a factory that can reach anything makes "what does this need" unanswerable.

`PluginProfile.core` selects a subset; `None` means all of them, because every existing
composition has all of them and a default that silently dropped one would be a behaviour change
nobody asked for.

**This was only possible because of the previous round's measurement**: all 24 mount orders
produce the same composition.  A list that can be reordered freely is a list that can become
data.

**A four-file deletion, and how it was found.**  Replacing five `mount` calls with one loop,
I sliced the file from the first mount to the turn runner's mount -- and everything between
them went with it: eight `registry.set_context` calls, the bundled-skill loading, the bundled
MCP handling, the "Plugins loaded" announcement, and the `_compose_system_prompt` call.  The
tests caught it as `KeyError: 'system_prompt'`, which is a **misleading** symptom: it looks
like an ordering problem, not a deletion.

What found it was comparing the two versions **statement by statement** rather than reading the
diff: `HEAD` 126 statements, mine 105, with 22 missing and 1 added.  The fix was better than
patching the damaged file -- rebuild from `HEAD` and apply only the intended change, which the
same comparison then confirmed as exactly 6 removed and 1 added.

## Review finding 9: the ledger was missing 22 of its readers

The ledger (`agent/runtime/core_services.py`) says it exists to enumerate **every read
point**, so that "was anything reading this" is answered from a table before a deletion.  It
checked one direction: every file it names really does read the service.

Nothing checked the other direction, so **a reader nobody listed was invisible**.  Measured
with the ledger's **own** scanner: **22 reader/file pairs across 7 services**, every one of
them a plugin this refactor added -- `agent/tools/plugin.py` reading `memory` and `registry`,
`agent/core/agent_plugin.py` reading `llm` and `plugin_catalog`, and so on.

That is not incomplete bookkeeping.  This session's notes already said what a false negative
does: *"a check with false negatives is worse than no check when it gates a deletion: it does
not merely miss a problem, it authorises one."*  The guard that was supposed to prevent exactly
that had the flaw it was written against.

`test_every_reader_of_a_service_is_declared` now checks the other direction, and the 22
readers are declared with the reason each one reads that service.

**And two of my own measurement mistakes on the way there.**  A regex of mine reported five
services as having *no ledger entry* -- they all have entries; the regex only matched blocks
ending where I guessed.  Then a `head -8` on a grep truncated the very results it was run to
establish, and I briefly reported that `PluginProfile` had **no consumer** when
`agent/plugins/profile_selection.py` imports it.  A truncated search for absence is not a
search.

## Review finding 8: "the agent is still the centre" -- partly outdated

Measured, and the review's premise holds less than it sounds:

- `AgentCore` holds the component container, but reads through `require_service`/`resolve`,
  and **those names are ledger entries** -- so it is not reaching arbitrarily.  It reads ten
  names, now all declared.
- `TurnRunner` **has** a plugin path (`setup(ctx)` provides `agent_loop`); `bind(components)`
  is the documented fallback for a hand-assembled runtime.
- `LoopDeps` is already "everything the turn loop needs from the agent, and nothing else" --
  but it is **derived from the agent** (`LoopDeps.of(agent)`), which is where the centrality
  actually lives.

What is genuinely left, and not done: `AgentPlugin` writes **12** config attributes onto one
`BaseAgent`, and `BaseAgent` carries ~35 instance attributes.  Turning those into a typed
settings value resolved once is the review's "separate Service from Settings", and it is a
change to a 2,500-line class with call sites across the runtime.  Recorded as the next piece
rather than started at the end of a long run.

**What was measured and is worth keeping**: the centrality is not "the agent reaches
everything" -- it is that the loop's dependency struct is built *from* the agent instead of
*from* the composition.  That is a much smaller thing to fix, and it is where to start.

## Review finding 10: settings are not services

`AgentPlugin` wrote twelve attributes onto the agent it had just built, ten of them derived
from config with **three different clamp idioms** spread over forty lines.  `agent/core/agent_settings.py`
now holds that derivation as one frozen value, and `apply_to` copies it onto the agent so the
dozen places that read `agent.max_parallel_agents` keep working unchanged.

The review's phrasing is the reason this is worth doing: the container was carrying **both**
services and settings, and they are not the same kind of thing -- a service is something a
composition chooses and can replace, a setting is a number the operator wrote in a file.
Nothing in `AgentSettings` is provided by a plugin, resolved by name, or replaceable at run
time.

**The risk in a move like this is silent, and one instance of it happened.**  Moving a
derivation is behaviour-preserving only if every clamp, default and *config path* moves with
it.  `result_content_max_chars` is read under `orchestration`, and the first version of
`from_config` looked for it at the top level -- which would have quietly stopped honouring a
configured value, with no error and no failing test.  It was caught by comparing against the
old code line by line, and `test_the_orchestration_section_is_where_the_orchestration_keys_live`
now asserts both directions: the value under the section is honoured, and a top-level spelling
of the same name is **not**.

**And the seam moved under two source-scanning tests, which were updated rather than
deleted.**  `policy_skill_id` is now read in `AgentSettings`, not in the plugin.  The
assertions follow the seam, because what matters is that the key is read *on the way to the
planner* -- a test that keeps passing by looking where the value no longer travels is worse
than one that fails.

## What is left of "the agent is the centre", measured

The previous section said the centrality is that `LoopDeps` is built *from* the agent rather
than *from* the composition.  Measured, so the next attempt starts from numbers rather than
from the phrase:

```
LoopDeps.of(agent)  ->  28 attributes
```

Of those, about **fifteen are private methods and callables**, not values:

    agent._bounded_text            agent._protected_turn_messages
    agent._input_token_budget      agent._output_room
    agent._token_budget            agent._format_agent_error
    agent._create                  agent._retry_policy()
    agent._context_manager_for     agent._recover_from_content_filter
    ...

So the loop does not merely read configuration from the agent -- it calls the agent's own
budget arithmetic, clipping, retry policy and error formatting.  **That is the centre**: not
that the agent holds state, but that the turn loop's *behaviour* is delivered through the
agent's private helpers.

Extracting it means moving budgeting, truncation, retry policy and error formatting out of a
2,537-line class and into the loop or into services -- a change with a wide blast radius, and
one that should not be started at the end of a long run.  **Recorded rather than begun.**

What is *not* left, contrary to the review's phrasing: `AgentCore` reads through
`require_service`/`resolve` and its names are ledger entries (now checked in both directions);
`TurnRunner` has a plugin path, with `bind` as the documented fallback; and the settings half
is done -- `AgentSettings` holds the derivation, so what the agent carries is closer to
"state plus collaborators" than "state plus a config bag".

## Review finding 11: a profile can say where a service comes from

`PluginProfile.core` says **which** of the composition's own plugins are present.  It cannot
say that a particular **service** comes from somewhere else -- and a deployment shipping its own
memory plugin needs exactly that: "include memory" and "memory comes from acme" are different
statements.

`PluginProfile.service_replacements` (service name -> plugin name) is that statement, and
`core_plugins(replacements=...)` **drops any core plugin whose `provides` intersects the
replaced set**.  By `provides`, not by name: `memory` offers both `ltm_store` and `memory`, so a
name-based rule would drop it for one spelling and leave a collision for the other -- which the
kernel refuses at the second `provide`, with an error naming neither the profile nor the
replacement.

**And the composition root checks the declaration rather than trusting it.**  Deciding what not
to mount says nothing about whether the named plugin came up and actually provided the service.
Without the check the failure is a `KeyError` three layers away; with it, the message names the
service, the expected plugin and what actually happened -- including "not provided at all".

**This was declined one round earlier as speculative, and that was the wrong call.**  The
reason given was "the kernel already supports replacement, so a declaration is a mechanism
without a consumer".  What changed the answer is `CoreContext`: once the composition's own
plugins were **data**, a profile could express *which* plugins exist and not *where a service
comes from* -- an asymmetry in the model rather than a missing feature.  The earlier judgement
was made before that structure existed.

## Review finding 12: the layers, named

Layering already happened -- defaults, then the file, then a bundle, then a profile -- but
through **two** mechanisms, and the scheduler's run config is the clearest case:

```python
run_cfg = dict(current_cfg)                  # base
run_cfg["workspace_root"] = str(workspace)   # a raw dict write
run_cfg["output_dir"] = str(run_output)      # a raw dict write
run_cfg = apply_profile_to_config(run_cfg, profile)   # a patch, validated
```

The two raw writes are layers too -- just layers `apply_patch` never saw, so they were not
validated against declared paths, not undoable, and not attributable.  "Which layer set
`output_dir`?" had no answer, and a typo in one became a setting nobody reads instead of an
error.

`agent/config_layers.py` is **not a new mechanism**: it is the existing one with the layers
named and ordered, and `resolve_layers` applies what it is given, in order, through
`apply_patch`.  `allow_new` stays **per entry**, so a run that legitimately adds
`workspace_root` keeps the strict rule for every other patch in the same layer.

**Two rounds running, I have had to reverse myself on this review item.**  First I called the
core-plugin declaration "the same information as a profile"; then I called service replacements
"a mechanism without a consumer".  Both times the structure that made the feature obviously
necessary -- `CoreContext`, then `provides` -- was built *after* the judgement.  The pattern is
worth naming: **a judgement about what a model should express is not reliable while the model is
still being built.**

## Next step, in order

1. **`SchedulerPlugin`.**  Its prerequisites are settled: single-sourced parameters
   (`agent/scheduler/settings.py`), a synchronous builder in its own module
   (`agent/scheduler/build.py`), and a runtime-factory service so a run can build a
   runtime without importing the composition root (`agent/runtime/factory.py`).  What
   remains is the non-isolated branch -- a run with no durable ids uses the composition's
   own mapping, which a `Context` does not hand out -- and that branch exists only for
   lightweight executors.
2. **Phase 6, the channel plugins** (CLI / Web / Feishu) -- measured next, and for the
   same reason Phase 5 was: to find out which of the three is genuinely a component.
3. **`BaseAgent` is still 2,530 lines** and still holds the sub-agent machinery, the
   tool-execution entry points and the streaming entry point. Those are the next
   things to leave it -- each is a plugin by the plan's table, and none is blocked.

## The third half: two listeners were still on the process bus

"Both halves had to move together" was the finding that produced composition-owned buses,
and it was one half short.  The two that moved were the *subscribing* half
(`Domain.attach` resolving a composition bus) and the *publishing* half (`BaseAgent`
taking the buses it is given).  The missed one is a listener that is neither: the plugin
catalog subscribes its `hooks.json` bridge at construction, from

```python
self._event_bus = event_bus if event_bus is not None else TOOL_EVENTS
```

and `bootstrap` constructs the catalog **without** `event_bus` while handing it the
component mapping that already contains `event_bus.tool`.  So in production the executor
published on the composition's tool bus and the bridge listened on the process one, and a
`PreToolUse: deny` command hook was loaded, reported, and never called.  The same
statement existed a second time in `fire_session_end`, which emitted `SessionFinished` on
`SESSION_EVENTS` while `EvolutionPlugin` subscribed through `SESSION.attach` -- the
session's transcript scored by nobody.

**No test could see either one.**  Every existing test of the tool bridge calls
`catalog.fire_pre_tool(...)` directly, never through a bus; and the two callers of
`fire_session_end` are a CLI loop and a channel.  That is the shape of blind spot worth
recording: the bridge had tests, the bridge was correct, and *the wire to it* had none.

The fix is `PluginCatalog._bus_for(domain)`: resolve `event_bus.<domain>` from the
composition's registry (the same names `Domain.attach` uses), falling back to the process
default **asked of the domain table** -- `bus_for(domain, None)` -- rather than to a second
copy of the table in the catalog, because a stale copy is still a bus that accepts
listeners.

Three tests, and they bite in both directions: the bridge is delivered a refusal through
the composition bus; a catalog with no composition still bridges on `TOOL_EVENTS`; and
the session's end reaches a plugin subscribed through `SESSION` into a real `PluginCatalog`.

## Typed subscription: ask, do not refuse

`Context.on` used to refuse a domain's event type and point at `Domain.attach`.  That
guard had a cost that only showed up here: **it taught a rule the kernel could have
applied itself.**  The composition knows where a type is published, so the kernel now
asks -- `host.event_bus_lookup = event_domains.bus_for_type` -- and

```python
ctx.on(ToolPreExecute, self.gate)      # routed: tool bus
ctx.on(MyOwnEvent, handler)            # no domain owns it: the host's bus
```

Both forms of subscription go through the same table, and `Context.on` lost a slot
(`_domain_lookup`), a `ValueError` and eleven lines of message.  The tests for it changed
shape rather than count: *delivery* instead of *a raised message*, because a message is
not a listener -- the old test would have passed with the subscription going nowhere.

`EvolutionPlugin` switched to the single-type form (`ctx.on(TurnCompleted, self.on_turn)`)
and lost the `isinstance` guard on the first line of the handler; it had been subscribing
to the whole `TURN` domain and dropping two of the three types, because asking for one was
not possible.  `SESSION.attach` stays: sessions are a domain it takes as a whole.

## The provider refactor made every configured provider a startup prerequisite

Reported as "the gateway no longer starts":

```
Provider: opencodego | format: openai | model: deepseek-v4.1-flash
Error: API key env var 'ANTHROPIC_API_KEY' not set (provider: ?).
```

The active provider was configured and working.  The failure came one statement later, in
`build_routing_transport`, which built a client for **every provider that owns a model**
so that per-call model overrides could be routed.  Six providers in `config.json` and one
exported key is the ordinary case -- the routing table exists so the others are
*offerable*, not so they are *required*.

The regression was the provider-registry commit: it made
`bootstrap._provider_client_factory` delegate to `providers.build_client`, which resolves
`$ENV` and **raises**.  The function it replaced resolved `$ENV` to `""` and built the
client anyway, so an unused provider's missing key cost a failing request *later* rather
than a refused start.  Naming the failure a "single construction path" hid that this path
is called eagerly for providers nobody selected.  `provider: ?` was the second half of the
same commit: a section on disk does not carry the key it is filed under, so the one message
about a missing credential could not say whose it was.

The fix keeps the model routable and carries the failure:

- `providers.missing_credential` answers "is this provider usable" **without raising**, so
  the composition can ask before it builds;
- `build_routing_transport` builds each routed provider's transport through
  `_route_transport`, which returns a `_ProviderUnavailable` holding the reason instead of
  raising -- *except* for the active provider, whose client the composition runs on;
- the model keeps its route, because dropping it would post a foreign model id to the
  active endpoint -- the misroute the routing table exists to prevent;
- `RoutingTransport.unavailable` exposes the reasons, and bootstrap prints them next to the
  `Provider:` line;
- the provider's own name is attached to the section before the client is built, in the
  routing loop and in the web connection test.

Tests: four in `tests/test_model_routing.py` (deferral, report, a usable sibling still
built, active still fatal), two in `tests/test_provider_registry.py` for the message and
for the literal-key control, and one at the composition level
(`test_a_composition_starts_with_an_unused_provider_uncredentialed`) because that is where
the user stood.

**A blind spot found while fixing `provider: ?`.**  The guard that keeps
`PROVIDER_FIELDS` and the loader in agreement scanned only `provider_cfg.get("key")` with
**double** quotes, and `resolve_api_key` had always read `provider_cfg.get('name')` with
single ones -- a validation that could not see half its subject.  The scan now takes both
quoting styles, which makes it see that read, and `_INJECTED = {"name"}` names the keys the
loader *writes about* a section rather than reads *in* one.

## An error path that could not describe its error

Also in the gateway log, and not from this refactor (blame: 2026-08-11):

```
Sleep extraction error: Error code: 402 - Insufficient account funds
Background consolidation error: 'ConsolidationResult' object has no attribute 'error'
```

The extraction call failed, the engine caught it and returned `success=False`, and the
caller's failure branch read `consolidated.error` -- a field `ConsolidationResult` never
had.  So the emit that exists to say *why* consolidation failed reported an `AttributeError`
about the reporting code, and the real reason (the provider's 402) was replaced by it.  An
error path that cannot describe its error is worse than none: it looks like one.

`ConsolidationResult` has an `error` field now, the engine fills it from the exception, and
`_coerce_consolidation_result` describes the one failure it can see (a consolidator that
answered with something that is not a result).  The test drives the real path --
`process_one_job` against a client that raises -- and asserts the *event*, because that is
where the log line comes from.

## The scoped view is for contributing, and the agent does not contribute

Reported as "the web chat does nothing at all".  Reproduced with a WebSocket client against
a running gateway: sending a message returned

```
{"type":"turn_complete","text":"","tool_calls":[],"turn_id":"m1"}
```

-- one empty completion, immediately, and nothing else.  No error, no text.  The server log
had the answer:

```
component=agent event=turn_started ...
component=agent event=turn_finished status=ok steps=0 content_len=0 duration_ms=0.6
component=channel_runner event=turn_failed error=set_context
```

A stack trace, obtained by instrumenting `ScopedTools` to print one:

```
turn_loop.py:341  prepare_turn   deps.registry.set_context("turn_request", user_message)
```

`agent_plugin` constructed the agent with `ctx.get("registry")`, and `Context.get` hands an
injectable service to a plugin through its per-plugin **view** when the service offers one --
`ToolRegistry` does.  That is exactly right for `ToolsPlugin`, whose registrations must come
back out when it unloads.  The agent contributes no tools: it *configures* the registry
(``turn_request`` on every turn, read back by the tool implementations), so the view was the
wrong object.  `prepare_turn` raised before any model call, which is why the symptom was
silence rather than an error anybody could read.

Fixed by resolving the service itself (`ctx.services.get("registry")`) while keeping
`registry` in `inject` -- "must not activate before it exists" and "which object it is
handed" are separate statements.  Verified end to end: `stream_chunk` then `turn_complete`
with the text, and `simple chat "..."` printing an answer.

**Why no test saw it: the stand-in was simpler than the real thing.**  `test_agent_plugin`'s
mapping held `"registry": object()`, which has no `scoped` method -- so `ctx.get` returned it
unchanged and the double could not tell a view from a service.  That test now uses a double
that offers `scoped` *because the real one does*, and a composition-level test
(`test_the_composed_agent_holds_the_composition_registry`) asserts the identity against the
real `ToolRegistry`.  Both fail if `agent_plugin` goes back to `ctx.get`.

The wider check, since this is a class of mistake rather than one mistake: the services that
offer a view are `registry`, `agents`, `commands`, `channels`, and every other consumer of
them is a *contributor* (`ctx.commands.add`, `ctx.agents.add`, `ctx.channels.add`,
`ctx.tools.register`).  The command modules resolve `registry` from the component mapping, so
they were always getting the service.  `agent_plugin` was the only configuration-side
consumer, and it was the only one wrong.

## The functional layer, and what it found in its first hour

Three defects in a row were invisible to a green suite of 3,600 tests, for one reason: the
tests that looked like they covered the ground replaced the thing that was broken.
`test_agent_plugin` handed over a registry double with no ``scoped`` method, so it could not
tell a view from a service; the CLI integration tests replace ``agent.send_message``, so the
turn loop they "drive" never ran.  Both times the suite said the wiring was fine.

`tests/test_functional_turns.py` drives the product instead: the real composition root, the
real catalog and plugins, the real agent, ``LoopDeps``, the turn loop, tool dispatch, the
command coordinator, the channel, the CLI command -- over a real socket to a local
OpenAI-compatible endpoint whose *answers* are scripted.  Six paths:

| Test | What it drives |
|---|---|
| `test_cli_chat_answers` / `..._sends_the_configured_model` | `simple chat`, end to end |
| `test_a_tool_call_goes_out_and_its_result_comes_back` | two model calls, tool result in the second request |
| `test_the_web_socket_streams_a_reply` | uvicorn + WebSocket + coordinator + per-session runtime |
| `test_a_user_plugin_contributes_a_tool_and_hears_the_turn` | discovery, scoped registration, tool call, `ctx.on(TurnCompleted, ...)` |
| `test_reloading_a_plugin_runs_its_new_code` | an edit with the same size *and* mtime -- the `.pyc` trap |
| `test_switching_provider_rebuilds_the_session_runtime` | the settings page's `activate`, then the next turn |
| `test_a_session_turn_writes_into_the_sessions_own_output_dir` | the per-session runtime, asserted by where the file lands |
| `test_a_turns_transcript_is_recorded_and_served_to_a_reconnecting_client` | the durable record a refreshed page reads |
| `test_a_scheduled_run_executes_a_turn_and_records_the_answer` | the unattended path: runtime factory, a real turn, the recorded run |
| `test_a_spawned_sub_agent_runs_with_its_own_restricted_tool_set` | a derived container: the sub-agent's request offers fewer tools, and no spawner |
| `test_unloading_a_plugin_takes_its_tool_with_it` | load and unload inside a **running** composition, a turn either side |

**What the last one found, immediately.**  Switching provider rebuilt the session runtime
correctly -- the log showed the new active provider and the new endpoint -- and the next
message still went to the **old** provider.  The session's ``AgentCore`` resolved
``agent_loop`` from its container, and the container inherits the composition's service by
copying the mapping, so every session turn ran through a loop **bound to the composition's
container** and resolved the *composition's* ``agent``.

That is a regression from making the loop a plugin (`24e8de1`).  The code before it had this
rule:

    turn_runner = resolve(self._components, "turn_runner")
    if turn_runner is None or isinstance(turn_runner, TurnRunner):
        return TurnRunner(self._components)     # bound to THIS container
    return turn_runner                          # a composition's own loop, verbatim

The ``isinstance`` check was doing real work: a stock loop carries the container it was bound
to, so it has to be re-bound to the container asking.  The plugin version dropped it and used
any service it found.  The consequences were wider than the provider switch: a web session
has **its own** agent, registry fork (workspace root, file policy, output dir, project memory
scope) and model settings, and none of them were reached by its turns -- a session's
isolation was decorative.

Fixed by restoring the distinction: a loop that is not a stock ``TurnRunner`` is used
verbatim (the substitution point), a stock one is re-bound per container.  Two unit guards in
`tests/test_runtime_contracts.py` pin both halves, and the functional test pins the
user-visible one.

**The lesson, stated so it can be applied**: a green suite is a statement about the tests, not
about the product.  Where a test replaces a collaborator, ask what the real collaborator has
that the double lacks -- a method, a binding, a container -- because that difference is
exactly where the bug will be.

## Two runs the layer did *not* have to fix

Worth recording, because "the functional layer found two bugs" is not the same claim as
"everything it drove was broken".  These two were already right, and both are now pinned
where nothing pinned them before:

**The durable transcript.**  A turn's user message and the reply are recorded and served
back by ``GET /api/sessions/<id>/messages`` -- the path a refreshed page reads.  Both halves
of the ``SessionLog`` change are exercised in one turn, through the product.  This is a place
the project has been wrong before (a comment in ``channels/base.py`` describes tool traces
vanishing on refresh), which is why it is worth a test rather than an assumption.

**A scheduled run.**  A task due immediately runs a real turn -- built by the scheduler's
runtime factory from the config on disk, so a genuinely different entry point -- reaches the
provider, and records its outcome.  A run has nobody watching it, so the store is the only
place its result exists: the test asserts on ``TaskRun.summary``, not on a console.

**A sub-agent.**  The parent asks to spawn one, the sub-agent answers, the parent answers --
three model calls.  The evidence that the *derived* registry was used is in what the middle
request offered: no ``spawn_agent``, and a shorter tool list.  A sub-agent that inherited the
parent's registry would be able to spawn, which is the recursion the exclusion exists to
prevent.

**Unloading a plugin in a running composition.**  A turn, then ``unload_plugin``, then
another turn: the plugin's tool is gone from what the model is offered, the count drops by
exactly one, and the composition still answers.  A fresh process per turn -- which is what
the CLI tests do -- cannot show this, and "the plugin is the unit of removal" is the promise
it checks.

Both are the shape the plan calls for: the plugin supplies the capability, and the
assertion is made where the user would look.

## The per-call context merge was never installed

The second thing the functional layer found, from the session-isolation test: a web
session's tools wrote into the **composition's** ``output_dir`` instead of the session's.

``ToolRegistry.call`` has a merge for exactly this: when the tool's owner registry is not
the registry being called through, it builds ``owner_context | caller_context`` and installs
it as a per-call ContextVar override **on the owner registry**, because that is the object
the tool implementation reads its context from:

    owner_registry = getattr(owner, "registry", None)
    if isinstance(owner_registry, ToolRegistry) and owner_registry is not self:
        override_token = owner_registry._context_override.set(merged)

After the built-in tool set became a plugin, the tool object holds ``ctx.get("registry")`` --
a ``ScopedTools`` **view**, which is what makes its 44 registrations removable.  A view is
not a ``ToolRegistry``, so the condition was false, no merge was installed, and every context
read a tool made returned the **composition's** value.  The tools read a lot through that
path: ``workspace_root``, ``output_dir``, ``file_access_policy``, ``write_scope``, the four
``shell_*`` policies, ``skill_catalog``, ``mcp_client``.

So a web session's own workspace, output dir and file policy were decorative -- and had been
since the tools-plugin round, with the whole suite green.  The same reads are what a
sub-agent's ``capability_profile``/``write_scope`` live in, which is why this was treated as
possibly a security hole; **measured, it was not**, and the reason is worth keeping:

``_file_mutation_authorizer`` is handed the **calling** registry by ``ToolRegistry.call``
rather than reading its own, so the write-scope gate always saw the caller's scope no matter
what was installed on the owner.  That asymmetry -- two paths that both read ``write_scope``,
one of which cannot be broken this way -- is now pinned by a control test, because a reader
who notices the first will reasonably suspect the second.

The fix keeps the view and makes it faithful where a tool implementation needs it:
``ScopedTools.registry`` (the registry behind the view, so ``call`` can recognise its owner)
and ``ScopedTools._context_override`` (the variable tools read, under the name they read it
by).  Not a general passthrough: what a contributed tool may read through a view should be a
short list somebody maintains.

## Not built, and why

Re-measured against a DSH-style plan (Cordis kernel / typed events / profiles / per-agent
scope), the deltas that remain are choices rather than gaps:

- **Per-agent registration of *services*** does not exist, and should not: a service is a
  composition-level thing, while per-agent variation is *selection* -- `ToolRegistry.derive`
  with a `ToolView` per capability profile, and a composed sub-system prompt.  Making
  `llm` per-agent would break the two-agents-one-composition case the suite pins.
- **Typed durable events** -- the durable side is `SessionLog` (one append path) plus the
  `EventCollector` log, and the live side is these buses.  They are separate mechanisms on
  purpose and the split is asserted; giving the durable side its own event *classes* would
  add a second vocabulary for something that is already one call.
- **`Runtime`** exists only as `agent/runtime/`: DTOs, locks, a heartbeat and a factory.
  There is no privileged runtime object to delete.
