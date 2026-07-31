# First-Principles Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the eight confirmed security, correctness, and reliability defects while preserving safe existing interfaces and making every boundary enforce an explicit postcondition.

**Architecture:** Side-effecting operations gain canonical boundary objects: scoped shell approvals, canonical plugin paths, pinned public network endpoints, and fenced scheduler leases. Model calls gain an explicit input-token budget checked immediately before transport invocation, while transport and calendar fixes reuse the existing continuation and `ZoneInfo` behavior.

**Tech Stack:** Python 3.11+, asyncio, SQLite, `shlex`, `socket`/`ssl`/`http.client`, `ZoneInfo`, pytest, Typer.

---

## Invariants To Preserve

- A model-produced value is never accepted as proof of user approval.
- A path or network address is validated after canonicalization and immediately before the side effect.
- A scheduler worker may renew, deliver, interrupt, or complete only while `active_run_id` still identifies its run and its lease is unexpired.
- `succeeded` means execution and required delivery both succeeded.
- No provider call occurs unless estimated input is strictly below its computed input budget.
- Recurring calendar schedules preserve configured local wall-clock time.

## File Structure

- Modify `agent/security/shell.py`: token-based command classification and scoped, expiring confirmation state.
- Create `agent/security/network.py`: public-address validation, DNS consistency checks, pinned HTTP(S) connections, and manual redirects.
- Modify `agent/tools/builtin_tools.py`: remove model-supplied confirmation, validate plugin slugs, and delegate `web_fetch` to the safe network boundary.
- Modify `agent/commands/builtin.py`: register the transport-neutral `/confirm <token>` command.
- Modify `agent/runtime/contracts.py`: publish `user_id` into the active agent context used by shell authorization.
- Modify `agent/scheduler/store.py`: immediate claim transaction, recovery, renewal, ownership checks, and fenced completion.
- Modify `agent/scheduler/runtime.py`: lease heartbeat, cancellation on ownership loss, pre-delivery fencing, and truthful terminal status.
- Modify `agent/scheduler/delivery.py`: preserve delivery errors separately from output paths.
- Modify `agent/scheduler/models.py`: advance weekly triggers in local calendar time.
- Modify `agent/cli.py`: reject scheduler leases below three seconds at the CLI boundary.
- Modify `channels/feishu.py`: raise after an explicit Feishu create/send failure.
- Modify `agent/memory/system.py`: typed context-limit failure and budget-driven, turn-safe compaction.
- Modify `agent/core/agent.py`: calculate provider input budgets and enforce them immediately before every model call.
- Modify `agent/core/transport.py`: map Anthropic `max_tokens` to the existing truncation continuation path.
- Modify `tests/test_shell_security.py`, `tests/test_builtin_tools.py`, `tests/test_commands.py`, and `tests/test_agent_integration.py`: shell authorization regressions and runtime identity propagation.
- Modify `tests/test_plugin_catalog.py` and `tests/test_builtin_tools.py`: plugin and web-fetch boundary regressions.
- Create `tests/test_network_security.py`: isolated DNS, address, redirect, and connection-pinning regressions.
- Modify `tests/test_scheduler.py` and `tests/test_feishu_channel.py`: claim, lease, fencing, delivery, Feishu, and DST regressions.
- Modify `tests/test_consolidation.py` and `tests/test_agent_integration.py`: context-budget and Anthropic continuation regressions.

## Task 1: Parse Shell Risk From Tokens

**Files:**
- Modify: `agent/security/shell.py:78-518`
- Modify: `tests/test_shell_security.py`

- [ ] **Step 1: Add failing classifier regressions**

Add parameterized tests proving all of these are medium-risk inline execution: `python3.11 -c 'print(1)'`, `python3   -I   -c 'print(1)'`, `/usr/bin/python3.12 -B -c 'print(1)'`, `ruby --disable-gems -e 'puts 1'`, and `bash --noprofile -c 'echo ok'`. Add tests proving `find . -delete` is high risk and malformed quoting such as `python3 -c 'unterminated` fails closed as high risk without a confirmation token.

