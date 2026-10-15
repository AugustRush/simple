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

`agent/runtime/core_services.py` is the ledger: service -> owners, consumers, contract.
`agent/runtime/service_vocabulary.py` is the **second** declaration, and answers a
different question: which names a *plugin* may touch at all (44 public; 27 reserved, each
with the reason it is the composition's).  The ledger describes the services that have
contracts; the vocabulary is the closed set the kernel's policy enforces.  They overlap
and neither implies the other -- `agent_core` is in the ledger and deliberately not
plugin-visible.

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
| `tests/test_core_services.py` | a ledger that disagrees with the source, in either direction; a service whose name is not documented |
| `tests/test_service_ownership.py` | a service that is neither plugin-provided nor a written floor |
| `tests/test_service_contracts.py` | an implementation that does not satisfy its protocol (signatures, async-ness, data members), a protocol member nobody calls, a ledger pointer no file checks, a waiver pointing at a check that does not exist, a hand-written `Scoped*` view |
| `tests/test_memory_contract.py` | a backend that fails the behavioural suites; a reader that reaches past the contract (an allowlist of 4 documented soft reads remains) |
| `tests/test_kernel.py` | a plugin that reads or provides a name it never declared; an `optional` read that does not join the unload cascade |
| `tests/test_contribution_attribution.py` | an undo that reaches past its own contribution (a name- or prefix-keyed removal) |
| `tests/test_service_vocabulary.py` | a plugin touching a name outside the vocabulary; a vocabulary entry no plugin uses; a wired policy that does not refuse |
| `tests/test_plugin_format_strategy.py` | an ecosystem's shapes back in the loader; a format whose strategy is declared but never called |
| `tests/test_plugin_catalog.py` | a declarative kind whose `inject` requirement and registration disagree; a registration body that names a kind instead of the table |
| `tests/test_plugin_hook_dispatch.py` | a dispatcher missing from `LOADER_HOOK_BOUNDARY`; a boundary entry naming a typed event that does not exist |
| `scripts/plugin_ecosystem_probe.py` | an ecosystem whose assets the loader does not find, or an inert hook that is reported as working |
| `scripts/functional_smoke.py` | a composition that cannot answer a turn end to end |

**Measured on the delivered tree (2026-10-15):** full suite **20 failed / 4108 passed /
2 skipped / 0 errors**.  Of the 20, nineteen are the frozen baseline of environment
failures (`sandbox-exec` is not permitted under this file policy; console-width probes in
a non-interactive harness) and one is a Python-3.14 harness issue in
`tests/test_web_channel.py` -- the round below (see its closing report) *removed* the
twentieth-baseline entry, a real cross-loop teardown bug in `MCPClient.close`, which had
been sitting inside the baseline and read as an environment failure.  Smoke **6 ok /
0 failed**; ecosystem probe **0 failure(s)**; ledger **15/15 contracts**; vocabulary
**44 public / 27 reserved**.

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
5. ~~**A kind table for declarative assets**~~ -- **done**: `DECLARATIVE_KINDS` is the
   table, and `_register_declarative_assets` only iterates it.
6. **`PluginCatalog` still owns two concerns it should not.**  The hook surface moved out
   (2432 -> 2145 lines, 75 -> 39 methods), but the **reload transaction** and the command /
   agent registries are still there.  The reload path is the harder half: it snapshots and
   restores the registries it owns, so splitting it means deciding who owns the rollback.
7. **Two overlapping declarations.**  `CORE_SERVICES` (15, with contracts) and
   `PUBLIC_SERVICES` (44, what a plugin may touch) are maintained separately and their
   relationship is only prose.  Only `agent_core` is in the first and not the second, which
   suggests they could be one record with two fields -- or that the ledger should be
   derived from the vocabulary.
8. **Two vocabulary entries have no in-tree user.**  `commands` and `agents` are published
   for third parties (`FOR_THIRD_PARTIES`, each with a reason) and exercised by
   `tests/test_service_vocabulary.py`, but no plugin in this tree contributes through the
   *view*: bundles go through the loader's markdown adapter, which registers on the raw
   registry.  Closing it means moving that adapter onto the view, which is the same edit
   that would let a plugin replace the loader's command registry.
9. **Source-scanning guards, and when that is right.**  Several new guards assert on
   source (`DECLARATIVE_KINDS`'s "the body only loops", the loader's "no ecosystem shape
   literals").  They are checking an **absence**, which no runtime assertion can see -- but
   the line is worth stating, so that reading the source does not become the default way to
   check a property a type or a refusal could check instead.
