"""Scheduled tasks and the workflows they run: creating, editing, listing, running and cancelling them. Split out of BuiltinTools._register (see the remaining-big-files survey, section 2c)."""

from __future__ import annotations

from agent.scheduler.models import LOCAL_TIMEZONE

_WORKFLOW_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": (
                "Short stable id for this step, used by other steps' "
                "depends_on, e.g. collect or summarise. On an edit this is the "
                "identity: a key already in the chain is that same step and "
                "keeps everything the call does not mention, and a key that is "
                "not there yet is a new step. Changing a key makes a new step "
                "and abandons the old one's run history."
            ),
        },
        "name": {"type": "string", "description": "Short step name"},
        "action_type": {
            "type": "string",
            "description": (
                "one of: message, agent_task, system_job. "
                "Inferred from the content field when left "
                "out -- `instruction` makes it an agent "
                "task, `message_text` a message, "
                "`job_name` a system job -- so filling in "
                "only one of those is enough."
            ),
        },
        "instruction": {
            "type": "string",
            "description": "Agent instruction, for action_type=agent_task",
        },
        "message_text": {
            "type": "string",
            "description": "Literal message, for action_type=message",
        },
        "job_name": {
            "type": "string",
            "description": "Internal job name, for action_type=system_job",
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Keys of the steps that must succeed before this "
                "one runs. Empty or absent makes this an entry step."
            ),
        },
        "trigger_type": {
            "type": "string",
            "description": (
                "Entry steps only: one of once, interval, daily, "
                "weekly, weekdays, monthly, signal. A step with "
                "upstreams must leave this out."
            ),
        },
        "signal_name": {"type": "string"},
        "at": {"type": "string", "description": "ISO datetime for once"},
        "every": {"type": "integer"},
        "unit": {"type": "string", "description": "minutes|hours|days|weeks"},
        "time_of_day": {"type": "string", "description": "HH:MM"},
        "day_of_week": {"type": "string", "description": "mon|tue|...|sun"},
        "day_of_month": {"type": "integer", "description": "1-31"},
        "timezone_name": {
            "type": "string",
            "default": LOCAL_TIMEZONE,
            "description": (
                "IANA timezone name. Omit to use this machine's timezone."
            ),
        },
        "workspace_root": {
            "type": "string",
            "description": "Overrides the workflow's folder for this step",
        },
        "permission_profile": {
            "type": "string",
            "description": "inherit|read_only|workspace_write",
        },
        "delivery_mode": {
            "type": "string",
            "description": "optional override: standalone or channel",
        },
        "criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What has to be true for this step to count as a "
                "success. Decides whether the steps below it run."
            ),
        },
        "verify_command": {
            "type": "string",
            "description": (
                "Command whose exit code decides this step's "
                "success; run in the step's folder. Must be a "
                "single low-risk command -- no pipes, redirection, "
                "`rm`, or inline interpreters."
            ),
        },
        "produces": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The files this step has to leave behind, as "
                "paths relative to its project folder. Declare "
                "them on the step that writes them: the run is "
                "then told where to write instead of guessing, "
                "and the steps below are handed the resolved "
                "absolute paths of the ones that exist. A "
                "declared file that is missing when the step "
                "ends fails that step, so the steps below it do "
                "not run on nothing."
            ),
        },
    },
    # `action_type` is deliberately not required: the content field names the
    # action, and requiring the label as well refused the obvious spelling of a
    # step with a complaint about a field its author never used -- see
    # ``action_payload``.
    "required": ["key", "name"],
}