```python
@pytest.mark.parametrize("command", [
    "python3.11 -c 'print(1)'",
    "python3   -I   -c 'print(1)'",
    "/usr/bin/python3.12 -B -c 'print(1)'",
    "ruby --disable-gems -e 'puts 1'",
    "bash --noprofile -c 'echo ok'",
])
def test_shell_classifies_inline_interpreter_variants(command):
    result = shell_command_check(command)
    assert result.risk_level == "medium"
    assert result.requires_confirmation is True

def test_shell_blocks_destructive_find_option():
    result = shell_command_check("find . -delete")
    assert result.risk_level == "high"
    assert result.requires_confirmation is False

def test_shell_parse_failure_is_high_risk():
    result = shell_command_check("python3 -c 'unterminated")
    assert result.risk_level == "high"
    assert result.requires_confirmation is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/test_shell_security.py -k 'interpreter_variants or destructive_find or parse_failure'`

Expected: FAIL because versioned/flagged interpreters bypass literal patterns, `find -delete` is allowed, and malformed quoting falls back to whitespace splitting.

- [ ] **Step 3: Replace substring rules with parsed argv classification**

Introduce `_parse_command_tokens(command) -> list[str]` that uses `shlex.split` and raises a private `ShellParseError` on `ValueError`; never fall back to `str.split`. Normalize interpreter basenames with a full match such as `python(?:\d+(?:\.\d+)*)?`, then scan argv for the interpreter's execution option (`-c` for Python/shells, `-e` for Perl/Ruby) even when other flags precede it. Add a command-option table containing at least `("find", "-delete")` as non-confirmable high risk.

```python
_INLINE_INTERPRETERS = (
    (re.compile(r"python(?:\d+(?:\.\d+)*)?\Z"), frozenset({"-c"})),
    (re.compile(r"(?:bash|sh|zsh|fish)\Z"), frozenset({"-c"})),
    (re.compile(r"(?:perl|ruby)\Z"), frozenset({"-e"})),
)
_HIGH_RISK_OPTIONS = {"find": frozenset({"-delete"})}

def _inline_execution(tokens: list[str], command_name: str) -> bool:
    for pattern, execution_flags in _INLINE_INTERPRETERS:
        if pattern.fullmatch(command_name):
            return any(token in execution_flags for token in tokens[1:])
    return False
```

Perform parse failure handling before allowlist or risk checks so malformed input cannot be approved accidentally. Keep existing high-risk commands, shell-operator blocking, wrapper resolution, allowed-root handling, and compatibility exports.

- [ ] **Step 4: Run the shell security module and verify GREEN**

Run: `uv run pytest -q tests/test_shell_security.py`

Expected: PASS.

- [ ] **Step 5: Commit the classifier fix**

```bash
git add agent/security/shell.py tests/test_shell_security.py
git commit -m "fix: classify shell risk from parsed tokens"
```

## Task 2: Make Shell Confirmation User-Scoped

**Files:**
- Modify: `agent/security/shell.py:126-182`
- Modify: `agent/tools/builtin_tools.py:128-157,1279-1368`
- Modify: `agent/commands/builtin.py:778-901`
- Modify: `agent/runtime/contracts.py:485-508`
- Modify: `tests/test_shell_security.py`
- Modify: `tests/test_builtin_tools.py:522-635`
- Modify: `tests/test_commands.py:910-1002`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Add failing scope, expiry, and one-time-use tests**

Define tests around an explicit immutable scope and an injectable `now` value. Cover matching approval, wrong session, wrong channel, wrong user, expired token, second redemption, exact normalized command matching, and allowlist expiry.

```python
scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
check = shell_command_check("mv a b", scope=scope, now=t0)
assert shell_command_confirm(check.confirmation_token, scope=scope, now=t0) is True
assert shell_command_confirm(check.confirmation_token, scope=scope, now=t0) is False
assert shell_command_check("mv a b", scope=scope, now=t0).allowed is True
assert shell_command_check("mv a b", scope=scope, now=t0 + timedelta(minutes=6)).allowed is False
```

Add command tests proving `/confirm <token>` redeems using `CommandContext.session_id`, `channel_name`, and `metadata["user_id"]`, accepts no command argument, and returns a neutral invalid/expired message without revealing another scope's details.

