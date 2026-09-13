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

### Key capabilities at a glance

| Capability | How |
|---|---|
| **Intent-before-action** | Write/shell tools require the assistant to declare what it will do before executing |
| **Unified event stream** | Every tool call, hook, and lifecycle fact is a replayable `RuntimeEvent` |
| **LLM retry** | Transient API errors (rate limit, 5xx) retried 3x with exponential backoff |
| **Config validation** | Startup warnings for typos and invalid values — never blocks startup |
| **Named sessions** | `--name prod` for isolated session data with shared config by default |
| **Plugin hooks** | 8 lifecycle hooks: prompt submit, tool matchers, command hooks, continue loop |
| **Vision** | Image attachments sent directly to vision-capable models (Anthropic, OpenAI) |
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

`tests/test_retrieval_quality.py` guards the pinned baseline in CI. Re-pin
with `--save-baseline` after an intentional change, and read the tag
breakdown first.


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

The React source lives under `frontend/`. Development builds stay in
`frontend/dist/`; they do not modify the Python package tree. To refresh the
bundle embedded in a packaged release, run `npm run build:release` from that
directory.

```bash
# Install web dependency
uv sync --extra web

# Start gateway with the web channel enabled
uv run simple gateway

# In a named session
uv run simple gateway --name prod
```

Configure in `~/.agent/config.json`:
```json
{
  "channels": {
    "web": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 8787,
      "auth_token": "",
      "cors_origins": ["http://localhost:5173"]
    }
  }
}
```

The gateway serves a built-in React + Ant Design UI at
`http://127.0.0.1:8787/`:

```bash
uv sync --extra web
uv run simple gateway
# open http://127.0.0.1:8787/
```

The React UI provides chat, session management, plugin/skill browsing, settings
editing, command palette and streamed responses, all through the same
`/api/...` endpoints.  Or use the API directly:

| Method | Path | Description |
|---|---|---|
| GET | `/` | Built-in React UI |
| GET | `/api/health` | Health check |
| GET | `/api/commands` | Commands available in the web channel |
| GET | `/api/config` / POST | Read masked config / save config |
| GET | `/api/plugins` | List loaded plugins |
| GET | `/api/skills` | List loaded skills |
| GET | `/api/context` | Context-manager statistics |
| GET | `/api/sessions` | List sessions in the current agent home |
| POST | `/api/sessions` | Create a session id |
| PATCH | `/api/sessions/{id}` | Rename a session (`{"title": "..."}`) |
| DELETE | `/api/sessions/{id}` | Delete a session's durable history |
| POST | `/api/sessions/{id}/reveal` | Reveal the session's isolated agent home in Finder/file manager |
| POST | `/api/plugins/{name}/toggle` | Enable/disable a plugin (`{"enabled": true/false}`) |
| GET | `/api/sessions/{id}/messages` | Recent messages for a session |
| POST | `/api/sessions/{id}/messages` | Send a message (non-streaming) |
| WS | `/api/sessions/{id}/stream` | Streaming events (`stream_chunk`, `tool_start`, `status`, `confirm_request`, `attachment`, `turn_complete`, …) |
| GET | `/api/files?path=…&session_id=…` | Download a file owned by a session (its home: `output/`, `uploads/`, plus the workspace folder picked for it) |
| GET | `/api/files?path=…&task_id=…&run_id=…` | Download the recorded output of a scheduled run |

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
| `audio.transcription_command` | External STT argv-style command template (`{path}`, `{language}` placeholders; shell operators are rejected) |
| `mcp_servers` | MCP server definitions (name, command, args, env) |
| `plugins` | Per-plugin enable/disable (`{"evolution": {"enabled": false}}`) |
| `user_tools.enabled` | Trust every Python tool in `~/.agent/tools/*.py`. Off by default; individually approved tools load either way (see [Authoring user tools](#authoring-user-tools)) |
| `evolution` | Enable/disable session scoring and rule learning |
| `scheduler` | Poll/lease/concurrency settings |
| `tavily_api_key` | Optional Tavily search API key |
| `output_dir` | Override default `~/.agent/output` |
| `file_access` | Startup-only workspace read/write policy plus resource limits for file tools (see [File access](#file-access)) |
| `permissions.shell_level` | Default shell permission level: `ask`, `medium`, `high`, or `full` (see [Shell permissions](#shell-permissions)) |
| `permissions.shell_sandbox` | OS sandbox mode: `restricted`, `read_all` (default), or `none` (danger-full-access, `full` level only) |
| `permissions.shell_secret_paths` | Extra home-relative paths the sandboxed shell may neither read nor write (e.g. `[".ssh", ".docker", ".kube"]`); extends the built-in secret set |
| `permissions.shell_devices` | Device/service access (Metal/IOKit) inside the sandbox; **default `true`** (set `false` for the strictest posture) |
| `shell_allowed_commands` | Persistent shell allowlist that skips confirmation (see [Shell permissions](#shell-permissions)) |
| `assistant_identity` | Deterministic assistant name/role for fact recall |
| `system_prompt_file` | Load custom system prompt from `.md` or `.txt` |

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
| `/sessions` | List named sessions and recent scored history |
| `/session <id>` | View session details by ID prefix |
| `/tools` | List available tools |
| `/skills` | List available skills |
| `/plugins` | List loaded plugins |
| `/model [name]` | Show or switch the session model |
| `/permissions [level\|default <level>]` | Show or set the shell permission level |
| `/auto-approve on\|off\|status` | Session shortcut for high-risk auto-approval |
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
| Memory | `memory_write`, `memory_read`, `memory_search`, `memory_index`, `set_identity` |
| Context | `context_retrieve` |
| Scheduling | `schedule_create`, `schedule_list`, `schedule_delete` |
| Web | `web_search`, `web_fetch`, `tavily_search` |
| Output | `clean_output` |
| Orchestration | `spawn_agent` |
| Skills | `activate_skill`, `list_skill_files`, `read_skill_file`, `create_skill`, `update_skill`, `delete_skill`, `write_skill_file` |
| User tools | `create_tool`, `update_tool`, `delete_tool`, `list_tools`, `install_tool_dependency` |

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
| `remote-agent` | Delegate tasks to remote AI agents (Codex, Claude Code) on other machines via SSH |
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
- **LLM retry**: transient API errors (rate limits, 5xx) retried with exponential backoff

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
│   ├── memory/         # LTMStore, MemoryPalace, ConsolidationEngine, StagingBuffer
│   ├── tools/          # ToolRegistry, BuiltinTools, MCPClient, UserToolCatalog, executor
│   ├── exec/           # SubprocessProvider seam: one place children are spawned
│   ├── runtime/        # AgentCore, TurnInput, TurnResult, TurnExecution, TurnRunner
│   ├── orchestration/  # OrchestrationPlanner, parallel/pipeline/rendezvous execution
│   ├── channels/       # Channel ABC, CliChannel, ChannelRunner, Feishu/Lark channel
│   ├── scheduler/      # SchedulerService, SchedulerStore, triggers, delivery
│   ├── security/       # Shell command blocking; sandbox/ = policy + per-OS backends
│   ├── skills/         # SkillBundle, SkillCatalog, skill parsing, hot-reload
│   ├── plugins/        # PluginCatalog, AgentPlugin protocol, HookResult, lifecycle
│   ├── _builtin/       # Built-in plugins (evolution) and skills (daily-summary, etc.)
│   ├── cli.py          # Typer CLI (interactive, gateway, scheduler, config, memory)
│   ├── config.py       # Config loading, validation, ModelClientFactory, system prompt
│   ├── bootstrap.py    # Component wiring from config
│   ├── evolution.py    # Session scoring, prompt rewriting, tool generation
│   ├── shared.py       # Paths, defaults, tracing, named-session support
│   └── pathing.py      # Path resolution and workspace containment
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

`tests/test_sandbox_conformance.py` spawns real sandboxed children and asks the
OS what they are actually permitted to do, rather than reading the generated
profile. It skips only when the host has no enforcing sandbox at all — so if
macOS detection ever breaks, those tests fail instead of quietly skipping while
nothing is enforced. `tests/test_filesystem_sandbox.py` keeps what is genuinely
seatbelt-specific: the generated `.sb` text and its cache key.

Latest verification: `uv run pytest -q` → `1757 passed, 1 skipped`
