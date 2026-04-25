# Personal Agent

Personal AI agent package with:

- provider abstraction for Anthropic and OpenAI-compatible APIs
- interactive chat plus single-turn CLI mode
- tool calling for shell, files, memory, web access, and sub-agents
- fixed-loci memory palace backed by SQLite
- staged conversation buffering, background consolidation, and orphan-session recovery
- skills system with hot-reload, built-in skill-manager, and progressive-disclosure design
- plugin system with `plugin.json` manifests, skill/MCP bundling, per-plugin enable/disable, and user-override semantics
- MCP tool ingestion, user tool plugins, and prompt-evolution utilities

## Requirements

- Python `3.11+`
- `uv`
- At least one configured model provider

Supported providers:

- `anthropic`
- `openai`
- `deepseek`
- `qwen`
- `ollama`
- any other OpenAI-compatible endpoint via `base_url`

## Install

```bash
uv sync --group dev
```

Dependencies:

- `anthropic`
- `openai`
- `typer`
- `rich`
- `mcp`
- `pytest` (dev group)

## Configuration

The agent reads config from `~/.agent/config.json`.

Let first run scaffold it interactively, or copy the example manually:

```bash
mkdir -p ~/.agent
cp config.example.json ~/.agent/config.json
```

Key config sections:

| Section | Purpose |
|---|---|
| `active_provider` | Which provider to use |
| `providers.<name>.*` | API key, format, base URL, model list, max tokens |
| `context.storage` | LTM category cap, decay factor |
| `context.consolidation` | Token ratio, keep-last-N, idle seconds, min messages |
| `mcp_servers` | MCP server definitions (name, command, args) |
| `plugins` | Per-plugin enable/disable (`{"evolution": {"enabled": false}}`) |
| `evolution` | Enable/disable session scoring and rule learning |
| `scheduler` | Poll/lease settings for the persistent scheduler service |
| `tavily_api_key` | Optional Tavily search key |
| `output_dir` | Override default `~/.agent/output` |
| `system_prompt_file` | Load a custom system prompt from a `.md` or `.txt` file |

`config.example.json` shows the full shape used by the current runtime.

## Usage

Interactive mode:

```bash
uv run simple
```

Single-turn chat:

```bash
uv run simple chat "Summarize this repository"
```

Scheduler service:

```bash
uv run simple scheduler
```

Create a daily scheduled task:

```bash
uv run simple schedule daily daily-summary \
  --time 09:00 \
  --timezone Asia/Shanghai \
  --prompt "Summarize yesterday's progress"
```

Config commands:

```bash
uv run simple config list
uv run simple config models
uv run simple config get providers.qwen.base_url
```

Evolution commands:

```bash
uv run simple evolve --stats
uv run simple evolve --rewrite
uv run simple evolve --apply-best
```

Memory commands:

```bash
uv run simple memory ls
uv run simple memory index
uv run simple memory show identity/user
uv run simple memory search "preferences"
uv run simple memory tidy
```

Install as a tool to get `simple` directly on `PATH`:

```bash
uv tool install --editable .
```

Install as a tool with Feishu support:

```bash
uv tool install --reinstall --editable . --with lark-oapi
```

## Feishu Gateway

If you run from the repository, install the optional Feishu dependency into the
project environment and start through `uv run`:

```bash
uv sync --extra feishu
uv run simple gateway
```

If you use the globally installed `simple` command from `uv tool install`, the
tool has its own isolated Python environment. In that case, install or reinstall
the tool with `lark-oapi` included:

```bash
uv tool install --reinstall --editable . --with lark-oapi
simple gateway
```

Do not mix these two paths:

- `uv sync --extra feishu` only affects the repository `.venv`
- `simple gateway` may use the separate `uv tool` environment on your `PATH`

## Interactive Commands

| Command | Description |
|---|---|
| `/memory` | Browse and manage the memory palace |
| `/context` | Show context manager statistics |
| `/evolve` | Rewrite system prompt from session history |
| `/generate-tool <description>` | Generate and hot-load a new tool |
| `/tools` | List all registered tools |
| `/skills` | List available skills |
| `/plugins` | List loaded plugins and their sources |
| `/model [name]` | Show current model or switch to a different one |
| `/ralph <goal>` | Launch an autonomous multi-iteration task loop |
| `/quit` | End the session |

