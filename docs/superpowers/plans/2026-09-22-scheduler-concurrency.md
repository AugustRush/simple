# Scheduler Concurrency Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the scheduler's throughput independent of how long any single run
takes, so that unrelated tasks — and unrelated workflows — stop waiting on each
other; keep the concurrency cap honest while doing it; and never run the same
work twice.

**Architecture:** A scheduler *tick* and a *run* are different time scales, and
the current code conflates them: `run_once` claims a batch and then waits for the
whole batch to finish, so the poll loop's period is `max(run duration) +
poll_seconds`. The fix is to make a tick mean "deliver signals, claim what can
start, start it" and never wait for what it started. The poll interval becomes a
fallback for clock-scheduled work rather than the unit of latency, and because a
workflow hop is a signal round-trip through that same tick, killing the tick's
coupling to run duration also removes the per-hop poll penalty.

Three supporting rules follow, and each is a real defect today:

1. **The cap must be enforced where claims are made.** `max_concurrent_runs`
   currently bounds one poll batch, not the process; manual runs bypass it
   entirely.
2. **A lease must span claim → terminal state.** It is renewed only *after* the
   semaphore is acquired, so a claimed run queued for a slot has an unrenewed
   lease and can be declared abandoned while it is still alive — after which the
   work runs a second time.
3. **A derived "now" must be claim time + real elapsed.** `run_now()` starts its
   monotonic clock after the semaphore, so every timestamp written for a queued
   run is early by the queue wait. That undercount is currently what *hides*
   defect 2 — the ownership check is evaluated at a time in the past and passes.

**Tech Stack:** Python 3.13, asyncio, SQLite (WAL), pytest. No frontend change.

---

## Status

**Implemented and committed 2026-09-22** (`8a23f2b`, before the amend below —
check `git log` for the hash that actually shipped). Tasks 0–4 and 6 are done;
Task 5's measurement was taken and its decision recorded (it is *not* to change
the default). Every claim in the diagnosis was measured before the change and
re-measured after.

| | Before | After |
| --- | --- | --- |
| **D1** — an unrelated task due at +0.50s starts at | +3.18s | **+0.55s** |
| **D2** — claims per tick | `limit=10`, regardless of free slots | ≤ free slots, every path |
| **D3** — cost of a workflow hop | one poll interval | one step |
| **D4** — a run that queued for a slot | ran **twice** (one `interrupted`) | once, `succeeded` |
| **D5** — `finished_at` error after a 2.5s queue wait | 2.53s early | < 1s (asserted) |
| Full suite | 16 failed / 2577 passed / 1 skipped | 16 failed / **2585** passed / 1 skipped |

The FAILED set is identical as a set before and after; the +8 passed is exactly
the eight tests added. The 16 are the pre-existing sandbox set
(`sandbox-exec: sandbox_apply: Operation not permitted`, plus
`test_build_components_loads_user_tool_plugins`) and this work does not touch
them.

Eight tests were added, each confirmed RED for its intended reason before the
change and GREEN after (the last two are regression guards rather than defect
tests, and the final one was verified against a deliberately broken form — see
Task 4 Step 1):

| Test | File | RED reason observed |
| --- | --- | --- |
| `test_a_long_run_does_not_delay_a_task_that_becomes_due` | `test_scheduler.py` | `runs did not settle within 8.0s: {'long': ['running'], 'later': []}` |
| `test_the_scheduler_does_not_claim_more_than_it_can_start` | `test_scheduler.py` | `claimed [1, 1, 1] runs for one slot` |
| `test_a_run_waiting_for_a_slot_keeps_its_lease` | `test_scheduler.py` | `the queued run ran 2 times` |
| `test_a_run_that_queued_records_the_time_it_actually_finished` | `test_scheduler.py` | `finished_at is 2.53s from the real clock at completion` |
| `test_a_claim_that_never_starts_gives_its_slot_back` | `test_scheduler.py` | `assert 1 == 0` |
| `test_a_step_runs_when_its_upstream_finishes_not_at_the_next_poll` | `test_scheduler_workflows.py` | `TimeoutError` (nothing ran for 8s with `poll_seconds=3600`) |
| `test_a_wake_that_arrived_before_the_wait_is_drained_once` | `test_scheduler.py` | added as a guard, not from a defect — see Task 4 Step 1 |

Three reproductions were checked in while the diagnosis was being established —
each self-contained (its own temporary database), each printing a timeline. They
are cited by name throughout the diagnosis below; **the files no longer exist**, so
those citations are provenance for the pasted output, not instructions to run:

```bash
# HISTORICAL — these three have been deleted; see the paragraph below.
cd /Users/shike/Desktop/simple
./.venv/bin/python scripts/repro_scheduler_tick_blocking.py   # D1, ~5s
./.venv/bin/python scripts/repro_scheduler_lease_wait.py      # D4, ~17s
./.venv/bin/python scripts/repro_scheduler_lease_masked.py    # D5, ~15s
```

The third was the *negative* case and the subtlest of the three: it printed a
clean result, and the point was that the clean result was an artefact of D5
rather than evidence that D4 was unreachable.

**They have since been deleted**, as this section said they should be once Task 0
converted them into tests: every scenario they exercised is now a test, and a
standalone reproduction that duplicates a test is a second thing to keep true.
The before/after numbers they produced are the table above.

`scripts/measure_scheduler_run_cost.py` is kept. It is not a reproduction of a
defect — it is the evidence behind Task 5's decision, which cannot be encoded as
a test because the answer depends on the operator's provider and MCP setup. Re-run
it before revisiting the cap.

### Three deviations from this plan as written

All are improvements, recorded so the next reader does not have to guess why the
code does not match the steps:

1. **The wake is set in `_execute_with_limit`, not in `_execute_claimed`'s
   `finally`** (Task 4 Step 2). `_execute_claimed` is not reached on every path
   that ends a claim: a run cancelled *while waiting for a slot* returns from the
   acquire loop without ever entering it. Setting the event one level up covers
   every exit — terminal state, cancellation before execution, and release — which
   is what Step 2 actually asked for ("also set it after a claim is released").
2. **`_start_background_claim` gained a `forget` done-callback.** Registering the
   slot before the coroutine has run means a task cancelled in that window never
   reaches the `finally` that gives the slot back, and `_free_slots()` counts these
   entries — so the scheduler would keep believing a slot is busy until it claimed
   nothing at all. `shutdown()` is exactly that window. This was latent before
   (the old `limit=10` did not read the dict) and Task 2 made it matter.
   `test_a_claim_that_never_starts_gives_its_slot_back` covers it.
3. **`_free_slots()` reads the existing `_active_tasks`, not a new `_inflight`
   set** (Task 2 Step 1). `_active_tasks` already had exactly the required
   lifecycle — written at claim (`_start_background_claim` and
   `_execute_claimed`), cleared in `_execute_with_limit`'s `finally` — and
   `health()["active_runs"]` already reported from it. A second set would have
   been the same fact stored twice, with the two able to disagree. Step 1's
   intent ("track in-flight runs explicitly, register on both paths") is met; only
   the container differs.

### Still open

- **Task 1 Step 2's "record this in the commit body" — done.** The commit body
  states that the timestamp fix (D5) is a *consequence* of moving the clock's
  origin to claim time, not a separate edit.
- **Task 6 Step 4** (a real session, end to end) is still not done and cannot be
  done here: no provider credentials. See Task 6.

---

## First principles

- **A tick must not await its own work.** If it does, the scheduler's period is
  set by its slowest job, and every unrelated job inherits that latency. This is
  the whole complaint; everything else is a consequence or a guard.
- **Latency should come from events, not from the clock.** A poll interval is the
  right fallback for "is anything due?" and the wrong mechanism for "the step
  that just finished unblocked the next one".
- **`max_concurrent_runs` must mean one thing.** A cap enforced in one code path
  and not another is not a cap.
- **A lease is a claim on a task, so it lives from claim to terminal state.**
  Any window in which a claimed run is not renewing is a window in which the
  store may legitimately decide the run is abandoned.
- **Queued time is real time.** A run that waited for a slot must record having
  waited, or its history lies and, as here, its own guards misjudge it.

---

## The diagnosis

All numbers are from this machine on 2026-09-22, working tree at `64e2538` plus
the uncommitted scheduler work (see the coordination note at the end). The three
`scripts/repro_scheduler_*.py` files cited below **have since been deleted** —
their scenarios are tests now (see Status) — so those citations record where the
pasted output came from rather than pointing at a file you can run.

### D1 — The tick is a batch barrier (the reported symptom)

`run_forever` awaits `run_once`, and `run_once` awaits every run it claimed:

```python
# agent/scheduler/runtime.py:157-173
while True:
    await self.run_once()                 # returns only after the whole batch
    await asyncio.sleep(self.poll_seconds)
