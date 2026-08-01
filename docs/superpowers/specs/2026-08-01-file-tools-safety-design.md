# File Tools Safety and Snapshot Design

## Goal

Replace the built-in file read and write contracts with a coherent, fail-closed
file service for repository work, general text files, and bounded generated
files. The design must prevent stale overwrites, partial edits, encoding damage,
path-boundary escapes, and accidental self-modification by the running Agent.

This is an intentional breaking change. Existing `read_file` and `write_file`
argument schemas do not need compatibility adapters.

## First Principles

1. Authorization is checked at the side-effect boundary and is not inferred
   from a path string, prompt instruction, or model intent.
2. `workspace` and `output_dir` are distinct security domains. The workspace is
   configurable; `output_dir` is always readable and writable.
3. A read produces an immutable, content-addressed snapshot. A mutation must
   identify the snapshot on which it is based.
4. Validation completes before mutation begins. A failed multi-step edit leaves
   the target byte-for-byte unchanged.
5. Text must round-trip losslessly. Unsupported encodings fail closed rather
   than being decoded with replacement characters.
6. Tool responses are bounded data structures, not unbounded copies of files.
7. A policy that can be bypassed through `shell` is not a security boundary.

## Scope

The first release covers:

- line-window reads for UTF-8 and UTF-8-BOM regular files;
- atomic whole-file create and overwrite;
- atomic batches of exact text replacements;
- optimistic concurrency based on exact-byte revisions;
- one startup-loaded workspace access policy shared by file tools, directory
  enumeration, and shell execution;
- bounded tool results and compaction-safe file-operation summaries.

The first release does not include automatic merging, fuzzy patching, unified
diff parsing, non-UTF-8 detection, binary editing, partial-line streaming,
append sessions, chunk-upload state, runtime permission escalation, or
model-controlled policy changes.

## Configuration and Effective Authorization

Only workspace access is configurable:

```json
{
  "file_access": {
    "workspace": {
      "read": true,
      "write": false
    },
    "max_read_lines": 400,
    "max_read_bytes": 65536,
    "max_snapshot_bytes": 16777216,
    "max_write_bytes": 4194304,
    "max_replacements": 100,
    "max_list_results": 1000
  }
}
```

The concrete defaults above are initial safe limits and may be tuned during
implementation only if tests demonstrate that the existing provider/tool
budgets require lower values. They remain startup configuration, not tool-call
arguments. Unknown keys or non-boolean workspace permissions are configuration
errors. Limits must be positive integers and must have hard implementation
ceilings so an unsafe configuration cannot make one tool result or in-memory
mutation unbounded.

`output_dir` is not a configurable permission domain. It is always readable and
writable. Its location remains controlled by the existing top-level
`output_dir` setting.

After resolving existing symlinks, `workspace` and `output_dir` must be disjoint:
neither root may equal, contain, or be contained by the other. Bootstrap rejects
an overlapping configuration before registering file or shell tools. This is a
security invariant, not a precedence rule; an always-writable `output_dir`
inside the workspace would otherwise bypass workspace protection. Root identity
checks also account for aliases that resolve to the same filesystem object.

`FileAccessPolicy` is constructed during bootstrap and is immutable for the
lifetime of the Agent instance. Changing the configuration requires a restart.
The model has no tool for reading, replacing, or elevating the active policy.

The effective workspace rules are:

- reads require `file_access.workspace.read`;
- directory enumeration requires `file_access.workspace.read`;
- writes require `file_access.workspace.write` and containment in the current
  `write_scope`;
- an empty or absent `write_scope` denies every workspace mutation even when
  workspace writes are enabled;
- a sub-Agent can only narrow the parent startup policy. Its capability profile
  and `write_scope` can never enable a permission disabled at bootstrap;
- `output_dir` reads and writes remain available without `write_scope`.

The default is therefore a readable, non-writable workspace and a readable,
writable `output_dir`.

## Architecture

### `FileAccessPolicy`

This component owns root selection and authorization. It accepts an explicit
root (`workspace` or `output_dir`) and a relative path, then returns a typed,
canonical target only after validating the requested operation.

