"""Direct tests for the pieces extracted out of ``BaseAgent``.

Each of these was previously reachable only by driving a full turn through
``send_message`` — a 500-line function with an LLM call in the middle — so
none of them had focused tests.  Being testable in isolation is most of the
point of the extraction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.core.agent import _ContentFilterRecovery
from agent.orchestration import contracts
from agent.runtime.heartbeat import TurnHeartbeat


# ── Output contract ────────────────────────────────────────────────────────


def test_contract_parse_normalizes_and_round_trips():
    contract = contracts.OutputContract.parse(
        {"format": "  JSON  ", "required_keys": ["a", " ", "b"], "required_files": []}
    )
    assert contract.format == "json"
    assert contract.required_keys == ("a", "b")
    assert contract.to_dict() == {"format": "json", "required_keys": ["a", "b"]}


def test_empty_contract_is_falsy():
    assert not contracts.OutputContract.parse(None)
    assert not contracts.OutputContract.parse({})
    assert contracts.OutputContract.parse({"format": "json"})


def test_json_contract_implies_a_deliverable_block():
    """The invariant validate() relies on: requires_json ⇒ requires_deliverable."""
    contract = contracts.OutputContract.parse({"format": "json"})
    assert contract.requires_json
    assert contract.requires_deliverable(expected_output="")


def test_instructions_and_validator_agree_on_the_block():
    """The prompt half and the checking half must ask for the same thing."""
    contract = contracts.OutputContract.parse({"required_keys": ["total"]})
    instructions = contracts.contract_instructions("give me a total", contract)

    assert any("<deliverable>" in line for line in instructions)

    good = 'text <deliverable>{"total": 3}</deliverable> more'
    result = contracts.validate(
        good,
        expected_output="give me a total",
        contract=contract,
        resolve_path=Path,
    )
    assert result.ok
    assert result.structured_content == {"total": 3}


def test_validate_rejects_missing_deliverable():
    result = contracts.validate(
        "just prose",
        expected_output="a summary",
        contract=contracts.OutputContract.parse(None),
        resolve_path=Path,
    )
    assert not result.ok
    assert "missing <deliverable>" in (result.error or "")


def test_validate_rejects_non_json_and_non_object_deliverables():
    contract = contracts.OutputContract.parse({"format": "json"})
    bad_json = contracts.validate(
        "<deliverable>not json</deliverable>",
        expected_output="",
        contract=contract,
        resolve_path=Path,
    )
    assert not bad_json.ok
    assert "not valid JSON" in (bad_json.error or "")

    not_object = contracts.validate(
        "<deliverable>[1, 2]</deliverable>",
        expected_output="",
        contract=contract,
        resolve_path=Path,
    )
    assert not not_object.ok
    assert "must be an object" in (not_object.error or "")


def test_validate_reports_missing_required_keys_but_keeps_the_parse():
    contract = contracts.OutputContract.parse({"required_keys": ["a", "b"]})
    result = contracts.validate(
        '<deliverable>{"a": 1}</deliverable>',
        expected_output="",
        contract=contract,
        resolve_path=Path,
    )
    assert not result.ok
    assert "missing required deliverable keys: b" in (result.error or "")
    assert result.structured_content == {"a": 1}


def test_validate_checks_required_files(tmp_path):
    present = tmp_path / "there.txt"
    present.write_text("x", encoding="utf-8")
    contract = contracts.OutputContract.parse(
        {"required_files": ["there.txt", "missing.txt"]}
    )

    result = contracts.validate(
        "done",
        expected_output="",
        contract=contract,
        resolve_path=lambda name: tmp_path / name,
    )
    assert not result.ok
    assert "missing.txt" in (result.error or "")
    assert "there.txt" not in (result.error or "")


def test_validate_surfaces_an_unresolvable_path():
    contract = contracts.OutputContract.parse({"required_files": ["../escape"]})

    def _reject(_name: str) -> Path:
        raise ValueError("outside the workspace")

    result = contracts.validate(
        "done", expected_output="", contract=contract, resolve_path=_reject
    )
    assert not result.ok
    assert "invalid required output file path" in (result.error or "")


def test_no_contract_passes_content_through():
    result = contracts.validate(
        "anything",
        expected_output="",
        contract=contracts.OutputContract.parse(None),
        resolve_path=Path,
    )
    assert result.ok
    assert result.content == "anything"


def test_extract_deliverable_is_case_and_newline_tolerant():
    assert contracts.extract_deliverable("<DELIVERABLE>\n x \n</deliverable>") == "x"
    assert contracts.extract_deliverable("<deliverable>  </deliverable>") is None
    assert contracts.extract_deliverable("") is None


def test_string_list_coerces_scalars_and_drops_blanks():
    assert contracts.string_list(None) == []
    assert contracts.string_list("  ") == []
    assert contracts.string_list("one") == ["one"]
    assert contracts.string_list(["a", " ", "b"]) == ["a", "b"]
    assert contracts.string_list(7) == ["7"]


# ── Content-filter recovery state ──────────────────────────────────────────


def test_recovery_records_and_forgets_a_submission():
    recovery = _ContentFilterRecovery()
    assert recovery.submitted_tool_uses is None

    tool_uses = [{"name": "shell"}]
    results = ["output"]
    recovery.record_submission(tool_uses, results)
    assert recovery.submitted_tool_uses == tool_uses
    assert recovery.submitted_results == results

    # Copies, not aliases: mutating the turn's lists must not rewrite history.
    tool_uses.append({"name": "read_file"})
    assert recovery.submitted_tool_uses == [{"name": "shell"}]

    recovery.forget_submission()
    assert recovery.submitted_tool_uses is None
    assert recovery.submitted_results is None


def test_pending_response_is_consumed_exactly_once():
    """Consumed twice, the second loop iteration would replay a stale response."""
    recovery = _ContentFilterRecovery()
    recovery.pending_response = "recovered"

    assert recovery.take_pending_response() == "recovered"
    assert recovery.take_pending_response() is None


# ── Turn heartbeat ─────────────────────────────────────────────────────────


class _RecordingWriter:
    def __init__(self):
        self.writes = []
        self.progress_marks = 0

    def mark_progress(self):
        self.progress_marks += 1

    def write(self, **kwargs):
        self.writes.append(kwargs)
        return kwargs


class _RecordingSink:
    def __init__(self):
        self.beats = []

    def on_heartbeat(self, **kwargs):
        self.beats.append(kwargs)


def test_heartbeat_writes_phase_transitions():
    writer = _RecordingWriter()

    async def _run():
        heartbeat = TurnHeartbeat(
            writer=writer, interval_seconds=60.0, turn_id="t1"
        )
        async with heartbeat:
            heartbeat.operation("LLM", "claude-opus-5")
            heartbeat.operation("tools", "shell", current_tool="shell")

    asyncio.run(_run())

    states = [w["state"] for w in writer.writes]
    assert "LLM" in states
    assert "tools" in states
    assert states[-1] == "finished"
    assert writer.writes[-1]["active"] is False
    assert writer.progress_marks == 2


def test_heartbeat_stop_records_the_turn_status():
    writer = _RecordingWriter()

    async def _run():
        heartbeat = TurnHeartbeat(writer=writer, interval_seconds=60.0)
        await heartbeat.__aenter__()
        await heartbeat.stop(status="cancelled", detail="cancelled")

    asyncio.run(_run())

    assert writer.writes[-1]["status"] == "cancelled"
    assert writer.writes[-1]["active"] is False


def test_heartbeat_ticks_the_sink_and_stops_cleanly():
    writer = _RecordingWriter()
    sink = _RecordingSink()

    async def _run():
        heartbeat = TurnHeartbeat(
            writer=writer,
            interval_seconds=0.01,
            pending_messages=lambda: 2,
            sink_provider=lambda: sink,
        )
        async with heartbeat:
            heartbeat.operation("LLM", "model")
            await asyncio.sleep(0.08)

    asyncio.run(_run())

    assert sink.beats, "expected at least one sink tick"
    assert sink.beats[0]["current_op"] == "LLM"
    assert sink.beats[0]["pending_messages"] == 2


def test_heartbeat_without_a_writer_is_inert_but_still_ticks_the_sink():
    sink = _RecordingSink()

    async def _run():
        async with TurnHeartbeat(
            writer=None, interval_seconds=0.01, sink_provider=lambda: sink
        ) as heartbeat:
            heartbeat.operation("LLM", "model")
            await asyncio.sleep(0.05)

    asyncio.run(_run())  # must not raise
    assert sink.beats


def test_heartbeat_survives_a_raising_sink():
    class _Exploding:
        def on_heartbeat(self, **kwargs):
            raise RuntimeError("UI is gone")

    async def _run():
        async with TurnHeartbeat(
            writer=None, interval_seconds=0.01, sink_provider=lambda: _Exploding()
        ) as heartbeat:
            heartbeat.operation("LLM", "model")
            await asyncio.sleep(0.05)

    asyncio.run(_run())  # a broken UI must not fail the turn


def test_heartbeat_stops_even_when_the_body_raises():
    writer = _RecordingWriter()

    async def _run():
        heartbeat = TurnHeartbeat(writer=writer, interval_seconds=0.01)
        with pytest.raises(ValueError):
            async with heartbeat:
                heartbeat.operation("LLM", "model")
                raise ValueError("turn blew up")
        assert heartbeat._task is None

    asyncio.run(_run())
    assert writer.writes[-1]["active"] is False


def test_non_positive_interval_falls_back_to_a_sane_default():
    heartbeat = TurnHeartbeat(writer=None, interval_seconds=0.0)
    assert heartbeat._interval == 5.0
