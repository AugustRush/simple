# Declared Work Products, and the Other Half of the Contract

## Goal

Let a task say **what it has to leave behind**, and make that declaration mean
something in three places: the run is told where to write, the step below is
handed what actually exists, and "the file is not there" fails the run without
anybody writing a check for it.

This is the other half of a contract whose first half already existed.
`Acceptance` (`criteria` + `verify_command`) says what *done* means. Nothing
said what *work product* was owed. The gap was not theoretical: it is visible in
the live scheduler database (`~/.agent/tasks/scheduler.db`, read-only copy).

| Task | What it already said | What was missing |
| --- | --- | --- |
| `b2466bdd` A股模拟盘每日结算 | criterion: "`/tmp/amsim_report.md` 已写入且修改日期为当日"; `verify_command`: `find …/reports -name 'daily_*.md' -mmin -4500` | The criterion names one file; the command checks a **different** one. The run was never told about either. |
| the `xhs-flow` steps (`2eabc9a0`, `d082c7a2`, `e6462ed7`, …) | criterion: "`.xhs-flow/evidence/env.ok` 首行是 `ok env …`"; `verify_command`: `grep -q "^ok env " .xhs-flow/evidence/env.ok` | The only way to say "this step must produce this file" was to write a command that greps it. Eleven steps do this. |

The second row is the shape of the fix: every one of those `verify_command`s is
a hand-rolled existence check for a file the step was never asked to write.

## First Principles

1. **A contract has two halves, and a half that cannot be stated is not
   optional.** "Did you do the job" and "did you leave the thing I asked for"
   are different questions. Only the first had a field.
2. **A promise that the producer cannot see is not a promise.** The criterion
   was written down, stored, and used to judge the run afterwards — and never
   shown to the run. A target the worker is not given is not a target.
3. **A path is a name.** Giving a product a name *and* a path creates two
   identities for one artifact, and two identities drift. Name it by its path.
4. **Promised ≠ produced ≠ consistent.** Three different statements. A reader
   that collapses any two of them cannot answer the question they were asked.
5. **A promise with no file behind it is not an address.** Handing a downstream
   step `/w/report.md` when no such file exists is how it reads *last* round's
   file and believes it is this round's.
6. **Judgement is symmetric.** Declaring a bar and meeting it is a pass, not a
   silence. A verdict of "nothing was judged" is a claim, and it can be false.
7. **A statement about a moment must be recorded at that moment.** Files are
   overwritten and deleted; "was it there when the run ended" is the only
   version of the question that stays answerable.
8. **Refuse what could never be checked, where it is written.** The
   alternative is finding out at 3am from a run whose product never existed.

## What Exists

Measured against the tree, not assumed.

**The consumer side already exists, and this is why the design is small.**

| Piece | Where |
| --- | --- |
| One run's report shape, built by both the emission and the claim | `store.py:_run_report_payload` |
| What the steps above produced, read off the **trigger** (so it works for hand-wired signal chains with no `Workflow`) | `store.py:_upstream_reports_in_transaction` |
| The block that hands those reports to a run's prompt | `cli.py:_describe_upstream_results` |
| Acceptance: the criterion, judged once when the run ends | `verification/`, `runtime.py:_evaluate_acceptance`, `models.py:acceptance_for_run` |

So the binding between a producer and a consumer is **already resolved at run
time** (`task:<id>:succeeded`, or `depends_on` compiled into the same thing), and
there is exactly **one** payload builder. The missing piece is one field on that
payload: *which files the producing run left*.

**What did not exist.**

- No way to declare a file. A task whose product was a file said so in prose, or
  in a `verify_command` checking a file nobody had told the run to write.
- `criteria` was never injected into the run's prompt. It was used to judge
  afterwards and never shown. (Found while placing the new field next to it —
  see Part 3.)
- The run history could not say whether a product existed. `_run_detail` reported
  status, verdict and output path; a promised-and-missing file was invisible.

## Part 1 — The declaration

### One field, paths only

```python
produces: list[str]        # on NewScheduledTask, ScheduledTask, WorkflowStep, TaskRun
```

A product is named by its path inside the task's workspace, relative to
`workspace_root`. There is deliberately no separate `name`: two identities for
one artifact drift, which is the same reason a run's `error` is not also copied
into a `reason` field.

Three readers are served by that one list:

1. the run is told the resolved **absolute** paths (Part 3),
2. the step below is handed the addresses of the ones that **exist**
   (`products_for_handoff`),
3. "the declared file is there" becomes a judgement nobody writes a command for
   (Part 2).

### Rules, and where they are enforced

