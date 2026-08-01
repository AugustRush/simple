# File Tools Safety Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the built-in file tools with explicit-root, snapshot-based, atomic text operations and enforce the same immutable workspace policy across sub-Agents, shell execution, and context compaction.

**Architecture:** Add a focused `agent/tools/files.py` service containing immutable policy, stable snapshots, bounded directory enumeration, and atomic mutations. `BuiltinTools` becomes a schema adapter; `ToolRegistry` gains call-aware authorization for root-dependent writes; a platform adapter in `agent/security/filesystem_sandbox.py` enforces the policy for shell descendants. Bootstrap owns policy construction and passes one immutable instance through the runtime.

**Tech Stack:** Python 3.11+, standard library (`dataclasses`, `hashlib`, `os`, `stat`, `tempfile`, `fcntl`/`msvcrt`, `base64`, `json`, `subprocess`), pytest, macOS `sandbox-exec` with fail-closed unsupported-platform behavior.

---

## File Structure

- Create `agent/tools/files.py`: configuration values, error envelopes, root/path authorization, stable snapshots, list cursors, cross-process locks, and atomic create/overwrite/edit.
- Create `agent/security/filesystem_sandbox.py`: immutable shell sandbox request, macOS profile generation, command wrapping, capability detection, and fail-closed errors.
- Create `tests/test_file_service.py`: focused unit and concurrency tests for the new service.
- Create `tests/test_filesystem_sandbox.py`: profile-generation and real-process shell isolation tests.
- Modify `agent/config.py`: default `file_access` values and strict validation/resolution.
- Modify `agent/bootstrap.py`: construct and wire one immutable policy; validate disjoint roots before tool registration.
- Modify `agent/tools/builtin_tools.py`: replace public file schemas/handlers, inject the service, and wrap shell commands with the sandbox adapter.
- Modify `agent/tools/runtime.py`: support input-aware tool authorization and root-dependent mutation capability.
- Modify `agent/tools/executor.py`: preserve structured authorization failures at the normal pre-execution boundary.
- Modify `agent/core/agent.py`: retain output mutation tools for every sub-Agent profile while narrowing workspace access.
- Modify `agent/security/content_filter.py`: compact structured file results without losing revision/range metadata.
- Modify `agent/config.py` prompt composition and `README.md`: document explicit roots, revisions, fixed output access, and restart-only workspace policy.
- Modify existing tests in `tests/test_builtin_tools.py`, `tests/test_agent_integration.py`, `tests/test_content_filter.py`, and `tests/test_output_dir.py` to use the breaking schemas and new policy wiring.

## Task 1: Strict Startup Configuration and Immutable Policy

**Files:**
- Create: `agent/tools/files.py`
- Modify: `agent/config.py`
- Modify: `agent/bootstrap.py`
- Test: `tests/test_file_service.py`
- Test: `tests/test_output_dir.py`

- [ ] **Step 1: Write failing tests for defaults and validation**

Add tests proving the default policy is workspace read/deny-write, `output_dir` is always read/write, all numeric limits are positive and bounded, unknown `file_access` keys fail, non-boolean permissions fail, and workspace/output overlap (equal, parent/child, or symlink alias) aborts bootstrap.

```python
def test_file_access_defaults_are_safe():
    cfg = resolve_file_access_config({}, workspace_root=workspace, output_dir=output)
    assert cfg.workspace_read is True
    assert cfg.workspace_write is False

@pytest.mark.parametrize("output", [workspace, workspace / "out", workspace.parent])
def test_policy_rejects_overlapping_roots(workspace, output):
    with pytest.raises(FilePolicyConfigError, match="disjoint"):
        FileAccessPolicy.from_config(DEFAULTS, workspace, output)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `uv run pytest tests/test_file_service.py tests/test_output_dir.py -q`

Expected: FAIL because `FileAccessPolicy`, `FileAccessLimits`, and strict config resolution do not exist.

- [ ] **Step 3: Implement immutable config and policy skeleton**

Create frozen dataclasses and typed failures in `agent/tools/files.py`:

```python
@dataclass(frozen=True)
class FileAccessLimits:
    max_read_lines: int = 400
    max_read_bytes: int = 64 * 1024
    max_snapshot_bytes: int = 16 * 1024 * 1024
    max_write_bytes: int = 4 * 1024 * 1024
    max_replacements: int = 100
    max_list_results: int = 1000

