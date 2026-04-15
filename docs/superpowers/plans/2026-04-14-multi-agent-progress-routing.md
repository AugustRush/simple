# Multi-Agent Progress Routing Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sub-agent progress follow the active conversation sink so Feishu users see live multi-agent progress and final answers in separate surfaces.

**Architecture:** Introduce structured sub-agent progress events in the agent core, route them through `OutputSink`, and let Feishu render them into a dedicated process streaming card while CLI renders concise status lines. Keep the parent answer flow unchanged except for finalizing the process card before the final response.

**Tech Stack:** Python, asyncio, pytest, unittest.mock, `lark-oapi`

---

### Task 1: Add failing tests for structured sub-agent progress routing

**Files:**
- Modify: `tests/test_agent_integration.py`
- Modify: `tests/test_channel_layer.py`
- Test: `tests/test_agent_integration.py`
- Test: `tests/test_channel_layer.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_spawn_agent_reports_events_to_active_sink(monkeypatch):
    ...

def test_output_sink_accepts_subagent_event():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent_integration.py -k 'spawn_agent_reports_events' -v`
Run: `pytest tests/test_channel_layer.py -k 'subagent_event' -v`
Expected: FAIL because the output sink does not yet receive structured sub-agent events.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass
class SubAgentProgressEvent:
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent_integration.py -k 'spawn_agent_reports_events' -v`
Run: `pytest tests/test_channel_layer.py -k 'subagent_event' -v`
Expected: PASS

### Task 2: Add failing tests for Feishu process-card behavior

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_feishu_sink_subagent_event_schedules_process_card_update():
    ...

def test_feishu_sink_turn_complete_finalizes_process_card_before_final_answer():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_feishu_channel.py -k 'subagent_event or process_card' -v`
Expected: FAIL because Feishu does not yet maintain a separate progress card.

- [ ] **Step 3: Write minimal implementation**

```python
class FeishuOutputSink(OutputSink):
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_feishu_channel.py -k 'subagent_event or process_card' -v`
Expected: PASS

### Task 3: Run the focused regression suite

**Files:**
- Test: `tests/test_agent_integration.py`
- Test: `tests/test_feishu_channel.py`
- Test: `tests/test_channel_layer.py`

- [ ] **Step 1: Run targeted integration tests**

Run: `pytest tests/test_agent_integration.py -k 'spawn_agent' -v`
Expected: PASS

- [ ] **Step 2: Run Feishu and channel-layer tests**

Run: `pytest tests/test_feishu_channel.py -v`
Run: `pytest tests/test_channel_layer.py -v`
Expected: PASS