It rejects:

- absolute paths;
- empty paths where a regular file is required;
- `.` or `..` path components;
- paths containing NUL bytes;
- targets outside the selected root after canonicalization;
- symlink traversal that escapes the selected root;
- non-regular existing targets for file operations;
- operations denied by the root policy or effective `write_scope`.

The sole exception to rejecting `.` components is the exact path `.` for
`list_files`, where it denotes the selected root directory. `.` remains invalid
inside any longer path and for every regular-file operation.

Authorization must be resistant to time-of-check/time-of-use path replacement.
The implementation should use descriptor-relative operations with no-follow
semantics where the platform supports them. If the platform cannot provide a
race-resistant primitive for a requested mutation, that mutation fails closed.
A lexical `resolve()` check alone is not sufficient for writes.

### `FileSnapshotStore`

This stateless component opens an authorized regular file, reads exact bytes
within configured limits, validates UTF-8, records text metadata, and computes
the revision over the complete file bytes:

```text
revision = "sha256:" + sha256(exact_file_bytes).hexdigest()
```

The revision includes a UTF-8 BOM when present. It is never computed over a
normalized string. Snapshot reads use a stable file descriptor and verify that
the file did not change while being read; an unstable read returns a retryable
conflict instead of a revision/content pair assembled from different states.

### `AtomicFileMutator`

This component implements create, overwrite, and structured edit. For an
existing file it acquires the canonical-target lock, obtains a stable snapshot,
checks the expected revision, validates and applies the complete mutation in
memory, then writes a temporary file in the target directory and atomically
replaces the target.

It preserves the original file mode on overwrite/edit and preserves the
original BOM. A new file uses the process `umask` and never requests executable
permissions. Before the replace, it flushes the temporary file and calls
`fsync`; after replacement it syncs the containing directory when supported.
Temporary files are cleaned after every failed path. A successful response is
returned only after the replacement has completed.

Mutation serialization has two layers:

- an in-process lock keyed by canonical root and relative path;
- an advisory lock stored in a private lock namespace under `output_dir`, keyed
  by the canonical root identity and relative target path, for cooperating Agent
  processes. Lock bookkeeping never creates files beside a workspace target or
  outside its effective `write_scope`.

The lock is acquired before reading the comparison revision and held through
replacement. Unsupported or failed cross-process locking returns a fail-closed
error. This protocol gives strict stale-write exclusion among cooperating Agent
processes. Descriptor-relative identity and revision checks are repeated
immediately before replacement to detect non-cooperating changes and prevent
symlink redirection.

Portable filesystem APIs do not provide atomic compare-revision-and-rename
against arbitrary non-cooperating writers. Therefore the service does not claim
strict CAS against an external editor that ignores the Agent lock and changes
the file in the final check/replace interval. The default non-writable workspace
eliminates that risk for the Agent repository. When workspace writes are
explicitly enabled, this narrow residual risk must be documented rather than
hidden behind a false concurrency guarantee.

### Tool Adapters

`BuiltinTools` retains registration and JSON-schema adaptation only. It does
not resolve paths, decode content, calculate revisions, or perform writes.
`read_file`, `write_file`, `edit_file`, and `list_files` call the shared service.

The current capability system filters whole tools and cannot express a
root-dependent permission. It must gain call-aware authorization before tool
execution: `write_file` and `edit_file` remain available for `output_dir` in
every Agent profile, while a request with `root=workspace` additionally
requires the profile's `workspace_write` capability, the startup workspace
write switch, and `write_scope` containment. Schema validation runs before this
authorization hook, and authorization runs before any path I/O. Tool listing
and system-prompt capability descriptions must reflect this effective behavior
instead of treating availability of the tool as workspace-write permission.

## Rooted Path Contract

Every file operation takes an explicit security root and a relative path:

```json
{
  "root": "workspace",
  "path": "agent/config.py"
}
```

There is no implicit root, absolute-path mode, or automatic redirection from
workspace to `output_dir`. The returned `path` remains root-relative; a separate
`root` field identifies the domain. This avoids leaking or later replaying host
absolute paths in model context.

