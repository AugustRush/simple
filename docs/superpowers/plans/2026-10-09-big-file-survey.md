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

## 4. What landed

Every increment below is on the branch, each verified by running the full suite
and diffing the FAILED set against the baseline above.

### 4.1 web.py — the payload shape (`d51d166`)

`agent/channels/web.py` **4191 → 3881 lines**; the C1 cluster (299 lines, 8
functions, zero `self`) moved to `agent/channels/web_payloads.py`.

It was chosen first, in the roadmap's own terms ("a survey, not a rewrite"),
because it is the largest self-contained, provably pure block in the largest
file, and it is guarded by tests in **four** modules — three of which imported
the helpers by name. Following the repository's convention for extracted
modules (`spawn_planning`, `token_budget`), the helpers took public names, and
the 15 call sites in `web.py` plus the three test import lines were updated;
**no test body changed**, so the guards are the same guards.

The new module carries the comments that explain why each payload looks the way
it does — `status` vs `verdict`, why `with_snapshot` is off inside a list, why
`in_flight` cannot be re-derived by the client — because those are the reason
the cluster is worth a file.

### 4.2 feishu.py — the pure renderer half (`385f1de`)

`agent/channels/feishu.py` **2587 → 2326 lines**; the R cluster (216 lines plus
36 lines of constants) moved to `agent/channels/feishu_render.py` as eight
public functions.

The boundary was measured before anything moved: four of the eight functions are
called only from `_send_response_async`, and the other four only from
`build_card_elements`; no consumer exists outside `feishu.py` and one test
block. Twelve renderer regexes and the three size ceilings moved with them —
except `MD_ELEMENT_CHAR_LIMIT`, which `_render_primary_markdown` still reads, so
`feishu.py` imports it rather than defining a second copy of the same number.
The 19 direct renderer assertions now call the module functions; the assertions
themselves are unchanged.

The move deliberately preserved two warts rather than tidying them on the way
out, and says so in the module docstring: `split_long_markdown_text`'s "word
boundaries" comment describes an intent the loop does not implement, and its
long-line branch mixes three- and four-space indentation.

### 4.3 A mistake worth recording

The first version of the feishu script used the rename pattern
`(?<![\w.])_name`, which silently skips every `self._name` / `cls._name`
reference — the exact form the renderer used. The result would have been a
module full of `self._TABLE_RE`. It was caught by grepping the generated module
for `self.`/`cls.`, then `git checkout -- agent/channels/feishu.py` restored the
file from HEAD and the whole extraction was redone rather than patched in
place. The same rewrite also had the constant range off by one line (it took
`_IMAGE_EXTS = {` too); `py_compile`'s `IndentationError` caught that, and the
ranges were then taken from `git show HEAD:... | grep -n` instead of counted
from unnumbered output.

Both are the failure mode the closing report named: **the splice risk is the
definition next to the block, not the block.**

### 4.4 web.py — the outbound half (`de9adb8`)

`agent/channels/web.py` **3881 → 3204 lines**; `WebOutputSink` (595 lines) and
the helpers that had no other reader moved to `agent/channels/web_sink.py`.
The class body is byte-identical after the move (checked against `HEAD`), and
the boundary was measured with `ast` rather than assumed: `_turn_tag`,
`_operation_tag`, `_jsonable`, `_MEDIA_EXTS`, `_RASTER_EXTS` and
`_svg_has_raster_sibling` have **zero** references outside the class, so they
travelled with it.

Two names stayed shared, and the move gave them a **single home**:
`CONFIRMATION_TIMEOUT_SECONDS` is read by the sink and by
`WebChannel.wait_for_confirmation`, so `web.py` now reads it *through the sink
module* instead of importing a copy — two copies would have been the very
"one bound, two names" smell the survey found in `builtin_tools.py`, and it
would have quietly weakened the test that shortens the clock (the monkeypatch
target moved to `web_sink`, the assertions did not change).
`_normalize_confirm_decision` is imported by name.

Two pairs were deliberately **not** de-duplicated, which supersedes what §5
said about the twins: `_turn_tag` / `_operation_tag` each carry a docstring
describing a *different* failure, and `CONFIRM_DECISIONS` is the vocabulary the
normaliser's branches already spell out.

### 4.5 feishu.py — the progress wordings, and a missing guard (`6df57ac`)

`agent/channels/feishu.py` **2326 → 2152 lines**; the ten progress formatters
moved to `agent/channels/feishu_progress.py`. The reason this move was worth
more than its 168 lines: the survey measured that **no test called any of
these functions directly** — the only coverage was indirect, through
`on_subagent_event` and `_render_primary_markdown` asserting a finished card.

So this increment added a guard as well as a move:
[`tests/test_feishu_progress.py`](../../../tests/test_feishu_progress.py), 16
direct assertions on the cases the code deliberately refuses to guess — a
boolean that is an `int`, a denominator that is *missing* rather than zero, a
parallel batch with no declared concurrency, a pipeline that lost subtasks, and
an event nobody recognises. The moved bodies are identical after dropping the
receiver parameters (146 lines compared).

