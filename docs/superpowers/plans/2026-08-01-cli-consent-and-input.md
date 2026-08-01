# CLI Consent Gate and Input Experience Plan

**Goal:** Make medium-risk shell actions obtain real, attributable human
consent in the terminal, and replace the single-line input with persistent
history and paste-safe async input — without changing agent-core semantics.

## First Principles

1. A model-produced value is never proof of user approval. Approval is a
   human decision made in the human's own medium (the terminal), and it is
   bound to the existing scoped, expiring confirmation token — never to a
   token the model relays as if it were consent.
2. Consent is solicited at the moment of the side effect. A confirmation
   buried inside a tool-error string is not a consent request; the CLI must
   present an actual question before the risky action runs.
3. Every input read has exactly one reader. A consent prompt that runs in a
   background thread can outlive a force-cancelled turn and swallow the
   human's next message; interactive input must be async-native so
   cancellation is clean.
4. Non-interactive contexts degrade honestly. Without a terminal, the CLI
   declines and keeps the structured confirmation-required result; it never
   silently allows.
5. The abstraction boundary stays intact. `OutputSink` owns rendering and
   consent prompts; tool semantics, risk classification, and the allowlist
   remain owned by `agent/security/shell.py`.

## Invariants To Preserve

- Only `shell_command_confirm(token, scope)` can add a command to the
  session allowlist; the token is generated and validated by shell.py.
- A cancelled turn never executes the approved action.
- Existing non-TTY flows (`gateway`, Feishu, `/confirm <token>` command)
  keep their current behavior.
- Tool/risk behavior is unchanged; only the interactive surface changes.

## Changes

- `agent/core/output.py`: add async `OutputSink.on_tool_confirmation(...)`
  (default False); `CliOutputSink` prompts `y/N` in the terminal only.
- `agent/tools/builtin_tools.py`: on a confirmation-required shell command,
  ask the active sink; on approval, redeem the token via
  `shell_command_confirm` and re-check before executing.
- `agent/cli.py`: replace rich `Prompt` input with a `prompt_toolkit`
  `PromptSession` backed by `~/.agent/cli_history` (persistent history,
  paste-safe, async), exposed as `_ask_user_input`.
- `pyproject.toml`/`uv.lock`: add `prompt_toolkit`.
- `tests/test_cli_ui.py`: consent-gate regression tests (approve, decline,
  cancelled-turn, non-terminal sink) and history persistence tests.

## P1: Streaming Markdown and Tool Progress

- `agent/core/output.py`: a line-buffered streaming markdown renderer
  (`_StreamMarkdown`) styles completed lines (headings, inline code/bold/
  italic/links, lists, quotes, rules) and buffers fenced code until the
  closing fence for syntax highlighting; a truncated fence flushes as plain
  lines at turn end.  Terminal scrollback is preserved because lines are
  printed once and never re-rendered.  Non-TTY sinks keep the raw chunk
  path unchanged.
- `agent/core/output.py`: tool progress events with `current`/`total` render
  a transient rich progress bar under the tool line in terminals; without
  totals the existing spinner/text behavior is retained.
- `tests/test_cli_ui.py`: renderer, fence buffering, partial-line, progress
  bar, and non-TTY fallback regressions.

## Verification

Full suite passes (`uv run pytest -q`); interactive loop tests continue to
feed inputs through the new `_ask_user_input` seam.

## P2: Non-TTY Chat Approval and Consent Serialization (follow-up)

Review found that the consent gate left non-terminal sinks on the old
`/confirm <token>` path, where the token is invisible to the human, so a
gateway user's explicit "批准" reply could never redeem a blocked command.
Changes:

- `agent/security/shell.py`: a repeated blocked check for the same scope +
  normalized command reuses the outstanding pending token instead of
  minting a fresh one; add `shell_pending_for_scope` and
  `shell_approve_single_pending` (redeems only when exactly one unexpired
  pending record exists for the scope).
- `agent/commands/coordinator.py`: a short, exact approval reply ("批准",
  "同意", "yes", …) for a text turn redeems the sole pending confirmation
  for that session/channel/user scope and rewrites the forwarded text to
  name the approved command so the agent retries it. Ambiguity (zero or
  several pending) is never auto-approved; the explicit `/confirm <token>`
  path still disambiguates.
- `agent/tools/builtin_tools.py`: the interactive consent flow runs under
  one async lock and re-checks the allowlist inside it, so parallel
  medium-risk shell calls serialize and an identical command approved once
  is not prompted about again; the confirmation-required error now carries
  model-facing approval guidance (terminal y/N vs. gateway "批准").
- `agent/core/output.py`: module-level `_APPROVAL_LOCK` shared by the tool
  consent flow.

## P3: CLI Approval Menu, Gateway Text Consent (follow-up)

