"""Tests for background memory queue/worker APIs."""

import asyncio
import threading
import time


def _endpoint(api_format: str = "openai"):
    """A stand-in endpoint for tests whose consolidation never calls a model.

    The worker's contract is to hand its endpoint to the context manager
    untouched, so these tests only need the wire format to travel with the
    client — the client itself is never used.
    """
    from agent import ModelEndpoint

    return ModelEndpoint(client=None, api_format=api_format)


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

    for i in range(ctx_mgr.staging_turn_threshold):
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
            endpoint=_endpoint(),
            model="x",
            extractor=lambda *_: [],
        )

    asyncio.run(run_once())

    assert ctx_mgr.staging.count() == 0


def test_background_worker_stop_cancels_active_job_cleanly(capsys):
    from agent import BackgroundMemoryWorker

    started = threading.Event()
    cancelled = threading.Event()

    class _BlockingContextManager:
        def should_process_jobs(self):
            return True

        async def process_one_job(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker = BackgroundMemoryWorker(
        _BlockingContextManager(),
        _endpoint(),
        "x",
        poll_seconds=10,
    )
    worker.start()
    assert started.wait(timeout=1)

    started_at = time.monotonic()
    worker.stop()
    asyncio.run(worker.wait())

    assert time.monotonic() - started_at < 1
    assert cancelled.is_set()
    captured = capsys.readouterr()
    assert "Exception in thread" not in captured.err
    assert "Background consolidation error" not in captured.out


def test_process_one_job_materializes_resolved_fact_from_identity_entry(tmp_path):
    """Consolidated identity prose becomes a fact, so a restart still has it.

    It becomes ``identity_note`` and not ``name``: reading a name out of a
    paragraph is a judgement about what a sentence means, and only a deliberate
    ``set_identity`` call states that.
    """
    from agent import LTMEntry

    ctx_mgr, staging = _build_context_manager(tmp_path)

    staging.append("user", "以后你叫阿福。")
    staging.append("assistant", "好，从现在开始我叫阿福。")
    ctx_mgr.enqueue_consolidation("staged_turns")

    async def run_once():
        await ctx_mgr.process_one_job(
            endpoint=_endpoint(),
            model="x",
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

    facts = ctx_mgr.store.read_resolved_facts(
        subject="assistant", predicate="identity_note"
    )

    assert [fact.value for fact in facts] == ["助手的名字是阿福"]


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
            endpoint=_endpoint(),
            model="x",
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
        endpoint, model, extractor=None
    ):
        calls.append((model, endpoint.api_format))
        ctx_mgr.staging.clear_all()
        ctx_mgr._needs_consolidation = False
        return True

    ctx_mgr.process_one_job = fake_process_one_job

    async def run():
        worker = BackgroundMemoryWorker(
            ctx_mgr=ctx_mgr,
            endpoint=_endpoint(),
            model="x",
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
        endpoint=_endpoint(),
        model="x",
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
            endpoint,
            model,
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
        endpoint=_endpoint(),
        model="x",
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


def test_background_worker_pool_uses_one_thread_for_multiple_sessions():
    from agent import BackgroundMemoryWorkerPool

    class _FakeContextManager:
        def __init__(self):
            self.pending = 1
            self.processed = 0

        def should_process_jobs(self):
            return self.pending > 0

        async def process_one_job(self, *_args, **_kwargs):
            if self.pending <= 0:
                return False
            self.pending -= 1
            self.processed += 1
            return True

    first = _FakeContextManager()
    second = _FakeContextManager()
    pool = BackgroundMemoryWorkerPool(poll_seconds=0.01)
    pool.register("a", first, "model-a", _endpoint())
    pool.register("b", second, "model-b", _endpoint())

    async def run():
        pool.start()
        await asyncio.sleep(0.05)
        pool.stop()
        await pool.wait()

    asyncio.run(run())

    assert first.processed == 1
    assert second.processed == 1
    assert pool._thread is not None


def test_process_one_job_logs_reason_and_session_context(tmp_path, capsys):
    ctx_mgr, staging = _build_context_manager(tmp_path)
    staging.append("user", "hello")
    staging.append("assistant", "world")
    ctx_mgr.enqueue_consolidation("staged_turns")

    async def run_once():
        await ctx_mgr.process_one_job(
            endpoint=_endpoint(),
            model="x",
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
            endpoint=_endpoint(),
            model="x",
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
        endpoint,
        model,
        keep_last=None,
        staging=None,
    ):
        raise RuntimeError("transient failure")

    memory_system.ConsolidationEngine.consolidate = failing_consolidate
    try:
        async def run_once():
            return await ctx_mgr.process_one_job(
                endpoint=_endpoint(),
                model="x",
            )

        result = asyncio.run(run_once())
    finally:
        memory_system.ConsolidationEngine.consolidate = original_consolidate

    assert result is False
    assert ctx_mgr._needs_consolidation is True
    assert staging.count() == 2


def test_background_worker_pool_slow_session_does_not_starve_others():
    """Consolidation jobs run concurrently across sessions.

    A slow session's LLM call (tens of seconds in production) must not
    block another session's already-queued job from completing in the
    same poll cycle.
    """
    from agent import BackgroundMemoryWorkerPool

    class _FakeContextManager:
        def __init__(self, delay: float):
            self.delay = delay
            self.pending = 1
            self.processed = 0
            self.started_at: list[float] = []

        def should_process_jobs(self):
            return self.pending > 0

        async def process_one_job(self, *_args, **_kwargs):
            if self.pending <= 0:
                return False
            self.pending -= 1
            self.started_at.append(time.monotonic())
            await asyncio.sleep(self.delay)
            self.processed += 1
            return True

    slow = _FakeContextManager(delay=0.3)
    fast = _FakeContextManager(delay=0.0)
    pool = BackgroundMemoryWorkerPool(poll_seconds=0.01)
    pool.register("slow", slow, "model-a", _endpoint())
    pool.register("fast", fast, "model-b", _endpoint())

    async def run():
        pool.start()
        # Both jobs start in the first cycle; fast finishes long before
        # slow does, proving they were not serialized.
        await asyncio.sleep(0.1)
        fast_done_early = fast.processed == 1
        pool.stop()
        await pool.wait()
        return fast_done_early

    fast_done_early = asyncio.run(run())

    assert slow.processed == 1
    assert fast.processed == 1
    assert fast_done_early, "fast session was serialized behind the slow one"


def test_pool_wake_is_scoped_to_the_woken_session():
    """One session's wake must not bypass another session's idle gate."""
    from agent import BackgroundMemoryWorkerPool

    class _FakeContextManager:
        def __init__(self):
            self.pending = 1
            self.processed = 0

        def should_process_jobs(self):
            # Mid-conversation: the idle gate holds unless this is the session
            # that was explicitly woken.
            return False

        async def process_one_job(self, *_args, **_kwargs):
            if self.pending <= 0:
                return False
            self.pending -= 1
            self.processed += 1
            return True

    woken = _FakeContextManager()
    gated = _FakeContextManager()
    pool = BackgroundMemoryWorkerPool(poll_seconds=0.01)
    pool.register("woken", woken, "model", _endpoint())
    pool.register("gated", gated, "model", _endpoint())

    async def run():
        pool.start()
        pool.wake("woken")
        await asyncio.sleep(0.1)
        pool.stop()
        await pool.wait()

    asyncio.run(run())

    assert woken.processed == 1
    assert gated.processed == 0, "another session's idle gate was bypassed"


def test_pooled_handle_wait_waits_out_inflight_job():
    """Eviction unregisters, then closes the staging buffer.

    Handle.wait() must wait for the session's in-flight consolidation rather
    than returning immediately, or the buffer is closed under a running job.
    """
    from agent import BackgroundMemoryWorkerPool, PooledMemoryWorkerHandle

    started = threading.Event()
    release = threading.Event()

    class _SlowContextManager:
        def __init__(self):
            self.pending = 1

        def should_process_jobs(self):
            return self.pending > 0

        async def process_one_job(self, *_args, **_kwargs):
            if self.pending <= 0:
                return False
            self.pending -= 1
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return True

    pool = BackgroundMemoryWorkerPool(poll_seconds=0.01)
    pool.register("s", _SlowContextManager(), "model", _endpoint())
    handle = PooledMemoryWorkerHandle(pool, "s")

    async def run():
        pool.start()
        assert started.wait(timeout=1)
        handle.stop()  # Eviction order: unregister first.
        waiter = asyncio.ensure_future(handle.wait())
        await asyncio.sleep(0.05)
        assert not waiter.done(), "wait() returned while the job was running"
        release.set()
        await asyncio.wait_for(waiter, timeout=1)
        pool.stop()
        await pool.wait()

    asyncio.run(run())