Add a tool-schema test proving `confirmation_token` is absent, and replace tests that pass a token to `_shell` with a regression proving that unexpected model input is rejected by the registry call signature.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/test_shell_security.py tests/test_builtin_tools.py tests/test_commands.py tests/test_agent_integration.py -k 'shell and (confirm or scope or expiry or user_id or schema)'`

Expected: FAIL because confirmation state is global, tokens store only a command, `/confirm` does not exist, and the tool accepts token input.

- [ ] **Step 3: Implement scoped confirmation records**

Use monotonic concepts expressed as timezone-aware UTC datetimes so tests can inject time. Normalize only outer whitespace; approval equality remains byte-for-byte on the normalized string.

```python
@dataclass(frozen=True)
class ShellAuthorizationScope:
    session_id: str
    channel_name: str
    user_id: str = ""

@dataclass(frozen=True)
class PendingShellConfirmation:
    command: str
    scope: ShellAuthorizationScope
    expires_at: datetime

_pending_tokens: dict[str, PendingShellConfirmation] = {}
_session_allowlist: dict[tuple[ShellAuthorizationScope, str], datetime] = {}
CONFIRMATION_TTL = timedelta(minutes=5)
```

Change `shell_command_check(..., scope, now)` to create scoped records and to consult only an unexpired `(scope, normalized_command)` approval. Change `shell_command_confirm(token, *, scope, now)` to pop first, then validate scope and expiry; no command argument is accepted. `shell_session_allowlist_clear()` must clear both maps for test/session cleanup.

- [ ] **Step 4: Remove model redemption and add `/confirm`**

Remove `confirmation_token` from the shell JSON schema, `_shell` signature, redemption branch, and recovery hint. In `_shell`, derive scope from `agent.core.agent._active_agent_context.get()`; use `session_id`, `channel_name`, and `user_id`, falling back to `("default", "cli", "")` only when no active context exists in direct tests.

Register a core descriptor with `usage="/confirm <token>"`, scope `all`, and `concurrency="anytime"`. Its handler must require exactly one token and call:

```python
scope = ShellAuthorizationScope(
    context.session_id,
    context.channel_name,
    str(context.metadata.get("user_id") or ""),
)
approved = shell_command_confirm(request.args, scope=scope)
```

Return `CommandResult(response_text="Confirmation accepted. Retry the requested operation.")` on success and a single invalid/expired/scope-mismatch response on failure.

Update `_publish_turn_runtime_metadata` to set or clear `ctx.metadata["user_id"]` from each `TurnInput`, preventing one user's identity from leaking into a later turn on the same runtime state.

- [ ] **Step 5: Run affected shell and command tests**

Run: `uv run pytest -q tests/test_shell_security.py tests/test_builtin_tools.py tests/test_commands.py tests/test_agent_integration.py -k 'shell or confirm or runtime_metadata'`

Expected: PASS.

- [ ] **Step 6: Commit the authorization boundary**

```bash
git add agent/security/shell.py agent/tools/builtin_tools.py agent/commands/builtin.py agent/runtime/contracts.py tests/test_shell_security.py tests/test_builtin_tools.py tests/test_commands.py tests/test_agent_integration.py
git commit -m "fix: require user-scoped shell confirmation"
```

## Task 3: Canonicalize Plugin Targets

**Files:**
- Modify: `agent/tools/builtin_tools.py:688-837`
- Modify: `tests/test_plugin_catalog.py:2258-2305`

- [ ] **Step 1: Add failing traversal and absolute-name tests**

Parameterize explicit install and uninstall names with `../escape`, `a/b`, `a\\b`, `/tmp/escape`, `.`, `..`, `""`, and whitespace-padded names. Patch clone/copy/rmtree and assert none is invoked. Add positive coverage for `alpha`, `alpha-2`, and `alpha_beta`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/test_plugin_catalog.py -k 'plugin_name or traversal or absolute'`

Expected: FAIL because explicit install names are joined without validation.

- [ ] **Step 3: Add one canonical target helper used by install and uninstall**

```python
_PLUGIN_SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?\Z")

def _resolve_user_plugin_target(name: str) -> tuple[str, Path]:
    if name != name.strip() or not _PLUGIN_SLUG.fullmatch(name):
        raise ValueError("plugin name must be a canonical slug")
    root = shared.USER_PLUGINS_DIR.expanduser().resolve(strict=False)
    target = (root / name).resolve(strict=False)
    if target.parent != root:
        raise ValueError("plugin target must be a direct child of USER_PLUGINS_DIR")
    return name, target
```

