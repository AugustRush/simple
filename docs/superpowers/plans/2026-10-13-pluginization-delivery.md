# Pluginization: the shape, the map, and how to extend it

**Status:** delivered.  Every service the composition declares has a written contract
(15/15), every contribution kind is either a capability service or a floor with a stated
reason, and adding an ecosystem or replacing an implementation no longer edits the loader
or the composition root.

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
   attributed and unwound, and a protocol.
3. **Adapters** (`agent/plugins/formats.py`, and the loader's `_CatalogPlugin`) -- turn
   an ecosystem's directory layout into a **plugin object**.  A declarative bundle *is* a
   plugin whose `setup` an adapter writes.
4. **Plugins** -- code plugins and declarative bundles.  They contribute only through
   injected services, in their own `setup`.

## The map

`agent/runtime/core_services.py` is the ledger: service -> owners, consumers, contract.

| plugin-provided (11) | floor (4) |
|---|---|
| `registry`, `skill_catalog`, `agent`, `agent_loop`, `llm`, `context_manager`, `ltm_store`, `memory`, `channels`, `mcp_client`, `contributed_mcp_servers` | `plugin_catalog`, `agent_core`, `commands`, `agents` |

A **floor** is a service that cannot be provided by a plugin, and each one says why in
`tests/test_service_ownership.py`'s `FLOORS` (the loader cannot be provided by what it
loads; the registries it writes into *while loading* must exist before the first plugin
activates and are restored by the reload rollback).  A floor still has a protocol.

Contribution kinds that are **not** services, with the reason:

* **hooks** -- the config sits on the contributing plugin's own `PluginMeta` (there is no
  central list to drift from) and its only reader is the loader, which *dispatches* it.
  A service there would have exactly one caller: the thing that provides it.
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

## How to extend

* **A new ecosystem** (a plugin format): add one `PluginFormat` to
  `agent/plugins/formats.py` -- manifest paths, marketplaces, hook/MCP file names,
  markers, environments.  `scripts/plugin_ecosystem_probe.py` is the acceptance test, and
  a fourth format defined only in `tests/` proves the loader needs no edit.
* **A new contribution kind**: give it a capability service (registry + `scoped(ctx)`
  view) + a protocol + a ledger entry + a row in `tests/test_service_contracts.py`.  The
  loader does not change; a bundle contributes it in its own `setup` through `inject`.
* **A new implementation of a service**: satisfy its protocol and point a profile at it
  (`service_replacements`).  The memory layer is the worked example: two stores and two
  context services, with `run_store_contract` / `run_context_contract` as the shared
  behavioural suites, chosen by profile
  (`sqlite-fts-memory`, `record-only-context`).
* **A new service**: add the ledger entry with owners, consumers and a contract; the
  guards check it in both directions (a declared owner must write it, every reader must
  be declared) and the ownership guard refuses an unowned service without a stated reason.

## How it is checked

| check | what it refuses |
|---|---|
| `tests/test_core_services.py` | a ledger that disagrees with the source, in either direction; a service whose name is not documented |
| `tests/test_service_ownership.py` | a service that is neither plugin-provided nor a written floor |
| `tests/test_service_contracts.py` | an implementation that does not satisfy its protocol (signatures, async-ness, data members), a protocol member nobody calls, a ledger pointer no file checks |
| `tests/test_memory_contract.py` | a backend that fails the behavioural suites; a reader that reaches past the contract (an allowlist of 4 documented soft reads remains) |
| `scripts/plugin_ecosystem_probe.py` | an ecosystem whose assets the loader does not find, or an inert hook that is reported as working |
| `scripts/functional_smoke.py` | a composition that cannot answer a turn end to end |

**Measured on the delivered tree:** full suite **19 failed / 3945 passed / 2 skipped / 0
errors** -- the 19 are a frozen baseline of environment failures (`sandbox-exec` is not
permitted here, console-width probes) recorded before this work began and unchanged by it;
smoke **6 ok / 0 failed**; ecosystem probe **0 failure(s)**; ledger **15/15 contracts**.

## Backlog (recorded, not blocking)

1. **Provenance / translators** -- designed in
   [`2026-10-12-capability-provenance-and-adapter-translators-design.md`](../specs/2026-10-12-capability-provenance-and-adapter-translators-design.md),
   not implemented.  It answers "where did this capability come from, and may it be
   trusted", which is a *policy* layer above the plugin tree.
2. **The 4 remaining soft reads** (`tests/test_memory_contract.py`): the token budget's
   estimator reads (a designed fallback, not a hole), the web's session-service store
   (a decision about `SessionService`), and two name collisions the scan cannot tell
   apart from `LTMStore` members.
3. **`MemoryPalace()` at 4 CLI sites** (`agent/cli.py`) -- those commands build a palace
   directly with no composition in scope, so a *replaced* memory service is unreachable
   from them.
4. **DSH profile import** -- reading is done (`agent/plugins/dsh.py`, read-only); importing
   needs the consent machinery item 1 describes.
5. **A kind table for declarative assets** -- `_register_declarative_assets` is a sequence
   of per-kind blocks; a table would make "which kinds exist" declarative.  Optional: the
   kinds are *ours*, not an ecosystem's, so an ecosystem cannot invent one.
