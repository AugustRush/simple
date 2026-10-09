# The three big files — survey and extraction plan

**Scope:** the three modules the README roadmap names as the next large piece on
`refactor/service-kernel`:

| file | lines | what the roadmap said |
| --- | --- | --- |
| `agent/channels/web.py` | 4191 | "behind the plugin boundary, but their internals are untouched" |
| `agent/channels/feishu.py` | 2587 |  |
| `agent/tools/builtin_tools.py` | 5514 |  |

The roadmap's instruction was explicit: **start with a survey, not a rewrite**.
This document is that survey. It measures what is in the three files, records
what is already guarded, and orders the extractions cheapest-first. Nothing in
the three files has been restructured yet.

---

## How these numbers were produced

Line ranges are from `ast` (`lineno` / `end_lineno`) on the working tree at
`99d0d4e`. Cluster line counts are sums of member line counts, so they exclude
the blanks, banners and docstrings between members; they are *cost estimates*,
not file sizes. Tests were read, not run, for the coverage table.

The claims this document leans on — the ones a plan would be built on — were
re-checked by hand at the lines quoted. They are marked **[verified]** below;
everything else is a survey measurement and is marked as such.

**Environment.** This host has no `uv` and no pre-existing virtualenv, and the
bundled interpreter is signed with a Team ID under hardened runtime, so pip
wheels with ad-hoc signatures (e.g. `pydantic_core`) are refused by macOS
library validation. The suite was therefore run from a venv built in `/tmp`
from an ad-hoc re-signed copy of the interpreter:

```bash
mkdir -p /tmp/dshpy/bin && cp <runtime>/bin/python3.12 /tmp/dshpy/bin/
ln -s <runtime>/lib /tmp/dshpy/lib && codesign --force --sign - /tmp/dshpy/bin/python3.12
/tmp/dshpy/bin/python3 -m venv /tmp/simple-venv
/tmp/simple-venv/bin/python3 -m pip install -e '.[web]' pytest lark-oapi
/tmp/simple-venv/bin/python3 -m pytest -q -p no:cacheprovider --tb=no
```

A venv inside the workspace cannot be re-signed (`codesign` gets `Write
permissions error` there), which is why it lives in `/tmp`.

**Baseline, measured under this session's `workspace-write` policy:**
`19 failed, 3712 passed, 1 skipped in 94.77s`. The 19 are byte-identical to the
`workspace-write` baseline already frozen in
[`2026-10-01-baseline-failures.txt`](2026-10-01-baseline-failures.txt): 12
`test_sandbox_conformance` (the sandbox cannot be applied from this policy), 3
`test_cli_ui` console-width tests (non-terminal stdout), 1 user-tool plugin
load, 1 output-dir sandbox write, 2 user-tool isolation. The pass count is
higher than the frozen file's 2641 because the tree has grown since; the FAILED
set is the comparison that matters.

---

## 1. `agent/channels/web.py` — 4191 lines

### Shape

19 module-level functions (1 async), 3 classes. `WebOutputSink` (496–1090, 595
lines, 36 methods) and `WebChannel` (1178–4191, 3014 lines, 97 methods; 60
`Route`s + 1 `WebSocketRoute` + 1 static `Mount`). `_build_app` alone is 223
lines and `_stream` 173.

### Clusters (survey-measured)

| # | cluster | lines | notes |
| --- | --- | --- | --- |
| C1 | scheduler/workflow **payload shaping** (pure) | 299 | 8 functions, no `self`, no request, no components |
| C2 | approval / confirmation | 187 | `_normalize_confirm_decision`, sink confirmation, `/confirm` |
| C3 | scheduler HTTP handlers | 564 | 20 handlers |
| C4 | workflow definition + handlers | 427 | the largest test block (~30 tests) |
| C5 | provider / config | 388 | `WebConfig`, masking, 5 endpoints |
| C6 | plugins / skills / context | 230 | |
| C7 | sessions (service + HTTP) | 383 | |
| C8 | file serving / FS roots | 111 | |
| C9 | attachments / uploads / media | 214 | |
| C10 | `WebOutputSink` event bridging | 323 | |
| C11 | transport / app / route table | 541 | touches every cluster |
| C12 | feishu | 133 | lazy imports |
| C13 | OS folder picker | 131 | |
| C14 | tags / `_jsonable` / trigger-field | 43 | leaf |

### Single-cluster imports (they move with their cluster)