One thing was checked and **not** changed: `agent/core/output.py:1062` has a
same-named `_format_subagent_event`, but it is the CLI console's wording
(`"Sub-agents running: …"`, no emoji) rather than the card's. Two surfaces, two
renderings — not a duplicate to merge.

### 4.6 builtin_tools.py — memory, and what the guards caught (`2298a4b`)

`agent/tools/builtin_tools.py` **5514 → 5428 lines**; the six memory tools
became `MemoryTools` in `agent/tools/memory_tools.py`.

The form is dependency injection, and it was chosen over the alternative the
survey offered (a shared `ok()` / `error()` module, which would have meant
touching **108** call sites and pre-empting the decision for the eleven
clusters not yet moved). The two response builders are passed in, so the shape
still has exactly one definition and twelve modules cannot drift into twelve
spellings of "ok". Every memory test already drove the tools through
`registry.call(...)`, so what is guarded is the registration boundary.

**Two mistakes, both caught by guards written for the purpose:**

1. The first attempt de-indented the methods to module level, as the three
   previous extractions had. The AST guard (*every method keeps its receiver*)
   failed immediately: the target here is a **class**, not a module, so the
   indentation had to stay. The guard itself was wrong once too — `clear` is an
   `AsyncFunctionDef`, and a check that only counted `FunctionDef` reported the
   wrong method list.
2. The verbatim move copied the body but not the names it was standing on:
   `clear` reads `_active_sink`, a module global of its **old** home. The tool
   executor turned the resulting `NameError` into an ordinary tool error, so
   `memory_clear` answered `ok: False` — and
   `test_memory_clear_uses_human_confirmation_and_storage_api` caught it. The
   fix imports it in the new module, with a comment saying why.

