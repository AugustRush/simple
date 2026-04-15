# Feishu Channel: CardKit Streaming + File Sending

## Problem

The current Feishu channel implementation has two gaps:

1. **No streaming output** — Chunks are accumulated silently; the user sees nothing until the full response is ready. For long responses this feels unresponsive.
2. **No file sending** — When the agent creates files (via `write_file` tool or output_dir), they are never sent to Feishu. Users must manually retrieve them.

## Reference

`/Users/shike/Desktop/nanobot/nanobot/channels/feishu.py` — A production Feishu channel that implements both features using the CardKit streaming API and `im.v1.image.create` / `im.v1.file.create` upload endpoints.

## Design

### 1. CardKit Streaming

#### Data structure

```python
@dataclass
class _FeishuStreamBuf:
    text: str = ""            # accumulated full text
    card_id: str | None = None  # CardKit card ID
    sequence: int = 0         # monotonic sequence for updates
    last_edit: float = 0.0    # time.monotonic() of last card update
```

One `_stream_buf` instance per `FeishuOutputSink` (one sink per incoming message).

#### New sync methods on FeishuOutputSink

- `_create_streaming_card_sync() -> str|None` — Create a CardKit card with `streaming_mode: True` containing a single empty markdown element (element_id: `"streaming_md"`). Send it to the chat. Return `card_id`.
- `_stream_update_text_sync(card_id, content, sequence) -> bool` — Update the markdown element content on the card via `cardkit.v1.card_element.content`.
- `_close_streaming_mode_sync(card_id, sequence) -> bool` — Set `streaming_mode: False` via `cardkit.v1.card.settings` to finalize the card.

#### Modified OutputSink methods

**`on_stream_chunk(chunk)`**:
1. Append `chunk` to `_stream_buf.text`.
2. If `_stream_buf.card_id` is None: create streaming card via executor. If creation fails, leave `card_id` as None (fallback mode).
3. If card exists and `>= 0.5s` since last edit: update card content via executor.

**`on_turn_complete(full_text, tool_calls)`**:
1. Determine final text = `full_text or "".join(self._chunks)`.
2. If `_stream_buf.card_id` exists:
   - Final update with complete text.
   - Close streaming mode.
   - Clear `_stream_buf`.
3. If `_stream_buf.card_id` is None (fallback): use existing `_send_response_async(text)` path unchanged.

**`on_tool_start(name, inputs)`**:
- If streaming card is active: append formatted tool hint to `_stream_buf.text`, update card. Do NOT send a separate card.
- If no streaming card: send separate tool-hint card (existing behavior).

#### Config

Add `streaming: bool = True` to `FeishuConfig`. When False, `on_stream_chunk` only accumulates (current behavior).

### 2. File Sending

#### Extension maps

```python
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
_FILE_TYPE_MAP = {
    ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
    ".xls": "xls", ".xlsx": "xls",
    ".ppt": "ppt", ".pptx": "ppt",
}
# Anything not in FILE_TYPE_MAP uses file_type "stream"
```

#### New sync methods on FeishuOutputSink

- `_upload_image_sync(file_path) -> str|None` — `im.v1.image.create` with `image_type="message"`, return `image_key`.
- `_upload_file_sync(file_path) -> str|None` — `im.v1.file.create` with mapped `file_type`, return `file_key`.
- `_send_file_async(file_path)` — Async wrapper: determine image vs file by extension, upload, send via `_do_send` with `msg_type="image"` (content: `{"image_key": key}`) or `msg_type="file"` (content: `{"file_key": key}`).

#### Trigger 1: write_file tool

Modify `on_tool_end(name, result)`:
- If `name == "write_file"`: parse file path from `result` string, schedule `_send_file_async(path)`.

#### Trigger 2: output_dir files

- `FeishuOutputSink.__init__` accepts optional `output_dir: Path|None`.
- Record `_turn_start = time.time()` at construction.
- In `on_turn_complete`, after sending text: scan `output_dir` for files with `mtime > _turn_start`, upload each via `_send_file_async`.
- `FeishuChannel` receives `output_dir` from components (set in `create_sink` after session start).

#### Trigger 3: /send command

In `FeishuChannel._on_message`, before dispatching to the handler:
- If message text matches `/send <path>`:
  - Resolve path (absolute, or relative to `_output_dir`).
  - Validate file exists.
  - Create a temporary sink, call `_send_file_async(resolved_path)`, drain.
  - Return without invoking the agent handler.

### 3. Integration Changes

#### FeishuConfig

```python
@dataclass
class FeishuConfig:
    # ... existing fields ...
    streaming: bool = True
```

#### FeishuChannel

- Add `_output_dir: Path | None = None` attribute.
- In `start()` or via a new `set_output_dir(path)` method, receive output_dir from the ChannelRunner/components.
- `create_sink()` passes `output_dir` to `FeishuOutputSink`.
- `_on_message()`: intercept `/send <path>` commands.

#### _build_gateway_channels (agent.py)

No changes needed — `output_dir` is passed after channel construction, during session start.

#### ChannelRunner

After `fire_session_start`, set `channel._output_dir = components.get("output_dir")` for each Feishu channel. Alternatively, add this to `FeishuChannel.start()` via handler closure.

### 4. Edge Cases & Fallbacks

| Scenario | Behavior |
|----------|----------|
| CardKit API unavailable / permission error | `card_id` stays None, falls back to accumulate-and-send |
| Streaming card timeout (Feishu closes it) | Final update fails, fall back to sending regular interactive card |
| File upload fails | Log warning, skip sending that file |
| `/send` path doesn't exist | Reply with error text to the chat |
| `streaming: false` in config | `on_stream_chunk` only accumulates (current behavior) |
| Image > 10MB / File > 30MB (Feishu limits) | Upload API returns error, logged and skipped |

### 5. Files to modify

| File | Changes |
|------|---------|
| `channels/feishu.py` | Add `_FeishuStreamBuf`, streaming methods, file upload methods, modify sink methods, add `/send` command |
| `agent.py` | Pass `output_dir` to Feishu channel after session start (minor, ~3 lines in ChannelRunner) |
| `tests/test_feishu_channel.py` | Add tests for streaming flow, file upload, /send command |