`RUN_IN_FLIGHT_STATUSES`, `acceptance_payload`, `run_needs_attention` and
`verification_payload` are each read by **only** the C1 payload functions
(web.py:33–40). `parse_task_signal` → `_signals`; `ensure_command_router` →
`_ensure_router`; `path_contains` → `_file`; `MessageAttachment` /
`attachment_kind_for_mime` → C9.

### Guard status

C1 is **strongly** guarded: `tests/test_web_channel.py` exercises it through
four endpoints, and three other modules import the helpers by name —
`tests/test_scheduler_acceptance.py:738,768` (`_scheduler_run_payload`,
`_scheduler_task_payload`) and `tests/test_scheduler_attention.py:577,591,630,642`
(`_attention_payload`). Keeping those names importable from
`agent.channels.web` keeps those guards live across the move.

### Verified findings

**[verified] `CONFIRM_DECISIONS` (web.py:52) has no reader.** A repo-wide grep
finds the definition and a mention inside a docstring, nothing else; the three
strings are hard-coded again in `_normalize_confirm_decision` (60–66). The
constant is documentation that cannot disagree loudly — either make the function
use it or drop it.

**[verified] `_turn_tag` (475–483) and `_operation_tag` (486–493) are one
function written twice**, differing only in the variable and key name
(`"turn_id"` / `"operation_id"`).

**[verified] `_web_session_output_dir` (105–110) and `_web_session_upload_dir`
(113–117) are one function written twice**, differing only in the leaf
(`"output"` / `"uploads"`).

**[verified] `WebOutputSink.defer_temporary_attachment_cleanup` (934–935)
returns `True` unconditionally** while `OutputSink`'s contract
(`agent/core/output.py:527-533`) says to return true "only after irrevocably
assuming responsibility for cleanup". The caller
(`agent/commands/coordinator.py:678-693`) then skips `path.unlink`. The web sink
emits an `attachment` event carrying that path (843–864), so *deleting* it would
break the UI link — the honest reading is that the sink claims ownership and
never performs the cleanup, not that the flag is simply wrong. This is a
behavioural decision to settle deliberately, not a refactor to fold in.

Recorded from the survey and **not** yet independently verified: the session-id
validator (`_upload_attachment`, 1733) accepting 8–128 characters while
`shared.web_session_home` accepts 1–64 and `_web_session_upload_dir` silently
falls back to the shared `unknown` home; `_stream` (3833–3836) discarding the
tasks it creates without observing their exceptions; `_schedule_run_artifacts`
(2371–2409) materialising `sorted(root.rglob("*"))` before applying its 500-item
cap and calling `stat()` without an `OSError` guard. Each has a concrete line
reference in the survey; each needs a test before it becomes a fix.

### Extraction order

1. **C1 → `agent/channels/web_payloads.py`** (299 lines). Pure, leaf, and
   guarded from four test modules. Re-export the public names from `web.py` so
   the existing importers keep working. *(This is the first increment; §4.)*
2. **C14 + tier-0 helpers** (`_turn_tag`/`_operation_tag`, `_jsonable`,
   `_normalize_confirm_decision` and the confirmation constants) → a
   `web_text.py`-shaped module. De-duplicate the twin helpers while moving.
3. **C5 provider/config** (388 lines) — `_mask_api_keys` /
   `_restore_masked_api_keys` / `_provider_write_error` are pure; the endpoints
   follow.
4. **C8 file serving** (111), **C12 feishu** (133), **C13 picker** (131) — small,
   and each already has endpoint guards.
5. **The sink (C10 + C9 + C2)** → `web_sink.py`. The survey verified that
   `WebOutputSink` never references `WebChannel`, so it is already severable;
   this is the largest single move in the file.
6. **C3/C4/C7 behind a thin `WebChannel`** — last, because they are what remains
   when everything else has moved.

---

## 2. `agent/channels/feishu.py` — 2587 lines

### Shape

Prelude 1–348; `FeishuOutputSink` 349–1862 (1514 lines, 59%, 61 methods);
`FeishuChannel` 1870–2587 (718 lines, 27 methods). Crucially, `lark_oapi` is
**never imported at module level** — only `importlib.util.find_spec` at 65 and
function-scoped imports thereafter — so renderer and lease code is testable with
no SDK stubs at all.

### Clusters (survey-measured)