A free-name scan over all five extracted modules reported no dangling globals
after the fix. **It was then measured as a candidate repository guard and
rejected**: run across `agent/`, the same scanner produces 452 findings, almost
all of them false — closure variables, walrus bindings, `with … as` targets and
comprehension variables that a naive scope walk cannot see. Turning it into a
guard would mean writing a real scope analyser, not a heuristic. The defect it
found was caught by the suite anyway (`memory_clear`'s test); the scanner's
value was in pointing at *why* the test failed, not in replacing it.

### 4.7 builtin_tools.py — the web cluster, and one bound with two names (`015d03a`)

`agent/tools/builtin_tools.py` **5428 → 5187 lines**; the three registrations
that make up `web_fetch` / `web_search` / `tavily_search` (eight methods) became
`WebTools` in `agent/tools/web_tools.py`, in the same injected-responder form as
`MemoryTools`.

The recorded finding about the search bound was closed on the way: the schemas
advertised `WEB_SEARCH_MAX_RESULTS` while the handler clamped with
`TAVILY_SEARCH_MAX_RESULTS` — two names, both `10`, free to drift. The schemas
now quote the enforced constant and the advertised-only name is gone.

This move is the first one whose blast radius was mostly in the **tests**, and
it is worth recording what that looked like, because it is the splice risk in
test form:

- two `monkeypatch.setattr(BuiltinTools, "_make_tavily_request" / "_tavily_search", …)`
  targeted private methods by class;
- one patch targeted a **module path**,
  `agent.tools.builtin_tools.fetch_public_http_url` — the function moved house,
  so the patch's address had to move too, or the test would have silently
  stopped exercising the thing it was written to exercise;
- four direct calls into `tools._web_*`, and two now-unused `BuiltinTools`
  imports.

A structural guard caught the rest on its own: `tests/test_context_keys.py`
holds the ledger of which file reads each context key, and it failed with
*"web_proxy: agent/tools/builtin_tools.py does not read it; web_tools.py reads
it but is not listed"* — exactly the sentence needed to fix
`agent/runtime/context_keys.py`. That is the ledger doing the job it exists for.

The receiver guard needed the opposite polarity this time: `strip_html` and
`make_tavily_request` are `@staticmethod`, so "every method keeps its receiver"
is false for them. The guard is now decorator-aware (`static ⇔ no self`).

### 4.8 builtin_tools.py — output housekeeping (`03971b3`)

`agent/tools/builtin_tools.py` **5187 → 5155 lines**; `_clean_output` (33 lines)
became `OutputTools.clean` in `agent/tools/output_tools.py`.

The cluster is small, and the interesting part is what was deliberately **not**
done. `clean_output` is the only tool whose registration is conditional
(`if self._output_dir is not None`), and the survey suggested the condition
"becomes structural once it lives with the output directory". It does not in
this round: the condition stays in `_register`, beside the other 43
registrations, because that is where "which tools this deployment has" is
decided. What moved is the behaviour — the containment check on `subdir`, the
age filter, files before the directories they empty, and a result that reports
counts plus at most ten per-file errors rather than failing the call.

The proof that the split is in the right place is a test that did not change:
`test_clean_output_not_registered_when_no_output_dir` still asserts the
condition, and it still passes untouched. Six `bt._clean_output(...)` call sites
in `tests/test_output_dir.py` became `bt._output_tools.clean(...)`.

### 4.9 feishu.py — attachment leases and capacity (`e74606a`)

`agent/channels/feishu.py` **2152 → 2048 lines**; the lease trio
(`_AttachmentLease`, `_QueuedAttachment`, `_AttachmentBatchCapacityLease`),
`_cleanup_attachment_path`, and the process-wide capacity semaphore with its
lock and error string moved to `agent/channels/feishu_attachments.py`.

They were already module-level, so the work here was not unwinding a class but
**choosing a home** — and the justification is that they are about threads,
files and ownership rather than about Feishu, and never touch the lark client,
so they can be read and tested without the SDK.

Two things stayed, on purpose. `_ATTACHMENT_CANCEL_GRACE_SECONDS` stayed in
`feishu.py` because the sink is what waits on it. The capacity semaphore is
defined **and read in the new module only**, because three tests shrink it
through `monkeypatch.setattr("agent.channels.feishu._ATTACHMENT_BATCH_CAPACITY",
…, raising=False)` — and that call would have become a **silent no-op** after
the move: `raising=False` means no error, while the class reads the new module's
global.

Because that is exactly the "guard quietly stopped guarding" failure, it was
measured rather than assumed:

| patch target | `try_acquire(2)` with capacity 1 | verdict |
| --- | --- | --- |
| `agent.channels.feishu_attachments` (new) | `None` | effective |
| `agent.channels.feishu` (old, `create=True`) | a lease | a no-op |

and `hasattr(agent.channels.feishu, "_ATTACHMENT_BATCH_CAPACITY")` is now
`False`, so without `raising=False` the three tests would have failed loudly
instead. The targets were updated to the module that actually reads the name.

One ledger note followed the code: `agent/runtime/state_ledger.py`'s "not
scanned, and why" list recorded `_ATTACHMENT_BATCH_CAPACITY_LOCK` at
`agent/channels/feishu.py:85`; its coordinates are now
`agent/channels/feishu_attachments.py:44`.

### 4.10 feishu.py — inbound parsing (`2a1b64a`)

`agent/channels/feishu.py` **2048 → 1973 lines**; `clean_at_mentions`,
`is_group_chat_type` and `extract_post_content` moved to
`agent/channels/feishu_parsing.py` as public functions.

The justification is not size — it is that these three import nothing from
Feishu and nothing from lark, so they are the part of a 1973-line channel file
that a reader can verify without the SDK. `extract_post_content` is the one that
earns the file: the same message arrives direct, under a locale key, or wrapped
in `{"post": …}`, and a reader that handled one shape would **silently drop the
text** of a message that arrived in another.

The eight tests that already called two of them were moved to the new module's
import path and their call sites renamed; **no assertion changed**. The private
`_AT_PLACEHOLDER_RE` stayed private, because only `clean_at_mentions` reads it.

A slip worth recording: while batch-renaming here I typed `_clean_placeholder(`
instead of `clean_at_mentions(`; the next command changed it back, and a
repo-wide grep confirms no residue. The habit that caught it is the one this
document keeps recommending — grep after every rename rather than trusting the
last one.

### 4.11 builtin_tools.py — the signal tools (`973e829`)

`agent/tools/builtin_tools.py` **5155 → 5042 lines**; `report_outcome`,
`emit_signal` and `list_signals` became `SignalTools` in
`agent/tools/signal_tools.py`.

This is the first cluster whose extraction depended on a **sibling** rather than
on leaf state: all three reach the scheduler through `_schedule_store()`. The
move therefore injects the accessor (`SignalTools(self._schedule_store, …)`)
instead of dragging the store along — which is the concrete meaning of the
survey's "it is a god-*namespace*, not a god-object": the dependency is
declarable.

Two properties travelled with the code into the module's docstring.
`report_outcome` has **no success case** — an unattended run is judged by its
acceptance check, and an agent's own assurance is a claim rather than evidence,
so a parameter usable only in the harmful direction is not offered; outside a
scheduled run it refuses rather than quietly accepting. And `emit_signal` does
**not** run the subscriber: it may be called from inside the run the scheduler
is executing, and starting another from there would nest one scheduler lifetime
inside another.

Three imports that only this cluster used (`_active_run_self_report`,
`_active_signal_context`, `VERDICT_FAILED`) left the mid-file `# noqa: E402`
block with it, leaving only `_active_schedule_target`.

### 4.12 builtin_tools.py — step output (`7bd1ee1`)

`agent/tools/builtin_tools.py` **5042 → 4854 lines**; `read_step_output`, its
private budget helper, the graph-walking `_workflow_ancestors` and the three
`READ_STEP_OUTPUT_*` bounds moved to `agent/tools/step_output_tools.py`.

The tool's safety property is what the file is named for: it is not a file
reader with a narrower name, it is a lookup into **this workflow's own history**
— only `output_path` values the scheduler recorded are ever read, so a path it
was not told about is not reachable through it. Addressing is by step key, the
workflow comes from the run being served rather than from an argument, and only
*ancestors* are readable, because a step beside it has not run this round and a
step below it has not run at all.

The two ceilings moved too, and now have one home: the schemas still quote
`READ_STEP_OUTPUT_MAX_CHARS` / `READ_STEP_OUTPUT_DEFAULT_CHARS`, so
`builtin_tools.py` imports them from the new module, while
`READ_STEP_OUTPUT_BUDGET_SHARE` no longer exists there at all. The 14 direct test
calls (`tools.` and `handoff.`) became `._step_output_tools.read(...)` with no
assertion touched, so the 15 guards around this tool are the same guards.

### 4.13 builtin_tools.py — scheduler + workflows, one move (`00683a8`)

`agent/tools/builtin_tools.py` **4854 → 3441 lines** — the largest single
reduction in the series — with all twenty-one methods landing in
`agent/tools/schedule_tools.py` as `ScheduleTools`.

**One class, not two, and that was measured.** The workflow half reads five of
the scheduler's own helpers (`schedule_store`, `schedule_target`,
`schedule_trigger`, `action_payload`, `workflow_steps`) and eighteen names from
`agent.scheduler`. Splitting them into two classes would have converted all
those calls into constructor parameters, so they are one extraction.