## `read_file`

### Request

```json
{
  "root": "workspace",
  "path": "agent/config.py",
  "start_line": 1,
  "line_count": 200
}
```

`start_line` is one-based and defaults to 1. `line_count` must be positive and
defaults to a conservative value no greater than `max_read_lines`. A
`line_count` above the configured limit is an `invalid_request`, not silently
clamped. A `start_line` greater than `total_lines` is also `invalid_request`;
the empty-file request at `start_line=1` is the sole empty-range success.

### Success

```json
{
  "ok": true,
  "root": "workspace",
  "path": "agent/config.py",
  "content": "...",
  "revision": "sha256:...",
  "start_line": 1,
  "end_line": 200,
  "total_lines": 480,
  "next_start_line": 201,
  "size_bytes": 18240,
  "returned_bytes": 7210,
  "encoding": "utf-8",
  "bom": false,
  "newline": "lf"
}
```

`content` contains exact decoded text, excluding an encoding BOM, and has no
injected line-number prefixes. The separate `bom` field records whether the
exact byte snapshot began with a UTF-8 BOM.
`next_start_line` is `null` at end of file. Empty files return empty content,
`total_lines: 0`, `end_line: null`, and `next_start_line: null`. A file whose
last line has no terminator still counts that line. A trailing line terminator
does not create an additional empty line. Returned content includes each
selected line's original terminator when one exists. `returned_bytes` is the
length of that returned content encoded as strict UTF-8 and excludes the
stripped encoding BOM. `newline` is one of `lf`, `crlf`, `cr`, `mixed`, or
`none`; mixed newlines are readable and editable because exact replacement does
not require global normalization.

The response is bounded by both `max_read_lines` and `max_read_bytes` and ends
only at a complete line boundary. If the requested first line alone exceeds the
byte bound, the tool returns `line_too_large`; it never emits a partial line that
cannot be addressed by the line cursor.

Computing a full-file revision and total line count requires scanning the
complete file. To keep resource use bounded, files larger than
`max_snapshot_bytes` return `file_too_large`. The implementation may scan in
chunks and retain only the requested line window; it must not load the entire
file merely to return a page.

## `list_files`

`list_files` uses the same explicit root and policy as reads:

```json
{
  "root": "workspace",
  "path": "agent",
  "recursive": true,
  "pattern": "*.py",
  "cursor": null,
  "max_results": 200
}
```

`path` identifies an existing directory and defaults to `.`. A missing path
returns `not_found`; an existing non-directory returns `not_directory`.
`recursive` defaults to
false. `pattern` is an optional basename glob with no path separator; it
defaults to `*`. Recursive enumeration visits descendants in deterministic
lexicographic root-relative path order. `max_results` must be positive and no
greater than `max_list_results`; excessive requests are rejected rather than
clamped.

Results are bounded and contain root-relative paths only:

```json
{
  "ok": true,
  "root": "workspace",
  "path": "agent",
  "items": [
    {"path": "agent/config.py", "kind": "file", "size_bytes": 18240},
    {"path": "agent/tools", "kind": "directory", "size_bytes": null}
  ],
  "count": 2,
  "truncated": true,
  "next_cursor": "opaque-value"
}
```

The cursor is an opaque, root/path/pattern/recursive-bound continuation token;
using it with different request parameters is `invalid_request`. It resumes
strictly after the last emitted lexical path. Directory listings are discovery,
not immutable snapshots: concurrent directory changes can cause omissions or
new items between pages, and callers must use `read_file` revisions for file
consistency.

Enumeration never follows directory symlinks. A symlink entry may be returned
with `kind=symlink`, but its target and target metadata are not exposed and it
is never traversed. The traversal maintains bounded internal state, detects
directory identity cycles, and stops when `max_list_results` is reached; it
does not gather an unbounded tree and sort it afterward.

The complete `kind` vocabulary is `file`, `directory`, `symlink`, and `other`.
Sockets, FIFOs, devices, and any platform-specific non-regular entry are
reported as `other` with `size_bytes: null` and are never opened or traversed.

## `write_file`