def register_schedule_tools(
    r,
    *,
    schedule_tools,
) -> None:
        r.register(
            "schedule_create",
            (
                "Create a persistent scheduled task. Use when the user asks for a reminder, "
                "a delayed follow-up, or a recurring future message. "
                "The user has to ask: a question about how something works, or about the "
                "process behind it, is answered in the reply and is not a request to add a "
                "task -- if one would help, propose it and let the user say yes. "
                "Choose `action_type=message` for a literal future message, "
                "`action_type=agent_task` for future agent work, or "
                "`action_type=system_job` for internal maintenance. "
                "For once: provide `at`. For interval: provide `every`, `unit`, and `at` (anchor). "
                "For daily or weekdays: provide `time_of_day`. For weekly: provide `day_of_week` and `time_of_day`. "
                "For monthly: provide `day_of_month` and `time_of_day`. "
                "For signal: provide `signal_name` -- the task runs when that signal is emitted, "
                "which is how a step can be made to follow another task instead of a clock. "
                "The run is unattended: set `permission_profile` to say what it may do "
                "(inherit/read_only/workspace_write), and pass `workspace_root` when the "
                "task should work in a directory other than the current session's. "
                "The task outlives this conversation, so `intent` has to carry the "
                "user's own words: it is checked against the request that commissioned "
                "this turn, and a call whose intent cannot be found in it is refused."
            ),
            {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": (
                            "Fill this in, or the call is refused. Quote the "
                            "user, verbatim -- at least six characters copied "
                            "from what they actually typed this turn, e.g. "
                            "their sentence 「每天早上九点提醒我看盘」. "
                            "It is matched against this turn's request, so a "
                            "paraphrase, a translation, or a sentence about why "
                            "the task would be useful is refused. If nothing in "
                            "this turn asked for a task, do not call this at "
                            "all: propose it in your reply and let the user ask."
                        ),
                    },
                    "name": {"type": "string", "description": "Short task name"},
                    "trigger_type": {
                        "type": "string",
                        "description": "one of: once, interval, daily, weekly, weekdays, monthly, signal",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": (
                            "Signal to wait for when trigger_type=signal. A "
                            "finished task's own signal is "
                            "task:<task_id>:succeeded, task:<task_id>:failed, "
                            "task:<task_id>:cancelled or task:<task_id>:interrupted "
                            "-- use schedule_list to find task ids. For a signal "
                            "you will emit yourself, check list_signals for the "
                            "name already in use."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Backward-compatible content field. Defaults to a literal message unless action_type=agent_task.",
                        "default": "",
                    },
                    "action_type": {
                        "type": "string",
                        "description": "one of: message, agent_task, system_job",
                    },
                    "message_text": {
                        "type": "string",
                        "description": "Literal message to send at the scheduled time",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Agent instruction to execute at the scheduled time",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Internal system job name, e.g. memory_tidy",
                    },
                    "timezone_name": {
                        "type": "string",
                        "description": (
                            "IANA timezone name like Asia/Shanghai. Omit it to use "
                            "the timezone this machine is already in -- a wall-clock "
                            "time that names no zone means the local one, never UTC."
                        ),
                        "default": LOCAL_TIMEZONE,
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO datetime for once triggers",
                    },
                    "every": {
                        "type": "integer",
                        "description": "Interval count for interval triggers",
                    },
                    "unit": {
                        "type": "string",
                        "description": "minutes|hours|days|weeks for interval triggers",
                    },
                    "time_of_day": {
                        "type": "string",
                        "description": "HH:MM for daily/weekly triggers",
                    },
                    "day_of_week": {
                        "type": "string",
                        "description": "mon|tue|wed|thu|fri|sat|sun for weekly triggers",
                    },
                    "day_of_month": {
                        "type": "integer",
                        "description": "1-31 for monthly triggers; months without that date are skipped",
                    },
                    "delivery_mode": {
                        "type": "string",
                        "description": "optional override: standalone or channel",
                    },
                    "permission_profile": {
                        "type": "string",
                        "description": (
                            "What this unattended run may do. one of: inherit "
                            "(no overrides; anything needing confirmation is refused), "
                            "read_only (never write the workspace), "
                            "workspace_write (write inside the workspace and run commands "
                            "without asking -- also requires `workspace_root`). "
                            "Defaults to inherit."
                        ),
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": (
                            "Absolute path of the project folder the run works in. "
                            "Required for permission_profile=workspace_write; "
                            "defaults to the current session's folder."
                        ),
                    },
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "What has to be true for a run of this task to count as a "
                            "success, in plain sentences. Read by whoever looks at a "
                            "failed run, and given to the run itself so it knows what "
                            "it is aiming at. Omit for a task with no checkable "
                            "outcome, which then succeeds as soon as it delivers."
                        ),
                    },
                    "verify_command": {
                        "type": "string",
                        "description": (
                            "Optional command that decides success by its exit code -- "
                            "run in the task's folder after the work, 0 means it "
                            "worked. Use this whenever success is mechanically "
                            "checkable (a test suite, a build, a linter, a script that "
                            "validates the output); it is the only evidence a run "
                            "cannot talk its way out of. Must be a single low-risk "
                            "command: no pipes, redirection, `rm`, or inline "
                            "interpreters such as `python -c`. A command that cannot "
                            "be run is refused here, while you can still fix it."
                        ),
                    },
                    "produces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The files this task has to leave behind, as paths "
                            "relative to the project folder -- e.g. report.md or "
                            "state/equity.csv. Fill this in whenever the work's "
                            "product is a file: it is the only way the run is told "
                            "where to write (until now the path existed only in "
                            "prose, or only inside verify_command, checking a file "
                            "the run was never told about), it is what a downstream "
                            "task is handed, and a declared file that is missing "
                            "when the run ends fails the run on its own. Must be "
                            "relative and inside the folder: no absolute paths, no "
                            "'..'. Omit for a task whose only output is its answer."
                        ),
                    },
                },
                "required": ["name", "trigger_type"],
            },
            schedule_tools.schedule_create,
            source="builtin",
        )
    
        r.register(
            "schedule_list",
            "List persistent scheduled tasks.",
            {"type": "object", "properties": {}, "required": []},
            schedule_tools.schedule_list,
            source="builtin",
        )
    
        r.register(
            "schedule_runs",
            (
                "Read a scheduled task's run history -- when each run happened, "
                "whether it succeeded, and, when it did not, why -- together with "
                "the task's whole current definition (its instruction, its trigger, "
                "its criteria, the files it declares, its permission posture), in "
                "the same vocabulary schedule_update takes -- that is, every field "
                "under `task` except `id`, `workflow_id`, `step_key`, `enabled`, "
                "`delivery_mode`, `delivery_target` and `request_quote`, which are "
                "reported for reading only because the graph, "
                "schedule_set_enabled and the channel own them. Use it after "
                "schedule_create or workflow_create to find out what actually "
                "happened, and whenever the user asks whether a task ran, whether "
                "it worked, or what went wrong with it. A scheduled run happens "
                "with nobody watching and its outcome exists nowhere else, so this "
                "is the only way to tell a task that has been succeeding from one "
                "that has failed every night since it was made. Returns the run's "
                "`error` and the acceptance check's own result; a run's full text "
                "is at the `output_path` it returns. The most common failure is a "
                "defect in the definition rather than in the work, so read the "
                "definition here and fix it with schedule_update -- retrying a "
                "definition that cannot be satisfied only fails again."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "The task's id, from schedule_list or workflow_list."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many recent runs to return (default 10, max 50).",
                        "default": 10,
                    },
                },
                "required": ["task_id"],
            },
            schedule_tools.schedule_runs,
            source="builtin",
        )
    
        r.register(
            "schedule_delete",
            (
                "Delete a persistent scheduled task by id, along with its run "
                "history. Refused for a task that is running (cancel the run "
                "first) and for a step of a workflow that still exists -- a step "
                "is the graph's to remove, so delete the workflow or edit it "
                "instead. A step left behind by an already-deleted workflow is "
                "an ordinary task and can be deleted here."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id to delete",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this task is being deleted, and what "
                            "it was for."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.schedule_delete,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "schedule_update",
            (
                "Change part of an existing scheduled task. Only the fields named in "
                "the call change; everything else keeps the value the task already "
                "has. Use this whenever a task is wrong rather than missing -- the "
                "verify_command names a file the step never writes, the criterion is "
                "not what the user meant, the instruction needs a line added, a "
                "declared product was never filled in. Read the current definition "
                "first with schedule_runs, which returns it in exactly the vocabulary "
                "this takes. "
                "The edit keeps the task id, so its run history survives and every "
                "step subscribed to its signal keeps working -- which is why this, "
                "and not deleting the task and creating it again, is how a definition "
                "is corrected. Deleting takes the id and the history with it. "
                "If the task is a step of a workflow, its trigger belongs to the graph "
                "and cannot be changed here; edit the workflow instead. Whether the "
                "task runs at all is schedule_set_enabled's. Pass only the fields you "
                "are changing: a field sent with the same value it already had is "
                "harmless, and `changed` in the reply says what actually moved."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list or schedule_runs",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. What this edit does and why, specifically "
                            "-- e.g. 「把 verify_command 改成检查 07_正文.md，它实际写在 "
                            "日期目录下」."
                        ),
                    },
                    "name": {"type": "string", "description": "New task name"},
                    "action_type": {
                        "type": "string",
                        "description": "one of: message, agent_task, system_job",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "New agent instruction (action_type=agent_task)",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Same as `prompt`; accepted for symmetry with workflow_create",
                    },
                    "message_text": {
                        "type": "string",
                        "description": "New literal message (action_type=message)",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Internal system job name (action_type=system_job)",
                    },
                    "trigger_type": {
                        "type": "string",
                        "description": (
                            "New trigger kind: once, interval, daily, weekly, "
                            "weekdays, monthly, signal. Refused on a step that "
                            "waits on upstreams."
                        ),
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO datetime, for trigger_type=once or interval",
                    },
                    "every": {"type": "integer", "description": "Interval count"},
                    "unit": {
                        "type": "string",
                        "description": "minutes|hours|days|weeks, for interval",
                    },
                    "time_of_day": {
                        "type": "string",
                        "description": "HH:MM for daily/weekly/weekdays/monthly",
                    },
                    "day_of_week": {"type": "string", "description": "mon|tue|...|sun"},
                    "day_of_month": {"type": "integer", "description": "1-31"},
                    "signal_name": {
                        "type": "string",
                        "description": "Signal to wait for, when trigger_type=signal",
                    },
                    "timezone_name": {
                        "type": "string",
                        "description": "IANA timezone name, e.g. Asia/Shanghai",
                    },
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replaces the success criteria. Sending [] removes "
                            "them -- which is an answer, not an omission: a "
                            "field left out is what keeps them."
                        ),
                    },
                    "verify_command": {
                        "type": "string",
                        "description": (
                            "Replaces the acceptance command whose exit code "
                            "decides success. Must be a single low-risk command: "
                            "no pipes, redirection, `rm`, or inline interpreters."
                        ),
                    },
                    "produces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replaces the files this task declares it will leave "
                            "behind, as paths relative to the project folder. "
                            "Call out [] to declare none."
                        ),
                    },
                    "permission_profile": {
                        "type": "string",
                        "description": "inherit|read_only|workspace_write",
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": "Absolute path of the project folder the run works in",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "How long one run may take, 10 to 604800",
                    },
                    # The four below are here so that a definition read back
                    # with ``schedule_runs`` can be handed straight in.  They
                    # are the rest of what the builder reads, and leaving them
                    # out did not hide them -- it made the whole read-back
                    # unusable, because this schema refuses unknown fields and
                    # the reply names all of them.
                    "context_policy": {
                        "type": "string",
                        "description": "stateless|task_history|shared_memory",
                    },
                    "retry_policy": {
                        "type": "object",
                        "description": (
                            "How a failed run is retried: "
                            "{max_attempts, backoff_seconds}"
                        ),
                    },
                    "selected_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replaces the skills pinned to this task. "
                            "Call out [] to pin none."
                        ),
                    },
                    "model_override": {
                        "type": ["string", "null"],
                        "description": (
                            "Model id this task's runs use; null to follow the "
                            "active provider. An id no provider group owns is "
                            "refused here rather than at 3am."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.schedule_update,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "schedule_set_enabled",
            (
                "Switch a scheduled task on or off. Use it to pause something that "
                "is failing or unwanted, and to bring it back. The task keeps its "
                "definition and its history; what changes is whether it is still "
                "triggered. A task that is already running or queued is not affected "
                "-- to stop a run in flight use schedule_cancel. Refused for a step "
                "of a workflow that still exists: a step's switch is rewritten from "
                "its workflow's on every save, so pausing the workflow is the switch "
                "that holds."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "false pauses the task, true lets it run again",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this task is being switched, "
                            "specifically."
                        ),
                    },
                },
                "required": ["task_id", "enabled", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.schedule_set_enabled,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "schedule_run",
            (
                "Start a run of a scheduled task right now instead of waiting for its "
                "next occurrence. With no `run_id` it runs the task as it is now, "
                "which is how an edit is confirmed: fix the definition with "
                "schedule_update, then run it and read the result with schedule_runs. "
                "With a `run_id` it re-runs the snapshot that run was started with, so "
                "the outcome is about the work and not about the definition -- the "
                "same flake gets another try unchanged, and a fix cannot be mistaken "
                "for one. The reply says which of three things happened, and they are "
                "not the same promise: `started` means a scheduler in this process is "
                "executing the run now (the run id is returned), `woken` means a live "
                "scheduler on another event loop took the request and will start it "
                "immediately, and `queued` means there is no scheduler in this process "
                "-- the run is on the durable queue for the gateway or the "
                "`agent scheduler` service to consume. Say which one you got; do not "
                "report a queued run as started. Either way the run itself happens in "
                "the background and may take minutes: the outcome is in schedule_runs, "
                "not in this reply. Refused while the task already has a run in "
                "flight."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list",
                    },
                    "run_id": {
                        "type": "string",
                        "description": (
                            "Optional. Re-run this earlier run's own snapshot "
                            "instead of the task as it is now."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this task is being run now, specifically."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.schedule_run,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "schedule_cancel",
            (
                "Ask a run that is in flight to stop. Use it when a run is stuck, "
                "overrunning its timeout, or clearly doing the wrong thing. It is a "
                "request, not a kill: the run stops at its next checkpoint, so the "
                "reply says the cancellation was asked for rather than done -- check "
                "schedule_runs for where it actually ended. Omit `run_id` to mean the "
                "task's current run, which is the only one it can mean. To stop a task "
                "from being triggered again, use schedule_set_enabled instead."
            ),
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Scheduled task id, from schedule_list",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "Optional. Defaults to the run currently in flight.",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this run is being cancelled, "
                            "specifically."
                        ),
                    },
                },
                "required": ["task_id", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.schedule_cancel,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "workflow_create",
            (
                "Create a chain of scheduled tasks, where each step runs only after the "
                "steps it depends on have succeeded. Use this when the user asks for "
                "several jobs to run in an order -- fetch, then transform, then report -- "
                "rather than one job; describing an existing process is not asking for a "
                "new one to be built, so propose the chain instead of creating it. "
                "Do not chain steps with schedule_create and signals "
                "instead: a chain built that way has no recorded edges, so nothing knows "
                "which steps a failure should stop. "
                "Give every step a `key`, and list its upstreams in `depends_on`. A step "
                "with no upstreams is where the chain starts and must carry its own "
                "`trigger_type` (the same vocabulary as schedule_create); a step with "
                "upstreams must NOT have one, because its upstreams are what starts it. "
                "Give each step `criteria` saying what has to be true for it to count as "
                "done, and a `verify_command` whenever that is mechanically checkable -- "
                "that check is what decides whether the next step runs. The graph is "
                "validated before anything is written: a cycle, a missing upstream, or a "
                "step that can never be judged is refused with the step named. "
                "The chain outlives this conversation, so `intent` has to carry the "
                "user's own words: it is checked against the request that commissioned "
                "this turn, and a call whose intent cannot be found in it is refused."
            ),
            {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": (
                            "Fill this in, or the call is refused. Quote the "
                            "user, verbatim -- at least six characters copied "
                            "from what they actually typed this turn, the "
                            "sentence that asked for this chain. It is matched "
                            "against this turn's request, so a paraphrase or a "
                            "sentence about why the chain would be useful is "
                            "refused. If nothing in this turn asked for one, do "
                            "not call this at all: propose the chain in your reply."
                        ),
                    },
                    "name": {"type": "string", "description": "Short workflow name"},
                    "description": {
                        "type": "string",
                        "description": "What this chain is for, in one line",
                        "default": "",
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": (
                            "Absolute path of the project folder the chain works in. "
                            "Steps that do not name their own folder use this one."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "description": "The steps, in any order; `depends_on` gives the order.",
                        "items": _WORKFLOW_STEP_SCHEMA,
                    },
                },
                "required": ["name", "steps"],
            },
            schedule_tools.workflow_create,
            source="builtin",
        )
    
        r.register(
            "workflow_list",
            (
                "List workflows, with each one's steps and the edges between them. "
                "Use before creating a chain, to see whether one already exists."
            ),
            {"type": "object", "properties": {}, "required": []},
            schedule_tools.workflow_list,
            source="builtin",
        )
    
        r.register(
            "workflow_delete",
            (
                "Delete a workflow by id. The tasks it built are disabled rather than "
                "erased, so their run history stays readable; this stops the chain and "
                "stops any signal subscription its steps hold. The leftovers are then "
                "ordinary tasks: schedule_delete removes any of them, and its history, "
                "when nobody needs to read it any more."
            ),
            {
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow id to delete; workflow_list shows them",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. Why this chain is being deleted, and what "
                            "it was for."
                        ),
                    },
                },
                "required": ["workflow_id", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.workflow_delete,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "workflow_update",
            (
                "Change an existing chain in place: add a step, remove one, move one, "
                "reword one, rename or pause the whole chain. Use this whenever a "
                "workflow needs adjusting -- never delete it and build it again, "
                "which cuts every edge: a step's task id appears in the signal names "
                "the steps below it subscribe to, so a rebuilt chain carries on "
                "subscribed to ids nothing emits. Editing keeps those ids. "
                "Steps are matched by `key`, and that is the whole contract: a key "
                "already in the chain is the same step and keeps every field this "
                "call does not mention; a new key is a new step; a stored key missing "
                "from the list is removed, and its task is switched off rather than "
                "deleted, so its run history survives. "
                "Leave `steps` out entirely to rename or pause without restating the "
                "graph. When you do send it, send the whole list in the order the "
                "chain should run, keeping the existing keys for the steps that "
                "already exist -- changing a key abandons that step's history. Read "
                "the current chain first with workflow_list."
            ),
            {
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow id, from workflow_list",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "Required. What this change does and why, specifically."
                        ),
                    },
                    "name": {"type": "string", "description": "New workflow name"},
                    "description": {
                        "type": "string",
                        "description": "New one-line description of what the chain is for",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": (
                            "Pause or resume every step. This is the switch that "
                            "holds for a chain, since a step cannot be switched "
                            "on its own."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "description": (
                            "The whole chain, in the order it should run; "
                            "`depends_on` gives the actual order. Omit to leave the "
                            "steps untouched. Keep the existing keys."
                        ),
                        "items": _WORKFLOW_STEP_SCHEMA,
                    },
                },
                "required": ["workflow_id", "intent"],
                "additionalProperties": False,
            },
            schedule_tools.workflow_update,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
