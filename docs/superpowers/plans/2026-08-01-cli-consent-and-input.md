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

## Verification

Full suite passes (`uv run pytest -q`); interactive loop tests continue to
feed inputs through the new `_ask_user_input` seam.