**The injected surface was measured before anything moved.** An AST pass over
the twenty-one methods collected every `self.X(...)` call whose `X` is *not* in
the cluster, and the answer was exactly five: `_schedule_store` (15 calls),
`_error` (26), `_ok` (12), `_active_workspace_root` (3),
`_chosen_workspace_root` (3). That is the constructor. The store accessor
deliberately **stays on the host**, because three clusters share it — signals,
step output, and the host itself — and moving it would cut the shared seam.

**The free-name scan fired before the tests did, and that is the point.** A
verbatim move copies the body, not the names it stands on: `_run_detail` reads
`_clip` and the three `RUN_*` bounds, none of which travelled. The scan reported
all four, and they moved too once it was clear nothing else read them — this is
the memory-cluster lesson applied *before* the suite instead of after. One
residue was still caught by a test rather than by the scan:
`test_schedule_runs_clips_a_reason_that_would_flood_the_window` imported
`RUN_REASON_CHARS` from the old home, and now imports it from the new one.

The mechanical remainder: twelve registration lines point at
`self._schedule_tools.<public name>`; fifteen direct test calls (`tools._workflow_create(…)`
and friends) became `tools._schedule_tools.workflow_create(…)` with **no
assertion changed**; three documentation references to `_schedule_create`,
`_schedule_delete` and `_action_payload` were renamed with the methods; and
`normalize_products` / `_active_schedule_target` no longer exist in
`builtin_tools.py` at all.

### 4.14 builtin_tools.py — the file cluster, and the defect the six earlier moves hid (`c1f89fc`)

`agent/tools/builtin_tools.py` **3441 → 3181 lines**; the file layer —
the four CRUD tools, `send_file`, the path resolvers, the write authorizer, the
workspace snapshot/move helpers and the `file_service` **property** — became
`FileTools` in `agent/tools/file_tools.py`.

**The property is why this cluster is not a plain move.** `file_service`
resolves the provider through four fallbacks — a session's `ContextVar`
override, the constructor's injected provider, the registry's `file_service`
context, and a default `FileService` built from the workspace root and output
directory — and it resolves **on use**, not in `__init__`, because bootstrap
injects the constructor argument before it sets the context. Both the order and
the laziness moved intact. Only four members are read from outside the cluster
(`_shell` three, `_transcribe_audio` one), measured with an AST pass first, and
they are reached through the host's `self._file_tools` handle.

**What this move exposed is the important part, and it was a defect in the six
extractions that came before it.** `ToolRegistry.call` installs the calling
registry's merged context on the registry the tool implementation reads:

```python
owner = getattr(fn, "__self__", None)
owner_registry = getattr(owner, "registry", None)      # <- the convention
owner_registry._context_override.set(merged_context)
```

Once a method moves into a cluster object, `fn.__self__` is **the cluster, not
`BuiltinTools`** — and those clusters stored the registry as `_registry` or not
at all, so `owner_registry` became `None` and **the merge silently stopped
happening**. A session's own `workspace_root`, `output_dir`, `file_service` and
`write_scope` stopped reaching the tools, which fell back to the composition's.

The unit tests could not see it: they call a tool through the *same* registry it
was registered on, where no merge is needed. The functional layer caught it —
`test_a_session_turn_writes_into_the_sessions_own_output_dir` and
`test_switching_a_sessions_workspace_moves_where_its_tools_write` — as a file
landing in the composition's `output/` instead of
`web/sessions/<id>/output/`.

