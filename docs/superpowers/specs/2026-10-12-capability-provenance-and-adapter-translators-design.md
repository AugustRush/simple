# Capability provenance and adapter translators — design

Two abstractions are missing, and three "we did not do it" items are symptoms of
them rather than three separate decisions:

* importing a DSH profile's MCP servers, installing from npm, and Codex's `apps` /
  `interface.defaultPrompt` are all *capability adoption* problems, and the plugin
  model has no place to say where a capability came from or whether a human looked
  at it;
* the same three are also *translation* problems, and `PluginFormat` can say where
  a file is but not what it means.

This document states the invariants, the data shapes, the migration order and the
tests for each. It is a design, not a plan of record: nothing here is implemented
yet, and the measurements below are what it has to stay true to.

## A. Trust is procedural, not structural (measured)

What a capability is today: a name bound to an object.

```
ctx.provide("ltm_store", store)        # 25 call sites across the tree
```

Nothing on that object says where it came from, what it will execute, or whether
anyone approved it. The only trust machinery in the tree is:

| Piece | Where | What it actually is |
|---|---|---|
| content-shape judgement | `agent/tools/plugin_tools.py:_plugin_executable_content` | a *string* built from path markers (`__init__.py`, `.mcp.json`, `hooks/hooks.json`, manifest keys) |
| the question | `confirm_plugin_install` → `sink.on_tool_confirmation(risk_level="high")` | one call site, inside the installer |
| the record | `agent/security/plugin_approval.py` | `_APPROVED: dict[str, float]`, keyed `session\0channel\0user\0source`, **TTL 5 minutes, in-process** |
| the reader | `plugin_install_was_approved(scope, source)` | the installer, and nothing else |

