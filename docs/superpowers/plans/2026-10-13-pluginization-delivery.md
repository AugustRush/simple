# Pluginization: the shape, the map, and how to extend it

**Status:** delivered **and enforced**.  Every service the composition declares has a
written contract (15/15), every contribution kind is either a capability service or a
floor with a stated reason, and adding an ecosystem or replacing an implementation no
longer edits the loader or the composition root.  The difference between "delivered" and
"enforced" is the round recorded in
[`2026-10-15-pluginization-hard-enforcement-closing-report.md`](2026-10-15-pluginization-hard-enforcement-closing-report.md):
each claim below is now refused by something, rather than being a convention an author
had to overhear.  Numbers in this document were re-measured on 2026-10-15.

This document is the entry point.  The reasoning behind the shape is in
[`2026-10-13-capability-services-and-contribution-shape-design.md`](../specs/2026-10-13-capability-services-and-contribution-shape-design.md);
the earlier rounds are in
[`2026-10-12-pluginization-and-adapters-status.md`](2026-10-12-pluginization-and-adapters-status.md).

## The four layers

1. **Kernel** (`agent/kernel/`) -- `Plugin` (`name`/`inject`/`setup`), services, events,
   scopes.  A plugin declares what it needs; the kernel holds it *pending* until those
   services exist and unwinds everything it registered through its scope.  Nothing here
   knows what a skill or a channel is.
2. **Capability services** -- one per contribution kind, all the same shape:
   a registry that owns the records, a per-plugin **view** whose contributions are
   attributed and unwound, and a protocol.  The view half is now literally shared --
   `ScopedContribution` (the two fields, `owner`, and the one `_contribute` step that
   must not be re-implemented) plus the `ScopedService` protocol it satisfies -- rather
   than written out six times, which is how two of the six had got the undo wrong.
3. **Adapters** (`agent/plugins/formats.py`, and the loader's `_CatalogPlugin`) -- turn
   an ecosystem's directory layout into a **plugin object**.  A declarative bundle *is* a
   plugin whose `setup` an adapter writes.
4. **Plugins** -- code plugins and declarative bundles.  They contribute only through
   injected services, in their own `setup`.

## The map

`agent/runtime/services.py` is **the** declaration: one entry per name the composition
resolves out of its mapping (68 today), each saying whether a *plugin* may touch it, what
protocol an implementation must satisfy when there is one, and -- for the composition's own
-- why.  There used to be two modules here, and that was the problem: "is this name a
service?" had two answers, they could each grow without the other noticing, and the
`memory` entry pointed at the wrong protocol for months because nothing asked.

**What is not in the table is the point.**  Every entry used to declare `owners` (who
writes it) and `consumers` (who reads it) as well.  Both are *derivable from the source*,
so declaring them made the table a hand-maintained mirror of the code, kept in step by a
1900-line AST scanner and four assertions whose only job was to notice when it fogged.  The
scanner is still there -- it is how the guards compute the answer -- but the answer is no
longer copied into the declaration.  That direction cannot drift: the code is the truth and
the table is the policy.

| plugin-provided (11) | floor (4) |
|---|---|
| `registry`, `skill_catalog`, `agent`, `agent_loop`, `llm`, `context_manager`, `ltm_store`, `memory`, `channels`, `mcp_client`, `contributed_mcp_servers` | `plugin_catalog`, `agent_core`, `commands`, `agents` |

A **floor** is a service that cannot be provided by a plugin, and each one says why in
`tests/test_service_ownership.py`'s `FLOORS` (the loader cannot be provided by what it
loads; the registries it writes into *while loading* must exist before the first plugin
activates and are restored by the reload rollback).  A floor still has a protocol.

**A floor is about the registry, not about the contribution.**  `commands` and `agents`
are floors because the *loader* has to own those two registries -- but the things that go
*into* them are contributed by the plugin whose files declare them, through
`ctx.commands` / `ctx.agents`, exactly like a skill root or an MCP config.  Conflating the
two is what left the markdown adapter writing to the raw registry, and therefore left
these two names published with no in-tree user; they now have one.

Contribution kinds that are **not** services, with the reason:

* **hooks** -- the config sits on the contributing plugin's own `PluginMeta` (there is no
  central list to drift from) and its only reader is the loader, which *dispatches* it.
  A service there would have exactly one caller: the thing that provides it.  The
  dispatch itself now lives in `agent/plugins/hook_dispatch.py`, and the boundary between
  it and the kernel's typed events is written down in `LOADER_HOOK_BOUNDARY` (each string
  hook names the typed event it mirrors, or says why it has none).
