# Personal Agent

Single-file personal AI agent with:

- provider abstraction for Anthropic and OpenAI-compatible APIs
- interactive chat and single-turn CLI modes
- tool calling (`shell`, file I/O, memory, context retrieval, sub-agents)
- fixed-loci memory palace with SQLite-backed long-term memory
- per-session staging and consolidation
- self-evolution utilities for session scoring and prompt rewriting

## Requirements

- Python `3.11+`
- One model provider credential:
  - `ANTHROPIC_API_KEY`, or
  - `OPENAI_API_KEY`, or
  - `DEEPSEEK_API_KEY`, or
  - `DASHSCOPE_API_KEY`, or
  - a local OpenAI-compatible endpoint such as Ollama

## Dependencies

Required runtime dependencies:

- managed by `uv` from `pyproject.toml`
- runtime packages:
  - `anthropic`
  - `openai`
  - `typer`
  - `rich`
  - `sqlite3` is used from the Python standard library, so no extra install is needed

Optional dependencies:

- `mcp`
  Enables MCP client integration. The dependency is optional, but configured MCP servers can be connected and their tools registered at startup.

Development dependencies:

- `pytest` via the `dev` dependency group

Install with `uv`:

```bash
uv sync --group dev
```

If you want MCP support:

```bash
uv sync --group dev --extra mcp
```

## Configuration

The agent reads config from `~/.agent/config.json`.

Bootstrap from the example file:

```bash
mkdir -p ~/.agent
cp config.example.json ~/.agent/config.json
```

Then edit provider settings and API keys. The config supports:

- `anthropic`
- `openai`
- `deepseek`
- `qwen`
- `ollama`

You can also point any OpenAI-compatible provider at a custom `base_url`.

## Usage

Interactive mode:

```bash
uv run simple
```

Single-turn chat:

```bash
uv run simple chat "Summarize this repository"
```

Show configured providers/models:

```bash
uv run simple config models
```

Show current config:

```bash
uv run simple config list
```

Memory commands:

```bash
uv run simple memory ls
uv run simple memory index
uv run simple memory show identity/user
uv run simple memory search "preferences"
uv run simple memory tidy
```

Evolution commands:

```bash
uv run simple evolve --stats
uv run simple evolve --rewrite
uv run simple evolve --apply-best
```

### Direct `simple` Command

If you want to run `simple` directly instead of `uv run simple`, install the project tool once from this directory:

```bash
uv tool install --editable .
```

After that, the command is available as:

```bash
simple
simple chat "Summarize this repository"
```

## Built-in Tools

The runtime registers these built-in tools:

- `shell`
- `read_file`
- `write_file`
- `list_files`
- `memory_write`
- `memory_read`
- `memory_search`
- `memory_index`
- `context_retrieve`
- `spawn_agent`

Skills are loaded as bundles from:

- user skills: `~/.agent/skills/**/SKILL.md`
- built-in skills: `<repo>/skills/**/SKILL.md`

Skill bundles use `SKILL.md` as the entrypoint and may include supporting files such as templates, examples, and scripts. The runtime exposes progressive-disclosure helpers so the model can activate a skill and inspect bundle files on demand.

Built-in tool behavior:

- file tools are bounded to the current workspace root
- file and memory tools return structured JSON payloads to the model
- shell commands are still powerful, but timeouts now terminate the spawned process group

## Memory Architecture

The current memory system has four layers:

1. `Working memory`
   Current `ctx.messages` and active tool results.

2. `Staging`
   Raw user/assistant turns are written to a per-session JSONL buffer under `~/.agent/context/_staging/`.

3. `Long-term memory`
   Structured memory is stored in `~/.agent/context/palace.db` and projected into JSON snapshots and markdown views.

4. `Memory palace projection`
   Human-readable markdown files are written under `~/.agent/memory/`.

Fixed top-level palace loci:

- `identity`
- `projects`
- `people`
- `concepts`
- `episodes`
- `tasks`
- `procedures`
- `archive`

Legacy alias:

- `knowledge` -> `concepts`

### Consolidation

When enough conversation has accumulated, the context manager can:

- summarize the current session into `episodes`
- extract durable facts into fixed loci
- decay low-value memory
- compress recent working memory

### Retrieval

There are two retrieval paths:

- implicit prompt injection: long-term memory only
- explicit context retrieval: current session + long-term memory

## Project Layout

```text
.
├── agent.py
├── memory_projection.py
├── tool_runtime.py
├── config.example.json
├── docs/
│   └── superpowers/
│       ├── plans/
│       └── specs/
└── tests/
    ├── test_builtin_tools.py
    ├── test_consolidation.py
    ├── test_evolution.py
    ├── test_ltm_store.py
    ├── test_memory_palace_store.py
    ├── test_retriever.py
    └── test_staging.py
```

## Testing

Run the full test suite:

```bash
uv run pytest -q
```

Run the memory benchmark script:

```bash
python scripts/benchmark_memory.py --sizes 1000 10000 --search-runs 10 --write-runs 10
```

The script prints JSON with per-size search and write latency metrics so you can compare performance before and after storage changes.

Save benchmark output for later comparison:

```bash
python scripts/benchmark_memory.py --sizes 1000 10000 --output bench.json
python scripts/benchmark_memory.py --sizes 1000 10000 --compare bench.json
python scripts/benchmark_memory.py --sizes 1000 10000 --output bench.csv
python scripts/benchmark_memory.py --sizes 1000 10000 --output bench.jsonl
```

Current status in this workspace:

- `79` tests passing

## Notes

- `agent.py` is still the main entrypoint, but tool runtime and memory projection code have been extracted into separate modules.
- SQLite is now the source of truth for long-term context memory.
- Markdown memory files are currently a projection layer, not the authoritative store.
- `memory tidy` now performs local retention/projection maintenance instead of a foreground LLM reclassification pass.
- MCP client wiring is implemented for stdio servers. Real MCP smoke coverage is opt-in because it depends on external tools such as `npx` and a configured server.
