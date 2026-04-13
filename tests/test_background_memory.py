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
    ctx_mgr.min_messages = 1
    ctx_mgr.staging_turn_threshold = 99
    ctx_mgr.staging_token_threshold = 999999
    staging.append("user", "short turn")
    staging.append("assistant", "short reply")
    ctx_mgr.mark_activity()

    calls = []

    async def fake_process_one_job(client, model, api_format="anthropic", extractor=None):
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
