# Unified Command Routing and Ralph Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one deterministic slash-command runtime for CLI and Feishu, repair in-flight channel control, and replace the CLI-bound Ralph loop with a safe resumable service.

**Architecture:** Introduce a focused `agent.commands` package for parsing, descriptors, routing, portable handlers, and session coordination. Introduce an `agent.ralph` package for task state, parsing, persistence, safe verification, and execution. CLI and Feishu become adapters around the shared coordinator; `AgentCore` remains responsible only for model turns.

**Tech Stack:** Python 3.11 dataclasses and asyncio, Typer/Rich adapters, existing `OutputSink`, `CancelToken`, `AgentCore`, shell security classifier, pytest.

**Design spec:** `docs/superpowers/specs/2026-07-25-unified-command-routing-and-ralph-design.md`

---

## File Map

**Create:**

- `agent/commands/__init__.py`: stable command API exports.
- `agent/commands/models.py`: immutable request/result/descriptor/context types.
- `agent/commands/router.py`: parsing, command/skill precedence, help, plugin registration.
- `agent/commands/coordinator.py`: per-session operation state and queue ownership.
- `agent/commands/builtin.py`: portable built-in handlers and registry construction.
- `agent/ralph/__init__.py`: stable Ralph exports.
- `agent/ralph/models.py`: task, status, iteration, and verification results.
- `agent/ralph/parser.py`: pure `/ralph` argument parser.
- `agent/ralph/store.py`: atomic task persistence and prefix lookup.
- `agent/ralph/verify.py`: low-risk argv verifier.
- `agent/ralph/service.py`: transport-neutral iteration state machine.
- `tests/test_commands.py`: router, handlers, coordinator, and model-isolation tests.
- `tests/test_ralph.py`: parser, store, verifier, and service tests.

**Modify:**

- `agent/runtime/contracts.py`: session operation state, queues, and model override.
- `agent/core/agent.py`: request-scoped effective model and interjection queue.
- `agent/bootstrap.py`: construct router/coordinator/Ralph dependencies.
- `agent/channels/base.py`: delegate message routing to the shared coordinator.
- `channels/feishu.py`: remove handler-wide per-chat serialization and move `/send` into routing.
- `agent/plugins/catalog.py`: expose plugin command metadata without changing hook behavior.
- `agent/_builtin/plugins/evolution/__init__.py`: structured portable command results.
- `agent/cli.py`: remove built-in/Ralph command bodies and delegate input.
- `agent/__init__.py`: remove Ralph definitions and re-export the new stable API.
- `README.md`: channel-scoped command documentation.
- Existing focused tests where compatibility assertions belong.

---

### Task 1: Command Contracts and Deterministic Router

**Files:**
- Create: `agent/commands/__init__.py`
- Create: `agent/commands/models.py`
- Create: `agent/commands/router.py`
- Create: `tests/test_commands.py`

- [ ] **Step 1: Write failing contract and parser tests**

Cover immutable `CommandRequest`, case-preserving arguments, surrounding
whitespace, descriptor scope, and `CommandResult` defaults.

```python
def test_parse_command_preserves_argument_case():
    request = parse_command("  /model DeepSeek-Chat  ")
    assert request.name == "model"
    assert request.args == "DeepSeek-Chat"
```

- [ ] **Step 2: Run the focused tests and confirm red**

Run: `uv run pytest tests/test_commands.py -q`
Expected: FAIL because `agent.commands` does not exist.

- [ ] **Step 3: Implement minimal command models and parsing**

Define:

```python
@dataclass(frozen=True)
class CommandDescriptor:
    name: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    usage: str = ""
    description: str = ""
    scopes: frozenset[str] = frozenset({"all"})
    concurrency: Literal["anytime", "idle_only", "interrupt"] = "idle_only"
    accepts_interjections: bool = False
```

`CommandContext` carries components, config, session state, sink, and immutable
turn metadata. `CommandResult` carries response text, attachments,
`forward_text`, action, and error level.

- [ ] **Step 4: Write failing precedence and help tests**