| Rule | Function | Why |
| --- | --- | --- |
| Relative, inside the workspace | `product_path_problem` | An absolute path, or one that leaves with `..`, makes the scheduler vouch for a file the declaring task's own permission profile would never have let it write — and tells the downstream run to go somewhere that profile cannot reach. |
| A file, not a directory | `product_path_problem` (trailing `/`) | An empty directory satisfies an existence check while containing nothing, so "the folder is there" must not pass for "the work is there". |
| At most `MAX_PRODUCTS` / `MAX_PRODUCT_PATH_CHARS` | `validate_products` | A declaration nobody can read is a declaration nobody checks, and the list travels into every downstream prompt. |
| Deduplicated, trimmed | `normalize_products` | Two spellings of one path are one promise; two entries would make "did it leave it" answerable twice with different answers. |

`validate_products` is called from `SchedulerStore._check_products`, which runs
in **both** `_create_task_in_transaction` and `_update_task_in_transaction`. Only
guarding creation would let a task be repaired into a state it could never have
been created in.

`find_matching_task`'s dedup signature includes the declaration: two tasks that
disagree about what they produce are two tasks. The same argument as for
`acceptance`.

### It rides on the run's snapshot

`execution_snapshot(task)` carries `"produces"`, and
`produces_for_run(task, run)` reads the snapshot first, falling back to the task
only when the key is absent (a run from before this existed).

This is what makes the judgement stable: editing a task while one of its runs is
in flight must not change what that already-running execution is held to. Same
rule, same reason, as `acceptance_for_run`.

## Part 2 — Measuring, judging, handing over

### Measured when the run ends

`runtime._measure_products(task, run)` resolves each declared path against the
run's workspace and records `{path, absolute, exists, bytes}`. Recorded, not
recomputed on demand: this is a statement about *this* run, and the file may be
overwritten or deleted afterwards.

`TaskRun.products` holds the report; `scheduled_task_runs.products_json` holds it
on disk. All four terminal paths record it — succeeded, cancelled, exception,
interrupted — so a reader never has to ask which terminal states left a record.

### The verdict is symmetric

```
report is empty            -> ""        (VERDICT_NONE: nothing was declared)
any declared path missing  -> FAILED
every declared path there  -> PASSED
```

The middle two keep the existing asymmetric combination rule
(`combine_verdicts`: any source can fail, all must pass). The first is the part
that is easy to get wrong: `VERDICT_NONE` is defined as *"no bar was declared,
so nothing was judged"*. A task that declared products **did** declare a bar, and
a file that is present satisfies it. Recording `""` there would be a false
statement — and a quiet one, because `_status_for` treats `""` and `PASSED`
identically, so the run's status does not change. Only the record lies.

Nothing is inferred across the halves: a task that declares no products does
**not** grow an implicit "check that the products exist", and a file being
present is not a claim about its contents.

A missing product is reported in the run's `error` as
`声明的产物没有产出：<paths>` — beside the other reasons, where somebody reading a
failure will look.

### Handing over: `products_for_handoff(declares, report)`

Three things are kept apart, and collapsing any two is the failure this exists
to prevent:

- what the task **promised** — read off the task row, edited with it;
- what the run **left** — the recorded report;
- whether those **agree**, which is the only thing that makes either of them
  mean anything downstream.

A promised path with no file behind it goes into `products_missing` as a *name*,
never into `products` as an address. An address is resolved by the producing run
and carried verbatim, so a step whose folder changed since still points at the
file that was actually written.

This one function feeds all three readers of the handoff:

| Reader | Call site |
| --- | --- |
| the signal payload | `store._run_report_payload` |
| the claim-time payload (run started by hand or by a retry) | `store._upstream_reports_in_transaction` → same builder |
| the prompt block for the step below | `cli._describe_upstream_results` |
| the `schedule_runs` tool | `builtin_tools._run_detail` |

The last one matters for more than tidiness: deriving "missing" from the report
alone would let *declared but never measured* read as *nothing missing*. The
declaration comes from the run's own snapshot, the measurement from the report,
and one function combines them.

## Part 3 — What the run is told

`_describe_run_contract(acceptance=…, produces=…, workspace=…)` writes both
halves in one block:

```text
This run has to leave these files behind, at exactly these paths. They are
resolved against the project folder you are working in:
- /Users/…/reports/weekly.md
A declared file that is not there when this run ends is recorded as a failure,
whatever else the run managed to do.

This run is judged when it ends, and work that does not meet this is recorded
as a failure even when it is delivered:
- 本周每个交易日都有数据
- A command run in the project folder has to exit 0: test -s data/clean.csv
```

Three placement decisions:

- **Above the upstream-results block.** Both halves of the contract are the same
  for every run of a given task, so they stay part of the one cacheable prefix;
  only "what the upstreams produced" changes per round, and data belongs after
  the instructions rather than in the middle of them.