## Scheduler Commands

| Command | Description |
|---|---|
| `simple schedule once ...` | Create a one-shot scheduled task |
| `simple schedule interval ...` | Create a fixed-interval scheduled task |
| `simple schedule daily ...` | Create a daily scheduled task |
| `simple schedule weekly ...` | Create a weekly scheduled task |
| `simple schedule list` | List persisted scheduled tasks |
| `simple schedule show <id>` | Show one task and its run history |
| `simple schedule pause <id>` | Disable a scheduled task |
| `simple schedule resume <id>` | Re-enable a scheduled task |
| `simple schedule delete <id>` | Delete a task and its recorded runs |
| `simple scheduler` | Run the persistent scheduler service |

Invoke a skill explicitly with a slash prefix matching its ID:

```
/<skill-id> <request>
```

## Runtime Tools

Built-in tools registered at startup:

| Group | Tools |
|---|---|
| Time | `current_time` |
| Shell | `shell` |
| Files | `read_file`, `write_file`, `list_files` |
| Memory palace | `memory_write`, `memory_read`, `memory_search`, `memory_index` |
| Context retrieval | `context_retrieve` |
| Web | `web_search`, `web_fetch`, `tavily_search` |
| Output cleanup | `clean_output` (when output dir is configured) |
| Multi-agent orchestration | `spawn_agent` |
| Skill runtime | `activate_skill`, `list_skill_files`, `read_skill_file` |
| Skill management | `create_skill`, `update_skill`, `delete_skill`, `write_skill_file` |

Also registered at runtime:

- MCP tools from configured `mcp_servers` and plugin-bundled MCP servers
- User tools loaded from `~/.agent/tools`
- Auto-generated tools created via `/generate-tool`

Behaviour guarantees:

- file tools are bounded to the current workspace root
- tool payloads are structured JSON where possible
- shell calls are timeout-bounded
- sub-agents inherit the parent context manager but do not recursively receive `spawn_agent`

## Multi-Agent Orchestration Modes

The runtime does not primarily choose orchestration mode from user-facing
keywords. Instead, mode selection happens when the model emits one or more
`spawn_agent` calls in the same assistant turn.

At the runtime level, the trigger rules are:

- `direct`
  - no `spawn_agent` call is emitted, or only one sub-agent is spawned
- `parallel`
  - multiple `spawn_agent` calls are emitted in the same turn
  - none of them has `depends_on`
  - none of them sets `coordination_mode="rendezvous"`
- `pipeline`
  - multiple `spawn_agent` calls are emitted in the same turn
  - at least one subtask declares a non-empty `depends_on`
- `rendezvous`
  - multiple `spawn_agent` calls are emitted in the same turn
  - at least one subtask sets `coordination_mode="rendezvous"`
  - this takes precedence over `pipeline` and `parallel`

Important constraints:

- Orchestration only happens within one assistant response. If the model emits
  one sub-agent now and another in a later turn, the runtime will not join them
  into the same pipeline or rendezvous batch.
- `depends_on` must point to subtask ids from the same batch.
- `rendezvous` is bounded. The current default is two rounds.

### How To Trigger Each Mode

From the user side, the practical way to trigger a mode is to ask for a task
shape that naturally leads the model to emit the matching `spawn_agent`
structure.

#### `parallel`

Use when you want several independent perspectives to work at the same time.

Example prompts:

```text
同时让 3 个子 agent 分别从性能、正确性、可维护性 review 这次改动，最后汇总结论。
```

```text
并行找出这个项目里和认证、缓存、调度相关的实现风险，每个子 agent 负责一个方向。
```

Expected runtime shape:

- same-turn multiple `spawn_agent`
- no `depends_on`

#### `pipeline`

Use when later workers should consume earlier workers' outputs.

Example prompts:

