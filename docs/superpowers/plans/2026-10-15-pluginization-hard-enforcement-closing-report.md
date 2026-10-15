# Pluginization: enforcement, then cleanup — closing report

**Scope:** the work that followed a first-principles review of the pluginization
implementation, asking one question of each claim in
[`2026-10-13-pluginization-delivery.md`](2026-10-13-pluginization-delivery.md): *is this
enforced, or is it a convention the author has to overhear?*  The review's finding was
that the kernel was genuinely pluginized and the layer above it was **documented rather
than enforced** — the declarations existed, and none of them refused anything.

**Result:** eleven commits, three passes. Every claim in the delivery document is now
refused by something: a declaration the kernel rejects, a policy that closes the
vocabulary, a strategy the format owns, a base class the guards insist on, a contract
checked at composition time. The full-suite FAILED set was byte-identical to the baseline
after each commit — and two entries were then **removed** from that baseline: one real bug
wearing an environment failure's clothes, and one test that had stopped staging its own
race. What is left is nineteen failures, every one a thing this machine cannot do.

| Measure | Before | After |
| --- | --- | --- |
| Full suite | 21 failed / 4011 passed / 2 skipped | **19 failed / 4116 passed / 2 skipped** |
| FAILED set | — | **minus two fixes** (`4dc7ddf`, `0798d4c`); nothing added |
| `agent/plugins/catalog.py` | 2432 lines / 75 methods | **2052 / 39** |
| Service declarations | 2 modules, 15 + 44 names | **1 module, 67 names** |
| `agent/kernel/{service,context}.py` | 381 + 308 | **517 + 456** |
| New modules | — | **5** (`hook_dispatch`, `hook_types`, `reload`, `service_contracts`, `services`) |
| Test files touched | — | **25** (3 new, guards added or fixtures corrected in the rest) |
| Baseline entries that are environment limits | 18 of 21 | **19 of 19** |
| Smoke / ecosystem probe | 6 ok / 0 failed; 0 failures | **unchanged** |

---

## The criteria, and what each one caught

The review asked five falsifiable questions of the implementation. They are worth
keeping, because each one found something the documentation claimed and the code did not
do.

| # | Criterion | What the tree actually did |
| --- | --- | --- |
| C1 | The kernel does not know the application | **held**: `agent/kernel/` imports nothing from `agent.*` |
| C2 | A declaration is a contract, not documentation | **failed**: every protocol was `@runtime_checkable` and never `isinstance`-checked; `inject` was advisory; the ledger was a hand-written mirror checked by an AST scan |
| C3 | A registration is an effect | **failed**: `ctx.services` handed out the writable registry, whose `provide` records no undo |
| C4 | A dependency is a graph | **failed**: `ctx.get` bypassed `inject`, so an undeclared reader was invisible to `unload`/`replace` |
| C5 | The boundary is provable | **failed**: no plugin→kernel import test, and the one outward-import rule missed relative imports |

---

## What changed, in the order it changed

| # | What | Commit | The claim it makes enforceable |
| --- | --- | --- | --- |
| 1 | `inject` / `optional` enforced in `Context.get`/`raw`/`services`; the read-only declared view; identity-keyed undos; `ScopedContribution` + `ScopedService` | `fc7922a` | C2, C3, C4 |
| 2 | Closed service vocabulary via a `service_policy` hook; `PluginFormat` strategies; `DECLARATIVE_KINDS`; `PluginCatalog` split; `_core_services` unified; hook/event boundary | `72efb7f` | C2, C5 |
| 3 | `MCPClient.close()` waits only for the tasks its own loop owns | `4dc7ddf` | — (a bug the baseline was hiding) |

Two decisions in #1 are worth recording, because both are about *not* changing semantics
while making something strict:

* **`optional` had to be invented.** Writing an optional collaborator into `inject` would
  have made the plugin permanently `pending` in a minimal composition — a behaviour
  change, not a tightening. `optional` permits the read without gating activation, and
  the unload cascade walks `inject ∪ optional`, because a tolerated read still captured
  the object.
* **A name a plugin provides counts as declared for that plugin.** Offer-and-read-back is
  one act ("provide a default if nobody else did, then use what is there"), and it cannot
  be a dependency on somebody else.

---

## The defects the honest guards found

Making the checks refuse things is what turned three latent problems into failures. All
three were found by the *guard*, not by a user:

1. **`RoutingTransport.image_content_block(self, mime_type, data)`** did not match its
   contract's `base64_data`. Every caller passes positionally, so nothing broke — but a
   caller using the keyword would have. The `llm` contract had never been checked: its
   waiver pointed at a file containing zero mentions of `ModelTransport`.
2. **`ModelTransport.observed_input_tokens`** was called by nothing in `agent/` — only by
   its own test — and duplicated `observed_usage`. Deleted, and the test's assertions
   moved onto the member the turn loop actually uses.
3. **The contract guard's "a data member must not be `None`"** rejected
   `thinking_effort = None`, which is the documented sentinel for "the config never said".
   Now `None` is only a failure when the contract does not also declare it.

And one more, from the *loader's* side: `DECLARATIVE_KINDS` made it impossible for a
bundle's `inject` requirement and its registration to disagree — they used to be two
sequences that had to be kept in step by hand.

---