@dataclass(frozen=True)
class FileAccessPolicy:
    workspace_root: Path
    output_root: Path
    workspace_read: bool = True
    workspace_write: bool = False
    limits: FileAccessLimits = FileAccessLimits()
```

Add `file_access` to `DEFAULT_CONFIG`, reject invalid nested keys/types instead of silently normalizing them, resolve both roots once, and reject overlap before `BuiltinTools` is created. Store the immutable policy in components and registry context by object reference; never store a mutable dict as active authority.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_file_service.py tests/test_output_dir.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/config.py agent/bootstrap.py agent/tools/files.py tests/test_file_service.py tests/test_output_dir.py
git commit -m "feat: add immutable file access policy"
```

## Task 2: Race-Resistant Rooted Paths and Stable Snapshots

**Files:**
- Modify: `agent/tools/files.py`
- Test: `tests/test_file_service.py`

- [ ] **Step 1: Write failing path and snapshot tests**

Cover explicit `workspace`/`output_dir` roots, relative paths only, traversal, embedded `.`/NUL, exact `.` listing exception, symlink escape, regular-file enforcement, workspace read denial, UTF-8/BOM/invalid UTF-8/NUL handling, newline metadata, empty files, strict one-based ranges, complete-line byte limits, oversized first lines, stable SHA-256 revisions, and mutation-during-read conflict detection.

```python
def test_snapshot_strips_bom_but_hashes_exact_bytes(service):
    target.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta")
    result = service.read("workspace", "note.txt", start_line=1, line_count=1)
    assert result["content"] == "alpha\r\n"
    assert result["bom"] is True
    assert result["revision"] == "sha256:" + sha256(target.read_bytes()).hexdigest()
```

- [ ] **Step 2: Run snapshot tests and confirm RED**

Run: `uv run pytest tests/test_file_service.py -q -k 'path or snapshot or read'`

Expected: FAIL because secure resolution and snapshot reads are not implemented.

- [ ] **Step 3: Implement descriptor-oriented resolution and snapshot reads**

Add `AuthorizedPath`, `FileServiceError`, strict root parsing, component-by-component `os.open(..., dir_fd=..., O_NOFOLLOW)` where available, and post-open `fstat` checks. Scan files in chunks up to `max_snapshot_bytes`, compute SHA-256 over exact bytes, incrementally decode strict UTF-8 after consuming one BOM, calculate line ranges with terminators retained, and verify stable identity/size/mtime before returning.

Use `codecs.getincrementaldecoder("utf-8")(errors="strict")` so UTF-8 code points and CRLF pairs split across byte chunks are handled correctly. Maintain only the hash, byte/line counters, newline metadata state, decoder carry, and requested line-window buffer; discard decoded lines before and after the requested window. Never concatenate the full decoded file or retain all lines merely to compute `revision`/`total_lines`.

All expected failures use:

```python
{"ok": False, "error": {"code": code, "message": message,
                          "details": details, "retryable": retryable}}
```

- [ ] **Step 4: Run snapshot tests and confirm GREEN**

Run: `uv run pytest tests/test_file_service.py -q -k 'path or snapshot or read'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/files.py tests/test_file_service.py
git commit -m "feat: add versioned bounded file snapshots"
```

## Task 3: Bounded Deterministic Directory Enumeration

**Files:**
- Modify: `agent/tools/files.py`
- Test: `tests/test_file_service.py`

- [ ] **Step 1: Write failing list tests**