`write_file` is for whole-file creation or replacement.

### Create

```json
{
  "root": "output_dir",
  "path": "reports/result.md",
  "mode": "create",
  "content": "..."
}
```

Create requires the target not to exist and always creates missing parent
directories within the selected root after every component has passed policy
checks. An existing non-directory parent returns `not_directory`. Creation
never replaces an existing path.

### Overwrite

```json
{
  "root": "workspace",
  "path": "agent/config.py",
  "mode": "overwrite",
  "expected_revision": "sha256:...",
  "content": "..."
}
```

Overwrite requires an existing regular file and a valid `expected_revision`.
There is no force flag. Content must be valid UTF-8, contain no NUL byte, and
fit `max_write_bytes`. The tool preserves an existing UTF-8 BOM unless the
contract later adds an explicit BOM field; the caller supplies decoded content
without the encoding BOM and cannot accidentally remove it through a
read-modify-overwrite cycle.

### Success

Both modes return a bounded summary and never echo the full content:

```json
{
  "ok": true,
  "root": "workspace",
  "path": "agent/config.py",
  "mode": "overwrite",
  "old_revision": "sha256:...",
  "new_revision": "sha256:...",
  "old_size_bytes": 18240,
  "new_size_bytes": 18302,
  "byte_delta": 62
}
```

For create, `old_revision` is `null` and `old_size_bytes` is zero.

## `edit_file`

`edit_file` applies an ordered batch of exact replacements to one snapshot:

```json
{
  "root": "workspace",
  "path": "agent/config.py",
  "expected_revision": "sha256:...",
  "replacements": [
    {
      "old_text": "old value",
      "new_text": "new value",
      "expected_count": 1
    }
  ]
}
```

The replacement list must be non-empty and no greater than `max_replacements`.
The aggregate strict-UTF-8 byte length of every `old_text` and `new_text` must
not exceed `max_write_bytes`, which bounds request material retained for the
edit. Each fragment must be strictly UTF-8 encodable and contain no NUL;
`old_text` must also be non-empty. `expected_count` is required and must be a
positive integer.

Match counting and replacement use deterministic, non-overlapping,
left-to-right semantics equivalent to Python `str.count(old_text)` followed by
`str.replace(old_text, new_text)`. For example, `old_text="aa"` occurs once in
`"aaa"`, not twice. Text inserted by one occurrence is not scanned again by the
same replacement operation. Each later replacement runs against the complete
in-memory result of the preceding operation, so it may match text introduced by
an earlier operation. The observed non-overlapping count must equal
`expected_count` exactly before that step is applied.

The service first validates the target revision and all static request limits,
then simulates every replacement. Every intermediate string, not only the final
one, must encode to no more than `max_write_bytes`; an operation that temporarily
expands beyond the limit fails before the next operation. After the final
operation the service strictly encodes the text and verifies that it contains
no NUL and does not begin with U+FEFF. It commits only if every check succeeds.
Any failure leaves the target byte-for-byte unchanged. Edits preserve the
original BOM and all bytes outside the replaced spans, including newline style
and absence of a final newline.

Success returns old/new revisions, old/new byte sizes, byte delta, replacement
operation count, and total replaced occurrence count. It does not return the
new file body.

## Encoding Rules

Readable and mutable text is limited to strict UTF-8 with an optional leading
UTF-8 BOM. The decoder consumes exactly one leading encoding BOM; that marker
is excluded from `read_file.content` and from the string on which `edit_file`
matches. The revision and byte-size fields still cover the complete original
bytes including the BOM. Overwrite/edit reattach the preserved BOM exactly
once. Create writes UTF-8 without an encoding BOM. Because a leading U+FEFF has
the same bytes as a UTF-8 BOM, caller-provided content beginning with U+FEFF is
rejected as `invalid_request` until a future contract adds an explicit BOM
field. U+FEFF elsewhere in content is ordinary text. Decode errors, embedded
NUL bytes, and BOMs for other encodings return `unsupported_encoding`. The
implementation never uses `errors=replace`.

