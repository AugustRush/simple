"""The registry of module-level mutable state that escapes the service layer.

Why this file exists
--------------------
The refactor these entries belong to promises one thing above all others: a
registration made by a provider is an *effect*, and it unwinds when the
provider unloads.  That promise is only as good as its weakest escape hatch.
A ``ContextVar`` set by a tool, a path constant rewritten through ``global``,
or a module-level dict that accumulates across sessions all survive unload --
the provider is gone and its state is not.  The service layer would then be
decoration on top of state nobody owns.

So the state is written down once, here, and ``tests/test_state_inventory.py``
fails whenever the tree acquires module-level mutable state that is not in this
ledger.  Adding an entry is deliberately cheap and editing the ledger is
deliberately required: the review cost is the only thing standing between
"invented a new global" and "shipped it quietly".

What the scanner looks for
--------------------------
Exactly three shapes, because the guard test can only enforce what it can find:

``CONTEXTVAR``
    A module-level ``x = ContextVar(...)``.  These are the implicit-argument
    channel; because they propagate through ``copy_context()`` into worker
    threads, their reach is wider than a plain module global.

``REBOUND``
    A module-level name reassigned through a ``global`` statement inside a
    function.  The module's value changes for every reader at once, so two
    providers cannot hold different views of it.

``CONTAINER``
    A module-level name mutated in place (``name[k] = v``, ``name.pop(...)``,
    ``name.add(...)``) from a function that neither declares it ``global`` nor
    assigns it locally.  Nothing signals the mutation, so it accumulates
    silently across sessions.

Deliberately **not** scanned, and why
-------------------------------------
State that the scanner cannot see is not state that does not exist.  These
shapes are real and are recorded as notes rather than entries, because listing
them would make the guard test assert something it never verified:

- **Instance attributes.** ``ToolRegistry._context_override``
  (``agent/tools/runtime.py:388``) is per-registry, not per-module.  It is the
  tool-resolution seam Task 4 must reconcile; a reader looking for it should
  not conclude it was overlooked.
- **Module-level bindings never rebound and never mutated.** ``CONSOLE``
  (``agent/shared.py:510``) is constructed once and used, never reassigned.
  ``_SYNC_TOOL_EXECUTOR_LOCK`` (``agent/tools/runtime.py:80``),
  ``_IN_PROCESS_LOCKS_GUARD`` (``agent/tools/files.py:225``) and
  ``_ATTACHMENT_BATCH_CAPACITY_LOCK``
  (``agent/channels/feishu_attachments.py:44``) are the
  same shape: built at import, called, never replaced.
- **``ContextVar`` declared inside a function.** ``runtime_event_buffer``
  (``agent/channels/base.py:562``) is created per channel runner, so it is
  function-local by construction rather than module state.
- **A process-level singleton built by a constructor call.**
  ``agent/core/providers.py:SHARED`` is the provider registry: the wire formats
  the process knows, populated by two ``register`` calls at import.  It is
  module-level mutable state by any useful definition -- Task 2 added it -- but
  its shape is a ``ProviderRegistry()`` call rather than a scanned one.  It is
  process-wide *on purpose*, for the same reason ``_DEFAULT_PROVIDER`` in
  ``agent/exec/subprocess.py`` is: a wire format belongs to the process, not to
  a session.  Recorded here, and assigned to Task 9, so the process-layout work
  has the complete list to reason about rather than the list the scanner could
  reach.

Scope vocabulary
----------------
``process``
    One value for the whole interpreter.  Cannot be per-agent by construction.
``session``
    Keyed by session but stored in one module-level structure.
``turn``
    Valid only for the duration of one turn; set and reset by the turn driver.
``build``
    Written once during bootstrap.  Mutable in form, effectively constant
    after startup.

Convergence vocabulary
----------------------
``service``   becomes a service resolved through the registry
``explicit``  becomes an explicit parameter threaded through the call
``scope``     becomes a disposable registration scope owned by its provider
``stays``     deliberately retained; ``reason`` says why
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Shape = Literal["REBOUND", "CONTAINER", "CONTEXTVAR"]
Scope = Literal["process", "session", "turn", "build"]
Convergence = Literal["service", "explicit", "scope", "stays"]


@dataclass(frozen=True)
class StateEntry:
    """One module-level name that something outside could be relying on."""

    symbol: str
    path: str
    line: int
    shape: Shape
    scope: Scope
    convergence: Convergence
    #: Task number in docs/superpowers/plans/2026-10-01-service-kernel-refactor.md
    task: int | None = None
    reason: str = ""


LEDGER: tuple[StateEntry, ...] = (
    # ── Turn-scoped implicit arguments ───────────────────────────────────────
    # How the runtime passes "which turn am I in" without putting it in a
    # signature.  They reach worker threads through copy_context(), so a tool
    # running off-loop still sees them.
    StateEntry("_active_sink", "agent/core/output.py", 1070, "CONTEXTVAR", "turn", "explicit", 7,
               "Current turn's OutputSink; every tool and sub-agent event exits here."),
    StateEntry("_active_event_collector", "agent/core/output.py", 1112, "CONTEXTVAR", "turn", "explicit", 7,
               "Per-turn RuntimeEvent collector; drained into TurnExecution.events."),
    StateEntry("_active_assistant_text", "agent/core/output.py", 1119, "CONTEXTVAR", "turn", "explicit", 7,
               "Most recent assistant text, read by the requires_intent gate."),
    StateEntry("_active_tool_progress", "agent/tools/executor.py", 54, "CONTEXTVAR", "turn", "explicit", 6,
               "Progress callback a running tool reports through."),
    StateEntry("active_agent_context", "agent/core/turn_context.py", 40, "CONTEXTVAR", "turn", "stays", 7,
               "Tools read ctx.messages through this. Intended to become state the "
               "ctx service owns and exposes, not to disappear. Moved out of "
               "agent/core/agent.py when the turn loop began moving out of "
               "BaseAgent: the loop has to ask which agent is in flight, and "
               "importing that from agent.py would be a cycle. Re-exported under "
               "the old private name so the twelve callers did not change."),
    StateEntry("missing_usage_warned", "agent/core/turn_context.py", 52, "REBOUND", "process", "stays", None,
               "Whether the 'provider reported no usage' line has been printed. Process "
               "scope, not turn scope: it is a fact about this run, not about an agent. "
               "Moved from a BaseAgent class attribute when the loop code that reads it "
               "left BaseAgent -- a class attribute was a cycle for the module."),
    StateEntry("active_orchestration_runs", "agent/core/turn_context.py", 38, "CONTEXTVAR", "turn", "explicit", 8,
               "This turn's orchestration run records; moves with the orchestration driver. "
               "Sank into agent/core/turn_context.py with the other turn-scoped state when the "
               "turn loop moved out of BaseAgent, and is re-exported under the old private name."),
    StateEntry("_active_schedule_target", "agent/tools/runtime.py", 25, "CONTEXTVAR", "turn", "explicit", 7,
               "Delivery target when the turn answers a scheduled run."),
    StateEntry("_active_signal_context", "agent/tools/runtime.py", 34, "CONTEXTVAR", "turn", "stays", None,
               "Signal cascade depth. Belongs to the scheduler, which Task 10 defers; "
               "converging it before the scheduler becomes a plugin would move state "
               "twice for no gain."),
    StateEntry("_active_run_self_report", "agent/tools/runtime.py", 61, "CONTEXTVAR", "turn", "stays", None,
               "A mutable object rather than a value on purpose: sync tools run under "
               "copy_context(), so a set() inside the tool lands in the copy and the "
               "caller never sees it. Replacing this needs a different mechanism, not "
               "a tidier one."),
    StateEntry("_active_cancel_token", "agent/shared.py", 399, "CONTEXTVAR", "turn", "explicit", 7,
               "Cancellation token for the running turn."),
    StateEntry("_active_session_id", "agent/shared.py", 407, "CONTEXTVAR", "session", "service", 4,
               "Feeds the provider {session} header. Session identity belongs to the "
               "session service, not to a ContextVar."),
    # ── Session-keyed tables (session scope, process storage) ────────────────
    StateEntry("_session_allowlist", "agent/security/shell.py", 231, "CONTAINER", "session", "service", 9,
               "Shell approval allowlist, keyed by (scope, command)."),
    StateEntry("_pending_tokens", "agent/security/shell.py", 232, "CONTAINER", "session", "service", 9,
               "Shell confirmation tokens awaiting a human reply."),
    StateEntry("_pending_session_scope", "agent/security/shell.py", 237, "CONTAINER", "session", "service", 9,
               "'Always allow this command' marks travelling with their token."),
    StateEntry("_session_auto_approve", "agent/security/shell.py", 238, "CONTAINER", "session", "service", 9,
               "Scopes a human opted into auto-approval."),
    StateEntry("_session_permission_levels", "agent/security/shell.py", 239, "CONTAINER", "session", "service", 9,
               "Per-scope shell permission level."),
    StateEntry("_session_sandbox_modes", "agent/security/shell.py", 240, "CONTAINER", "session", "service", 9,
               "Per-scope sandbox mode."),
    StateEntry("_PENDING", "agent/security/plugin_approval.py", 20, "CONTAINER", "session", "service", 5,
               "Plugin installs awaiting human approval, TTL 5 minutes."),
    StateEntry("_APPROVED", "agent/security/plugin_approval.py", 21, "CONTAINER", "session", "service", 5,
               "Plugin installs already approved for a scope."),
    # ── Path constants rewritten wholesale by _set_agent_home ───────────────
    # One `global` rewrite path (shared.py:101-133) plus a second writer in
    # agent/__init__.py:126-135.  Every reader in the process sees the change,
    # which is why two sessions cannot hold different homes today.
    StateEntry("AGENT_HOME", "agent/shared.py", 135, "REBOUND", "process", "service", 9,
               "The root of the whole process layout; see the block note above."),
    StateEntry("MEMORY_DIR", "agent/shared.py", 136, "REBOUND", "process", "service", 9),
    StateEntry("SKILLS_DIR", "agent/shared.py", 137, "REBOUND", "process", "service", 9),
    StateEntry("TOOLS_DIR", "agent/shared.py", 138, "REBOUND", "process", "service", 9),
    StateEntry("PACKAGE_ROOT", "agent/shared.py", 139, "REBOUND", "process", "service", 9,
               "Rebound even though it derives from __file__, not from the home."),
    StateEntry("BUILTIN_SKILLS_DIR", "agent/shared.py", 140, "REBOUND", "process", "service", 9),
    StateEntry("PROMPTS_DIR", "agent/shared.py", 141, "REBOUND", "process", "service", 9),
    StateEntry("RL_DIR", "agent/shared.py", 142, "REBOUND", "process", "service", 9),
    StateEntry("SCHEDULER_DIR", "agent/shared.py", 143, "REBOUND", "process", "service", 9),
    StateEntry("SCHEDULER_DB_FILE", "agent/shared.py", 144, "REBOUND", "process", "service", 9),
    StateEntry("CONFIG_FILE", "agent/shared.py", 145, "REBOUND", "process", "service", 9),
    StateEntry("INDEX_FILE", "agent/shared.py", 146, "REBOUND", "process", "service", 9),
    StateEntry("SESSIONS_FILE", "agent/shared.py", 147, "REBOUND", "process", "service", 9),
    StateEntry("DEFAULT_OUTPUT_DIR", "agent/shared.py", 148, "REBOUND", "process", "service", 9),
    StateEntry("PLUGINS_DIR", "agent/shared.py", 149, "REBOUND", "process", "service", 9),
    StateEntry("USER_PLUGINS_DIR", "agent/shared.py", 150, "REBOUND", "process", "service", 9),
    StateEntry("CONTEXT_DIR", "agent/shared.py", 256, "REBOUND", "process", "service", 9),
    StateEntry("STAGING_DIR", "agent/shared.py", 267, "REBOUND", "process", "service", 9),
    StateEntry("PALACE_DB_FILE", "agent/shared.py", 269, "REBOUND", "process", "service", 9),
    StateEntry("TASKS_DIR", "agent/__init__.py", 123, "REBOUND", "process", "service", 9,
               "A second writer of the same layout; see _set_agent_home in agent/__init__.py."),
    # ── Rebound process-level machinery ─────────────────────────────────────
    StateEntry("_SYNC_TOOL_EXECUTOR", "agent/tools/runtime.py", 79, "REBOUND", "process", "stays", None,
               "A process-wide pool so blocking tools cannot starve the event loop. "
               "Deliberately not the loop's default executor, because channels push "
               "their own blocking calls through that one; a single pool is the point."),
    StateEntry("_PLUGIN_INSTALL_LOCK", "agent/tools/plugin_tools.py", 126, "REBOUND", "process", "service", 5,
               "Serializes plugin installs process-wide."),
    StateEntry("_IN_PROCESS_LOCKS", "agent/tools/files.py", 224, "CONTAINER", "process", "stays", None,
               "Per-path write locks keyed by (root kind, root path, relative path). "
               "The keys are paths, so the table is process-wide by nature; narrowing "
               "it per agent would weaken the lock rather than scope it."),
    StateEntry("_RETRYABLE_CLASSES", "agent/core/llm_errors.py", 42, "REBOUND", "process", "stays", None,
               "The tuple of SDK error classes worth retrying, built once on first "
               "use from whichever SDKs are importable. Process-wide is correct: the "
               "answer depends on installed packages, not on any agent, and asking "
               "per-agent would rebuild the same tuple. Extracted from BaseAgent in "
               "Task 7, where it was a class attribute."),
    StateEntry("_system_prompt_cache_key", "agent/config.py", 1152, "REBOUND", "process", "service", 2,
               "Static system-prompt segment cache; last writer wins across agents."),
    StateEntry("_system_prompt_cache_value", "agent/config.py", 1153, "REBOUND", "process", "service", 2),
    # ── Interactive CLI state ───────────────────────────────────────────────
    # One interactive session per process, so process level by construction
    # rather than by accident.  Converging these would mean inventing a second
    # interactive session, not isolating an existing one.
    StateEntry("_cli_prompt_session", "agent/cli.py", 86, "REBOUND", "process", "stays", None,
               "The process's one interactive prompt session."),
    StateEntry("_cli_router", "agent/cli.py", 167, "REBOUND", "process", "stays", None,
               "The process's one command router; a CLI has exactly one command line."),
    StateEntry("_current_cancel_token", "agent/cli.py", 1039, "REBOUND", "process", "stays", None,
               "Points at the one foreground operation a terminal can have."),
    StateEntry("_sigint_count", "agent/cli.py", 1040, "REBOUND", "process", "stays", None,
               "Ctrl-C arming counter; meaningless outside one terminal."),
    StateEntry("_ACTIVE_TUI", "agent/cli.py", 1142, "REBOUND", "process", "stays", None,
               "The process's one live TUI."),
)


#: Index for the guard test and for lookup by symbol.
BY_SYMBOL: dict[str, StateEntry] = {entry.symbol: entry for entry in LEDGER}


def entries_for_task(task: int) -> tuple[StateEntry, ...]:
    """Entries a task in the refactor plan is expected to converge."""
    return tuple(e for e in LEDGER if e.task == task)


def outstanding() -> tuple[StateEntry, ...]:
    """The ledger's debt list: entries with a convergence plan, not yet done.

    Distinct from a *problem*: every one of these carries the ``reason`` that
    names its target, so the list is work scheduled rather than work forgotten.
    ``test_ledger_records_where_state_is_owned`` is what catches the other
    case -- an entry that neither converges nor justifies itself.
    """
    return tuple(e for e in LEDGER if e.convergence != "stays")


def unjustified() -> tuple[StateEntry, ...]:
    """Entries marked ``stays`` without an argument for keeping them."""
    return tuple(e for e in LEDGER if e.convergence == "stays" and not e.reason)
