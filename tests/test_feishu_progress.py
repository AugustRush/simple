"""The Feishu progress wordings, asserted directly.

These formatters used to be private methods on ``FeishuOutputSink`` with no
direct test: the only coverage was indirect, through ``on_subagent_event`` and
``_render_primary_markdown`` asserting a finished card.  A wording change could
therefore only be caught by a test about something else — which is exactly what
the extraction into ``agent/channels/feishu_progress.py`` made visible.

The cases below are the ones the code deliberately refuses to guess at: a
boolean that is an ``int``, a denominator that is missing rather than zero, a
concurrency nobody declared, and an event nobody recognises.
"""

from __future__ import annotations

from agent import SubAgentProgressEvent
from agent.channels.feishu_progress import (
    event_spec_count,
    format_batch_finished_event,
    format_batch_started_event,
    format_byte_count,
    format_mode_aware_subagent_event,
    format_seconds,
    format_subagent_event,
    format_tool_progress_line,
    pluralize,
    positive_int,
)


def _event(kind: str, **fields) -> SubAgentProgressEvent:
    return SubAgentProgressEvent(kind=kind, **fields)


# ─────────────────────────────────────────────────────────────────────────────
# Coercions: what is not a number
# ─────────────────────────────────────────────────────────────────────────────


def test_a_boolean_is_not_a_number():
    """``True`` is an ``int`` in Python, so a flag must not render as a count.

    Without the explicit ``bool`` guard, ``int(True) == 1`` and
    ``float(True) == 1.0`` would turn a boolean field into "1 subtask" or
    "1.00s" — a number the caller never sent.
    """
    assert positive_int(True) is None
    assert positive_int(False) is None
    assert format_seconds(True, precision=2) is None
    assert format_byte_count(True) is None


def test_positive_int_needs_a_positive_number():
    assert positive_int(4) == 4
    assert positive_int(4.9) == 4
    assert positive_int(0) is None
    assert positive_int(-3) is None
    assert positive_int("4") is None
    assert positive_int(None) is None


def test_seconds_are_formatted_at_the_requested_precision():
    assert format_seconds(2.5, precision=2) == "2.50s"
    assert format_seconds(0.125, precision=3) == "0.125s"
    assert format_seconds("2.5", precision=2) is None


def test_byte_counts_switch_unit_at_1024():
    assert format_byte_count(512) == "512B"
    assert format_byte_count(1024) == "1.0KB"
    assert format_byte_count(1536) == "1.5KB"
    assert format_byte_count(2 * 1024 * 1024) == "2.0MB"
    assert format_byte_count(None) is None


def test_pluralize_handles_the_zero_case():
    assert pluralize(1, "round") == "round"
    assert pluralize(2, "round") == "rounds"
    assert pluralize(0, "stage") == "stages"


# ─────────────────────────────────────────────────────────────────────────────
# Tool progress lines
# ─────────────────────────────────────────────────────────────────────────────


def test_a_percentage_needs_a_denominator_and_a_zero_one_is_not_an_error():
    """``total: 0`` means "no denominator", not "divide by zero"."""
    without = format_tool_progress_line(
        "shell", {"status": "running", "current": 3, "total": 0}
    )
    assert "%" not in without
    assert without == "- **shell**: running"

    with_total = format_tool_progress_line(
        "shell", {"status": "running", "current": 1, "total": 2}
    )
    assert "50%" in with_total


def test_a_progress_line_carries_bytes_only_when_the_tool_reports_them():
    both = format_tool_progress_line(
        "read_file", {"bytes_done": 1024, "bytes_total": 2048}
    )
    assert "(1.0KB/2.0KB)" in both

    done_only = format_tool_progress_line("read_file", {"bytes_done": 1024})
    assert "(1.0KB)" in done_only

    neither = format_tool_progress_line("read_file", {})
    assert "(" not in neither


def test_a_progress_line_keeps_the_message_and_normalises_the_status():
    line = format_tool_progress_line(
        "web_fetch", {"status": "in_progress", "message": "reading page"}
    )
    assert line == "- **web_fetch**: in progress - reading page"


# ─────────────────────────────────────────────────────────────────────────────
# Sub-agent batch lines
# ─────────────────────────────────────────────────────────────────────────────


