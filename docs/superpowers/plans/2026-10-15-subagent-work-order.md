# Subagent work order: the remaining god-class extractions

This is the supervision contract for delegating the remaining work described in
[the remaining-big-files survey](2026-10-15-remaining-big-files-survey.md).  It exists because the
rules below were each paid for with a failed run during the first 24 extractions, and a delegated
agent does not inherit that memory unless it is written down.

## Division of labour

| Phase | Who | May write? | May run pytest? | May commit? |
|---|---|---|---|---|
| Measurement | measurement subagents | **no** | **no** | no |
| Implementation | one implementation subagent at a time | yes, in its own cluster's files | targeted files only | **no** |
| Verification | the supervisor | only fixes | **yes, alone** | **yes** |

Two hard constraints, both learned here:

* **Never two pytest runs at once.**  A second run contends on the agent home's `.owner.lock`
  (`agent/runtime/lock.py`) and presents as a *hang*, not a failure -- one run of mine sat for
  fifteen minutes before I recognised it.  Check `pgrep -f pytest` before starting one.
* **Never run a script from `/tmp`.**  `sys.path[0]` becomes `/tmp`, and a stray
  `/private/tmp/inspect.py` there shadows the stdlib `inspect` (which `ast.get_docstring`
  imports), producing a confusing numpy error.  Keep scratch scripts inside the repository and
  delete them.

## Acceptance criterion

The FAILED **set** must equal the recorded baseline byte-for-byte -- never a count, and never
across policies.  Under `danger-full-access` the baseline is the three console-width probes
listed in [the baseline record](2026-10-01-baseline-failures.txt):

```
tests/test_cli_ui.py::test_console_width_check_is_throttled
tests/test_cli_ui.py::test_console_width_follows_terminal_resize
tests/test_cli_ui.py::test_console_width_survives_a_failing_terminal_probe
```

## Rules for a moving change

Each of these is a bug that actually happened while doing this work.

1. **The receiver parameter is dropped structurally.**  Split the parameter list and drop the
   entry named `self`/`cls`.  A comma-shaped regex misses `def f(self)` -- one parameter, no
   comma -- and yields `(conn, self)`.  `@staticmethod` has no receiver to drop; a body using
   `cls.sibling()` is coupled even though it never says `self`.
2. **Rewriting call sites must exclude definition sites.**  A pattern over `name(` matches
   `def name(` too, and inserts a second argument: `def _ensure_schema(conn, conn)`.
3. **A moved sibling's new first parameter must be passed at its call sites.**  Changing
   `self.helper(` to `helper(` is half the change when `helper` now takes `conn`.  Apply a
   pattern over *all* call sites and grep for leftovers; do not work from a remembered list --
   two table names I "remembered" were wrong.
4. **The closure is more than the receiver.**  A moved body also reads module-level names:
   constants (move one with its only reader -- a constant read by nobody else belongs with the
   code that reads it) and imports (re-create them, **respecting `ImportFrom.level`**: `from
   .models import X`, never `from models import X` or `from None import rows`).
5. **Run the unbound-name check before the tests.**  It finds rule 4's misses in a second; a
   test run finds them one failure at a time.  The checker must know that `vararg`/`kwarg` bind
   names, that comprehension targets bind, and that a nested function closing over an enclosing
   local is not unbound.
6. **A wrapper keeps the original signature verbatim** (including `self`) and delegates: the
   public API of the class must not change, so no call site and no test is edited for that
   reason.  Reusing the receiver-stripped parameter list for both the function and the wrapper
   drops `self` from the wrapper and turns every positional call into an arity error.
7. **Import the new module into the old one** (`from . import usage`).  The moving script does
   not do this; forgetting it fails at call time, not at import time.
8. **Tests are consumers of the class, and they move with the name.**  If a test monkeypatches a
   method (`monkeypatch.setattr(store, "_step_task_spec", ...)`) the patch target becomes the
   module where the call now resolves.  If a test patches a module-level constant, the same
   applies.  If a test calls the method as a class attribute (`SchedulerStore._identity_values`),
   it points at the new module.  Report every such edit.
9. **Byte-identical, or say why not.**  Compare the moved body against the original with a diff
   and confirm the *only* differences are the intended ones (name, receiver, imports, sibling
   calls).  My first attempt at this verification was itself wrong -- it joined `splitlines()`
   with `""` and reported the whole function as one changed line.

## Report format for an implementation subagent

Concise, no code dumps:

* files changed, with before/after line counts
* the exact transformations applied
* the unbound-name check result
* the moved-body diff summary (how many hunks, and that each is intended)
* the targeted test command and its result
* every test file edited and why
* anything unresolved, stated as a question rather than a guess
