"""Tests for background memory queue/worker APIs."""

import asyncio
import threading
import time


def _build_context_manager(tmp_path):
    from agent import (
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        LTMStore,
        StagingBuffer,
    )

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )
    return ctx_mgr, staging


def test_mark_activity_enqueues_memory_work(tmp_path):
    ctx_mgr, staging = _build_context_manager(tmp_path)

    ctx_mgr.mark_activity()
    staging.append("user", "Hello background worker")
    job = ctx_mgr.next_job()

    assert job is not None
    assert job["reason"] in {
        "staged_turns",
        "high_value",
        "session_end",
        "idle",
    }


def test_should_enqueue_uses_staging_volume(tmp_path):
    ctx_mgr, staging = _build_context_manager(tmp_path)

    for i in range(6):
        staging.append("user", f"turn {i}")

    ctx_mgr.mark_activity()

    assert ctx_mgr.should_enqueue_consolidation()


def test_background_worker_processes_queued_consolidation(tmp_path):
    ctx_mgr, staging = _build_context_manager(tmp_path)

    staging.append("user", "We decided to prefer concise responses.")
    staging.append("assistant", "Noted.")
    ctx_mgr.enqueue_consolidation("staged_turns")

    async def run_once():
        await ctx_mgr.process_one_job(
            client=None,
            model="x",
            api_format="openai",
            extractor=lambda *_: [],
        )

    asyncio.run(run_once())

    assert ctx_mgr.staging.count() == 0


def test_process_one_job_materializes_resolved_fact_from_identity_entry(tmp_path):
    from agent import LTMEntry

    ctx_mgr, staging = _build_context_manager(tmp_path)

    staging.append("user", "以后你叫阿福。")
    staging.append("assistant", "好，从现在开始我叫阿福。")
    ctx_mgr.enqueue_consolidation("staged_turns")

    async def run_once():
        await ctx_mgr.process_one_job(
            client=None,
            model="x",
            api_format="openai",
            extractor=lambda *_: [
                LTMEntry(
                    id="assistant-identity",
                    category="identity",
                    entity="assistant",
                    memory_type="self_identity",
                    content="助手的名字是阿福",
                    importance=0.9,
                    created_at="2026-04-25",
                    updated_at="2026-04-25",
                )
            ],
        )

    asyncio.run(run_once())

    facts = ctx_mgr.store.read_resolved_facts(subject="assistant", predicate="name")

    assert [fact.value for fact in facts] == ["阿福"]


def test_process_one_job_preserves_turns_appended_during_consolidation(tmp_path):
    ctx_mgr, staging = _build_context_manager(tmp_path)

    staging.append("user", "older turn")
    staging.append("assistant", "older reply")
    ctx_mgr.enqueue_consolidation("staged_turns")

    started = threading.Event()
    release = threading.Event()

    def extractor(staged, job):
        started.set()
        assert [msg["content"] for msg in staged] == ["older turn", "older reply"]
        assert release.wait(timeout=1)
        return []

    async def run_once():
        await ctx_mgr.process_one_job(
            client=None,
            model="x",
            api_format="openai",
            extractor=extractor,
        )

    worker = threading.Thread(target=lambda: asyncio.run(run_once()))
    worker.start()
    assert started.wait(timeout=1)

    staging.append("user", "new turn should survive")
    release.set()
    worker.join(timeout=1)

    assert [msg["content"] for msg in staging.read_all()] == ["new turn should survive"]


def test_background_worker_processes_idle_staging_without_prequeued_job(tmp_path):
    from agent import BackgroundMemoryWorker

    ctx_mgr, staging = _build_context_manager(tmp_path)
    ctx_mgr.idle_seconds = 0
    # Keep staging_token_threshold high to prevent the slow-path explicit enqueue
    # from firing, ensuring no job is pre-queued in _jobs.
    ctx_mgr.staging_token_threshold = 999999
    # Append exactly staging_turn_threshold entries so has_staged_work fires via
    # the background idle path without an explicit enqueue.
    for i in range(ctx_mgr.staging_turn_threshold):
        staging.append("user", f"turn {i}")
    ctx_mgr.mark_activity()

    calls = []

    async def fake_process_one_job(
        client, model, api_format="anthropic", extractor=None
    ):
        calls.append((model, api_format))
        ctx_mgr.staging.clear_all()
        ctx_mgr._needs_consolidation = False
        return True

    ctx_mgr.process_one_job = fake_process_one_job

    async def run():
        worker = BackgroundMemoryWorker(
            ctx_mgr=ctx_mgr,
            client=None,
            model="x",
            api_format="openai",
            poll_seconds=0.01,
        )
        worker.start()
        time.sleep(0.05)
        worker.stop()
        await worker.wait()

    asyncio.run(run())

    assert calls == [("x", "openai")]