Cover core-over-plugin precedence, plugin-over-skill precedence, explicit
invocable skill recognition, unknown slash suggestions, ordinary text fallthrough,
reserved core names, and help filtered by scope.

- [ ] **Step 5: Implement `CommandRouter` minimally**

Expose separate `classify()` and `execute()` methods so session policy is
decided before side effects. Classification returns one of `command`, `skill`,
`unknown_slash`, or `text`. Never call the model from the router.

- [ ] **Step 6: Verify and commit**

Run:

```bash
uv run pytest tests/test_commands.py -q
uv run pytest tests/test_agent_integration.py -k skill -q
git diff --check
```

Expected: all selected tests pass.

Commit: `feat: add deterministic command router`

---

### Task 2: Session Operation State and Coordinator

**Files:**
- Create: `agent/commands/coordinator.py`
- Modify: `agent/runtime/contracts.py`
- Modify: `agent/runtime/__init__.py`
- Test: `tests/test_commands.py`
- Test: `tests/test_runtime_contracts.py`

- [ ] **Step 1: Write failing session-state tests**

Add tests for:

- `operation_state`: `idle`, `active`, `cancelling`;
- `accepts_interjections`;
- separate `pending_interjections` and `restart_queue` lists;
- `model_override`;
- compatibility metadata pointing at `pending_interjections`, not restart data.

- [ ] **Step 2: Run tests and confirm red**

Run: `uv run pytest tests/test_runtime_contracts.py tests/test_commands.py -q`
Expected: FAIL on missing state fields/coordinator.

- [ ] **Step 3: Implement the state fields and coordinator shell**

Keep state transitions synchronous until the active flag and fresh cancel token
are installed. The coordinator accepts injected `AgentCore` and router
dependencies for isolated tests.

- [ ] **Step 4: Write failing state-transition tests**

Cover:

- ordinary active input entering interjections only for capable operations;
- ordinary input during deterministic commands entering `restart_queue`;
- `/now` idle/active/non-capable behavior;
- force, graceful, upgrade, repeated cancel, and cancel-with-restart;
- unknown slash fail-closed in every state;
- explicit skill busy behavior;
- late interjection promotion on normal completion;
- explicit discard count on cancellation;
- FIFO restart draining;
- command received/handled/rejected/failed/forwarded runtime events;
- every command path drains a drain-capable sink before returning.

- [ ] **Step 5: Implement minimal coordinator transitions**

Use one `handle(TurnInput, RuntimeSessionState, OutputSink)` entry point. Route
command results through the sink, dispatch `forward_text` through `AgentCore`,
and always clear state in `finally`.

- [ ] **Step 6: Verify and commit**

Run:

```bash
uv run pytest tests/test_runtime_contracts.py tests/test_commands.py -q
git diff --check
```

Commit: `feat: add session command coordinator`

---

### Task 3: Portable Built-In Commands

**Files:**
- Create: `agent/commands/builtin.py`
- Modify: `agent/commands/router.py`
- Test: `tests/test_commands.py`
- Reference: `agent/cli.py:530-967`

- [ ] **Step 1: Write failing handler tests**

Use fake components/state/sinks to cover:

- `/help`, `/memory`, `/context`, `/sessions`, `/session`;
- `/tools`, `/skills`, `/plugins`;
- `/export` response and queued attachment;
- command scope for `/quit` and `/send`;
- validation and user-facing errors.

- [ ] **Step 2: Confirm the tests fail**

Run: `uv run pytest tests/test_commands.py -k builtin -q`
Expected: FAIL because built-in registration is absent.

- [ ] **Step 3: Implement portable Markdown/string handlers**

Handlers return `CommandResult`; they must not construct Rich objects, call
`shared.CONSOLE`, or import Feishu. Generate help from registered descriptors.
Use existing path containment for `/send` and `OutputSink.queue_attachment()`.

- [ ] **Step 4: Verify and commit**

Run:

```bash
uv run pytest tests/test_commands.py -k 'builtin or help or export or send' -q
uv run pytest tests/test_output_dir.py -q
git diff --check
```

Commit: `feat: add portable built-in commands`

---

### Task 4: Request-Scoped Model Override