| cluster | lines | purity |
| --- | --- | --- |
| R markdown/post/card rendering + splitting | 216 (+36 constants) | pure; only `re`/`json` |
| P sub-agent / tool progress formatting | 176 | pure except `_append_progress_text` |
| A attachment lease/cleanup (module) | 105 | `threading` + a module-global semaphore |
| A2 sink attachment queue/batch | 171 | threads + executor |
| S streaming-card lifecycle (async) | 125 | loop + executor |
| S2 CardKit sync API | 157 | lark client |
| T transport / send path | 233 | lark client |
| B sink event bridging | 162 | |
| N sink init/tracing/scheduling | 113 | |
| C channel lifecycle/registration | 165 | monkeypatches `lark_oapi.ws.client.loop` at 1988 |
| E channel event handlers + dedup | 250 | |
| D download/resource fetch | 182 | lark client |

The survey measured that in 1113–1870, 49% of lines are instance-free: 141
staticmethods plus 231 lines whose only `self`/`cls` use is reading a class
constant or calling a sibling pure helper.

### Guard status

**19 tests call the pure renderers directly** — `_detect_msg_format` (10),
`_parse_md_table` (3), `_markdown_to_post` (2), `_build_card_elements` (2),
`_split_elements_by_table_limit` (2) — inside
`tests/test_feishu_channel.py:322-451`. The **P** cluster is guarded indirectly
by 12 tests asserting formatted text through `on_subagent_event` /
`_render_primary_markdown`, and has no direct call-site test.

### Verified findings

**[verified] `self._stream_flush_pending = False` appears twice in a row**
(feishu.py:982 and 983); the second is dead.

**[verified] `self._progress_phase_closed = False` (feishu.py:1385) is a dead
write** — the attribute is never initialised in `__init__` (434–465) and never
read anywhere in the file. It is the residue of a removed "phase closed"
feature.

**[verified] `if TYPE_CHECKING: pass` (61–62)** is a dead branch; `TYPE_CHECKING`
is imported and used for nothing else.

Recorded from the survey, not yet independently verified: `_flush_stream_async`
(928–932) and `_flush_progress_async` (934–938) are identical but for the flag
name; `_format_batch_started_event` / `_format_batch_finished_event` repeat a
four-statement prologue and a three-way dispatch; the tool-call card text is
duplicated at 528 and 1030; `_extract_message_attachments` (2199–2209) has no
production caller; and the `_split_long_markdown_text` comment at 1818 says
"word boundaries" while the loop at 1829 cuts at character offsets.

### Extraction order

1. **R → `agent/channels/feishu_render.py`** (~250 lines with constants). Pure,
   zero lark, and 19 direct guards. Convert the renderer group to module
   functions (their `self`/`cls` uses are the shared constants and sibling pure
   helpers only).
2. **P → the same module or a sibling** (168 lines) — the progress formatters.
   The 40-ish class constants move with them.
3. **A (lease trio)** (92 lines + `_cleanup_attachment_path` + the semaphore and
   its cap) — module-level already, no lark.
4. **M helpers** (`_ws_reconnect_delay`, `_clean_at_mentions`,
   `_is_group_chat_type`, `_extract_post_content`, the resource-filename
   helpers).
5. Client-bound clusters (S/S2/T/D/C/E/A2) stay until the pure layers are out;
   they are what makes `FeishuOutputSink` a 1514-line class.

---

## 3. `agent/tools/builtin_tools.py` — 5514 lines

### Shape

Module level 1–477 (14 constants, 11 functions, `_WORKFLOW_STEP_SCHEMA`
357–475). `BuiltinTools` 478–5514 (5037 lines, 86 methods), of which
**`_register` alone is 1656 lines (597–2252) — 30% of the file** — registering
44 tools.

### The headline the survey measured

**No cluster reads more than 5 distinct leaf instance attributes.** The class is
not a god-*object*; it is a god-*namespace*. Coupling is (a) `self.registry`
used as a context bag and (b) sibling-method calls — both declarable as data.
That is what makes extraction tractable at all.

### Clusters, cheapest first (survey-measured)

