# Unified Command Routing and Ralph Runtime Design

## Goal

Unify deterministic slash-command behavior across the interactive CLI and
Feishu without moving command semantics into the language model. At the same
time, move Ralph out of the CLI module and make its execution resumable,
cancellable, transport-neutral, and safe.

The user-visible result is:

- core commands behave consistently in CLI and Feishu;
- transport-specific commands remain explicit (`/quit` for CLI, `/send` for
  Feishu);
- unknown slash commands return a deterministic error instead of reaching the
  model;
- skill invocation remains available through `/skill <id>` and `/<skill-id>`;
- `/cancel`, `/now`, and in-flight interjections reach the active Feishu
  session immediately;
- `/ralph <goal>` no longer crashes and uses the same service in every channel.

## Problem

The current implementation has four independent command-like paths:

1. `agent.cli._interactive_loop()` owns most built-in commands.
2. `PluginCatalog` exposes slash handlers, but only the CLI invokes them.
3. `AgentCore` parses direct slash input as a possible skill invocation.
4. Feishu intercepts `/send`, while `ChannelRunner` separately intercepts
   `/cancel` and `/now`.

There is no shared command contract or precedence policy. Consequently, most
README commands work only in the interactive CLI, unknown commands reach the
model, plugin commands are channel-dependent, and help text is duplicated.

Feishu also holds a per-chat lock around the complete handler call. The lock
prevents a second message from reaching `ChannelRunner` while a model turn is
running, so the cancellation and mailbox code downstream of that lock cannot
perform its stated function.

Ralph has additional correctness and safety problems:

- `/ralph <goal>` reads `parts[1]`, although `parts` is only assigned by the
  `/model` branch, causing `UnboundLocalError`;
- resume restarts iteration numbering instead of continuing from persisted
  progress;
- exceptions can escape the command loop without persisting a terminal task
  state;
- verification uses `create_subprocess_shell()` directly and bypasses the
  project's shell safety policy;
- output and progress are coupled to the Rich console.

## First Principles

1. **A command is control-plane input.** A recognized command must be parsed and
   executed deterministically before any LLM call.
2. **One behavior has one owner.** Command routing belongs to one router;
   per-session concurrency belongs to one coordinator; Ralph iteration belongs
   to one service.
3. **Transports adapt, domains decide.** CLI and Feishu convert input and output
   but do not implement core command behavior.
4. **Session-local state must remain session-local.** A command in one Feishu
   chat must not mutate the model or context used by another chat.
5. **Unknown input must fail closed.** Unknown slash commands return an error
   with suggestions instead of being interpreted by the model.
6. **Autonomous verification is less privileged than an interactive shell.**
   Ralph verification may run only commands classified as low risk and never
   through a shell interpreter.
7. **Durable state precedes user-visible success.** Ralph task transitions are
   persisted before completion, interruption, or failure is reported.

## Approaches Considered

### A. Independent Command Router Before AgentCore (Chosen)

Both CLI and channel messages pass through a transport-neutral router before
normal model dispatch. The router owns parsing, command metadata, help,
plugin adaptation, and deterministic results.

This keeps command handling separate from model-turn lifecycle and allows the
same handlers to run in every transport.

### B. Put Commands Inside AgentCore

This provides one entry point, but mixes deterministic control operations with
prompt hooks, model execution, memory staging, and turn maintenance. Commands
that do not constitute model turns would need special cases throughout
`AgentCore`.

### C. Implement Commands as Skills or Tools

This minimizes explicit routing but makes execution dependent on model
interpretation. It cannot reliably implement cancellation, model selection,
exit, or unknown-command errors and is therefore rejected.

## Scope

### In Scope

- a shared command request, descriptor, result, and router;
- migration of current built-in CLI commands to portable handlers;
- shared routing for plugin commands;
- generated `/help` output;
- explicit command/skill precedence and unknown-command handling;
- session-local model override;
- Feishu concurrency repair;
- extraction and hardening of Ralph;
- README command documentation updates;
- focused unit, integration, and concurrency tests.

