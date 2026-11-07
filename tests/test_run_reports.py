"""A finished run speaks once, in the conversation, and survives a reload.

This is the difference between an agent and a dashboard: the work comes back to
where it was asked for, instead of waiting to be discovered. The tests here are
about the *rules* (which runs deserve a sentence, and where the sentence goes)
rather than about a browser -- the browser's half is in test_web_ui_e2e.py.
"""

from __future__ import annotations

import pytest

from agent import reports


@pytest.fixture(autouse=True)
def _clean_bus():
    """No listener outlives its test: the bus is process-wide by design."""
    saved = list(reports._listeners)
    reports._listeners.clear()
    yield
    reports._listeners[:] = saved


def test_an_internal_errand_says_nothing():
    """The agent's own housekeeping is machinery, not a message.

    A nightly "memory tidied" is the sentence that teaches a person to stop
    reading the conversation -- and this channel exists to carry the things
    that matter.
    """
    assert (
        reports.build_report(
            task_name="整理记忆",
            task_id="t0",
            run_id="r0",
            status="succeeded",
            summary="memory tidied",
            kind="system_job",
        )
        is None
    )


def test_a_run_with_nothing_to_say_is_not_a_report():
    assert (
        reports.build_report(
            task_name="每天早上看日志",
            task_id="t1",
            run_id="r1",
            status="succeeded",
            summary="",
        )
        is None
    )


def test_a_result_that_finished_speaks():
    """A standing instruction's answer *is* its result, even a quiet one."""
    report = reports.build_report(
        task_name="每天早上看日志",
        task_id="t1",
        run_id="r1",
        status="succeeded",
        summary="没有发现异常",
    )
    assert report is not None
    assert report["task_name"] == "每天早上看日志"
    assert report["text"] == "没有发现异常"


def test_a_failure_always_speaks():
    """Silence after a failure is indistinguishable from having forgotten."""
    report = reports.build_report(
        task_name="每晚复盘",
        task_id="t2",
        run_id="r2",
        status="failed",
        summary="",
        error="provider returned 500",
    )
    assert report is not None
    assert report["status"] == "failed"
    assert "provider returned 500" in report["text"]


def test_a_result_that_is_still_going_or_skipped_is_not_a_report():
    for status in ("queued", "running", "skipped"):
        assert (
            reports.build_report(
                task_name="x",
                task_id="t",
                run_id="r",
                status=status,
                summary="something",
            )
            is None
        ), status


def test_a_report_is_one_line_and_bounded():
    report = reports.build_report(
        task_name="读日志",
        task_id="t3",
        run_id="r3",
        status="succeeded",
        summary="第一行\n第二行\n\n第三行 " + "很长" * 400,
    )
    assert report is not None
    # One line in the transcript: the artifact keeps the formatting.
    assert "\n" not in report["text"]
    assert len(report["text"]) <= 600
    assert report["text"].endswith("…")


def test_one_broken_listener_does_not_silence_the_others():
    """A channel that is down must not fail a run that already succeeded."""
    heard: list[dict] = []

    def broken(_report):
        raise RuntimeError("channel is down")

    reports.subscribe(broken)
    reports.subscribe(heard.append)
    reports.publish({"kind": "run_report", "text": "跑完了"})

    assert [item["text"] for item in heard] == ["跑完了"]


def test_unsubscribing_stops_the_sentence_without_silencing_anyone_else():
    first: list[dict] = []
    second: list[dict] = []

    unsubscribe = reports.subscribe(first.append)
    reports.subscribe(second.append)
    unsubscribe()
    reports.publish({"kind": "run_report", "text": "只该到一个"})

    assert first == []
    assert [item["text"] for item in second] == ["只该到一个"]


# ── the durable half: the sentence is in the transcript, not just on the wire ──


def _sample_report() -> dict:
    report = reports.build_report(
        task_name="每晚复盘",
        task_id="t9",
        run_id="r9",
        status="succeeded",
        summary="整理出 3 条值得看的日志",
    )
    assert report is not None
    return report


def test_a_report_is_journalled_and_reads_back_as_a_message(tmp_path):
    """What the push showed and what a reload shows have to be the same row."""
    from agent import LTMStore
    from agent.session_service import SessionService

    service = SessionService(store=LTMStore(context_dir=tmp_path / "context"))
    row = service.append_report("s-main", _sample_report())

    assert row is not None
    assert row["role"] == "report", "a report is its own kind of row, not a user or agent turn"
    assert row["title"] == "每晚复盘"
    assert row["taskId"] == "t9"

    messages = service.get_messages("s-main")["messages"]
    assert [item["role"] for item in messages] == ["report"]
    assert messages[0]["content"] == row["content"]


def test_the_channel_journals_the_report_and_pushes_the_same_row(tmp_path):
    from agent import LTMStore, shared
    from agent.channels.web import WebChannel, WebConfig
    from agent.main_session import write_main_session_id
    from agent.session_service import SessionService

    channel = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=0, auth_token="", cors_origins=()))
    channel._session_service = SessionService(store=LTMStore(context_dir=tmp_path / "context"))

    pushed: list[dict] = []

    class _Sink:
        def on_report(self, row):
            pushed.append(row)

    write_main_session_id("s-main")
    channel._live_sinks["s-main"] = {_Sink()}
    channel._on_run_report(_sample_report())

    assert [item["content"] for item in pushed] == ["整理出 3 条值得看的日志"]
    # The pushed row is the journal's own row, not a shape assembled twice.
    stored = channel._service().get_messages("s-main")["messages"]
    assert stored and stored[0]["content"] == pushed[0]["content"]
    assert stored[0]["seq"] == pushed[0]["seq"]


def test_a_report_with_no_conversation_yet_is_dropped(tmp_path):
    """A gateway that has never been opened has nowhere to put the sentence."""
    from agent import LTMStore
    from agent.channels.web import WebChannel, WebConfig
    from agent.main_session import clear_main_session_id
    from agent.session_service import SessionService

    clear_main_session_id()
    channel = WebChannel(WebConfig(enabled=True, host="127.0.0.1", port=0, auth_token="", cors_origins=()))
    channel._session_service = SessionService(store=LTMStore(context_dir=tmp_path / "context"))

    channel._on_run_report(_sample_report())

    assert channel._service().list_sessions() == []
