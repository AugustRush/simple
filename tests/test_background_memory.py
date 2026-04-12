"""Tests for background memory queue/worker APIs."""

import asyncio
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


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
