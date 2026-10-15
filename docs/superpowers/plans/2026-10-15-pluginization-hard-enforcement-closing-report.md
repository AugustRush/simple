# Pluginization hard-enforcement round — closing report

**Scope:** the work that followed a first-principles review of the pluginization
implementation, asking one question of each claim in
[`2026-10-13-pluginization-delivery.md`](2026-10-13-pluginization-delivery.md): *is this
enforced, or is it a convention the author has to overhear?*  The review's finding was
that the kernel was genuinely pluginized and the layer above it was **documented rather
than enforced** — the declarations existed, and none of them refused anything.

**Result:** three commits. Every claim in the delivery document is now refused by
something: a declaration the kernel rejects, a policy that closes the vocabulary, a
strategy the format owns, a base class the guards insist on. The full-suite FAILED set
was byte-identical to the baseline after each commit — and one entry was then **removed**
from that baseline because it turned out to be a real bug wearing an environment
failure's clothes.

| Measure | Before | After |
| --- | --- | --- |
| Full suite | 21 failed / 4011 passed / 2 skipped | **20 failed / 4108 passed / 2 skipped** |
| FAILED set | — | identical, **minus one real fix** (`4dc7ddf`) |
| `agent/plugins/catalog.py` | 2432 lines / 75 methods | **2145 / 39** |
| `agent/kernel/{service,context}.py` | 381 + 308 | **517 + 456** |
| New modules | — | **3** (`hook_dispatch`, `hook_types`, `service_vocabulary`) |
| New test files | — | **3** (672 lines) + guards added to 9 existing files |
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

## What remains

Recorded rather than fixed, in the delivery document's backlog:

1. **`PluginCatalog` still owns the reload transaction and the two registries.** The hook
   surface is out; the reload path is the harder half, because it snapshots and restores
   the registries it owns, so the split has to decide who owns the rollback.
2. **Two overlapping declarations** — `CORE_SERVICES` (15, with contracts) and
   `PUBLIC_SERVICES` (44, what a plugin may touch). Their relationship is prose; only
   `agent_core` is in the first and not the second.
3. **`commands` and `agents` have no in-tree user** through the view: bundles go through
   the loader's markdown adapter, which registers on the raw registry. Moving that adapter
   onto the view is the same edit that would let a plugin replace the loader's registry.
4. **Source-scanning guards.** Several new guards read source, because they check an
   *absence* — "the loader does not know this shape" — which no runtime assertion can see.
   The line is worth stating so that reading source does not become the default way to
   check something a type or a refusal could check.

And two things this round did **not** touch, still true from the review: the four
remaining god classes (`SchedulerStore` 4085 lines, `WebChannel` 3319, `LTMStore` 2834,
`BaseAgent` 2535), and the `MemoryPalace()` construction at four CLI sites, which makes a
*replaced* memory service unreachable from those commands.