```text
先让 researcher 收集事实，再让 planner 基于这些事实给出方案，最后让 critic 审查方案。
```

```text
先分析问题根因，再生成修复方案，最后基于修复方案补测试。
```

Expected runtime shape:

- same-turn multiple `spawn_agent`
- downstream subtasks include `depends_on`

#### `rendezvous`

Use when you want bounded multi-round coordination rather than one-pass fan-out.

Example prompts:

```text
让正方和反方分别给方案，互相回应一轮后，再收敛成最终建议。
```

```text
让 researcher 和 critic 先各自独立判断，再进行一轮交叉校验，最后输出共识和分歧。
```

Expected runtime shape:

- same-turn multiple `spawn_agent`
- at least one subtask sets `coordination_mode="rendezvous"`

### Notes For Prompting

- If you want `parallel`, avoid wording that implies strict sequencing such as
  “先…再…”.
- If you want `pipeline`, be explicit about stage order and upstream/downstream
  dependency.
- If you want `rendezvous`, explicitly ask for “互相回应一轮”, “交叉校验”, or
  “收敛共识”, because that is what encourages the model to emit coordinated
  subtask structure instead of plain fan-out.
- If you want deterministic testing, inspect gateway logs. The runtime emits
  `execution_mode` in sub-agent batch events, and the gateway now also emits
  interaction logs for message receipt, agent execution, and reply delivery.

## Skills

Skills are instruction bundles that extend the agent with specialized workflows. Each skill is a directory containing `SKILL.md` (required) and optional supporting files.

### Discovery order

1. Built-in skills: `<repo>/skills/**/SKILL.md`
2. Plugin-bundled skills: loaded when a plugin's `plugin.json` declares a `"skills"` path
3. User skills: `~/.agent/skills/**/SKILL.md`

User skills with the same ID override built-in or plugin-bundled skills.

### SKILL.md format

```markdown
---
name: My Skill
description: What this skill does and when to use it (used for triggering)
user-invocable: true
disable-model-invocation: false
---

Instructions for the agent when this skill is activated.
```

### Resource directories

| Directory | Purpose | Loaded into context |
|---|---|---|
| `scripts/` | Executable code for deterministic operations | Can execute without reading |
| `references/` | Documentation, schemas, API docs | On demand |
| `assets/` | Templates, images, boilerplate | Not loaded; used in output |

### Built-in skills

| Skill | Description |
|---|---|
| `skill-manager` | Create, update, delete, and manage user skill bundles in the current session |

### Hot-reload

After `create_skill`, `update_skill`, `delete_skill`, or `write_skill_file`, the catalog reloads automatically. The interactive loop detects the dirty flag before the next turn and recomposes the system prompt so the model sees the updated skill list immediately—no restart required.

### Managing skills at runtime

The `skill-manager` built-in skill exposes four tools:

- **`create_skill`** — create a new skill bundle under `~/.agent/skills/`
- **`update_skill`** — update metadata or instructions of an existing user skill
- **`delete_skill`** — remove a user skill bundle
- **`write_skill_file`** — write or update a supporting file inside a skill bundle

The skill-manager bundle also includes `scripts/init_skill.py` and `scripts/quick_validate.py` for scaffolding and validating skill directories from the shell.

## Plugins

Plugins extend the agent with lifecycle hooks, system prompt contributions, and slash commands. Each plugin is a directory under `agent/_builtin/plugins/` (built-in) or `~/.agent/plugins/` (user).

### Plugin structure

