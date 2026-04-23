# Feishu Mode-Aware Orchestration Feedback Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Feishu multi-agent progress feedback from generic event text to mode-aware batch summaries for `parallel`, `pipeline`, and `rendezvous`.

**Architecture:** Keep orchestration runtime unchanged and add a Feishu-only formatting layer inside `FeishuOutputSink`. Batch-level events (`batch_started`, `batch_finished`) will prefer `event.metrics` over free-form `event.message`, while single-agent events remain compact. Unknown or legacy events will safely fall back to the current generic text path.

**Tech Stack:** Python, asyncio, pytest, unittest.mock, `lark-oapi`

---

### Task 1: Add failing Feishu sink tests for mode-aware batch rendering

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_feishu_sink_parallel_batch_started_uses_mode_aware_summary():
    ...

def test_feishu_sink_parallel_batch_finished_shows_detailed_metrics():
    ...

def test_feishu_sink_pipeline_batch_finished_shows_stage_count():
    ...

def test_feishu_sink_pipeline_batch_finished_marks_early_stop():
    ...

def test_feishu_sink_rendezvous_batch_finished_shows_rounds():
    ...

def test_feishu_sink_batch_events_fall_back_without_metrics():
    ...
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest -q tests/test_feishu_channel.py -k 'parallel_batch or pipeline_batch or rendezvous_batch or fall_back_without_metrics'`
Expected: FAIL because Feishu currently treats batch events as generic text and does not render mode-aware summaries from `event.metrics`.

- [ ] **Step 3: Keep assertions focused on user-visible behavior**

Use assertions against `_progress_buf.text` or the formatter output so the tests lock the Feishu-facing strings, not internal helper names.

### Task 2: Implement Feishu-only batch event formatters

**Files:**
- Modify: `channels/feishu.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Add a mode-aware formatter entry point**

Implement a private formatter path in `FeishuOutputSink`, for example:

```python
def _format_mode_aware_subagent_event(self, event: SubAgentProgressEvent) -> str:
    ...
```

This function should:

- special-case `batch_started` and `batch_finished`
- prefer structured `event.metrics`
- fall back to the existing generic formatter when metrics are missing or unusable

- [ ] **Step 2: Implement `parallel` batch summaries**

Render:

- `batch_started`: mode + `spec_count` + `max_parallel_agents`
- `batch_finished`: `completed/spec_count`, `duration_seconds`, and `write_scope_check_seconds` when available

Keep successful `parallel` batches detailed by default.

- [ ] **Step 3: Implement `pipeline` batch summaries**

Render:

- `batch_started`: dependency-driven wording
- `batch_finished` success: `completed/spec_count`, `stage_count`, `duration_seconds`
- `batch_finished` early stop: “ended early” wording whenever `completed < spec_count`

Do not infer root cause text beyond the observable early-stop state.

- [ ] **Step 4: Implement `rendezvous` batch summaries**

Render:

- `batch_started`: mode + subtask count + max rounds when available
- `batch_finished`: subtask count, `rounds_completed`, `duration_seconds`

Use singular/plural wording that stays readable for `1 round` vs `2 rounds`.

- [ ] **Step 5: Preserve current handling for non-batch events**

Keep `agent_started`, `agent_finished`, and `agent_failed` on the existing compact rendering path so the progress card remains scannable.

- [ ] **Step 6: Run the targeted tests to verify they pass**

Run: `uv run pytest -q tests/test_feishu_channel.py -k 'parallel_batch or pipeline_batch or rendezvous_batch or fall_back_without_metrics'`
Expected: PASS

### Task 3: Run focused regressions around sink behavior

**Files:**
- Test: `tests/test_feishu_channel.py`
- Test: `tests/test_channel_layer.py`

- [ ] **Step 1: Run the full Feishu sink test module**

Run: `uv run pytest -q tests/test_feishu_channel.py`
Expected: PASS

- [ ] **Step 2: Run channel-layer regressions**

Run: `uv run pytest -q tests/test_channel_layer.py`
Expected: PASS

### Task 4: Run full verification

**Files:**
- Test: `tests/test_feishu_channel.py`
- Test: `tests/test_channel_layer.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 2: Summarize the user-visible change**

Capture the new Feishu behavior in terms of:

- how `parallel` now shows detailed successful batch metrics
- how `pipeline` now distinguishes successful completion from early termination
- how `rendezvous` now surfaces rounds explicitly
- how legacy events still degrade safely
