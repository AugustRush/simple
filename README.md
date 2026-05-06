# Simple — Personal AI Agent

A personal AI agent with memory, tool calling, multi-agent orchestration, scheduling, skills, plugins, and multi-channel delivery.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- At least one configured model provider

Supported providers:

| Provider | Format | Notes |
|---|---|---|
| Anthropic | `anthropic` | Native SDK, vision support |
| OpenAI | `openai` | Native SDK, vision support |
| DeepSeek | `openai` | OpenAI-compatible endpoint |
| Ollama | `openai` | Local, no API key needed |
| Qwen | `openai` | OpenAI-compatible endpoint |
| Custom | `openai` | Any OpenAI-compatible `base_url` |

## Quick Start

```bash
# Install dependencies
uv sync

# First run — interactive setup wizard
uv run simple
```

The setup wizard guides you through provider selection, API key configuration, and model choice. Config is written to `~/.agent/config.json`.

## Examples

### Multi-instance deployment

```bash
# Production instance
uv run simple gateway --name prod    # -> ~/.agent-prod/{config.json,palace.db,...}

# Development instance with its own config
uv run simple gateway --name dev     # -> ~/.agent-dev/

# Default (no --name)
uv run simple gateway                # -> ~/.agent/
```

Each named instance has independent config, memory, context database, scheduler, skills, and plugins.

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

# Deliver to Feishu chat
uv run simple schedule daily standup \
  --time 09:00 --timezone Asia/Shanghai \
  --prompt "Generate standup notes from yesterday's activity" \
  --delivery-mode channel --chat-id ou_xxxxxx

# Manage tasks
uv run simple schedule list
uv run simple schedule show <task-id>
uv run simple schedule pause <task-id>
uv run simple schedule delete <task-id>
```

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
        print("Hello from slash command!")
```

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

### Working with MCP tools

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
| `providers.<name>.*` | API key, format, base URL, model list, max tokens |
| `context.storage` | LTM category cap, decay factor |
| `context.consolidation` | Token ratio, keep-last-N, idle seconds, min messages |
| `channels.feishu` | Feishu bot credentials (`app_id`, `app_secret`, `group_policy`, etc.) |
| `audio.transcription_command` | External STT command template (`{path}`, `{language}` placeholders) |
| `mcp_servers` | MCP server definitions (name, command, args, env) |
| `plugins` | Per-plugin enable/disable (`{"evolution": {"enabled": false}}`) |
| `evolution` | Enable/disable session scoring and rule learning |
| `scheduler` | Poll/lease/concurrency settings |
| `tavily_api_key` | Optional Tavily search API key |
| `output_dir` | Override default `~/.agent/output` |
| `assistant_identity` | Deterministic assistant name/role for fact recall |
| `system_prompt_file` | Load custom system prompt from `.md` or `.txt` |

## Usage

### Interactive mode

```bash
uv run simple
```

### Single-turn chat

```bash
uv run simple chat "Summarize this repository"
```

### Multi-instance deployment

Run multiple isolated instances with `--name`:

```bash
uv run simple gateway --name prod    # -> ~/.agent-prod/
uv run simple gateway --name dev     # -> ~/.agent-dev/
uv run simple gateway                # -> ~/.agent/ (default)
```

Each instance has completely independent config, memory, context database, scheduler, skills, and plugins. Also works with `--name` on any service command:

```bash
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

### Scheduler service

```bash
uv run simple scheduler
```

### Scheduling tasks

```bash
# Daily
uv run simple schedule daily daily-summary \
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

