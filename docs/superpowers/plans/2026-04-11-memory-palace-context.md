# Memory Palace Context Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild memory consolidation from a foreground message compressor into an event-driven background memory system with typed upserts, fixed palace loci, and routed retrieval.

**Architecture:** The interactive loop should only append staged turns and enqueue memory work. A background worker consolidates session events into episodic, semantic, task, and procedural memory items stored in SQLite. Retrieval reads from structured memory with locus-aware routing, while markdown remains a projection layer.

**Tech Stack:** Python 3.11, `sqlite3`, `asyncio`, existing Rich/Typer CLI, pytest, current single-file `agent.py`.

---

## File Structure

- Modify: `agent.py`
- Modify: `README.md`
- Modify: `tests/test_staging.py`
- Modify: `tests/test_consolidation.py`
- Modify: `tests/test_ltm_store.py`
- Create: `tests/test_background_memory.py`

### Task 1: Model the new background memory pipeline in tests

**Files:**
- Create: `/Users/shike/Desktop/simple/tests/test_background_memory.py`
- Modify: `/Users/shike/Desktop/simple/tests/test_ltm_store.py`

- [ ] **Step 1: Write failing tests for typed upsert behavior**

```python
def test_add_entry_upserts_identity_preference(tmp_path):
    from agent import LTMStore, LTMEntry
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    first = LTMEntry(
        id="pref-a",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=0.8,
        status="active",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )
    second = LTMEntry(
        id="pref-b",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=0.9,
        status="active",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )

    store.add_entry(first)
    store.add_entry(second)

    entries = store.read_entries("identity")
    assert len([e for e in entries if e.content == "Prefers concise responses"]) == 1
    assert entries[0].importance == 0.9
```

- [ ] **Step 2: Write failing tests for task-state upsert behavior**

```python
def test_add_entry_upserts_task_status(tmp_path):
    from agent import LTMStore, LTMEntry
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    open_task = LTMEntry(
        id="task-open",
        category="tasks",
        entity="fix_auth_bug",
        memory_type="task",
        content="Fix the auth bug",
        importance=0.9,
        status="open",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )
    done_task = LTMEntry(
        id="task-done",
        category="tasks",
        entity="fix_auth_bug",
        memory_type="task",
        content="Fix the auth bug",
        importance=0.9,
        status="done",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )

    store.add_entry(open_task)
    store.add_entry(done_task)

    entries = store.read_entries("tasks")
    assert len([e for e in entries if e.entity == "fix_auth_bug"]) == 1
    assert entries[0].status == "done"
```

- [ ] **Step 3: Run the new tests to verify failure**

Run: `uv run pytest -q tests/test_background_memory.py tests/test_ltm_store.py::test_add_entry_upserts_identity_preference tests/test_ltm_store.py::test_add_entry_upserts_task_status`

Expected: FAIL because current store append semantics do not upsert.

- [ ] **Step 4: Implement minimal stable-key upsert logic**

Modify `/Users/shike/Desktop/simple/agent.py`:
- add a stable identity key / merge key for memory records
- make `identity`, `tasks`, `projects`, and `procedures` upsert by semantic key
- keep `episodes` append-only

- [ ] **Step 5: Re-run the same tests and verify pass**

Run: `uv run pytest -q tests/test_background_memory.py tests/test_ltm_store.py::test_add_entry_upserts_identity_preference tests/test_ltm_store.py::test_add_entry_upserts_task_status`

Expected: PASS

### Task 2: Replace foreground consolidation with queued background work

**Files:**
- Create: `/Users/shike/Desktop/simple/tests/test_background_memory.py`
- Modify: `/Users/shike/Desktop/simple/agent.py`

- [ ] **Step 1: Write a failing test for background job enqueue on user turn**

```python
def test_mark_activity_enqueues_memory_work(tmp_path):
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager, StagingBuffer
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )

    ctx_mgr.mark_activity()
    staging.append("user", "hello")

    job = ctx_mgr.next_job()
    assert job is not None
    assert job["reason"] in {"staged_turns", "high_value", "session_end", "idle"}
```

- [ ] **Step 2: Write a failing test for staged-volume trigger instead of working-memory token trigger**

```python
def test_should_enqueue_uses_staging_volume(tmp_path):
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager, StagingBuffer
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
        min_messages=4,
    )

    for i in range(6):
        staging.append("user", f"turn {i}")
    ctx_mgr.mark_activity()

    assert ctx_mgr.should_enqueue_consolidation() is True
```

- [ ] **Step 3: Run the new tests to verify failure**

Run: `uv run pytest -q tests/test_background_memory.py`

Expected: FAIL because the current context manager has no explicit queue or staged-volume trigger.

- [ ] **Step 4: Implement a background memory queue and worker**

Modify `/Users/shike/Desktop/simple/agent.py`:
- add a lightweight in-process memory job queue
- expose `enqueue_consolidation`, `next_job`, and `should_enqueue_consolidation`
- change the interactive loop so normal turns enqueue jobs instead of awaiting `sleep()`
- keep session-end flush as a fallback

- [ ] **Step 5: Re-run the same tests and verify pass**

Run: `uv run pytest -q tests/test_background_memory.py`

Expected: PASS

### Task 3: Make idle/background processing real instead of nominal