**Files:**
- Modify: `agent/runtime/contracts.py`
- Modify: `agent/runtime/contracts.py:156-172`
- Modify: `agent/core/agent.py:1048-1055`
- Modify: `agent/core/agent.py:2013-2241`
- Modify: `agent/core/agent.py:2568-2586`
- Modify: `agent/commands/builtin.py`
- Test: `tests/test_runtime_contracts.py`
- Test: `tests/test_commands.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write failing concurrent model-isolation tests**

Create two runtime states sharing one fake `BaseAgent`, set different model
overrides, run concurrently, and assert each transport call receives its own
model while `agent.model` remains unchanged.

- [ ] **Step 2: Confirm red**

Run: `uv run pytest tests/test_commands.py -k model -q`
Expected: FAIL because model selection mutates the shared agent or is ignored.

- [ ] **Step 3: Implement effective-model resolution**

Publish `state.model_override` into the per-turn context. Add one
`BaseAgent._effective_model(ctx)` helper and use it for the main create/stream
path, heartbeat detail, sub-agent construction, and active-context helper calls.
Do not mutate `self.model`.

- [ ] **Step 4: Implement `/model` validation**

List configured models when no argument is provided. Reject names outside the
active provider's configured model list. Update only `state.model_override`.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/test_commands.py -k model -q
uv run pytest tests/test_runtime_contracts.py tests/test_agent_integration.py -k 'model or turn_runner' -q
git diff --check
```

Commit: `feat: isolate model selection per session`

---

### Task 5: Plugin Command Adapter and Evolution Migration

**Files:**
- Modify: `agent/commands/router.py`
- Modify: `agent/plugins/catalog.py:1324-1328`
- Modify: `agent/_builtin/plugins/evolution/__init__.py:216-290`
- Modify: `README.md:150-220`
- Test: `tests/test_commands.py`
- Test: `tests/test_plugin_catalog.py`

- [ ] **Step 1: Write failing compatibility tests**

Cover Python and declarative plugin handlers returning:

- `CommandResult` as a direct portable reply;
- string as legacy `forward_text`;
- `None` as handled side effect;
- exception as a stable error result.

Assert the per-invocation components overlay contains the current state context
without mutating the shared components dict.

- [ ] **Step 2: Confirm red**

Run: `uv run pytest tests/test_commands.py -k plugin -q`

- [ ] **Step 3: Implement adapter and migrate evolution commands**

Keep `get_slash_commands()` compatibility. Register plugin descriptors after
core descriptors. Refactor `/evolve`, `/generate-tool`, and `/stats` to return
structured results and use `CommandContext`/overlay values instead of console
printing or global `components["ctx"]`.

- [ ] **Step 4: Verify and commit**

Run:

```bash
uv run pytest tests/test_commands.py -k plugin -q
uv run pytest tests/test_plugin_catalog.py -q
uv run pytest tests/test_agent_integration.py -k 'evolve or generate_tool' -q
git diff --check
```

Commit: `feat: route plugin commands across channels`

---

### Task 6: Ralph Models, Parser, Store, and Safe Verifier

**Files:**
- Create: `agent/ralph/__init__.py`
- Create: `agent/ralph/models.py`
- Create: `agent/ralph/parser.py`
- Create: `agent/ralph/store.py`
- Create: `agent/ralph/verify.py`
- Create: `tests/test_ralph.py`
- Modify: `agent/__init__.py:122-170`

- [ ] **Step 1: Write failing parser/model tests**

Cover start/list/resume, quoted goals, `--max`, quoted `--verify`, missing
values, invalid bounds, and status serialization.

- [ ] **Step 2: Confirm red**

Run: `uv run pytest tests/test_ralph.py -k 'parser or model' -q`

- [ ] **Step 3: Implement pure parser and task/result dataclasses**

Use `shlex` and return a typed parse result or validation error. Define explicit
statuses and `RalphIterationResult`/`VerificationResult`.

- [ ] **Step 4: Write failing store tests**

Cover atomic save/load, exact ID, unique prefix, ambiguous prefix, corrupt file,
and persisted current-iteration cursor.

