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
| 1 Plugin Kernel | **done** | `agent/kernel/` — Context / Plugin / Service / Event / Scope / Lifecycle, 53 tests in `tests/test_kernel.py` |
| 2 Service Injection | **done** | ToolRegistry ✅ `registry`, Session ✅ `context_manager`, Model ✅ `llm` |
| 3 Agent Loop Plugin | **done** | `agent_loop` is a plugin with declared `inject`; the loop **body** is in `agent/core/turn_loop.py`. `BaseAgent` keeps the facades tests reach |
| 4 Capability Layer | **done** | tools / commands / sub-agent roles are each a service with `scoped(ctx)`; `PluginAssets` -- a contribution service no plugin called -- was deleted |
| 5 Execution Plugins | **2 of 4 done, 2 have no separable boundary** | Ralph ✅ and Scheduler ✅ mounted as plugins; Orchestration / Workflow measured, see below |
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
