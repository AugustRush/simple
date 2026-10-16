"""Signals and step output: emitting a signal, listing the ones a session knows, reading a step's output, and reporting how a run ended. Split out of BuiltinTools._register (see the remaining-big-files survey, section 2c)."""

from __future__ import annotations

from .step_output_tools import READ_STEP_OUTPUT_DEFAULT_CHARS
from .step_output_tools import READ_STEP_OUTPUT_MAX_CHARS

def register_signal_tools(
    r,
    *,
    signal_tools, step_output_tools,
) -> None:
        r.register(
            "emit_signal",
            (
                "Announce that something happened, so any task waiting on that signal runs. "
                "Use this to make a step follow this one without waiting for a clock: emit a "
                "signal here, and another task created with trigger_type=signal and the same "
                "signal_name will run. Subscribe before you emit: a signal with nobody "
                "waiting is recorded and then closed as unmatched, and it will not start a "
                "run for a subscriber created afterwards. "
            "Signals a task emits by itself (task:<id>:succeeded / :failed / :cancelled) "
            "need no call. Call `list_signals` first to see which names are already in use. "
            "Emitting starts whatever is waiting on that name, so `intent` has to carry "
            "the user's own words: it is checked against the request that commissioned "
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
                            "「把 xhs.package.ready 这个信号发出去」. An emission "
                            "wakes tasks, so it is an action rather than a note, "
                            "and it is matched against this turn's request. If "
                            "nothing in this turn asked for a signal, say what "
                            "you would emit in your reply instead."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Signal name. Use a dotted name you choose, e.g. "
                            "report.ready, or task:<task_id>:succeeded to follow a task."
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "Facts to pass downstream, readable by the subscriber in the "
                            "run's signal context. Keep it small."
                        ),
                    },
                },
                "required": ["name"],
            },
            signal_tools.emit_signal,
            source="builtin",
        )
    
        r.register(
            "list_signals",
            (
                "List signal names that have been emitted recently, with how many people "
                "are waiting on each. Use before creating a signal-triggered task so the "
                "subscription matches an existing emission instead of a guess."
            ),
            {"type": "object", "properties": {}, "required": []},
            signal_tools.list_signals,
            source="builtin",
        )
    
        r.register(
            "read_step_output",
            (
                "Read the latest successful output of a step this one stands on. "
                "Use it when this run needs what an upstream step actually produced, rather "
                "than the one-line summary already in this run's context -- for example to "
                "decide what a message should say. Address the step by its key (step2), not "
                "by a task or run id. Only the steps this one depends on, directly or "
                "transitively, are readable, and only outputs the scheduler itself recorded, "
                "so this cannot read arbitrary files."
            ),
            {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "string",
                        "description": "Step key of the upstream step, e.g. step2",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            f"Maximum characters of the output to return "
                            f"(1-{READ_STEP_OUTPUT_MAX_CHARS}). May be capped "
                            f"lower by this run's remaining context budget; the "
                            f"result says so and gives the path to read the rest."
                        ),
                        "default": READ_STEP_OUTPUT_DEFAULT_CHARS,
                    },
                },
                "required": ["step"],
            },
            step_output_tools.read,
            source="builtin",
        )
    
        r.register(
            "report_outcome",
            (
                "Declare that this scheduled run could not do the job it was given. "
                "Call it when you discover the work cannot be done -- the data source is "
                "gone, a required file is missing, the inputs contradict each other -- "
                "instead of writing a plausible answer anyway. The run is then recorded "
                "as failed with your reason, the steps below it do not run, and a person "
                "is told. Do not call it to report success: an unattended run is judged "
                "by its own acceptance check, and your assurance is not evidence. Only "
                "usable inside a scheduled run."
            ),
            {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why the work could not be done, in one or two sentences"
                        ),
                    },
                },
                "required": ["reason"],
            },
            signal_tools.report_outcome,
            source="builtin",
        )