- [ ] **Step 5: Implement `RalphTaskStore`**

Inject the tasks directory. Preserve compatibility with existing JSON fields
and default missing new status/error fields when loading older tasks.

- [ ] **Step 6: Write failing verifier tests**

Cover low-risk argv execution, shell operators, medium/high-risk commands,
malformed/empty commands, environment allowlist, output tail cap, timeout,
cancellation, SIGTERM/SIGKILL process-group cleanup.

- [ ] **Step 7: Implement safe verifier**

Call `shell_command_check()`, require low risk, execute with
`create_subprocess_exec(..., start_new_session=True)`, and bound timeout/output.

- [ ] **Step 8: Verify and commit**

Run:

```bash
uv run pytest tests/test_ralph.py -k 'parser or store or verifier' -q
uv run pytest tests/test_shell_security.py -q
git diff --check
```

Commit: `feat: add safe Ralph domain primitives`

---

### Task 7: Ralph Execution Service and Command Handler

**Files:**
- Create: `agent/ralph/service.py`
- Modify: `agent/commands/builtin.py`
- Modify: `agent/bootstrap.py`
- Test: `tests/test_ralph.py`
- Test: `tests/test_commands.py`
- Reference: `agent/cli.py:87-267`

- [ ] **Step 1: Write failing state-machine tests**

Cover:

- promise completion without verifier;
- verifier-authoritative completion;
- nonzero/timeout diagnostics entering the next prompt;
- execution and verifier infrastructure failure;
- cancellation as interrupted;
- max-iteration exhaustion;
- persistence before observer output;
- persistence failure as durability error;
- resume from `current_iteration + 1`;
- observer exceptions not changing task truth;
- post-run memory staging once.
- pending interjections drained in place at each Ralph iteration boundary and
  included in the next iteration prompt with arrival order/urgency preserved;
- interjections arriving during a model/tool step remain available to that
  step's existing `BaseAgent` mailbox drain;
- completed and `max_iterations_reached` tasks are rejected by resume.

- [ ] **Step 2: Confirm red**

Run: `uv run pytest tests/test_ralph.py -k service -q`

- [ ] **Step 3: Implement `RalphService` minimally**

Inject turn executor, store, verifier, context manager, and observer. Keep the
service free of Rich/Feishu imports. Use the session cancel token and
model override for each fresh iteration context. Publish the shared
`pending_interjections` list into the active iteration context for existing
tool-loop drains, then explicitly drain any remaining entries at the next Ralph
iteration boundary and append a structured interjection block to that
iteration's prompt. Clear the same list in place so coordinator and service
retain one queue identity.

- [ ] **Step 4: Implement `/ralph` handler**

Map parser results to list/start/resume service calls. Report ambiguous IDs and
invalid options as command errors. Mark the Ralph descriptor
`accepts_interjections=True` and `idle_only`.

- [ ] **Step 5: Add the original crash regression**

Drive `/ralph demo task` through the command coordinator and assert it starts a
task instead of raising `UnboundLocalError`.

- [ ] **Step 6: Verify and commit**

Run:

```bash
uv run pytest tests/test_ralph.py tests/test_commands.py -k ralph -q
git diff --check
```

Commit: `feat: add resumable Ralph service`

---

### Task 8: ChannelRunner and Feishu Concurrency Integration

**Files:**
- Modify: `agent/channels/base.py:99-432`
- Modify: `channels/feishu.py:1607-1623`
- Modify: `channels/feishu.py:1953-2120`
- Test: `tests/test_channel_layer.py`
- Test: `tests/test_feishu_channel.py`

- [ ] **Step 1: Write failing end-to-end concurrency tests**

Use two same-chat Feishu events while the first handler is blocked. Assert:

- `/cancel` reaches the active token before the first turn completes;
- `/now` and ordinary text reach the correct interjection queue;
- messages during a non-interjection command become restart turns;
- different chats remain independent;
- normal rapid messages are neither duplicated nor lost.

- [ ] **Step 2: Confirm the existing lock causes red**

