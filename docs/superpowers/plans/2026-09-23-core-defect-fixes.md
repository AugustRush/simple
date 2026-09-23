# Core Defect Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if
> subagents available) or superpowers:executing-plans to implement this plan. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two state-corrupting defects in the agent core (an Anthropic
session loses its tool results; an in-turn compaction can lose the turn's own
request), and close the four lesser gaps the same survey turned up — without
touching the prefix-cache invariants established by
`2026-09-21-prompt-cache-prefix-stability.md`.

**Architecture:** One root cause explains both P0 defects: **`ctx.messages` has no
enforced contract.** It is at once the provider payload, the durable session record
(serialized to SQLite, restored cross-process), the input to compaction, and the
dedup source for retrieval — four roles whose requirements conflict, with nothing
asserting the one property all four need: *every message is a JSON-native dict*.
`transport` writes SDK objects into it; `memory` reads them assuming dicts. Fix the
boundary, then the two downstream consumers that silently mis-handle what they are
given.

**Tech Stack:** Python, SQLite, pytest. No frontend change.

---

## Status

**Implemented, verified, committed. Tasks 0-6 are done, in seven commits.**

| run | failed | passed | skipped |
|---|---|---|---|
| before any fix | 24 | 2634 | 1 |
| after all fixes | 16 | 2650 | 1 |

`2650 − 2631 = 19`: the tree gained exactly the 19 tests this work added — the 18
this plan lists, plus the reader-guard test from note 5 — and all 19 pass. The 16
that remain are the known environment set (12
`test_sandbox_conformance`, 2 `test_user_tool_isolation`, `test_builtin_tools`,
`test_agent_integration::test_build_components_loads_user_tool_plugins`) — the
`FAILED` list is byte-identical to the recorded baseline, so **no test changed
state except the ones this plan intended to change**. The "before" figure's 24 is
those same 16 plus the 8 red-by-design tests that existed at the time.

Measured in two passes, because `tests/test_channel_layer.py` cannot be read from
inside the harness sandbox (see *Verification protocol*): 2585 passed / 16 failed
with it excluded, and 65 passed when it runs alone. Both passes need a fresh
`TMPDIR` and `$HOME/.local/bin` on `PATH`.

**Four things the plan got wrong, corrected during implementation** — each is
recorded at its task below: the `input_tokens` direction (Task 4), the
position-in-chunk fallback (Task 5), the "silent" checkpoint failure (Task 6),
and one defect the plan never listed (Task 5).

---

## Corrections to the initial survey — read before trusting anything else

The first pass over-reported. Three corrections, all in the direction of *less*
alarm, and all load-bearing for the ordering below:

1. **The recent cache work did not introduce or widen P0-1.** `b93246a`'s own commit
   message states that before it, `_prepare_provider_context` ran `compact_messages`
   **unconditionally on every provider step**. Moving the cut to the turn boundary
   *reduced* how often the bug fires (every step → once per turn). P0-1 predates the
   cache work and was never latent. Do not attribute it to `b93246a`, and do not
   "fix" it by reverting anything.
2. **The usage asymmetry is not live today.** `cache_control` is never sent anywhere
   in `agent/`, so Anthropic reports `cache_read_input_tokens = 0` and its
   `input_tokens` *is* the total. For every provider actually configured
   (`deepseek`, `huoshan`, `qwen`, `opencodego`, `ApiKeyFun` — all `api_format:
   "openai"`), `prompt_tokens` includes cache hits and `prompt_cache_hit_tokens` is a
   subset, so `total_tokens = input + output` is correct and calibration receives a
   true `|P_n|`. It is a **guard to install before caching is enabled**, not a bug to
   fix urgently.
3. **The dedup regression is real but narrow.** `3828df4` changed
   `_build_user_message_content` from `return user_message` to always prefixing
   `turn_context`, and staging stores the raw `turn_input.text`
   (`runtime/contracts.py:393`) — so exact-equality dedup for *user* entries can no
   longer match, where it did before. But assistant entries still match
   (`build_final_message` stores a plain string), and the whole path is disabled from
   the second turn on via `_has_provider_checkpoint`. Its practical reach is
   therefore *"armed only when the checkpoint save failed"* — which couples it to
   Task 6.