Test root `.` handling, missing/non-directory targets, basename-only patterns, deterministic lexical ordering, recursive/non-recursive behavior, bounded results, request-bound cursors, file/directory/symlink/other kinds, symlink non-traversal, cycles, workspace read denial, and concurrent-change best-effort semantics.

- [ ] **Step 2: Run list tests and confirm RED**

Run: `uv run pytest tests/test_file_service.py -q -k list`

Expected: FAIL because `FileService.list_files` and cursors do not exist.

- [ ] **Step 3: Implement bounded traversal and opaque cursors**

Use sorted `os.scandir` batches and a bounded traversal stack; never call `rglob()` over an unbounded tree. Encode cursor state as URL-safe base64 JSON containing a version, normalized request fingerprint, and last emitted relative path. Treat the cursor as continuation state, not authorization. Do not follow symlinks or expose their target metadata.

- [ ] **Step 4: Run list tests and confirm GREEN**

Run: `uv run pytest tests/test_file_service.py -q -k list`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/files.py tests/test_file_service.py
git commit -m "feat: add bounded rooted file listing"
```

## Task 4: Atomic Create, Overwrite, and Structured Edit

**Files:**
- Modify: `agent/tools/files.py`
- Test: `tests/test_file_service.py`

- [ ] **Step 1: Write failing mutation tests**

Cover output creation, safe parent creation, create conflicts, workspace global switch plus `write_scope` intersection, mandatory revisions, stale revisions, BOM/mode/newline preservation, strict UTF-8/NUL/leading-U+FEFF rejection, ordered non-overlapping replacements, exact counts, inserted-text behavior, aggregate/intermediate/final limits, rollback, temporary cleanup, and old/new summaries.

Add a create-mode permission test under a controlled process umask proving the published file mode is `0o666 & ~umask` and never gains executable bits; do not inherit the temporary helper's usual `0o600` mode.

Add same-process and multiprocessing tests proving only one cooperative writer can commit from a shared revision. Add target substitution tests that must fail closed and explicitly avoid claiming portable strict CAS against a non-cooperating final-interval writer.

- [ ] **Step 2: Run mutation tests and confirm RED**

Run: `uv run pytest tests/test_file_service.py -q -k 'create or overwrite or edit or concurrent or atomic'`

Expected: FAIL because mutation APIs and locks do not exist.

- [ ] **Step 3: Implement bounded atomic mutation**

Add per-path in-process locks and advisory locks under `output_dir/.simple-internal/locks/<sha256-key>.lock`. Acquire before the comparison snapshot and hold through commit. Validate all edit fragments and every intermediate string in memory. Count/replace with Python's non-overlapping `str.count`/`str.replace` semantics.

Write a unique temporary file in the authorized target directory, apply existing mode, flush/fsync, repeat descriptor identity and revision checks, then use descriptor-relative `os.replace`. Sync the directory where supported and clean every failed temporary path. Use exclusive descriptor-relative creation for `mode=create` so an existing target is never replaced.

For create, do not write directly to the public target. Fully write and fsync a unique same-directory temporary inode, then publish it with descriptor-relative `os.link(temp_name, target_name, follow_symlinks=False)`, which atomically fails when the target exists; unlink the temporary name only after successful publication and sync the directory. If atomic same-filesystem no-replace publication is unavailable, return `atomic_replace_failed` and leave the target absent. Add failure injection before fsync, before link, and after failed link to prove no partial public target remains.

Reserve `output_dir/.simple-internal` for lock/profile/temp bookkeeping. `FileAccessPolicy` rejects that first path component for every public file/list operation, and shell sandbox profiles explicitly deny it even though the rest of `output_dir` is writable. Place locks beneath `.simple-internal/locks` rather than a caller-visible `.locks` directory.

- [ ] **Step 4: Run mutation tests and confirm GREEN**

Run: `uv run pytest tests/test_file_service.py -q -k 'create or overwrite or edit or concurrent or atomic'`

Expected: PASS.

- [ ] **Step 5: Run the full service module**

Run: `uv run pytest tests/test_file_service.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/tools/files.py tests/test_file_service.py
git commit -m "feat: add atomic revision-checked file edits"
```

## Task 5: Replace Built-in Tool Contracts

**Files:**
- Modify: `agent/tools/builtin_tools.py`
- Modify: `agent/bootstrap.py`
- Modify: `tests/test_builtin_tools.py`
- Modify: `tests/test_output_dir.py`

- [ ] **Step 1: Replace legacy tests with failing public-contract tests**

Assert exact schemas and results for `read_file(root,path,start_line,line_count)`, `list_files(root,path,recursive,pattern,cursor,max_results)`, `write_file(root,path,mode,content,expected_revision)`, and `edit_file(root,path,expected_revision,replacements)`. Remove expectations for absolute paths, binary metadata, byte-zero truncation, implicit output redirection, and force overwrite.

- [ ] **Step 2: Run built-in tool tests and confirm RED**

Run: `uv run pytest tests/test_builtin_tools.py tests/test_output_dir.py -q`

Expected: FAIL against the old registrations and handlers.

- [ ] **Step 3: Inject `FileService` and make handlers thin adapters**

Construct one service from the bootstrap policy, pass it to `BuiltinTools`, register `edit_file`, and replace legacy helpers with direct service calls. Keep absolute paths only in internal channel/attachment tools that have separate trusted boundaries; do not route them through the public file schemas.

- [ ] **Step 4: Run built-in tool tests and confirm GREEN**

Run: `uv run pytest tests/test_builtin_tools.py tests/test_output_dir.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/builtin_tools.py agent/bootstrap.py tests/test_builtin_tools.py tests/test_output_dir.py
git commit -m "feat: replace built-in file tool protocols"
```

## Task 6: Root-Aware Tool Authorization and Sub-Agent Narrowing

**Files:**
- Modify: `agent/tools/runtime.py`
- Modify: `agent/tools/executor.py`
- Modify: `agent/core/agent.py`
- Modify: `tests/test_builtin_tools.py`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Write failing call-aware authorization tests**

Test that read-only/research/implementation profiles retain `write_file` and `edit_file` for `output_dir`; workspace requests additionally require `workspace_write`, startup write enablement, and scope containment; malformed, missing, unknown, wrong-typed, and invalid-enum inputs return structured `invalid_request` before the authorizer or handler runs; child policies only narrow; authorization failures retain structured `access_denied` envelopes.

- [ ] **Step 2: Run authorization tests and confirm RED**

Run: `uv run pytest tests/test_builtin_tools.py tests/test_agent_integration.py -q -k 'capabilit or profile or write_scope or file_author'`

Expected: FAIL because capability filtering currently removes the entire write tool.

- [ ] **Step 3: Add a call-aware authorization hook**

Extend `ToolDef`/`ToolRegistry.register` with an optional synchronous authorizer receiving validated input and effective registry context. Before invoking the authorizer, validate tool input against the registered schema's supported subset (`object`, `properties`, `required`, `additionalProperties`, scalar/array types, `items`, `enum`, and integer bounds) with no coercion. Return a structured `invalid_request` envelope on the first deterministic validation failure. Mark the four built-in file schemas `additionalProperties: false`; keep a compatibility path for external tool schemas using unsupported JSON Schema keywords rather than pretending they were fully validated.

Invoke the authorizer only after validation and before the tool function, returning its structured denial unchanged. Give file mutation tools the base `output_write` capability; their authorizer requires `workspace_write` only when `root == "workspace"`. Include `output_write` in restricted sub-Agent allowlists and copy only a narrowed immutable file policy plus normalized `write_scope`.

- [ ] **Step 4: Run authorization tests and confirm GREEN**

Run: `uv run pytest tests/test_builtin_tools.py tests/test_agent_integration.py -q -k 'capabilit or profile or write_scope or file_author'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/runtime.py agent/tools/executor.py agent/core/agent.py tests/test_builtin_tools.py tests/test_agent_integration.py
git commit -m "feat: authorize file writes by target root"
```

## Task 7: Enforce the Policy in Shell Descendants

**Files:**
- Create: `agent/security/filesystem_sandbox.py`
- Modify: `agent/tools/builtin_tools.py`
- Modify: `agent/bootstrap.py`
- Test: `tests/test_filesystem_sandbox.py`
- Test: `tests/test_builtin_tools.py`

- [ ] **Step 1: Write failing sandbox adapter tests**

Test immutable sandbox requests, escaped macOS profile literals, workspace hidden when read is false, workspace read-only by default, only scoped entries writable when enabled, output and private temp writable, host temp/home/Agent state/siblings non-writable, and unsupported adapter fail-closed behavior.

Use real subprocess tests on Darwin guarded by `sandbox-exec` availability. Commands must attempt create/overwrite/rename/delete/chmod through direct paths and symlinks and spawn a child process that repeats a prohibited write.

- [ ] **Step 2: Run sandbox tests and confirm RED**

Run: `uv run pytest tests/test_filesystem_sandbox.py tests/test_builtin_tools.py -q -k sandbox`

Expected: FAIL because shell currently executes without an OS filesystem sandbox.

- [ ] **Step 3: Implement the platform adapter**

Create a Darwin adapter that writes a generated Seatbelt profile under `output_dir/.simple-internal/sandbox` and returns an argv prefix for `/usr/bin/sandbox-exec -f <profile>`. Start from deny-default, allow process/runtime primitives and explicit read-only system/runtime roots, expose workspace according to read policy, and allow writes only under public output paths plus normalized scope entries while explicitly denying `.simple-internal` to the child. Set `TMPDIR`, `TMP`, and `TEMP` to a private public scratch directory under `output_dir/sandbox`.

Represent unsupported Linux/Windows adapters explicitly. When a restricted policy has no enforcing adapter, omit/deny shell with `sandbox_unavailable`; never fall back to the existing pre/post workspace snapshot or artifact moving as authorization.

- [ ] **Step 4: Route shell execution through argv-safe sandbox wrapping**

Keep the user command parsed by the intended shell inside the sandbox, but launch the outer sandbox wrapper with `asyncio.create_subprocess_exec` so profile paths are not interpolated into a shell string. Preserve cancellation, heartbeat, timeout, stdout/stderr, and risk confirmation behavior.

- [ ] **Step 5: Run sandbox tests and confirm GREEN**

Run: `uv run pytest tests/test_filesystem_sandbox.py tests/test_builtin_tools.py -q -k sandbox`

Expected: PASS, including real Darwin write denial on the current platform.

- [ ] **Step 6: Commit**

```bash
git add agent/security/filesystem_sandbox.py agent/tools/builtin_tools.py agent/bootstrap.py tests/test_filesystem_sandbox.py tests/test_builtin_tools.py
git commit -m "feat: enforce file policy in shell sandbox"
```

## Task 8: Compaction-Safe Structured File Summaries

**Files:**
- Modify: `agent/security/content_filter.py`
- Modify: `tests/test_content_filter.py`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Write failing summary and structural-history tests**

Test that summarized reads drop `content` but preserve root/path/revision/range/next cursor and encoding metadata; writes/edits preserve old/new revisions and statistics; structured errors preserve stable code/details/retryability; list summaries preserve bounded items or cursor metadata. Add an integration test that compacts a read/edit history and verifies tool-call/result pairs remain structurally complete and a stale edit still requires reread.

- [ ] **Step 2: Run summary tests and confirm RED**

Run: `uv run pytest tests/test_content_filter.py tests/test_agent_integration.py -q -k 'summar or compact or structurally_complete'`

Expected: FAIL because the current read summary treats the JSON payload as raw text.

- [ ] **Step 3: Implement metadata-preserving summaries**

Parse structured built-in results first. Copy an explicit allowlist of metadata fields, set `summarized: true`, omit bodies, and leave tool-call/result messages indivisible in existing compaction. Preserve legacy fallback summarization for plugin tools and non-JSON results.

- [ ] **Step 4: Run summary tests and confirm GREEN**

Run: `uv run pytest tests/test_content_filter.py tests/test_agent_integration.py -q -k 'summar or compact or structurally_complete'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/security/content_filter.py tests/test_content_filter.py tests/test_agent_integration.py
git commit -m "fix: preserve file snapshot metadata during compaction"
```

## Task 9: Prompt, Documentation, and Breaking-Call Migration

**Files:**
- Modify: `agent/config.py`
- Modify: `README.md`
- Modify: `tests/test_agent_integration.py`
- Modify: any repository call sites found by `rg -n 'read_file|write_file|list_files' agent tests README.md`

- [ ] **Step 1: Write failing prompt and migration tests**

Assert prompts describe explicit roots, pagination, revisions, restart-only workspace policy, fixed output access, and no implicit redirection. Assert every built-in schema/call site uses the new arguments and `edit_file` appears in active capability descriptions.

- [ ] **Step 2: Run migration tests and confirm RED**

Run: `uv run pytest tests/test_agent_integration.py tests/test_runtime_contracts.py -q -k 'prompt or tool or file'`

Expected: FAIL while old prompt/call assumptions remain.

- [ ] **Step 3: Update prompts, docs, and all in-repo callers**

Remove claims that workspace paths are redirected. Document the `~/.agent/config.json` settings and restart requirement. Update examples to read a revision before overwrite/edit and to use `root=output_dir` for artifacts. Keep unrelated public APIs unchanged.

- [ ] **Step 4: Run migration tests and confirm GREEN**

Run: `uv run pytest tests/test_agent_integration.py tests/test_runtime_contracts.py -q -k 'prompt or tool or file'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git diff --name-only
git add agent/config.py README.md tests/test_agent_integration.py tests/test_runtime_contracts.py
# Add any additional migrated call-site path explicitly after reviewing the list above.
git commit -m "docs: migrate to safe file tool contracts"
```

## Task 10: Full Verification and Main Integration

**Files:**
- Modify only files required by failures attributable to this feature.

- [ ] **Step 1: Run focused security and file suites**

Run:

```bash
uv run pytest \
  tests/test_file_service.py \
  tests/test_filesystem_sandbox.py \
  tests/test_builtin_tools.py \
  tests/test_content_filter.py \
  tests/test_output_dir.py -q