So approval is (a) keyed by the *source string* a human was shown, (b) gone after
five minutes, (c) gone after a restart, and (d) attached to nothing that ends up
running. Four source kinds can lead to execution today --
`local path`, `git`, `marketplace entry`, `bundled core plugin` -- and each of them
produces capabilities that are anonymous afterwards. A fifth kind (another
application's profile, or an npm tarball) cannot even be expressed: it either goes
through `install_plugin`, which knows only its own sources, or it does not happen.

**Invariant A.** Every capability that can execute something carries, on itself:

1. **who declared it** — the source, plus the plugin/import that produced it;
2. **what it can run** — the concrete commands, entry points or hook lines;
3. **whether a human approved *that*** — a durable answer, not a five-minute one.

### Data shapes

```python
@dataclass(frozen=True)
class Provenance:
    source: str            # "github.com/x/y", "$HOME/.dsh/profiles/desktop", "core:memory"
    kind: str              # "plugin" | "import" | "core"
    declared_by: str = ""  # the manifest/import that named this capability ("dsh-mcp-client")
    digest: str = ""       # of what will run, so an edit is not the same approval

@dataclass(frozen=True)
class Execution:
    kind: str              # "python-entry" | "mcp-stdio" | "hook-command" | "npm-package"
    payload: dict          # command, args, env — exactly what would run
```

* `ServiceRegistry.provide(name, value, *, replace=False, provenance=None)` — the
  registration becomes the place the answer lives, because that is the moment a
  capability enters the composition.
* `ctx.provide(...)` passes it through, and the *scope* records it, so unwinding
  a plugin also unwinds its provenance.
* A durable ledger (a small SQLite table next to the other agent state, or a JSONL
  under the home) records `(provenance.digest, execution.kind) → approved_at,
  approved_by, prompt_shown`. `plugin_approval.py`'s in-process map stays for the
  *interactive* round trip; the durable record is what a restart reads.
* `Execution` is produced by the *format adapters*, not by the installer: what
  counts as code is the ecosystem's answer, and the adapters already know it
  (`executable_files`, `executable_dirs`, `executable_keys`, `mcp_files`,
  `hooks_files`). `_plugin_executable_content` becomes a rendering of
  `list[Execution]`, which is why the same list can be shown by the DSH import and
  by an npm source without either learning the markers.

### The one report every adoption path reads

```python
@dataclass(frozen=True)
class CapabilityReport:
    source: Provenance
    would_run: tuple[Execution, ...]
    inert: tuple[str, ...]        # translated declarations nothing dispatches (see B)
    approved: bool                # from the durable ledger, by digest
    unsupported: tuple[str, ...]  # recognised and not adopted, with the reason
```

`install_plugin`, a future `import_dsh_profile`, and a future npm source are then
three **producers** of a report and one **consumer** of the confirmation, instead
of three special cases. "Approve" becomes a property of what will run, not of a
string that was typed.

### Migration order (each step testable on its own)

1. `Provenance` + `Execution` + `provide(provenance=...)`; core plugins pass
   `kind="core"`, everything else `kind="plugin"`. Behaviour unchanged; a test
   asserts every `provide` site passes one (a source scan, like the ledger's).
2. `CapabilityReport` from a plugin directory, built on the adapters' markers;
   the installer renders it. `_plugin_executable_content`'s current strings become
   *one* renderer, so the existing installer tests must stay green unchanged.
3. The durable ledger + digest; `install_plugin` writes and reads it. A test:
   approve, restart the process (fresh module state), install again → already
   approved; edit the hook command → digest changed → asks again.
4. `import_dsh_profile(path)` as the second producer: it reports
   `would_run` (stdio commands) and imports only what a human approved. This is
   the item that was blocked on "consent" — after step 3 it needs no new concept.

## B. Adapters locate; they do not interpret (measured)

`PluginFormat` today, classified by what it actually does:

| Field | Kind |
|---|---|
| `manifests`, `marketplaces`, `hooks_files`, `mcp_files`, `markers`, `executable_files`, `executable_dirs`, `executable_keys`, `user_skill_roots` | **locator** — a path or a filename |
| `hook_events` | **translator** — the only one: `"PreToolUse" → "on_pre_tool"` |
| `root_environment`, `data_environment`, `project_environment` | vocabulary — names for shell variables |

`hook_events` is also where the design's problem is measurable: three formats
declare 30 translated event names, and **4** of them reach a command-hook
dispatcher (`on_pre_tool`, `on_post_tool`, `on_prompt_submit`, `on_turn_end`);
26 translate and never run. The knowledge of which four exist lives in
`catalog.COMMAND_HOOK_EVENTS`, a hand-written tuple kept honest by a test that
reads the call sites out of the source. That is a patch on the seam: the fact is
*derived twice* (once by hand, once by scan) because nothing declares it.

**Invariant B.** A translation declares its target. "Translated but nothing runs"
is computed from the declarations, not maintained beside them.

### Data shapes

```python
@dataclass(frozen=True)
class Contribution:
    kind: str        # "skill" | "command" | "agent" | "hook" | "mcp" | "app" | "prompt"
    name: str
    payload: dict

@dataclass(frozen=True)
class PluginFormat:
    ...
    #: Where each translated event is handled.  A dispatcher registers itself:
    #: ``PluginCatalog.fire_pre_tool`` is ``hook_targets()["on_pre_tool"]``.
    hook_targets: Mapping[str, str] = field(default_factory=dict)
    #: How to read contributions this format declares but the loader has no
    #: locator for (a YAML patch, an app manifest, a default prompt).  Absent
    #: means "the locators above cover it".
    interpreter: Optional[Callable[[Path], Iterable[Contribution]]] = None
```

* `catalog` gets its dispatched set from the registered targets: a decorator or an
  explicit `register_hook_target("on_pre_tool", "fire_pre_tool")` at the class
  definition. `COMMAND_HOOK_EVENTS` becomes `tuple(hook_targets())`, and the
  source-scanning test keeps its job (it now checks the *registry* against the
  call sites, which is what it should have been checking).
* `interpreter` is what DSH needed: a format that *is* a plugin-adjacent directory
  but whose declarations are YAML rows. With it, `agent/plugins/dsh.py`'s reader
  becomes one adapter in the registry instead of a module beside it — and Codex's
  `apps` / `defaultPrompt` become `Contribution(kind="app"|"prompt")`, which the
  catalog either consumes (if a consumer registers for that kind) or reports as
  unsupported with a reason. **Contribution kinds therefore need a registry too**,
  or "a new kind" is the same closed-set problem one level up:

```python
register_contribution_kind("skill", loader=..., consumer="SkillCatalog")
```

### Migration order

1. `hook_targets` on the formats + registration at the dispatchers; `inert_*`
   computed. The existing inert-hook tests must pass unchanged (they are the
   acceptance criterion for "computed, not listed").
2. `Contribution` + the kind registry; the declarative loaders (`_load_command_files`,
   `_load_agent_files`, skills, mcp) are expressed as contributions.
3. `interpreter`; `dsh.py` registers a format that uses it, and its tests move from
   "the module reads a profile" to "the registry reads a profile".
4. Codex `apps` / `defaultPrompt` as contributions — adopted if a consumer exists,
   reported otherwise.

## What this does not cover

* **The memory protocol** (the queue below). It is a *different* defect: the
  protocol is not closed under attribute access, which the guard that checks it
  cannot see because it scans methods only. It is being fixed next, and by
  measurement, not by this design.
* **Sandboxing hook and MCP execution.** This design says who approved what; it
  does not constrain what an approved command may do. That is the file-access and
  shell-permission layer's job and it already exists.
* **Marketplace/bundle patch semantics.** They already work through the profile
  patches; provenance attaches to what they produce, not to how they are written.

## How we will know it worked

| Claim | Test |
|---|---|
| every capability carries provenance | source scan: every `provide(` passes it, or is on a recorded exemption list |
| approval is durable and per-execution | approve → fresh process state → still approved; edit the command → asked again |
| one report, three producers | installer, DSH import and npm source all render `CapabilityReport`; the installer's existing tests stay green |
| translations declare targets | `hook_targets` == the call sites found in the source (the existing guard, widened) |
| nothing runs that no consumer requested | a contribution of an unknown kind is reported, never executed |
