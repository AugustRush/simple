# Scheduler Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent scheduler that can execute structured scheduled tasks across restarts and deliver results either to task history/output files or stable Feishu chat targets.

**Architecture:** Create a dedicated `agent.scheduler` subsystem with focused model, store, runtime, and delivery modules. Drive implementation with TDD: lock trigger math and store semantics first, then add runtime dispatch and CLI commands, and finally wire channel delivery.

**Tech Stack:** Python, asyncio, sqlite3, Typer, Rich, pytest

---

### Task 1: Add scheduler trigger and persistence regression tests

**Files:**
- Create: `tests/test_scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing trigger tests**

```python
def test_daily_trigger_next_after_returns_next_local_wall_clock_time():
    ...

def test_weekly_trigger_rolls_forward_to_named_weekday():
    ...
```

- [ ] **Step 2: Run focused tests to verify they fail**

Run: `python -m pytest -q tests/test_scheduler.py -k 'trigger or store'`
Expected: FAIL because scheduler modules do not exist yet.

- [ ] **Step 3: Add failing store tests for create/list/claim/recover**

```python
def test_scheduler_store_claims_due_task_and_creates_run():
    ...

def test_scheduler_store_recovers_stale_run_and_requeues_task():
    ...
```

- [ ] **Step 4: Re-run the same focused tests**

Run: `python -m pytest -q tests/test_scheduler.py -k 'trigger or store'`
Expected: FAIL with missing imports or missing behavior.

### Task 2: Implement scheduler models and SQLite store

**Files:**
- Create: `agent/scheduler/__init__.py`
- Create: `agent/scheduler/models.py`
- Create: `agent/scheduler/store.py`
- Modify: `agent/shared.py`
- Modify: `agent/__init__.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Add shared scheduler paths/constants**

```python
SCHEDULER_DIR = AGENT_HOME / "tasks"
SCHEDULER_DB_FILE = SCHEDULER_DIR / "scheduler.db"
```

- [ ] **Step 2: Implement trigger dataclasses and serialization helpers**

```python
@dataclass
class ScheduleTask:
    ...
```

- [ ] **Step 3: Implement SQLite-backed task/run store**

```python
class SchedulerStore:
    def create_task(...): ...
    def list_tasks(...): ...
    def claim_due_tasks(...): ...
    def recover_stale_runs(...): ...
```

- [ ] **Step 4: Run focused scheduler tests**

Run: `python -m pytest -q tests/test_scheduler.py -k 'trigger or store'`
Expected: PASS

### Task 3: Add runtime dispatch and standalone execution

**Files:**
- Create: `agent/scheduler/runtime.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Add failing runtime tests**

```python
def test_scheduler_service_executes_due_agent_prompt_task_and_persists_run():
    ...

def test_scheduler_service_coalesces_missed_interval_runs():
    ...
```

- [ ] **Step 2: Run runtime-focused tests to verify they fail**

Run: `python -m pytest -q tests/test_scheduler.py -k 'runtime or service or execute'`
Expected: FAIL because runtime loop is not implemented.

- [ ] **Step 3: Implement scheduler service and execution helpers**

```python
class SchedulerService:
    async def run_forever(self): ...
    async def run_once(self): ...
```

- [ ] **Step 4: Implement standalone `agent_prompt` execution using fresh AgentContext**

```python
async def execute_agent_prompt(...): ...
```

- [ ] **Step 5: Re-run runtime-focused tests**

Run: `python -m pytest -q tests/test_scheduler.py -k 'runtime or service or execute'`
Expected: PASS

### Task 4: Add system jobs and Feishu channel delivery

**Files:**
- Create: `agent/scheduler/delivery.py`
- Modify: `channels/feishu.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Add failing tests for `memory_tidy` system job and Feishu delivery**

```python
def test_scheduler_service_executes_memory_tidy_system_job():
    ...

def test_scheduler_feishu_delivery_sends_to_stable_chat_target():
    ...
```

- [ ] **Step 2: Run focused tests to verify they fail**

Run: `python -m pytest -q tests/test_scheduler.py -k 'delivery or system_job'`
Expected: FAIL

- [ ] **Step 3: Implement standalone and Feishu delivery adapters**

```python
class SchedulerDelivery:
    async def deliver(...): ...
```

- [ ] **Step 4: Implement `memory_tidy` system job handler**

```python
async def execute_system_job(...): ...
```

- [ ] **Step 5: Re-run focused scheduler and Feishu tests**

Run: `python -m pytest -q tests/test_scheduler.py tests/test_feishu_channel.py -k 'scheduler or delivery or tidy'`
Expected: PASS

### Task 5: Add CLI schedule management and scheduler service command

**Files:**
- Modify: `agent/cli.py`
- Modify: `README.md`
- Modify: `config.example.json`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Add failing CLI tests for task creation and listing**

```python
def test_schedule_cli_creates_daily_task():
    ...

def test_schedule_cli_lists_persisted_tasks():
    ...
```

- [ ] **Step 2: Run the CLI-focused tests to verify they fail**

Run: `python -m pytest -q tests/test_scheduler.py -k 'cli or command'`
Expected: FAIL because the CLI commands do not exist.

- [ ] **Step 3: Add `schedule` Typer group and `scheduler` service command**

```python
schedule_app = typer.Typer(help="Scheduled task commands")
```

- [ ] **Step 4: Re-run the CLI-focused tests**

Run: `python -m pytest -q tests/test_scheduler.py -k 'cli or command'`
Expected: PASS

### Task 6: Run full verification and update docs

**Files:**
- Modify: `README.md`
- Modify: `config.example.json`
- Modify: any touched scheduler modules/tests

- [ ] **Step 1: Run all scheduler-adjacent tests**

Run: `python -m pytest -q tests/test_scheduler.py tests/test_feishu_channel.py tests/test_agent_integration.py`
Expected: PASS

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 3: Sanity-check the new CLI help output**

Run: `python -m agent --help`
Expected: Exit 0 with `schedule` and `scheduler` commands present.