```

```python
# agent/scheduler/runtime.py:143-146
if claimed:
    await asyncio.gather(
        *[self._execute_with_limit(item.task, item.run) for item in claimed]
    )
```

So while anything is running, the loop does not deliver signals, does not claim,
and does not poll. **Measured** (`scripts/repro_scheduler_tick_blocking.py`, `poll=0.1s`, cap 3, two
of three slots free throughout):

```
[ 0.02s] START A-long
[ 3.03s] END   A-long
[ 3.18s] START B-unrelated     <- B was due at +0.50s
```

B was due at +0.50s and started at +3.18s. The delay is A's duration, not the
poll interval, and it is not a cap effect — two slots were idle.

### D2 — The cap does not mean what it says

| version | where the semaphore lives | effect |
|---|---|---|
| `HEAD` | created **inside** `run_once`, per batch | bounds one batch only; `run_task_now`/`retry_run` bypass it entirely |
| working tree | instance attribute `self._run_semaphore` (line 64), acquired in `_execute_with_limit` (line 206) | now genuinely process-wide, including manual runs |

Verified against `HEAD` in a worktree (`cap=1`, two manual runs): both started at
`+0.22s` — `HEAD` runs them concurrently, i.e. the cap was not enforced at all for
manual work. The uncommitted change fixes that scope. It also creates D4, because
manual runs now queue behind clock runs.

### D3 — A workflow hop costs a full poll

Workflow steps are chained by signals, not by in-process calls: a step's
completion emits a signal, and `deliver_signals` turns pending emissions into
queued runs. `deliver_signals` is called from exactly one place in production —
the top of `run_once` (`runtime.py:107-116`). Therefore each hop costs at least
one tick, and an N-step chain costs at least `N × poll_seconds` of pure waiting
before any step's own duration is counted. The tests state this model outright:
`for hop in range(6): await service.run_once(now=NOW + timedelta(seconds=30 * hop))`.

With `poll_seconds` defaulting to 30 (`agent/config.py:101`), a 10-step chain
pays ≥ 5 minutes of waiting. D1 then multiplies it: any other running task makes
each hop cost that task's remaining duration instead.

### D4 — A run waiting for a slot can be declared abandoned, then run again

`_renew_lease` is started at `runtime.py:515-517`, inside `_execute_claimed` —
which runs only *after* the semaphore is acquired at line 206. A claimed run
waiting for a slot therefore holds a lease that nothing renews. If the wait
exceeds `lease_seconds` (default 300), the next `claim_due_tasks` calls
`_recover_stale_runs_in_transaction` (line 2606), which sees `lease_until < now`
with the run still `running` and does two things (lines 2786-2818): marks the run
`interrupted`, and clears `active_run_id` while setting `next_run_at` back to the
run's `scheduled_for` — i.e. into the past, so the task is due again.

**Measured** (`scripts/repro_scheduler_lease_wait.py`, `cap=1`, `lease=3s`, `poll=0.5s`, both runs
manual so the loop keeps ticking while they wait):

```
[ 0.22s] EXEC A-holds-slot  run=5d229c58
[ 8.25s] EXEC B-waits       run=f73c9159
[ 8.76s] EXEC B-waits       run=2f66aa8a