The CLI consent question is presented as a numbered option menu instead of
a bare `y/N` prompt: `1) 批准执行` / `2) 拒绝`, confirmed with Enter
(aliases `y`/`yes`/`同意`/`批准` and `n`/`no`/`拒绝`); Ctrl+C cancels.
Gateway channels keep the plain-text reply flow, with “同意” as the
canonical approval phrase (the coordinator also accepts “批准”/“yes” and
disambiguates via `/confirm <token>` when several commands are pending).

Invariants preserved: only `shell_command_confirm(token, scope)` writes the
allowlist; chat approval accepts no command argument and is scoped to the
pending record; high-risk commands remain non-confirmable.

## P4: Consent Must Really Block the Turn (follow-up)

Live runs showed the consent menu was being rendered over by parallel tool
results and heartbeats: the tool loop gathers every call in a batch, so the
other tools kept executing (and printing) while the human was deciding —
making it look like the agent continued without consent. Changes:

- `agent/core/agent.py`: `_execute_regular_tool_calls` runs any batch that
  contains a `shell` call sequentially; a consent-pending shell call now
  blocks the whole batch, so no other tool executes or prints during the
  menu. Batches without shell calls keep the parallel `asyncio.gather` path.
- `agent/core/output.py`: while an approval menu is on screen,
  `CliOutputSink` buffers all renders (`on_tool_start/end/progress/blocked`,
  heartbeats, status, info, error, notifications, stream chunks,
  sub-agent events) and flushes them only after the menu closes; the shared
  `_consent_pending()` helper reports whether a consent prompt is open.
- `agent/tools/executor.py`: while `_consent_pending()`, the tool deadline
  is extended instead of cancelling the call, so a slow human decision is
  never silently dropped as a timeout.

Invariants preserved: parallel execution remains for non-shell batches; the
menu is still the only path into the allowlist; cancellation during the menu
still declines (fail closed).

## P5: Persistent Allowlist and Session Auto-approve (follow-up)

Personal-assistant runs kept asking for approval per command.  Added two
ways to reduce friction without touching high-risk blocking:

- `shell_allowed_commands` config: a persistent allowlist.  A full command
  string matches exactly; a bare command name (e.g. `osascript`) allows every
  invocation of that command.  Managed at runtime with `/allow <command>` and
  `/deny <command>` (both write the config and update the live registry
  context; sub-agents inherit the allowlist like `shell_blocked_commands`).
- `/auto-approve on|off|status`: per-session toggle (scoped by
  session/channel/user) that skips medium-risk confirmation for the rest of
  the session.  In-memory only; high-risk commands and shell-operator blocks
  stay unconditional.

Invariants preserved: only the security module decides risk; pre-approval
never bypasses high-risk commands, destructive options, or operator blocks;
the config allowlist is opt-in and persisted by the user's own slash command.

## P6: Permission Levels (follow-up)

Added Codex-style shell permission levels instead of a binary ask/allow:

- `ask` (default): low-risk auto, medium-risk asks, high-risk blocked.
- `medium`: medium-risk also auto-allowed (equals `/auto-approve on`).
- `high`: high-risk commands and destructive options auto-allowed; the
  user-configured `shell_blocked_commands` blacklist and the
  operator/pipe-pattern guards still apply.
- `full`: everything allowed except `shell_blocked_commands` and
  parse-fail-closed.

Configuration: `permissions.shell_level` in config.json is the default for
every session (backfilled for old configs, validated at load).  Runtime:
`/permissions` shows the effective level, `/permissions <level>` overrides
the current session only (scoped by session/channel/user, in-memory),
`/permissions default <level>` persists the config default and applies it to
the live registry.  Sub-agents inherit the config default via registry
context, not the session override (sub-agent sessions are distinct).

Invariants preserved: `shell_blocked_commands` blocks at every level; the
allowlist, per-command confirmation, and consent menu remain the only paths
into medium-risk execution when the level is `ask`.

## P7: One Tier Down — Only High-Risk Asks (follow-up)

The default consent gate moved down one tier: medium-risk commands
(rm/mv/curl/ssh/interpreters, script files, absolute paths) now run
automatically, and only high-risk constructs ask the human — and they are
now confirmable instead of hard-blocked:

- `ask` (default): low/medium auto; high-risk commands, destructive options,
  pipe-to-shell patterns, and plain shell operators/redirection require
  confirmation and run after approval.
- `medium`: high-risk commands/options/patterns auto-allowed; plain
  operators still ask.
- `high`/`full`: everything auto-allowed.

Unchanged: `shell_blocked_commands` blocks at every level; cwd escapes and
command substitution stay non-confirmable; parse failures fail closed.  The
absolute-path recovery hint was removed with its branch (absolute paths are
now medium-risk and auto-allowed).