### Out of Scope

- persisting `/model` selection to configuration;
- making Ralph a detached scheduler or background job;
- capturing arbitrary text printed by legacy third-party plugins;
- redesigning Feishu cards;
- changing the natural-language skill matching policy;
- changing scheduler CLI commands.

## Package Structure

```text
agent/
  commands/
    __init__.py
    models.py       # Request, context, descriptor, result
    router.py       # Parsing, precedence, lookup, dispatch, help
    builtin.py      # Portable built-in command handlers
  ralph/
    __init__.py
    models.py       # RalphTask and status values
    parser.py       # Pure slash-argument parser
    store.py        # Atomic persistence and prefix lookup
    service.py      # Iteration loop and state transitions
    verify.py       # Low-risk argv verification runner
```

`agent.cli` retains Typer service commands and the stdin loop, but it no longer
contains core slash-command implementations or Ralph domain logic.

## Command Model

### CommandRequest

An immutable request contains:

- original text;
- normalized command name;
- argument text with original casing preserved;
- channel name and session ID;
- immutable transport metadata.

Parsing trims surrounding whitespace, requires the first non-whitespace
character to be `/`, compares command names case-insensitively, and never
lowercases arguments.

### CommandContext

The execution context contains:

- runtime components;
- runtime configuration;
- the current `RuntimeSessionState`;
- the current `OutputSink`;
- channel and message metadata;
- a callback for forwarding normalized text into `AgentCore` when a command
  intentionally expands into a model turn.

Handlers must use the context rather than global console or global session
state.

### CommandDescriptor

Each command declares:

- canonical name and aliases;
- help usage and description;
- allowed scopes: `all`, `cli`, or `feishu`;
- concurrency policy: `anytime`, `idle_only`, or `interrupt`;
- async handler.

Core command names are reserved. Plugins cannot silently replace them.
Duplicate plugin names are rejected with an explicit load warning.

### CommandResult

A handler returns a structured result with:

- `handled`;
- optional Markdown/plain response text;
- optional attachments;
- optional `forward_text` for a subsequent model turn;
- optional transport action such as `exit_cli`;
- status level for errors and warnings.

Command exceptions are caught at the router boundary, logged with command and
session metadata, and returned as a user-visible error. They do not terminate
the CLI or gateway.

## Routing Precedence

For idle sessions, routing order is:

1. registered core command or alias;
2. registered plugin command;
3. explicit invocable skill (`/skill <id>` or `/<skill-id>`);
4. deterministic unknown-command response with close matches;
5. non-slash text forwarded to `AgentCore`.

This means a skill cannot shadow a core command. Namespaced plugin commands
continue to work. A direct skill invocation is recognized only when the skill
exists and is user-invocable.

For active sessions:

- `interrupt` commands execute immediately (`/cancel`, `/cancel graceful`,
  `/cancel <new task>`, `/now <message>`);
- `anytime` read-only commands may respond immediately on their own sink;
- `idle_only` commands return a clear busy error and do not enter the mailbox;
- explicit skill invocations are model-turn requests and return the same busy
  error rather than entering the active operation;
- unknown slash commands return the deterministic unknown-command error and
  never enter a queue or reach the model;
- ordinary text enters the current operation only when that operation declares
  interjection support; otherwise it queues as a later turn.

Slash/skill classification therefore always happens before state-based queue
routing. The fail-closed unknown-command rule applies in `idle`, `active`, and
`cancelling` states.

Cancellation semantics are fixed:

- `/cancel` force-cancels the active operation, aborting the current LLM request
  and force-terminating registered child processes;
- `/cancel graceful` requests cooperative cancellation and graceful child
  process termination at the next safe boundary;
- `/cancel <new task>` force-cancels and replaces the pending restart queue with
  the supplied task as its first entry;
- cancellation while idle is an informational no-op;
- a later force cancellation upgrades a pending graceful cancellation;
- repeated cancellation is idempotent apart from restart-queue updates.