---

## Verified evidence

Reproductions live outside the tree and are not committed:

```bash
cd /Users/shike/Desktop/simple
./.venv/bin/python /tmp/repro_core.py    # blocks dropped + request dropped
./.venv/bin/python /tmp/repro_core2.py   # same, starting from a real anthropic.types.Message
```

**Both are stale after the fix, and re-running them is misleading.** They are
kept here as the *evidence that the defects were live*, not as a post-fix check.
`repro_core.py` hand-builds the block list, so it bypasses the transport — the
boundary the fix is at — and still prints BROKEN, correctly. Its section B calls
`fit_to_budget` without `protected`, i.e. the old calling convention.
`repro_core2.py` calls `build_assistant_message(None, response, ...)`, relying on
the old body never touching `self`; the new body does
(`self._json_native_blocks`) and raises `AttributeError`.

The post-fix confirmation is `/tmp/repro_core3.py`, which starts from a real
`anthropic.types.Message`, goes through a real transport instance, and checks the
shape the fix actually produces. Measured output:

```
A. real Message -> real transport -> real compaction
  stored assistant content: ['text', 'tool_use']
  [ok ] the stored message is JSON-native (no SDK objects)
  [ok ] tool_use survived compaction -- kept 1
  [ok ] tool_result survived compaction -- kept 1
  [ok ] the checkpoint round-trip stores blocks, not a repr

B. an interjection must not displace the turn's request
  with protected=[request]: ['ORIGINAL-TURN-REQUEST xxxxxx']
  [ok ] the request is what the provider reads as the turn to answer
  without protected (fallback): ['[context-eviction] 1 earlier', '<user_interjection>stop</use']
  [ok ] the newest-user-message heuristic still names the interjection
```

Section B is the whole point in two lines: **the old path still loses the
request** (it answers an eviction notice, with the interjection as the newest
message), and the new one does not. That is the defect and the fix, side by side
in one run.

`repro_core2.py` output, verbatim:

```
stored assistant message content: ['TextBlock', 'ToolUseBlock']
after the turn-boundary compaction that runs every turn:
  user      'please list the files'
  assistant ['TextBlock', 'ToolUseBlock']
  tool_use on the wire: True, tool_result on the wire: False
  -> *** BROKEN: tool_use sent without tool_result ***
checkpoint round-trip (json.dumps(..., default=str)):
  assistant content as stored: ["TextBlock(citations=None, text='let me look', type='text')", ...
```

Why this is live, not theoretical:

- The `anthropic` provider is configured with a key and Claude models
  (`~/.agent/config.json`: `claude-opus-4-5`, `claude-sonnet-4-5`,
  `claude-haiku-3-5`), and `config.example.json` ships it as a default option.
- Both paths produce SDK objects: `create()` returns
  `client.messages.create(...)` directly (`transport.py:381`) and `stream()` returns
  `await stream.get_final_message()` (`transport.py:415`). Neither normalises.
- Only `parse_response` reads blocks by attribute (`transport.py:435-439`), and it
  reads the **live response**, never a stored message — so normalising the stored
  form breaks nothing. This is what makes Task 1 small.
- `compact_messages` → `fit_to_budget` → `_repair_tool_history` (`context.py:768`)
  runs unconditionally, and the result is assigned back to `ctx.messages`
  (`agent.py:1550`, `agent.py:1593`) — the loss is persistent, not per-request.

---

## First principles

- **A boundary owns its format.** `ctx.messages` is serialized (`store.py:1403`,
  `default=str`), restored (`channels/base.py:257`), and replayed to a provider. The
  layer that *writes* it must emit what every reader can consume; the readers must
  not each re-derive the type. Today `transport` writes objects and `memory` guards
  with `isinstance(block, dict)` in two places — a patch that hides the defect
  instead of removing it.
