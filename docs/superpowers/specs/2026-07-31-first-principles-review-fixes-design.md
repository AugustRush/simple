# First-Principles Review Fixes Design

## Goal

Fix the eight confirmed correctness, security, and reliability defects from the
2026-07-31 repository review without replacing the existing public runtime,
tool, scheduler, or transport APIs unnecessarily.

## Design Principles

1. Authorization must carry user provenance. A value returned to the model is
   not proof that a user approved an action.
2. Filesystem and network destinations must be canonicalized and checked at the
   boundary where the side effect occurs.
3. A scheduled run may mutate task ownership only while it holds the current
   lease. Claim, renew, recover, and complete operations must be fenced.
4. A terminal state must describe the real outcome. Failed delivery and
   truncated output cannot be reported as success.
5. Context management must verify the post-condition, not merely invoke a
   compaction function.
6. Calendar schedules are defined in local wall-clock time, not by fixed UTC
   durations.

## Shell Authorization And Classification

Replace literal substring matching for inline interpreter execution with token
classification based on `shlex`. Interpreter variants such as `python3.11`,
extra whitespace, and flags before `-c` must all receive the same risk level.
Add explicit destructive-option classification for commands such as
`find -delete`.

The shell tool will no longer accept a confirmation token supplied by the
model. A restricted call creates a pending confirmation record containing the
exact normalized command, `session_id`, `channel_name`, optional `user_id`, and
an absolute expiry of 5 minutes. The record is one-time use. The user approves
it through a portable `/confirm <token>` command; the coordinator, not a tool
call, supplies the session/channel identity to the redemption function. A
confirmation is accepted only when all identity fields match. The later shell
invocation is allowed only when its normalized command is byte-for-byte equal
to the command stored by that approval; `/confirm` itself does not accept a
command argument. The resulting allowlist is keyed by `(session_id,
channel_name, user_id)` and expires with the approval record. High-risk
commands remain non-confirmable. Parsing failures and malformed quoting fail
closed.

## Plugin Path Boundary

Explicit plugin names must obey the same slug grammar as derived names. Resolve
the target before clone/copy and require it to be a strict child of
`USER_PLUGINS_DIR`. Reject absolute names, separators, traversal components,
empty slugs, and the plugins root itself before performing any I/O.

## Web Fetch Network Boundary

Validate HTTP and HTTPS destinations before opening them and after every
redirect. Use a manual redirect handler with a maximum of 5 hops; never let
`urlopen` follow redirects implicitly. Resolve every hostname and reject the
URL if any returned address is loopback, private, link-local, multicast,
reserved, unspecified, or an IPv4-mapped IPv6 address in those ranges. Literal
IPv4 and IPv6 forms are checked the same way. The request must connect through
the validated public address (or an equivalent rebinding-safe connector), and
the hostname is revalidated immediately before each hop. Reject URLs without
a valid hostname. Preserve the existing byte and timeout limits.

## Scheduler Ownership And Delivery

Acquire SQLite write ownership with `BEGIN IMMEDIATE` before selecting due
tasks. In that same transaction, transition expired `active_run_id` rows to
`interrupted` and clear their task ownership before selecting candidates.
Claim only tasks with `active_run_id IS NULL`, inserts the run, and updates the
task in one transaction with `WHERE enabled = 1 AND active_run_id IS NULL AND
next_run_at <= ?` plus an affected-row check. Task updates use compare-and-swap
predicates rather than an unconditional `WHERE id = ?` update.

Require `lease_seconds >= 3`. While a run is active, renew its lease every
`lease_seconds / 3` seconds, which is strictly shorter than the lease. Renewal
succeeds only when `task_id` and `active_run_id` match and the current
`lease_until >= renewal_time`; an expired worker cannot regain ownership. The
worker records the affected-row count. Recovery and completion also use that
run id as a fencing token; completion only transitions a currently-running
row. Immediately before external delivery, perform the same unexpired-lease
ownership check. If renewal or the pre-delivery check returns zero rows or
raises a database error, the worker cancels the task, skips delivery, and
records the run as `interrupted` when it still owns the row. A stale worker may
never call an external delivery after losing the lease.

Feishu send methods must raise when the API returns an explicit non-success
response so the existing delivery retry loop executes. A delivery is
successful only for `stored` or `delivered`; `skipped` is terminal but not a
success for a non-empty scheduled payload. If delivery still returns
`status="failed"`, the scheduler records the run as `failed`, stores the final
exception text in `error`, preserves the generated output path in
`output_path`, and does not update `last_success_at`. The run/task update is
performed under the same ownership predicate as completion.

## Context And Transport Completion

Context compaction receives an explicit `input_token_budget` and uses the
existing estimator over text, list text/content blocks, and tool-use input JSON.
The postcondition is `estimate_tokens(messages) < budget` (strictly below the
provider context window minus reserved output/system/tool capacity). It drops
the oldest complete user/assistant turns first; an assistant tool-call message
and its following tool-result message(s) are one indivisible turn. The newest
user request is retained. Callers receive a typed `ContextLimitError` when the
newest request alone cannot fit and return an `AgentResult` with that error
before making any provider call. No caller may proceed after a failed
postcondition check.

Anthropic `stop_reason="max_tokens"` maps to the same transport-level
completion error as OpenAI `finish_reason="length"`, allowing the existing
bounded continuation path to run.

## Calendar Semantics

Advance weekly schedules by seven calendar days in the configured `ZoneInfo`
timezone, then convert back to UTC. This preserves the configured local time
across daylight-saving transitions. For ambiguous/nonexistent local times,
use the same `ZoneInfo` fold/normalization behavior as the existing daily
trigger implementation.

## Testing

Every fix starts with a focused regression test that fails on the current
implementation:

- shell whitespace/version/flag variants, destructive options, user-only
  confirmation, and cross-session isolation;
- explicit plugin traversal and absolute names;
- loopback/private destinations and redirects;
- concurrent SQLite claims, lease renewal, stale-worker completion, and
  delivery failure persistence;
- Feishu API non-success retry behavior;
- sub-`keep_last_messages` over-budget context and unfit newest requests;
- Anthropic truncation continuation;
- weekly DST transitions.

Run the affected test modules after each red-green cycle, then run the complete
test suite. The real MCP smoke test remains environment-gated.

## Compatibility

Existing low-risk shell calls, plugin installs with valid names, public web
fetches, scheduler database files, provider configuration, and non-DST
schedules retain their current interfaces. The intentional compatibility
change is removal of model-redeemable shell confirmation; callers must use the
user command boundary instead.