Every accepted cancellation gets an immediate acknowledgement on the command's
sink. The original operation emits at most one final cancellation result when
its execution unwinds. A queued restart begins only after that unwind and the
coordinator's cleanup completes.

`/now <message>` adds an urgent interjection when an interjection-capable
operation is active. During a non-interjection command it queues the payload as
the next turn. When idle it forwards the payload as an ordinary new turn.

## Command Inventory

Shared core commands:

- `/help`
- `/memory`
- `/context`
- `/sessions`
- `/session <id>`
- `/export`
- `/tools`
- `/skills`
- `/plugins`
- `/model [name]`
- `/ralph <goal> [--max N] [--verify "command"]`
- `/ralph list`
- `/ralph resume <id>`
- `/cancel [graceful|<new task>]`
- `/now <message>`

Transport-specific commands:

- CLI: `/quit`, `/exit`, `/q`
- Feishu: `/send <path>`

Plugin-provided commands such as `/evolve`, `/generate-tool`, and `/stats` are
shared when their plugin is enabled.

`/help` is generated from descriptors filtered by channel scope. The README
documents the same scopes but is not the runtime source of truth.

## Session-Local Model Selection

The current CLI implementation calls `BaseAgent.set_model()`, which mutates a
shared agent. That is unsafe once `/model` is available to multiple concurrent
Feishu sessions.

Add `model_override: str | None` to `RuntimeSessionState`. `TurnRunner` passes
the override explicitly to `BaseAgent.send_message()`, and provider requests
resolve the effective model per call without mutating `BaseAgent.model`.

`/model` lists models from the active provider. Switching validates that the
requested name is in the configured model list and updates only the current
session. Ralph uses the same override.

## Plugin Compatibility

`PluginCatalog.get_slash_commands()` remains available. The command router
adapts each legacy handler into a plugin `CommandDescriptor` with `idle_only`
concurrency by default.

Legacy handlers continue receiving `(raw_cmd, components)`, but `components`
is a shallow per-invocation overlay that includes:

- `ctx` for the current session;
- `command_context`;
- `command_sink`;
- current channel and session IDs.

The shared components dictionary is never mutated with per-session values.

Handlers may return:

- `CommandResult`;
- a string, always treated as `forward_text` to preserve the existing legacy
  behavior for Python and declarative plugin commands;
- `None`, treated as a handled side-effect-only legacy command.

Portable plugins that need to reply directly return `CommandResult` with
response text. No implicit marker or source-dependent string interpretation is
used.

Built-in evolution handlers are migrated to portable structured results and
must not print directly to `shared.CONSOLE`. Arbitrary console output from
third-party legacy handlers is not captured; such plugins remain executable
but must adopt structured returns for cross-channel output.

## Session Coordination and Feishu Concurrency

`ChannelRunner` becomes the only owner of per-session concurrency decisions.
Feishu schedules each accepted message on the main event loop without holding
a per-chat lock across the handler call.

`RuntimeSessionState` owns two distinct FIFO collections:

- `pending_interjections`: messages that may be drained into the currently
  active model/tool loop;
- `restart_queue`: complete new turns that begin only after cancellation and
  cleanup of the active operation.

The two collections are never aliased. `BaseAgent` drains
`pending_interjections` in place at each existing tool-loop boundary, preserving
arrival order. Urgency is metadata and does not reorder entries. Entries that
arrive after the state becomes `cancelling` go to `restart_queue`, never back
into the operation being stopped.

Every active operation also declares `accepts_interjections`:

- normal model turns and Ralph runs set it to `true` and drain the same
  `pending_interjections` collection at model/tool or Ralph iteration
  boundaries;
- deterministic commands set it to `false`, so ordinary messages received
  during the command append to `restart_queue` and run afterward.

