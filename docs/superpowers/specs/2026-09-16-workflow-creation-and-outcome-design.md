# Workflow Creation and Run Outcome Design

## Goal

Two things, which turn out to be one thing:

1. Let the agent build a workflow from inside a conversation — decompose a
   complex task into single tasks and chain them.
2. Say what "this task succeeded" means, so that a task can fail for a reason
   other than crashing.

They are one thing because a step is only a step if it has a boundary, and a
boundary *is* an acceptance check. "Split it into single tasks" without a
criterion per task does not produce a pipeline; it produces fragments that are
each individually plausible and collectively wrong. So the second half is not a
follow-up to the first — it is what makes the first half meaningful.

## First Principles

1. **Three questions, three fields.** "Did it run?", "did the work achieve what
   it was for?", and "did the result arrive?" are independent. A field that
   answers two of them answers neither.
2. **Only one value means success.** Naming the single succeeding value, rather
   than listing the failing ones, makes a status invented later default to
   "did not succeed" — the safe direction for anything downstream.
3. **"We could not tell" is not "it failed".** A check that was refused,
   timed out, or failed to start is evidence about the check, not about the
   work. Collapsing the two makes the system assert something it never
   observed.
4. **A graph that can be wrong is wrong before it runs.** Cycles, dangling
   upstreams, and a step with two contradictory triggers are all caught at
   creation, where somebody can still read the error. An acceptance check that
   can never be evaluated belongs in the same category.
5. **An unattended run needs a target it can be judged against.** A prompt with
   no criterion is judged by whether the model replied, which is always yes.
6. **Autonomous verification is less privileged than an interactive shell.**
   Inherited unchanged from the Ralph design: low-risk commands only, never
   through a shell interpreter.

## What Exists

Measured against the tree, not assumed.

**Workflows are fully built. They are just not reachable from a conversation.**

| Piece | Where |
| --- | --- |
| `Workflow` / `WorkflowStep` with stored `depends_on` edges | `agent/scheduler/models.py:877-1037` |
| Topological order; rejects cycles, dangling/self upstreams, bad kind, missing payload, entry-without-trigger, non-entry-with-trigger | `models.py:1040-1140` |
| A dependent step's trigger is `signal_all` of `task:<upstream>:succeeded` | `models.py:1143-1168` |
| Transitive blocked set, for skipping after a failure | `models.py:1171-1191` |
| Store the graph and build its tasks in one transaction | `store.py:2454-2488` |
| Materialize in dependency order; inherit the entry step's folder | `store.py:2621-2676` |
| `GET/POST/PUT/DELETE /api/workflows` | `web.py:1653-1699`, routes at `3479-3491` |

There is **no agent tool** for any of it. `schedule_create` creates one loose
task; the only way to build a graph is the HTTP API. So the agent can wire two
tasks together by hand with `trigger_type=signal` plus `emit_signal`, and the
result is not a `Workflow`: it does not appear in the workflow tab, and
`read_step_output` cannot resolve it, because that tool needs
`scheduler_workflow_id` in its context (`builtin_tools.py:3323`).

**Success is decided by delivery, not by the work.**

`agent/scheduler/runtime.py:389-394`:

```python
successful_delivery = delivery_status in {"stored", "delivered"}
if delivery_status == "skipped" and not result.text_output.strip():
    successful_delivery = True
status = "succeeded" if successful_delivery else "failed"
```

`status` therefore answers "did delivery work". Consequences:

- An `agent_prompt` task that produced a useless answer is `succeeded`, because
  the answer was stored. The one thing the run was for is the one thing never
  asked about.
- A task whose work was perfect but whose delivery target was unreachable is
  `failed` — and that single word also triggers a retry, skips every downstream
  step, and raises an attention flag, all for a cause unrelated to the work.
- The original design listed these as separate fields
  (`docs/superpowers/specs/2026-04-19-scheduler-design.md:121,125` — `status`
  *and* `delivery_status`). The record still stores both; the runtime fold is
  what merges them into one word.

**The agent cannot say "I could not do this."** `_agent_executor`
(`agent/cli.py:831-846`) fails a run only when `result.error` is set, which is
the harness's error channel — an API failure, not a judgement about the work.
An agent that discovers halfway through that the data source is gone has no way
to end the run honestly; it writes prose, and the run is `succeeded`.

