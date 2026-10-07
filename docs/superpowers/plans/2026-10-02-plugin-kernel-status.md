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
| 2 Service Injection | **2 of 3 done** | ToolRegistry ✅ `registry`, Session ✅ `context_manager`, **Model ✗ — see below** |
| 3 Agent Loop Plugin | **first step done** | `agent_loop` is a plugin with declared `inject`; the loop *body* is still in `BaseAgent` — blocked on Phase 2's Model piece |
| 4 Capability Layer | mechanism ready | the scoped service view is the mechanism it needs; `PluginAssets` is its first user |
| 5 Execution Plugins | not started | Workflow / Ralph / Orchestration / Scheduler |
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

## Measured blocker: the Model adapter cannot become a service yet

Phase 2 names three services — ToolRegistry, Session, Model. The first two are
done. The third was attempted and **reverted**, and the measurements below are the
reason. They are recorded here because the attempt is what produced them.

What exists already: `ModelTransport` is an ABC with three implementations
(`AnthropicTransport`, `OpenAITransport`, `RoutingTransport`), and `BaseAgent`
takes one by constructor argument. So the *seam* is real. What is missing is that a
composition cannot replace it without editing the code that builds the agent.

Two obstacles, both structural rather than mechanical:

1. **The composition mapping does not exist when the global agent is built.**
   `bootstrap` constructs the global `BaseAgent` around line 800 and assembles the
   `components` mapping around line 973. Passing the mapping to the agent is
   therefore impossible without reordering the composition root — and the mapping
   is also what the plugin catalog's kernel *adopts*, so reordering it moves where
   plugins become visible.

2. **The adapter is assigned directly in 8+ places** (`agent._transport = fake`),
   all of them in tests that swap in a stand-in after construction. Making
   `_transport` a resolving property removes the setter and breaks them.

   Measured fallout of the attempt: 24 failures from `components` being undefined
   at the construction site and 17 from the missing setter.

If the next attempt keeps the property, those 8 sites need a deliberate answer:
either a setter that assigns the fallback (two ways to set the adapter) or the
tests building a composition instead. The second is the honest one and is more
work; the first reintroduces exactly the ambiguity the container is meant to
remove.

## Measured blocker: the loop body cannot move out of `BaseAgent` yet

`_run_turn_loop` (415 lines) and `send_message` (110 lines) touch 15 private
helpers plus `max_tool_call_iterations`. Measured coupling of those helpers:

- only **two** are genuinely self-contained (`_is_content_filter_block`,
  `_new_watchdog_state` — 3 lines each, not worth extracting);
- **six** reach the transport (`_assistant_message`, `_handle_end_turn`,
  `_parse_response`, `_recover_from_content_filter`,
  `_recover_incomplete_tool_response`, `_tool_result_messages`);
- the rest reach the context manager, the registry, or both.

So the loop's real dependency is the composition's model and session pieces, which
is the same blocker as above. Per the plan's own ordering, Phase 2's Model work is
the prerequisite — not an optional detour.

### A measurement bug worth remembering

The first version of that scan printed only the attributes starting with `_` and
labelled an empty set "(nothing)". It reported `_inject_pending_interjections` as
having no coupling; the function in fact ends with `self.registry.set_context(...)`.

The output could not distinguish "no dependencies" from "dependencies exist but none
were shown" — and the plan was briefly to extract a function on the strength of it.
Any scan used to justify a refactor has to print the whole set it is testing for,
or it is a validation that cannot fail.

## Next step, in order

1. **Make the model adapter a service.** Resolve obstacle 1 by giving the
   composition root its mapping earlier, or by making the agent's adapter lookup
   lazy in a way that does not need the mapping at construction. Resolve obstacle 2
   by moving the 8 test sites onto a composition rather than an assignment.
2. **Then** move the loop body: with `llm` and `context_manager` as services, the
   six transport-reaching helpers become the loop's own dependencies and
   `_run_turn_loop` can move into the `agent_loop` plugin.
3. **Then** Phase 4's capability layer, on the scoped view that already exists.