On a normal operation unwind, any interjections that arrived after the final
drain are moved to the front of `restart_queue` in original arrival order. They
become later turns and cannot leak into a future context silently. On a
cancellation unwind, undrained interjections are cleared and the cancellation
result reports how many were not applied; cancellation intentionally invalidates
updates to the stopped task. An explicit `/cancel <text>` keeps its replacement
restart queue and is not overwritten by those discarded interjections.

The coordinator state transitions are:

| State | Input | Action | Next state |
|---|---|---|---|
| `idle` | ordinary text or idle-only command | create token and dispatch | `active` |
| `idle` | `/now <text>` | dispatch `<text>` as an ordinary turn | `active` |
| `idle` | any `/cancel` form | report no active operation; `/cancel <text>` dispatches `<text>` | `idle` or `active` |
| any | unknown slash | return deterministic error; never enqueue | unchanged |
| `active` | explicit skill invocation | return busy error | `active` |
| `active` | ordinary text, interjection-capable operation | append to `pending_interjections` | `active` |
| `active` | ordinary text, non-interjection command | append to `restart_queue` | `active` |
| `active` | `/now <text>`, interjection-capable operation | append urgent interjection | `active` |
| `active` | `/now <text>`, non-interjection command | append payload to `restart_queue` | `active` |
| `active` | `/cancel` or graceful form | signal token and acknowledge | `cancelling` |
| `active` | `/cancel <text>` | force signal; replace restart queue with `<text>` | `cancelling` |
| `active` | anytime command | execute on its own sink | `active` |
| `active` | idle-only command | return busy error | `active` |
| `cancelling` | explicit skill invocation | return busy error | `cancelling` |
| `cancelling` | ordinary text | append to `restart_queue` | `cancelling` |
| `cancelling` | `/cancel <text>` | replace restart queue with `<text>` | `cancelling` |
| `cancelling` | other cancellation | upgrade or acknowledge idempotently | `cancelling` |
| `cancelling` | anytime command | execute on its own sink | `cancelling` |
| `cancelling` | active operation unwinds | start FIFO restart if present, otherwise stop | `active` or `idle` |

Replacing on `/cancel <text>` makes the newest explicit redirection
authoritative. Ordinary messages received afterward append in arrival order and
become subsequent turns. A restart that becomes active uses the normal rules,
so remaining queued entries wait until it completes.

The coordinator performs these steps without awaiting between state lookup and
marking a new operation active:

1. resolve or create the session state;
2. route interrupt/anytime commands;
3. apply the state-transition table, including interjection versus restart
   ownership;
4. otherwise mark the operation active, record its interjection capability, and
   create a fresh cancel token;
5. dispatch a command or model turn;
6. clear active state in `finally`;
7. process the next queued restart message, if present.

Because all callbacks run on one asyncio event loop and the active flag is set
before the first dispatch await, this transition is atomic with respect to
other message tasks. No second lock owns the same ordering policy.

The existing per-sink send-tail serialization remains unchanged; it orders
Feishu API calls without blocking inbound message routing.

## Ralph Design

### Parsing

`ralph.parser` is pure and independently tested. It supports:

- `list`;
- `resume <task-id-prefix>`;
- a goal plus optional `--max N` and quoted `--verify` command.

It uses `shlex` parsing, preserves the goal text, rejects missing option values,
and bounds iterations to a configured range. Invalid input returns usage text
through `CommandResult`; it never raises into the transport loop.

### Task State

Ralph statuses are explicit:

- `running`;
- `complete`;
- `max_iterations_reached`;
- `interrupted`;
- `failed`.

The store continues atomic JSON persistence. Prefix lookup returns a distinct
not-found or ambiguous-prefix result instead of loading an arbitrary task.

Resume starts at `current_iteration + 1` and never executes more than the
persisted `max_iterations`. A completed task cannot be resumed. An exhausted
task requires an explicit future extension feature and is not silently granted
more iterations in this change.

### Execution Service

`RalphService` owns the iteration loop. Dependencies are injected:

- agent/turn execution dependency;
- task store;
- verification runner;
- context manager;
- progress observer backed by the current `OutputSink`;
- session cancel token and model override.