**The vocabulary already exists — in Ralph, unused by the scheduler.**

| Concept | Where |
| --- | --- |
| `goal`, `completion_criteria: list[str]`, `verify_command`, `completion_promise` | `agent/ralph/models.py:204-219` |
| `VerificationStatus`: `passed / failed / timeout / cancelled / rejected / setup_error` | `models.py:33-39` |
| Invariant: only `passed` is success; a `failed` verdict requires a nonzero exit code | `models.py:89-96` |
| `RalphVerifier`: shlex parse, safety gate, curated env, process-group teardown, timeout | `agent/ralph/verify.py:29-228` |

Ralph answers exactly the question being asked here, and the scheduler does not
use any of it. The design below adopts that vocabulary rather than inventing a
second one.

## Part 1 — Creating a workflow from a conversation

### The tool

One tool, `workflow_create`, mirroring the shape `POST /api/workflows` already
accepts so that there is one definition of a valid graph, not two.

```
workflow_create(
  name: str,                        # ≤60 chars
  description: str = "",
  enabled: bool = True,
  steps: [
    {
      key: str,                     # ≤40 chars, no whitespace, unique
      name: str,
      kind: "agent_prompt" | "message" | "system_job",
      prompt: str,                  # agent_prompt
      message_text: str,            # message
      job_name: str,                # system_job
      depends_on: [str],            # upstream keys; empty ⇒ entry step
      trigger_type: str,            # entry steps only
      at | every+unit | time_of_day | day_of_week | day_of_month | signal_name,
      acceptance: {                 # Part 2
        criteria: [str],
        verify_command: str,
      },
      permission_profile: str = "inherit",
      workspace_root: str = "",
      timeout_seconds: int = 1800,
      model_override: str | None = None,
      selected_skills: [str] = [],
    }
  ]
)
```

The tool translates its arguments into `WorkflowStep` objects and hands them to
`validate_workflow_graph`. It must **not** re-implement the checks: the existing
validator already produces messages that name the offending step
(`步骤 step2 依赖了不存在的上游：step9`), and a second copy would be free to
disagree with the one that runs.

The asymmetry between entry and dependent steps is carried into the schema
rather than hidden: `trigger_type` is meaningful only when `depends_on` is
empty, and the validator refuses both a dependent step that carries a trigger
and an entry step that does not. The tool description must say so, because
"run at 9am" and "run after A" are two different answers to *when*.

### What it returns

Enough to confirm what was built without a second call:

```
{
  "workflow": {"id": ..., "name": ..., "enabled": ...},
  "steps": [
    {"key": "fetch",  "task_id": ..., "kind": "agent_prompt",
     "depends_on": [], "next_run_at": "...", "acceptance": "2 条验收条件"},
    {"key": "render", "task_id": ..., "depends_on": ["fetch"],
     "next_run_at": null, "acceptance": "verify: python -m pytest -q"}
  ],
  "order": ["fetch", "render"]
}
```

`next_run_at` is `null` for every dependent step, and that is correct rather
than missing: a step woken by a signal has no clock to name
(`models.py:230-254`). Reporting it as `null` says "waits for its upstream";
omitting the field would say nothing.

### Gating

`workflow_create` / `workflow_list` / `workflow_delete` join the gated tool
group in `agent/core/context_assembler.py:19`, alongside the existing
`_SCHEDULE_TOOLS`. The unlock keywords must include the ones **the product
itself uses** — `自动化`, `工作流`, `流程`, `workflow` — because the current
schedule keyword list (`schedule / remind / recurring / cron / 定时 / 提醒 /
周期`) does not contain `自动化`, which is the word the automation page, the tab,
and the user all use. That gap is a defect in its own right: measured last
session, 「帮我创建一个自动化」 and 「以后每天帮我汇总一下」 both leave the
scheduler tools hidden.

### What the agent is told about decomposition

Capability without a method produces one giant step. The system prompt
(`agent/config.py:794-804`, injected only when the tools are selected) gains a
short contract:

- one step = one outcome that can be checked on its own;
- a step that cannot say how it would know it succeeded is not a step yet;
- data moves downstream as an *address*, not as text — the block at
  `agent/cli.py:555-575` hands over the output path and the run reads it with
  `read_step_output`, because a step's output can be kilobytes and the context
  budget is finite.

## Part 2 — What "succeeded" means

### Three questions, three fields