- **Absolute paths.** One resolution, so the run and the downstream step cannot
  disagree about where the file was supposed to go.
- **Empty string when neither half was declared.** An ordinary task does not grow
  a section about a contract it does not have.

`_describe_upstream_results` gained two lines per upstream:

```text
  produced 「notes.md」: /w/notes.md (40 bytes)
  declared but NOT produced: summary.md -- if you find a file at one of those
  paths it is from an earlier round, not this one.
```

The second line must be present, not omitted. Omitting it is exactly how the step
below ends up reading last round's file and believing it is this round's.

## Migration

`SCHEMA_VERSION = 13`. Two columns:

```sql
ALTER TABLE scheduled_tasks     ADD COLUMN produces_json TEXT NOT NULL DEFAULT '[]'
ALTER TABLE scheduled_task_runs ADD COLUMN products_json TEXT NOT NULL DEFAULT '[]'
```

Guarded by `PRAGMA table_info` so it is idempotent. **Nothing is backfilled**, for
the reason the two previous columns have no backfill either: a task written
before this existed was written by somebody who was never asked what it produces,
so there is nothing to put there — and inventing a path from the task's name
would look exactly like a declaration, and be a promise the task never made.

Both columns default to `[]`, which reads as "nothing promised / nothing
recorded" — true of every pre-existing row. Read back through `normalize_products`
and the defensive `products_payload`, so a corrupt blob cannot make a run's
history unreadable.

## Scope

**In:** the declaration, its validation, the run snapshot, the measurement and
its record on all terminal paths, the symmetric verdict, the handoff payload, the
prompt block, the four tools' input/output, and tests.

**Out, deliberately:**

- **No UI.** Nothing on the task-detail page shows products yet. The tools answer
  the question, and the shape they answer in (`products` / `products_missing`) is
  the shape a page would render.
- **No CLI flag.** `simple schedule add` does not take `--produces`. The
  declaration is reachable from a conversation and from the tools; a CLI flag is
  a third spelling to keep in step.
- **No content checks.** "The file exists" is a statement about presence only.
  Emptiness, size, or structure belongs to `verify_command`, which is what it is
  for.
- **No content addressing, no products through the database.** A product is a
  file another program has to read (`state/equity.csv`, `figures/01.png`), and a
  person has to be able to open it. The filesystem is the right substrate, and
  the real tasks' paths are chosen by consumers *outside* the scheduler.
- **No convention directory.** Writing everything to
  `<workspace>/.task-output/<task_id>/` would need no schema change, and was
  rejected: `Desktop/赚钱/A股模拟盘/state/equity.csv` is where `daily_run.py`
  looks, and that cannot move.

## Open Decisions

1. **Should a zero-byte file count as produced?** Currently yes — `exists` is
   `is_file()`, and `bytes` is recorded but not judged. The counter-argument is
   that a truncated write leaves a zero-byte file that "passes", and the argument
   for the status quo is that size thresholds are guesses that belong in a
   `verify_command`. Left as-is; `bytes` is recorded so the decision can be
   revisited with data.
2. **Should `produces` be inherited by workflow steps from their upstreams?**
   No — a step declares what *it* writes. Inference would make the graph's
   meaning depend on a traversal order.
3. **Should the migration offer to backfill from existing `verify_command`s?**
   No. A `grep`/`find` command's target is inferable in principle, and inferring
   it would silently assert a promise the task's author never made. Instead the
   live tasks that would benefit are named here (see Goal) so a human can decide
   per task.

## Test Plan

`tests/test_scheduler_products.py` (30 cases) — the declaration's rules and
validation; the report on disk; the symmetric verdict; the three-state handoff;
the payload builder's both-halves shape; end-to-end workflow handoff including
the "promised and not written stops the chain" case; the prompt contract; the
migration from a rewound v12 database.

`tests/test_builtin_tools.py` (+9) — creation echoes the stored list (not the
argument); a path outside the workspace is refused with the path quoted; the
lists carry it; `schedule_runs` separates *declared nothing* from *produced
nothing*; a step declares its products and the chain says so per step.

Verification actually performed:

- `tests/test_scheduler_products.py` + `test_scheduler_acceptance.py`: 57 pass;
  every scheduler test file together: 310 pass.
- Full suite against a parent-commit copy (`git archive HEAD`, current tests
  copied in, run from inside the copy so `agent` resolves there): the failure set
  is a **superset** with an empty difference against the current tree's 18 — no
  regressions. The parent's 27 extra reds are the 18 new tests plus 9 from
  unrelated uncommitted work.
- Migration on a read-only `sqlite3.backup()` copy of the live database:
  v12 → v13, 52 tasks and 124 runs all readable, both new fields `[]`, nothing
  backfilled. The live file was never opened for writing.