* **prompts** -- plugins append to the system prompt through the loader that composes it
  (`compose_all_prompts`), which is a read of the plugin tree rather than a collection.

## The rules that turned out to be load-bearing

1. **A view is the service plus attribution, not a narrower service.**  Reads pass through
   (`ScopedSkillCatalog.__getattr__`); only the contribution calls the view owns are
   attributed.  *Measured:* the moment `skill_catalog` grew a `scoped()`, the kernel's
   `ctx.get("skill_catalog")` began returning the view, and ralph -- which only looks a
   skill up by id -- died with `'ScopedSkillCatalog' object has no attribute 'get'`.
2. **A service whose consumer computes a value from it in `setup` cannot be provided by
   that consumer.**  The MCP client builds the merged server list in its own `setup`, so
   the registry has a plugin of its own, provided first; the client declares it in
   `inject` and is activated last, after the contributors.
3. **A service name that is also a config or asset key makes every scan wrong.**  The
   capability is named for what it holds (`contributed_mcp_servers`, because
   `mcp_servers` is the MCP config's own field), and the ledger scanner counts a dict
   literal as a provision only when it *is* a component mapping (returned, merged with
   `update`, or assigned to a components-named target).
4. **A floor is a decision, not debt.**  It gets a written interface and a reason that
   says why a plugin *cannot* provide it.
5. **A contribution is a record, not a set of effects.**  The skill catalog replays its
   contributed roots over its intrinsic layers, so two contributors touching one skill id
   cannot make one's removal restore the other's stale bundle.
6. **A declaration nobody enforces is documentation.**  `inject` was advisory (`ctx.get`
   read the registry directly), `ctx.services` handed out the raw registry without an
   undo, and two of the six undos removed by *name* or *source prefix* rather than by
   identity -- so a plugin's unload could delete another plugin's tool (`plugin:foo` is a
   prefix of `plugin:foobar`) or another plugin's command.  All three are refusals now.
   The guard for this class of claim is a test that goes red when the enforcement is
   removed: `tests/test_kernel.py`'s declaration tests,
   `tests/test_contribution_attribution.py`, `tests/test_service_vocabulary.py`.

## How to extend

* **A new ecosystem** (a plugin format): add one `PluginFormat` to
  `agent/plugins/formats.py` -- manifest paths, marketplaces, hook/MCP file names,
  markers, environments, **and its strategies** (`normalize_hook_entries`,
  `translate_hook_output`, `substitute_command_args`), which is what makes the loader
  stop knowing an ecosystem's shapes.  Every strategy has a default, so a format that
  speaks this project's own conventions supplies none.
  `scripts/plugin_ecosystem_probe.py` is the acceptance test, and a fourth format defined
  only in `tests/` proves the loader needs no edit --
  `tests/test_plugin_format_strategy.py` goes further and proves a *strategy* is what runs.
* **A new contribution kind**: give it a capability service (registry + `scoped(ctx)`
  view) + a protocol + a ledger entry + a vocabulary entry if plugins may touch it + a row
  in `tests/test_service_contracts.py` and `SCOPED_VIEWS`.  Subclass `ScopedContribution`
  -- the guard refuses a hand-written `Scoped*` view.  A **declarative** kind (one a
  bundle's *files* declare) is one row in `DECLARATIVE_KINDS`, which carries both the
  service it goes through and how it registers, so the `inject` requirement and the
  registration cannot disagree.
* **A new implementation of a service**: satisfy its protocol and point a profile at it
  (`service_replacements`).  The memory layer is the worked example: two stores and two
  context services, with `run_store_contract` / `run_context_contract` as the shared
  behavioural suites, chosen by profile
  (`sqlite-fts-memory`, `record-only-context`).
* **A new service**: add the ledger entry with owners, consumers and a contract; the
  guards check it in both directions (a declared owner must write it, every reader must
  be declared) and the ownership guard refuses an unowned service without a stated reason.
  A plugin that *reads* it also has to declare it in `inject`/`optional` **and** find the
  name in the vocabulary -- the second check is what keeps composition internals out.

## How it is checked

| check | what it refuses |
|---|---|
| `tests/test_core_services.py` | a declared name the source never writes; a name the source uses that is not declared; a reserved entry with no reason; a declaration that has grown back a second copy of the names |
| `tests/test_service_ownership.py` | a service that is neither plugin-provided nor a written floor |
| `tests/test_service_contracts.py` | an implementation that does not satisfy its protocol (signatures, async-ness, data members), a protocol member nobody calls, a ledger pointer no file checks, a waiver pointing at a check that does not exist, a hand-written `Scoped*` view |
| `tests/test_memory_contract.py` | a backend that fails the behavioural suites; a reader that reaches past the contract (an allowlist of 4 documented soft reads remains) |
| `tests/test_kernel.py` | a plugin that reads or provides a name it never declared; an `optional` read that does not join the unload cascade |
| `tests/test_contribution_attribution.py` | an undo that reaches past its own contribution (a name- or prefix-keyed removal) |
| `tests/test_service_vocabulary.py` | a plugin touching a name outside the vocabulary; a vocabulary entry no plugin uses; a wired policy that does not refuse |
| `tests/test_plugin_format_strategy.py` | an ecosystem's shapes back in the loader; a format whose strategy is declared but never called |
| `tests/test_plugin_catalog.py` | a declarative kind whose `inject` requirement and registration disagree; a registration body that names a kind instead of the table |
| `tests/test_plugin_hook_dispatch.py` | a dispatcher missing from `LOADER_HOOK_BOUNDARY`; a boundary entry naming a typed event that does not exist |
| `tests/test_plugin_catalog.py` | a rolled-back reload that rebinds the plugin table instead of mutating it |
| `scripts/plugin_ecosystem_probe.py` | an ecosystem whose assets the loader does not find, or an inert hook that is reported as working |
| `scripts/functional_smoke.py` | a composition that cannot answer a turn end to end |

**Measured on the delivered tree (2026-10-15):** full suite **19 failed / 4116 passed /
2 skipped / 0 errors**, and **every one of the 19 is a thing this machine cannot do** --
12 `sandbox-exec` refusals under this file policy, 3 console-width probes in a
non-interactive harness, and 4 more from the same sandbox cause.  That property is the
point: a baseline is a list of environment limits, and two entries were removed from it by
fixes rather than by an environment change (a real cross-loop teardown bug in
`MCPClient.close`, and a test that had stopped staging its own race under Python 3.14).
Smoke **6 ok / 0 failed**; ecosystem probe **0 failure(s)**; declaration **67 names, 43
public / 24 reserved, 15 with contracts**.

## Backlog (recorded, not blocking)

1. **Provenance / translators** -- designed in
   [`2026-10-12-capability-provenance-and-adapter-translators-design.md`](../specs/2026-10-12-capability-provenance-and-adapter-translators-design.md).
   **All four steps have shipped** (step 1 in a corrected shape): every plugin carries a `Provenance`
   (`agent/runtime/provenance.py`) recorded where the facts are, and a capability's origin is
   *derived* through `who_provides`, so the composition can now answer "where did this come
   from" for every name it holds -- guarded by a test that asks the real composition.
   Step 2 shipped too: `formats.executions` says what a plugin would *run* (including the
   concrete command, read from its own files) and `CapabilityReport` is the one structure
   every adoption path reads, with the installer gating and wording from that single scan.
   Step 3 shipped as a re-keying rather than a new store: the durable ledger already
   existed (`agent/security/consent.py`), so a durable grant for an executable plugin is now
   keyed by a digest of what would run -- `plugin:<source>@<digest>` -- while the question is
   still shown under the source.  Editing a hook command, an MCP server's command, or a
   Python entry point's bytes asks again; moving the directory does not.
   Step 4 shipped too: `import_dsh_profile` reads another application's profile, reports the
   stdio commands it declares, asks through the same digest-keyed rule, and writes a native
   plugin directory the loader already contributes and attributes.

   The load path is covered too, as **change detection** rather than an approval gate (the
   ledger shares a trust domain with the plugin directory, so a gate there would be
   unenforceable against the actor it targets).  A user plugin whose content was approved or
   has run here before loads; the machine's first look lists and records what is present;
   anything else does not load and is reported with what it would have run.

   **Open, and a real workflow cost:** a plugin *author* editing their own plugin in
   `~/.agent/plugins/` looks exactly like a sync tool updating files behind them, so the edit
   does not load until it is approved or observed again.  The escape hatch -- a directory the
   user names as their own, trusted by *path* rather than by content -- is not built.  It is a
   decision about what "mine" means (a config list, or a ledger rule the user grants), which
   is why it is a separate step rather than an implementation detail.  Until those
   exist the *trust* half is not met: a plugin in `~/.agent/plugins/` is still executed at
   startup on the strength of being there.
2. ~~**The 4 remaining soft reads** (`tests/test_memory_contract.py`)~~ -- **down to 2, and
   the description of them was wrong.**  Measured rather than assumed: two of the four were
   *not* name collisions but real reads of a `ContextManager`'s `store`, waived under
   `MemoryService`'s name because the allowlist was keyed by `(file, attribute)` and the three
   protocols share member names.  Both are now closed at the call site -- `channels/web.py`
   and `commands/builtin.py` resolve the declared `ltm_store` service instead of reaching
   through the context manager (the same object; every implementation builds its manager from
   it), so the store is reached as a service *with a contract*.  The allowlist is keyed by
   **protocol** as well now, so a future waiver cannot silently cover two interfaces.  What
   remains is two reads that are soft *by design*: `LTMStore.dir` (the context directory, which
   the contract does not expose) and `ContextManager.consolidation` (the second
   `ContextService` implementation has no engine at all, so declaring it would delete the
   no-engine path rather than narrow anything).  The protocol docstring that named two modules
   which had long since stopped reading `store` is corrected.


3. ~~**`MemoryPalace()` at 4 CLI sites**~~ -- **done**: the read-only `memory` commands go
   through `agent.bootstrap.memory_service(cfg)`, which mounts the composition's own
   declaration of that name (and only that name -- no provider, no API key, no session), so
   a `service_replacements` pointing `memory` elsewhere reaches them.  Pinned by
   `tests/test_memory_backend_selection.py`.
4. **DSH profile import** -- reading is done (`agent/plugins/dsh.py`, read-only); importing
   needs the consent machinery item 1 describes.
5. ~~**A kind table for declarative assets**~~ -- **done**: `DECLARATIVE_KINDS` is the
   table, and `_register_declarative_assets` only iterates it.
6. ~~**`PluginCatalog` still owns two concerns it should not.**~~ -- **done**: the hook
   surface moved to `hook_dispatch.py` and the reload transaction to `reload.py`
   (2432 -> 2052 lines, 75 -> 39 methods).  The transaction owns its snapshot and rollback;
   the loader keeps the lock, because that serializes *this instance's* reloads.  Splitting
   it also settled what a reload actually guarantees: it is atomic about the two registries
   the loader owns, and **not** about plugin activation -- a failure after discovery leaves
   the newly discovered tree with the previous registries, and the class says so rather
   than re-discovering from a disk that is usually what broke.  The old rollback rebound
   the plugin table, which the hook dispatcher holds by reference; that is fixed and
   `tests/test_plugin_catalog.py` pins it.
7. ~~**Two overlapping declarations.**~~ -- **done**: `agent/runtime/services.py` is one
   table, and `owners`/`consumers` were **deleted** from it rather than merged, because both
   are derivable from the source -- declaring them is what made the old ledger a
   hand-maintained mirror.  The scan that computes them also found four names that had been
   invented (config keys and a local variable, not component keys) and two scanner blind
   spots (an annotated dict literal, and a key computed by `bus_name`).
9. **`post_turn_maintenance` was removed, not fixed.**  It was a published name with no
   contract and no provider, so a replacement had to reproduce eleven keyword-only
   parameters from reading a private method.  If a composition ever wants to own the
   post-turn step, it should come back with a contract and a provider together.
10. ~~**Two vocabulary entries have no in-tree user.**~~ -- **done**: the markdown adapter
   contributes through `ctx.commands` / `ctx.agents`, so `FOR_THIRD_PARTIES` is empty and
   the last raw-registry bypass on the contribution path is gone.  (The registries are
   still floors *and still provided by the loader*; only the writing moved.)
9. **Source-scanning guards, and when that is right.**  Several new guards assert on
   source (`DECLARATIVE_KINDS`'s "the body only loops", the loader's "no ecosystem shape
   literals").  They are checking an **absence**, which no runtime assertion can see -- but
   the line is worth stating, so that reading the source does not become the default way to
   check a property a type or a refusal could check instead.
