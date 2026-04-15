# Feishu Streaming And Files Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add real Feishu streaming responses and automatic file delivery for generated artifacts.

**Architecture:** Extend `FeishuOutputSink` so it can either stream via CardKit or fall back to the existing send-on-complete path, while also uploading and sending generated files through Feishu media APIs. Keep channel integration small by passing `output_dir` into sink creation and intercepting `/send <path>` commands before agent dispatch.

**Tech Stack:** Python, asyncio, `lark-oapi`, pytest, unittest.mock

---

### Task 1: Add failing sink tests for streaming and file delivery

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_feishu_sink_stream_chunk_schedules_streaming_when_enabled():
    ...

def test_feishu_sink_write_file_tool_end_schedules_file_send(tmp_path):
    ...

def test_feishu_sink_turn_complete_sends_new_output_dir_files(tmp_path):
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_feishu_channel.py -k 'stream or file or output_dir' -v`
Expected: FAIL because the sink does not yet stream via CardKit or send files.

- [ ] **Step 3: Write minimal implementation**

```python
class FeishuOutputSink(OutputSink):
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_feishu_channel.py -k 'stream or file or output_dir' -v`
Expected: PASS

### Task 2: Add failing channel tests for output_dir wiring and `/send`

**Files:**
- Modify: `tests/test_feishu_channel.py`
- Modify: `agent.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_channel_runner_wires_output_dir_to_feishu_channels(...):
    ...

def test_feishu_channel_send_command_uses_output_dir(tmp_path):
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_feishu_channel.py -k 'send_command or output_dir_to_feishu' -v`
Expected: FAIL because the channel does not yet resolve or send files from `/send`.

- [ ] **Step 3: Write minimal implementation**

```python
class FeishuChannel(Channel):
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_feishu_channel.py -k 'send_command or output_dir_to_feishu' -v`
Expected: PASS

### Task 3: Run the focused regression suite

**Files:**
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Run the focused test module**

Run: `pytest tests/test_feishu_channel.py -v`
Expected: PASS

- [ ] **Step 2: Run a channel-layer smoke check**

Run: `pytest tests/test_channel_layer.py -v`
Expected: PASS
