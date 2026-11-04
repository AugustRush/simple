# Simple — Personal AI Agent

A personal AI agent with memory, tool calling, multi-agent orchestration,
scheduled workflows judged by their own acceptance checks and by the work
products they declare, skills, plugins, and multi-channel delivery.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- At least one configured model provider
- Node 18+ — only to build the React frontend; the packaged bundle is already in
  the repo, so running the gateway needs no Node

A provider is a **named entry in `providers`** — the name is yours. What decides
how it is spoken to is its `api_format`, of which there are exactly two:

| `api_format` | Client | Used for |
|---|---|---|
| `anthropic` | `anthropic.AsyncAnthropic` | Anthropic API |
| `openai` | `openai.AsyncOpenAI` | OpenAI, and any OpenAI-compatible `base_url` — DeepSeek, Qwen/DashScope, Groq, Together, vLLM, Ollama |

The setup wizard writes five of them (`anthropic`, `openai`, `deepseek`,
`ollama`, `other`), but those are just names it chose; anything that speaks the
OpenAI wire format is one `providers.<your-name>` entry with a `base_url`.

Per-provider keys: `api_format`, `api_key` (a literal, or `$ENV_VAR` to read the
environment), `base_url`, `default_model`, `models` (the list `/model` offers),
`supports_vision` (whether image attachments go to the model directly),
`max_tokens`, `context_window` (the input limit compaction is measured against).

## Quick Start

```bash
# Install dependencies
uv sync

# First run — interactive setup wizard
uv run simple
```

The setup wizard guides you through provider selection, API key configuration, and model choice. Config is written to `~/.agent/config.json`.

### Key capabilities at a glance

| Capability | How |
|---|---|
| **Intent-before-action** | Write/shell tools require the assistant to declare what it will do before executing |
| **Nothing gets created unasked** | Creators (`automation_create`, `emit_signal`) must quote the user's own words from this turn, or the call is refused |
| **Workflows** | Chain scheduled tasks into a graph, each step judged by its own acceptance check |
| **Declared work products** | `produces` names the files a task has to leave behind: the run is told where to write, the steps below are handed what actually exists, and a promised file that is missing fails the run on its own |
| **Run outcomes** | `succeeded` / `failed` / `unverified` / `skipped` — "we could not tell" is not "it failed" |
| **Signals** | `emit_signal` wakes whatever subscribed, so a step can follow another without a clock |
| **Unified event stream** | Every tool call, hook, and lifecycle fact is a replayable `RuntimeEvent` |
| **LLM retry** | Transient API errors (rate limit, 5xx) retried 3x with exponential backoff; a spent quota or empty balance is not retried |
| **Config validation** | Startup warnings for typos and invalid values — never blocks startup |
| **Named sessions** | `--name prod` for isolated session data with shared config by default |
| **One writer per agent home** | A second process on the same home is refused rather than allowed to corrupt it |
| **Plugin hooks** | 8 lifecycle hooks: prompt submit, tool matchers, command hooks, continue loop |
| **Vision** | Image attachments sent straight to the model when the provider sets `supports_vision` |
| **Memory you can see and correct** | The `#/memory` page lists what it remembers with provenance, and lets you edit, pin or delete an entry — and reject a belief |
| **Spend guard** | `budget.max_session_tokens` stops the loop before the next model call when a session is out of budget, and the page shows the same number beside the composer |
| **It can reach you** | A scheduled run that needs a person posts a macOS notification — the same runs the red badge counts, so the two cannot disagree |
| **Read-only connectors** | `connectors.email` gives the agent search and read over your own mailbox — no send, no move, no marking read, and no tools at all until you configure it |
| **Graceful shutdown** | Feishu drains pending messages before closing WebSocket |

### Measuring retrieval quality

Memory recall is the system's core value, so it has an instrument rather than
only a latency benchmark:

```bash
uv run python scripts/eval_retrieval.py            # recall@k / MRR by capability
uv run python scripts/eval_retrieval.py --compare  # diff against the pinned baseline
uv run python scripts/eval_retrieval.py --harvest  # seed a local set from your own store
```

It drives the production path (`ContextManager.rank_ltm_entries`) over a
labeled set in `tests/eval/`, and separates the two failure modes that look
identical from outside:

- **`candidate_recall`** — did stage 1 (FTS) even fetch the right entry? This
  is a hard ceiling; ranking cannot recover what was never retrieved.
- **`recall@k` / `mrr`** — given the candidates, did stage 2 rank it well?

What it found: the failures are stage 1, not ranking, and the fix is the
query rather than the algorithm. Memory search is **lexical** — it matches
words, not meanings — so one unmodified user question misses a memory that
says the same thing differently, or says it in the other language. Re-asking
fixes essentially all of it:

| | single query | with reformulation |
|---|---|---|
| `candidate_recall` | 0.696 | 1.000 |
| `mrr` | 0.609 | 1.000 |
| `cross-lingual` | 0.143 | 1.000 |
| `paraphrase` | 0.385 | 1.000 |

That is why `context_retrieve` takes **`queries`** (a list), not `query`: the
parameter shape makes reformulation the default path, which a sentence in a
tool description does not reliably achieve. `--multi-query` reproduces the
right-hand column. Treat it as an upper bound — the reformulations are
authored, so it shows what the approach can reach, not what a given model
will produce.

The set also carries cases with **no** right answer, so the instrument is not
one-directional: a run reports `false_positive_rate` and `avg_spurious_results`
alongside recall. Both are currently 0.5, which is the honest number for two
hand-written negatives and is the thing a "just return more candidates"
change would move.

`tests/test_retrieval_quality.py` guards the pinned baseline in CI. Re-pin
with `--save-baseline` after an intentional change, and read the tag
breakdown first.

## Current State & Roadmap

**The architecture is a plugin kernel.** `agent/kernel/` is the whole core — Context, Plugin,
Service, Event, Scope, Lifecycle — and everything the agent is made of (Agent, Model, Tool,
Session, Memory, Scheduler, Channels, Ralph, Orchestration) is a plugin or a service mounted on
top of it. The **composition is data**: a profile names the plugins, bundles and config layers a
deployment has, so "this deployment has no memory" is a sentence in configuration rather than a
branch in the wiring. A plugin's registrations are effects on a `ServiceScope` it receives from
`install()` and gets back on unload, the four event domains (`tool`, `turn`, `llm`, `session`)
are buses the *composition* provides, and `tests/test_kernel_boundary.py` enforces the boundary
structurally: the core may not import a domain.

**There is a functional layer, not only units.** `tests/test_functional_turns.py` drives the
real composition root and the real entry points — the CLI `chat` command, the web WebSocket,
scheduled runs, plugin hot-swap, sub-agents, approvals, MCP, Ralph — through a whole turn,
against a local OpenAI-compatible fake provider. No network; 43 paths in ~27s. It is built on
one rule: **a double must not be simpler than the thing it stands in for, in the way that
matters** — so the fake carries the real thing's failure modes (rate limits, an unpaid account,
a content rejection, a stream that ends without `[DONE]`, tool-call arguments split across
deltas) and not merely its shape.

**What this work found: twelve defects**, each with a measured failure and a guard that was
verified by putting the violation back. Most share a shape — *something belonging to a
composition left pointing at a process-wide default; a check that read "present" as "intended";
an interface with three answers squeezed into two; a vocabulary missing the spelling the real
world produces.* One of the twelve was a regression the refactor itself introduced, caught by
the same discipline. The full record, with the measurements and the shape of each finding, is
[`docs/superpowers/plans/2026-10-02-plugin-kernel-status.md`](docs/superpowers/plans/2026-10-02-plugin-kernel-status.md).
This work is on the `refactor/service-kernel` branch.

### Roadmap

| Item | State |
|---|---|
| Plugin kernel: core, composition-as-data, scopes, domain buses | **done** — [`agent/kernel/`](agent/kernel), guarded by `tests/test_kernel_boundary.py` |
| Recomposing a running process (a different memory backend, a different profile) | **done** — saving the config, or `POST /api/composition/reload`, rebuilds in place: quiesce (409 while a turn runs), drop the session runtimes, unmount the parts (the channels stay), remount, rebind, bump the revision. Proven by swapping `LTMStore` → `SqliteFtsStore` in a live gateway and answering the next turn against it |
| Functional layer over the real entry points | **done** — 43 paths, [`tests/test_functional_turns.py`](tests/test_functional_turns.py) |
| `simple chat` and MCP | **done** — the one-shot command waits for the background connect (bounded by `mcp_connect_timeout_seconds`) and says so when the wait runs out; before, its own teardown cancelled the connection and the model was offered every tool *except* the configured ones |
| Content-filter recovery | **done** — reads the provider's own `code` (`content_policy_violation`) first and its prose second; the phrase list alone missed the real rejection, and that predicate gates the only mechanism that *repairs* a refusal instead of reporting it |
| `LoopDeps.of(agent)` surface | **partly** — the six pure pass-throughs (`_output_room` and friends, each `return self._token_budget.X(...)`) are gone, and both routes (`of` / `from_services`) are now driven with a real `TokenBudget`; the route that had never been driven was **broken**. `of` still reads 22 attributes off the agent, 13 of them underscored — each either real logic or a documented test seam (the model-call seam, the recovery seam) |
| `agent/channels/web.py`, `feishu.py`, `agent/tools/builtin_tools.py` | **done** — surveyed first, as the roadmap asked, then extracted cluster by cluster: [`docs/superpowers/plans/2026-10-09-big-file-survey.md`](docs/superpowers/plans/2026-10-09-big-file-survey.md) is the survey and the per-move record, [`2026-10-09-big-file-extraction-closing-report.md`](docs/superpowers/plans/2026-10-09-big-file-extraction-closing-report.md) the closing report. **12292 → 7223 lines** across the three files, eighteen modules holding what left, and the full suite's FAILED set diffed against the baseline after each of the 29 commits. `web.py` 4191 → 3204 ([`web_payloads.py`](agent/channels/web_payloads.py), [`web_sink.py`](agent/channels/web_sink.py)); `feishu.py` 2587 → 1952 ([`feishu_render.py`](agent/channels/feishu_render.py), [`feishu_progress.py`](agent/channels/feishu_progress.py) — which also gained the 16 direct tests it never had — [`feishu_attachments.py`](agent/channels/feishu_attachments.py), [`feishu_parsing.py`](agent/channels/feishu_parsing.py), [`feishu_files.py`](agent/channels/feishu_files.py)); `builtin_tools.py` 5514 → 2067 ([`memory_tools.py`](agent/tools/memory_tools.py), [`web_tools.py`](agent/tools/web_tools.py), [`output_tools.py`](agent/tools/output_tools.py), [`signal_tools.py`](agent/tools/signal_tools.py), [`step_output_tools.py`](agent/tools/step_output_tools.py), [`schedule_tools.py`](agent/tools/schedule_tools.py) — 21 methods, −1413 lines — [`file_tools.py`](agent/tools/file_tools.py), [`audio_tools.py`](agent/tools/audio_tools.py), [`context_tools.py`](agent/tools/context_tools.py) — 11 new direct tests — [`shell_tools.py`](agent/tools/shell_tools.py) and [`plugin_tools.py`](agent/tools/plugin_tools.py)). The functional layer caught a defect the first six of those modules had introduced: a cluster whose methods are registered as tools must expose a public `registry`, or `ToolRegistry.call` cannot install the calling registry's merged context and a session's own workspace and output dir silently stop reaching the tools. What is left in `builtin_tools.py` is the host — the 1656-line `_register` table, the shared accessors and `_current_time` — and turning that table into per-cluster registrations is a design change, deliberately left as its own piece of work |
| Browser-side JavaScript | **covered, in two layers** — (1) `tests/test_web_route_contract.py` extracts every `/api/...` call the page makes from `frontend/src` and matches it against the real route table (74 call sites → 68 distinct endpoints, 0 mismatches): it catches an endpoint renamed or removed under the page, without a browser. (2) `tests/test_web_ui_e2e.py` launches a real gateway subprocess, loads the built bundle in headless Chromium and asserts that the shell renders, that a message is answered **in the DOM**, that a second route renders its own view, that navigation is clickable, and that nothing logged a JavaScript error — the failure the protocol tests cannot see. It needs `playwright` (~100MB), so it is an optional extra (`[e2e]`) and **skips, visibly**, when absent |
| Feishu channel | **not covered** — an external service; it shares the `Channel` abstraction with web, so most of the wiring is exercised, but the channel itself is undriven |
| Intermittent `tests/test_ralph.py` failures | **not reproduced** in ~130 attempts (including two concurrent full suites under load) and repaired where it could not be diagnosed: the two wall-clock assumptions that made a slow machine indistinguishable from a broken product are gone, and the failure messages now carry the observed facts |

## Examples

### Named sessions

`--name` selects a session for both the gateway and the CLI.  Each session has
its own data directory but shares the default agent configuration unless it
explicitly overrides it with its own `config.json`:

```bash
# Session "prod" — data in ~/.agent-prod/, config shared from ~/.agent/config.json
uv run simple gateway --name prod
uv run simple --name prod

# Session "dev" with its own config override
mkdir -p ~/.agent-dev
cp ~/.agent/config.json ~/.agent-dev/config.json
uv run simple gateway --name dev

# Default session
uv run simple gateway                # -> ~/.agent/
uv run simple                        # -> ~/.agent/
```

Each named session has independent memory, context database, scheduler,
staging, and skills; agent configuration (provider, model, system prompt,
channels) is shared from `~/.agent/config.json` by default.  A session only
uses its own config when `~/.agent-<name>/config.json` exists.

