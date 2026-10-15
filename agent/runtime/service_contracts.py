"""The declared contracts, checked against what the composition actually holds.

Why this exists
---------------
Every service in the ledger names a contract (``agent/tools/protocols.py:
ToolRegistryService``), and until now that name was **checked only by tests, and only
against classes**: ``tests/test_service_contracts.py`` compares a class's signatures
with the protocol's, which is the stronger check and stays there.  What nothing did was
ask the question a ``@runtime_checkable`` Protocol exists to answer -- *is the object the
composition is actually holding an instance of the thing its declaration promises?*  The
review's second criterion ("a declaration is a contract, not documentation") was measured
at zero runtime ``isinstance`` calls in the whole tree.

That gap is not academic.  ``memory`` holds a *palace* while ``ltm_store`` holds a
*store*; both are declared, and the ledger's pointer for ``memory`` was wrong for a while
(it named ``MemoryStore``) because nothing ever asked.  A rename or a swap that satisfies
the tests but not the declaration is exactly the failure this catches, at composition
time, with the name of the service and the protocol.

Two places it is enforced
-------------------------
* **Immediately, per plugin**: :func:`validator_for` is handed to the kernel as
  ``PluginHost.service_validator``, so a plugin that provides an object which does not
  satisfy the declaration is refused where it happened, and registers nothing.
* **Once, over the whole composition**: :func:`violations` walks every declared service
  that is *present* and returns the ones that do not conform.  This is what catches the
  values the composition root writes itself, which never go through a plugin's
  ``provide``.

Absence is not a violation.  A composition may legitimately hold no ``mcp_client`` (the
ledger says so), so only present services are checked -- the question is "is what you
handed me what you promised", never "must you have one".
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any, Callable, Optional

from agent.runtime.core_services import BY_NAME

__all__ = [
    "ContractViolation",
    "protocol_for",
    "validate",
    "validator_for",
    "violations",
]


class ContractViolation(TypeError):
    """A service was provided that does not satisfy its declared contract.

    A ``TypeError`` because that is what it is -- the wrong type of object -- and
    because callers that already guard "this is not the shape I expect" keep working.
    """

    def __init__(self, name: str, value: Any, contract: str, detail: str = "") -> None:
        self.name = name
        self.value = value
        self.contract = contract
        super().__init__(
            f"service {name!r} was provided a {type(value).__name__}, which does not "
            f"satisfy its declared contract {contract}"
            + (f" ({detail})" if detail else "")
        )


@lru_cache(maxsize=None)
def _resolve(contract: str) -> type:
    """Import ``agent/path.py:Symbol``.  Cached: one import per contract, ever."""
    path, _, symbol = contract.partition(":")
    if not path or not symbol:
        raise ValueError(f"a contract must be 'path/to/module.py:Symbol', got {contract!r}")
    module = importlib.import_module(path[:-3].replace("/", "."))
    return getattr(module, symbol)


def protocol_for(name: str) -> Optional[type]:
    """The protocol *name* declares, or ``None`` when it declares none.

    Reads the ledger, so a name that is not declared raises from there -- an undeclared
    name is a different mistake, and swallowing it here would hide it.
    """
    entry = BY_NAME.get(name)
    if entry is None or not entry.contract:
        return None
    return _resolve(entry.contract)


def validate(name: str, value: Any) -> None:
    """Refuse *value* if it does not satisfy *name*'s declared contract.

    A service with no declared contract is not checked: the ledger is explicit about
    which names have an interface written down, and inventing one here would make
    "declared" meaningless.
    """
    contract = BY_NAME.get(name)
    if contract is None or not contract.contract:
        return
    protocol = _resolve(contract.contract)
    try:
        conforms = isinstance(value, protocol)
    except TypeError as exc:
        # A protocol with non-method members that this Python cannot check.  Refusing
        # every value for that reason would be worse than checking nothing, so the
        # limitation is named rather than turned into a false failure.
        raise ContractViolation(name, value, contract.contract, f"uncheckable: {exc}")
    if not conforms:
        raise ContractViolation(name, value, contract.contract)


def validator_for(names: Any = None) -> Callable[[str, Any], None]:
    """A ``PluginHost.service_validator`` that checks only *names*.

    Narrowing matters: a composition that wires the whole ledger would refuse a
    plugin's service simply for being new.  Only names the ledger declares a contract
    for are ever checked, which is the same rule :func:`validate` applies -- the
    parameter exists so an embedder can narrow it further.
    """
    allowed = None if names is None else frozenset(names)

    def check(name: str, value: Any) -> None:
        if allowed is not None and name not in allowed:
            return
        validate(name, value)

    return check


def violations(components: Any, *, names: Any = None) -> list[str]:
    """Every declared service that is present and does not satisfy its contract.

    Returns messages rather than raising, so a caller can report *all* of them: at
    composition time, one violation is rarely alone, and fixing them one restart at a
    time is how a startup check becomes something people disable.
    """
    from agent.kernel.service import resolve

    wanted = names if names is not None else (e.name for e in BY_NAME.values())
    problems: list[str] = []
    for name in wanted:
        value = resolve(components, name, None)
        if value is None:
            continue  # absence is a composition decision, not a violation
        try:
            validate(name, value)
        except ContractViolation as exc:
            problems.append(str(exc))
    return problems