# Manage
uv run simple schedule list
uv run simple schedule show <id>
uv run simple schedule pause <id>
uv run simple schedule resume <id>
uv run simple schedule delete <id>
```

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

| Command | Description |
|---|---|
| `/help` | Show all commands |
| `/memory` | Memory export summary |
| `/context` | LTM context manager statistics |
| `/sessions` | List recent session history |
| `/session <id>` | View session details |
| `/tools` | List all available tools |
| `/skills` | List available skills |
| `/plugins` | List loaded plugins |
| `/model [name]` | Show or switch active model (session only) |
| `/ralph <goal>` | Launch autonomous multi-iteration task loop |
| `/ralph list` | List all Ralph autonomous tasks |
| `/ralph resume <id>` | Resume a paused Ralph task |
| `/evolve` | Trigger system-prompt self-evolution |
| `/generate-tool <desc>` | Generate a new user tool |
| `/quit` | Exit the agent |

## Built-in Tools

| Group | Tools |
|---|---|
| Time | `current_time` |
| Shell | `shell` |
| Files | `read_file`, `write_file`, `list_files`, `send_file` |
| Media | `transcribe_audio` |
| Memory | `memory_write`, `memory_read`, `memory_search`, `memory_index` |
| Context | `context_retrieve` |
| Scheduling | `schedule_create`, `schedule_list`, `schedule_delete` |
| Web | `web_search`, `web_fetch`, `tavily_search` |
| Output | `clean_output` |
| Orchestration | `spawn_agent` |
| Skills | `activate_skill`, `list_skill_files`, `read_skill_file`, `create_skill`, `update_skill`, `delete_skill`, `write_skill_file` |

Also registered at runtime:

- MCP tools from configured `mcp_servers` and plugin-bundled MCP servers
- User tools loaded from `~/.agent/tools/*.py`
- Auto-generated tools via `/generate-tool`

Behaviour guarantees:

- File tools are bounded to the workspace root
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
- `depends_on` must reference subtask IDs from the same batch
- Rendezvous is bounded (default: 2 rounds)
- Sub-agents inherit the parent context manager but do not recursively receive `spawn_agent`

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

### Discovery order

1. Built-in skills: `agent/_builtin/skills/`
2. Plugin-bundled skills: declared via `plugin.json` `skills` field
3. User skills: `~/.agent/skills/`

User skills with the same ID override built-in or plugin-bundled skills.

### Built-in skills

| Skill | Description |
|---|---|
| `daily-summary` | Generate structured daily/weekly summaries from context memory, session history, and scheduled tasks |
| `multi-agent-orchestration` | Decide when to use parallel, pipeline, or rendezvous multi-agent execution |
| `skill-manager` | Create, update, delete, and manage user skill bundles |

### Hot-reload

After creating, updating, or deleting a skill, the catalog reloads automatically. The system prompt is recomposed before the next turn — no restart required.

## Plugins

Plugins extend the agent with lifecycle hooks, system prompt contributions, slash commands, and bundled MCP servers or skills.

### Plugin structure

```
my-plugin/
├── plugin.json       # Structured manifest (recommended)
├── __init__.py       # register() entry point (required)
├── skills/           # Bundled skills (declared in plugin.json)
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

| Hook | When called |
|---|---|
| `on_session_start(components)` | Once before the interactive loop |
| `on_turn_end(event)` | After each assistant turn |
| `on_session_end(event)` | When the session ends |
| `on_pre_tool(event)` | Before each tool call (return `action="block"` to veto) |
| `on_post_tool(event)` | After each tool call |
| `compose_system_prompt(current)` | Append rules to the system prompt |
| `register_slash_commands()` | Expose slash commands |

### Built-in plugins

| Plugin | Description |
|---|---|
| `evolution` | Detects user corrections, extracts behavioral rules, and scores sessions for continuous improvement |

### User plugins

Place plugins under `~/.agent/plugins/`. User plugins with the same name override built-in plugins. Disable any plugin via config:

```json
{"plugins": {"evolution": {"enabled": false}}}
```

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

## Project Layout

```
.
├── agent/
│   ├── core/           # BaseAgent, AgentContext, attachments, output sink
│   ├── memory/         # LTMStore, MemoryPalace, ConsolidationEngine, StagingBuffer
│   ├── tools/          # ToolRegistry, BuiltinTools, MCPClient, UserToolCatalog
│   ├── runtime/        # TurnInput, TurnResult, TurnRunner, RuntimeSessionState
│   ├── orchestration/  # Parallel, pipeline, rendezvous execution
│   ├── channels/       # Channel ABC, CliChannel, ChannelRunner
│   ├── scheduler/      # SchedulerService, SchedulerStore, triggers, delivery
│   ├── security/       # Shell command blocking
│   ├── skills/         # SkillBundle, SkillCatalog, skill parsing
│   ├── plugins/        # PluginCatalog, AgentPlugin protocol, lifecycle hooks
│   ├── _builtin/       # Built-in plugins and skills
│   ├── cli.py          # Typer CLI (interactive, gateway, scheduler, config, memory)
│   ├── config.py       # Config loading, ModelClientFactory, system prompt composition
│   ├── bootstrap.py    # Component wiring from config
│   ├── evolution.py    # Session scoring, prompt rewriting
│   ├── shared.py       # Paths, defaults, utility functions
│   └── pathing.py      # Path resolution and security
├── channels/
│   └── feishu.py       # Feishu/Lark channel + output sink
├── scripts/
│   └── benchmark_memory.py
├── tests/
├── config.example.json
├── pyproject.toml
└── uv.lock
```

## Testing

```bash
# Full suite
uv run pytest -q

# Specific area
uv run pytest tests/test_builtin_tools.py -q
uv run pytest tests/test_scheduler.py -q

# Memory benchmark
python scripts/benchmark_memory.py --sizes 1000 10000 --search-runs 10
```

Latest verification: `uv run pytest -q` → `555 passed, 1 skipped`