The diagnosis was measured rather than guessed: a temporary print inside
`file_service` showed `override_keys=None` **and** `injected=True` on the
composition's registry, i.e. no merged context had been installed on the object
the tool actually reads. Following `runtime.py:551-567` back to the convention,
and then to its two existing users (`ScopedTools.registry`,
`MCPClient.registry`), gave the fix: **every cluster that provides registered
tools exposes a public `registry`**. All seven now do (`WebTools`/`FileTools`
renamed from `_registry`; the other five take it as a constructor argument), and
each `__init__` says why it must stay public, so a later tidy-up cannot quietly
make it private again.

That is the rule for the clusters still to move: **a cluster whose methods are
registered as tools must expose `registry`, or the composition's context wins
silently.**

Two imports left the host with the cluster (`FileAccessPolicy`, `FileService`,
`write_scope_allows`, `OutputSink`, `time` — five in total), as did
`_injected_file_service` / `_fallback_file_service`, which the property owned.
The ledger followed: `file_service` and `capability_profile` now name
`agent/tools/file_tools.py` as the reader, and `file_access_policy` /
`write_scope` list it alongside the host.

### 4.15 builtin_tools.py — the audio tool (`0cfbb21`)

`agent/tools/builtin_tools.py` **3181 → 3081 lines**; `transcribe_audio` and its
two helpers became `AudioTools` in `agent/tools/audio_tools.py`.

The argv builder is a security boundary, not a convenience, which is why the
cluster is worth a file: the command template comes from **configuration**, so a
template containing shell operators would be a way to run arbitrary shell from a
config value. `shell_command_uses_shell_features` refuses it outright rather
than letting a shell parse it; what is left is split with `shlex` and
substituted literally, and a template that cannot be split (or splits to
nothing) is refused with the reason.

The subprocess runs in the agent's output directory with `AGENT_OUTPUT_DIR` and
`AGENT_WORKSPACE_ROOT` in its environment. Both are resolved **at call time**
through injected callables — a session's own output dir has to win over the
composition's. `resolve_tool_path` is injected from `FileTools` (the sibling
cluster moved in §4.14) as a bound method rather than reaching back into the
host.

### 4.16 builtin_tools.py — context, and the guard it was missing (`71135a9`)

`agent/tools/builtin_tools.py` **3081 → 2932 lines**; `context_retrieve` and
`clear_context` became `ContextTools` in `agent/tools/context_tools.py`.

This was the **least-guarded pair in the file** — the survey measured two tests
— so the move added a direct test module rather than trust them:
[`tests/test_context_tools.py`](../../../tests/test_context_tools.py), eleven
assertions on rules that only the pair enforced. Moving code whose rules are
held up by accident, and leaving them held up by accident, is not preserving
behaviour.

Three documented rules travelled into the module's docstring, each written
because the obvious version is wrong:

- **`queries` is plural on purpose, and a bare string is accepted on purpose.**
  A model filling an array parameter with a single string is routine, and
  iterating that string would yield its *characters* — one good query silently
  becoming dozens of one-character ones.
- **The manager is resolved from the active turn**, not captured at
  construction: tool definitions are shared across multiplexed sessions, and the
  bootstrap manager's staging session is not user-facing. The current turn is
  also excluded from the search, or the answer comes back as though it were a
  memory.
- **An empty result is a hint, not an error** — it is far more often a
  badly-shaped query than an absent memory.

The two methods are not adjacent: the shared `_schedule_store` accessor sits
between them and **stayed**, since three clusters share it. Six direct test calls
were rewired with no assertion changed, the host produced no unused imports, and
the ledger was untouched (this cluster reads no context keys).

### 4.17 builtin_tools.py — the shell tool, and the last cluster (`61d582b`)

`agent/tools/builtin_tools.py` **2932 → 2611 lines**; `_shell` (271 lines),
`_terminate_process` and the model-facing `_SHELL_CONFIRMATION_GUIDANCE` became
`ShellTools` in `agent/tools/shell_tools.py`.

This is the tool everything else is measured against, and the most entangled
cluster in the file: **eight** registry context keys and nine reads of the
registry. Ten callables are injected — the workspace root, the output directory,
the resource home, the confirmation prompt, the two response builders, and three
operations borrowed from `FileTools` — so the class depends on ten functions
rather than on a class with forty tools in it.

Five decisions travelled with the code, each of which looks like something to
simplify:

- **The sandbox mode is linked to the permission level.**  `none` is honoured
  only when the *effective* level is `full`; otherwise it fails safe back to
  `read_all`.  Turning the sandbox off must not be reachable by a config edit
  the permission level never agreed to.
- **The pre/post snapshot is not an authorization mechanism.**  It is a
  file-domain invariant on top of the OS boundary: an `output_dir` call under
  danger-full-access must still not leave new files in the workspace.
- **A sandbox denial is answered with the narrow knob that would have worked.**
- **The scratch dir is released in a `finally`** — otherwise it leaked once per
  invocation, unbounded for a long-running gateway.