## What the previous delivery claim got wrong

The delivery document said **"delivered"** and meant it. Three of its statements were
true as *descriptions* and false as *mechanisms*, which is the distinction this round
exists to draw:

| It said | It meant | Now |
| --- | --- | --- |
| "a per-plugin **view** whose contributions are attributed and unwound" | six hand-written views; two attributed by name or prefix, so one plugin's unload could remove another's | one `ScopedContribution` base, identity-keyed undos, and a guard against a seventh hand-written view |
| "adding an ecosystem ... no longer edits the loader" | true for *paths*; the shapes (matcher groups, hook output, `$ARGUMENTS`) were in the loader and applied to every ecosystem | the shapes are `PluginFormat` strategies, and a test proves a replaced strategy is what runs |
| "a plugin declares what it needs" | `inject` gated activation, and then `ctx.get` reached anything in the adopted mapping | `inject ∪ optional` is the complete set a plugin may touch, and a second policy decides whether the name is a plugin's at all |

Its measured numbers were also stale: `19 failed / 3945 passed` at the time of writing,
`21 / 4011` when this round began. The document now carries re-measured numbers and says
when they were taken.

---

## The second round: what the first round's own claims made visible

Four items, each of which only became *askable* once the first round had made a claim
refusable (`1e06a3a`, `a5def2a`, `fc4f67f`, `c86e2a3`, `7e55b65`):

1. **A contract nobody checks is documentation.** The first round made the *declaration*
  refusable; it did not make the *implementation* checkable. Measured: zero `isinstance`
  calls against any capability protocol in the tree — the `memory` ledger entry had pointed
  at `MemoryStore` while the service held a *palace*, and nothing asked. There is now a
  runtime audit (`agent/runtime/service_contracts.py`) plus a `service_validator` hook the
  kernel consults **before** recording a plugin's `provide`. It refused four things on its
  first run, three of them real: the MCP plugin offering `None` as `mcp_client` ("absent"
  expressed as "here is a None"), a test double answering 8 of 13 protocol members, and two
  agent doubles that were not `AgentService`s.
2. **Owners and consumers were a hand-maintained mirror of the source.** The two
  declarations merged into `agent/runtime/services.py` — and the fields were *deleted*
  rather than merged, because a fact derivable from the code does not belong in a
  declaration. The scan then found four names that had been **invented** (config keys and a
  local dict, not component keys) and two scanner blind spots.
3. **A floor is about the registry, not about the contribution.** `commands` and `agents`
  were floors *and* had their contributions written to the raw registry, which is why both
  names sat in a "published for third parties, used by nobody" waiver. The markdown adapter
  now contributes through `ctx.commands`/`ctx.agents`, and the waiver is gone. The coupling
  this exposed is worth recording: the `inject` requirement and the registration were two
  separate predicates, and when they disagreed **every Python-only plugin failed to load**.
4. **A published name that cannot be substituted knowingly is not an extension point.**
  `post_turn_maintenance` was resolvable, substitutable, undocumented and provided by
  nobody: a replacement had to reproduce eleven keyword-only parameters from a private
  method, and got a `TypeError` at the end of a turn. Removed (`7f20228`); if a composition
  wants the step, it comes back with a contract and a provider together.

Splitting the reload transaction produced the round's most useful negative result: a reload
is **atomic about the registries the loader owns and not about plugin activation**, and the
rollback must **mutate in place** — it used to rebind the plugin table, which the hook
dispatcher holds by reference, so a rolled-back catalog kept firing hooks at the tree it had
just abandoned while reporting success.

## What remains

Recorded rather than fixed:

1. **Provenance and consent for third-party code.**  This is the one part of the
   pluginization goal still unmet, and it is not a maintainability gap: a Python plugin in
   `~/.agent/plugins/` is `exec_module`-ed at startup with the process's full privileges,
   and `plugin_approval` gates *installation*, not *loading*.  The design is written
   (`2026-10-12-capability-provenance-and-adapter-translators-design.md`) and nothing is
   implemented.  The first-principles criterion it fails is the fifth one — "the boundary
   is provable: a third party extends this by depending only on a published interface" —
   which now holds for the *mechanism* and not for *trust*.
2. **The four god classes.**  `SchedulerStore` 4085 lines, `WebChannel` 3319, `LTMStore`
   2834, `BaseAgent` 2535.  Orthogonal to pluginization, and the repository has an
   established method for them (survey → extract → closing report, FAILED set compared
   byte-for-byte after every step).
3. **The 4 remaining soft reads** in `tests/test_memory_contract.py` — a designed fallback,
   a decision about `SessionService`, and two name collisions the scan cannot tell apart.
4. **DSH profile import** — reading is done (`agent/plugins/dsh.py`, read-only); importing
   needs the consent machinery item 1 describes.
5. **Source-scanning guards.**  Several guards read source because they check an *absence*
   ("the loader does not know this shape"), which no runtime assertion can see.  The line is
   worth stating so that reading source does not become the default way to check something a
   type or a refusal could check.
6. **The god classes' smaller cousin:** `agent/memory/context.py` at 2483 lines, and the
   four remaining `MemoryPalace`-adjacent direct constructions in `agent/cli.py` that are
   *not* on the read-only path — `memory tidy` builds a full composition, which is correct
   but heavier than the rest.