| Question | Field | Values |
| --- | --- | --- |
| Did it run? | `status` | `queued` `running` `succeeded` `failed` `cancelled` `interrupted` `skipped` |
| Did the work achieve what it was for? | `verdict` **(new)** | `""` (nothing declared) `passed` `failed` `unknown` |
| Did the result arrive? | `delivery_status` | already stored; stops deciding `status` |

`verdict` is empty for a task that declares nothing. That is the honest default
and it keeps every existing row meaning what it meant: a task with no criterion
has not been judged, which is different from having been judged and passed.

### The criterion

Adopted verbatim from Ralph rather than re-invented:

- `criteria: list[str]` — the acceptance conditions **in words**. Shown to the
  run in its system prompt, so an unattended agent knows what it is being
  judged against instead of inferring a target from a prompt.
- `verify_command: str` — the machine check. Exit code 0 means the work met the
  criterion. Run by `RalphVerifier`, which is lifted out of `agent/ralph/` into
  a shared module and used unchanged: low-risk commands only, never through a
  shell interpreter, curated environment allowlist, hard timeout, process-group
  teardown.

The two halves are complementary and neither is optional in practice: the words
are what the agent aims at, the command is what the record can be trusted to
say.

### Verdict states — the part that must not collapse

`VerificationStatus` has six values. Only one of them is a verdict about the
work:

| `VerificationStatus` | `verdict` | What the run says |
| --- | --- | --- |
| `passed` | `passed` | 达标 |
| `failed` | `failed` | 未达标 — carries the verifier's `stderr_tail` |
| `timeout` | `unknown` | 无法判定：验收命令超时 |
| `rejected` | `unknown` | 无法判定：验收命令未通过安全检查 |
| `setup_error` | `unknown` | 无法判定：验收命令无法启动 |
| `cancelled` | `unknown` | 无法判定：运行已取消 |

Folding `rejected` into `failed` would make the system report "the work is
wrong" when what happened is "we never looked". That is the same failure mode
the signal-state docstring refuses to commit (`models.py:503-519`: a signal
nobody subscribes to is `unmatched`, not silently indistinguishable from one
that worked), and the same one `run_needs_attention` exists to avoid
(`models.py:763-780`).

### Where the check is validated

**At creation, not at run time.** Principle 4: everything a graph can be wrong
about is wrong before it runs. So `verify_command` is parsed and put through the
safety gate when the task or workflow is written, and a command that would be
refused is refused *there* — with a sentence, while somebody is looking. This is
what keeps `rejected` rare at run time (only environment differences can still
produce it) and it means a task can never be created around a check that will
never run.

### Who may fail a run

Two sources, and the safe direction is that **either can fail it, both must
pass for it to pass**:

- the **command check**, as above;
- the **run's own report** — a new tool, `report_outcome(status, reason)`, so an
  agent that discovers the data source is gone can end the run honestly instead
  of writing plausible prose. Today it has no way to do this.

| command | self-report | `verdict` |
| --- | --- | --- |
| passed | (none) | `passed` |
| failed | (any) | `failed` |
| passed | failed | `failed` |
| unknown | not failed | `unknown` |
| (none declared) | (none) | `""` |

A self-report is weaker evidence than a command, so it can only ever *lower* the
verdict. It cannot upgrade `unknown` to `passed` — an agent's assurance is not a
substitute for the check that could not run.

### How the verdict reaches the rest of the system

- **Run status.** `verdict == failed` ⇒ the run did not achieve what it was for,
  so `status = failed`. `verdict == unknown` ⇒ the run has not been shown to
  succeed, but calling it `failed` asserts something unobserved; it gets its own
  terminal status, `unverified`.
- **Downstream.** No change needed. Dependent steps already subscribe to
  `task:<upstream>:succeeded` (`models.py:1163-1168`), so a step that did not
  succeed does not wake its children, and `workflow_downstream_steps` already
  computes the transitive set to mark `skipped`.
- **Attention.** `ATTENTION_STATUSES` gains `unverified`, and
  `run_needs_attention` gains the verdict as a third input beside
  `missed_count` and the status. "We could not tell whether this worked" is
  precisely something a person has to look at. The SQL half
  (`SchedulerStore._attention_clause`) must change in step; the existing test
  that walks a run through both representations is what keeps them honest.
