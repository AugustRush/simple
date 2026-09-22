# Prompt Cache Prefix Stability Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise the provider prompt-cache hit rate from the measured 55% toward the
structural ceiling (~99% after a session's first request) by making the request
payload's head invariant and its body append-only, without truncating long
outputs and without weakening any existing guard.

**Architecture:** The payload is ordered `S ‖ T ‖ M` (system prompt, tool schemas,
messages). A provider prefix cache returns `h_n` = the length of the longest
prefix of the request that fully matches a persisted cache unit. Everything
below follows from three invariants: **the head must be a pure function of
session-stable state**, **the body must be append-only**, and **policy numbers
must not live in the payload**. The fix moves every per-turn value out of `S`
into a frozen, dated message at the end of `M`; makes compaction a rare, batched,
out-of-band event; and separates the provider output cap from the input budget
reserve so neither decides the other.

**Tech Stack:** Python, SQLite, pytest, Typer, React/TS (frontend untouched).

---

## Status

**Tasks 0, 1, 2 and 3 are implemented and verified. Task 4 is complete except for
Step 3, which cannot be run yet — see below.**

Full suite: **16 failed / 2577 passed / 1 skipped**, with the FAILED set byte-identical
to the recorded baseline (16 entries, all `sandbox-exec: sandbox_apply: Operation not
permitted`, plus `test_build_components_loads_user_tool_plugins`). Baseline was
16 / 2570, so the seven new tests are the entire delta. The suite was run after each
task, not once at the end, so any regression would have been attributable to a single
change.

**Outstanding — Task 4 Step 3.** The end-to-end reading (does the <20% band stop
dominating the uncached bill; do `foreground` rows exist; is the head stable within a
session) needs a *new* session. Every row in the store predates the Task 0 transport
fix, so `scripts/analyze_cache_hits.py` still reports the same 641 rows over
2026-09-09 → 2026-09-21 and prints its own "no `foreground` / `tool_step` rows"
warning. Re-run it after a real interactive session to close this out. Nothing in the
code needs to change for it; the data simply does not exist yet.

**Two deliberate deviations from the plan as written**, both recorded in full at the
end of their task sections:

1. **Task 1 Step 4** — the plan said to drop the keyword gate on the tool set. Not
   done: three of its groups carry no `requires_request` guard, so shipping them
   unconditionally would expose destructive tools. The *ordering* fix was implemented
   instead, which is behaviour-neutral and captures most of the cache benefit.
2. **Task 2 Step 4** — the plan said to append the eviction notice as the newest
   message. Implemented, measured, and rejected: it made the notice the message a
   provider reads as the turn to answer. It now sits immediately before the newest
   request, which keeps the cache property the step was after and the prompt
   semantics it had not considered.

---

## First principles

Billed input cost for request *n*, with `c_hit ≈ 0.1 × c_full`:

```
cost_n = h_n × c_hit + (|P_n| − h_n) × c_full
```

`h_n` is bounded by the first position at which `P_n` diverges from anything
already persisted. Therefore:

1. **A volatile value in `S` or `T` caps `h_n` for every request at that
   position.** The head is the only part of the payload that *can* be shared by
   every request in a session, so a volatile value there discards the largest
   possible amount of sharing. The tail is unique per request anyway, so
   volatile content there is free.
2. **An agent loop's body is naturally append-only** (`M_n = M_{n−1} ‖ [new]`),
   which yields `h_n ≈ |P_{n−1}|` — ~99% for every request after the first. Any
   delete-from-front, reorder, rewrite, or mid-insert destroys the prefix for
   everything after the edit point.
3. **`max_tokens` is a sampling parameter and is not in `P_n`**, so it may vary
   freely per request at zero cache cost. Letting it decide *when the body is
   edited* is the conflation that makes it expensive.
4. **Context length and cache hits are the same currency.** Trimming 40k of
   context saves `40k × 0.1 = 4k`-equivalent; a trim that turns a 50k request
   from 99% into 13% costs `45k × 0.9 = 40k`-equivalent. A cache-destroying trim
   is never worth it. Trim rarely and deeply, never shallowly and often.
5. **The term you optimize must be measured.** The main conversation path
   currently records nothing.

### Measured baseline (`~/.agent/context/palace.db`, `usage_events`)

Reproduce with `uv run python scripts/analyze_cache_hits.py`. Reading at 641 rows
(2026-09-09 → 2026-09-21):

| phase | calls | input tokens | cached | hit |
|---|---|---|---|---|
| subagent | 502 | 20.41M | 11.24M | 55.1% |
| consolidation | 139 | 0.30M | 0.02M | 6.9% |
| foreground / tool_step | **0** | — | — | — (not recorded) |

| band | calls | input | uncached | share of uncached |
|---|---|---|---|---|
| <20% | 250 | 6.21M | 5.28M | 55.9% |
| 20–50% | 133 | 4.42M | 3.21M | 34.0% |
| 50–80% | 53 | 2.08M | 0.59M | 6.2% |
| 80–95% | 60 | 2.54M | 0.31M | 3.3% |
| 95%+ | 145 | 5.47M | 0.06M | 0.6% |

- **90% of the uncached bill sits in the two lowest bands**, across 383 of 641
  calls — the misses are the normal case, not an outlier to be tolerated.
- `subagent` and `foreground` share `_prepare_turn`, so the subagent figures are
  a *proxy* for the conversation path, not a measurement of it. Task 0 is what
  turns the proxy into a reading.
- 13 persisted `provider_checkpoints` rows have an eviction notice as
  **`messages[0]`** (60, 84, 128, 145, 156, 172 messages dropped), proving
  compaction rewrites position 0 of the body.

### Violations of the invariants

**Rule 1 — head volatility**

| # | Site | Violation |
|---|---|---|
| 1 | `agent.py:2596` | `<retrieved_context>` (LTM results for *this* turn's query) appended to `S` |
| 2 | `agent.py:2541` | `<session_checkpoint>` appended to `S` |
| 3 | `agent.py:2611` | `## Active Skills` appended to `S` |
| 4 | `agent.py:2627` | `## Orchestration policy` (carries request-derived `decision.guidance`) appended to `S` |
| 5 | `config.py:1087` | `Current UTC time: %Y-%m-%d %H:%M UTC` inside `S`, ahead of the plugin prompts |
| 6 | `agent.py:2552` | `select_tools` varies `T` per turn by keyword match on the user message |
| 7 | `config.py:877-888` | the same tool list is *also* rendered as prose into `S`, so `T` and `S` move together |
| 8 | `agent.py:3449` | every subagent spawn re-renders `S`; with #5 no two spawns share a head |
| 9 | `config.py:819` | `_StaticPromptInputs.tools` keys `S` on the tool list, so #6 forces `S` to re-render |

Amplifier: `S` is **restored** at the end of the turn (`agent.py:3200`), so the
next turn refills the same offsets with different content — volatility at a fixed
offset that precedes the entire body.

**Rule 2 — body edits**

| # | Site | Violation |
|---|---|---|
| 10 | `agent.py:1438` → `context.py:791` | `fit_to_budget` drops the oldest units **before every provider call** |
| 11 | `context.py:808` | eviction notice **prepended** to position 0 of the body |
| 12 | `context.py:625` | `_repair_tool_history` can drop protocol units from the middle |
| 13 | `agent.py:2668` | `messages[-1]` rewritten after a continuation |
| 14 | `agent.py:3332` | post-turn compaction rewrites the body *and* `_with_task_context` rewrites `S` |

**Rule 3 — policy conflated with payload**

| # | Site | Violation |
|---|---|---|
| 15 | `agent.py:1383-1399` | `_input_token_budget` uses `self.max_tokens` as the input reserve → 57k instead of ~113k |
| 16 | `agent.py:1360-1376` | `_retrieval_token_budget` passes `output_tokens=self.max_tokens` → retrieval starved to ~1k of ~9k |
| 17 | `agent.py:1532-1540` | `_next_structured_output_budget` measures spare room against a budget that shrinks as the output budget grows |
| 18 | `bootstrap.py:47-63` | `_reserve_input_context` only fires when `max_tokens >= context_window`, so it never fires for deepseek (64000 < 128000) |

**Rule 4 — compaction depth**

| # | Site | Violation |
|---|---|---|
| 19 | `context.py:780-792` | drops units **one at a time** until it just fits → the body is re-cut on nearly every step at the shallowest possible depth |

**Rule 5 — measurement**

| # | Site | Violation |
|---|---|---|
| 20 | `transport.py` `OpenAITransport` | never sent `stream_options.include_usage` and discarded the usage-only chunk (`if not chunk.choices: continue`) — so every *streamed* call, i.e. every interactive turn, reported no usage and was recorded nowhere. Fixed in Task 0. |

### Target invariants

- **I1** `S` and `T` are byte-identical for the life of a session. No time, no
  query text, no retrieval, no per-turn policy.
- **I2** Within a session `M` is only appended to. The single exception is
  compaction.
- **I3** Compaction happens at most once per turn, drops to a low-water mark, and
  never writes at position 0.
- **I4** Per-turn volatile context is appended once and then frozen — which is
  also the semantically correct record of what the model actually saw.
- **I5** `max_tokens` is derived per call from the remaining room and never
  decides when the body is edited.
- **I6** Every provider call on every path lands in `usage_events` with its hit
  ratio and the reason for any body edit.

**Accepted trade-off:** I4 converts head volatility into tail growth. The
retrieved block is no longer discarded at end of turn, so it is re-sent (at ~1/10
price once cached) until compaction removes it. This is bounded by the existing
retrieval budget (15% of remaining room) and is strictly cheaper than the
alternative — a 10× re-bill of the entire history on the next turn.

**Out of scope:** frontend, Anthropic explicit `cache_control` breakpoints
(follow-on once I1 holds), and the `consolidation` phase's 7% (each call is an
independent one-shot prompt with no reusable prefix — a design property, not a
defect).

---

## File Structure

- Modify `agent/core/agent.py`: move the four `_prepare_turn` injections out of
  `ctx.system_prompt` into the user message; add the per-call output cap; move
  compaction to the turn boundary; add the usage-record diagnostic.
- Modify `agent/memory/context.py`: batch `fit_to_budget` to a low-water mark;
  append (not prepend) the eviction notice.
- Modify `agent/config.py`: drop the UTC timestamp from `S`; expose
  `output_reserve`; make `_StaticPromptInputs` session-scoped.
- Modify `agent/core/context_assembler.py`: remove the per-turn keyword gate on
  `T`.
- Modify `agent/bootstrap.py`: fix `_reserve_input_context`; wire `output_reserve`.
- Modify `agent/memory/store.py`: add the head/body fingerprint + edit-reason
  columns (or `metadata_json` keys) to `usage_events`.
- Modify `agent/core/transport.py` (Task 0): request and read streamed usage.
- Modify `agent/shared.py` (Task 0): carry usage on the OpenAI response object.
- Modify `config.example.json` (Task 0): document the `stream_usage` opt-out.
- Add `agent/core/payload_shape.py` (Task 0): one home for "what a prefix cache
  sees", so the recorded fields and the analysis script cannot drift apart.
- Add `scripts/analyze_cache_hits.py` (Task 0): the read-only hit-rate report.
- Add `tests/test_usage_telemetry.py`, `tests/test_cache_hit_analysis.py`
  (Task 0).
- Modify `tests/test_consolidation.py`: compaction depth, notice placement,
  prefix-extension regressions.
- Modify `tests/test_context_assembler.py`: tool-set invariance.
- Modify `tests/test_system_prompt_cache.py`: head invariance.
- Modify `tests/test_agent_integration.py`: budget/cap separation, foreground
  usage recording.
- Modify `tests/test_web_channel.py`: assertions that inspect `ctx.system_prompt`.

---

## Task 0: Make the main path measurable — DONE

Prerequisite for everything else — without a hit ratio on the interactive path
there is no way to verify Tasks 1–3.

**Files (as built — the plan's guess at the shape was wrong, see below):**
- Modify: `agent/core/transport.py` (the OpenAI streaming path)
- Modify: `agent/core/agent.py` (`_prepare_provider_context`,
  `_observe_provider_usage`, new `_warn_missing_usage`)
- Modify: `agent/shared.py` (`_OAIResponse.usage`)
- Modify: `config.example.json` (documents the opt-out)
- Add: `agent/core/payload_shape.py`
- Add: `scripts/analyze_cache_hits.py`
- Add: `tests/test_usage_telemetry.py` (28 tests)
- Add: `tests/test_cache_hit_analysis.py` (30 tests)

**Root cause — one level below where this plan looked.** The plan guessed
`_context_manager_for(ctx)` returned `None` on the interactive path. It does not:
the manager is reached, and the row is written whenever the provider reports
usage. The defect is in the transport:

- `OpenAITransport._create_kwargs` never sent
  `stream_options: {"include_usage": true}`; and
- `OpenAITransport.stream` discarded the usage-only chunk with
  `if not chunk.choices: continue` — and that empty-`choices` chunk is the *only*
  place an OpenAI-compatible stream carries a usage object.

`send_message` streams whenever a callback is supplied, so **every** interactive
turn took the streaming path and recorded nothing, while every non-streaming call
(subagents, consolidation) was recorded. That is the whole of the "0 foreground
rows" finding: not a failed write, an unrequested field. The suppression at
`agent.py:1549` was hiding nothing, because there was nothing to suppress.

- [x] **Step 1: Find why `foreground`/`tool_step` rows never land.** Traced to
  the transport rather than the store. Fixed by requesting
  `stream_options.include_usage` on streaming calls and reading the usage-only
  chunk before the `continue`. The silent skip is replaced by a one-shot visible
  warning (`_warn_missing_usage`) so a provider that reports nothing says so once
  per process instead of leaving an unexplained gap.
- [x] **Step 2: Add a failing regression test** asserting that a foreground
  provider call records exactly one `usage_events` row with `phase='foreground'`,
  plus a test that a *silent* stream writes no row and warns once — a row of
  zeros would read as "this call cost nothing" rather than "nobody said".
  Red-checked: reverting both halves of the transport fix produced exactly the
  three expected failures.
- [x] **Step 3: Record the cache diagnostics.** `metadata_json` now carries
  `head_fingerprint` (system prompt + tool schemas together — either one changing
  invalidates everything after it), `body_front_fingerprint`, `body_messages`,
  and `body_compacted`. Names differ from the plan's sketch deliberately:
  `body_front_fingerprint` matches the module's own vocabulary, and
  `body_compacted` is namespaced so the flag cannot be confused with a body
  *count*. `compaction_reason` was dropped: the only body edit that exists today
  is `fit_to_budget`, so a reason field would have one value and would not earn
  its column. Fingerprints use `blake2b`, not `hash()`, because the value is
  written to a database and read by a later process — a per-process-salted hash
  would report a change that never happened (covered by a two-`PYTHONHASHSEED`
  subprocess test).
- [x] **Step 4: Add a read-only analysis query.**
  `scripts/analyze_cache_hits.py` prints hit rate by phase, by band (with each
  band's share of the *uncached* bill), and by body rewrite, plus head stability
  per session and the worst sessions by uncached input. It opens the store
  `mode=ro` — the script cannot damage what it reports on, and a test asserts a
  write is refused. The same commit adds the pure aggregation functions the
  report is built from, so the numbers are unit-tested rather than eyeballed.

**Consequence for reading the baseline:** since `stream_options` was never sent,
no streamed call has *ever* reported usage. The measured 55.1% therefore
describes only non-streaming traffic (subagents, consolidation) — it is the
evidence *for* the compaction mechanism, not a figure for the conversation path.
That path's true hit rate is still unmeasured and is exactly what this task now
enables. Current reading: 641 rows, 54.4% overall, 55.9% of all uncached tokens
concentrated in the <20% band.


## Task 1: Freeze the head (I1)

**Files:**
- Modify: `agent/core/agent.py` (`_prepare_turn`, `send_message`)
- Modify: `agent/config.py` (`_compose_system_prompt`, `_static_prompt_inputs`)
- Modify: `agent/core/context_assembler.py` (`select_tools`)
- Modify: `tests/test_agent_integration.py`, `tests/test_web_channel.py`,
  `tests/test_system_prompt_cache.py`, `tests/test_context_assembler.py`

- [x] **Step 1: Add a test asserting `ctx.system_prompt` is unchanged across two
  consecutive turns** of one session (today it differs, because of the four
  injections).
  → `test_a_turns_context_never_reaches_the_system_prompt` in
  `tests/test_agent_integration.py` drives two turns of one session and asserts
  the prompt is byte-identical across them; `test_retrieved_context_cannot_close_
  its_untrusted_envelope` asserts the stronger property for a single turn
  (`ctx.system_prompt == "system"`, unchanged from construction).
- [x] **Step 2: Move the four injections into the turn's user message.** Build
  one block, in order: `<session_checkpoint>`, `<retrieved_context>`,
  `## Active Skills`, `## Orchestration policy`, plus a `Current UTC time:` line.
  Compose it into `_build_user_message_content(user_message, attachments)` —
  prepend a text block when the content is a multimodal block list, otherwise
  join with a delimiter. The user's own words stay last in the message.
  Keep `registry.set_context("turn_request", user_message)` as-is: it is metadata,
  not payload, and the `requires_request` guards depend on it.
  → `_prepare_turn` collects them into `turn_blocks` and passes the joined text
  as `turn_context=` to `_build_user_message_content`, which emits it as the
  first `parts` entry — before `user_message`, so the request is still read last.
  The multimodal branch puts the whole joined text in one leading text block.
  `set_context("turn_request", ...)` is untouched and still unconditional.
- [x] **Step 3: Remove `Current UTC time` from `_compose_system_prompt`**
  (`config.py:1087-1090`). The per-turn block now carries the timestamp, which is
  also more accurate than a session-frozen value.
  → `config.py` no longer contains the string; its only occurrence is the
  `turn_blocks` append in `_prepare_turn`, with a comment recording why it moved.
  The `original_system` round-trip is gone with it — `grep -rn original_system
  agent/ tests/` returns nothing.
- [x] **Step 4: Freeze `T`.** Drop the keyword gate in `ContextAssembler.select_tools`
  and always ship the session's full tool set. The docstring at
  `context_assembler.py:52-61` already establishes the gate was never a guard
  (calls dispatch by name against the whole registry; `requires_request` is what
  refuses an unasked creation) — so this removes a cost, not a protection. Keep
  `requires_request`.
  → **Deviation: the gate was kept; the *order* was fixed instead.** Three gated
  groups (`install_plugin`/`uninstall_plugin`, `delete_skill`, `delete_tool`)
  carry no `requires_request` guard, so shipping them unconditionally would
  expose destructive tools to every turn, and two tests pin the gate on purpose.
  `select_tools` now sorts the always-on schemas first and the gated ones last
  (`_CONDITIONAL_TOOLS`), which is membership-neutral and still gives two turns
  with different gates the longest possible common prefix. Pinned by
  `test_the_always_on_schemas_come_before_the_turn_dependent_ones`, which asserts
  a gated turn's list is the plain turn's list with the extras *appended*. See
  the deviation note below for the measurement.
- [x] **Step 5: Render `S` once per session.** Keep `_StaticPromptInputs` as the
  correctness mechanism, but ensure a session re-renders only when a genuine
  capability change occurs (tool/skill/MCP/workspace change), not per turn.
  → With the four injections and the timestamp out of `S`, the only remaining
  writer of `ctx.system_prompt` is the session's own re-render on a capability
  change; `_StaticPromptInputs` still gates it. Pinned by Step 1's test.
- [x] **Step 6: Run the focused tests.** Expect `test_web_channel.py` assertions
  that monkeypatch or inspect `_compose_system_prompt` to need updating — that is
  the intended signal, not a regression.
  → Seven `test_agent_integration.py` assertions matched the injected text inside
  `ctx.system_prompt`; they were re-pointed at the message the turn now builds,
  which is the signal the step predicted.

### Deviation recorded

The plan's Step 4 assumed the keyword gate was never a guard and could simply be
dropped. That is true of *most* of the gated groups but not all of them:
`install_plugin`, `uninstall_plugin`, `delete_skill` and `delete_tool` have no
`requires_request` guard, so an unconditional shipment would put a destructive
tool in front of every turn. Two existing tests also pin the gate deliberately.

What the gate actually costs is not membership but **order**: the first
conditionally-gated tool sits 6th of 44 in registration order, so a turn that
merely mentions a keyword used to cut the reusable prefix down to about five
schemas' worth. Sorting the stable set first and the gated set last captures
that benefit without changing which tools a turn is offered, so the fix is
behaviour-neutral by construction. Recorded in `_CONDITIONAL_TOOLS`'s comment
with the measurement.

## Task 2: Make the body append-only and compaction batched (I2, I3)

**Files:**
- Modify: `agent/core/agent.py` (`_prepare_provider_context`, `send_message`,
  `_post_turn_maintenance`)
- Modify: `agent/memory/context.py` (`fit_to_budget`, `_eviction_notice`)
- Modify: `tests/test_consolidation.py`

- [x] **Step 1: Add a test asserting two consecutive steps within one turn produce
  a body that is a strict prefix-extension** (no unit dropped, no notice inserted)
  when the payload is under the low-water mark.
  → `test_a_body_under_the_budget_is_only_ever_appended_to` asserts the retained
  entries are the *same objects*, not merely equal: equal-but-rebuilt is still a
  rewrite at the wire level.
- [x] **Step 2: Move compaction out of `_prepare_provider_context`** (currently
  `agent.py:1436-1454`, i.e. once per provider call) to the turn boundary in
  `send_message`, so it runs at most once per turn.
  → `BaseAgent._compact_body_for_turn`, called from `send_message` immediately
  after `_prepare_turn`. The in-loop call stays as an emergency path for a step
  whose own tool results overflow the window, with a comment saying so.
- [x] **Step 3: Batch the drop.** Replace the one-unit-at-a-time loop
  (`context.py:780-792`) with a drop to a low-water mark (e.g. 50% of budget), so
  the next compaction is far away. Keep the existing guarantee that the newest
  real user request always survives.
  → `_COMPACTION_LOW_WATER = 0.5`. Pinned by
  `test_a_triggered_compaction_cuts_to_the_low_water_mark`, whose assertion is
  deliberately *not* written in terms of that constant (which would hold for any
  value of it) but in terms of what the cut must buy: room for another step.
- [x] **Step 4: Append the eviction notice instead of prepending it**
  (`context.py:807-808`). Position 0 of the body must never be rewritten. Append
  it as the newest message so it becomes part of the append-only stream.
  → **Implemented as "insert before the newest request", not "append last".**
  Appending it last was measured to make the notice the final message of the
  body, and the last message is the one a provider reads as the turn to answer —
  the model would answer the notice instead of the user. Prepending rewrites
  position 0. Immediately before the newest request is the only placement that
  leaves every retained message byte-identical *and* keeps the user's words last.
  Pinned by `test_the_eviction_notice_never_becomes_the_current_request`.
- [x] **Step 5: Guard the post-turn rewrite** at `agent.py:3329-3343` so
  compaction and `_with_task_context` only run between turns, never inside a tool
  loop.
  → `_TURN_IN_PROGRESS_KEY`, set for the whole of `send_message` and read at the
  top of `_post_turn_maintenance` (hoisted above the point where `metadata` is
  rebound to the caller's `record_kwargs`). Recording, staging and checkpointing
  are deliberately *not* gated: they touch no payload. Pinned by
  `test_post_turn_maintenance_leaves_a_mid_turn_payload_alone`.
- [x] **Step 6: Run the focused tests** and confirm the eviction-notice
  round-trip (`_strip_eviction_notices` must still collapse cumulative counts).
  → `test_eviction_notices_do_not_accumulate_and_count_cumulatively` still passes;
  the notice is located by sentinel rather than by position, so the move is
  invisible to the round-trip.

### Deviation recorded

The plan's Step 4 was written as "append it as the newest message". That was
implemented, measured, and rejected: with `M = [… retained …, user(request),
user(notice)]` the notice becomes the current turn. The final placement keeps the
cache property the step was after (nothing before the cut is touched) and the
prompt semantics the plan did not consider.

## Task 3: Separate the output cap from the input reserve (I5)

**Files:**
- Modify: `agent/core/agent.py` (`_input_token_budget`, `_prepare_provider_context`,
  `_create`, `_stream_response`, `_retrieval_token_budget`,
  `_next_structured_output_budget`)
- Modify: `agent/config.py`, `agent/bootstrap.py`
- Modify: `tests/test_agent_integration.py`

- [x] **Step 1: Add a test asserting a short-context call still sends the full
  configured cap** (no truncation regression) **and a long-context call sends a
  cap clamped to the remaining room.**
  → `test_the_output_cap_follows_the_request_instead_of_capping_the_conversation`.
  The long case also proves the decoupling: a ~60k-token body is above the ~36k
  the old cap-derived budget allowed and gets past `_create` at all.
- [x] **Step 2: Add `output_reserve`** (default 8192, configurable) and use it —
  not `self.max_tokens` — as the input reserve:
  `_input_token_budget = context_window − output_reserve − overhead`. For
  deepseek this moves the compaction ceiling from ~57k to ~113k.
  → `shared.DEFAULT_OUTPUT_RESERVE`, a `BaseAgent.__init__` keyword propagated to
  sub-agents, resolved per provider by `bootstrap._active_output_reserve`, and
  validated in `config.py` plus documented in `config.example.json`.
- [x] **Step 3: Compute the sent cap per call.** In `_prepare_provider_context`,
  where the actual input estimate is already available, set
  `ctx.metadata["_output_cap"] = min(configured_cap, context_window − estimate − margin)`;
  read it in `_create` and `_stream_response` instead of `self.max_tokens`.
  `max_tokens` is not part of the prefix, so this is cache-neutral.
  → New `_payload_overhead` and `_output_room` helpers. **`_create` had to stop
  resolving `output_max_tokens` before forwarding it** — doing so turned the cap
  back into the input reserve and re-created the bug one level down, which the
  Step 1 test caught. `_configured_output_cap` now resolves it in one place.
- [x] **Step 4: Point `_retrieval_token_budget` and `_next_structured_output_budget`
  at the same reserve** so retrieval stops being starved (~1k → ~9k) and the
  output-budget escalation stops measuring against a budget it just shrank.
  → Retrieval now sizes against `output_reserve` in both branches.
  **Violation #17 is not a bug in effect, but the two forms are not the same
  number.** The old form is `current_budget + max(0, input_budget − estimate −
  256)`, with `input_budget` shrunk by `current_budget`; that equals the room when
  the room is the larger of the two and `current_budget` otherwise. Measured on
  this machine: 61413 tokens of room under a 64000 cap returned **64000** there and
  **61413** here. What is preserved is the escalation *decision*, because the caller
  retries only when this exceeds `current_budget` — below that both forms decline,
  and above it both return the room. So the rewrite is safe, but not because the
  terms cancel; the earlier claim of algebraic identity was wrong and is corrected
  in the code comment too. Expressing it as `_output_room` removes the dependence on
  `current_budget` appearing in the budget's reserve and never names a cap the
  window cannot hold.
- [x] **Step 5: Fix `_reserve_input_context`** (`bootstrap.py:47-63`) so the guard
  keys on the reserve rather than on `max_tokens >= context_window`, which never
  fires for this provider.
  → The guard now keys on the reserve. Note what it must *not* do: the plan's
  phrasing implied it should fire for deepseek, but firing means lowering the
  configured cap, which is the truncation regression this whole task exists to
  avoid. With the input budget no longer derived from the cap, a healthy cap
  needs no correction — so the guard is made correct for the configs it was
  written for (a cap that leaves less than the reserve free) and leaves 64000
  against 128000 alone. Pinned by
  `test_the_output_reserve_clamps_only_a_cap_that_would_starve_the_window`.
- [x] **Step 6: Keep `max_truncation_continuations` at 6.** Continuation requests
  reuse the identical prefix (`_build_continuation_context` copies `ctx.messages`
  after the partial assistant message is appended), so they are near-100% cached
  and the auto-continue path remains the safety net for long outputs.
  → Unchanged, as instructed. `_continue_truncated_response` calls `_create` with
  no explicit budget, so each segment gets the full configured cap whenever the
  window has room for it — the reserve is a floor on the room left, never a
  ceiling on the cap.

### Collateral test updates (all faithful, none weakened)

Three tests built deliberately tiny windows (`context_window=100`,
`max_tokens=20`) and relied on the cap doubling as the reserve. They now name
`output_reserve=20` explicitly, which is what they always meant.
`test_retrieval_budget_default_leaves_room_for_the_conversation` was rewritten to
use a deepseek-like `max_tokens=64000` with `output_reserve=8192` and to assert
the budget is *not* the cap-derived value — so it now tests the decoupling rather
than coincidentally matching it.

## Task 4: Verify

**Files:** none modified.

- [x] **Step 1: Run the focused suites** for each touched module.
  → `test_consolidation`, `test_runtime_contracts`, `test_agent_integration`,
  `test_context_assembler`, `test_usage_telemetry`, `test_web_channel`,
  `test_model_routing` — all green except the one pre-existing sandbox failure.
- [x] **Step 2: Run the full suite** and compare the FAILED set against the
  recorded baseline — use the `simple-agent-verify` skill's statement-multiset
  and baseline-compare discipline rather than eyeballing.
  → **16 failed / 2576 passed / 1 skipped**, and the FAILED set is *identical* to
  `/tmp/base_review_canon.txt` (16 entries, all `sandbox-exec: sandbox_apply:
  Operation not permitted` plus `test_build_components_loads_user_tool_plugins`).
  Baseline was 16 / 2570, so the six new tests are the whole delta and nothing
  regressed. The run was also done per task — after Task 1, Task 2 and Task 3 —
  so a regression would have been attributable to one change.
- [ ] **Step 3: Re-run the `usage_events` analysis** on a real session and compare
  against the baseline table above. Success criteria:
  - `phase='foreground'` and `'tool_step'` rows exist;
  - the <20% hit band stops dominating uncached tokens;
  - the count of rows with a changed `head_fingerprint` within a session drops to
    the number of genuine capability changes (target: 0);
  - `compacted=true` rows are a small fraction of calls, not the steady state.
  → **Cannot be completed in this session, and saying otherwise would be false.**
  `scripts/analyze_cache_hits.py` runs and reports the *same* 641 rows over
  2026-09-09 → 2026-09-21, because those are the rows already on disk: the
  Task 0 transport fix only takes effect when the agent runs again, so no
  `foreground`/`tool_step` row and no `head_fingerprint` exists yet. What the run
  does confirm is that the report's own guard rails work — it prints "no
  'foreground' / 'tool_step' rows" and "No row carries `head_fingerprint` yet"
  rather than silently showing a clean-looking table. Re-run this step after a
  real interactive session; that is the only way to close it.
- [x] **Step 4: Confirm no truncation regression** by exercising a long single
  response and a large single tool call, and confirming either one clean response
  or an auto-continued one with no duplicated seam text.
  → 17 truncation/continuation tests pass, including the OpenAI `length`-finish
  auto-continue, the seam-overlap trim, the structured-tool-output retry and the
  continuation-budget-exhausted error. The guarantee is also pinned structurally
  rather than only exercised:
  `test_a_compacted_body_still_leaves_room_for_the_full_configured_cap` asserts
  that a body at the compaction low-water mark still leaves room for the *entire*
  configured cap, so the per-call clamp cannot bite on a normally-operating
  session. Verified to discriminate: raising `_COMPACTION_LOW_WATER` to 0.95
  drops the room to 13,922 against a 64,000 cap and the test fails.

### The numbers, measured

`~/.agent/config.json` is deepseek with `context_window=128000`,
`max_tokens=64000`, no `output_reserve` (so the 8192 default applies). The fixed
overhead — system prompt plus tool schemas — is floored by the smallest input the
provider has ever billed for a sub-agent call: **6,331 tokens**.

| | input budget |
|---|---|
| before | 128,000 − 64,000 − 6,331 = **57,669** |
| after | 128,000 − 8,192 − 6,331 = **113,477** |

**+55,808 tokens** of usable conversation, with the configured 64,000-token cap
untouched. That is the plan's "~57k → ~113k" reproduced against the real config
rather than assumed.

---

## Risks

- **Retrieval in the user message changes what the model sees.** Tests assert on
  `ctx.system_prompt` (e.g. `test_web_channel.py:1110,1186,4150` monkeypatch
  `_compose_system_prompt`). Update those assertions deliberately.
- **Freezing `T` ships more schemas.** The real risk is behavioral (the model
  volunteering `schedule_create`), which `requires_request` already refuses. Do
  not weaken it.
- **Turn 1 of a session can never hit** (nothing is persisted yet), and a head
  change forces a re-persist. Do not promise 99% on the first request.
- **Tail growth from I4** is bounded by the retrieval budget but is a real,
  permanent increase in history size. Watch the `compacted` rate after Task 2.
- **Task 3 touches the budget math that guards every call.** Land Tasks 1 and 2
  first, then Task 3, so a regression is attributable to one change.