- **A turn is the unit of intent.** Compaction may drop history; it may not drop what
  the turn is *for*. "The newest user message" is a proxy for the turn's request that
  a mid-turn interjection falsifies.
- **A guard that cannot be measured is not a guard.** The eviction notice's position
  was decided by measurement (cache plan, Deviation 2: *"it made the notice the
  message a provider reads as the turn to answer"*). The interjection reintroduces
  exactly that shape through a different door — evidence that the rule should be
  stated over the *request object*, not over message position.

---

## Task 0 — Land the failing tests first

No production change. These four tests must be **red** on the current tree and green
after their task. Without them the fixes are unverifiable.

- [x] Create `tests/test_core_defect_regressions.py`.
- [x] `test_anthropic_tool_result_survives_compaction`: build a real
      `anthropic.types.Message` with a `ToolUseBlock`, pass it through
      `AnthropicTransport.build_assistant_message`, append the matching
      `tool_result`, run `ContextManager.fit_to_budget`, and assert the counts of
      `tool_use` and `tool_result` are equal. (RED now.)
- [x] `test_turn_request_survives_an_interjection`: a message list of
      `[user(request), user(interjection)]` under a budget that forces a cut; assert
      the original request is retained **and is the last message**. (RED now.)
- [x] `test_anthropic_checkpoint_round_trip_preserves_blocks`: `json.loads(json.dumps(
      messages, default=str))` must not contain a Python repr. (RED now.)
- [x] `test_stream_without_finish_reason_is_not_a_clean_end_turn`: a fake chunk
      sequence carrying no `finish_reason` must not report `end_turn`. (RED now.)
- [x] Record the RED run and the full-suite baseline in the plan status before
      starting Task 1.

## Task 1 — Normalise provider content at the transport boundary (P0)

- [x] `AnthropicTransport.build_assistant_message` (`transport.py:448-449`): return
      `{"role": "assistant", "content": [b.model_dump() for b in response.content]}`.
      `model_dump()` yields exactly the wire shape (`ToolUseBlockParam`,
      `ThinkingBlockParam`), **including `signature`** on thinking blocks — dropping
      that field would break multi-turn extended thinking, so assert it survives.
- [x] Confirm nothing else needs changing: `parse_response` (`transport.py:432-441`)
      and `_thinking_text` (`transport.py:368-375`) read the live response and are
      unaffected. Re-run the grep for attribute-style block access as a check, not as
      a substitute for the test.
- [x] Decide and record the invariant in a comment: **every message appended to
      `ctx.messages` is JSON-serializable.** Note that `store.py:1403`'s
      `default=str` stays as a safety net but must no longer be *load-bearing*.
- [x] Consider (and only then decide) whether `memory`'s two `isinstance(block, dict)`
      guards (`context.py:558-563`, `context.py:640-644`) should keep tolerating
      non-dict blocks. **Reversed on re-reading — see note 5 below.** The original
      recommendation ("keep them — they are what would have caught this") was
      backwards: they did not catch it, they *concealed* it. The protocol paths
      now raise via `_content_blocks`; the two text-extraction paths still skip,
      deliberately and with the reason stated at each site.
- [x] Green: Task 0's first and third tests, plus `tests/test_consolidation.py` and
      any transport suite.

## Task 2 — Pin the turn's own request (P0)

- [x] `_prepare_turn` (`agent.py:2870-2877`) already holds the message object it
      appends. Record it by identity: `ctx.metadata["_turn_request_message"] = <that
      dict>`, and clear it in `send_message`'s `finally` beside the other
      `_selected_tools` / `_provider_step` pops (`agent.py:3459-3464`).
- [x] Thread it into compaction: `compact_messages` / `fit_to_budget` take an
      optional `protected: Sequence[dict] | None`. When supplied, the set of
      never-dropped units is derived from those objects **by identity**
      (`_protected_indexes`), and `_with_notice_before_protected_request` places
      the notice immediately before the last protected object. Reuse the existing
      identity idiom rather than inventing a second one.
- [x] Keep `_is_real_user_request` as the fallback when nothing is supplied — the
      scheduler's `stateless` path and `_compact_body_for_turn`'s manager-less branch
      still need it.
- [x] Pass it from both call sites: `_compact_body_for_turn` (`agent.py:1550`) and the
      emergency cut in `_prepare_provider_context` (`agent.py:1593`).
- [x] Note the interaction with interjections explicitly in the docstring:
      `_inject_pending_interjections` (`agent.py:2734`) appends a real user message
      mid-turn, and that is precisely what must not displace the protected request.
- [x] Green: Task 0's second test, plus the compaction suite.

## Task 3 — Charge the retrieval budget for what the message actually carries (P2)

- [x] `allocate` (`context_assembler.py:356-361`) charges
      `estimate([{"role": "user", "content": current_user_content}])`, and
      `_prepare_turn` passes only the raw `user_message` (`agent.py:2810-2814`). The
      message actually appended is `turn_context + "\n\n" + user_message`
      (`agent.py:428`), so `remaining` is overstated by the size of `turn_blocks`
      (checkpoint + retrieved context + skills + policy + time) and retrieval is sized
      against room that does not exist.
- [x] Restructure `_prepare_turn` into two phases: assemble every block **except**
      retrieval, measure the assembled content, allocate, then append the retrieval
      block. This is circular today only because retrieval is sized before the
      message it rides in is built.
- [x] Test: a turn with a large checkpoint/handoff block must yield a smaller
      retrieval budget than the same turn without one.

## Task 4 — Split total input from uncached input (P2, guard)

Install this **before** anyone enables caching; on its own it changes no behaviour.

- [x] `ProviderUsage` (`usage.py:9-17`): add `total_input_tokens`, keep
      `input_tokens` meaning *uncached*. `total_tokens` uses the total.
- [x] `extract_provider_usage` (`usage.py:30-57`): Anthropic total =
      `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (read the
      third field — it is currently never read anywhere, so a cold cache write is
      invisible); OpenAI total = `prompt_tokens`.
- [x] Point calibration at the total: `_observe_provider_usage`
      (`agent.py:1669-1673`) must feed `|P_n|`, not the uncached slice, or
      `_MIN_CALIBRATION = 1.0` clamps and the estimator can never learn.
- [x] Test: synthetic Anthropic usage with `cache_read_input_tokens > 0` and
      `cache_creation_input_tokens > 0` → total reflects both; a `foreground` row
      records the total.

## Task 5 — Gateway robustness (P2)

The six configured providers are third-party OpenAI-compatible gateways, so the
transport's assumptions about well-formed streams are load-bearing.

- [x] `finish_reason` defaults to `"stop"` (`transport.py:588`) and is only
      overwritten `if choice.finish_reason` (`:665`). Track `saw_finish`; if the
      stream ends without one, surface it as incomplete rather than as a clean
      `end_turn` — otherwise truncated text is committed as the answer.
- [x] `tool_calls_acc` is keyed on `tc_delta.index` (`transport.py:648`), which the
      SDK types as `Optional[int]`. When it is absent, every call merges into one
      accumulator (id/name overwritten, arguments concatenated) and one corrupt call
      is executed. Fall back to the delta's position in `delta.tool_calls`.
- [x] `tool_result_rollback_count` returns `tool_call_count` (`transport.py:765`)
      while `build_tool_result_messages` appends `len(zip(tool_calls, results))`
      (`:759-763`); when `results` is shorter, the rollback at `agent.py:2045-2049`
      deletes one message too many and cuts real history. Make the count derive from
      what was actually appended.
- [x] Tests: a stream with no `finish_reason`; a delta with `index=None`.

## Task 6 — Stop arming the dedup path by accident (P2)

- [x] `_recent_unconsolidated_context` (`context.py:1282-1324`) compares staged text
      to message content by exact equality. Either compare against the assembled
      content's tail, or — preferred — keep a per-session set of injected content
      hashes in `ctx.metadata` and skip what was already injected. `blake2b` is
      already the project's idiom (`payload_shape.py:31-39`); reuse it.
- [x] `save_provider_checkpoint` failures are swallowed by
      `_suppress_with_log` (`agent.py:3635`) while `_has_provider_checkpoint` is set
      only on success (`:3640-3644`). A silent failure therefore *keeps the broken
      path armed*. Log the failure at warning level and decide deliberately whether
      the flag should mean "a checkpoint exists" or "a save was attempted".
- [x] Test: with the dedup path forced on, two identical consecutive turns must not
      inject the same block twice.

---

## Implementation notes — where this plan was wrong

1. **Task 4: `input_tokens` keeps meaning the *whole prompt*, not the uncached
   part.** The plan said to redefine it as uncached and add a separate
   `total_input_tokens`. That is the wrong direction.
   `usage_events.input_tokens` and `scripts/analyze_cache_hits.py` already define
   the column as the total, with `uncached = input − cached` and
   `hit = cached / input`; redefining it would have pushed every existing hit
   rate above 100% and forced the analysis script to change. What was actually
   broken was Anthropic *extraction*, which returned the uncached remainder as
   though it were the total. So the fix is provider-neutral extraction —
   `extract_provider_usage` sums `input_tokens + cache_read + cache_creation`
   whenever `prompt_tokens` is absent — plus `uncached_input_tokens` and
   `cache_creation_input_tokens` as named quantities. One column, one meaning, no
   consumer changed.

2. **Task 5: "position within the chunk" does not identify a tool call.** Two
   calls streamed one per chunk are *both* at position 0, so that fallback merged
   exactly the case it was meant to separate — caught by the regression test, not
   by reading. The delta's own shape is what identifies a call: it *opens* with an
   `id` and a `name`, and every delta after that is an argument fragment for the
   call already open. An opening delta takes a fresh slot; a continuation appends
   to the open one.

3. **Task 5 also turned up a defect this plan never listed.**
   `_parse_tool_arguments` reports unparseable arguments as
   `{"_malformed_arguments": <raw>}`, and nothing anywhere consumed that marker —
   so a call cut mid-arguments was *executed* with a dict of the wrong shape.
   `has_incomplete_tool_calls` now treats it as incomplete protocol, which routes
   it to the existing `_recover_incomplete_tool_response` retry.

4. **Task 6: the checkpoint failure was never silent, and no new state was
   needed.** `shared._suppress_with_log` already logs at WARNING with
   `exc_info=True`, so the failure was always visible. And once the visibility
   test is correct, a failed save no longer *arms* anything — the path behaves
   correctly whether or not a checkpoint exists. Adding a second flag to
   compensate for a bug elsewhere would have been the wrong trade, so
   `_has_provider_checkpoint` keeps its honest meaning ("a checkpoint exists") and
   is unchanged.

5. **Task 1: the reader's tolerant guards had to go loud, and the plan's own
   first-principles section said so.** The plan's Task 1 bullet recommended
   keeping the `isinstance(block, dict)` skips on the grounds that "they are what
   would have caught this". They are not — they are what *hid* it. Measured by
   restoring the old guard: a history whose `tool_use` block is an SDK object
   compacts to `[[], ['ToolUseBlock']]` — the `tool_result` is deleted with no
   error, no log and no trace, which is the P0 itself. The same section of this
   plan had already called these guards "a patch that hides the defect instead of
   removing it"; the bullet contradicted it and the bullet was wrong.

   So the protocol readers now share one `_content_blocks` helper that raises
   `ContextLimitError` (which `_format_agent_error` turns into a visible error)
   instead of skipping. That is safe because every writer was checked to emit
   dicts: both `build_assistant_message`s, both `build_tool_result_messages`,
   both `build_final_message`s, both `image_content_block`s and
   `_build_user_message_content`. The two text-extraction paths
   (`_checkpoint_summary`, the staged-turn visibility check) still skip — there
   the cost is a line of summary text, not a deleted tool result — and each says
   so at the site.

---

## Verification protocol

Per `simple-agent-verify`, every run needs a fresh `TMPDIR` and `$HOME/.local/bin` on
`PATH` or the failure set is not deterministic:

```bash
cd /Users/shike/Desktop/simple
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
T=$(mktemp -d /tmp/simple-suite-XXXXXX)
TMPDIR="$T" ./.venv/bin/python -m pytest tests/ -q
```

**`tests/test_channel_layer.py` is unreadable from inside the harness sandbox.**
The shim at
`/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/vendor/shim/sitecustomize.py`
brokers every file open; a *read* of that path is routed to the host for
"sensitive content" approval and the default window is **5 seconds**
(`_broker_response_timeout_seconds`). When nobody answers it in time pytest dies
during collection with `PermissionError: Sensitive content approval timed out`
and `Interrupted: 1 error during collection`, which aborts the whole run and
looks like a code failure. It is not one. The same gate blocks non-Python
readers, where there is no broker to route through at all — `wc -c
tests/test_channel_layer.py` simply hangs and is killed at the 2-minute mark
(exit 137).

Two ways through, both used here:

```bash
# 1. give the approval window time to be answered, and lift the sandbox
CODEBUDDY_SANDBOX_BROKER_READ_TIMEOUT_MS=120000 ./.venv/bin/python -m pytest tests/test_channel_layer.py -q

# 2. or keep it out of the run that must not be aborted, and run it separately
./.venv/bin/python -m pytest tests/ --ignore=tests/test_channel_layer.py -q
```

Prefer (2): one denial anywhere in the collection pass costs the entire suite,
and splitting means the other 60-odd files are never hostage to it.

- Baseline, measured on this tree rather than taken from the recorded figure:
  **16 failed / 2631 passed / 1 skipped**. The recorded baseline in
  `2026-09-21-prompt-cache-prefix-stability.md` says 2577 passed; the tree has
  gained ~54 tests since, so compare the `FAILED` set, not the count. The 16 are
  `test_sandbox_conformance` (12), `test_user_tool_isolation` (2),
  `test_builtin_tools::test_shell_allows_output_dir_writes` and
  `test_agent_integration::test_build_components_loads_user_tool_plugins` — all
  `sandbox-exec: sandbox_apply: Operation not permitted`, i.e. environment, not
  code. A new entry in `FAILED` is a regression; a wall of `ERROR` confined to
  one file is the `TMPDIR` trap.
- RED checks performed, each by disabling the fix and restoring it: the
  retrieval-budget test reported `17930 < 17930` (identical budgets) and the
  dedup test re-injected the staged turn. Both were red before their fix.
- Run the suite after **each** task, not once at the end.
- For each fix, do a RED check: disable the fix and confirm the corresponding Task 0
  test goes red, then restore.
- Commit per task, except where two tasks are not separable at a hunk boundary.
  Committed as seven: `docs(plan)`, `test(core)`, then Task 1; **Task 2 and Task 3
  together** (they meet in `_prepare_turn`, where the request object is both the
  thing being named and the thing retrieval is sized against); Task 4; Task 5;
  Task 6. `git diff <base>..HEAD` reproduces the verified working tree exactly
  (+393/−108 over the six files), which is how the split was checked.
- The per-task RED property was verified at the Task 2+3 commit by stashing the
  remaining work: exactly 8 tests failed there, and they were precisely Task 4's
  two, Task 5's five, and Task 6's one. Every Task 1/2/3 test was green.

## Out of scope

- Decomposing `send_message` (525 lines, nesting 7, ~15 cross-scope mutable locals).
  Real, already recorded, and not worth touching without a differential test harness
  in place first.
- `AppCtx` re-listing ~218 of 237 names.
- Any change to the head/body invariants in `2026-09-21-prompt-cache-prefix-stability.md`.
  Tasks 1, 2 and 5 must leave `_compact_body_for_turn`'s single-cut-at-the-boundary
  property and the `S ‖ T ‖ M` ordering intact.