Derived names may continue replacing invalid source characters with `-`, but must pass through the same helper. Validate before `mkdir`, clone, copy, existence checks, or removal. Convert `ValueError` to the existing `{ok: False, error: ...}` tool result.

- [ ] **Step 4: Run plugin tests and verify GREEN**

Run: `uv run pytest -q tests/test_plugin_catalog.py tests/test_builtin_tools.py -k 'plugin'`

Expected: PASS.

- [ ] **Step 5: Commit the plugin boundary**

```bash
git add agent/tools/builtin_tools.py tests/test_plugin_catalog.py
git commit -m "fix: constrain plugin targets to canonical slugs"
```

## Task 4: Pin `web_fetch` To Public Endpoints

**Files:**
- Create: `agent/security/network.py`
- Modify: `agent/tools/builtin_tools.py:895-1042`
- Create: `tests/test_network_security.py`
- Modify: `tests/test_builtin_tools.py:778-849`

- [ ] **Step 1: Add failing address-validation tests**

Test literal and resolved loopback, private, link-local, multicast, reserved, unspecified, and IPv4-mapped IPv6 addresses. Test that one unsafe address in a mixed DNS answer rejects the entire host. Test missing hostnames and non-HTTP(S) schemes.

```python
@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "::", "::ffff:127.0.0.1",
])
def test_resolve_public_endpoint_rejects_non_public_literals(host):
    with pytest.raises(UnsafeNetworkTarget):
        resolve_public_endpoint(f"http://[{host}]/" if ":" in host else f"http://{host}/")
```

- [ ] **Step 2: Add failing redirect, rebinding, and pinning tests**

Use injected resolver and connection factory fakes. Cover a public URL redirecting to loopback, more than five redirects, DNS results changing between validation and pre-connect resolution, relative `Location`, and a successful request whose connection factory receives the already validated public IP while the HTTP `Host` header and HTTPS SNI hostname remain the original hostname.

- [ ] **Step 3: Run network tests and verify RED**

Run: `uv run pytest -q tests/test_network_security.py tests/test_builtin_tools.py -k 'web_fetch or public_endpoint or redirect or rebinding'`

Expected: FAIL because no network security module exists and `urlopen` follows redirects/connects after an uncontrolled DNS lookup.

- [ ] **Step 4: Implement the isolated network boundary**

In `agent/security/network.py`, add `UnsafeNetworkTarget`, `ResolvedEndpoint`, and `FetchResponse`. Parse with `urllib.parse.urlsplit`; require `http` or `https`, a hostname, and a valid port. Resolve with `socket.getaddrinfo(host, port, type=SOCK_STREAM)`, canonicalize through `ipaddress.ip_address`, unwrap `ipv4_mapped`, and reject when any address is not globally routable by the explicit loopback/private/link-local/multicast/reserved/unspecified checks.

Resolve twice per hop and require identical canonical address sets. Open `http.client.HTTPConnection` to one validated IP. For HTTPS, use a small connection subclass that connects to the validated IP but wraps the socket with `ssl.create_default_context().wrap_socket(..., server_hostname=original_hostname)` so certificate validation and SNI use the hostname. Send the original hostname in `Host` (including a non-default port).

Implement redirects without an automatic redirect handler:

```python
for hop in range(max_redirects + 1):
    first = resolve_public_endpoint(current_url, resolver=resolver)
    second = resolve_public_endpoint(current_url, resolver=resolver)
    if first.addresses != second.addresses:
        raise UnsafeNetworkTarget("DNS answers changed before connection")
    response = request_pinned(second, connection_factory=connection_factory)
    if response.status not in {301, 302, 303, 307, 308}:
        return read_bounded(response, max_bytes=max_bytes, on_progress=on_progress)
    if hop == max_redirects:
        raise UnsafeNetworkTarget("too many redirects")
    current_url = urllib.parse.urljoin(current_url, require_location(response))
```

Always close response/connection, retain existing timeout and byte limits, and report progress from bounded reads.

- [ ] **Step 5: Wire `BuiltinTools` to the safe helper**

Keep `_make_urllib_request(url, timeout)` as the thread-call seam used by existing tests, but make it call `fetch_public_http_url(...).body`. Do not use `urllib.request.urlopen` for `web_fetch`; Tavily's fixed configured API endpoint remains unchanged. Return the final URL in the tool payload if available without changing existing `content`, `truncated`, and `chars` fields.

