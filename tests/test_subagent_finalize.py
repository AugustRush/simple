"""Finishing a sub-agent: the payload its parent reads, and the memory it leaves.

The two halves are separated because only the second has a side effect.  That
separation is what makes the interesting rules testable without a database:
a failed contract must not write memory, and an oversized result is summarized
for the parent while the **full** text goes to memory.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.subagent_finalize import (
    FinalizedResult,
    build_result_payload,
    finalize_sub_agent,
)


class _Result:
    def __init__(self, content: str = "the answer", error=None, tools=None) -> None:
        self.content = content
        self.error = error
        self.tool_calls_made = tools if tools is not None else ["bash"]


class _Entry:
    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


class _Store:
    def __init__(self) -> None:
        self.entries: list = []

    def add_entry(self, entry) -> None:
        self.entries.append(entry)


class _Manager:
    def __init__(self, session_id: str = "s-1") -> None:
        self.staging = type("S", (), {"session_id": session_id})()
        self.store = _Store()


def _ok_validator(content, **kwargs):
    """Close to the real ``_validate_output_contract`` for the no-contract case.

    Content is positional, and the validator returns the content it was given --
    which is why a placeholder set *before* validation can be overwritten.  That
    is real behaviour, not a stub artefact: see the test below.
    """
    return True, content, None, None


def _payload(result=None, validator=_ok_validator, expected_output="", contract=None):
    return build_result_payload(
        result=result or _Result(),
        role="researcher",
        shaped_task="read the docs",
        expected_output=expected_output,
        output_contract=contract or {},
        validate_output_contract=validator,
    )


# ── The payload ──────────────────────────────────────────────────────────────


def test_a_clean_result_is_ok() -> None:
    payload = _payload()
    assert payload["ok"] is True
    assert payload["content"] == "the answer"
    assert payload["tool_calls_made"] == ["bash"]
    assert "error" not in payload


def test_empty_content_is_filled_in_before_validation_but_not_after() -> None:
    """The ``(no output)`` marker is a pre-validation default, not a guarantee.

    ``payload["content"]`` starts as the marker, then a *successful* validation
    replaces it with whatever the validator returned -- which for an empty answer
    with no contract is the empty string.  Recorded rather than changed: it is
    pre-existing behaviour moved verbatim, and the marker still does its job in
    the failure path, where nothing overwrites it.
    """
    assert _payload(result=_Result(content=""))["content"] == ""

    def failing_validator(content, **kwargs):
        return False, "", None, "no deliverable"

    assert _payload(result=_Result(content=""), validator=failing_validator)[
        "content"
    ] == "(no output)"


def test_a_sub_agent_error_marks_the_payload_not_ok() -> None:
    payload = _payload(result=_Result(error="upstream 503"))
    assert payload["ok"] is False
    assert payload["error"] == "upstream 503"


def test_a_contract_failure_marks_the_payload_and_carries_the_reason() -> None:
    def failing_validator(content, **kwargs):
        return False, "", None, "missing <deliverable> block"

    payload = _payload(validator=failing_validator, expected_output="a patch")
    assert payload["ok"] is False
    assert payload["error"] == "missing <deliverable> block"


def test_the_sub_agent_error_overwrites_the_contract_error() -> None:
    """A run that both failed and missed its contract reports the *cause*.

    The contract miss is a consequence of the failure, and the failure is what a
    reader can act on.  The original ordering is preserved here because it is
    easy to "tidy" into the opposite precedence.
    """

    def failing_validator(content, **kwargs):
        return False, "", None, "contract not satisfied"

    payload = _payload(
        result=_Result(error="model refused"), validator=failing_validator
    )
    assert payload["error"] == "model refused"


def test_structured_content_appears_only_when_offered() -> None:
    def structured_validator(content, **kwargs):
        return True, "text", {"a": 1}, None

    assert _payload(validator=structured_validator)["structured_content"] == {"a": 1}
    assert "structured_content" not in _payload()


def test_a_successful_contract_replaces_the_content_with_its_own() -> None:
    """The validator may extract a deliverable out of the raw answer."""

    def extracting_validator(content, **kwargs):
        return True, "extracted", None, None

    assert _payload(validator=extracting_validator)["content"] == "extracted"


def test_the_validator_receives_the_raw_content_and_contract() -> None:
    seen: dict = {}

    def recording_validator(content, *, expected_output, output_contract):
        seen.update(content=content, expected_output=expected_output, output_contract=output_contract)
        return True, content, None, None

    _payload(
        result=_Result(content="raw"),
        validator=recording_validator,
        expected_output="a patch",
        contract={"format": "json"},
    )
    assert seen["content"] == "raw"
    assert seen["expected_output"] == "a patch"
    assert seen["output_contract"] == {"format": "json"}


# ── Finalization: summarization ──────────────────────────────────────────────


def _finalize(payload, *, content=None, limit=10, manager=None, summarize=None, run_id=""):
    calls: list = []

    async def default_summarize(text, role, task):
        calls.append((text, role, task))
        return "SUMMARY"

    async def run():
        return await finalize_sub_agent(
            payload,
            full_content=payload["content"] if content is None else content,
            role="researcher",
            shaped_task="read the docs",
            run_id=run_id,
            content_limit=limit,
            summarize=summarize or default_summarize,
            context_manager=manager,
            new_id=lambda: "id-1",
            now="2026-01-01 00:00 UTC",
            entry_type=_Entry,
            suppress=_null_suppress,
        )

    return asyncio.run(run()), calls


#: The real helper, not a stub: the behaviour under test is that a store failure
#: is swallowed, so a substitute that does not swallow would test nothing.
from agent.shared import _suppress_with_log as _null_suppress  # noqa: E402


def test_a_short_result_is_not_summarized() -> None:
    payload = _payload()
    finalized, calls = _finalize(payload, limit=1000)
    assert finalized.summarized is False
    assert calls == []
    assert payload["content"] == "the answer"


def test_a_long_result_is_summarized_for_the_parent() -> None:
    payload = _payload(result=_Result(content="x" * 100))
    finalized, calls = _finalize(payload, limit=10)
    assert finalized.summarized is True
    assert payload["content"] == "SUMMARY"
    assert calls and calls[0][1] == "researcher"


def test_the_summary_replaces_only_the_payload_not_the_memory_text() -> None:
    """The parent's window is finite; the memory entry is not.

    Writing the summary to memory would lose the detail exactly when it was
    worth keeping, which is why ``full_content`` is a separate argument.
    """
    full = "y" * 200
    manager = _Manager()
    payload = _payload(result=_Result(content=full))
    _finalize(payload, content=full, limit=10, manager=manager)
    assert payload["content"] == "SUMMARY"
    assert full in manager.store.entries[0].content
    assert "SUMMARY" not in manager.store.entries[0].content


# ── Finalization: the memory write ───────────────────────────────────────────


def test_a_successful_result_is_written_to_memory() -> None:
    manager = _Manager()
    payload = _payload()
    finalized, _ = _finalize(payload, manager=manager)
    assert len(manager.store.entries) == 1
    assert finalized.memory_entry_id == "id-1"
    assert payload["artifact_ref"] == "id-1"


def test_a_failed_result_is_not_written_to_memory() -> None:
    """A failed sub-agent's text is not a finding, and memory is read as fact."""
    manager = _Manager()
    payload = _payload(result=_Result(error="boom"))
    _finalize(payload, manager=manager)
    assert manager.store.entries == []
    assert "artifact_ref" not in payload