`/sessions` lists the discoverable sessions and their config source.  Sessions
are isolated worlds; cross-session interaction stays at the filesystem level
(read another session's data when needed) rather than keeping multiple live
working memories in one process.

Two different things share the word "session", and they are not the same shape:

| | Data home | Why |
|---|---|---|
| `--name prod` | `~/.agent-prod/` — a sibling of the default, picked by a person | Same machine, different worlds |
| Web session `abc123` | `<agent home>/web/sessions/abc123/` — under the *active* home | A browser tab's isolation, kept where it belongs |

**One live owner per agent home.** Several subsystems assume they are the only
writer of that directory — the staging partition is keyed by session id and the
CLI hardcodes one, memory index files and plugin approval state are
read-modify-write with no cross-process coordination — so rather than audit
every such site, the invariant is enforced at the entry points: a second
process on the same home is refused instead of being allowed to corrupt it. Use
`--name` (or `SIMPLE_AGENT_HOME=~/.agent-dev simple`) for a second instance;
`SIMPLE_ALLOW_MULTI_INSTANCE=1` is the explicit opt-out when you know what you
are doing.

### Feishu Gateway

```bash
# Install Feishu dependency
uv sync --extra feishu

# Start gateway
uv run simple gateway

# Production instance with Feishu
uv run simple gateway --name prod
```

Configure in `~/.agent/config.json`:
```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxxx",
      "app_secret": "xxxx",
      "group_policy": "mention",
      "streaming": true
    }
  }
}
```

### Web frontend

The gateway can also serve an HTTP/WebSocket API for a browser frontend.  The
web channel is just another channel, so it shares the same session machinery
as Feishu (one conversation id per chat thread).

The React source lives under `frontend/`. Development builds (`npm run build`)
stay in `frontend/dist/` and do not modify the Python package tree.

**The gateway serves the bundled copy, not your dev build.**
`agent/_builtin/web/dist/` is the canonical asset and is checked in;
`frontend/dist/` is gitignored and may be an older build, so it is never
allowed to silently override the package after a restart. To serve the dev
build while working on the frontend, set `SIMPLE_WEB_USE_SOURCE_DIST=1`. To
refresh the bundle that ships, run `npm run build:release` from `frontend/`.

```bash
# Install web dependency
uv sync --extra web

# Start gateway with the web channel enabled
# (opens the UI in your browser once the port is listening)
uv run simple gateway

# Do not open a browser
uv run simple gateway --no-open

# In a named session
uv run simple gateway --name prod
```

The browser is opened only when the gateway is attached to a terminal, so a
service, a CI job or a background command never puts a window on someone's
screen; `SIMPLE_NO_BROWSER=1` suppresses it too. The URL is printed either way.

Configure in `~/.agent/config.json`:
```json
{
  "channels": {
    "web": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 8787,
      "auth_token": "",
      "cors_origins": [],
      "max_active_sessions": 16,
      "session_idle_ttl_seconds": 900
    }
  }
}
```

Each Web session gets its own runtime home under `<agent home>/web/sessions/
<session_id>`, with independent context, memory, skills and tools — not a
`~/.agent-<id>` sibling. At most `max_active_sessions` are kept resident; when
one is full the least-recently-active *idle* session is evicted, and its state
is rebuilt from the provider checkpoint on the next message, so eviction costs
a replay rather than the chat. `session_idle_ttl_seconds` evicts idle sessions
eagerly. A session mid-turn is never evicted, under either limit.

The gateway serves a built-in React + Ant Design UI at
`http://127.0.0.1:8787/`:

```bash
uv sync --extra web
uv run simple gateway
# open http://127.0.0.1:8787/
```

The React UI has seven views — 主页 (the agent: a box and a ledger of what it did),
对话 (chat), 扩展 (plugins and skills), 记忆 (memory), 自动化 (scheduled tasks and
workflows), 自主 (proposals it wants to act on) and 设置 (settings) — plus a task
board panel, a command palette and streamed responses, all through the same
`/api/...` endpoints.  There is no separate 会话管理 page: threads live in the
sidebar, because a conversation is where work happened rather than a record to
manage.  Or use the API directly:

| Area | Method Path | Description |
|---|---|---|
| UI | `GET /` | Built-in React UI |
| UI | `GET /api/health` | Health check |
| UI | `GET /api/commands` | Commands available in the web channel |
| UI | `GET /api/context` | Context-manager statistics |
| UI | `GET /api/scheduler/health` | Scheduler liveness |
| Config | `GET /api/config`, `POST /api/config` | Read masked config / save config |
| Sessions | `GET`, `POST`, `DELETE` `/api/sessions` | List (in the current agent home) / create an id / delete several |
| Sessions | `PATCH`, `DELETE` `/api/sessions/{id}` | Rename (`{"title": "..."}`) / delete durable history |
| Sessions | `GET /api/sessions/{id}/state` | Durable state for one session |
| Sessions | `GET`, `POST` `/api/sessions/{id}/messages` | Recent messages / send one (non-streaming) |
| Sessions | `POST /api/sessions/{id}/cancel` | Cancel the turn in flight |
| Sessions | `DELETE /api/sessions/{id}/queue/{message_id}` | Take a queued message back before it runs |
| Sessions | `POST /api/sessions/{id}/attachments` | Upload an attachment |
| Sessions | `POST /api/sessions/{id}/workspace/pick` | Pick the session's project folder (an explicit read/write grant) |
| Sessions | `POST /api/sessions/{id}/task-guidance/dismiss` | Dismiss the task-setup hint |
| Sessions | `GET`, `PATCH` `/api/sessions/{id}/permissions` | Read / change this session's shell permission posture |
| Sessions | `DELETE /api/sessions/{id}/approvals` | Drop this session's standing approvals |
| Sessions | `POST /api/sessions/{id}/reveal` | Reveal the session's isolated agent home in Finder/file manager |
| Sessions | `WS /api/sessions/{id}/stream` | Streaming events: `stream_chunk`, `stream_snapshot`, `tool_start`, `tool_progress`, `tool_end`, `tool_blocked`, `status`, `confirm_request`, `attachment`, `subagent_event`, `workspace_changed`, `notification`, `info`, `error`, `heartbeat`, `turn_complete` |
| Files | `GET /api/files?path=…&session_id=…` | Download a file owned by a session (its home: `output/`, `uploads/`, plus the workspace folder picked for it) |
| Files | `GET /api/files?path=…&task_id=…&run_id=…` | Download the recorded output of a scheduled run |
| Files | `POST /api/fs/pick-directory` | Open the OS folder dialog |
| Schedules | `GET`, `POST`, `PATCH` `/api/schedules` | List / create / edit several at once |
| Schedules | `POST /api/schedules/preview` | Preview the next fire times for a spec |
| Schedules | `GET`, `POST` `/api/schedules/attention` | Runs waiting for a person / acknowledge them |
| Schedules | `GET`, `PUT`, `PATCH`, `DELETE` `/api/schedules/{task_id}` | One task |
| Runs | `POST /api/schedules/{task_id}/run` | Run it now |
| Runs | `GET /api/schedules/{task_id}/runs` | Run history |
| Runs | `POST /api/schedules/{task_id}/runs/{run_id}/retry` | Retry one run |
| Runs | `POST /api/schedules/{task_id}/runs/{run_id}/cancel` | Cancel one run |
| Runs | `POST /api/schedules/{task_id}/runs/{run_id}/acknowledge` | Acknowledge an alert |
| Runs | `GET /api/schedules/{task_id}/runs/{run_id}/output` | The run's recorded output |
| Runs | `GET /api/schedules/{task_id}/runs/{run_id}/artifacts` | List the run's artifacts |
| Runs | `GET /api/schedules/{task_id}/runs/{run_id}/artifacts/{path}` | Download one artifact |
| Workflows | `GET`, `POST` `/api/workflows` | List the chains / create one |
| Workflows | `PUT`, `DELETE` `/api/workflows/{workflow_id}` | Edit / delete a chain (its tasks are disabled, not erased) |
| Signals | `GET /api/signals` | Emitted signal names and how many tasks wait on each |
| Plugins | `GET /api/plugins`, `POST /api/plugins/{name}/toggle`, `DELETE /api/plugins/{name}` | List / enable-disable (`{"enabled": true/false}`) / uninstall |
| Skills | `GET /api/skills`, `POST /api/skills/{id}/toggle`, `DELETE /api/skills/{id}` | List / switch off / delete |
| Feishu | `GET /api/feishu/chats`, `POST /api/feishu/test` | List chats the bot can reach / send a test message |

`path` alone is never enough: the request must name an owner so the gateway can
prove the caller is entitled to the file. A request with no owner, an unknown
session, or a path outside that owner's sandbox is refused with `403 forbidden
path`.

`auth_token` is empty by default because the server binds to localhost.  Set it
when exposing the port beyond the local machine, and put the frontend behind
HTTPS with `Authorization: Bearer <token>` (or `X-Auth-Token`).

### Scheduling tasks

```bash
# Daily summary at 9 AM Shanghai time
uv run simple schedule daily morning-summary \
  --time 09:00 --timezone Asia/Shanghai \
  --prompt "Summarize yesterday's progress and list today's schedule"

# One-shot reminder
uv run simple schedule once deploy-reminder \
  --at "2026-05-05T16:00:00+08:00" --timezone Asia/Shanghai \
  --prompt "Check if the production deploy completed successfully"

# Every-30-minutes health check
uv run simple schedule interval health-check \
  --every 30 --unit minutes \
  --anchor-at "2026-05-05T00:00:00+08:00" \
  --prompt "Verify all services are healthy"

# Weekly, on the machine's own clock
uv run simple schedule weekly report \
  --day mon --time 09:00 \
  --prompt "Summarize last week's progress"

# Deliver to Feishu chat
uv run simple schedule daily standup \
  --time 09:00 --timezone Asia/Shanghai \
  --prompt "Generate standup notes from yesterday's activity" \
  --delivery-mode channel --chat-id ou_xxxxxx

# Manage tasks
uv run simple schedule list
uv run simple schedule show <task-id>
uv run simple schedule pause <task-id>
uv run simple schedule resume <task-id>
uv run simple schedule delete <task-id>
```

`--timezone` defaults to **this machine's zone**, never UTC — an unnamed zone
means the one you are sitting in. Pass an IANA name (`Asia/Shanghai`) when the
task has to fire at that hour regardless of where the machine is.

The CLI covers `once`, `interval`, `daily` and `weekly`. The scheduler itself
also understands `weekdays`, `monthly` and `signal` triggers; those, and chains
of tasks, are built through [`automation_create`](#workflows-a-chain-of-steps) or
the Automation page.

**A task is stored where you made it, but it runs where a clock is.** Creating a task
does not start a scheduler, so the reply says which clock will serve it: `runner:
"this-process"` when one is live here, `"external"` when the task is only queued for a
gateway or `agent scheduler`. Saying that at creation is the cheapest moment to say
it -- the asker is still there. `schedule_run` answers the same way, with three
distinct words: `started` (running here, with a run id), `woken` (a scheduler on
another loop took the request), `queued` (nothing is serving it yet).

### The Automation page: one list, one creator

The page used to greet you with two tabs -- 任务 and 流程 -- and two creators, which asked
you to classify your own request before filling anything in. It is one list now, because
it was always one thing:

- **A task is a chain of one step**, and a chain is several steps with edges. The list
  shows one row per thing you manage (`流程 · N 个步骤` or `任务`), with its next run, its
  last status and what needs a person. `任务 / 流程 / 全部` is a **filter** in the toolbar.
- **Creation is a conversation, not a form.** The page's one entry is **对话新建**: it
  opens a new session with the first sentence already in the composer (*要做什么 / 什么时候
  做 / 在哪个目录*). A form asked you to know the tool's vocabulary -- trigger types,
  permission profiles, and whether your request was "one task" or "a chain"; the agent
  knows all of that and can ask one question at a time. The knowledge lives in a
  **skill** (`agent/_builtin/skills/automation-manager/SKILL.md`), the capability in one
  tool (`automation_create`), and the page only lists and edits what exists.
- **One row, never two per step.** A chain's steps are tasks in the store (that is what
  makes the two shapes one), and the list derives from `GET /api/automations`, whose rules
  -- a step is never its own row, a chain's attention is its steps' -- live on the server
  in `agent/scheduler/automations.py` rather than in the page's merge code.

**Changing one is the same conversation.** The row's pencil (and the task drawer's
button) opens a session seeded with that automation's name and id; the agent calls
`schedule_update` / `workflow_update` with `intent`. The page therefore has no editor at
all: it lists, opens a read-only drawer (a task's run history; a chain's steps), and
starts conversations. The two inline editors and their ~1,200 lines of frontend state
went with it, along with the dead `POST /api/schedules/preview` they were the only caller
of. What still needs a person is exactly what a person is good at: saying what should be
different.

### Workflows: a chain of steps

A workflow is a graph of scheduled tasks where each step runs only after the
steps it depends on have **succeeded** — and "succeeded" is decided by that
step's own acceptance check and by the files it declared it would produce, not
by whether the model replied.

```text
You: 帮我建一条流水线：先抓数据，再清洗，最后生成周报发我

Agent: [automation_create]
       fetch    → 抓取原始数据     produces: ["data/raw.csv"]
       clean    → 清洗并落盘       depends_on: [fetch]
                                   produces: ["data/clean.csv"]
       report   → 生成周报          depends_on: [clean]
                                   produces: ["reports/weekly.md"]
```

```jsonc
// the shape behind that call
{
  "intent": "帮我建一条流水线：先抓数据，再清洗，最后生成周报发我",  // the user's words, verbatim
  "name": "weekly-report",
  "steps": [
    {"key": "fetch",  "name": "抓取原始数据", "action_type": "agent_task",
     "instruction": "…", "trigger_type": "daily", "time_of_day": "06:00",
     "produces": ["data/raw.csv"]},
    {"key": "clean",  "name": "清洗并落盘", "action_type": "agent_task",
     "instruction": "…", "depends_on": ["fetch"],
     "produces": ["data/clean.csv"]},
    {"key": "report", "name": "生成周报", "action_type": "agent_task",
     "instruction": "…", "depends_on": ["clean"],
     "produces": ["reports/weekly.md"]}
  ]
}
```

Three things are load-bearing:

- **`depends_on` is the trigger.** A step with upstreams must *not* carry a
  `trigger_type` of its own — its upstreams are what start it. Only an entry
  step (no upstreams) keeps a clock or a signal, which is why one graph can
  start from a schedule, from a person, or from anything else the scheduler
  already understands.
- **`produces` is the work product; `criteria` is the target;
  `verify_command` is the verdict.** `produces` names the files the step has to
  leave behind, as paths relative to its folder — the path *is* the name, which
  is why there is no separate one to drift. Declaring them is what tells the run
  where to write instead of leaving it to guess, what hands the steps below the
  resolved absolute paths of the files that exist, and what turns "the file is
  there" into a check nobody has to write: a declared file that is missing when
  the step ends fails that step on its own, and the steps below it do not run.
  `criteria` (what has to be true) goes into the run's system prompt — a
  scheduled run has nobody to ask, so a prompt with no stated target is judged
  only by whether the model replied, which it always does. `verify_command` is
  the machine-checkable half: a single low-risk command whose exit code decides,
  run in the step's folder, and the steps below it run only if it passed. An
  agent's opinion of its own work is not evidence, which is why that half exists
  at all. Nothing is inferred across them: a task that declares no products does
  not grow an implicit "check that the products exist", and a file being present
  is not a claim about its contents.
- **The graph is validated before anything is written.** A cycle, an upstream
  that does not exist, a product path outside the folder, or a step that can
  never be judged is refused *with the step named*, while somebody can still
  read the error.

`workflow_delete` stops the chain and disables the tasks it built rather than
erasing them, so their run history stays readable. The leftovers are then
ordinary tasks: `schedule_delete` removes any of them, and its history, when
nobody needs to read it any more.

Neither delete is how a *wrong* definition is fixed. `schedule_delete` takes the
task's runs with it — they are its history, and rows left behind would name a
task that no longer exists. A chain cannot be repaired step by step either:
deleting a live step is refused, so the only route left is rebuilding the whole
workflow, which is how the real scheduler ended up with **nine** step tasks
belonging to workflows that are gone (eight from one retired chain, one from a
probe) — disabled, pointing at a graph id nothing answers to, and with no
history left to explain them. `workflow_update` and `schedule_update` change a
definition in place: same task id, same run history, every edge intact, because
a step's id is what the steps below it subscribed to. The steps and tasks those
two drop are disabled, not deleted.

The **自动化** page in the Web UI has a 工作流 tab showing the same graph, and
its navigation entry carries a badge for runs that failed while the page was
closed — the whole problem being a failure nobody was looking at. Behind it is
`GET/POST/PUT/DELETE /api/workflows`.

### What a run's status means

Three questions are asked separately, because a field that answers two of them
answers neither: **did it run**, **did the work achieve what it was for**, and
**did the result arrive**.

| Status | Means |
|---|---|
| `succeeded` | Judged, met the bar, and the result arrived |
| `failed` | Judged and did not meet the bar — *or* the result did not arrive |
| `unverified` | The bar could not be evaluated: it was refused, timed out, or failed to start |
| `skipped` | Never started, because something above it in the chain failed |
| `cancelled` / `interrupted` | Terminal, and deliberately not success |

Two rules hold it together:

- **Only one value means success.** `succeeded` is named explicitly and
  everything else defaults to "did not succeed", so a status invented later
  cannot accidentally read as a pass to the step below it.
- **"We could not tell" is not "it failed".** A check that never ran is
  evidence about the *check*. Calling it `failed` would stop every downstream
  step, raise an alert, and record "the work was wrong" when the truth is "we
  never looked" — so it gets its own status. Retries are opt-in
  (`retry_policy.max_attempts`, default `1`), and when a task does opt in only
  `failed` and `unverified` are retried: a `cancelled` run was cancelled on
  purpose. One failure is refused a retry even then — an error that says the
  provider account itself cannot serve requests (spent plan quota, empty
  balance). The backoff is in seconds and the quota window in hours, so the
  second attempt can only spend a whole step to prove it fails the same way.

A run can also carry the agent's own verdict on its work, and the combination
is deliberately asymmetric: **either source can fail the run, and both must
pass for it to pass.** An agent saying "done" is not evidence that a command
which never executed would have agreed, so a self-report can only ever *lower*
the verdict. That is what `report_outcome` is for — declaring that the job
could not be done (*the data source is gone, a required file is missing, the
inputs contradict each other*) instead of writing a plausible answer anyway.
It records the run as failed with your reason, does not start the steps below
it, and tells a person. It is never a way to report success.

### Signals: following without a clock

A signal is the other way to start a step. `emit_signal` announces that
something happened — `report.ready`, or `task:<task_id>:succeeded` — and any
task created with `trigger_type: signal` and that name runs. The
`task:<id>:succeeded` / `:failed` / `:cancelled` family is emitted by the
scheduler itself, so following a task needs no call at all.

```text
You: 抓完数据后通知下游

Agent: [emit_signal name="report.ready" intent="抓完数据后通知下游"]
```

**Subscribe before you emit.** A signal with nobody waiting is recorded and
closed as unmatched, and it will not start a run for a subscriber created
afterwards. `list_signals` shows which names have been emitted recently and how
many tasks are waiting on each, so a subscription can be matched to a real
emission instead of a guess.

A step that needs to read what an upstream step actually produced — not the
one-line summary already in its context — uses `read_step_output`, addressing
the step by its `key`. Only steps it depends on, directly or transitively, are
readable, and only outputs the scheduler itself recorded, so it cannot be used
to read arbitrary files.

### Growing a capability at runtime

The kernel is 1,826 lines of 96,492 -- the small-core claim is measurable. What
matters for a *personal* agent is the other direction: it should be able to grow the
capability it needs instead of waiting for a release. Five kinds are creatable by the
model at runtime, and four take effect without a restart:

| Kind | Tool the model calls | When it is usable |
|---|---|---|
| Tool | `create_tool` / `update_tool` / `delete_tool` | the next turn, in this session |
| Skill | `create_skill` / `update_skill` / `write_skill_file` | the next turn (catalog reloads, prompt is republished) |
| Scheduled task | `automation_create` (no `steps`) | the next scheduler poll (≤ 30 s) |
| Workflow (chain) | `automation_create` (with `steps`) | the next trigger tick |
| Plugin | `install_plugin` (git / https / local path) | the same call — the catalog hot-reloads |

Two knobs set how much freedom that is:

- **`user_tools.enabled: true`** trusts `~/.agent/tools` wholesale: the agent writes a
  tool and activates it without asking. Off (the default), each new file stops at a
  hash approval in the conversation, and editing the file revokes the approval.
- **`install_plugin`** asks only when the plugin *ships code*; a plugin that is pure
  declarations (skills, commands, an MCP server) installs unattended.

**A skill the agent made is freely editable; one a person wrote is not.** A `SKILL.md`
is pasted into the system prompt, so rewriting it rewrites the agent's own
instructions. The boundary is drawn on *ownership*, not on the verb: `create_skill`
stamps `created-by: agent` **and a digest of what it wrote**, and `update_skill` /
`delete_skill` / `write_skill_file` refuse to touch anything whose digest does not
match without a stated `intent`. That second half matters: a hand edit to an
agent-made skill changes the digest, so the bundle becomes somebody else's again --
and an *authorised* edit to a user's skill does not stamp a digest, so the protection
does not erode one permitted change at a time. Either way the refusal says whose file
it is and how to ask again.

What is deliberately **not** creatable is the layer that authorises creation: the tool
sandbox, the approval gate, the budget guard, the audit trail. A thing the agent can
rewrite cannot be the thing that authorises the agent. So `create_tool` runs the
module through an out-of-process import probe and then executes it in a sandboxed
child that cannot read `config.json`, the memory database or the session records --
while the tools directory itself *is* readable, because a tool that cannot read its
own source is a tool that never runs.

None of that was true before it was tested. The loop had three breaks with one shape:
a plugin is handed a **scoped view** of the registry (so its registrations are
undoable), and three places assumed the registry itself -- `create_tool` raised
`AttributeError` *after* writing the file and its approval, `user_tools_enabled` was
read from a context key nobody ever wrote (so the trust switch was decorative and
unattended creation was always refused), and the sandbox's extra read grants sat
before the home's deny, which last-match-wins overrode, so every user tool failed its
first call with `PermissionError`. The end-to-end test is
`test_the_agent_builds_a_capability_mid_conversation_and_uses_it_next_turn`: turn 1
asks for something the agent has no tool for and the model creates one; turn 2 calls
it over the same WebSocket.

**A reload re-reads the config, not only the directories.** Which plugins load -- an
entry's `enabled` flag, or the profile's entries -- is now applied live. Before this, a
reload re-discovered the plugin *directories* and ignored the `plugins` **section**, so
the page's on/off switch had to assign `catalog._plugin_config` by hand to make its own
save take effect; that private poke is gone, because the reload reads the file it was
asked to honour.

**A composition change is applied, not just saved.** The profile's `core` selection and
its `service_replacements` decide which core plugins are *mounted* -- a different memory
backend, a different session service. A plugin reload cannot do that, so it reports
`restart_required` instead of claiming it applied. Two interfaces can:

- **Saving the config** (`POST /api/config`) detects a composition change and rebuilds the
  process in place: it drops the session runtimes, unmounts the parts, re-reads the config,
  remounts, rebinds the channel, and bumps the revision -- so the very next turn runs
  against the new tree. This is the one a person uses, from the settings page.
- **`POST /api/composition/reload`** does the same on demand.

Both refuse while a turn is running (`409`, with the busy session ids): between unmounting
a service and mounting its replacement there is a window where it does not exist, and a
turn landing in that window fails as though data had been lost. The host plugins -- the
channel registry and the channels themselves -- stay mounted across a rebuild, because
they own the sockets the process is serving on: "recompose" must not mean "disconnect
everyone". A failed rebuild puts the previous profile back and reports whether that
worked (`restored`), rather than leaving a half-mounted tree that merely looks fine.

What is *not* dynamic: the process's own identity (its home, its `--name`, its event
buses). Those are what a restart is for.

### Autonomy: what the agent would do

The agent **acts on what is reversible and asks about everything else**. The line is
drawn by the tool, not by the model: a proposal whose action can only read runs without
waiting; one that can change something outside the workspace becomes a proposal on the
`自主` page and waits for a person. There is no configuration that moves an irreversible
action across that line.

It also has **no daily action cap**. The work is bounded by what the rules can see -- a
rule fires at most once per session per fact, a live proposal is not written twice, and
an answered one never returns -- so a cap would only decide which finite set of work to
skip, and would skip it by arrival order. Instead the pass **orders what it finds by
priority and works through it**:

| rank | rule | who is blocked |
|---|---|---|
| 0 | a queue that has not moved | a person, right now |
| 1 | a failure nobody read | nobody yet -- but it can repeat unseen |
| 2 | a task that stopped halfway | nobody; the next turn may pick it up |

An order, deliberately, not a score: weights nobody can justify are how two different
situations end up looking equally urgent.

```bash
uv run simple autonomy review   # the live proposals
uv run simple autonomy tick     # one pass, now
uv run simple autonomy stats    # the funnel's own numbers
uv run simple autonomy decide <id> --state declined
```

It also runs on its own, every ten minutes, inside the gateway — because its input is
*silence*: it looks for sessions nobody is in and failures nobody has read. A patrol that
only runs when a command is typed finds nothing, since typing is the opposite of silence.

**How a candidate is decided, and why in that order.** `auto` (reversible — the tool
declared `read` and nothing else) / `prepare` (irreversible, or a file write a person
should see — write the exact call that *would* have been made and wait) / `drop` (already
declined, or below the confidence floor). The checks can only make the answer more
cautious, and **reversibility is read from the tool's declared capabilities** — `read` /
`output_write` / `state_write` / `side_effect` / `shell` — never from prose a model wrote
about itself. The party being supervised does not grade its own homework. An undeclared
tool (an unknown name, a plugin nobody classified) is treated as irreversible: "no
capabilities" is not "declared safe". There is no configuration that makes an irreversible
action automatic.

**Permission, not a count.** A daily cap was tried and removed: it decided *which* finite
set of work to skip and skipped it by arrival order, while the set is already bounded by
the facts. So the admission answer is entirely about what the tool can change — reversible
runs, everything else is prepared — and the **calibration** comes from the numbers the
patrol keeps about itself:

```
最近 7 天巡逻 120 趟，看了 1440 个会话，跳过（有人在用 1300 · 太久没动 100），产出 3 条提议
```

A page (`自主`) shows the proposals, each with what it would do, why it would not just run,
the tool it would call, and the reversibility class; answering it is one click, and **a
decline is kept** — the same suggestion does not come back in new words, which is what
happens when "somebody already said no" lives only in a context window.

What the `自主` page shows is therefore the *irreversible* queue: the reversible ones
already ran and appear under `已答复 · 已执行` with what the run reported. The page can
still only write `accepted` / `declined` / `acted`; a proposal the executor refused stays
a proposal.

### Browser-level tests (optional)

```bash
uv pip install --python .venv/bin/python --extra e2e .
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/test_web_ui_e2e.py
```

They launch `python -m agent.cli gateway` as a subprocess (a real one, on a real port,
serving the built bundle), drive it with headless Chromium, and assert what only a
browser can: the page mounts, a reply reaches the DOM, a second route renders, and the
console stays clean. Without the extra they **skip**, which is the honest report — a
skipped test is visible in the summary, while a test that claims to check the page and
really checks an HTTP response is not.

### Creating a skill

```bash
# Skill "code-review" in ~/.agent/skills/code-review/SKILL.md
mkdir -p ~/.agent/skills/code-review
cat > ~/.agent/skills/code-review/SKILL.md << 'EOF'
---
name: Code Review
description: Review code changes for correctness, security, and style.
user-invocable: true
---

## Steps
1. Read the changed files with `read_file`
2. Check for: security issues, edge cases, error handling gaps
3. Format findings as a table: Severity | File | Issue | Suggestion
4. Summarize with an overall recommendation (approve / changes requested)
EOF
```

The skill is hot-reloaded. Next turn the agent will see it and can activate it:

```
You: /code-review Review my last PR changes
```

### Writing a plugin

Plugins are Python modules in `~/.agent/plugins/`. Minimal example:

```bash
mkdir -p ~/.agent/plugins/hello
```

**`~/.agent/plugins/hello/plugin.json`:**
```json
{
  "name": "hello",
  "version": "1.0.0",
  "description": "Greet the user on session start",
  "hooks": {
    "on_pre_tool": [
      {"matcher": "^shell$", "timeout": 5.0}
    ]
  }
}
```

**`~/.agent/plugins/hello/__init__.py`:**
```python
def register():
    return HelloPlugin()

class HelloPlugin:
    name = "hello"
    version = "1.0.0"

    def on_session_start(self, components):
        print("Hello! Plugin loaded.")

    async def on_prompt_submit(self, text, metadata):
        # Block messages containing secrets
        from agent.plugins.catalog import HookResult
        if "API_KEY" in text:
            return HookResult(action="block", message="Message contains secret")
        return HookResult()

    async def on_turn_end(self, event):
        from agent.plugins.catalog import HookResult
        if "error" in event.agent_response.lower():
            return HookResult(
                action="continue",
                message="The previous response contained an error. Please fix it."
            )
        return HookResult()

    def compose_system_prompt(self, current):
        return "Always sign your responses with: — your personal agent"

    def register_slash_commands(self):
        return {"hello": self._handle_hello}

    async def _handle_hello(self, raw_cmd, components):
        from agent.commands import CommandResult
        return CommandResult(response_text="Hello from slash command!")
```

Slash commands are routed through the same portable command layer on every
channel. Handlers keep the legacy `(raw_cmd, components)` signature: `raw_cmd`
contains the command name without `/` plus its arguments, while `components`
is a shallow per-invocation overlay containing the current session `ctx`,
`command_context`, `command_sink`, `channel_name`, and `session_id`. Mutating
this overlay does not mutate the shared component mapping.

For cross-channel output, return `CommandResult`. Existing handlers remain
compatible: a returned string is forwarded as the next model input, and
`None` means the command handled its side effects without a response. Both
synchronous and asynchronous handlers are supported; synchronous handlers use
bounded worker capacity so they do not block the async command loop. Saturated
or failed commands are converted to a stable error response rather than
escaping into the transport. Plugin reload replaces routed plugin descriptors
as one snapshot when the runtime supplies its command router, so added,
changed, and removed commands take effect together.

Plugin hooks:
| Hook | When | Can do |
|------|------|--------|
| `on_session_start` | Startup | Capture components (client, model, memory) |
| `on_prompt_submit` | Before agent sees message | Block, inject context |
| `on_pre_tool` | Before tool execution | Block tools (with matchers) |
| `on_post_tool` | After tool execution | Observe results |
| `on_turn_end` | After each turn | Continue loop, inject context |
| `on_session_end` | Shutdown | Score session, persist analytics |
| `compose_system_prompt` | System prompt build | Append behavior rules |
| `register_slash_commands` | Startup | Register /commands |

#### How a plugin's commands are named

A plugin does not own the bare namespace. Its commands are namespaced by the
plugin, so the name says who owns it and two plugins cannot claim the same word:

| the plugin declares | the command is |
|---|---|
| `commands/deploy.md` | `/myplugin:deploy` |
| `register_slash_commands() → {"deploy": handler}` | `/myplugin:deploy` |
| `register_slash_commands() → {"mine:deploy": handler}` | `/mine:deploy` (already qualified) |

Built-in plugins are the exception, because they are this app's own vocabulary:
`/evolve`, `/generate-tool` and `/stats` are documented here and keep their bare
names. Anything you install is reachable only under its own prefix — a user
plugin declaring `stats` used to take over the built-in `/stats`, and which one
won depended on whether it had been installed before or after the process
started.

`/help` lists every command with its description, taken from the command file's
`description` front-matter for a markdown command and from the handler's
docstring for a Python one — so write one.

#### Code that appears in your plugin directory

A plugin that runs code (a `__init__.py`, an MCP server, a hook command) is checked before it
is loaded, and the answer is about **content** rather than about the plugin's name:

- the **first time** this machine sees your plugin directory, every plugin is listed with what
  it would run and then recorded. That is a blessing of what is already there, printed rather
  than silent;
- after that, a plugin that was **already recorded** loads without a word, and one whose code
  **changed** — or one that simply appeared — does not load, and is reported with what it would
  have run;
- installing or importing through `install_plugin` / `import_dsh_profile` records the approval,
  so the supported paths keep working.

The reason a change stops rather than warns is that this cannot tell *you* editing your own
plugin from a sync tool, a package install or a clone updating files behind you: both are
"the content changed".

**If a directory is yours, say so**, and edits in it load without re-approval:

```json
{
  "plugins": {
    "trusted_paths": ["~/src/my-plugins"]
  }
}
```

A path is a statement about ownership, which is what an author needs; it is deliberately not
the same as "always allow whatever is at this address", which is a promise no URL can keep.

One honest limit: the record of what has been seen lives in `~/.agent/trust/`, a plain file in
the same home as the plugin directory. Anything that can write code into one can write a record
into the other, so this raises the cost of a generic tool dropping files into a conventional
directory — it is not a boundary against someone who can already write to your home.

### Memory management

```bash
# Browse memory
uv run simple memory index

# Read a memory entry
uv run simple memory show identity/user

# Search
uv run simple memory search "preferences"

# AI-assisted tidy (reorganize and deduplicate)
uv run simple memory tidy
```

In-session commands:
```
/memory     — memory export summary
/context    — LTM stats (categories, staged turns, idle time)
/sessions   — recent session history with scores
/session abcd1234  — details of a specific session
```

#### The memory page (`#/memory`)

The store is the system's core value, and until this page existed it had no
surface: you could ask the agent what it remembered and get whatever the ranking
felt like, and a wrong memory could only be argued with inside a conversation.
The page lists every entry with its category, entity, importance, source
conversation and age, and lets you change it:

- **Entries** — edit the text, category, entity or importance; pin an entry so
  retention no longer decays or archives it; delete it outright; or write a new
  one by hand. Adding goes through the same write path the agent's own memory
  tools use, so remembering something twice folds into the row that exists
  rather than becoming a second answer.

- **Facts** — the resolved beliefs, each with the evidence that produced it
  (source kind, conversation, confidence). They are deliberately not editable: a
  fact is an inference over an append-only ledger, and the next consolidation
  pass would overwrite a hand edit. The page offers **忘记** instead, which
  archives the assertions and drops the resolved belief, so the next evidence
  resolves the key from scratch. Rejecting a belief and editing a note are
  different acts, so they are different verbs.

An edit is a field-wise patch: fields the page did not send (`scope`, the
provenance, the timestamps) are left alone, which is what stops "fix the wording"
from also resetting the score or re-attributing the memory.

Two invariants are load-bearing and are tested directly
(`tests/test_memory_curation.py`):

- **A pin survives every rewrite of the row.** Consolidation re-states entries
  through `add_entry`, which replaces the whole row; a pin that pass can drop is
  a button that lies.
- **Provenance is read from the evidence, never invented.** A store that cannot
  join answers empty so the page can say "来源未知" instead of naming a
  plausible conversation.

Backed by three additions to the `MemoryStore` contract (`update_entry`,
`delete_entry`, `retract_facts`), so the second backend implements them too and
`run_store_contract` exercises both.

#### The spend guard

`max_steps` bounds iterations, and iterations are not money: one tool-heavy turn
on an expensive model can stay well inside its step budget and still cost
whatever it costs. `budget.max_session_tokens` is the missing ceiling.

```json
{ "budget": { "max_session_tokens": 200000 } }
```

- **Where it checks.** At the step boundary, next to cancellation -- before the
  model request, so a stopped turn never leaves a tool half-run or a request in
  flight. Checking once per turn would only notice after the money was spent.
- **What it stops.** The next model call. The user gets one sentence saying what
  was spent, what the ceiling is, and which key raises it (localized the same
  way cancellation is); the turn's status is `budget_exhausted`, not a fabricated
  error, and no `usage_events` row is invented for a call that never happened.
- **What it counts.** `input_tokens + output_tokens` for the session, from the
  same `usage_events` ledger the page reads. Background consolidation and
  sub-agents land in that ledger too, so "the session spent 200k" includes the
  work it did while you were not looking.
- **The page's number is the guard's number.** `GET /api/sessions/{id}/usage`
  answers what the loop consults, and the composer shows it (`12.3k / 200k`,
  with a thin bar that turns red at the ceiling). A notice above the composer
  explains why the next message will not be answered.
- **`0` means off.** The default. An unset budget is never read as "zero tokens
  left": the guard is opt-in, and a deployment that never configured one must
  behave exactly as it did before this existed.
- **A guard that cannot read is not an outage.** If the service cannot total its
  usage -- no accounting table, an unreadable one -- the turn keeps running
  without a ceiling. The check is a courtesy, not a failsafe.

The number is read through `ContextService.token_usage`, a service verb rather
than a store one, so the second context service answers it too and the loop never
learns where usage rows live.

#### Reaching you when no page is open

Every other channel in this system is *pull*: the 自动化 page, the red badge on
the navigation entry, the run history. All of them wait for you to come and look
-- which is exactly what you are not doing when a 07:00 task fails and you open
the laptop at 11:00.

A scheduled run that ends **failed**, **interrupted**, **unverified**, or that
reports earlier occurrences were skipped while nothing was running, posts a macOS
notification. That is the same population the red badge counts, defined once
(`status_wants_a_person`) and used by both, so the badge and the banner cannot
disagree about which runs are owed a reader.

```json
{ "notifications": { "desktop": false } }
```

- **On by default, and narrow.** The alternative is a scheduler that fails
  silently. A banner per successful run would be noise, and noise is how the one
  that mattered gets dismissed unread, so success is not announced.
- **The message is the run's own words** -- its error first, because that is the
  question a failed run raises. It is a pointer to evidence, never a verdict:
  the agent's self-assessment is not what this channel carries.
- **Text is passed as argv, not interpolated into the AppleScript source.** A
  task name or an error message containing a quote is ordinary; a quote that
  ends the string literal in the script is an injected command. `tests/
  test_desktop_notify.py` sends a hostile message through the real command line
  and asserts it stays one argument.
- **A notification cannot fail a run.** It is sent after the run is recorded,
  every failure path answers `False`, and a hanging `osascript` is killed after
  five seconds -- a run must not wait forever on a permission prompt.
- **Silent where there is no notification centre.** A headless Linux gateway
  gets `False` and no subprocess, not an error per run.

The runtime decides *when* a run wants a person (the injected `notifier` call in
`SchedulerService`); the composition decides *how* it reaches them. That split is
the same one `delivery` has, and it is what lets a test assert the wiring without
posting a banner.

#### Connectors: read-only, and absent until configured

MCP is the general answer for reaching a third-party service, and it is the right
one for anything that ships a server. It is the wrong answer for the two things a
*personal* agent needs most -- your mail and your calendar -- because neither has an
official server, and the usual workaround is a shell command with a password pasted
into it. A **connector** is that other shape: small, in-repo, and read-only.

```json
{
  "connectors": {
    "email": {
      "host": "imap.example.com",
      "user": "me@example.com",
      "password_env": "EMAIL_PASSWORD",
      "mailbox": "INBOX"
    }
  }
}
```

- **Nothing is connected today, and nothing needs to be** for the capability to
  exist: with no configuration, the connector registers **no tools at all**. A tool
  the model can see but that can only answer "not configured" is worse than no tool
  -- it invites the model to try, fail, and explain its own failure to you.
- **Read-only three times over**, because "the agent promised" is not an enforcement
  mechanism: the mailbox is selected with `readonly=True` (the server refuses
  writes), bodies are fetched with `BODY.PEEK` (nothing is marked as read, which
  would otherwise quietly change your mailbox), and there is no function in the
  module that could emit `STORE`/`COPY`/`MOVE`/`EXPUNGE`/`APPEND`. There is no
  send verb to add a permission to later.
- **The password comes from the environment.** A literal `password` in the config is
  detected and **refused** in the status line, not honoured -- the config file is
  what people paste into issues and commit to dotfiles repos.
- **Search is matched here, not by IMAP `SEARCH`.** IMAP's own search needs a
  per-server charset story for non-ASCII and is a Chinese query's worst case; instead
  one batched `FETCH` of the newest window of headers comes back in one round trip
  and the substring match runs locally, where "contains" means the same thing on
  every provider.
- **The surface exists even when unusable.** `connectors` is a provided service
  holding every connector with a status and a reason, so "why can't it read my mail"
  has a sentence for an answer rather than a traceback.

The plugin is `agent/connectors/plugin.py`; it declares `cfg` and `registry`, so
"What it contributes depends on configuration" is expressed by the only mechanism
that can express it. Tools are registered with `capabilities=("read",)` and nothing
else, which is what a read-only permission profile filters on.

### Multi-agent orchestration

```text
# Parallel — 3 independent reviewers
You: 让 3 个子 agent 分别从性能、正确性、可维护性 review 这次改动

# Pipeline — sequential dependency
You: 先让 researcher 收集事实，再让 planner 给出方案，最后让 critic 审查方案

# Rendezvous — multi-round debate
You: 让正方和反方分别给方案，互相回应一轮后，再收敛成最终建议
```

### Autonomous task loop (Ralph)

```text
You: /ralph "make all tests pass in this project" --max 15 --verify "pytest tests/"

# List tasks
You: /ralph list

# Resume interrupted task
You: /ralph resume abc123def456
```

### Evolution

```bash
# View scores and session history
uv run simple evolve --stats

# Let the agent rewrite its own system prompt from session feedback
uv run simple evolve --rewrite

# Apply the best-scoring prompt from history
uv run simple evolve --apply-best
```

### Model switching

```text
You: /model              # list available models
You: /model deepseek-chat  # switch session to DeepSeek
```

`/model` is a plain model id, and the model **carries its client with it**: one
table (`routing_table`, built from `providers`) decides which provider owns
which model, each call is dispatched to that provider's transport, and anything
absent from the table goes to the active provider. When the same model id
appears under two providers the active one wins — which is also what the model
dropdown offers, so the visible option and the routing agree.

This matters beyond a dropdown: memory consolidation, the session-end flush,
and the evolution engine make their own calls, and they resolve the same pair
rather than pairing the active provider's client with whatever model the config
named. A `context.consolidation.model` from another provider's group used to be
posted to the active provider's endpoint and rejected with
`400 The supported API model names are ...` — which is the shape of that bug,
not of a bad model name.

Sub-agents inherit the parent's routing table, so a sub-agent asked for a model
from another group reaches its owner's endpoint too.

### Working with MCP tools

**A server's lifetime is its own.** `mcp_reconnect` (the model's tool) restarts one
server or all of them; a plugin reload that removes a plugin stops the MCP child that
plugin declared, instead of leaving it running until the process exits. The process
belonging to one server is stopped by the task that started it -- the MCP SDK's stdio
session opens an anyio cancel scope, and a scope has to be exited by the task that
entered it -- so `disconnect` asks rather than cancels.