**Files:**
- Modify: `/Users/shike/Desktop/simple/agent.py`
- Modify: `/Users/shike/Desktop/simple/tests/test_staging.py`

- [ ] **Step 1: Write a failing test that the background worker consumes queued jobs**

```python
def test_background_worker_processes_queued_consolidation(tmp_path):
    import asyncio
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager, StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    staging = StagingBuffer(path=tmp_path / "staging.jsonl", session_id="session-a")
    staging.append("user", "We decided to prefer concise responses.")
    staging.append("assistant", "Noted.")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )
    ctx_mgr.enqueue_consolidation("staged_turns")

    async def run_once():
        await ctx_mgr.process_one_job(client=None, model="x", api_format="openai", extractor=lambda *_: [])

    asyncio.run(run_once())
    assert ctx_mgr.staging.count() == 0
```

- [ ] **Step 2: Run the test to verify failure**

Run: `uv run pytest -q tests/test_staging.py::test_background_worker_processes_queued_consolidation`

Expected: FAIL because there is no explicit worker API yet.

- [ ] **Step 3: Implement a single-step worker API and loop integration**

Modify `/Users/shike/Desktop/simple/agent.py`:
- add `process_one_job(...)`
- make the idle loop process queued jobs instead of calling `should_idle_sleep(ctx.messages)`
- ensure the worker always prefers staged turns over `ctx.messages`

- [ ] **Step 4: Re-run the targeted test**

Run: `uv run pytest -q tests/test_staging.py::test_background_worker_processes_queued_consolidation`

Expected: PASS

### Task 4: Replace global decay with locus-aware retention rules

**Files:**
- Modify: `/Users/shike/Desktop/simple/agent.py`
- Modify: `/Users/shike/Desktop/simple/tests/test_ltm_store.py`

- [ ] **Step 1: Write failing tests for differentiated retention**

```python
def test_identity_memories_do_not_decay_like_episodes(tmp_path):
    from agent import LTMStore, LTMEntry
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    identity = LTMEntry(
        id="pref-1",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=1.0,
        status="active",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )
    episode = LTMEntry(
        id="ep-1",
        category="episodes",
        entity="session-1",
        memory_type="session_summary",
        content="We talked about preferences",
        importance=1.0,
        status="active",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )
    store.add_entry(identity)
    store.add_entry(episode)

    store.apply_retention()

    entries = store.all_entries()
    assert any(e.category == "identity" and e.importance == 1.0 for e in entries)
    assert any(e.category == "episodes" and e.importance < 1.0 for e in entries)
```

- [ ] **Step 2: Run the test to verify failure**

Run: `uv run pytest -q tests/test_ltm_store.py::test_identity_memories_do_not_decay_like_episodes`

Expected: FAIL because the current code only has uniform decay.

- [ ] **Step 3: Implement locus-aware retention**

Modify `/Users/shike/Desktop/simple/agent.py`:
- replace `apply_decay()` in the active path with `apply_retention()`
- keep conservative retention for `identity`, `projects`, `procedures`
- allow faster decay or archival for `episodes`
- make `tasks` state-driven instead of decay-driven

- [ ] **Step 4: Re-run the targeted test**

Run: `uv run pytest -q tests/test_ltm_store.py::test_identity_memories_do_not_decay_like_episodes`

Expected: PASS

### Task 5: Remove markdown-driven tidy from the active architecture

**Files:**
- Modify: `/Users/shike/Desktop/simple/agent.py`
- Modify: `/Users/shike/Desktop/simple/README.md`

- [ ] **Step 1: Write a failing test for maintenance reading from SQLite-backed truth**

```python
def test_memory_maintenance_uses_store_truth_not_projection(tmp_path):
    from agent import LTMStore
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    # Test should assert maintenance input is derived from active rows, not markdown files.
```

- [ ] **Step 2: Run the test to verify failure**

Run: `uv run pytest -q tests/test_background_memory.py::test_memory_maintenance_uses_store_truth_not_projection`

Expected: FAIL because maintenance/tidy currently reads markdown files.

- [ ] **Step 3: Replace `MemoryPalace.tidy()` with SQLite-backed maintenance**

Modify `/Users/shike/Desktop/simple/agent.py`:
- stop using markdown files as maintenance input
- either deprecate or rewrite `tidy()` to consume active store rows
- update README to document that SQLite is the authoritative source

- [ ] **Step 4: Re-run the targeted test**

Run: `uv run pytest -q tests/test_background_memory.py::test_memory_maintenance_uses_store_truth_not_projection`

Expected: PASS

### Task 6: Full verification

**Files:**
- Modify: `/Users/shike/Desktop/simple/agent.py`
- Modify: `/Users/shike/Desktop/simple/tests/*.py`
- Modify: `/Users/shike/Desktop/simple/README.md`

- [ ] **Step 1: Run the focused suite**

Run: `uv run pytest -q tests/test_background_memory.py tests/test_staging.py tests/test_consolidation.py tests/test_ltm_store.py`

Expected: PASS

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -q`

Expected: PASS

- [ ] **Step 3: Run CLI smoke checks**

Run:

```bash
uv run simple --help
simple --help
```

Expected: both commands exit 0.

- [ ] **Step 4: Review the diff for accidental regressions**

Run: `git diff --stat`

Expected: changes limited to memory architecture, docs, and tests.