```

Expected: PASS.

- [ ] **Step 2: Run orchestration and runtime integration suites**

Run:

```bash
uv run pytest \
  tests/test_agent_integration.py \
  tests/test_runtime_contracts.py -q
```

Expected: PASS.

- [ ] **Step 3: Run the complete suite**

Run: `uv run pytest -q`

Expected: PASS with only documented environment-gated skips.

- [ ] **Step 4: Run static repository checks**

Run:

```bash
git diff --check
rg -n 'errors="replace"|errors=.replace.|read_file.*max_bytes|write_file writes generated files to output_dir' agent README.md
```

Expected: `git diff --check` is clean; search returns no legacy file-tool decoding/schema/prompt paths. Shell stdout/stderr decoding may retain replacement behavior because it is not file round-tripping and should be reviewed explicitly rather than mechanically removed.

- [ ] **Step 5: Perform two CLI smoke tests**

Start with default workspace read/deny-write and verify an Agent can read a repository file, cannot edit it through `edit_file`, cannot change it through shell, and can create/edit a file in `output_dir`. Restart with workspace write enabled, spawn an implementation Agent with one-file `write_scope`, and verify only that file is writable.

- [ ] **Step 6: Request final code review and fix blocking findings**

Use `superpowers:requesting-code-review` with the spec, plan, commit range, focused test results, and full-suite result. Re-run affected tests after any fix.

- [ ] **Step 7: Merge the implementation branch into `main` and verify**

Use `superpowers:finishing-a-development-branch`. Merge non-interactively, then run `uv run pytest -q` on `main` and confirm the worktree is clean.