| # | cluster | lines | leaf state | guards |
| --- | --- | --- | --- | --- |
| 1 | memory | 84 | `{memory, chapter_normalizer}` | 5 + retrieval-quality tests |
| 2 | web (fetch/search/tavily) | 235 | `registry` (2 keys) | 8 |
| 3 | plugins + user tools | 427 | `registry` (6 keys) | service-side: 120 + 24 |
| 4 | signals | 109 | **none** | 5 |
| 5 | step output | 151 | `{_output_dir}` | 15 |
| 6 | output housekeeping (`_clean_output`) | 33 | `{_output_dir}` | 7 |
| 7 | workflows | 527 | **none** | 6 + `test_scheduler_workflows.py` |
| 8 | scheduler | 749 | `{_cached_schedule_store}` | 41 + `test_scheduler_editing.py` (38) |
| 9 | files | 248 | 5 incl. the `_file_service` property | 14 |
| 10 | audio | 105 | `registry` (1 key) | 4 |
| 11 | context | 151 | `{context_manager}` | **2 — thinnest** |
| 12 | shell | 306 | `registry` (11 keys) | 24 |

### Is `_register` a table?

**No — and the survey's answer is worth keeping.** It is a flat method of 44
sequential `r.register(...)` calls, one conditional (`clean_output` at 1980) and
one banner. The *call shape* is uniform (name, description, schema, handler,
`source=`, optional `capabilities=` / `authorizer=`), but the *payload* is not
mechanically dedupable: `intent` occurs 17 times as 17 deliberately different
sentences. A central data table would relocate ~1650 lines of prose without
removing duplication. The extraction-friendly form is **each cluster module
owning its own registration table**, after which the `BuiltinTools._register`
god-method disappears.

### Verified findings

**[verified] `_atomic_write_text = shared._atomic_write_text` (line 56) is
dead** — the name occurs once in the file, at its own assignment.

**[verified] The same registry lookup is written twice, and each instance calls
`get_context` twice** — lines 2352 and 2607 are the byte-identical expression
`self.registry.get_context("user_plugins_dir") and Path(str(self.registry.get_context("user_plugins_dir")))`.

**[verified] The authorization scope is built identically in two clusters** —
`_plugin_authorization_scope` (2559–2561) and `_shell` (3192–3194) construct the
same `ShellAuthorizationScope` from the same three metadata keys with the same
defaults.

Recorded from the survey, not yet independently verified: seven tools declare
`capabilities=("state_write","requires_intent")` at the registration site and
the same value again in `ToolRegistry._DEFAULT_TOOL_CAPABILITIES`
(`agent/tools/runtime.py:354-440`) with nothing asserting the two agree; five
registered tools have no capability in either place
(`list_installed_plugins`, `list_signals`, `list_tools`, `read_step_output`,
`report_outcome`); and `web_search`/`tavily_search` share one backend while the
`region` parameter advertised at 1905–1909 is otherwise unused.

### Extraction order

memory → web → output housekeeping → plugins/user tools → (approval + authz
service, which two clusters duplicate) → signals + step output → scheduler +
workflows (one move, not two) → files → audio → shell → context.

---

## 4. The first increment

**Move the web payload cluster (C1) out of `agent/channels/web.py` into
`agent/channels/web_payloads.py`.**

Why this one first, in the roadmap's own terms ("a survey, not a rewrite"):

- It is the largest self-contained, provably pure block in the largest file:
  299 lines, 8 functions, zero `self`, zero request handling, zero component
  access.
- It is guarded by tests in **four** modules, three of which import the helpers
  by name — so the guard is real and the move is measurable, not a matter of
  opinion.
- It sets the module-per-cluster shape the rest of web.py will follow, on the
  cheapest possible example.

The move keeps `agent/channels/web.py` importing the names, so existing callers
and the three test modules that import them by name keep working unchanged. The
new module carries the docstrings that explain *why* each payload looks the way
it does (`with_snapshot`, `verdict` vs `status`, `in_flight` for a queued
signal-woken run) — those comments are the reason the cluster is worth its own
file.

**Verification:** the four payload-guarding test modules, then the full suite,
comparing the FAILED set against the baseline above rather than reading the pass
count.

---

## 5. Explicitly out of scope

- **No rewrite.** No cluster below the "first increment" line is touched in this
  round.
- **No behaviour changes.** The findings in §1–§3 are recorded as decisions to
  be taken deliberately with a test each, not folded into a move. The
  temporary-attachment ownership question in web.py is the clearest example:
  the fix and the leak are one line apart, and which one is right depends on
  whether the browser is expected to fetch that path later.
- **The Profile/Bundle layer** from the closing report remains not done; it is a
  new functional layer, not a refactor of these three files.