- [ ] **Step 6: Run network and built-in tool tests**

Run: `uv run pytest -q tests/test_network_security.py tests/test_builtin_tools.py -k 'web_fetch or public_endpoint or redirect or rebinding'`

Expected: PASS.

- [ ] **Step 7: Commit the network boundary**

```bash
git add agent/security/network.py agent/tools/builtin_tools.py tests/test_network_security.py tests/test_builtin_tools.py
git commit -m "fix: pin web fetches to validated public endpoints"
```

## Task 5: Fence Scheduler Store Mutations

**Files:**
- Modify: `agent/scheduler/store.py:292-459`
- Modify: `tests/test_scheduler.py:73-150`

- [ ] **Step 1: Add failing claim and recovery concurrency tests**

Open two `SchedulerStore` instances on the same database. Use two threads and a barrier to call `claim_due_tasks` simultaneously; assert exactly one run and one claim. Add a stale-active-run fixture and assert `claim_due_tasks` recovers it to `interrupted`, clears ownership, and claims the requeued occurrence in the same call without overwriting the old run.

- [ ] **Step 2: Add failing fencing API tests**

Specify these store contracts in tests:

```python
assert store.renew_lease(task.id, run.id, now=t1, lease_seconds=30) is True
assert store.renew_lease(task.id, "stale-run", now=t1, lease_seconds=30) is False
assert store.renew_lease(task.id, run.id, now=after_expiry, lease_seconds=30) is False
assert store.owns_unexpired_lease(task.id, run.id, now=t1) is True
assert store.complete_run(task.id, "stale-run", finished_at=t1, status="failed") is False
```

Also prove stale recovery and stale completion cannot clear a newer `active_run_id` or mutate the newer run.

- [ ] **Step 3: Run store regressions and verify RED**

Run: `uv run pytest -q tests/test_scheduler.py -k 'concurrent_claim or claim_recovers or renew_lease or fenced or stale_worker'`

Expected: FAIL because selection precedes the write transaction and no renewal/CAS API exists.

- [ ] **Step 4: Move recovery and claim under `BEGIN IMMEDIATE`**

Add a private transaction context that executes `BEGIN IMMEDIATE`, commits on success, and rolls back on exception. Reject `lease_seconds < 3` with `ValueError` before beginning.

Inside the transaction: recover expired rows using `task_id + active_run_id` predicates; mark only still-running old runs interrupted; restore `next_run_at` from `scheduled_for`; then select only `enabled = 1`, due, `active_run_id IS NULL` tasks. Insert each run and update its task with:

```sql
UPDATE scheduled_tasks
SET next_run_at = ?, lease_until = ?, active_run_id = ?, updated_at = ?
WHERE id = ? AND enabled = 1 AND active_run_id IS NULL AND next_run_at <= ?
```

Append a claim only when `cursor.rowcount == 1`; otherwise remove/ignore the provisional run inside the same transaction. Avoid calling helpers that implicitly start another transaction while the immediate transaction is active.

- [ ] **Step 5: Implement lease and completion CAS methods**

`renew_lease(task_id, run_id, now, lease_seconds) -> bool` updates only when `active_run_id = run_id`, `lease_until IS NOT NULL`, and `lease_until >= now`. `owns_unexpired_lease(...)` requires the same ownership plus `lease_until >= now` and a running run row. `complete_run(...) -> bool` updates the currently running run and clears the task only under `WHERE id = task_id AND active_run_id = run_id`; update `last_success_at` only for `succeeded`. `recover_stale_runs` must reuse the fenced private recovery helper in its own immediate transaction.

- [ ] **Step 6: Run scheduler store tests**

Run: `uv run pytest -q tests/test_scheduler.py -k 'store or claim or recover or renew or fenced or stale'`

Expected: PASS.

- [ ] **Step 7: Commit scheduler store fencing**

```bash
git add agent/scheduler/store.py tests/test_scheduler.py
git commit -m "fix: fence scheduler claims and state transitions"
```

## Task 6: Renew Leases And Persist Delivery Failures

**Files:**
- Modify: `agent/scheduler/models.py:343-346`
- Modify: `agent/scheduler/runtime.py:14-106`
- Modify: `agent/scheduler/delivery.py:57-88`
- Modify: `agent/cli.py:879-901`
- Modify: `channels/feishu.py:1488-1577`
- Modify: `tests/test_scheduler.py:151-587`
- Modify: `tests/test_feishu_channel.py:2440-2535`

