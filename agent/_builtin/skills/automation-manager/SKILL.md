---
name: Automation Manager
description: Create and shape automation tasks and chains (定时任务、提醒、按信号触发的后续工作). Use when the user asks for something to happen later, repeatedly, or after another job finishes — "每天早上提醒我", "每周出一份报告", "这个跑完再跑那个".
user-invocable: true
disable-model-invocation: false
---

# Automation Manager

Turning a sentence into an automation. The capability is one tool — `automation_create` —
and everything below is how to use it well. There is **no form to fill in**: the user
talks, you write the call. The Automation page only lists and edits what exists.

## First: should this be an automation at all?

- The user asked for something **later**, **repeatedly**, or **after another job**. That
  is the whole bar. A question about how the scheduler works is not a request to add one.
- If it would help but nobody asked, **propose it in your reply** and let them say yes.
  The request gate enforces this: `automation_create` must carry `intent`, quoting the
  user's own words from this turn (at least six characters, verbatim). A paraphrase, or a
  sentence about why it would be useful, is refused — and that refusal is correct.
- If the work can be done now, do it now. An automation is a promise about the future,
  and the cheapest future is the one that already happened.

## One step or several

`automation_create` has two shapes and the difference is `steps`:

| the user said | shape | what you write |
|---|---|---|
| "每天早上 9 点提醒我看日志" | one task | `automation_create(name, trigger_type="daily", time_of_day="09:00", action_type="message", message_text=…)` |
| "先抓数据，再写报告，最后发给我" | a chain | `automation_create(name, steps=[{key: "fetch", …}, {key: "report", depends_on: ["fetch"], …}])` |

**Never build a chain out of several single tasks connected by `signal_name`.** Those
tasks work, but the edges live only in the signal names: nothing records which steps a
failure must stop, and nothing can show the shape. A chain records the graph.

An entry step carries the trigger; a step with `depends_on` **must not** — its upstreams
are what start it, and two answers to "when does this run" is one answer too many (it is
refused).

## Writing a step or a task

- **`action_type`**: `message` for a literal future message, `agent_task` for work to do
  later, `system_job` for internal maintenance. The content field names it:
  `message_text`, `instruction`, `job_name`.
- **`workspace_root`**: the folder the run works in. Required for
  `permission_profile=workspace_write`, and worth passing whenever the work touches files.
- **`criteria`**: what has to be true for the run to count. Write it as sentences a person
  would accept. For a step in a chain this decides whether the next step runs, so an
  unstated criterion is a chain that continues on failure.
- **`verify_command`**: when success is mechanically checkable (a test, a build, a lint),
  this is the only evidence a run cannot talk its way out of. It must be a single low-risk
  command — no pipes, redirection, `rm`, or inline interpreters.
- **`produces`**: the files the work must leave behind, relative to the folder. Declaring
  them is what tells the run where to write and what the next step is handed. A declared
  file that is missing when the run ends fails the run.
- **`permission_profile`**: `inherit` (the session's), `read_only`, `workspace_write`. An
  unattended run gets no human to answer a permission question, so anything the profile
  does not allow is refused and recorded — choose the narrowest one that can do the job.
- **`trigger_type`**: `once` (`at`), `interval` (`every`, `unit`, and the anchor —
  `anchor_at`, or `at` for a step), `daily`/`weekdays` (`time_of_day`), `weekly`
  (`day_of_week`, `time_of_day`), `monthly` (`day_of_month`, `time_of_day`), `signal`
  (`signal_name`, for work that waits for a named event).

## After creating

Say what you made, in the user's terms: what will run, when it next runs, and where the
result goes. If the reply also carries a `runner` of `external`, say that a gateway or
`agent scheduler` has to be running for it to fire — a stored task is not a running clock.

To change one later, use `schedule_update` / `workflow_update` (they take `intent`
explaining what you are changing and why). Do not delete and recreate to make a small
edit: the task id is the run history and every downstream step subscribed to its signal.

## What not to do

- Do not create a chain for one step. If the request is one job, it is one task.
- Do not invent a schedule the user did not say. " Regularly" is not "every 30 minutes".
- Do not mark a task `workspace_write` when `read_only` would do.
- Do not promise a run that has already been refused: if the call comes back with an
  error, report it and let the user decide.