**Noticing a child that died is a different question, and it is opt-in.** The SDK reports
no exit, so a dead server looks exactly like an idle one until a tool call fails. Set
`mcp_health_check_seconds` to get a periodic ping per connected server:

```json
{ "mcp_health_check_seconds": 60 }
```

`0` (the default) means never. When it is on, a server that stops answering is
reconnected **once**, and then stays failed until something asks again -- `mcp_reconnect`
clears that mark, because asking is new information and a timer is not. Unbounded retries
on a permanently broken server are the failure mode this cap exists to prevent.


```json
{
  "mcp_servers": [
    {
      "name": "filesystem",
      "command": "npx",
      "args": ["-y", "@anthropic-ai/mcp-server-filesystem", "/path/to/allowed/dir"]
    }
  ]
}
```

MCP tools appear alongside built-in tools and are listed in `compose_system_prompt`. Plugins can also bundle MCP servers via `plugin.json` `mcp_servers` field.

---

## Configuration

Config lives at `~/.agent/config.json`. First run creates it automatically.

Config validation runs at startup — warnings are printed for unknown keys or invalid values, but the agent still starts with best-effort defaults.

```bash
# View current config
uv run simple config list

# List configured providers
uv run simple config models
```

Key config sections:

| Section | Purpose |
|---|---|
| `active_provider` | Which provider to use |
| `providers.<name>.*` | `api_format`, `api_key`, `base_url`, `default_model`, `models`, `supports_vision`, `max_tokens`, `context_window` (see [Requirements](#requirements)) |
| `model`, `max_tokens` | Top-level overrides for the active model and its output cap |
| `context.storage` | LTM category cap, decay factor |
| `context.consolidation` | Token ratio, keep-last-N, idle seconds, min messages |
| `memory` | Auto-tidy cadence: `tidy_interval_seconds`, `tidy_file_threshold` |
| `max_steps` | Steps (one model request plus the tools it calls) allowed in one turn; default 200, max 500. `max_tool_call_iterations` is the older spelling of the same bound and is still accepted |
| `max_truncation_continuations` | How many times a response truncated at the token cap may auto-continue; default 6 |
| `llm_max_retries`, `llm_retry_base_delay` | Retry count and backoff base for transient API errors; default 3 and 1.0s |
| `orchestration` | Sub-agent bounds: `max_parallel_agents`, `max_agents_per_turn` (0 derives it), `sub_agent_timeout_seconds`, `sub_agent_retries` |
| `channels.feishu` | Feishu bot credentials and behaviour (`app_id`, `app_secret`, `encrypt_key`, `verification_token`, `allow_from`, `react_emoji`, `group_policy`, `streaming`) |
| `channels.web` | Bind address, `auth_token`, `cors_origins`, `max_active_sessions`, `session_idle_ttl_seconds` (see [Web frontend](#web-frontend)) |
| `audio.transcription_command` | External STT argv-style command template (`{path}`, `{language}` placeholders; shell operators are rejected) |
| `mcp_servers` | MCP server definitions (name, command, args, env) |
| `plugins` | Per-plugin enable/disable (`{"evolution": {"enabled": false}}`) |
| `skills` | Per-skill enable/disable (`{"skill-manager": {"enabled": false}}`) |
| `user_tools.enabled` | Trust every Python tool in `~/.agent/tools/*.py`. Off by default; individually approved tools load either way (see [Authoring user tools](#authoring-user-tools)) |
| `evolution` | Enable/disable session scoring and rule learning |
| `scheduler` | Poll/lease settings (`poll_seconds`, `lease_seconds`) |
| `tavily_api_key` | Optional Tavily search API key |
| `web_proxy` | Proxy for `web_fetch` (e.g. `http://127.0.0.1:7897`). Required on machines whose DNS returns reserved addresses — Clash's `enhanced-mode: fake-ip` does this, and the direct path refuses such answers by design. `null`/absent = read `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` (honouring `no_proxy`); `none` = always connect directly. Credentials: `http://user:pass@host:port` |
| `output_dir` | Override default `~/.agent/output` |
| `file_access` | Startup-only workspace read/write policy plus resource limits for file tools (see [File access](#file-access)) |
| `permissions.shell_level` | Default shell permission level: `ask`, `medium`, `high`, or `full` (see [Shell permissions](#shell-permissions)) |
| `permissions.shell_sandbox` | OS sandbox mode: `restricted`, `read_all` (default), or `none` (danger-full-access, `full` level only) |
| `permissions.shell_secret_paths` | Extra home-relative paths the sandboxed shell may neither read nor write (e.g. `[".ssh", ".docker", ".kube"]`); extends the built-in secret set |
| `permissions.shell_devices` | Device/service access (Metal/IOKit) inside the sandbox; **default `true`** (set `false` for the strictest posture) |
| `shell_allowed_commands` | Persistent shell allowlist that skips confirmation (see [Shell permissions](#shell-permissions)) |
| `shell_blocked_commands` | Extra commands no permission level can run — the blacklist is not bypassable by raising the level |
| `assistant_identity` | Deterministic assistant name/role for fact recall |
| `system_prompt_file` | Load custom system prompt from `.md` or `.txt` |

A key starting with `_` is a documentation companion, not a setting: the example
config carries its explanation in `_<key>_readme` beside the value, and those
keys are never reported as typos. Any other unknown key is warned about once at
startup and ignored.

### File access

Built-in file tools are rooted and snapshot-based. Every operation takes an
explicit `root` (`workspace` or `output_dir`) and a root-relative `path`;
absolute paths, traversal, and symlink escapes are rejected. `read_file`
returns a bounded line window with an exact SHA-256 `revision`; `write_file`
and `edit_file` require that revision as `expected_revision`, so a stale write
can never silently overwrite newer content. Failed edits leave the target
byte-for-byte unchanged.

The workspace is read-only by default. To enable workspace writes, set
`file_access.workspace.write` to `true` in `config.json` and grant the target
paths through a sub-agent `write_scope`; `output_dir` is always readable and
writable for generated artifacts. The policy is loaded only at startup —
changing it requires a restart.

```json
{
  "file_access": {
    "workspace": { "read": true, "write": false },
    "max_read_lines": 400,
    "max_read_bytes": 65536,
    "max_snapshot_bytes": 16777216,
    "max_write_bytes": 4194304,
    "max_replacements": 100,
    "max_list_results": 1000
  }
}
```

Example round trip:

```text
read_file(root="workspace", path="agent/config.py")     # -> revision "sha256:..."
edit_file(root="workspace", path="agent/config.py",
          expected_revision="sha256:...",
          replacements=[{old_text, new_text, expected_count}])
```

### Shell permissions

Medium-risk shell commands (`rm`, `mv`, `ssh`, `curl`, interpreters, script
files, absolute paths) run automatically. Only **high-risk** constructs —
destructive commands/options (`mkfs`, `dd`, `shutdown`, `find -delete`),
shell operators (`;`, `|`, `&&`, redirection), and pipe-to-shell patterns —
ask the human, and they become runnable after approval.

The permission level and the OS sandbox are linked: the level decides what
asks for confirmation, and the sandbox decides what the command may touch.

Permission levels (most → least restrictive):

| Level | Low/medium risk | High-risk commands/options | Operators/patterns |
|---|---|---|---|
| `ask` (default) | auto | confirm | confirm |
| `medium` | auto | auto | confirm |
| `high` / `full` | auto | auto | auto |

> **You almost certainly do not need `shell_sandbox: none`.** The usual reason
> people reach for it is GPU access, and that is a different knob:
> `shell_devices` (default `true`) exposes Metal/IOKit inside the sandbox.
> Measured on macOS: PyTorch MPS and MLX both run under `read_all` with
> `shell_devices: true`, identical to unsandboxed, and both fail with
> `shell_devices: false`. Before disabling the sandbox, match the failure to
> its knob:
>
> | Symptom | Knob |
> |---|---|
> | GPU / Metal / MLX unavailable | `shell_devices: true` (already the default) |
> | Cannot write inside the workspace | an approved `write_scope` |
> | Cannot read a credential dir you need | remove it from `shell_secret_paths` |
> | Cannot read outside the workspace | `shell_sandbox: read_all` (the default) |
> | A tool nests its own sandbox (Chrome/Electron) | `--no-sandbox` / `ELECTRON_DISABLE_SANDBOX=1` |
>
> When the sandbox *is* off, the agent says so on every start and in
> `/permissions`. `none` is a task-scoped decision — prefer
> `/permissions sandbox session none` over the persistent form, so it expires
> with the session instead of outliving the reason you needed it.

Shell sandbox modes:

| Mode | Reads | Writes | Notes |
|---|---|---|---|
| `restricted` | System dirs + workspace/output only | Open by default; secrets, autostart, user data + workspace denied | Reads are the locked-down axis |
| `read_all` (default) | **Whole machine except secrets** | Open by default; secrets, autostart, user data + workspace denied | Local tooling just works; credentials stay unreadable |
| `none` | Everything | Everything | Danger-full-access: no OS sandbox, GPU/Metal reachable. **Only valid with `shell_level: full`** |

Writes are **open by default** — there is no per-tool allowlist (npm caches,
HuggingFace downloads, Chrome/Electron state, MCP servers and temp dirs all
just work).  The explicit denials name three asset classes, chosen by what an
attacker gains rather than by where the user files things:

**1. Secrets — denied for read *and* write.**  A write boundary does nothing
for a credential: the damaging act is reading it and shipping it out, and the
sandbox allows unrestricted network.  Covers `~/.aws`, `~/.azure`, `~/.gnupg`,
`~/.netrc`, `~/.git-credentials`, `~/.config/gh`, `~/.config/gcloud`,
`~/Library/Keychains`, `~/.claude.json`, and the agent's own home (its
`config.json` holds your provider API keys).

`~/.ssh`, `~/.docker` and `~/.kube` are **not** read-denied by default —
`git push` over SSH, `docker` and `kubectl` all need them, and a default that
breaks `git push` just gets switched off wholesale.  Add them when the
instance does not need those tools:

```json
{ "permissions": { "shell_secret_paths": [".ssh", ".docker", ".kube"] } }
```

**2. Later-executed code — write denied.**  Escaping a write sandbox never
means defeating seatbelt; it means leaving a line for the user's next login
shell to run.  Covers shell rc files (`~/.zshrc`, `~/.zshenv`, `~/.zprofile`,
`~/.bashrc`, `~/.profile`, `~/.config/fish`, …), launchd drop points
(`~/Library/LaunchAgents`, `/Library/LaunchDaemons`, …) and PATH directories
(`/usr/local/bin`, `/opt/homebrew/bin`, `~/.local/bin`, `~/bin`).

**3. User data — write denied.**  Documents/media (`~/Documents`, `~/Desktop`,
`~/Downloads`, `~/Movies`, `~/Music`, `~/Pictures`), personal library data,
the workspace unless a `write_scope` explicitly reopens it, and the agent's
internal bookkeeping.

GUI/rendering workloads (headless Chrome, Electron screenshots) receive the
generic system facilities App Store GUI apps get from `application.sb`
(process-local mach bootstrap, app-sandbox file extensions, preference reads).

> **What this sandbox is for.** It contains accidents and injected
> instructions, not a determined attacker who already has code execution.
> Reads outside the secret set stay open in `read_all`, and network egress is
> unrestricted — so a command that genuinely wants to exfiltrate something no
> list anticipated can. Use `restricted` when that matters.

The shell tool takes a `root` parameter (`workspace` by default, or
`output_dir`) and resolves relative `cwd` values inside that root. Project
commands therefore run in the selected workspace, like a coding agent opened
on that folder. Generated deliverables, downloads, attachments, and temporary
files should explicitly use `output_dir`; Web keeps that directory isolated per
session. When a call is scoped to `output_dir` and still creates new files
inside the workspace (for example via absolute paths), the tool moves them to
`output_dir/workspace-artifacts/` — this invariant holds even when the OS
sandbox is disabled.

Selecting a project folder in Web is an explicit, session-scoped read/write
grant for that folder. The selection and access mode are stored in the session
manifest and restored after a gateway restart. Global skills, plugins, tools,
and configuration still come from `~/.agent`; conversation state, attachments,
runtime logs, and generated output remain in the session home.

One limitation is architectural: seatbelt cannot nest — a tool that installs
its own OS sandbox (headless Chrome, Electron) must disable it
(`--no-sandbox` / `ELECTRON_DISABLE_SANDBOX=1`) or run unsandboxed
(`shell_sandbox: none` with `shell_level: full`).

Device/service access (Metal/IOKit — GPU, local ML) is open by default inside
the sandbox, the same posture the profile already takes for network.  The
seatbelt profile opens the Metal/IOKit services (the same mechanism App
Store sandboxes use) while reads stay open and writes stay scoped:

```json
{
  "permissions": { "shell_level": "ask", "shell_sandbox": "read_all", "shell_devices": true }
}
```

With this configuration the TTS skill's local generation works end-to-end
inside the sandbox (verified on macOS), and high-risk commands still ask for
confirmation.  Set `shell_devices: false` if you want to deny device access
while keeping file reads/writes as configured.

Runtime commands:

| Command | Effect |
|---|---|
| `/permissions` | Show the effective level/sandbox, config default, and any session override |
| `/permissions <level>` | Persist the config default and apply it immediately (survives restart; sub-agents inherit) |
| `/permissions session <level>` | Override the level for this session only |
| `/permissions sandbox <mode>` | Persist the default sandbox mode and apply it immediately (`none` requires `full`) |
| `/permissions sandbox session <mode>` | Override the sandbox mode for this session only |
| `/permissions default <level\|sandbox <mode>>` | Explicit alias for the persistent form |
| `/permissions reset [level\|sandbox]` | Restore the built-in defaults (`ask` / `read_all`) and clear session overrides |
| `/auto-approve on\|off\|status` | Persist the `medium` / `ask` shortcut |
| `/auto-approve session on\|off` | Apply the shortcut to this session only |
| `/allow <command>` | Persistently allow one command (exact string) or command name (all invocations) |
| `/deny <command>` | Remove an entry from the persistent allowlist |
| `/confirm <token>` | Approve one pending confirmation explicitly |

Approval UX: in an interactive terminal a numbered menu appears
(`1) 批准执行` / `2) 拒绝`, also accepts `y`/`n`/`同意`/`拒绝`); in gateway
channels (e.g. Feishu) the agent shows the exact command and the user replies
"同意" (or uses `/confirm <token>`).

Config example:

```json
{
  "permissions": { "shell_level": "ask", "shell_sandbox": "read_all" },
  "shell_allowed_commands": ["mkfs /dev/disk0", "osascript"]
}
```

An entry containing a space matches that exact command; a bare name (like
`osascript`) allows every invocation of that command. The allowlist and the
config default apply to sub-agents spawned later; a `session`-scoped
`/permissions` override applies only to that session and is lost on restart.
Permission changes made through slash commands persist and take effect
immediately; editing `config.json` by hand takes effect at the next startup.

For true machine-wide execution (local GPU/MLX workloads, arbitrary home
directory access), set `"shell_level": "full"` **and**
`"shell_sandbox": "none"`:

```json
{
  "permissions": { "shell_level": "full", "shell_sandbox": "none" }
}
```

This disables `sandbox-exec` entirely for shell commands — the agent can then
read and write anything on the machine, exactly like your own terminal.
`none` is refused unless the level is `full`, and the `shell_blocked_commands`
blacklist plus structural guards (cwd escapes, command substitution, parse
failures) still apply.

Unconditional guards that no level can bypass: the `shell_blocked_commands`
blacklist, cwd escapes (`cd`/`pushd` inside a command), command substitution
(`` ` ``/`$()`), and commands that cannot be parsed safely.

## Usage

### Interactive mode

```bash
uv run simple
```

### Where a long-running process keeps its trail

`simple gateway` and `simple scheduler` run for days, so they also write to a rotating
file under the agent home:

```
~/.agent/logs/gateway.log      # 5 MB per file, 3 generations
~/.agent/logs/scheduler.log
```

The path is printed at startup next to the Web UI address. The console keeps writing the
same lines; the file is what survives closing the terminal, a launchd/systemd start, or
this morning. Three properties are deliberate: the handler is attached **once** (a second
gateway start in the same process does not double every line), it is **bounded** (an
unrotated log is how "I have logs" becomes "I have a full disk"), and a logging failure
**never stops the process** -- an unwritable home yields a warning, not a dead gateway.

### Single-turn chat

```bash
uv run simple chat "Summarize this repository"
```

### Named sessions

Run a named session with `--name`:

```bash
uv run simple gateway --name prod    # data -> ~/.agent-prod/
uv run simple gateway --name dev     # data -> ~/.agent-dev/
uv run simple gateway                # data -> ~/.agent/ (default)
```

Each session has independent memory, context database, scheduler, staging,
skills, and plugins.  Agent configuration is shared from `~/.agent/config.json`
by default; place a `config.json` in the session directory to override it.
`--name` also works on the CLI and scheduler:

```bash
uv run simple --name prod
uv run simple scheduler --name prod
```

### Feishu Gateway

Connect to Feishu/Lark bot via WebSocket long connection:

```bash
# Install Feishu dependency
uv sync --extra feishu

# Start gateway
uv run simple gateway
```

Or install globally:

```bash
uv tool install --reinstall --editable . --with lark-oapi
simple gateway
```

### Web channel

Serve the HTTP/WebSocket API for a browser frontend:

```bash
uv sync --extra web
uv run simple gateway
```

See [Web frontend](#web-frontend) for the endpoint table and configuration.

### Scheduler service

```bash
uv run simple scheduler
```

### Scheduling tasks

```bash
# Daily
uv run simple schedule daily morning-recap \
  --time 09:00 --timezone Asia/Shanghai \
  --prompt "Summarize yesterday's progress"

# Once
uv run simple schedule once reminder \
  --at "2026-05-03T14:00:00+08:00" \
  --prompt "Check the deploy status"

# Interval
uv run simple schedule interval health-check \
  --every 30 --unit minutes \
  --anchor-at "2026-05-03T00:00:00+08:00" \
  --prompt "Verify all services are healthy"

# Weekly (`--time` and `--timezone` default to this machine's clock)
uv run simple schedule weekly report \
  --day mon --time 09:00 \
  --prompt "Summarize last week's progress"

# Manage
uv run simple schedule list
uv run simple schedule show <id>
uv run simple schedule pause <id>
uv run simple schedule resume <id>
uv run simple schedule delete <id>
```

The four CLI verbs cover `once`, `interval`, `daily` and `weekly`; the scheduler
also understands `weekdays`, `monthly` and `signal`, and chains of tasks —
see [Workflows](#workflows-a-chain-of-steps).

### Evolution

```bash
uv run simple evolve --stats        # Show RL statistics
uv run simple evolve --rewrite      # Generate improved system prompt
uv run simple evolve --apply-best   # Apply best-scoring prompt from history
```

### Memory

```bash
uv run simple memory ls                  # Memory export summary
uv run simple memory index               # Show memory JSONL projection
uv run simple memory show identity/user  # Read a memory entry
uv run simple memory search "preferences" # Search across all memory
uv run simple memory tidy                # AI-assisted memory reorganization
```

## Interactive Commands

Commands are handled by a shared runtime coordinator and work across all
channels unless marked otherwise.

In the interactive CLI, typing a `/`-command opens a live command palette
right in the input line: a bare `/` shows every command, and each extra
character narrows the list (e.g. `/p` → `/permissions`, `/plugins`),
updating on every keystroke. Use `↑`/`↓` to move, Enter to run the
highlighted command, `Tab` to complete, and Esc to dismiss. Typing `/` and
pressing Enter (instead of a longer command) opens the full browse menu;
commands with fixed options (e.g. `/permissions`, `/auto-approve`) show a
second menu for the argument there.

In a real terminal the interactive CLI runs in a full-screen layout: the
conversation, tool traces and markdown stream in the upper pane, while the
input line stays docked at the bottom of the terminal and never scrolls
away. Your submitted input is echoed into the pane in the same `›` style as
the classic prompt, so the conversation reads exactly as before. The
slash-command palette and permission menus all work from that bottom line;
the output pane scrolls through the whole conversation with `↑`/`↓` (when
the input is empty), `PgUp`/`PgDn` or the mouse wheel, and `Home`/`End` jump
to the top or newest message. While a turn is running, typing still works:
Enter queues the message (shown as `⏎ 已排队…` in the pane) and it is sent
when the turn finishes, and `Ctrl+C` cancels the running turn. Rendering is
incremental, so long conversations do not slow the input down. `Ctrl+D`
exits. When stdin/stdout are not a terminal (pipes, scripts, tests), the CLI
falls back to the classic line-by-line prompt automatically.

### Shared (all channels)

| Command | Description |
|---|---|
| `/help` | Show commands available in this channel |
| `/memory` | Memory export summary |
| `/context` | Long-term context statistics |
| `/compact` | Compress this session's context, keeping long-term memory |
| `/clear` (`/reset-context`) | Drop this session's context; long-term memory is untouched |
| `/workspace [path]` (`/cwd`) | Show or change the session's working folder |
| `/sessions` (`/history`) | List named sessions and recent scored history |
| `/session <id>` | View session details by ID prefix |
| `/tools` | List available tools |
| `/skills` | List available skills |
| `/plugins` | List loaded plugins |
| `/model [name]` | Show or switch the session model |
| `/permissions [<level>\|sandbox <mode>\|session <level>\|default …\|reset …]` | Show or set the shell permission level and sandbox mode |
| `/auto-approve on\|off\|session on\|off\|status` | Shortcut for high-risk auto-approval |
| `/allow <command>` | Add a command to the persistent shell allowlist |
| `/deny <command>` | Remove a command from the persistent shell allowlist |
| `/confirm <token>` | Approve one pending restricted shell command |
| `/export` | Export the current session to Markdown |
| `/ralph <goal> [--max N] [--verify "cmd"]` | Start a Ralph task |
| `/ralph list` | List all Ralph tasks |
| `/ralph resume <id>` | Resume a paused Ralph task |
| `/cancel [graceful]` | Cancel the current operation |
| `/cancel <new task>` | Cancel and queue a new task |
| `/now <message>` | Send an urgent interjection |

### CLI only

| Command | Description |
|---|---|
| `/` | Open the interactive command selection menu |
| `Tab` | Complete the `/`-command currently being typed |
| `/quit` (`/exit`, `/q`) | Exit the CLI |
| `/open <path>` | Open a file or directory with the default app |
| `/reveal <path>` (`/finder`) | Reveal a file in the system file manager |
| `Ctrl+C` | Interrupt a blocking operation (force cancel) |

### Feishu only

| Command | Description |
|---|---|
| `/send <path>` | Send a file from the output directory |

### Plugin commands

Plugins contribute additional slash commands at startup. Common ones include
`/evolve` and `/generate-tool` from the built-in evolution plugin.
`/generate-tool <description>` writes a user tool, verifies it, and — after you
approve it — activates it in the running session; see
[Authoring user tools](#authoring-user-tools).

`/help` is generated from the live descriptor set and automatically reflects
which commands are available in each channel.

### Cancellation behaviour

- **CLI:** `Ctrl+C` force-cancels the current LLM request and terminates child
  processes immediately. `/cancel` has the same effect.
- **Feishu:** `/cancel` arrives as an asynchronous message. A same-chat `/cancel`
  reaches the active turn at the next coordinator boundary even when another
  operation is blocking that chat.
- `/cancel graceful` requests cooperative cancellation at the next safe
  tool-loop boundary. A subsequent force `/cancel` upgrades it.
- `/cancel <new task>` force-cancels and starts the supplied task.

### Plugin command return contract

Plugin command handlers may return a `CommandResult`, a string (treated as
forward text), or `None` (side-effect only). Portable plugins use
`CommandResult` with explicit `response_text` to avoid coupling to a specific
output sink.

## Built-in Tools

| Group | Tools |
|---|---|
| Time | `current_time` |
| Shell | `shell` |
| Files | `read_file`, `write_file`, `edit_file`, `list_files`, `send_file` |
| Media | `transcribe_audio` |
| Memory | `memory_write`, `memory_read`, `memory_search`, `memory_index`, `memory_clear`, `set_identity` |
| Context | `context_retrieve`, `clear_context` |
| Scheduling | `automation_create` (task or chain), `schedule_list`, `schedule_runs`, `schedule_update`, `schedule_set_enabled`, `schedule_run`, `schedule_cancel`, `schedule_delete` |
| Workflows | `workflow_list`, `workflow_update`, `workflow_delete` (creation is `automation_create` with `steps`) |
| Signals | `emit_signal`, `list_signals` |
| Runs | `read_step_output`, `report_outcome` |
| Web | `web_search`, `web_fetch`, `tavily_search` |
| Output | `clean_output` |
| Orchestration | `spawn_agent` |
| Skills | `activate_skill`, `list_skill_files`, `read_skill_file`, `create_skill`, `update_skill`, `delete_skill`, `write_skill_file` |
| Plugins | `install_plugin`, `uninstall_plugin`, `list_installed_plugins` |
| User tools | `create_tool`, `update_tool`, `delete_tool`, `list_tools`, `install_tool_dependency` |

Two of these are gated on the request, not on the tool: `automation_create`
(a task or a chain) and `emit_signal` create something that outlives the
conversation, so each must carry an `intent` quoting the user's own words from
this turn (at least six characters, verbatim) and a call whose `intent` cannot
be found in the request is refused. `read_step_output` and `report_outcome`
only resolve inside a scheduled run.

The five that maintain existing work — `schedule_update`, `schedule_set_enabled`,
`schedule_run`, `schedule_cancel`, `workflow_update` — carry `requires_intent`
instead: the caller has to say what it is doing and why, but not in the user's
words. The sentence that should reach for them is a complaint ("那个日报老是
失败"), not a request naming a field, so demanding a quote would make
delete-and-recreate the only repair — which is what these tools exist to end.
Every one of them keeps the task id, and with it the run history and every
downstream step subscribed to its signal.

`automation_create` is also how a task declares what it has to produce
(`produces`), and `schedule_list`, `workflow_list` and
`schedule_runs` are how that declaration is read back: the creation half and
the observation half are one pair, because a task is created in a conversation
and then runs in none. A task the agent built that has been failing every night
since the day it was made looks exactly like one that has been succeeding, from
the task row alone — `schedule_runs` is the only place the difference exists.
It returns the whole definition alongside the runs, in exactly the vocabulary
`schedule_update` accepts, so what is read can be sent back with one field
changed and nothing else. The keys it reports that no edit may write —
`id`, `workflow_id`, `step_key`, `enabled`, `delivery_mode`, `delivery_target`,
`request_quote` — are the ones somebody else owns: the graph, the switch, the
channel, and the past.

Also registered at runtime:

- MCP tools from configured `mcp_servers` and plugin-bundled MCP servers
- Trusted user tools from `~/.agent/tools/*.py` when `user_tools.enabled=true`
- Individually approved user tools, even when `user_tools.enabled` is false
- Tools written by `create_tool` or `/generate-tool`

### Authoring user tools

A user tool is a Python module in `~/.agent/tools/` exposing
`register(registry)`. `create_tool` / `update_tool` are the supported way to
write one; `/generate-tool <description>` has the model write it first and then
takes the same path. Both enforce the same pipeline:

1. **Structural validation** — the source must parse and must define a
   top-level `register(registry)` that actually registers something.
2. **Out-of-process import probe** — the module is imported in a subprocess and
   reports the tools it registers, so a module that raises, blocks, or exits
   cannot take the live session with it.
3. **Human confirmation** — activation requires explicit approval, recorded
   against a hash of the file's contents in `~/.agent/tools/.approved.json`: it
   survives restarts, and editing the file revokes it.
4. **Hot load** — on approval the tool is registered in the running session; no
   restart and no manual file renaming.

#### User tools never run in the agent process

A user tool is Python the *model wrote*. Loading it with `exec_module` — the
old behaviour — handed it everything the agent has: the provider API keys in
memory and in `config.json`, the memory database, the tool registry it could
rewrite, and the event loop it could block. That made the least-trusted code
in the system the only code with no boundary around it, while the shell tool,
which merely runs commands, had an OS sandbox.

Loading now probes the module out of process for its schema and registers a
**proxy**. Each call runs the tool body in a fresh child process:

| | In-process (before) | Child process (now) |
|---|---|---|
| Provider API keys | readable | unreachable (`config.json` is sandbox-denied) |
| Registry / agent state | mutable | unreachable |
| `sys.exit`, crash, hang | takes the session down | kills the child only |
| Runaway tool | unbounded | killed on timeout |
| Cost | ~0 | ~25–35 ms per call |

This holds at every `shell_sandbox` setting — **process isolation is not the
same thing as a sandbox**, and it is worth something even at `none`. When a
sandbox mode *is* configured, the same profile the shell tool uses is applied
on top, so tools inherit the credential-read and autostart-write denials
rather than needing a policy of their own.

One behavioural change: module-level state no longer persists between calls,
since each invocation is a fresh interpreter. For model-authored code that is
closer to a fix than a regression, but a tool that memoized in a module-level
dict will now recompute.

> **Plugins are deliberately still in-process.** `~/.agent/plugins/` is loaded
> with `exec_module` and has the same access a user tool used to. The trust
> story differs — a plugin is something *you* installed, like an editor
> extension, whereas a user tool is something the *model* wrote — and plugin
> hooks pass rich objects and mutate prompts mid-turn, so the boundary is not
> a simple RPC. Treat installing a plugin as running its author's code.

Third-party packages go through `install_tool_dependency`, which runs
`pip install --target ~/.agent/tools/_deps`. That directory is prepended to
`sys.path` when tools load, so a tool's dependency never enters the active
project or the ambient interpreter. Installing packages from the `shell` tool
(`pip install`, `uv add`, `poetry add`, …) requires confirmation at every
permission level for the same reason.

Behaviour guarantees:

- File tools are bounded to the workspace root
- Shell commands are risk-classified: high-risk commands are rejected, restricted commands return a confirmation token before they may run
- Shell working-directory changes must use the tool `cwd` parameter; inline `cd`/shell control operators are rejected
- Audio transcription commands are executed as argv, not via shell string interpolation
- User Python tools are not loaded by default; enabling them trusts and executes local Python code in-process, and individual tools can instead be approved one at a time by content hash
- Third-party packages for user tools install into `~/.agent/tools/_deps`, never into the active project or the ambient interpreter
- Tool payloads are structured JSON where possible
- Shell calls are timeout-bounded and security-checked
- Shell commands are validated against a blocked list (`rm`, `dd`, `mkfs`, `shred`, etc.)

## Multi-Agent Orchestration

The agent supports four execution modes for sub-agent coordination:

### Modes

| Mode | Trigger | Use case |
|---|---|---|
| **direct** | No `spawn_agent` calls, or single sub-agent | Simple questions, single-domain tasks |
| **parallel** | Multiple `spawn_agent` calls, no dependencies | Independent perspectives, fan-out review |
| **pipeline** | Multiple calls with `depends_on` | Sequential stages with upstream→downstream data flow |
| **rendezvous** | Multiple calls with `coordination_mode="rendezvous"` | Multi-round debate, cross-validation, consensus building |

The mode is derived from the batch's own `spawn_agent` calls, in this order:
an explicit `coordination_mode` on the calls wins; otherwise any `depends_on`
makes it `pipeline`; otherwise more than one call makes it `parallel`; otherwise
`direct`. The value is read per batch, not per call: as long as one call in the
batch sets `coordination_mode`, that one value governs every call in it, and
only a batch whose calls are all blank falls through to the `depends_on` and
call-count rules. Two values that disagree, or a value outside `parallel` /
`pipeline` / `rendezvous`, are rejected rather than resolved.

Do not confuse this with the planner's own `mode`, which is a different
vocabulary of two: `direct` when `spawn_agent` is unavailable, `explicit`
otherwise. The planner never returns `pipeline` or `rendezvous` — those are
reached only through the calls above — so a planner `mode` of `explicit` means
"this turn may orchestrate", not "this turn is a pipeline".

### How to trigger each mode

```text
# Parallel — independent concurrent work
让 3 个子 agent 分别从性能、正确性、可维护性 review 这次改动

# Pipeline — sequential dependency-driven
先让 researcher 收集事实，再让 planner 给出方案，最后让 critic 审查方案

# Rendezvous — multi-round coordination
让正方和反方分别给方案，互相回应一轮后，再收敛成最终建议
```

### Constraints

- Orchestration only happens within a single assistant turn
- The whole graph is validated before any child starts, and a bad graph fails the
  turn instead of half-running. `depends_on` must name subtask IDs from the same
  batch — unknown IDs, self-dependency, duplicate dependencies and cycles are all
  refused. `parallel` and `rendezvous` batches may not declare `depends_on` at
  all; `rendezvous` also refuses `early_exit`, because it is a bounded
  convergence protocol rather than a winner-take-all fan-out
- `capability_profile: "implementation"` without a `write_scope` is refused —
  workspace writes are only ever enabled by naming the paths
- Rendezvous is bounded (default: 2 rounds)
- Sub-agents inherit the parent context manager but do not recursively receive `spawn_agent`
- Bounds come from `orchestration`: `max_parallel_agents` (3),
  `max_agents_per_turn` (0 → derived from the parallel count), and a wall-clock
  `sub_agent_timeout_seconds` that covers every retry attempt rather than each one
- A `role` of the form `plugin:<plugin>:<agent>` resolves against a plugin's
  `agents/` directory, and that definition's markdown body is prepended to the
  sub-agent's system prompt — so a plugin can ship the roles it wants used

## Skills

Skills are instruction bundles that extend the agent with specialized workflows. Each skill is a directory containing `SKILL.md` with YAML frontmatter and markdown instructions.

### SKILL.md format

```markdown
---
name: My Skill
description: What this skill does and when to use it
user-invocable: true
disable-model-invocation: false
---

Instructions for the agent when this skill is activated.
```

`user-invocable: false` removes the slash command; `disable-model-invocation:
true` stops the model activating it on its own. The built-in orchestration skill
is the one bundle that uses both, because it is read as policy rather than
activated.

What its frontmatter actually does is narrower than the key names suggest, so
read the three cases apart:

- `max-rendezvous-rounds` is the only key with a hard effect — it caps how many
  rendezvous rounds a batch may run.
- `parallel-keywords`, the two `pipeline-*-keywords` lists, `pipeline-keywords`
  and `rendezvous-keywords` are matched against the request and rendered into
  one advisory sentence (`Advisory cues for this request: …`) inside the
  `## Orchestration policy` block. They never choose the mode; they tell the
  model which shape *may* fit, and the model still has to encode it.
- `default-mode` is parsed into a field that nothing consults. The line the
  SKILL.md declares (`default-mode: direct`) has no effect on any run.

### Discovery order

Loaded in this order, into one catalog keyed by skill id:

1. Built-in skills: `agent/_builtin/skills/`
2. User skills: `~/.agent/skills/`
3. Plugin-bundled skills: declared via `plugin.json` `skills` field

A user skill with the same id as a built-in **replaces** it, which is how you
override a shipped skill. Plugin-bundled skills are namespaced under the
plugin that ships them (`<plugin>:<id>`), so they cannot collide with a built-in
or a user skill — and a bare leaf name like `code-review` is accepted as an
alias only when exactly one non-plugin skill claims it, so activation stays
unambiguous.

### Built-in skills

| Skill | Description |
|---|---|
| `multi-agent-orchestration` | **Not an activatable skill.** The orchestration planner reads its frontmatter (`parallel-keywords`, `max-rendezvous-rounds`, …) as policy, which is why it is `user-invocable: false` and `disable-model-invocation: true`. Switching it off leaves the planner on its built-in defaults — no advisory cue sentence, rendezvous capped at 2 — and every run still gets the base `## Orchestration policy` block. It does **not** change the mode, which is `explicit` whenever `spawn_agent` is available |
| `skill-manager` | Create, update, delete, and manage user skill bundles |

Built-in skills ship with the package and cannot be deleted from the
interface. Turn one off with the switch in the Skills view, or in
`config.json`:

```json
{ "skills": { "skill-manager": { "enabled": false } } }
```

A skill that is off disappears from the prompt's skill list, from slash
commands, and from `activate_skill`, which refuses it by name. It stays in
the Skills view so it can be switched back on.

### Hot-reload

After creating, updating, or deleting a skill, the catalog reloads automatically. The system prompt is recomposed before the next turn — no restart required. Switching a skill on or off takes effect the same way: the next turn sees the change, and the switch itself is answered immediately.

## Plugins

Plugins extend the agent with lifecycle hooks, system prompt contributions, slash commands, and bundled MCP servers or skills.

### Plugin structure

```
my-plugin/
├── plugin.json       # Structured manifest (recommended)
├── __init__.py       # register() entry point (required)
├── skills/           # Bundled skills (declared in plugin.json)
├── agents/           # Role definitions a sub-agent can be spawned as
├── commands/         # Bundled slash commands
└── .mcp.json         # Bundled MCP servers
```

### plugin.json

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What this plugin does",
  "skills": "./skills/",
  "mcp_servers": [
    {"name": "my-server", "command": "npx", "args": ["my-mcp-server"]}
  ]
}
```

### Lifecycle hooks (all optional, duck-typed)

| Hook | When called | Key capability |
|---|---|---|
| `on_session_start(components)` | Once before the interactive loop | Capture client, model, memory references |
| `on_prompt_submit(text, metadata)` | Before agent sees user message | Block messages, inject context |
| `on_pre_tool(event)` | Before each tool call | Veto execution with `action="block"`; scoped by matcher |
| `on_post_tool(event)` | After each tool call | Observe results; scoped by matcher |
| `on_turn_end(event)` | After each assistant turn | Return `action="continue"` to auto-loop |
| `on_session_end(event)` | When the session ends | Score, persist analytics |
| `compose_system_prompt(current)` | System prompt build | Append behavior rules |
| `register_slash_commands()` | Startup | Register `/commands` |

### Hook configuration (plugin.json)

```json
{
  "hooks": {
    "on_pre_tool": [
      {"matcher": "^shell$", "timeout": 5.0}
    ],
    "on_turn_end": [
      {
        "type": "command",
        "command": "python3 ~/.agent/plugins/audit/hook.py",
        "timeout": 10.0
      }
    ]
  }
}
```

- **`matcher`** — regex to scope hooks to specific tool names. No matcher = all tools.
- **`timeout`** — per-hook override (default: 2s global).
- **`type: "command"`** — external script hooks via stdin/stdout JSON. Exit code 2 = block.
- **`${…}` substitution** — hook commands may use `${CLAUDE_PLUGIN_ROOT}`,
  `${CLAUDE_PLUGIN_DATA}` and `${CLAUDE_PROJECT_DIR}` (with `CODEX_*` aliases),
  each with an optional `:-default`.

### Plugins written for Claude Code and Codex

The loader also reads the other ecosystems' layouts, so a plugin does not have
to be rewritten to be installed:

- Any of `plugin.json`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`
- `mcpServers` as well as `mcp_servers`
- Skills default to the `skills/` subdirectory, so a Claude Code plugin needs no
  manifest entry to expose them — and a directory with only `skills/`,
  `commands/`, `agents/` or a `SKILL.md` is a plugin even with no manifest
- `.claude-plugin/marketplace.json`, or Codex's root `marketplace.json` — local
  relative sources are expanded; remote entries belong to the installer
- `hooks/hooks.json`, and a `"hooks"` value that is a path string rather than an
  inline block
- PascalCase event names, normalized: `PreToolUse` → `on_pre_tool`, `Stop` →
  `on_turn_end`, `SessionStart` → `on_session_start`, `UserPromptSubmit` →
  `on_prompt_submit`, …

**Read the last one carefully.** Only the eight hooks in the table above have a
dispatcher. Every other name in that vocabulary — `Notification`, `PreCompact`,
`SubagentStart`, `PermissionRequest` — is accepted, normalized, and then never
fires. It fails silently in the direction of doing nothing, which is why it is
worth knowing before you wonder why a hook never ran.

### Built-in plugins

| Plugin | Description |
|---|---|
| `evolution` | Detects user corrections, extracts behavioral rules, and scores sessions for continuous improvement |

### User plugins

Place plugins under `~/.agent/plugins/`. User plugins with the same name override built-in plugins. Disable any plugin via config:

```json
{"plugins": {"evolution": {"enabled": false}}}
```

`install_plugin` clones a git URL or copies a local path into
`~/.agent/plugins/<name>/`, then hot-reloads the catalog. Reinstalling an
existing name requires `replace: true` (upgrade); a failed clone, validation
or reload rolls back automatically and restores the previous version. Plugins
with executable content (Python `__init__.py`, MCP servers, hooks) are treated
as arbitrary code: the CLI shows an approval menu before activation, and in
gateway channels the agent asks the user to reply "同意" (a pending record is
created and redeemed by the coordinator), after which the identical source
must be retried. Declarative-only plugins (skills/commands without Python,
MCP or hooks) install without confirmation.

## Runtime Architecture

```
Transport (CLI / Feishu / Scheduler)
        │
        ▼
AgentCore.handle_turn(TurnInput, RuntimeSessionState)
        │
        ├── on_prompt_submit hooks (block / inject context)
        ├── skill parsing & hot-reload
        ├── TurnRunner.run() → BaseAgent.send_message()
        │       ├── step loop: one model request + the tools it calls
        │       ├── LLM retry (3x exponential backoff on transient errors)
        │       ├── Tool execution (RegularToolExecutor)
        │       │       ├── Intent-before-action protocol
        │       │       └── Plugin pre/post hooks
        │       └── EventCollector (ContextVar-scoped)
        ├── complete_turn() → plugin hooks, staging, consolidation
        └── TurnExecution { result, continuation_rounds, events: tuple[RuntimeEvent, ...] }
```

**Turn / step / continuation** name three different counts:

| Term | Scope | Where counted |
|---|---|---|
| **step** | one model request plus the tools that request calls | `BaseAgent.send_message`, reported as `step_started` / `step_ended`; bounded by `max_steps` |
| **continuation round** | one pass of the turn loop (round 1 is the initial completion) | `TurnExecution.continuation_rounds` |
| **turn** | the whole processing of one user input | `TurnRunner.run()` |

Key properties:
- **Transport-neutral**: same turn boundary for CLI and Feishu
- **Replayable event stream**: every tool call, hook, and lifecycle fact is a `RuntimeEvent`
- **Context rewrites are logged**: the two paths that change what the model already
  saw — compaction and content-filter rollback — emit `consolidation_compaction`
  and `context_rolled_back` carrying the dropped roles and token estimate, so
  "never retrieved" stays distinguishable from "retrieved, then dropped"
- **Intent-before-action**: write/shell tools require the assistant to declare intent first
- **Request-before-creation**: the two creators (`automation_create`,
  `emit_signal`) must quote the user's words from the current
  turn, while the maintenance tools (`schedule_update`, `schedule_set_enabled`,
  `schedule_run`, `schedule_cancel`, `workflow_update`) state what they are
  doing; both gates are capabilities on the tool (`requires_intent`,
  `requires_request`), not per-tool branches
- **LLM retry**: transient API errors (rate limits, 5xx) retried with exponential backoff.
  An account that is spent rather than throttled is not one of them: a spent
  plan quota, an arrears notice, or an empty balance arrives on the same status
  as a rate limit, and `agent.shared.ACCOUNT_EXHAUSTED_MARKERS` is what tells
  the two apart — for the retry loop and for the scheduler that queues run retries

### Which tool schemas a turn is offered

The system prompt carries the **whole inventory** — every registered tool by
name, description and source (`builtin` / `mcp` / `runtime`) — so the model can
always answer "what can you do". What varies per turn is which *schemas* are
sent alongside it. `ContextAssembler.select_tools` starts from the registry and
holds back the groups whose schema is only worth its tokens when the sentence
calls for it:

| Held back by default | Opened by |
|---|---|
| `automation_create`, `schedule_delete`, `schedule_update`, `schedule_set_enabled`, `schedule_run`, `schedule_cancel`, `workflow_delete`, `workflow_update` | Naming or describing scheduled work **and** asking for it |
| `schedule_runs` | Never — the answer to "did it actually run" is phrased in the user's own words, which name no cadence |
| `emit_signal` | A scheduled run, or an ask that names scheduled work |
| `report_outcome` | Being inside a scheduled run — no sentence in a conversation makes it usable |
| `memory_index`, `memory_clear` | `memory`, `remember`, `forget`, `记忆`, `记住`, `忘记`, `上下文` |
| `install_plugin`, `create_tool`, `create_skill`, … | `plugin`, `skill`, `tool`, `插件`, `技能`, `工具` |
| `spawn_agent` | `parallel`, `sub-agent`, `并行`, `子代理`, or an explicit orchestration request |
| `tavily_search`, `transcribe_audio` | A research-shaped query; an audio attachment |

Two details are decisions rather than omissions:

- **Reading is not asking.** `schedule_list`/`workflow_list` open on a mention
  — "我有哪些定时任务" and "你们的工作流怎么用" are both questions about the
  feature and both want the list — while the creators additionally need a
  cadence ("每天") or a make-a-thing verb applied to a feature that was named
  ("帮我做一个流程"). Loose helpers like "帮我做" are deliberately *not* verbs:
  they are how any job is asked for, which is exactly the sentence that once
  got misread into building a task nobody asked for.
- **This gate is budget, not permission.** Calls are dispatched by name against
  the whole registry, so a schema that was not sent is still a tool that can be
  called. What actually refuses an unasked creation is the executor's
  `requires_request` check. The gate only means a caller that *did* ask is not
  charged for the schemas, and one that did not is not invited by them.

## Memory & Context Architecture

Four-layer memory system:

1. **Working memory** — active `ctx.messages` in RAM for the current interaction
2. **Staging** — raw turns buffered per-session in SQLite (`palace.db`), consolidated in background
3. **Fact storage** — exact facts (`fact_assertions` → `resolved_facts`) for identity and preferences
4. **Long-term memory** — free-form entries in SQLite with JSONL export for inspection

Fixed palace loci: `identity`, `projects`, `people`, `concepts`, `episodes`, `tasks`, `procedures`, `archive`

### Consolidation lifecycle

- Stage raw turns per session
- Queue background jobs when staged volume or idle time reaches threshold
- Recover orphaned staging files from interrupted sessions on startup
- Extract facts, summaries, and durable memories into LTM
- Apply retention/decay policies
- Compact working memory while preserving task context

## MCP

MCP (Model Context Protocol) servers are configured via:

1. `mcp_servers` in `config.json`
2. Plugin-bundled `mcp_servers` in `plugin.json`

Connected tools are injected into the runtime registry and appear in the composed system prompt.
MCP server stderr is written to `<output_dir>/mcp-logs/<server>.stderr.log` so
server diagnostics cannot overwrite the interactive CLI input line.

## Project Layout

```
.
├── agent/
│   ├── core/           # BaseAgent, AgentContext, OutputSink, RuntimeEvent, EventCollector
│   │                   #   transport.py: per-format SDK transports + RoutingTransport
│   ├── runtime/        # AgentCore, TurnInput, TurnResult, TurnExecution, TurnRunner
│   │                   #   lock.py: one owner per agent home; heartbeat.py: telemetry
│   ├── commands/       # CommandRouter, CommandDescriptor, built-in slash commands
│   ├── memory/         # LTMStore, MemoryPalace, ConsolidationEngine, StagingBuffer
│   ├── tools/          # ToolRegistry, BuiltinTools, executor, user tools + child runner
│   ├── verification/   # The verdict vocabulary, and CommandVerifier (acceptance checks)
│   ├── exec/           # SubprocessProvider seam: one place children are spawned
│   ├── orchestration/  # OrchestrationPlanner, parallel/pipeline/rendezvous execution
│   ├── ralph/          # Autonomous task loop: models, parser, service, store
│   ├── channels/       # Channel ABC, CliChannel, ChannelRunner, Feishu/Lark, Web
│   ├── scheduler/      # SchedulerService, SchedulerStore, models, runtime, delivery,
│   │                   #   profiles (permission envelopes), unattended
│   ├── security/       # Shell blocking, content filter, approvals,
│   │                   #   network egress (web_fetch proxy);
│   │                   #   sandbox/ = policy + per-OS backends + scratch dir
│   ├── skills/         # SkillBundle, SkillCatalog, skill parsing, hot-reload
│   ├── plugins/        # PluginCatalog, AgentPlugin protocol, HookResult, lifecycle
│   ├── _builtin/       # Built-in plugin (evolution), skills, and the packaged web bundle
│   ├── cli.py          # Typer CLI (interactive, gateway, scheduler, config, memory)
│   ├── config.py       # Config loading, validation, ModelClientFactory, system prompt
│   ├── bootstrap.py    # Component wiring from config
│   ├── evolution.py    # Session scoring, prompt rewriting, tool generation
│   ├── session_service.py, sessions.py  # Session listing/durability; --name discovery
│   ├── lexical.py      # Lexical (BM25) scoring behind memory search
│   ├── usage.py        # Provider-neutral token usage extraction
│   ├── tui.py          # Full-screen interactive CLI layout
│   ├── shared.py       # Paths, defaults, tracing, named-session support
│   └── pathing.py      # Path resolution and workspace containment
├── frontend/           # React + Ant Design source; build:release writes the package bundle
├── docs/superpowers/   # Design specs (specs/) and implementation plans (plans/)
├── scripts/            # benchmark_memory.py, eval_retrieval.py
├── tests/
├── config.example.json
├── pyproject.toml
└── uv.lock
```

## Testing

```bash
# Full suite
uv run pytest -q

# The functional layer: real composition and entry points, a local fake provider
uv run pytest tests/test_functional_turns.py -q

# Specific area
uv run pytest tests/test_builtin_tools.py -q
uv run pytest tests/test_scheduler.py -q

# Memory benchmark
python scripts/benchmark_memory.py --sizes 1000 10000 --search-runs 10
```

`tests/test_sandbox_conformance.py` spawns real sandboxed children and asks the
OS what they are actually permitted to do, rather than reading the generated
profile. It skips only when the host has no enforcing sandbox at all — so if
macOS detection ever breaks, those tests fail instead of quietly skipping while
nothing is enforced. `tests/test_filesystem_sandbox.py` keeps what is genuinely
seatbelt-specific: the generated `.sb` text and its cache key.

Latest verification: `bash scripts/check.sh` → **4362 passed, 0 failed, 2 skipped**
(the browser layer needs the `[e2e]` extra; without it those four skip).

**The suite is green here: 0 failed.** It used to carry a pinned baseline of three
failures, all of them the same one —
`tests/test_cli_ui.py::test_console_width_check_is_throttled`,
`::test_console_width_follows_terminal_resize` and
`::test_console_width_survives_a_failing_terminal_probe` — and the honest description
then was "they are about the test's terminal rather than about the code". That was
true, and it was also the whole bug: `rich` treats an empty `TERM` as a **dumb
terminal** and returns a hardcoded 80x25 *before* it looks at the width it was given,
so the tests only ever passed in a shell that set `TERM`. Pinning the environment the
test measures (a `terminal` fixture: `TERM=xterm-256color`, no `COLUMNS`/`LINES`)
makes them deterministic everywhere, and the baseline is now empty.

That baseline was not free: a permanently red set teaches everyone to read the pass
count instead of the failures, which is precisely the reflex a CI run relies on. Which
is why there is now one:

```bash
bash scripts/check.sh          # uv lock --check + the whole suite
bash scripts/check.sh tests/   # ...or a slice, same gate
```

`.github/workflows/check.yml` calls that script (Python 3.11 and 3.13, plus a frontend
typecheck), so "green locally" and "green on the runner" are one claim rather than two.

A host that forbids nesting `sandbox-exec` adds failures of one shape — tests
that ask the OS to spawn a nested sandbox, or to read the agent home from inside
one. This machine does not have that restriction, so its baseline is three
failures rather than three plus those; on a host that does, they are environment
bound in exactly the same way and belong in the same baseline diff.

If your shell cannot create directories under the system temp directory,
pytest's `tmp_path` fixture errors out across the whole suite before a single
test runs. Point `--basetemp` at a directory that already exists:

```bash
uv run pytest -q --basetemp=/tmp/pytest-simple
```