--- B: 2 run(s) ---
  f73c9159  status='interrupted'  error=''
  2f66aa8a  status='succeeded'
```

**The same work ran twice.** The first execution's result was discarded, and the
run is recorded `interrupted` **with an empty error** — an operator sees a
phantom interruption and a duplicated side effect, with nothing to explain
either. The same script against `HEAD` shows one run, because `HEAD` has no
semaphore on the manual path (D2) so B never queued.

### D5 — Timestamps are early by the queue wait, and that is what hides D4

`run_now()` is built from a monotonic start captured at `_execute_claimed` entry:

```python
# agent/scheduler/runtime.py:507-512
monotonic_start = loop.time()
def run_now() -> datetime:
    elapsed = max(0.0, loop.time() - monotonic_start)
    return run.started_at.astimezone(UTC) + timedelta(seconds=elapsed)
```

`run.started_at` is the **claim** time, but `monotonic_start` is taken **after**
the semaphore, so any queue wait is subtracted from every derived timestamp. A run
that waited 6s reports times 6s early, and its `finished_at` is wrong by the same
amount.

This is not cosmetic: the lease-ownership check at line 561 is evaluated at
`run_now()`, so a run whose lease genuinely expired at `t+3s` is asked "do you
still own it?" at a claimed time of `t+0.7s` and answers **yes**. That is the only
reason `scripts/repro_scheduler_lease_masked.py` reports `succeeded` rather than `interrupted`. Two
defects cancel; fix either alone and the other becomes visible.

### What is *not* wrong

- **Steps of one workflow are serial by design.** They are a chain. Not a defect.
- **`active_run_id IS NULL` in the claim query** already prevents a task
  overlapping itself. Keep it.
- **No lock serialises unrelated tasks.** `turn_lock` is per-`RuntimeSessionState`
  (`agent/runtime/contracts.py:241`) and each run builds its own state
  (`agent/cli.py:913`); `_SYNC_TOOL_EXECUTOR_LOCK` guards lazy pool creation only;
  `_APPROVAL_LOCK` is used for `.locked()` alone. The interference is entirely
  D1 + D2, so no locking change is needed.
- **`max_concurrent_runs` is config-only** (`scheduler.max_concurrent_runs`,
  default 3) and not exposed in the UI. That is a reasonable choice; Task 5 only
  revisits the *default*.

---

## Task 0 — Baseline and the failing tests

- [x] **Step 1: Pin the baseline.** Run the full suite and record the FAILED set.
  It is expected to be the 16 pre-existing `sandbox-exec` failures; compare the
  FAILED *set*, not the count, and use the environment traps in the
  `simple-agent-verify` skill (fresh `TMPDIR`, `PATH` including `~/.local/bin`,
  `./.venv/bin/python`). Do not proceed on a different baseline.

- [x] **Step 2: Add the three failing tests**, one per defect, and confirm each
  goes **red** before it is made green. Do not write a test and a fix in the same
  step.

  All three were added and confirmed RED for the intended reason before any
  production edit — the observed one-line reasons are in the Status section. Two
  further tests were added beyond this plan while implementing it
  (`test_a_claim_that_never_starts_gives_its_slot_back` for the slot leak Task 2
  exposed, and `test_shutdown_releases_a_run_the_loop_started` for Task 3 Step 4);
  both are also in that table.

  1. `test_a_long_run_does_not_delay_a_task_that_becomes_due` — the D1 claim.
     Do **not** assert wall-clock timings; they are flaky. Use the event trick the
     existing concurrency test already uses (`tests/test_scheduler.py:1262-1277`):
     task A's executor awaits an `asyncio.Event` that only task B's executor sets.
     If the tick is a batch barrier, B can never start while A waits and the
     scenario deadlocks — so wrap it in `asyncio.wait_for(..., timeout=5)` and
     assert it completes. Red today.
  2. `test_a_run_waiting_for_a_slot_keeps_its_lease` — the D4 claim.
     `cap=1`, `lease_seconds=3`, `poll_seconds=0.5`; A holds the slot for 6s and B
     queues behind it, both started through `run_task_now`. Assert B has **exactly
     one** run and that it is `succeeded`, and that no run is `interrupted`.
     `scripts/repro_scheduler_lease_wait.py` is the shape of it. Red today (2 runs, one
     interrupted).
  3. `test_a_run_that_queued_records_the_time_it_actually_finished` — the D5
     claim. Assert a run that waited for a slot has `finished_at` within a small
     tolerance of the real clock at completion, rather than early by the queue
     wait. Red today.

- [x] **Step 3: Keep the two existing contracts that must survive.**
  `tests/test_scheduler_service_executes_claimed_tasks_concurrently`
  (`tests/test_scheduler.py:1230`) and the ~30 `run_once(now=NOW + timedelta(...))`
  call sites encode "one `run_once` = one tick with a deterministic outcome". They
  must still pass unchanged — see Task 3's note on why the new mode is a
  parameter rather than a new default.

---

## Task 1 — The lease spans the claim (do this first)

Correctness before throughput: Tasks 2–4 increase the number of runs that queue,
which makes D4 *more* likely. Land this first.

- [x] **Step 1: Move lease renewal and the cancellation watcher up into
  `_execute_with_limit`.** They currently start inside `_execute_claimed`
  (`runtime.py:514-520`); move them to the top of `_execute_with_limit`
  (`runtime.py:201`) so they cover the wait for a slot, and pass them down:

  ```python
  async def _execute_with_limit(self, task, run) -> None:
      loop = asyncio.get_running_loop()
      monotonic_start = loop.time()

      def run_now() -> datetime:
          elapsed = max(0.0, loop.time() - monotonic_start)
          return run.started_at.astimezone(UTC) + timedelta(seconds=elapsed)

      lost_ownership = asyncio.Event()
      renewal = asyncio.create_task(self._renew_lease(task, run, lost_ownership, run_now))
      cancellation_watcher = asyncio.create_task(self._watch_cancel_request(task, run))
      try:
          ...  # existing acquire loop, unchanged
          await self._execute_claimed(
              task, run, lost_ownership=lost_ownership, run_now=run_now
          )
      finally:
          ...  # release slot, then cancel renewal + watcher as _execute_claimed does today
  ```

  `_execute_claimed` then takes `lost_ownership` and `run_now` as keyword
  parameters and stops constructing them. **Keep the existing cancellation
  semantics exactly**: the `while not acquired` loop must go on checking
  `cancel_requested` and completing the run as `cancelled`, and the
  `except asyncio.CancelledError` branch must go on distinguishing
  `self._cancel_requested` from a plain stop. This step changes *when* the lease
  is renewed, nothing else.

- [x] **Step 2: Note why this also fixes D5.** `monotonic_start` is now captured at
  claim time, so `run_now()` = claim time + real elapsed, which is the actual wall
  clock, and the ownership check at line 561 is evaluated at the present rather
  than in the past. Record this in the commit body: the timestamp fix is a
  *consequence* of moving the clock's origin, not a separate edit.

- [x] **Step 3: Make D4's test green** and confirm D5's test is green too, then
  re-run `scripts/repro_scheduler_lease_wait.py` and paste the new output into the plan. Expected:
  one run for B, `succeeded`, and no `interrupted`.

- [x] **Step 4: Re-run the full suite** and diff the FAILED set against Task 0
  Step 1.

---

## Task 2 — Claim only what can start

Today `run_once` asks for `limit=10` regardless of how many slots exist
(`runtime.py:117-124`), so with the default cap of 3 it can hold seven runs
claimed-but-not-started — each holding a lease, each a D4 candidate.

- [x] **Step 1: Track in-flight runs explicitly.** Add a set registered at claim
  time and cleared in `_execute_with_limit`'s `finally`, and derive the budget:

  ```python
  self._inflight: set[str] = set()          # claimed, not yet terminal

  def _free_slots(self) -> int:
      return max(0, self.max_concurrent_runs - len(self._inflight))
  ```

  Register in `_start_background_claim` **and** for the inline path, so both count.

  **Implemented against the existing `_active_tasks` instead of a new set** — see
  deviation 3 in the Status section. The lifecycle and the "register on both
  paths" requirement are identical; a second container would have stored the same
  fact twice, free to disagree with `health()["active_runs"]`.

- [x] **Step 2: Claim at most the free slots.** In `run_once`, pass
  `limit=self._free_slots()`. `claim_due_tasks` handles `limit=0` correctly
  (`LIMIT 0` yields nothing) and still runs
  `_recover_stale_runs_in_transaction`, which is the behaviour we want: no new
  claims when nothing can start, but recovery still happens.

  Keep the existing rule that a task cannot overlap itself — do not relax
  `active_run_id IS NULL` in the claim query.

- [x] **Step 3: Keep delivering signals when there is no room.** `deliver_signals`
  runs before the claim and must stay there. A cascade should keep *queueing* even
  when it cannot start; that is backpressure, not a stall, and it is what makes
  Task 4's wake-up productive.

- [x] **Step 4: Add `test_the_scheduler_does_not_claim_more_than_it_can_start`** —
  `cap=1`, three tasks due; after one non-blocking tick, exactly one run is
  `running` and the other two have no run at all.

- [x] **Step 5: Re-run the full suite** and diff the FAILED set.

---

## Task 3 — The tick stops waiting for its own work

- [x] **Step 1: Add a `background` parameter to `run_once`, defaulting to the
  current blocking behaviour.**

  ```python
  async def run_once(self, now=None, *, background: bool = False) -> int:
      ...
      if claimed:
          if background:
              for item in claimed:
                  self._start_background_claim(item)
          else:
              await asyncio.gather(
                  *[self._execute_with_limit(item.task, item.run) for item in claimed]
              )
      return len(claimed)
  ```

  **Why a parameter and not a new default.** Roughly thirty tests call `run_once`
  and assert terminal states on the next line. Changing the default would force
  thirty edits, and each edit is an opportunity to weaken an assertion — the
  failure mode this repo already has a rule about ("name the new parameter, never
  weaken the assertion", from the prompt-cache plan). A parameter leaves every
  existing assertion untouched and makes the production mode explicit.

- [x] **Step 2: `run_forever` passes `background=True`.** This is the actual fix
  for D1. `run_once`'s docstring should say plainly that the blocking default
  exists for deterministic tests, and that the loop uses the non-blocking form.

- [x] **Step 3: Make D1's test green.** It must exercise the **production** mode —
  i.e. drive `run_forever`, not `run_once(background=True)` directly — or the
  shipped path stays untested. That is the whole point of the change.

- [x] **Step 4: Confirm `shutdown()` still drains.** `_background_tasks` is what
  `shutdown` cancels (`runtime.py:186-191`), and `_start_background_claim` adds to
  it. Verify a run started by the loop is cancelled and released on shutdown, and
  that `health()["active_runs"]` is accurate with work in flight.

- [x] **Step 5: Re-run the full suite** and diff the FAILED set.

---

## Task 4 — Wake on completion, so a workflow hop costs a step, not a poll

Task 3 makes hops cost a step's *duration* instead of the batch's. This makes the
*next* hop start as soon as the step finishes rather than up to `poll_seconds`
later, which is D3.

- [x] **Step 1: Replace the fixed sleep with a wake-or-timeout wait.**

  ```python
  self._wake = asyncio.Event()

  async def _idle_wait(self) -> None:
      try:
          await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
      except asyncio.TimeoutError:
          pass
      finally:
          self._wake.clear()
  ```

  **The `finally` is load-bearing, and measured rather than assumed.** A run can
  finish while the tick is still running, so the wake is routinely set before
  `_idle_wait` is reached. Clearing only the timeout path — "drain it when it
  actually woke us" — reads as tidier and is a busy loop: the flag stays set and
  every later wait returns instantly. Measured, 50 waits: **0.002s** with the
  clear on the timeout path against **2.506s** with the unconditional clear, i.e.
  100% CPU with nothing failing anywhere in the suite to notice it. The event is a
  "skip the rest of the sleep" flag, not a counter.

  `test_a_wake_that_arrived_before_the_wait_is_drained_once` guards it, and was
  verified as a real guard: with the clear moved onto the timeout path it fails at
  the drain check. The plan's code above is correct — the note is here because
  nothing about it *looks* load-bearing.

  `run_forever` calls `await self._idle_wait()` where it now calls
  `asyncio.sleep(self.poll_seconds)`. Keep the heartbeat writes around the tick
  exactly as they are.

- [x] **Step 2: Set the event where a run reaches a terminal state** — the
  `finally` of `_execute_claimed`, which runs after `complete_run` on the success,
  cancellation and failure paths alike. Also set it after a claim is released.
  Do not set it from inside the executors.

  **Implemented one level up, in `_execute_with_limit`'s `finally`** — see
  deviation 1 in the Status section. `_execute_claimed` is not reached on every
  path that ends a claim: a run cancelled *while waiting for a slot* returns from
  the acquire loop without ever entering it, and "set it after a claim is
  released" is only satisfied by the outer method.

- [x] **Step 3: Confirm there is no re-entrancy.** `run_once` is awaited only by
  `run_forever`, sequentially, so a wake cannot overlap a tick. If any other
  caller is added, it needs a guard — note this in the docstring rather than
  adding a lock speculatively.

  **Confirmed and noted** — `grep -rn '\.run_once(' agent/` finds `run_forever`
  as the only production caller, and the docstring now says the method is
  deliberately unguarded, why that is safe today, and that a second caller must
  serialise because "deliver, claim, start" is not atomic against itself (two
  overlapping ticks can each read the same free-slot count and between them claim
  more runs than there are slots).

- [x] **Step 4: Add `test_a_workflow_hop_does_not_wait_for_the_poll_interval`.**
  Set `poll_seconds` very large (e.g. `3600`) so that any progress *must* come
  from the wake, not the timer; run a two-step chain and assert the second step
  runs within a few seconds. Emit the upstream signal through the same API the
  tests already use (`store.emit_signal(task_signal_name(...), ...)`, see
  `tests/test_scheduler_workflows.py:928`). Red today: with only
  `sleep(poll_seconds)` the second step waits an hour.

- [x] **Step 5: Re-run the full suite** and diff the FAILED set. The
  `30 * hop` call sites should still pass unchanged — they drive `run_once`
  manually and so never exercised the sleep.

---

## Task 5 — Gated: revisit the default cap (do not do this blind)

`max_concurrent_runs` defaults to 3. Once the tick is non-blocking, 3 is a low
ceiling — but raising it is not free, and this step must not be taken without
measurement.

- [x] **Step 1: Measure the cost of one concurrent run.** Each run builds its own
  components (`agent/cli.py:819-823`, `_build_components_async` per run when
  `isolated_runtime`), so concurrency multiplies memory, SQLite connections, and
  provider request rate. Record RSS and open-connection count at
  `max_concurrent_runs` of 1, 3, and N.

  **Measured** with `scripts/measure_scheduler_run_cost.py`, one concurrency level
  per child process (peak RSS is a high-water mark, so three levels in one process
  would report the first level's peak three times). Each figure is the whole
  process after building `n` sets of components concurrently, so subtract the
  ~102 MB floor to read the marginal cost:

  | concurrent runs | wall | each | peak RSS | MB/run |
  | --- | --- | --- | --- | --- |
  | 1 | 0.457s | 0.457s | 126.9 MB | 26.2 |
  | 3 | 1.137s | 0.379s | 154.1 MB | 17.4 |
  | 5 | 1.787s | 0.357s | 184.3 MB | 16.4 |
  | 8 | 2.759s | 0.345s | 225.9 MB | 15.4 |

  Marginal cost settles at **~15–16 MB and ~0.35s per concurrent run**, and it
  *falls* with concurrency because the imports are shared. Eight concurrent runs
  cost ~226 MB in total. So memory and CPU are **not** what should bound the cap.

  Two dimensions this measurement cannot cover, and both are operator-specific:

  - **Provider request rate.** Building a client needs a real API key; this was
    measured with a placeholder key, so no request was made and no rate limit was
    observed. This is the most likely real ceiling.
  - **MCP subprocesses.** A configured MCP server is connected *per run*, so `n`
    concurrent runs means `n` child processes on top of everything above. The
    example config's placeholder server made even `n=2` get killed in this
    sandbox, which is why the script clears `mcp_servers` before measuring.

- [x] **Step 2: Set the default from the measurement**, or leave it at 3 and
  document the trade-off. Do not raise it on the theory that "more is faster" —
  the per-run component build may dominate.

  **Left at 3.** The measurement rules out the reason this step was written to
  guard against — the per-run build does *not* dominate — but it also gives no
  evidence for a higher number, and the two dimensions that would justify one
  (provider rate limits, per-run MCP subprocesses) were not measurable here.
  Raising it is now a defensible operator decision rather than a blind one: a
  deployment with no MCP servers and generous provider limits can raise it, and
  the cost is ~16 MB per extra slot. Recorded rather than guessed.

- [ ] **Step 3 (optional, only if measurement shows one workflow starving
  others): add a per-workflow cap** so a single workflow's fan-out cannot consume
  the whole global budget. If added, acquire the global and per-workflow
  semaphores in a fixed order and document the order, so no interleaving can
  deadlock. Note that D2's lesson applies: a cap enforced in one path is not a
  cap, so the per-workflow limit must be enforced where claims are made.

  **Not done, and not needed on this evidence.** The step is conditional on a
  workflow's fan-out starving others, and nothing measured here shows that: the
  cap is now enforced where claims are made, and a fan-out can only take the slots
  that exist. Revisit if a real deployment shows a wide workflow crowding out
  unrelated tasks.

- [ ] **Step 4: Consider exposing the cap in the UI.** It is currently
  config-only, and a user watching a queue build up has no way to act on it. This
  is a product decision, not a correctness one.

  **Not done — deliberately.** A product decision, and this change is a
  correctness fix; bundling the two would make the throughput fix harder to
  review. The cap is now enforced, so making it adjustable is meaningful where it
  was not before.

---

## Task 6 — Verify end to end

- [x] **Step 1: Re-run all three reproductions** and record the before/after in
  this document. `scripts/repro_scheduler_tick_blocking.py` should show B starting near +0.50s;
  `scripts/repro_scheduler_lease_wait.py` should show one run for B.

  **Done** — the before/after is the table at the top. `B` starts at **+0.55s**
  (was +3.18s) and D4's task runs **once**, `succeeded` (was twice, one
  `interrupted`). The three scripts have since been deleted; the tests cover them.
- [x] **Step 2: Full suite, FAILED set identical to the Task 0 baseline**, with the
  passed delta equal to the number of tests added.

  **Done** — `16 failed / 2577 passed / 1 skipped` baseline against
  **`16 failed / 2585 passed / 1 skipped`** now: +8, exactly the eight tests
  added, with the FAILED set identical as a set every time. Re-confirmed on the
  frozen tree after the last edit. Note for anyone repeating this: run the suite
  on a tree nobody is editing — an earlier run started before a test was appended
  and so collected without it, reporting 2583 and making the delta look one short.
- [x] **Step 3: Confirm the workflow invariants still hold** — each step of a fork
  runs exactly once and the join once
  (`tests/test_scheduler_workflows.py:888`). Task 1 protects this; Task 3 must not
  break it.

  **Done** — `pytest -k fork` → 3 passed. All 275 tests across the six
  `test_scheduler*` files pass.
- [ ] **Step 4: Leave a real session running** with two unrelated tasks of very
  different durations and confirm from the run history that the short one is no
  longer delayed to the long one's boundary, and that no run is `interrupted`
  with an empty error (D4's signature).

  **Not done — cannot be done from here.** It needs a live scheduler with real
  provider credentials; this environment has none (`ANTHROPIC_API_KEY` is unset),
  so any "real session" would fail for reasons unrelated to this change. The
  equivalent claim is made by
  `test_a_long_run_does_not_delay_a_task_that_becomes_due`, which drives the
  production `run_forever` path with an event handshake rather than by wall-clock
  timing. Worth doing once on a real deployment, since it is the only check that
  exercises the executors and the provider together.

---

## Coordination note — read before editing

The working tree currently holds **1,260 uncommitted lines across six scheduler
files** (`agent/scheduler/{editing,models,runtime,store}.py`,
`tests/test_scheduler.py`, `tests/test_scheduler_workflows.py`) belonging to
another session. That work is about **workflow definition integrity** (a
definition cannot change mid-execution, deleting a workflow cancels its queued
steps, join rounds do not cross-complete, retry does not fail a chain early) — not
about throughput. It does not overlap this plan's *intent*, but it does overlap
its *files*: Tasks 1–4 edit `run_once`, `_execute_with_limit` and
`_execute_claimed`, all of which that work already touches.

Therefore:

- Land this **on top of** that work, or after it is committed — not in parallel.
- The line numbers above are from the working tree as read on 2026-09-22 and will
  shift. Re-read the functions before editing rather than trusting the numbers.
- **Credit where it is due:** that work already fixed D2 by moving the semaphore
  to an instance attribute and routing manual runs through it. It also introduced
  D4, because the lease now has to survive a queue wait it was never renewed
  across — which is why Task 1 comes first. Say so in the commit body; it is a
  real improvement that needs one follow-up, not a regression to revert.

**How separable is this change? Not by hunk — checked, and this is the answer to
"can it land now?".** `git diff` on `agent/scheduler/runtime.py` has 19 hunks;
several mix the two sessions' work inside one hunk, so a `git apply` of "my"
hunks alone would need hand-splitting:

- `@@ -61,10 +61,16 @@` — their `self._run_semaphore` and this plan's
  `self._wake` (plus its comment) are six lines apart in the same hunk.
- `@@ -613,15 +816,20 @@` — their `retry_at=` refactor and this plan's
  `_execute_claimed` `finally` comment are in the same hunk.
- Most other hunks are cleanly one side or the other (theirs: the
  `_automatic_retry_at` / `_watch_cancel_request` / `cancel_run` rework and a
  batch of line-length reformats; this plan's: `_free_slots`, the `background`
  parameter, `_execute_with_limit`, `_idle_wait`, the `_execute_claimed`
  signature).

So: land this **after** that work is committed, as the note above already says.
Splitting two hunks by hand to land it first would be more risk than the delay
costs — and the interleaving is a reason, not an accident: both sessions were
editing the same four functions.

**How it actually landed (2026-09-22): one commit containing both.** That work was
never committed by its author — it was sitting uncommitted from 2026-09-21 17:15,
with no plan doc, no memory entry, and no stash, so "wait for it to be committed"
would have waited forever. Separating the two is not merely fiddly, it is
**not faithfully possible**: this plan's changes are built *on* symbols that work
introduced (`_execute_with_limit`, `_watch_cancel_request` — neither exists in the
parent commit), and reconstructing "their version alone" would mean inventing the
interior of `_execute_claimed` as it was before this plan touched it, which is
recorded nowhere. Guessing at it was rejected in favour of one honest commit whose
body names both pieces and says why they are together.

---

## Out of scope

- Splitting `useConversations` in the frontend. Unrelated.
- Making workflow steps execute in-process instead of via signals. The signal
  chain is what makes a workflow durable and resumable across restarts; Task 4
  removes the latency without giving that up.
- Parallelising the steps of one workflow beyond the fan-out the graph already
  declares. A chain is a chain.