Mixed newline sequences are not an encoding error. Read metadata reports them
as `mixed`; exact replacements operate on the decoded text without newline
normalization. Whole-file writes encode the caller's content as UTF-8 and
preserve the existing BOM during overwrite.

## Errors and Recovery

Every expected failure has the same envelope:

```json
{
  "ok": false,
  "error": {
    "code": "revision_conflict",
    "message": "File changed since it was read.",
    "details": {
      "expected_revision": "sha256:...",
      "actual_revision": "sha256:..."
    },
    "retryable": true
  }
}
```

Stable error codes are:

| Code | Meaning | Retryable |
| --- | --- | --- |
| `access_denied` | Effective root policy or `write_scope` denies the operation | No |
| `invalid_request` | A typed parameter, range, limit, pattern, or cursor is invalid | No |
| `invalid_path` | Path or root is malformed, absolute, or escapes its root | No |
| `not_found` | Required target does not exist | No |
| `already_exists` | Create target already exists | No |
| `not_directory` | A required directory or parent component is not a directory | No |
| `not_regular_file` | Existing target is not a regular file | No |
| `unsupported_encoding` | Bytes are not supported lossless text | No |
| `file_too_large` | File/request exceeds a configured resource ceiling | No |
| `line_too_large` | First requested line exceeds the page byte ceiling | No |
| `revision_required` | A mutation omitted its mandatory revision | No |
| `revision_conflict` | Current bytes do not match the expected snapshot | Yes |
| `match_count_mismatch` | An edit operation observed a different match count | No |
| `locking_unavailable` | Required safe locking is unavailable | Yes |
| `atomic_replace_failed` | The commit could not be completed safely | Yes |

Unexpected I/O failures use a stable `io_error` code and a sanitized message;
they do not expose arbitrary host paths or exception representations. Conflict
details may include the current revision so the caller can recognize change,
but recovery always requires a fresh `read_file`; tools never auto-merge or
retry a mutation against unseen content.

## Shell Enforcement

File-tool authorization alone cannot protect the Agent repository because a
shell process can write files independently. Shell execution therefore consumes
the same immutable `FileAccessPolicy`.

When workspace reads are disabled, the shell sandbox does not expose the
workspace at all and rejects a workspace `cwd`. When workspace reads are
enabled but writes are disabled, a shell process receives the workspace as
read-only and `output_dir` as writable. When workspace writes are enabled for a
sub-Agent, the sandbox exposes only the approved `write_scope` entries as
writable and leaves the remainder of the workspace read-only. All cases use an
operating-system filesystem sandbox; the command's `cwd` does not grant
additional rights.

The sandbox denies writes everywhere else on the host. It exposes only the
minimum platform runtime required to launch commands (executables, shared
libraries, devices, and process metadata) as read-only. A private temporary
directory is created under `output_dir` and supplied through `TMPDIR`, `TMP`,
and `TEMP`; the host's global temporary directories are not writable. Agent
state and configuration directories, user home contents, sibling repositories,
and arbitrary absolute paths are neither made writable nor implicitly exposed.
The platform adapter maintains an explicit read-only runtime allowlist and a
writable-root allowlist; it must not use a broad writable host mount.

The platform adapter must enforce these rights in the child process, including
descendants; command parsing, environment hints, pre/post directory snapshots,
and moving newly created files after execution are not authorization
mechanisms. On a platform where the required sandbox cannot be constructed,
the Agent must not register or execute `shell` for a restricted instance and
must report a clear startup/capability diagnostic. Existing risk classification
and user confirmation remain additional controls, not substitutes for the
filesystem boundary.

## Context and History

`read_file` returns only the requested bounded window. Continuation requires a
new call using `next_start_line`; there is no automatic insertion of subsequent
pages. Mutation results contain only revisions and statistics.

Tool-history summarization preserves the minimum state needed for safe follow-up:

- tool name, root, and relative path;
- success or stable error code;
- revision;
- returned line range and `next_start_line` for reads;
- old/new revisions and mutation statistics for writes/edits.

Read bodies may be compacted or evicted as context pressure grows. Tool-call and
tool-result structure remains indivisible so compaction cannot create an
orphaned tool result. Eviction of a body never relaxes mutation preconditions:
if the model no longer has the relevant content, it must read again before
editing.

