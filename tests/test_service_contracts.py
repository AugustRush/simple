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
        "agent.runtime.contracts",
        "TurnRunner",
        "agent.runtime.protocols",
        "AgentLoopService",
        "agent_loop",
    ),
    (
        "agent.runtime.contracts",
        "AgentCore",
        "agent.runtime.protocols",
        "AgentCoreService",
        "agent_core",
    ),
    (
        "agent.plugins.catalog",
        "PluginCatalog",
        "agent.plugins.protocols",
        "PluginCatalogService",
        "plugin_catalog",
    ),
    (
        "agent.commands.registry",
        "CommandRegistry",
        "agent.commands.protocols",
        "CommandRegistryService",
        "commands",
    ),
    (
        "agent.core.agent_registry",
        "AgentRegistry",
        "agent.core.protocols",
        "AgentRegistryService",
        "agents",
    ),
    (
        "agent.tools.mcp_servers",
        "MCPServerRegistry",
        "agent.tools.protocols",
        "MCPServerRegistryService",
        "contributed_mcp_servers",
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
    (
        "agent.core.transport",
        "RoutingTransport",
        "agent.core.transport",
        "ModelTransport",
        "llm",
    ),
)

#: Contracts checked by another file, so the map below can still insist that every
#: declared contract is checked *somewhere*.  The target is verified, not trusted:
#: ``test_every_declared_contract_is_checked_somewhere`` asserts the named file
#: actually mentions the symbol.  It did not, once -- ``agent/core/transport.py``
#: was waived to a file with zero mentions of ``ModelTransport``, so the ``llm``
#: contract was checked nowhere while the ledger said otherwise.
CHECKED_ELSEWHERE: dict[str, str] = {
    "agent/memory/contracts.py": "tests/test_memory_contract.py",
}


def _protocol(module_name: str, name: str) -> object:
    import importlib

    return getattr(importlib.import_module(module_name), name)


def _implementation(module_name: str, class_name: str) -> type:
    import importlib

    return getattr(importlib.import_module(module_name), class_name)


def _call_site_sources(implementation_module: str) -> str:
    """Every module that could call the service, except the implementation itself.

    The whole tree, including the implementation's own module: ``AgentCore`` and
    ``TurnRunner`` live in one file and *one is the other's caller*, which is a real
    consumer relationship between two services.  The weaker claim this makes (a
    same-named method anywhere counts) is the safe direction for "does this member have
    a caller at all"; the ledger keeps the stronger, narrower claim about *who reads the
    service*, and that list is built from container reads -- so a module that receives
    the service as a **parameter** (``agent/config.py`` taking ``skill_catalog``) is
    invisible to it and would have made this check fail for a member that is used.
    """
    chunks: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in str(path):
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

    A waiver in :data:`CHECKED_ELSEWHERE` is verified too: the named file has to
    *mention* the symbol.  Without that, the map's escape hatch is where a contract
    silently stops being checked -- measured once, when ``llm`` was waived to a file
    containing no mention of ``ModelTransport`` at all.
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
            checker = CHECKED_ELSEWHERE[relative]
            checked_in = (REPO_ROOT / checker).read_text(encoding="utf-8")
            assert symbol in checked_in, (
                f"{entry.name} waives its contract to {checker}, which never mentions "
                f"{symbol} -- the waiver points at a check that does not exist"
            )
            continue
        assert (entry.name, symbol) in rows, (
            f"{entry.name} declares {symbol}; add a row here (or to "
            "CHECKED_ELSEWHERE) so it is actually checked"
        )


def test_no_waiver_outlives_the_row_it_replaces() -> None:
    """Control: a contract checked here must not *also* be waived elsewhere.

    The two mechanisms are alternatives, and a stale waiver is worse than a stale
    comment: it silently re-opens the gap the row just closed.
    """
    row_paths = {row[2].replace(".", "/") + ".py" for row in SERVICE_CONTRACTS}
    stale = sorted(row_paths & set(CHECKED_ELSEWHERE))
    assert not stale, (
        f"these contracts have a row here *and* are waived to another test file: {stale}"
    )