- **Retry.** Unchanged in mechanism: retry fires when the run did not succeed.
  `unverified` retries, because a check that timed out may well pass next time.
- **Delivery.** Stops deciding `status`. A delivery failure keeps its own field
  and becomes its own attention reason. The retry-on-failure behaviour it
  currently triggers is preserved by making it a reason to retry, not by
  laundering it through `status`.

### Migration

New columns on `scheduled_tasks` / `scheduled_task_runs` (or a JSON column
beside `retry_policy_json`, which is the existing precedent for a small policy
blob) plus `verdict` on the run. Every existing row reads as
`verdict = ""` / no criterion, which is exactly what those rows mean: nothing
was declared, so nothing was judged. No backfill, no compatibility shim — a
task created before this change behaves as it does today.

## Scope

**In scope**

- `workflow_create` / `workflow_list` / `workflow_delete` tools, reusing
  `validate_workflow_graph` and `SchedulerStore.create_workflow`.
- Gating group + unlock keywords, including `自动化` / `工作流` / `流程`.
- The prompt contract for decomposition, injected only when the tools are
  selected.
- `acceptance` on a task and on a step: `criteria` + `verify_command`,
  validated at creation.
- `verdict` on the run, the `unverified` terminal status, `report_outcome`, and
  the attention/retry wiring.
- Frontend: verdict and the acceptance criterion on the run detail; `unverified`
  in the status vocabulary and the running/attention filters.

**Out of scope**

- A model call inside the tool to decompose a goal automatically. Decomposition
  is a language task and belongs to the agent that is already holding the
  conversation; a tool that secretly calls a model would be a second, invisible
  turn.
- Conditional edges (run B only if A produced X). The graph is a DAG of
  *success* edges; expressing a condition means a step whose job is the decision.
- Loop-until-verified inside a scheduled run. That is what `/ralph` is for, and
  a scheduled run is one shot by design.
- Auto-repair: a failed verdict does not rewrite the step.

## Open Decisions

1. **`unverified` as a new terminal status, or reuse `interrupted`?**
   Recommendation: new. `interrupted` means the run was cut short (lost lease,
   restart, timeout) and is already in `ATTENTION_STATUSES` for that reason;
   overloading it would make "we stopped it" and "we could not judge it"
   indistinguishable in the history — the same argument that gave `skipped` its
   own status (`models.py:866-874`).

2. **Does `verdict` apply to `message` and `system_job` tasks too?**
   Recommendation: yes, uniformly, but it will rarely be set. A `message` task
   has nothing to verify; a `system_job` might. Making it a property of the run
   rather than of `agent_prompt` keeps one rule instead of a special case.

3. **Does a failed verdict retry by default?**
   Recommendation: no, and this differs from today. A crashed run will very
   likely crash again *for the same reason*; a run whose work did not meet its
   criterion may well produce the same wrong thing again, and retrying it costs
   a full agent turn each time. Retry on `failed` stays available but must be
   opted into per task via `retry_policy`. `unverified` keeps retrying.

4. **Where does the shared verifier live?** `agent/ralph/verify.py` is
   well-tested and its safety argument is written down. Recommendation: move
   `RalphVerifier` + `VerificationResult`/`VerificationStatus` to
   `agent/verification/`, re-export from `agent.ralph` so nothing breaks, and
   have both callers use it. One implementation of "is this command safe to run
   unattended" is the whole point.

## Test Plan

Each of these fails against the current tree and passes after:

- a workflow created through the tool has tasks behind it, and a dependent
  step's trigger is a join of its upstreams' success signals;
- a cycle is refused by the tool with the ring named, not by the store;
- an entry step with a trigger and a dependent step with one are both refused,
  each naming the step;
- `verify_command` that is not low-risk is refused **at creation**;
- a run whose check exits nonzero is `failed` with the stderr tail in `error`,
  and its downstream steps are `skipped`;
- a run whose check is `rejected` / `timeout` / `setup_error` is `unverified`,
  not `failed`, and is not silently `succeeded`;
- a run whose self-report is failure is `failed` even when the check passed;
- a self-report cannot upgrade `unknown` to `passed`;
- a task with no criterion still behaves exactly as it does today;
- `run_needs_attention` and `_attention_clause` agree on a run with an
  `unverified` verdict — the existing two-representation test extended.

Then the full suite, comparing the FAILED set against the baseline rather than
against zero.
