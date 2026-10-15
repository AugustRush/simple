"""Command handlers resolve services through the container, not the raw mapping.

``_component(context, name)`` was ``context.components.get(name)``: a plain dict
lookup by string key.  It worked, and the reason it is worth changing is the same
one that made the scheduler's twenty-two ``SchedulerStore`` constructions worth
changing -- a lookup nothing declares is a lookup nobody can replace.  A command
handler that wants a different ``skill_catalog`` has no name to install one under.

The commands are a good first consumer because they already had **one** entry
point, so the migration is one function rather than twenty-nine call sites.

The snapshot semantic is preserved, not fixed
---------------------------------------------
``CommandContext.__post_init__`` wraps the mapping in ``MappingProxyType(dict(...))``
-- a *copy*, not a view.  So a service published after the context was built is
invisible to that context.  Whether that is desirable is a separate question, and
changing it here would silently alter what every command sees; these tests pin the
existing behaviour so the migration cannot be the thing that changes it.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from agent.commands.models import CommandContext
from agent.runtime.contracts import RuntimeComponents
from agent.kernel.service import ServiceRegistry


def _context(**components):
    return CommandContext(
        components=components,
        config={},
        session_state=None,
        sink=None,
    )


# ── The context exposes a container ──────────────────────────────────────────


def test_the_context_has_a_container() -> None:
    ctx = _context(skill_catalog="SKILLS")
    assert isinstance(ctx.services, ServiceRegistry)


def test_the_container_resolves_what_the_mapping_holds() -> None:
    ctx = _context(skill_catalog="SKILLS", agent_core="CORE")
    assert ctx.services.get("skill_catalog") == "SKILLS"
    assert ctx.services.get("agent_core") == "CORE"


def test_a_service_the_mapping_lacks_is_missing_not_none() -> None:
    """The point of resolving through the registry: absence is loud.

    ``.get`` on the mapping returned ``None``, which for a service is
    indistinguishable from a composition that legitimately has none.
    """
    from agent.kernel.service import MissingServiceError

    ctx = _context(skill_catalog="SKILLS")
    with pytest.raises(MissingServiceError):
        ctx.services.get("agent_core")


def test_the_helper_still_returns_none_for_an_absent_service() -> None:
    """Preserved: twelve call sites branch on a falsy result.

    ``_component`` keeps its forgiving shape; the container is what makes the
    absence *available* to a caller that wants it.
    """
    from agent.commands.builtin import _component

    ctx = _context(skill_catalog="SKILLS")
    assert _component(ctx, "agent_core") is None
    assert _component(ctx, "skill_catalog") == "SKILLS"


def test_the_helper_resolves_through_the_container() -> None:
    """Asserted at the source, because the behaviour is identical either way.

    A helper that quietly went back to ``components.get`` would pass every
    functional test in this file and defeat the migration.
    """
    import inspect

    from agent.commands import builtin

    source = inspect.getsource(builtin._component)
    assert "services" in source, (
        "_component must resolve through the container; see "
        "agent/runtime/services.py for why"
    )


# ── The snapshot semantic is preserved ───────────────────────────────────────


def test_the_context_takes_a_snapshot_not_a_view() -> None:
    """A service published after construction is invisible to that context.

    Recorded rather than changed: whether a command should see a later
    publication is a design question about command dispatch, and answering it
    inside a resolution refactor would alter every command's behaviour at once.
    """
    live = {"skill_catalog": "BEFORE"}
    ctx = CommandContext(components=live, config={}, session_state=None, sink=None)
    live["skill_catalog"] = "AFTER"
    assert ctx.services.get("skill_catalog") == "BEFORE"
    assert ctx.components.get("skill_catalog") == "BEFORE", "both agree"


def test_a_service_added_after_construction_is_invisible() -> None:
    live = {"skill_catalog": "S"}
    ctx = CommandContext(components=live, config={}, session_state=None, sink=None)
    live["plugin_catalog"] = "LATE"
    assert ctx.services.get("plugin_catalog", default=None) is None


def test_the_mapping_stays_read_only() -> None:
    """The container must not become a way to write into a command's dependencies."""
    ctx = _context(skill_catalog="SKILLS")
    assert isinstance(ctx.components, MappingProxyType)
    with pytest.raises(TypeError):
        ctx.components["new"] = 1  # type: ignore[index]


def test_the_container_is_built_once_per_context() -> None:
    """A command runs many lookups; rebuilding the registry per call is waste."""
    ctx = _context(skill_catalog="SKILLS")
    assert ctx.services is ctx.services


# ── Migration: no command reads the raw mapping for a service ────────────────


CORE_NAMES = (
    "agent_core",
    "context_manager",
    "memory",
    "registry",
    "skill_catalog",
    "plugin_catalog",
)


def test_no_command_reads_a_core_service_from_the_raw_mapping() -> None:
    """A direct ``components.get("skill_catalog")`` bypasses the container.

    Scanned because the two are functionally identical today: a bypass is
    invisible at runtime and would quietly undo the migration one call site at a
    time.
    """
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "agent/commands/builtin.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else ""
        if attr != "get" or not node.args:
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and key.value in CORE_NAMES):
            continue
        # Only flag a lookup whose receiver reaches the components mapping.
        receiver = ast.unparse(func.value)
        if "components" in receiver:
            offenders.append(f"L{node.lineno}: {ast.unparse(node)}")
    assert not offenders, (
        "these read a core service straight from the components mapping:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse _component(context, name), which resolves through the container."
    )


def test_the_components_mapping_is_not_the_only_channel() -> None:
    """``CommandContext`` must expose the container, not just the mapping.

    The guard for the goal's clause (d): a consumer that can only reach the
    mapping makes "replaceable" untrue regardless of what else is declared.
    """
    assert hasattr(CommandContext, "services")


def test_the_container_is_not_a_second_copy_of_the_mapping() -> None:
    """It adopts the same storage, so the two cannot disagree.

    Asserted through ``RuntimeComponents``, which is where the adoption lives; a
    context that built an independent registry could drift from the mapping that
    the rest of the runtime reads.
    """
    components = {"skill_catalog": "S"}
    rc = RuntimeComponents(components)
    assert rc.services.get("skill_catalog") == components["skill_catalog"]
    rc.services.provide("skill_catalog", "S2", replace=True)
    assert components["skill_catalog"] == "S2", "one storage, not two"