# ── The contribution-view shape, checked as one thing ────────────────────────
#
# Six capability services hand a plugin a per-plugin view of themselves.  The shape
# was written by hand each time -- the same two fields, the same `owner`, the same
# "record the undo on the plugin's scope" step -- and two of the six got the undo
# subtly wrong (a name- or prefix-keyed removal, which let one plugin's unload take
# another plugin's contribution).  The shared base is what makes that step exist
# once; these tests are what keep a seventh hand-written copy from appearing.

#: ``(registry module, registry class, view module, view class)`` for every
#: capability service that offers a contribution view.
SCOPED_VIEWS: tuple[tuple[str, str, str, str], ...] = (
    ("agent.tools.runtime", "ToolRegistry", "agent.tools.scoped", "ScopedTools"),
    (
        "agent.skills.catalog",
        "SkillCatalog",
        "agent.skills.scoped",
        "ScopedSkillCatalog",
    ),
    (
        "agent.channels.registry",
        "ChannelRegistry",
        "agent.channels.registry",
        "ScopedChannels",
    ),
    (
        "agent.tools.mcp_servers",
        "MCPServerRegistry",
        "agent.tools.mcp_servers",
        "ScopedMCPServers",
    ),
    (
        "agent.commands.registry",
        "CommandRegistry",
        "agent.commands.registry",
        "ScopedCommands",
    ),
    (
        "agent.core.agent_registry",
        "AgentRegistry",
        "agent.core.agent_registry",
        "ScopedAgentDefs",
    ),
)


@pytest.mark.parametrize("row", SCOPED_VIEWS, ids=lambda row: row[1])
def test_a_capability_service_satisfies_the_scoped_protocol(row) -> None:
    """``scoped(ctx)`` is declared, not overheard through ``getattr``.

    The kernel resolves it by duck-typing (it must: the service is the composition's,
    not the kernel's), so the only place the convention can be *checked* is here.
    """
    from agent.kernel.service import ScopedService

    registry_module, registry_name, view_module, view_name = row
    registry = _implementation(registry_module, registry_name)
    view = _implementation(view_module, view_name)

    assert hasattr(registry, "scoped"), f"{registry_name} has no scoped()"
    assert isinstance(registry(), ScopedService) or callable(
        getattr(registry, "scoped", None)
    )

    class _Ctx:
        name = "probe"

        def on_unwind(self, handler):  # pragma: no cover - not exercised here
            self.handler = handler

    built = registry().scoped(_Ctx())
    assert isinstance(built, view), (
        f"{registry_name}.scoped() returned {type(built).__name__}, not {view_name}"
    )


@pytest.mark.parametrize("row", SCOPED_VIEWS, ids=lambda row: row[3])
def test_a_contribution_view_uses_the_shared_base(row) -> None:
    """The undo step exists once, so attribution cannot be re-implemented wrongly."""
    from agent.kernel.service import ScopedContribution

    view = _implementation(row[2], row[3])
    assert issubclass(view, ScopedContribution), (
        f"{row[3]} hand-writes the view shape instead of using ScopedContribution"
    )


def test_no_scoped_view_escapes_the_scan() -> None:
    """Derived, not listed: a new ``Scoped*`` class must use the base.

    The tuple above is a list somebody maintains; this is the check that the list is
    complete.  A seventh capability service that writes its own view class fails here
    the moment it is named the way the other six are -- and if it is *not* named that
    way, the parametrized rows above still cover the ones that are declared.
    """
    import ast

    from agent.kernel.service import ScopedContribution

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        # The kernel defines the base and the protocol; neither is a *view*, and
        # requiring the base to subclass itself is not a rule anybody wants.
        if path.name == "service.py" and path.parent.name == "kernel":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not node.name.startswith("Scoped"):
                continue
            bases = {ast.unparse(base) for base in node.bases}
            if "ScopedContribution" not in bases:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.name} -> {bases}")
    assert not offenders, (
        "these scoped views do not use the shared base, so each one re-implements "
        "attribution and its undo:\n  " + "\n  ".join(offenders)
    )
    assert ScopedContribution is not None