## Integration and Migration

Bootstrap validates `file_access`, constructs one policy, and passes the same
instance to built-in file services and shell execution. Sub-Agent registries
receive a narrowed view derived from that immutable parent policy and their
capability profile/write scope.

The system prompt and README must be updated for explicit roots, line paging,
revision-based mutation, fixed `output_dir` access, and startup-only workspace
permissions. Existing prompt text claiming that `write_file` redirects paths
to `output_dir` must be removed.

Call sites and tests using the old schemas migrate directly. There is no
`read_file_v2`, compatibility alias, force-overwrite path, or deprecation
period. Plugin-provided tools are unaffected unless they explicitly opt into
the shared file service.

## Testing and Acceptance Criteria

Implementation follows focused red-green cycles. Required coverage includes:

1. Configuration defaults, invalid values, restart-only loading, and immutable
   narrowed sub-Agent policy derivation.
2. Explicit roots, root overlap/alias rejection, absolute paths, traversal,
   NULs, symlink escapes, symlink substitution races, and non-regular targets.
3. Workspace read denial across file tools, listing, and shell; workspace write
   denial, `write_scope` intersection, fixed `output_dir` access, and
   directory-listing parity.
4. UTF-8, UTF-8 BOM, CRLF, CR, mixed newlines, no final newline, empty files,
   invalid UTF-8, embedded NULs, and unsupported BOMs.
5. One-based paging, invalid ranges/limits, end-of-file metadata, complete-line
   and trailing-terminator semantics, returned-byte accounting, complete-line
   byte bounds, oversized first lines, snapshot-size ceilings, and mutation-size
   ceilings.
6. Create conflicts, mandatory overwrite revisions, stale revisions, mode
   preservation, safe parent creation, temporary cleanup, and directory sync
   behavior where supported.
7. Ordered replacement semantics, exact counts, overlapping/sequential effects,
   inserted-text traversal, invalid fragments/final text, empty patterns,
   aggregate payload, batch/intermediate/final-size enforcement, and
   byte-for-byte rollback after every failure point.
8. Same-process and cooperating cross-process races proving only one mutation
   can commit from a shared revision; observable external changes and target
   substitution must produce conflict/fail-closed outcomes. Tests must not claim
   portable strict CAS against a non-cooperating writer in the final
   check/replace interval.
9. Shell attempts to create, overwrite, rename, delete, chmod, or modify through
   descendants and symlinks outside effective writable roots; equivalent writes
   in `output_dir` must succeed. Host temp directories, home/Agent state, and
   sibling paths must remain non-writable while the private output temp works.
10. Bounded tool results and history summaries; after compaction, tool history
    remains structurally complete and retained revisions/ranges still drive
    safe reread behavior.
11. Updated integration tests for tool schemas, call-aware capability checks,
    prompts, content filtering, orchestration `write_scope`, and generated
    artifacts. Read-only/research profiles must be able to mutate `output_dir`
    but not the workspace.
12. Bounded deterministic directory enumeration, cursor/request binding,
    root `.` handling, complete entry kinds, symlink non-traversal, cycle
    handling, access denial, and concurrent-change semantics.

Acceptance requires the focused file/security/context tests and the complete
test suite to pass. Platform-specific shell sandbox tests may be skipped only
when the platform is explicitly unsupported; on that platform, tests must prove
that restricted shell capability fails closed instead of running unsandboxed.

## Success Conditions

The design is complete when:

- the default Agent can inspect its repository but cannot modify it through
  file tools or shell;
- enabling workspace writes at startup still requires explicit per-Agent
  `write_scope` containment;
- generated files can always be created and revised under `output_dir`;
- every overwrite/edit is based on an exact prior snapshot and stale writes
  cannot commit silently;
- failed edits and writes never leave partial target content;
- unsupported text never round-trips through replacement characters;
- large reads and mutation responses remain bounded in model context; and
- context compaction preserves structurally valid tool history and enough
  revision/range metadata for safe recovery.