```
my-plugin/
├── plugin.json          # Structured manifest (optional but recommended)
├── __init__.py          # register() entry point (required)
├── skills/              # Bundled skills (declared in plugin.json)
└── .mcp.json            # Bundled MCP servers (or inline in plugin.json)
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

If `plugin.json` is absent, the runtime falls back to `name`/`version` attributes on the plugin object.

### Plugin interface (duck-typed)

A plugin object can implement any subset of:

| Method | When called |
|---|---|
| `on_session_start(components)` | Once before the interactive loop |
| `on_turn_end(event: TurnEvent) -> HookResult` | After each assistant turn |
| `on_session_end(event: SessionEvent)` | When the session ends |
| `on_pre_tool(event: PreToolEvent) -> HookResult` | Before each tool call (returning `action="block"` prevents execution) |
| `on_post_tool(event: PostToolEvent) -> HookResult` | After each tool call |
| `compose_system_prompt(current: str) -> str` | Return a suffix to append to the composed system prompt |
| `register_slash_commands() -> dict[str, handler]` | Expose slash commands to the interactive loop |

### Built-in plugins

| Plugin | Description |
|---|---|
| `evolution` | Detects user corrections, extracts behavioral rules, and scores sessions for continuous improvement |

### User plugins

Place a plugin directory under `~/.agent/plugins/`. User plugins with the same name as a built-in plugin override the built-in.

### Enable / disable

In `~/.agent/config.json`:

```json
{
  "plugins": {
    "evolution": {"enabled": false}
  }
}
```

## MCP

MCP servers are configured in two ways:

1. **Config-level** — under `mcp_servers` in `~/.agent/config.json`
2. **Plugin-bundled** — via `mcp_servers` in a plugin's `plugin.json`

Connected MCP tools are injected into the runtime tool registry and appear in the composed system prompt alongside built-in tools.

## Memory And Context Architecture

The context system has four layers:

1. **Working memory** — active `ctx.messages` kept in RAM for the current interaction loop
2. **Staging** — raw user/assistant turns appended to per-session buffers in `~/.agent/context/palace.db` by default, with legacy JSONL compatibility for explicit file-based staging
3. **Long-term memory** — structured memories stored in `~/.agent/context/palace.db`, with user-facing JSONL export in `~/.agent/memory/memory.jsonl`
4. **Memory palace export** — on-demand JSONL projection for inspection; SQLite remains the source of truth

Fixed palace loci:

- `identity`, `projects`, `people`, `concepts`, `episodes`, `tasks`, `procedures`, `archive`

Legacy alias: `knowledge` → `concepts`

### Consolidation

- Stage raw turns per session
- Queue background jobs when staged volume or idle time warrants it
- Recover orphaned staging files from interrupted sessions on next startup
- Summarize the session into `episodes`
- Extract durable memories into fixed loci
- Apply retention/decay policies
- Compact working memory while preserving task context

Consolidation is chunked: long staged conversations are split into manageable prompt chunks before extraction.

### Retrieval

- **Implicit prompt injection** — assistant self-identity plus relevant long-term memory by default; current-session staging only for clear recall queries or post-compaction recovery
- **Explicit tool retrieval** — `context_retrieve` searches current-session staging plus long-term memory

For recall-style queries such as "what did we just discuss", the runtime falls back to recent `episodes` summaries when keyword search alone would miss the latest session.

## User Tool Plugins

User tool plugins are loaded from `~/.agent/tools`. Each plugin is a Python file exporting a `register(registry)` function. The interactive `/generate-tool` flow writes generated tools to this location and reloads them into the live registry automatically.

## Project Layout

```
.
├── agent/                          # Package runtime and public entrypoints
├── config.example.json             # Full config reference
├── agent/
│   ├── _builtin/
│   │   ├── plugins/
│   │   │   └── evolution/
│   │   └── skills/
│   │       └── skill-manager/
├── scripts/
│   └── benchmark_memory.py
├── tests/
├── pyproject.toml
└── uv.lock
```

## Testing

Run the full suite:

```bash
uv run pytest -q
```

Run the memory benchmark:

```bash
python scripts/benchmark_memory.py --sizes 1000 10000 --search-runs 10 --write-runs 10
```

Save or compare benchmark output:

```bash
python scripts/benchmark_memory.py --sizes 1000 10000 --output bench.json
python scripts/benchmark_memory.py --sizes 1000 10000 --compare bench.json
python scripts/benchmark_memory.py --sizes 1000 10000 --output bench.csv
python scripts/benchmark_memory.py --sizes 1000 10000 --output bench.jsonl
```

Latest local verification: `uv run pytest -q` → `451 passed, 1 skipped`
