"""Every service that declares a contract, checked the same way.

``tests/test_memory_contract.py`` owns the memory services, because those also have
a behavioural suite (``run_store_contract`` / ``run_context_contract``) and a second
implementation each.  This file covers the services whose contract is, so far, only
an interface.  Adding one is a row in :data:`SERVICE_CONTRACTS`, and the guards below
are the same three the memory services get:

1. **conformance** -- the implementation has every declared member, with the same
   parameter names and at-least-as-permissive defaults, and the same async-ness.
   ``isinstance`` alone checks names, and a renamed parameter breaks a caller that
   passes it by keyword;
2. **attribute closure** -- a declared *data* member is part of the interface too;
3. **no speculative members** -- a member nobody calls is a guess, so each one has to
   be reached from one of the ledger's declared consumers.  That also keeps the
   contract and the ledger from drifting apart: the protocol says what the service
   offers, the ledger says who uses it, and this asserts the second uses the first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_memory_contract import (
    _assert_conforms,
    _concrete_data_members,
    _protocol_attributes,
    _protocol_members,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``(implementation module, class, protocol module, protocol name, service name)``.
#: Modules are import paths; the service name is the ledger's, and its declared
#: consumers are the files the "every member has a caller" check reads.
SERVICE_CONTRACTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "agent.tools.runtime",
        "MCPClient",
        "agent.tools.protocols",
        "MCPClientService",
        "mcp_client",
    ),
    (
        "agent.channels.registry",
        "ChannelRegistry",
        "agent.channels.protocols",
        "ChannelRegistryService",
        "channels",
    ),
    (
        "agent.tools.runtime",
        "ToolRegistry",
        "agent.tools.protocols",
        "ToolRegistryService",
        "registry",
    ),
    (
        "agent.skills.catalog",
        "SkillCatalog",
        "agent.skills.protocols",
        "SkillCatalogService",
        "skill_catalog",
    ),
    (
        "agent.core.agent",
        "BaseAgent",
        "agent.core.protocols",
        "AgentService",
        "agent",
    ),
)

#: Contracts checked by another file, so the map below can still insist that every
#: declared contract is checked *somewhere*.
CHECKED_ELSEWHERE: dict[str, str] = {
    "agent/memory/contracts.py": "tests/test_memory_contract.py",
    "agent/core/transport.py": "tests/test_model_plugin.py",
}


def _protocol(module_name: str, name: str) -> object:
    import importlib

    return getattr(importlib.import_module(module_name), name)


def _implementation(module_name: str, class_name: str) -> type:
    import importlib

    return getattr(importlib.import_module(module_name), class_name)


def _call_site_sources(implementation_module: str) -> str:
    """Every module that could call the service, except the implementation itself.

    The whole tree rather than the ledger's consumer list: that list is built from
    *container reads* (``resolve``/``ctx.get``/...), so a module that receives the
    service as a **parameter** -- ``agent/config.py`` taking ``skill_catalog`` -- is
    invisible to it.  Scanning for callers is the weaker claim (a same-named method on
    another object counts), which is the safe direction for "does this member have a
    caller at all"; the ledger keeps the stronger, narrower claim about *who reads the
    service*.
    """
    chunks: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if "__pycache__" in str(relative):
            continue
        if str(relative) == f"{implementation_module.replace('.', '/')}.py":
            continue
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


@pytest.mark.parametrize("row", SERVICE_CONTRACTS, ids=lambda row: row[1])
def test_the_implementation_satisfies_its_protocol(row) -> None:
    module_name, class_name, protocol_module, protocol_name, _service = row
    implementation = _implementation(module_name, class_name)

    _assert_conforms(_protocol(protocol_module, protocol_name), implementation, class_name)


@pytest.mark.parametrize("row", SERVICE_CONTRACTS, ids=lambda row: row[1])
def test_every_declared_member_has_a_caller(row) -> None:
    """A member nobody calls is a guess, and the ledger names the callers.

    A protocol derived from call sites must not grow a member "for completeness": the
    surface this exercise exists to shrink is exactly that.
    """
    module_name, _class, protocol_module, protocol_name, _service = row
    protocol = _protocol(protocol_module, protocol_name)
    sources = _call_site_sources(module_name)

    def called(name: str) -> bool:
        # ``service.name(...)`` or the kernel's duck-typed ``getattr(service, "name")``
        # -- the latter is how the capability views are reached (``ctx.tools``).
        return bool(
            re.search(rf"\.{re.escape(name)}\s*\(", sources)
            or re.search("getattr\\([^)]*[\"']" + re.escape(name) + "[\"']", sources)
        )

    unused = [name for name in _protocol_members(protocol) if not called(name)]
    assert not unused, (
        f"{protocol_name} declares members nothing calls: {unused}"
    )
    attributed = [
        name
        for name in _protocol_attributes(protocol)
        if not re.search(rf"\.{re.escape(name)}\b", sources)
    ]
    assert not attributed, (
        f"{protocol_name} declares data nothing reads: {attributed}"
    )


@pytest.mark.parametrize("row", SERVICE_CONTRACTS, ids=lambda row: row[1])
def test_the_protocol_is_not_vacuous(row) -> None:
    """Control: an empty protocol would pass everything above."""
    _module, _class, protocol_module, protocol_name, _service = row
    protocol = _protocol(protocol_module, protocol_name)

    assert _protocol_members(protocol) or _protocol_attributes(protocol)


def test_every_declared_contract_is_checked_somewhere() -> None:
    """The ledger's ``contract`` column is the map, so every entry must be verified.

    A pointer at a protocol nobody checks is a claim, not a contract -- and adding a
    contract without a check is exactly the drift this file exists to notice.
    """
    from agent.runtime.core_services import CORE_SERVICES

    rows = {(row[4], row[3]) for row in SERVICE_CONTRACTS}
    for entry in CORE_SERVICES:
        if not entry.contract:
            continue
        relative, _, symbol = entry.contract.partition(":")
        assert (REPO_ROOT / relative).is_file(), (
            f"{entry.name} points at a missing file: {relative}"
        )
        if relative in CHECKED_ELSEWHERE:
            continue
        assert (entry.name, symbol) in rows, (
            f"{entry.name} declares {symbol}; add a row here (or to "
            "CHECKED_ELSEWHERE) so it is actually checked"
        )