- [ ] **Step 1: Add failing runtime lease tests**

Add tests that `SchedulerService(..., lease_seconds=2)` raises `ValueError`; a long executor renews near `lease_seconds / 3`; a zero-row or exception renewal cancels the executor; and failed renewal prevents the delivery fake from being called. Add a race test where ownership changes after execution but before delivery and assert the mandatory `owns_unexpired_lease` check skips delivery.

- [ ] **Step 2: Add failing delivery-state tests**

Change the expected result contract to:

```python
@dataclass
class DeliveryResult:
    status: str
    output_path: str = ""
    error: str = ""
```

Test exhausted delivery retries returning `DeliveryResult(status="failed", error="...")`; runtime records run `failed`, keeps the executor's `output_path`, writes the error, sets `delivery_status="failed"`, and does not change `last_success_at`. Test `skipped` with non-empty text as failed and empty standalone output as a terminal skip without a false delivery success.

- [ ] **Step 3: Add failing Feishu propagation test**

Make create return `success() == False` after reply fallback and assert `_do_send` raises `RuntimeError` containing code/message/log id. Add a scheduler delivery test with `max_retries=2` and patched sleep proving three Feishu attempts occur before failure is returned.

- [ ] **Step 4: Run focused runtime/delivery tests and verify RED**

Run: `uv run pytest -q tests/test_scheduler.py tests/test_feishu_channel.py -k 'lease or ownership or delivery_failure or skipped or non_success or retries'`

Expected: FAIL because there is no heartbeat/pre-delivery fence and Feishu errors are swallowed.

- [ ] **Step 5: Add heartbeat and ownership-loss control flow**

Start a renewal task when execution begins. It sleeps `lease_seconds / 3`, calls `renew_lease` with the current UTC time, and sets a lost-ownership event on `False` or database exception. Race execution against that event; cancel and await the executor when ownership is lost. Keep renewal active through the pre-delivery check, external delivery, and fenced terminal completion; only after `complete_run` returns (successfully or with a lost-fence result) may the outer `finally` cancel and await the renewal task.

Immediately before `_deliver`, call `owns_unexpired_lease`. If false or it raises, do not call delivery and call a fenced `interrupt_run`/`complete_run(..., status="interrupted")`; a stale result may legitimately return `False` when recovery already changed ownership.

- [ ] **Step 6: Make terminal status reflect delivery**

Treat only `stored` and `delivered` as successful delivery statuses. Permit `skipped` only when `result.text_output` is empty. Convert `failed`, unexpected statuses, or non-empty `skipped` to a fenced failed completion while retaining `result.output_path`. Populate `DeliveryResult.error` in the retry loop instead of misusing `output_path`.

Set the Typer `--lease-seconds` minimum to `3`; keep config's existing stricter validation compatible.

- [ ] **Step 7: Raise Feishu send failures**

Keep reply failure as a fallback to create. For create non-success, build a `RuntimeError`, record it in latency logging, and raise. In the outer exception handler, log and re-raise so `FeishuOutputSink.drain()` and `SchedulerDelivery` see the failure.

- [ ] **Step 8: Run scheduler and Feishu modules**

Run: `uv run pytest -q tests/test_scheduler.py tests/test_feishu_channel.py`

Expected: PASS.

- [ ] **Step 9: Commit lease and delivery reliability**

```bash
git add agent/scheduler/models.py agent/scheduler/runtime.py agent/scheduler/delivery.py agent/cli.py channels/feishu.py tests/test_scheduler.py tests/test_feishu_channel.py
git commit -m "fix: renew scheduler leases and surface delivery failure"
```

## Task 7: Enforce A Provider Input Budget

**Files:**
- Modify: `agent/memory/system.py:2165-2239,2911-2934`
- Modify: `agent/core/agent.py:1067-1075,2037-2523,2607-2623`
- Modify: `tests/test_consolidation.py:512-581`
- Modify: `tests/test_agent_integration.py:4104-4172,7313-7410`

- [ ] **Step 1: Add failing compaction postcondition tests**

Add tests where fewer than `min_messages` exceed the budget, where `keep_last_messages` itself exceeds the budget, and where the newest request alone cannot fit. Include Anthropic tool-use plus tool-result blocks and OpenAI assistant `tool_calls` plus `role="tool"` messages; assert no retained history contains an orphan tool result or a tool call without its results.

