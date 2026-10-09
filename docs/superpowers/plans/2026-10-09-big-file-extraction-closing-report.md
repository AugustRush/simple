# Big-file survey refactor — closing report

**Scope:** the work on branch `refactor/service-kernel` that began with
[`2026-10-09-big-file-survey.md`](2026-10-09-big-file-survey.md) and ends here:
extracting the internals of the three modules the README roadmap named as the
next large piece.

**Result:** 29 commits, every cluster in the survey moved, and a failure set
byte-identical to the baseline after every single step. The three files went
from **12292 to 7223 lines** (−41%) with **eighteen modules** holding what left.

---

## The evidence, in one place

| Measure | Baseline | Now |
| --- | --- | --- |
| `agent/channels/web.py` | 4191 lines | **3204** (−987) |
| `agent/channels/feishu.py` | 2587 lines | **1952** (−635) |
| `agent/tools/builtin_tools.py` | 5514 lines | **2067** (−3447) |
| Total | 12292 | **7223** |
| Modules extracted | — | **18** (6131 lines) |
| Direct tests added for moved code | — | **32** |
| Full suite | 19 failed / 3712 passed | **19 failed / 3744 passed** |
| FAILED set vs baseline | — | **identical after all 29 commits** |

The pass count rose by exactly the tests added (32), which is the point: no
existing test was weakened to accommodate a move.

Every increment ran the full suite and diffed the FAILED set rather than reading
the pass count, because the pass count moves for benign reasons and the FAILED
set does not.

---

## What moved, in the order it moved

| # | cluster | file | commit |
| --- | --- | --- | --- |
| 1 | scheduler/workflow payload shaping (299 lines) | `web_payloads.py` | `d51d166` |
| 2 | the pure renderer half (250) | `feishu_render.py` | `385f1de` |
| 3 | the outbound half — `WebOutputSink` (595) | `web_sink.py` | `de9adb8` |
| 4 | the progress wordings (168) | `feishu_progress.py` | `6df57ac` |
| 5 | memory (84) | `memory_tools.py` | `2298a4b` |
| 6 | web fetch/search (235) | `web_tools.py` | `015d03a` |
| 7 | output housekeeping (33) | `output_tools.py` | `03971b3` |
| 8 | attachment leases + capacity (105) | `feishu_attachments.py` | `e74606a` |
| 9 | inbound parsing (70) | `feishu_parsing.py` | `2a1b64a` |
| 10 | signal tools (109) | `signal_tools.py` | `973e829` |
| 11 | step output (151) | `step_output_tools.py` | `7bd1ee1` |
| 12 | **scheduler + workflows (1302)** | `schedule_tools.py` | `00683a8` |
| 13 | files (256, incl. a property) | `file_tools.py` | `c1f89fc` |
| 14 | audio (98) | `audio_tools.py` | `0cfbb21` |
| 15 | context (151) | `context_tools.py` | `71135a9` |
| 16 | **shell (306)** | `shell_tools.py` | `61d582b` |
| 17 | plugins + user tools (442) | `plugin_tools.py` | `4de794f` |
| 18 | download naming + typing (24) | `feishu_files.py` | `2e6a543` |

---

## The shape of the work

**Boundaries were measured, not assumed.** Every move began with an AST pass
that answered two questions: which module-level names does this cluster read, and
which of its members are read from outside. That is how `WebOutputSink` was
found to have six helpers with zero outside readers (§4.4), how `ScheduleTools`
turned out to need exactly five callables and not the host (§4.13), and how
`PluginTools` turned out to need exactly one (§4.18).

**Two clusters could not be split.** The survey measured that the workflow
methods read five of the scheduler's helpers and eighteen `agent.scheduler`
names, so scheduler + workflows moved as one 1302-line piece.

**Two moves added the guard they were missing.** `feishu_progress` (16 tests)
and `context_tools` (11) had no direct tests at all — only indirect coverage
through other paths. Moving code whose rules are held up by accident, and
leaving them held up by accident, is not preserving behaviour.

---

## The defect the series found in itself

The most valuable single finding is in **§4.14**. `ToolRegistry.call` installs
the calling registry's merged context on *the registry the tool implementation
reads*, and it finds that registry with:

```python
owner = getattr(fn, "__self__", None)
owner_registry = getattr(owner, "registry", None)
```

Once a method moves into a cluster object, `fn.__self__` is the cluster — and
six of the extracted clusters stored the registry as `_registry` or not at all.
`owner_registry` became `None`, and **the merge silently stopped happening**: a
session's own `workspace_root`, `output_dir`, `file_service` and `write_scope`
stopped reaching the tools.

The unit tests could not see it — they call a tool through the same registry it
was registered on, where no merge is needed. The **functional layer** caught it,
as a file landing in the composition's `output/` instead of
`web/sessions/<id>/output/`. The fix is a rule for every future extraction:

> **A cluster whose methods are registered as tools must expose a public
> `registry`.**

All seven affected clusters now do, and each `__init__` says why it must stay
public.

---

## Mistakes, and what caught them

Recorded because the pattern is the useful part — **a batch edit over code with
two shapes will handle one of them.**

- **Receiver-strip on a class target** (memory): the first attempt de-indented
  class methods to module level; an AST guard written for the purpose reported a
  class with only `__init__`.
- **The verbatim move copies the body, not the names it stands on** (memory):
  `clear` read `_active_sink`, a module global of its *old* home. The executor
  turned the `NameError` into an ordinary tool error, so `memory_clear` answered
  `ok=False` — caught by that tool's own test. The same class of miss recurred
  in the scheduler move (`_clip` and three `RUN_*` bounds), where a free-name
  scan caught it *before* the suite.
- **A guard that counted the wrong node type** (memory): the receiver check used
  `ast.FunctionDef` and missed `async def clear` (`AsyncFunctionDef`).
- **Patch targets that name a module path** (web, shell, plugins): three separate
  moves had to move `monkeypatch.setattr` targets and instance-level patches
  along with the code — including three that used `raising=False`, which would
  have become **silent no-ops**. That one was verified by measurement rather
  than assumed (§4.9).
- **A rename regex that skips `self.`** (feishu, twice): both times the tests
  caught it within a minute.
- **Seven import-trimming mistakes in one script** (shell): removing
  `from __future__ import annotations` turned a pre-existing lazy annotation
  (`-> SchedulerStore`, a type that was never imported) into a `NameError`.
- **Two off-by-one boundaries** (scheduler, plugins): both caught by asserting
  on the *next* construct rather than on the block's last line.
- **A scan that was measured before it became advice** (§4.6): the naive
  free-name scanner produces 452 findings across `agent/`, so the suggestion to
  turn it into a repository guard was withdrawn rather than shipped.

---

## What is deliberately not done

**`_register`, 1656 lines.** It mounts forty-odd tools onto the cluster objects.
The survey's answer is that the extraction-friendly form is each cluster owning
its own registrations — a **design change, not a move**. Every increment in this
series is behaviour-preserving by construction; a registration refactor is where
"which tools does this deployment have" would become a question with a different
answer, so it should be its own piece of work with its own survey.

**`_current_time`** (21 lines) and **feishu's `_ws_reconnect_delay`** (12 lines)
are small enough that moving them alone would create a module with less
justification than the ones it joined. `_current_time` has only ever sat next to
the web cluster; the back-off belongs to the channel's reconnect loop, which has
not been split.

**feishu's client-bound clusters** (`FeishuChannel`'s connection, event routing
and download path) were never in scope: the survey's §2 kept them behind the
pure layers precisely because they are the part that needs the SDK.

**The Profile/Bundle layer** from the previous closing report is still not done,
and remains what it was: a new functional layer, not a refactor.

---

## What a reader should take from this

1. **Measure the boundary before moving it.** Every extraction in this series
   began with an AST pass, and twice that pass changed the plan (the six helpers
   with no outside readers; the five callables and not the host).
2. **Move the guard with the code, and add one when there was none.** 32 tests
   exist now that did not, all on moved code.
3. **Diff the FAILED set, not the pass count.** The pass count moved by exactly
   the tests added; the FAILED set never moved at all.
4. **When a batch edit has two shapes, it will handle one.** The mistakes above
   are not carelessness — they are what a rename looks like when the same
   concept arrives as an instance method and a `@staticmethod`, or as an
   `ast.FunctionDef` and an `AsyncFunctionDef`.
