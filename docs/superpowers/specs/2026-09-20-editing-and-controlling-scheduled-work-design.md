# Editing and Controlling Scheduled Work

## Goal

Let a task or a chain be **repaired where it is wrong**. Read a definition back,
change one field of it in place, and say what actually changed — with the task
id, its run history and every edge it holds intact.

The agent could already build scheduled work and could already delete it.
Nothing in between existed, and the gap had a cost that is visible in the live
scheduler database (`~/.agent/tasks/scheduler.db`, read-only copy).

| Measured | Count |
| --- | --- |
| tasks | 52 |
| … carrying a `workflow_id` | 31 |
| … of those, belonging to a workflow **that no longer exists** | **9** |
| workflows | 2 |
| runs | 165 |
| runs held by those 9 orphan steps | **0** |

The nine are one retired chain (eight steps, from `8422256c`, replaced by the
live `f96f760c`) and one probe. Every one of them is `enabled = 0`,
`next_run_at = NULL`, and points at a graph id nothing answers to. They are what
"fix it by rebuilding it" leaves behind: rows nobody can explain, with no history
left to say what they did.

Meanwhile `_schedule_update`'s own docstring names the sharper version of the
same fact: of the runs waiting for a person, ten were waiting because a
*definition* was wrong — a `verify_command` naming a file that step never writes
— and there was no way to write the fix.

## First Principles

1. **A wrong definition has to be fixable where it is wrong.** Delete-and-recreate
   is not a repair; it is a new task that happens to share a name, and the run
   history that answers "has this ever worked" goes with the old one
   (`delete_task` deletes the runs).
2. **A field the caller does not mention keeps the value the task has.** One
   rule, in one place. The workflow builder always had it; the task builder did
   not, and an omitted field was not *skipped* — it was written back as the
   dataclass default.
3. **A definition that can be read must be writable.** A read-back the writer
   refuses as having unknown fields is worse than one that omits them: it is a
   description pretending to be an edit.
4. **Identity is what the edges are made of.** A step's task id appears inside
   the signal names its downstream steps subscribe to. Keeping the key keeps the
   id; keeping the id keeps the edges.
5. **A repair is reached for from a complaint, not from a request.** The sentence
   that should call `schedule_update` is 「那个日报老是失败」 — it quotes no
   field and names no tool. Demanding the user's words would leave
   delete-and-recreate as the only repair, which is the thing being fixed.
6. **An edit is judged by comparison.** A field rewritten to its default looks
   exactly like a field nobody touched. So the answer names the fields that
   moved — and says so when none did.
7. **One question, one name.** "Is it switched on" is not an argument to "change
   this task": at 3am it should be one tool and one call.

## What Exists

Measured against the tree, not assumed.

**Three callers, three copies of the same rules.** The schedule interface
(`channels/web.py`), the workflow interface (same file), and the agent's own
tools each built a definition from a request body. Two had already drifted: only
the interface checked a name's length and whether a skill could be attached, so
the same definition was acceptable through one door and refused through another.

**The workflow builder already had the right rule, written down.**
`_workflow_step_from_body` says a graph editor that edits the graph must not
erase a step's model by not mentioning it. The task builder was the exception.

**The store rewrites every column from the spec it is handed.**
`_update_task_in_transaction` does not diff against the stored row. So a builder
that leaves a field out of the spec does not preserve it — it clears it. Editing
a task's name through the interface cleared the files it had declared it would
produce, and the run went on failing against a promise its own row no longer
contained.

## Part 1 — One builder, one rule

New module: `agent/scheduler/editing.py`. Every path that turns a request body
into a task definition goes through it.

```python
task_from_body(body, existing, *, context, keep_trigger=False) -> NewScheduledTask
```

`task_from_body` is **total**: every field a task can hold is either named by the
body or read off `existing`. That is what makes `{}` a legal body meaning "change
nothing".

Two things need care and are marked in the module rather than left to each
caller:

- **The stored trigger is kept verbatim when the body names none of it.** The
  store decides whether a schedule moved by comparing the *serialised* trigger,
  so a rebuild that means the same thing still reads as "the time changed" and
  gets answered by pushing the next occurrence into the future — swallowing one
  that was already due. `TRIGGER_BODY_FIELDS` is the one list of what counts as
  naming it, and `channels/web.py` imports it rather than keeping a second copy.
- **A step with upstreams does not own its trigger.** `keep_trigger=True` is how
  the caller says so, and `step_owns_no_trigger(store, task)` answers *which*
  steps those are — from the graph, because a fan-in trigger and a hand-picked
  signal both report `trigger_type == "signal"`.