```python
compacted = manager.compact_messages(messages, input_token_budget=40)
assert engine.estimate_tokens(compacted) < 40
assert compacted[-1]["content"] == newest_request

with pytest.raises(ContextLimitError):
    manager.compact_messages(
        [{"role": "user", "content": "x" * 1000}],
        input_token_budget=10,
    )
```

- [ ] **Step 2: Add failing pre-provider tests**

Construct `BaseAgent(context_window=100, max_tokens=20)` with a fake context manager and transport. Prove system prompt and serialized tool schemas reduce the message budget, compaction happens even with two messages, the final estimate is strictly below budget, and an impossible newest request returns `AgentResult.error` containing `ContextLimitError` before fake transport `create` or `stream` is called.

- [ ] **Step 3: Run context tests and verify RED**

Run: `uv run pytest -q tests/test_consolidation.py tests/test_agent_integration.py -k 'input_budget or context_limit or tool_turn or over_budget'`

Expected: FAIL because compaction accepts no budget, honors count rather than size, and does not verify its result.

- [ ] **Step 4: Implement budget-driven complete-turn compaction**

Add `ContextLimitError(RuntimeError)`. Change `should_compact_messages(messages, input_token_budget)` to compare the estimator directly with the budget; `min_messages` remains relevant only to background consolidation, not provider safety.

Partition history into complete chronological units. A plain user message plus its plain assistant reply is a normal turn. An assistant message containing Anthropic `tool_use` blocks or OpenAI `tool_calls`, together with every immediately following Anthropic `tool_result` or OpenAI `role="tool"` message that answers those ids, is one indivisible tool-bearing turn. A `role="user"` message composed exclusively of `tool_result` blocks is never treated as a new user request. Drop the oldest complete units until `estimate_tokens(candidate) < input_token_budget`; never retain an orphan tool result or tool call. Protect the newest real user-request message itself, but allow older plain replies and complete tool-bearing turns after that request to be dropped if necessary. Raise `ContextLimitError` only when the budget is non-positive, tool history is structurally incomplete, or the newest user request alone is still at/over budget. Recheck the estimator after construction before returning.

- [ ] **Step 5: Centralize the immediate pre-call check**

Add a `BaseAgent._input_token_budget(ctx, tools)` helper:

```python
overhead = self.context_manager.consolidation.estimate_tokens([
    {"role": "system", "content": ctx.system_prompt},
    {"role": "system", "content": json.dumps(tools, ensure_ascii=False, sort_keys=True)},
])
return self.context_window - self.max_tokens - overhead
```

Add `_prepare_provider_context(ctx, tools)` that invokes `compact_messages` when needed and always verifies the strict postcondition. Call it inside `_create` and `_stream_response` immediately before delegating to transport; this also covers truncation continuations and tool-loop iterations. Remove the earlier best-effort pre-loop checks so there is one authoritative boundary. Add an explicit `ContextLimitError` branch in `_format_agent_error` that returns `ContextLimitError: <reason>`, then let the existing agent error path return an `AgentResult` without provider invocation. Update post-turn maintenance and test doubles to pass the explicit budget, but do not allow post-turn maintenance to substitute for the pre-call check.

- [ ] **Step 6: Run context and integration modules**

Run: `uv run pytest -q tests/test_consolidation.py tests/test_agent_integration.py -k 'compact or input_budget or context_limit or tool_turn or interactive_loop_compaction'`

Expected: PASS.

- [ ] **Step 7: Commit context enforcement**

```bash
git add agent/memory/system.py agent/core/agent.py tests/test_consolidation.py tests/test_agent_integration.py
git commit -m "fix: enforce provider input token budgets"
```

## Task 8: Continue Anthropic Truncation

**Files:**
- Modify: `agent/core/transport.py:197-209`
- Modify: `tests/test_agent_integration.py:4124-4172,4592-4635`

- [ ] **Step 1: Add a failing Anthropic continuation regression**