Each iteration uses a fresh `AgentContext`, as today, and receives task state
plus recent progress. Progress output is emitted through a Ralph observer; the
service contains no Rich or Feishu imports.

The turn dependency returns a `RalphIterationResult` containing content,
tool-call names, and an optional execution error. One iteration follows this
order:

1. check cancellation;
2. compute `iteration_number = current_iteration + 1` without advancing the
   durable cursor;
3. build the prompt from the goal, completion criteria, and recent progress;
4. execute one model/tool turn in a fresh context;
5. if execution returns an error, record it and transition to `failed`;
6. record a bounded content summary and tool calls;
7. when a verifier is configured, run it after every error-free iteration;
8. atomically persist the iteration result, set `current_iteration` to
   `iteration_number`, and store the selected next state;
9. emit progress only after persistence succeeds.

`current_iteration` therefore means the last durably recorded attempt. If the
process exits during a model or verifier call, resume repeats that uncommitted
iteration rather than skipping it.

Completion is deterministic:

- with a verifier, exit code `0` is the only completion signal; the promise
  token alone is insufficient;
- without a verifier, the configured completion promise in model content marks
  the task complete;
- verifier nonzero exit, timeout, or ordinary test failure appends a bounded
  diagnostic to progress and continues to the next iteration;
- verifier setup/infrastructure errors transition the task to `failed`;
- if no completion signal occurs by `max_iterations`, the task becomes
  `max_iterations_reached`.

The latest verifier diagnostic is included in the next iteration prompt so the
model can react to the actual failure.

The service checks cancellation before each iteration and publishes the same
cancel token to the iteration context so an in-flight model or tool operation
can be aborted. It persists after every iteration and before reporting any
terminal status.

All `Exception` instances from turn execution, verification, or service logic,
other than store-write failures described below, become `failed` with a bounded
diagnostic. `asyncio.CancelledError` and
cooperative/force token cancellation become `interrupted`. Observer/output
exceptions are logged and ignored because delivery failure must not change task
truth.

If a nonterminal or terminal store write fails, execution stops immediately and
returns a durability error. The service must not report the unpersisted state as
complete, failed, interrupted, or exhausted. Therefore every reported terminal
outcome has first been durably stored; a storage outage is reported separately
rather than pretending the terminal transition succeeded.

Post-task memory staging remains once per Ralph run, not once per iteration.

### Verification Safety

The verification runner:

1. parses the command with `shlex.split()`;
2. evaluates the original text with `shell_command_check()` and configured
   blocked commands;
3. accepts only `allowed=True` and `risk_level="low"`;
4. rejects confirmation-required, medium-risk, high-risk, empty, or malformed
   commands;
5. launches with `asyncio.create_subprocess_exec()` rather than a shell;
6. runs in the workspace root with a controlled environment;
7. enforces a timeout and retains at most the last 64 KiB from each output
   stream;
8. starts a new process session and, on cancellation or timeout, sends SIGTERM
   to the process group, waits a short bounded grace period, then sends SIGKILL.

The controlled environment contains only `PATH`, `HOME`, `LANG`, `LC_ALL`,
`LC_CTYPE`, `TMPDIR`, and `VIRTUAL_ENV` when present, plus
`AGENT_WORKSPACE_ROOT` and `AGENT_OUTPUT_DIR`. It does not forward provider API
keys or arbitrary configured secrets.

This permits normal checks such as `pytest tests/` while rejecting shell
operators, inline interpreters, destructive commands, and commands requiring
interactive confirmation.

## CLI Flow

The CLI loop remains responsible for prompting and Ctrl+C signal adaptation.
For each input it creates a `CommandRequest` and calls the same coordinator and
router used by channels. `exit_cli` ends the prompt loop after normal cleanup.

Ctrl+C remains the way to cancel a currently blocking CLI operation because
stdin cannot accept `/cancel` while the prompt loop is awaiting that operation.
`/help` describes this distinction explicitly.