- **The blocklist is checked before any subprocess exists**, and a
  confirmation-required command is **re-checked** after approval.

The survey's open question was answered by measurement: `_try_interactive_confirmation`
(75 lines) is shared with the plugin install path, so it **stayed on the host**
and is injected. `_resource_home` stayed too (the ledger records it as shared
with `scheduler/build.py`); `_sandbox_dir` had exactly one caller and moved.
`AudioTools` now borrows `shell_tools.terminate_process`, the same
sibling-injection shape as `file_tools.resolve_tool_path` in §4.15.

**Three mistakes were caught, and two of them by tests:**

1. The script that trimmed the host's unused imports also removed
   `from __future__ import annotations` — which immediately turned a
   **pre-existing** lazy annotation (`-> SchedulerStore`, a type that was never
   imported) into a `NameError`. Restored, and the annotation left as it was:
   it is a PEP 563 dependency, not this round's debt.
2. The receiver census used `[A-Za-z_]*\._shell(` and missed
   `kwargs["tools"]._shell(` — the bracket. The `AttributeError` found it.
3. The rename covered `def _sandbox_dir(` but not the internal call
   `self._sandbox_dir()`. Also found by a test.

The ledger moved eight keys (`shell_permission_level`, `shell_sandbox_mode`,
`shell_allowed_commands`, `shell_blocked_commands`, `shell_secret_paths`,
`shell_devices`, `file_access_policy`, `write_scope`) from
`agent/tools/builtin_tools.py` to `agent/tools/shell_tools.py`; `resource_home`
did not move. Thirty test call sites became `_shell_tools.run(...)` with no
assertion changed.

### 4.18 builtin_tools.py — plugins + user tools, the last cluster (`4de794f`)

`agent/tools/builtin_tools.py` **2604 → 2067 lines**; thirteen methods and the
six module-level helpers only they used became `PluginTools` in
`agent/tools/plugin_tools.py`.

**This cluster needed nothing from the host but the registry** — one injected
value, no callbacks, which makes it the cleanest of the eighteen. Its six
context keys reached through that registry are the whole dependency.

The helpers travelled with it, and each carries a decision worth keeping:

- **`canonicalize_plugin_source` rewrites `git://` to https** — port 9418 is a
  legacy transport that home and corporate networks routinely block and that
  carries no credentials; https, ssh and local paths are legitimate and stay.
- **`plugin_executable_content` names what will *run*** — a Python entry point,
  an MCP server or hooks is equivalent to executing arbitrary code, and the
  install path must be able to say which one it is before a person approves it.
- **`resolve_user_plugin_target` is a path-traversal boundary** for a name that
  arrives from a model: canonical slug, direct child of the plugins directory.
- **`plugin_install_lock` serialises installs process-wide** — an install is a
  clone/validate/swap/reload transaction that two calls would interleave.
- **`_PluginInstallError` is control flow, not failure** — it carries the
  structured payload out of the middle of the transaction so a failed upgrade
  can roll back and report the same shape.

**Three structural ledgers followed the code**, which is this repository's
guards doing the bookkeeping a move would otherwise get wrong:

| ledger | what it said |
| --- | --- |
| `context_keys.py` | six keys' readers move to `plugin_tools.py` |
| `state_ledger.py` | `_PLUGIN_INSTALL_LOCK` is a **REBOUND** global (assigned after `global`), so its coordinates moved — the guard first reported *"module-level mutable state outside the state ledger"*, then *"ledger entries that no longer exist"*, both pointing at the same line |
| `core_services.py` | the owners of `registry`, `skill_catalog` and `mcp_client` moved with their writer |

The tests showed the same coupling shape as the shell move, in a new place:
**three `monkeypatch.setattr(tools, "_reload_plugins", …)` calls had to move onto
`tools._plugin_tools`**, because the internal call is `self.reload_plugins()` on
the cluster object — patching the host instance would no longer be seen. And
`test_registry_context_boundary.py`, which drives `reload_plugins` *unbound*
(`BuiltinTools._reload_plugins(tool)`), now drives `PluginTools.reload_plugins(tool)`.
No assertion changed in either.

Two cleanups came with it: the dead alias
`_atomic_write_text = shared._atomic_write_text` (recorded in §3, verified as the
only occurrence in the file and imported by nobody) is gone, and seven host
imports that only this cluster used were removed.

Two mistakes were caught by the guards written for the purpose: the AST check
took *the first* `ClassDef` in the new module — which is `_PluginInstallError`,
not `PluginTools` — and reported a class with only `__init__`; and the helper
region's end was computed as three blank lines when the file has four.

## 5. Next

**Every cluster in §1–§3 has now moved.** The three files went from **12292 to
7244 lines** (web.py 4191 → 3204, feishu.py 2587 → 1973, builtin_tools.py 5514 →
2067), with **seventeen** modules holding what left — and one defect found and
fixed that the first six of those modules had introduced (§4.14).