Create minimal Anthropic response fakes with `stop_reason="max_tokens"` then `stop_reason="end_turn"`, text content blocks, and no tool uses. Assert `send_message` makes two provider calls, appends the existing continuation prompt, merges both text fragments, and returns no error. Add a direct transport assertion for the truncation error string.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'anthropic and (max_tokens or auto_continue)'`

Expected: FAIL because `AnthropicTransport.completion_error` always returns `None`.

- [ ] **Step 3: Map Anthropic's stop reason to the existing path**

```python
def completion_error(self, response):
    if getattr(response, "stop_reason", None) == "max_tokens":
        return "Model response was truncated (stop_reason=max_tokens)"
    return None
```

Do not add a second continuation mechanism; `_handle_end_turn` and `_continue_truncated_response` already bound attempts and merge overlap.

- [ ] **Step 4: Run truncation tests**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'truncat or auto_continue or max_tokens'`

Expected: PASS.

- [ ] **Step 5: Commit transport completion handling**

```bash
git add agent/core/transport.py tests/test_agent_integration.py
git commit -m "fix: continue truncated Anthropic responses"
```

## Task 9: Preserve Weekly Wall-Clock Time Across DST

**Files:**
- Modify: `agent/scheduler/models.py:121-148`
- Modify: `tests/test_scheduler.py:11-35`

- [ ] **Step 1: Add failing spring and fall DST tests**

Use `America/New_York` and a non-transition local time such as Sunday 09:00. Assert advancing from the week before spring-forward changes the UTC hour while retaining 09:00 local, and likewise for fall-back.

```python
trigger = WeeklyTrigger("sun", "09:00", "America/New_York")
next_run = trigger.advance_from(
    datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc),
    datetime(2026, 3, 1, 14, 1, tzinfo=timezone.utc),
)
assert next_run == datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc)
assert next_run.astimezone(ZoneInfo("America/New_York")).hour == 9
```

- [ ] **Step 2: Run DST tests and verify RED**

Run: `uv run pytest -q tests/test_scheduler.py -k 'weekly and dst'`

Expected: FAIL because `advance_from` adds seven days in UTC.

- [ ] **Step 3: Advance in local calendar time**

Mirror `DailyTrigger.advance_from`: convert `scheduled_for` to the configured `ZoneInfo`, add seven days there, convert to UTC, and repeat in local time until the candidate is after `now`. This deliberately inherits the existing `ZoneInfo` fold/nonexistent-time normalization behavior.

- [ ] **Step 4: Run trigger and scheduler tests**

Run: `uv run pytest -q tests/test_scheduler.py -k 'trigger or weekly or daily'`

Expected: PASS.

- [ ] **Step 5: Commit calendar semantics**

```bash
git add agent/scheduler/models.py tests/test_scheduler.py
git commit -m "fix: advance weekly schedules in local time"
```

## Task 10: Full Verification And Documentation Consistency

**Files:**
- Verify: `docs/superpowers/specs/2026-07-31-first-principles-review-fixes-design.md`
- Verify: all files changed by Tasks 1-9

- [ ] **Step 1: Run formatting/static repository checks already configured by the project**

Run: `git diff --check`

Expected: no output, exit 0.

- [ ] **Step 2: Run every directly affected test module together**

Run: `uv run pytest -q tests/test_shell_security.py tests/test_builtin_tools.py tests/test_commands.py tests/test_plugin_catalog.py tests/test_network_security.py tests/test_scheduler.py tests/test_feishu_channel.py tests/test_consolidation.py tests/test_agent_integration.py`

Expected: PASS with no unexpected skips or new warnings.

- [ ] **Step 3: Run the complete suite**

Run: `uv run pytest -q`

Expected baseline or better: at least `946 passed, 1 skipped`; all new regressions pass. The real MCP smoke test may remain environment-gated.

- [ ] **Step 4: Review the final diff against every design invariant**

Run: `git diff c9acd1b --stat && git diff c9acd1b -- docs/superpowers/specs/2026-07-31-first-principles-review-fixes-design.md`

Expected: implementation/test changes only; the approved spec remains unchanged unless a documented correction was required.

- [ ] **Step 5: Request two-stage code review**

Use `superpowers:requesting-code-review`: first verify spec compliance, then review code quality, concurrency races, security boundary bypasses, and missing tests. Resolve blocking findings and rerun the affected modules plus the full suite.

- [ ] **Step 6: Record final verification**

```bash
git status --short
git log --oneline c9acd1b..HEAD
```

Expected: only intentional tracked changes, with one focused commit per task and no uncommitted implementation artifacts.