## Output Rules

Handlers return portable text and attachments. Transport adapters render them
through `OutputSink`; handlers do not construct Rich tables or call Feishu APIs.

- CLI may render response text as Markdown.
- Feishu may render the same text using its existing plain/post/card selection.
- `/export` queues the generated Markdown file as an attachment in Feishu and
  reports its path in CLI.
- `/send` resolves paths with the existing output-directory containment rules.

Every async command path drains a drain-capable sink before returning.

## Error Handling and Observability

Command dispatch emits structured runtime/log events for:

- command received;
- command handled;
- command rejected by scope or busy policy;
- command failed;
- command forwarded to a skill or model turn.

Logs include command name, session ID, channel, duration, and outcome but do not
log full sensitive arguments. User errors return concise usage or validation
messages. Internal errors log a traceback and return a stable error response.

## Testing

Tests are written before implementation changes.

### Command Unit Tests

- parsing preserves argument case and trims surrounding whitespace;
- descriptor scope and concurrency policy;
- reserved-name and duplicate-plugin behavior;
- generated help is filtered by channel;
- exact precedence: core, plugin, skill, unknown slash, ordinary text;
- unknown commands include close matches and never reach `AgentCore`;
- unknown slash and explicit skill behavior remains deterministic while active
  or cancelling;
- handler exceptions become failed command results.

### Cross-Transport Integration Tests

- the same shared command returns equivalent content in CLI and Feishu;
- CLI-only and Feishu-only commands are rejected outside their scope;
- plugin string/structured/`None` results use the compatibility contract;
- per-session `/model` overrides do not affect another concurrent session;
- `/export` returns an attachment through both output paths.

### Feishu Concurrency Tests

- a second same-chat `/cancel` reaches `ChannelRunner` while the first turn is
  blocked and triggers the active cancel token;
- `/now` and ordinary follow-up messages enter the active mailbox;
- late undrained interjections become subsequent turns after normal completion
  and are explicitly reported as unapplied after cancellation;
- ordinary messages received during non-interjection commands queue as later
  turns;
- idle-only commands receive a busy response;
- different chats remain independent;
- removing the handler-wide lock does not duplicate normal turns.

### Ralph Tests

- `/ralph <goal>` reproduces the old crash before the fix and starts normally
  afterward;
- parser coverage for goal, list, resume, max, verify, quoting, and bad input;
- iteration state is saved after every iteration;
- resume begins after the persisted iteration;
- complete, exhausted, interrupted, and failed statuses persist correctly;
- ambiguous task prefixes are rejected;
- low-risk verification runs without a shell;
- shell operators and medium/high-risk verification commands are rejected;
- timeout and cancellation terminate verification processes;
- CLI and Feishu use the same Ralph service and receive progress.

Finally, run the full test suite and update the README verification count only
from actual output.

## Documentation Changes

Replace the single ambiguous command table with:

- shared commands;
- CLI-only commands;
- Feishu-only commands;
- skill invocation syntax;
- plugin command return contract.

Document that `/cancel` is asynchronous in Feishu, while Ctrl+C is the active
CLI cancellation mechanism. Remove duplicated hand-maintained command lists
where runtime-generated help is sufficient.

## Acceptance Criteria

1. No shared built-in or plugin slash command is implemented solely in
   `_interactive_loop()` or `FeishuChannel`.
2. Shared commands behave deterministically in CLI and Feishu.
3. Unknown slash commands never reach the model.
4. Model selection is isolated per runtime session.
5. Same-chat cancellation and mailbox messages reach an active Feishu turn.
6. `/ralph <goal>` does not crash, resume continues correctly, and every
   reported terminal state was persisted before delivery; persistence failure
   produces a distinct durability error.
7. Ralph verification cannot bypass shell safety or invoke a shell interpreter.
8. Command and Ralph domain code has no Rich or Feishu dependency.
9. Focused tests and the full existing suite pass.