def test_event_spec_count_prefers_the_declared_spec_count():
    assert event_spec_count(_event("batch_started", total=5, metrics={"spec_count": 2})) == 2
    assert event_spec_count(_event("batch_started", total=5)) == 5
    assert event_spec_count(_event("batch_started")) is None


def test_a_parallel_batch_without_a_declared_concurrency_declines():
    """It must not advertise a concurrency nobody wrote down.

    Declining returns ``None`` so the caller falls back to the runtime's own
    message instead of printing "max concurrency None".
    """
    unknown = _event("batch_started", total=3, metrics={"execution_mode": "parallel"})
    assert format_batch_started_event(unknown) is None

    known = _event(
        "batch_started",
        total=3,
        metrics={"execution_mode": "parallel", "max_parallel_agents": 4},
    )
    assert format_batch_started_event(known) == "Parallel batch: 3 subtasks, max concurrency 4"


def test_rendezvous_agrees_with_itself_about_the_number_of_rounds():
    one = _event(
        "batch_started",
        total=2,
        metrics={"execution_mode": "rendezvous", "max_rounds": 1},
    )
    assert format_batch_started_event(one) == "Rendezvous batch: 2 subtasks, max 1 round"

    three = _event(
        "batch_started",
        total=2,
        metrics={"execution_mode": "rendezvous", "max_rounds": 3},
    )
    assert format_batch_started_event(three) == "Rendezvous batch: 2 subtasks, max 3 rounds"


def test_a_pipeline_that_lost_subtasks_says_ended_early():
    """A short batch is not a finished one, and the line has to say so."""
    metrics = {"execution_mode": "pipeline", "stage_count": 2, "duration_seconds": 12.5}

    short = _event("batch_finished", completed=1, total=3, metrics=metrics)
    assert format_batch_finished_event(short) == (
        "Pipeline batch ended early: 1/3 across 2 stages in 12.50s"
    )

    full = _event("batch_finished", completed=3, total=3, metrics=metrics)
    assert format_batch_finished_event(full) == (
        "Pipeline batch finished: 3/3 across 2 stages in 12.50s"
    )


def test_the_scope_check_is_mentioned_only_when_it_was_measured():
    base = {"execution_mode": "parallel", "duration_seconds": 3.0}

    without = _event("batch_finished", completed=2, total=2, metrics=base)
    assert format_batch_finished_event(without) == "Parallel batch finished: 2/2 in 3.00s"

    with_scope = _event(
        "batch_finished",
        completed=2,
        total=2,
        metrics={**base, "write_scope_check_seconds": 0.125},
    )
    assert format_batch_finished_event(with_scope) == (
        "Parallel batch finished: 2/2 in 3.00s (scope check 0.125s)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Generic events and the fallback chain
# ─────────────────────────────────────────────────────────────────────────────


def test_an_unrecognised_event_renders_as_nothing_at_all():
    """``""`` and not a placeholder: the caller drops empty lines.

    ``_append_progress_text`` returns early on a blank line, so an event nobody
    recognises must render as empty rather than as a paragraph that would put a
    blank gap in the card.
    """
    assert format_subagent_event(_event("something_new")) == ""
    assert format_subagent_event(_event("phase_note")) == ""
    assert format_mode_aware_subagent_event(_event("something_new")) == ""
    assert format_mode_aware_subagent_event(_event("phase_note")) == ""


def test_a_batch_event_its_mode_declines_still_gets_a_line():
    """The mode-aware formatter falls back rather than dropping the event."""
    declined = _event("batch_started", total=3, metrics={"execution_mode": "parallel"})
    assert format_mode_aware_subagent_event(declined) == "Starting 3 sub-agents"

    accepted = _event("batch_started", total=3, metrics={"execution_mode": "pipeline"})
    assert format_mode_aware_subagent_event(accepted) == (
        "Pipeline batch: 3 subtasks, dependency-driven execution"
    )


def test_agent_lifecycle_lines_name_the_role_and_its_outcome():
    assert format_subagent_event(_event("agent_started", role="reviewer")) == (
        "→ **reviewer** started"
    )
    finished = _event(
        "agent_finished", role="reviewer", metrics={"duration_seconds": 2.5}
    )
    assert format_subagent_event(finished) == "✓ **reviewer** finished (2.5s)"
    # An unnamed sub-agent still gets a readable role rather than "None".
    assert format_subagent_event(_event("agent_failed")) == "✗ **agent** failed"