What is left, and what each piece is:

1. **`_register`, 1656 lines of `builtin_tools.py`** — the registration table
   itself, mounting forty-odd tools onto the cluster objects. §3's answer stands:
   the extraction-friendly form is each cluster owning its own registrations
   (a `TOOLS = (ToolSpec(...), ...)` per module), which is a **design change, not
   a move**. It is deliberately not attempted here: every increment in this
   document is behaviour-preserving by construction, and a registration
   refactor is where "which tools does this deployment have" would become a
   question with a different answer. It should be its own piece of work with its
   own survey.
2. **feishu.py's two leftovers** — `_ws_reconnect_delay` (12 lines, tested via
   the WS back-off cases) and the resource-filename trio
   (`_safe_resource_filename`, `_resource_fallback_filename`,
   `_attachment_mime_type`, ~25 lines). Both should move with the clusters that
   call them — the channel's reconnect loop and the download path — rather than
   on their own, which is why they have waited.
3. **`_current_time`** (21 lines) — a pure tool that has only ever sat next to
   the web cluster. It belongs with whatever module ends up owning the
   "miscellaneous single tools", or in a file of its own; it is small enough
   that the choice is not worth a round on its own.

**For any further move: expose `registry`** (§4.14). A cluster whose methods are
registered as tools and which hides that attribute will pass its unit tests and
lose the session's context in production.

## 6. Explicitly out of scope

- **No rewrite.** Only the clusters listed in §4 have moved; everything below
  the line in §5 is untouched.
- **No behaviour changes.** The findings in §1–§3 are recorded as decisions to
  be taken deliberately with a test each, not folded into a move. The
  temporary-attachment ownership question in web.py is the clearest example:
  the fix and the leak are one line apart, and which one is right depends on
  whether the browser is expected to fetch that path later.
- **The Profile/Bundle layer** from the closing report remains not done; it is a
  new functional layer, not a refactor of these three files.

## 7. The verified findings, and what happened to each

The survey's §1–§3 each recorded findings that were **not** folded into a move,
because a move preserves behaviour and these needed a decision.  They were then
worked through on the same discipline as the moves: a test that fails first
wherever the failure is observable, the full suite's FAILED set diffed against
the frozen `workspace-write` baseline, and a repository-style commit per finding.

**Every one of them is now closed.**  Ten were defects, three were duplicates of
a *rule* rather than of a spelling, and **three claims did not survive being
measured** — which is the point of marking them "not yet independently verified".

### 7.1 Fixed

| finding | commit | what it was |
| --- | --- | --- |
| `_stream` discards the tasks it creates, so a failing turn is invisible | `59cc66d` | the failing test **timed out** against the old code: the browser was told nothing at all |
| three disagreeing session-id rules (1–64 / 8–128 / `[:32]`) | `1e5435e` | a 65-char id was accepted then written into the shared `unknown` home; a 40-char session ran in the 32-char one's home |
| `/api/health` was the only `/api` route without `_authorized`, and returned the absolute `AGENT_HOME` | `b041ef8` | nothing (frontend, tests, docs) ever read that field |
| `_schedule_run_artifacts` materialised `sorted(rglob("*"))` before its cap, and called `stat()` unguarded | `e02c0ad` | the old inline body raised `OSError` on a file that vanished mid-listing → 500 |
| `if TYPE_CHECKING: pass`, the unread `_progress_phase_closed` write, the duplicated `_stream_flush_pending = False` | `f620074` | dead code that tells the reader something untrue |
| `CONFIRM_DECISIONS` declared but read by nobody | `55c4894` | the caller treats "not deny, not allow_session" as **approval**, so the vocabulary is now enforced at the one door |
| `web_search` advertised a DuckDuckGo `region` the Tavily backend ignores | `8e393b1` | a model that set it believed it had localised a search it had not |
| the authorization scope built identically in two clusters | `6995333` | it is the key the allowlist and permission level are stored under |
| `defer_temporary_attachment_cleanup` returned `True` unconditionally | `bcf1aef` | the sink claimed a cleanup it never performed; it now owns only files inside the session's own output directory (which `delete_session_async` removes), and refuses — loudly — anything else |
| five builtin tools with no capability in either place | `034a075` | the table's own comment records the doctrine: unclassified reads as "nobody classified this yet". A read-only profile was dropping the read tools; a `state_write` profile could not use `report_outcome` |

### 7.2 Duplicated rules, deduped

| finding | commit | the rule that now has one home |
| --- | --- | --- |
| `_turn_tag` / `_operation_tag` | `328c6a0` | the tag is *omitted* when the identity is unknown |
| `_web_session_output_dir` / `_web_session_upload_dir` | `328c6a0` | resolve the session home, and say so loudly when the id cannot be resolved |
| the `user_plugins_dir` lookup, three spellings | `c36e687` | read the key once, and expand `~` the same way for listing and installing |
| the `**Tool Call**` markdown fragment, twice | `2074005` | one event, two surfaces: `feishu_render.tool_call_markdown` |
| `_extract_message_attachments` had no production caller | `2074005` | production now *uses* the composition — the four tests were covering a path the product did not take |
| `split_long_markdown_text`'s "word boundaries" comment | `2074005` | the comment was the lie, not the code: it cuts at `start + limit` |
| the batch-line prologue | `2074005` | `batch_execution_mode`, because "what counts as a parallel batch" is what could drift |