def test_background_worker_polls_while_main_thread_is_blocked(tmp_path):
    from agent import BackgroundMemoryWorker

    class _FakeContextManager:
        def __init__(self):
            self.polls = 0

        def should_process_jobs(self):
            self.polls += 1
            return False

    ctx_mgr = _FakeContextManager()
    worker = BackgroundMemoryWorker(
        ctx_mgr=ctx_mgr,
        client=None,
        model="x",
        api_format="openai",
        poll_seconds=0.01,
    )

    async def run():
        worker.start()
        time.sleep(0.05)
        worker.stop()
        await worker.wait()

    asyncio.run(run())

    assert ctx_mgr.polls > 0


def test_background_worker_wake_drains_all_pending_jobs():
    from agent import BackgroundMemoryWorker

    class _FakeContextManager:
        def __init__(self):
            self.pending = 3
            self.processed = 0

        def pending_jobs(self):
            return self.pending

        def should_process_jobs(self):
            return False

        async def process_one_job(
            self,
            client,
            model,
            api_format="anthropic",
            extractor=None,
        ):
            if self.pending <= 0:
                return False
            self.pending -= 1
            self.processed += 1
            return True

    ctx_mgr = _FakeContextManager()
    worker = BackgroundMemoryWorker(
        ctx_mgr=ctx_mgr,
        client=None,
        model="x",
        api_format="openai",
        poll_seconds=0.01,
    )

    async def run():
        worker.start()
        worker.wake()
        time.sleep(0.05)
        worker.stop()
        await worker.wait()

    asyncio.run(run())

    assert ctx_mgr.processed == 3


def test_process_one_job_logs_reason_and_session_context(tmp_path, capsys):
    ctx_mgr, staging = _build_context_manager(tmp_path)
    staging.append("user", "hello")
    staging.append("assistant", "world")
    ctx_mgr.enqueue_consolidation("staged_turns")

    async def run_once():
        await ctx_mgr.process_one_job(
            client=None,
            model="x",
            api_format="openai",
            extractor=lambda *_: [],
        )

    asyncio.run(run_once())
    out = capsys.readouterr().out

    assert "staged_turns" in out
    assert staging.session_id in out


def test_process_one_job_reconstructs_sqlite_staging_from_job_metadata(tmp_path):
    from agent import (
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        LTMStore,
        StagingBuffer,
    )

    context_dir = tmp_path / "context"
    store = LTMStore(context_dir=context_dir, memory_dir=tmp_path / "memory")
    primary = StagingBuffer(context_dir=context_dir, session_id="primary")
    other = StagingBuffer(context_dir=context_dir, session_id="other")
    other.append("user", "other session turn")
    other.append("assistant", "other session reply")

    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=primary,
    )
    ctx_mgr.enqueue_staging_job("test", other)

    seen = {}

    def extractor(staged, job):
        seen["contents"] = [msg["content"] for msg in staged]
        seen["job"] = dict(job)
        return []

    async def run_once():
        return await ctx_mgr.process_one_job(
            client=None,
            model="x",
            api_format="openai",
            extractor=extractor,
        )

    assert asyncio.run(run_once()) is True
    assert seen["contents"] == ["other session turn", "other session reply"]
    assert other.count() == 0


def test_process_one_job_keeps_retry_signal_when_consolidation_fails(tmp_path):
    import agent.memory.system as memory_system

    ctx_mgr, staging = _build_context_manager(tmp_path)
    staging.append("user", "remember this")
    staging.append("assistant", "ack")
    ctx_mgr.mark_activity()
    ctx_mgr.enqueue_consolidation("staged_turns")

    original_consolidate = memory_system.ConsolidationEngine.consolidate

    async def failing_consolidate(
        self,
        messages,
        client,
        model,
        api_format="anthropic",
        keep_last=None,
        staging=None,
    ):
        raise RuntimeError("transient failure")

    memory_system.ConsolidationEngine.consolidate = failing_consolidate
    try:
        async def run_once():
            return await ctx_mgr.process_one_job(
                client=None,
                model="x",
                api_format="openai",
            )

        result = asyncio.run(run_once())
    finally:
        memory_system.ConsolidationEngine.consolidate = original_consolidate

    assert result is False
    assert ctx_mgr._needs_consolidation is True
    assert staging.count() == 2