def test_a_contract_failure_is_not_written_to_memory() -> None:
    """Even when the sub-agent itself succeeded: the answer did not meet the ask."""

    def failing_validator(content, **kwargs):
        return False, "", None, "not satisfied"

    manager = _Manager()
    payload = _payload(validator=failing_validator)
    _finalize(payload, manager=manager)
    assert manager.store.entries == []


def test_no_context_manager_skips_the_write_without_failing() -> None:
    payload = _payload()
    finalized, _ = _finalize(payload, manager=None)
    assert finalized.memory_entry_id is None
    assert payload["ok"] is True


def test_the_memory_entry_records_the_role_and_task() -> None:
    manager = _Manager()
    _finalize(_payload(), manager=manager)
    entry = manager.store.entries[0]
    assert entry.entity == "researcher"
    assert "read the docs" in entry.content
    assert entry.memory_type == "sub_agent_observation"


def test_a_tracked_run_is_scoped_to_its_run_id() -> None:
    manager = _Manager()
    _finalize(_payload(), manager=manager, run_id="run-7")
    assert manager.store.entries[0].scope == "run:run-7"


def test_an_untracked_run_says_so() -> None:
    manager = _Manager()
    _finalize(_payload(), manager=manager)
    assert manager.store.entries[0].scope == "run:untracked"


def test_the_session_id_comes_from_the_manager() -> None:
    manager = _Manager(session_id="session-9")
    _finalize(_payload(), manager=manager)
    assert manager.store.entries[0].source_session == "session-9"


def test_a_long_task_is_excerpted_in_the_entry() -> None:
    payload = build_result_payload(
        result=_Result(),
        role="r",
        shaped_task="t" * 2000,
        expected_output="",
        output_contract={},
        validate_output_contract=_ok_validator,
    )
    manager = _Manager()
    asyncio.run(
        finalize_sub_agent(
            payload,
            full_content=payload["content"],
            role="r",
            shaped_task="t" * 2000,
            content_limit=10_000,
            summarize=lambda *a: None,
            context_manager=manager,
            new_id=lambda: "i",
            now="now",
            entry_type=_Entry,
            suppress=_null_suppress,
        )
    )
    assert "t" * 501 not in manager.store.entries[0].content


def test_a_store_failure_does_not_fail_the_turn() -> None:
    """The artifact is a by-product of the turn, not a precondition for it."""

    class _BrokenStore:
        @staticmethod
        def add_entry(entry) -> None:
            raise OSError("disk")

    class _Broken(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.store = _BrokenStore()

    payload = _payload()
    finalized, _ = _finalize(payload, manager=_Broken())
    assert payload["ok"] is True
    assert finalized.memory_entry_id is None


def test_a_finalized_result_is_frozen() -> None:
    with pytest.raises(Exception):
        FinalizedResult(payload={}).summarized = True  # type: ignore[misc]
