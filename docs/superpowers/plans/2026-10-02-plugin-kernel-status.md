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
| 5 Execution Plugins | **1 of 4, and 2 of the 4 have no separable boundary** | Ralph ✅ mounted as a plugin; Scheduler / Orchestration / Workflow measured, see below |
| 6 Channel Plugins | not started | CLI / Web / Feishu |
| 7 Profile / Bundle / patch | partial | profiles and bundles exist from earlier work; patch semantics not unified |

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

## Next step, in order

1. **The Scheduler as a plugin.**  Its parameters are single-sourced now
   (`agent/scheduler/settings.py`), which was the first prerequisite.  The second is
   `setup` being synchronous: `_build_scheduler_service` is `async`, so the plugin either
   needs a synchronous builder or starts a task.  It also returns a triple --
   `(service, store, components)` where `components` is non-`None` only when the builder
   built its own -- and that flag exists to tell the caller **who tears down**.  A scope
   answers that question, so the plugin should own all three and the `finally` block
   that closes them by hand should go.
2. **Phase 6, the channel plugins** (CLI / Web / Feishu) -- measured next, and for the
   same reason Phase 5 was: to find out which of the three is genuinely a component.
3. **`BaseAgent` is still 2,530 lines** and still holds the sub-agent machinery, the
   tool-execution entry points and the streaming entry point. Those are the next
   things to leave it -- each is a plugin by the plan's table, and none is blocked.