`EditContext` carries the little that only a caller knows (the session's chosen
workspace, the skill catalogue, a model validator, whether Feishu is configured,
what a signal name means). Every field is optional with a permissive default, so
a test can build a definition without standing up a session.

## Part 2 — The read-back is the write-back

```python
task_from_body({}, task)                                == task
task_from_body(task_definition_payload(task), task)     == task
```

The first says an edit that names nothing changes nothing. The second says the
field list is complete — and it is why the inverse is written *next to* the
builder rather than in a reporter somewhere: a field the payload forgets is a
field an edit can silently reset, and the only way to notice is to have put the
pair down side by side.

Three things are deliberately **absent** from the payload, each because it
belongs to somebody else:

| Absent | Owned by |
| --- | --- |
| `workflow_id`, `step_key` | the graph, which decides them by materialising the workflow |
| `enabled` | `schedule_set_enabled` — and that path refuses a live workflow's step, so a second door here would be a way round the guard |
| `request_quote` | the past: evidence about who asked, not a setting |

`acceptance` is emitted **flat** (`criteria`, `verify_command`) rather than
nested. The builder reads the flat pair, so a payload carrying the nested object
would come back as a body mentioning neither — and the pair that decides whether
a run counts would be the one thing an edit could never move.

`schedule_runs` reports the same vocabulary plus the read-only keys
(`id`, `workflow_id`, `step_key`, `enabled`, `delivery_mode`, `delivery_target`,
`request_quote`), so a definition read there can be handed straight back with one
field changed.

## Part 3 — Five maintenance tools

| Tool | The question it answers |
| --- | --- |
| `schedule_update` | change part of this task's definition, in place |
| `schedule_set_enabled` | stop it / start it |
| `schedule_run` | run it now — as it is now, or that run's snapshot again |
| `schedule_cancel` | ask the run in flight to stop |
| `workflow_update` | add / remove / move / reword a step without the chain becoming a different chain |

**`schedule_set_enabled` is its own tool** because it is the switch somebody
reaches for while something is going wrong, and it should be one name rather than
an argument to a bigger one. It surfaces the store's own refusal unmoved: a step
of a workflow that still exists cannot be switched on its own, because its switch
is rewritten from the graph every time the graph is saved.

**`schedule_run` takes two questions with one verb**, and the difference is the
answer. With no `run_id` the task runs as it is *now* — which is how a fix is
confirmed (`schedule_update`, then this). With a `run_id` it re-runs the
**snapshot** that run started with, so the outcome is about the work rather than
about the definition: a flake gets another try unchanged, and a fix cannot be
mistaken for one. A task already running is refused rather than queued.

**`schedule_cancel` asks rather than kills.** The run stops at its next
checkpoint, so the reply says `cancel_requested` and not "stopped": reporting a
completed cancellation would be describing something this side of the process
cannot see.

**`workflow_update` reconciles by `key`**, which is the whole contract:

| Case | What happens |
| --- | --- |
| key already in the graph | same step; keeps every field the call does not mention |
| key not in the graph | new step, new task |
| stored key missing from the list | **removed** — its task is disabled, not deleted, so the record of what it did survives |

`steps` may be omitted entirely, which is how a rename or a pause is said without
restating the graph. The reply includes each step's `task_id`, because the ids
surviving an edit is the point.

### The gate: `requires_intent`, not `requires_request`

The three creators (`schedule_create`, `workflow_create`, `emit_signal`) demand a
**verbatim quote of the user's words**. These five demand that the caller say
what it is doing and why, but not in the user's words — the capability on the
tool is `requires_intent`. A complaint names no field, so requiring a quote would
make delete-and-recreate the only repair.

## Part 4 — A step is edited as a task, stored as a step

A step *is* an ordinary task — its own row, its own run history, its own switch —
but what it is **stored as** is a step of a graph, and the next save of that
workflow rebuilds the task from the graph. So an edit through the task path would
survive exactly until somebody moved an edge.

`mirror_step_edit(store, task)` copies the edited task back into its step, and
copies **every** field of `WorkflowStep`. A field left out is not merely
uncopied: the step is rebuilt from the dataclass default, so a rename would reset
the retry policy and an edit to a step's schedule would drop the files it promised
to produce. Two fields are copied from the *step*, not the task — `depends_on`
(a task cannot express an edge, so it must not be able to break or invent one) and
`trigger` (for a step with upstreams, the upstreams *are* its trigger).

The graph is not re-materialised: the task in hand was written a moment ago and
is the newer of the two, so rebuilding it from the copy would be a round trip
that can only lose something.

## What This Found

Seven real defects, each one fixable only once the rule lived in one place:

1. **`task_definition_payload` was not the inverse.** It nested `acceptance` and
   omitted keys the schema did not accept, so a read-back was refused with
   "unknown field(s)". Fixed; and pinned by a test that sends every key it emits
   straight back in.
2. **Acceptance was half-wiped.** Naming only `criteria` reset `verify_command`
   (and the reverse). Each half is now kept separately.
3. **A one-off that had already fired could not be edited.** The future-time
   check fired on edits. It now fires only when the caller actually supplies
   `at`.
4. **`unhashable type: 'list'`.** A JSON-Schema `type` written as a union
   (`["string","null"]`) crashed the tool-argument validator. The validator now
   handles a list of types.
5. **`workflow_update` did not report `task_id` per step** — the one thing the
   tool exists to preserve.
6. **The duplicate sweep and the create path disagreed about what "the same
   task" is.** `disable_duplicate_enabled_tasks` compared a narrower signature
   than `find_matching_task` (missing `acceptance_json` / `produces_json`). One
   column list now, `_TASK_IDENTITY_COLUMNS`, used by both.
7. **A carried `model_override` was re-validated**, so a task whose model had
   since been retired could never be edited at all. A *newly named* model is
   validated where somebody can see it; a carried one is not.

## Scope

**In:** the shared builder and its trigger/membership rules, `EditContext`, the
inverse payload, the five tools and their schemas, the intent gate, the
step-mirroring, the mentions in `context_assembler`'s held-back sets, the README,
and tests.

**Out, deliberately:**

- **No new store write path.** `update_task`, `set_enabled`, `claim_task_now`,
  `claim_retry`, `request_cancel` and `update_workflow` already existed; the tools
  call them. Nothing about editing needed a new column or a migration.
- **No `enabled` argument on `schedule_update`.** See Part 3 — the switch has one
  name, and `schedule_set_enabled` carries the store's guard with it.
- **No UI change.** The 自动化 page keeps its own editor; this is the agent's
  door to the same rules.
- **No quote rewriting.** `request_quote` stays evidence about the past.

## Open Decisions

1. **Should `schedule_update` be able to change a task's `kind`?** It can today
   (`action_type` is writable), and the content key travels with it. The
   counter-argument is that a prompt and a message are different jobs and a
   conversion is really a new task. Left writable, because the alternative forces
   the delete-and-recreate this design exists to end.
2. **Should a removed step be deleted when it has no runs?** It is disabled
   either way today. Deleting the row would tidy the nine orphans in the live
   database — and would also delete the evidence that they were ever there.
3. **Should the nine orphan steps be cleaned up?** Not by this change. They are
   readable, disabled, and `schedule_delete` can now remove one (a step whose
   workflow is gone is an ordinary task again). Named here so the decision is
   visible rather than swept.

## Test Plan

`tests/test_scheduler_editing.py` (**27 cases**, all passing) — the round trip in
both directions and that every emitted key is writable; an unnamed field keeps
its value; the edit names the fields it moved; membership and the quote cannot be
written; a step's trigger belongs to the graph and a stored fan-in survives an
edit that does not touch it; a fired one-off can be edited while a past one
cannot be created; the switch has one tool and refuses a live step; run-now,
re-run-the-snapshot, already-running, cancel-asks, cancel-with-nothing; a workflow
edit keeps its steps' ids and disables a removed step; a new model is refused
where somebody can see it while a carried one is not; all five declare
`requires_intent`.

Also updated: `test_scheduler_products.py` (+2, the sweep and the create path
agree; the two readers share one column list), `test_context_assembler.py` (a
question ships no mutator, a request ships them, `schedule_runs` is never
withheld), `test_builtin_tools.py` and `test_channel_layer.py` and
`test_scheduler_workflows.py` (hardcoded 2026-04-20 one-off times replaced with a
time computed from now, which is what the once-future rule always meant).

Verification actually performed:

- Focused: `test_scheduler_editing`, `test_scheduler_products`,
  `test_scheduler_workflows`, `test_context_assembler`, `test_builtin_tools`,
  `test_channel_layer` — **427 pass, 1 fail**, and that one is
  `test_shell_allows_output_dir_writes` (exit 71): the known nested-sandbox
  environmental failure, not a regression.
- Full suite against a pristine parent copy (`git archive HEAD | tar -x`, with
  `PYTHONPATH` pointing into the copy so `agent` resolves there): the FAILED sets
  are byte-identical to the 17 sandbox failures that are the local baseline —
  zero regressions.
- Live database re-read on a copy (never opened for writing): the counts in the
  Goal table, the nine orphan steps' `enabled`/`next_run_at`/run history, and
  `user_version = 13`.