### 7.3 Claims that measurement did not support

- **"Seven tools declare their capabilities twice."**  Thirteen registration
  sites declare `("state_write", "requires_intent")`, and the default table has
  **no entry for any of them** under any source: there is no double declaration
  today.  The *hazard* the survey named is real, though — a registration-site
  value wins over the table, so a contradictory default would be a lie a reader
  would trust.  That is what the three-way guard in `034a075` now prevents.
- **"`_format_batch_started_event` / `_format_batch_finished_event` repeat a
  prologue."**  Those names do not exist in today's code; the extraction had
  already made them `feishu_progress.format_batch_*`.  The prologue part was
  true and is deduped above.
- **"`_flush_stream_async` and `_flush_progress_async` are identical but for the
  flag name."**  They are — but the shared part (`_flush_primary_surface_async`)
  is already shared, and what remains is "clear *my own* pending flag".  Merging
  them would need `setattr(self, "_stream_flush_pending", ...)`: a stringly-typed
  attribute name where one misspelled letter silently creates a new attribute and
  never clears the real flag.  Recorded as a **false duplication**, deliberately
  left.

### 7.4 Deliberately not done

The `_register` table (a design change, §5) and the two helpers too small to
justify a module of their own (`_current_time`, `_ws_reconnect_delay`).

## 8. What testing the plugin path actually found

The extractions left `plugin_tools.py` new and battle-untested in anger, so the
install path was exercised end to end against **real plugins in a scratch home**
(`SIMPLE_AGENT_HOME`, the user's own `~/.agent` untouched): a native
`plugin.json` + `commands/` plugin, a Codex / Claude Code layout
(`.claude-plugin/plugin.json` + `commands/`), a Python plugin (`__init__.py`
with `register_slash_commands`), and an MCP-shaped one (`.mcp.json`), plus
uninstall. All four installed, hot-reloaded, and their commands ran; the
executable-content gate refused the Python plugin until it was approved, and the
identical retry then went through.

Three findings came out of it, and one of them is a claim this document has to
correct about *itself*.

### 8.1 A plugin could take a bare command name (`923a214`)

`markdown` commands are namespaced by the plugin, derived from the path
(`<plugin>:<stem>`). Python ones were registered under whatever key the author
returned, so a user plugin declaring `{"stats": handler}` took over the built-in
evolution plugin's `/stats`.

Measured, not assumed: right after installing it, `/stats` ran the user plugin;
in a **fresh process** the load order put evolution last and evolution won. So
the same command meant different things before and after a hot reload, and the
only notice was one line on the server console — which a person in a chat UI
never sees.

The rule now: **installed plugins are namespaced by the plugin; built-in plugins
keep their bare names**, because `/evolve` and `/generate-tool` are documented
user interface. A key that already carries `:` is left alone.

### 8.2 `simple chat "<line>"` had its own meaning for a slash (`3a4211e`)

`simple` (interactive) runs input through `CommandCoordinator`; `simple chat`
handed the text straight to the agent core. Two implementations of "what did the
user just say", and they had drifted: `simple chat "/demo-dsh:hello"` made the
model *improvise an execution* — it read the command name from its own prompt,
could not read the body, and reported success for a command that never ran.

`chat` now shares one pipeline with the interactive loop (`_command_coordinator`
+ `_run_cli_turn`). The merge is safe because the classifier already refuses to
swallow prose: `tests/test_commands.py` pins that pasted paths, grep hits and
diff lines reaching the model as text, and that typo'd names still get feedback.

### 8.3 A correction: the description mechanism was already there (`26a0046`)

The third finding — Python commands showing up in `/help` as bare names — was
diagnosed **wrongly at first**. The hypothesis was that `_command_metadata`
should fall back to the handler's docstring. It broke
`test_reload_validation_failure_rolls_back_catalog_and_router`, which asserts that
a markdown command's *registry metadata* is exactly what the file declared, and
the fallback pulled in the placeholder docstring the markdown path writes
(`Run /<cmd>`).

Measuring the real source showed that `CommandRouter.register_plugin_catalog`
already falls back to `inspect.getdoc(legacy_handler)`. The mechanism was never
missing: `/evolve` and `/stats` were bare because **those handlers had no
docstrings**. So the fix is data plus two tests — one pinning the mechanism (a
docstring becomes the description, and no docstring is honestly empty), one
pinning the data (every built-in command explains itself).

The lesson is the one this document keeps relearning: **the first hypothesis is
a guess until it is measured**, and a shared helper is the wrong place to put a
convention that only one caller follows.

