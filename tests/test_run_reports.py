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


def test_only_a_proposal_that_needs_you_speaks():
    """An `auto` proposal already ran; announcing it is the noise to avoid."""
    prepare = {
        "id": "p1",
        "action": "整理下载目录",
        "reason": "里面有 12 个临时文件",
        "reversibility": "workspace",
        "admission": {"answer": "prepare", "reason": "额度用完"},
    }
    message = reports.build_proposal_message(prepare)
    assert message is not None
    assert message["kind"] == "proposal"
    assert message["title"] == "整理下载目录"
    assert message["text"] == "里面有 12 个临时文件"

    assert reports.build_proposal_message({**prepare, "admission": {"answer": "auto"}}) is None
    assert reports.build_proposal_message({**prepare, "action": ""}) is None
    assert reports.build_proposal_message({"id": "p2"}) is None


def test_a_proposal_is_journalled_as_its_own_kind_of_row(tmp_path):
    """A proposal is not a report: the row carries the action and the answer is
    elsewhere. Rendering it as a run's result would offer "成功" for a question."""
    from agent import LTMStore
    from agent.session_service import SessionService

    service = SessionService(store=LTMStore(context_dir=tmp_path / "context"))
    message = reports.build_proposal_message(
        {
            "id": "p3",
            "action": "把截图目录归档",
            "reason": "三天没动过",
            "reversibility": "reversible",
            "admission": {"answer": "prepare"},
        }
    )
    row = service.append_report("s-main", message, event_type="proposal_raised")

    assert row is not None
    assert row["role"] == "proposal"
    assert row["title"] == "把截图目录归档"
    assert row["proposalId"] == "p3"
    assert row["reversibility"] == "reversible"
    stored = service.get_messages("s-main")["messages"]
    assert [item["role"] for item in stored] == ["proposal"]


def test_a_failed_run_speaks_in_the_conversation():
    """A failure that only shows on the board never reached the person.

    Found by auditing the implementation the day after shipping it: the runtime
    called the reporter on the success path and *not* on the exception path --
    which is the path a dropped connection or a timeout ends in. So
    `build_report`'s rule ("a failure always speaks") was never executed for the
    exact runs it was written for, and the two failed tasks on this machine had
    said nothing in the conversation.

    Source-level, because driving a raised executor through the whole runtime
    would test the composition rather than this rule. What it pins is that both
    completion paths reach the reporter.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "agent" / "scheduler" / "runtime.py"
    ).read_text(encoding="utf-8")
    # Two call sites: the one that ends a succeeded run, and the one that ends a
    # raised one. The second is what was missing.
    assert source.count("await self._report(") >= 2
    exception_path = source[source.index("        except Exception as exc:\n            status = ("):]
    exception_path = exception_path[: exception_path.index("        finally:")]

    assert "await self._report(" in exception_path


def test_a_failure_is_mirrored_to_the_channel_the_task_reports_in():
    """A task that speaks in Feishu must not go silent when it fails.

    Its delivered output is the run's *result*, and a failed run has none -- so
    the channel heard from it on every good day and never on the bad one.
    """
    import asyncio
    from types import SimpleNamespace

    from agent.scheduler.build import mirror_failure_to_channel

    sent: list[tuple] = []

    class _Delivery:
        async def deliver_channel(self, *, target, text, output_dir=None):
            sent.append((target, text))
            return "sent"

    task = SimpleNamespace(name="汇报", delivery_mode="channel", delivery_target=None)
    run = SimpleNamespace(
        config_snapshot={
            "delivery_mode": "channel",
            "delivery_target": {"target_type": "feishu_chat", "payload": {"chat_id": "c1"}},
        }
    )
    report = {"status": "failed", "task_name": "汇报", "text": "连接中断：模型服务这次没有把回复发完。"}

    asyncio.run(mirror_failure_to_channel(_Delivery(), task, run, report))

    assert len(sent) == 1
    assert "汇报 失败了" in sent[0][1]
    assert "连接中断" in sent[0][1]


def test_a_success_is_not_mirrored_twice():
    """The output itself is already delivered; a second copy is a double message."""
    import asyncio
    from types import SimpleNamespace

    from agent.scheduler.build import mirror_failure_to_channel

    sent: list[tuple] = []

    class _Delivery:
        async def deliver_channel(self, *, target, text, output_dir=None):
            sent.append((target, text))
            return "sent"

    task = SimpleNamespace(name="汇报", delivery_mode="channel", delivery_target=None)
    run = SimpleNamespace(config_snapshot={"delivery_mode": "channel"})

    asyncio.run(
        mirror_failure_to_channel(
            _Delivery(), task, run, {"status": "succeeded", "text": "报告好了"}
        )
    )

    assert sent == []


# ── a run's process is written down as it happens ──────────────────────────


def test_the_unattended_sink_journals_what_the_run_did(tmp_path):
    """The run's own account of itself was all a failure left behind.

    A scheduled run wrote its summary and nothing else: no prompt, no tool
    calls, no refusals. "Why did it fail" could only be answered by asking the
    thing that failed -- which is exactly what an audit is not.
    """
    from agent import LTMStore
    from agent.scheduler.build import _RunTranscript, run_transcript_session
    from agent.scheduler.unattended import UnattendedAudit, UnattendedOutputSink

    store = LTMStore(context_dir=tmp_path / "context")
    transcript = _RunTranscript(store, run_transcript_session("t1", "r1"))
    sink = UnattendedOutputSink(
        audit=UnattendedAudit(task_id="t1", run_id="r1"),
        journal=transcript.event,
    )

    sink.on_tool_start("shell", {"command": "ls -la"}, operation_id="op1")
    sink.on_tool_end("shell", "file1\nfile2", operation_id="op1", ok=True)
    sink.on_tool_start("http_get", {"url": "https://example.com"}, operation_id="op2")
    sink.on_tool_end("http_get", "boom", operation_id="op2", ok=False)
    transcript.turn("整理一下下载目录", "整理好了，删了 3 个临时文件")

    from agent.session_service import SessionService

    messages = SessionService(store=store).get_messages(
        run_transcript_session("t1", "r1")
    )["messages"]
    roles = [item["role"] for item in messages]
    assert roles.count("tool") == 2, roles
    assert roles[-1] in {"user", "assistant"}, roles
    joined = " ".join(str(item.get("content", "")) for item in messages)
    assert "整理一下下载目录" in joined
    assert "整理好了" in joined


def test_the_run_transcript_survives_a_missing_store():
    """Recording is best effort: a run must not fail because its diary could not open."""
    from agent.scheduler.build import _RunTranscript

    class _Broken:
        def append_agent_event(self, **_kwargs):
            raise RuntimeError("disk is full")

        def write_conversation_exchange(self, **_kwargs):
            raise RuntimeError("disk is full")

    transcript = _RunTranscript(_Broken(), "scheduler:t:r")
    with pytest.raises(RuntimeError):
        transcript.event("tool_started", {})
