# Plugin-isation and the adapters — status, and what is left

This continues `2026-10-02-plugin-kernel-status.md`. That document ended with the
kernel and the plugin model in place; this one covers the arc that made
**"a new ecosystem, or a different implementation, is an adapter"** true for the
parts where it can be true, and measures the parts where it still is not.

Verification for every number below: `pytest -q -p no:cacheprovider` →
**19 failed / 3896 passed / 2 skipped**, where the failed set is *identical to the
frozen baseline* (19 pre-existing entries, compared as a set, never by count);
`scripts/functional_smoke.py` → 6 ok; `scripts/plugin_ecosystem_probe.py` →
0 failures.

## What landed

| Piece | State | Evidence |
|---|---|---|
| One ecosystem = one adapter | **done** | `agent/plugins/formats.py`: `PluginFormat` + `PLUGIN_FORMATS` + `register_format`. `catalog.py` and `plugin_tools.py` contain no ecosystem literals (only prose). A fourth ecosystem (`acme_format`) is defined **only in a test** and both the loader and the installer accept it |
| The installer and the loader agree | **done** | `489fb82`, plus `scripts/plugin_ecosystem_probe.py` (fixtures written per ecosystem doc, load + install + hook env + a real gateway) |
| Store contract, replaceable | **done and proven** | `agent/memory/contracts.py` (`MemoryStore` 32, `ContextService` 15, `MemoryService` 8) + `run_store_contract`; a **second implementation** (`agent/memory/backends/sqlite_fts.py`, SQLite FTS5) passes the same suite unchanged; `service_replacements` + a second profile select it; the real composition resolves it (`who_provides("ltm_store") == "sqlite-fts-memory"`). Teeth: renaming `max_categories` turns the conformance test red |
| Services are provided by plugins | **9 of 11** | `tests/test_service_ownership.py`: `agent`, `agent_loop`, `context_manager`, `llm`, `ltm_store`, `mcp_client`, `memory`, `registry`, `skill_catalog`. Moves: `llm` (`ec7f410`), `mcp_client` (`95d9fb2`), `registry` + `skill_catalog` (`761f49f`) |
| The rest are floors, with reasons | **2, tested** | `agent_core` (a *view* of the composition: resolves five services at call time and publishes the prompt through the whole mapping) and `plugin_catalog` (the loader cannot be provided by what it loads). `ROOT_ASSEMBLED` is empty: a new service must be plugin-provided or explicitly waived/floor'd, and the guard names the ratio when it is not |
| Other agents' skills are the user's skills | **done** | `PluginFormat.user_skill_roots` + `SkillRoot` (with `system_dir`), `SHARED_SKILL_ROOTS`, catalog fallback layer. Measured on a scratch home: `vendor-skill → shared:agents`, `codex-skill → shared:codex`, `codex-internal → NOT LOADED` |
| DSH, read-only | **done** | `agent/plugins/dsh.py`: bundles, MCP rows converted to this runtime's shape, skill roots, `unsupported` reasons; YAML subset reader that refuses rather than guesses (PyYAML used if present). Reads the real `~/.dsh/profiles/desktop` → 2 stdio servers, 0 warnings |
| Hooks that can never run are reported | **done** | 30 translated event names, 4 with a dispatcher; the other 26 are named at activation, in the installer's approval message, and in the install payload (`inert_hooks`). The record is checked against the source, so the hand list cannot drift |

## Two blind spots this arc found in its own guards

1. **The service ledger's scanner conflated declarations with provisions.**
   `service_replacements={"ltm_store": …}` was counted as a *writer* of the
   service. Fixed, and it corrected the ledger in the other direction too: three
   files listed as `memory` consumers only ever read the config *section*
   (`cfg.get("memory")`), so the list went 8 → 5. A ledger that over-reports is a
   ledger that tells the next reader "do not replace this".
2. **The memory contract guard scans methods only.** It asserts that nothing
   outside `agent/memory/` calls a *method* beyond the protocol — and is green
   while consumers reach two **attributes** that the protocol does not declare:

   | Attribute | Consumers | Failure mode if a replacement lacks it |
   |---|---|---|
   | `.staging.session_id` | `channels/base.py:191, :397, :402` | `AttributeError` (the attribute, not the method, is required) |
   | `.staging.close` | `cli.py:1006` | tolerated (`getattr`), silently skipped |
   | `.staging.context_dir` / `._sqlite_backed` | `memory/context.py:58` (orphan recovery, called from `channels/base.py`) | tolerated, silently loses orphan-staging recovery |
   | `.store` | `config.py:1471`, `core/spawn_planning.py:372`, `core/subagent_finalize.py:138` | `close()`/`add_entry` on an object the protocol never promised |

   Plus: `agent/cli.py` builds `MemoryPalace()` directly in **four** places
   (`memory ls` / `search` / `index`), so replacing the `memory` service does not
   reach those commands.

   And the positive half, worth recording because it is why the layer is *close*:
   consolidation and the session-end path **do** resolve the service
   (`channels/base.py` → `resolve(components, "context_manager")`, then the pool
   calls `manager.process_one_job(...)`).

## Replaceability, stated honestly

| Layer | Claim | Why |
|---|---|---|
| `ltm_store` | **replaceable, proven** | second implementation + shared behavioural suite + profile selection + real-composition swap |
| `memory` (palace) | **declared, not proven** | 8-member protocol and selection work; nothing else implements it; four CLI sites bypass the service |
| `context_manager` | **declared, not proven, and not closed** | 15-member protocol + selection; no behavioural suite, no second implementation, and the attribute leaks above |

The next round closes exactly that gap: declare `staging` as its own contract (or
route its users through the manager), decide whether `.store` is part of
`ContextService` or gets removed from those three call sites, make the guard
attribute-aware, add `run_context_contract(...)` plus a second `ContextService`
implementation selected by profile, and point the four CLI sites at the service.

## Not built, and why

* **Importing a DSH profile's MCP servers into startup.** Adopting another
  application's `command` lines is *execution*, and this runtime requires a human
  for that — but the consent machinery is a five-minute, in-process, source-keyed
  side table (`agent/security/plugin_approval.py`), so there is nowhere to record
  "this capability, as it stands, was approved". Design:
  `specs/2026-10-12-capability-provenance-and-adapter-translators-design.md`.
* **An npm source for the installer** (`npm pack --ignore-scripts`). Same missing
  concept, plus a new acquisition channel with a different trust class.
* **Codex `apps` / `interface.defaultPrompt`.** Recognised, not mapped: this
  runtime has no counterpart concept. The honest treatment is the same
  "reported, never guessed" channel the inert hooks now use, and the design above
  gives contribution *kinds* a place to say so.
* **Claude Code's user-level skill root.** Not declared, because no evidence for
  its path exists in this tree or in the sources it was written against. One
  `SkillRoot` tuple adds it when there is evidence.