Run: `uv run pytest tests/test_feishu_channel.py -k 'concurrent or cancel or interjection' -q`
Expected: at least the same-chat cancellation test times out/fails because the
second message is waiting on `_chat_locks`.

- [ ] **Step 3: Delegate channel handling to the coordinator**

Replace `ChannelRunner._make_message_handler()` command/mailbox logic with the
shared coordinator. Retain per-chat state construction, memory workers, runtime
event logging, and session-end hooks.

- [ ] **Step 4: Remove handler-wide Feishu lock**

Schedule accepted events concurrently on the main loop. Retain each sink's
existing `_send_tail` ordering and message ID deduplication. Remove the transport
special case for `/send`; the scoped built-in handler owns it.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/test_channel_layer.py tests/test_feishu_channel.py -q
git diff --check
```

Commit: `fix: enable in-flight Feishu control messages`

---

### Task 9: CLI Migration to the Shared Coordinator

**Files:**
- Modify: `agent/cli.py:429-1029`
- Modify: `agent/bootstrap.py:376-406`
- Modify: `agent/channels/base.py:75-90`
- Test: `tests/test_agent_integration.py`
- Test: `tests/test_commands.py`

- [ ] **Step 1: Write failing CLI parity tests**

Drive representative shared commands through both the CLI adapter and a fake
channel sink and assert equivalent response content. Cover `/quit` action,
unknown slash errors, skill forwarding, plugin forwarding, Ctrl+C token wiring,
and `/ralph` regression.

- [ ] **Step 2: Confirm red**

Run: `uv run pytest tests/test_agent_integration.py -k interactive_loop -q`

- [ ] **Step 3: Remove command bodies from `_interactive_loop()`**

Keep prompt/read/cleanup and signal adaptation. Delegate every non-empty input
to the shared coordinator. Respect `exit_cli` after sink drain. Delete the old
Ralph loop/import aliases only after parity tests cover migrated behavior.

- [ ] **Step 4: Wire router/coordinator dependencies in bootstrap**

Build one router from current core/plugin descriptors and expose a coordinator
factory or shared coordinator dependency without storing per-session state in
the global components dict.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/test_agent_integration.py -k 'interactive_loop or chat_command' -q
uv run pytest tests/test_commands.py -q
git diff --check
```

Commit: `refactor: route CLI commands through shared runtime`

---

### Task 10: Documentation, Compatibility, and Full Verification

**Files:**
- Modify: `README.md:124-150`
- Modify: `README.md:238-288`
- Modify: `README.md:443-462`
- Modify: `README.md:567-607`
- Modify: compatibility exports in `agent/__init__.py`
- Test: all affected and full suites.

- [ ] **Step 1: Update command documentation**

Separate shared, CLI-only, Feishu-only, skill, and plugin commands. Document
Ctrl+C versus Feishu `/cancel`, structured plugin returns, and safe Ralph verify
restrictions. Remove duplicate claims that imply every slash command is handled
by every entry point.

- [ ] **Step 2: Run focused suites**

```bash
uv run pytest tests/test_commands.py tests/test_ralph.py -q
uv run pytest tests/test_runtime_contracts.py tests/test_channel_layer.py tests/test_feishu_channel.py -q
uv run pytest tests/test_plugin_catalog.py tests/test_agent_integration.py -q
```

Expected: all focused suites pass, with only documented skips/warnings.

- [ ] **Step 3: Reproduce both original failures**

Run automated regressions proving:

- `/ralph demo task` does not raise `UnboundLocalError`;
- a same-chat `/cancel` reaches the handler before a blocked first Feishu turn
  completes.

- [ ] **Step 4: Run full verification**

```bash
uv run pytest -q
uv run python -m compileall -q agent channels
git diff --check
git status --short
```

Expected: the full suite and compile check pass; diff check is clean; status
contains only intentional implementation/docs changes.

- [ ] **Step 5: Request final code review**

Use `@superpowers:requesting-code-review` with the spec, this plan, base commit,
and final commit. Resolve blocking findings and rerun the relevant tests.

- [ ] **Step 6: Final verification and commit**

Rerun the full commands from Step 4 after review fixes.

Commit: `docs: document unified command runtime`
