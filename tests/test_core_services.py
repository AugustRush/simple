"""The core service vocabulary, checked against the source.

The container already held these objects -- ``RuntimeComponents.services`` adopts
the component mapping -- so this is a *declaration* of which names are services,
not a new mechanism.  What it buys is that "replaceable" becomes a checkable
claim: a name with an owner, consumers, and a guard that fails when either list
drifts from the code.

The owner/consumer lists are asserted against an AST scan rather than trusted.
That is deliberate: hand-written ledgers in this refactor have been wrong twice,
once missing five entries entirely, and a ledger that disagrees with the code is
worse than none because it reports a service as filled when nothing fills it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.runtime.core_services import (
    CORE_SERVICES,
    CoreService,
    core_service_names,
    entry_for,
)
from agent.runtime.contracts import RuntimeComponents
from agent.runtime.services import MissingServiceError

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"


def _is_metadata_receiver(node: ast.AST) -> bool:
    """Whether *node* is a per-turn metadata mapping rather than a component one.

    The runtime keeps ``ctx.metadata`` / ``ctx_metadata`` carrying some of the same
    names as the services -- ``skill_catalog``, ``context_manager`` -- so a scan
    that treats any matching key as a service read reports files that never resolve
    a service at all.  Written once and used by both access shapes (subscript and
    ``.get``) because the first version of this exclusion covered only subscripts
    and left seven false positives behind.
    """
    if isinstance(node, ast.Call):
        # ``ctx.metadata.get`` -> look at ``ctx.metadata``.
        node = node.func.value if isinstance(node.func, ast.Attribute) else node.func
    # Unparse the node *itself*: for an attribute that yields the full dotted name
    # (``ctx.metadata``), where unparsing ``.value`` would yield only the root
    # (``ctx``) and silently miss the name being looked for.
    return "metadata" in ast.unparse(node)


def _string_key(node: ast.Call | ast.Subscript) -> str | None:
    """The literal string key of a ``.get("k")`` / ``["k"]`` expression."""
    if isinstance(node, ast.Call):
        if not node.args:
            return None
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return None


def _component_key(node: ast.Call) -> str | None:
    """The service name in ``_component(context, "name")``.

    The name is the **second** argument -- the first is the context -- so
    :func:`_string_key` returns ``None`` for these and a scanner that used it
    reported the commands package as reading nothing at all.
    """
    for arg in node.args[1:]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _is_event_bus_table(node: ast.Dict) -> bool:
    """Whether a dict literal maps names to event **buses** rather than services.

    The domain table keeps ``{"turn": TURN_EVENTS, "llm": LLM_EVENTS, ...}``, and
    a service named after a domain -- ``llm`` is now one -- makes that literal look
    like a component mapping.  It is not: it says which bus a domain publishes to.

    Recognised by its values, all of which are ``*_EVENTS`` bus references.  That
    is a project-wide naming convention (``TOOL_EVENTS``, ``TURN_EVENTS``,
    ``LLM_EVENTS``) rather than a guess about the variable's own name, and a
    component mapping never has that shape -- its values are clients, registries and
    managers.

    Without this the ledger would have to list ``event_domains.py`` as a *writer* of
    the ``llm`` service, which is a ledger that lies about the code.
    """
    values = [value for value in node.values]
    if not values:
        return False
    return all(
        isinstance(value, ast.Attribute) and value.attr.endswith("_EVENTS")
        for value in values
    )


def _scan() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(writers, readers)`` per core service name, by file.

    Four write mechanisms are counted, because the source uses all four and a
    scanner that knew fewer reported services as never written:

    - ``mapping["name"] = value`` -- the obvious one;
    - a ``{"name": value}`` **dict literal**, which is how bootstrap builds the
      component mapping it hands over;
    - ``registry.set_context("name", value)`` -- the tool registry's own key-value
      seam, which is a different mechanism carrying the same names;
    - ``ctx.provide("name", value)`` -- **how a plugin offers a service**, and the
      one mechanism this scanner was blind to.  It is the newest and now the
      primary path, so a ledger built without it reported every plugin-provided
      service as written by nothing.  A blind spot here is worse than a missing
      entry: it makes the guard insist that a *correct* tree is wrong.

    A read is any ``.get`` or subscript with the name as a literal key.
    """
    names = set(core_service_names())
    writers: dict[str, set[str]] = {}
    readers: dict[str, set[str]] = {}

    def note(bucket: dict[str, set[str]], key: str | None, rel: str) -> None:
        if key in names:
            bucket.setdefault(key, set()).add(rel)

    for path in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assignment_targets = _is_assignment_target(tree)
        for node in ast.walk(tree):
            # **Every** dict literal, anywhere -- not only ones bound by an
            # assignment.  Bootstrap delivers the component mapping from a
            # ``return {...}``, and a scanner that looked only at assignments
            # missed ``memory`` entirely while reporting the ledger as correct,
            # because the guard and the ledger shared the same blind spot.
            if isinstance(node, ast.Dict):
                if not _is_event_bus_table(node):
                    for key_node in node.keys:
                        if isinstance(key_node, ast.Constant):
                            note(writers, key_node.value, rel)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        note(writers, _string_key(target), rel)
            elif isinstance(node, ast.Call):
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else ""
                name_id = func.id if isinstance(func, ast.Name) else ""
                if attr == "set_context":
                    note(writers, _string_key(node), rel)
                elif attr == "provide":
                    # ``ctx.provide("x", v)`` / ``registry.provide("x", v)``.  The
                    # name is the first argument, which is the same position
                    # ``_string_key`` already reads -- ``provide`` is not the mirror
                    # of ``get`` in that respect, and a second helper for it would
                    # have been a duplicate carrying a wrong explanation.
                    note(writers, _string_key(node), rel)
                elif attr in ("get", "pop", "require", "lookup", "resolve"):
                    # ``lookup``/``resolve`` tolerate absence, ``require`` does
                    # not; all are container-mediated reads, which is the property
                    # being checked.  A metadata mapping is excluded because it
                    # carries some of the same names without being a component
                    # mapping -- see :func:`_is_metadata_receiver`.
                    if not _is_metadata_receiver(func.value):
                        note(readers, _string_key(node), rel)
                elif attr == "_core":
                    # ``self._core("x")`` on the plugin catalog: an attribute
                    # call, so it never reaches the bare-name branch below.
                    note(readers, _string_key(node), rel)
                elif name_id in (
                    "_component",
                    "resolve",
                    "require_service",
                ):
                    # The shared accessors -- ``resolve(components, "x")`` and
                    # ``require_service(components, "x")`` -- plus the two local
                    # ones: ``_component(context, "x")`` in the commands package and
                    # ``self._core("x")`` on the plugin catalog.  They exist so a
                    # caller *states* whether absence is tolerated rather than
                    # implying it through the choice of dict accessor.  Each takes
                    # the container first, so the key is the argument after it.
                    note(readers, _component_key(node), rel)
            elif isinstance(node, ast.Subscript):
                # Two exclusions, both learned from false positives:
                #
                # * a subscript that is an assignment *target* is a write, and
                #   counting it as a read too made ``ctx.metadata["skill_catalog"]
                #   = catalog`` look like a consumer of the service;
                # * a ``*metadata*`` mapping is not a component mapping.  The
                #   runtime keeps a per-turn metadata dict carrying some of the
                #   same names, and treating it as a service read put files in the
                #   ledger that never resolve a service at all.
                if id(node) in assignment_targets:
                    continue
                if _string_key(node) is None:
                    continue
                if _is_metadata_receiver(node.value):
                    continue
                note(readers, _string_key(node), rel)
    return writers, readers


def _is_assignment_target(tree: ast.AST) -> set[int]:
    """Line numbers of subscript nodes that are assignment targets."""
    targets: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    targets.add(id(target))
    return targets


# ── The ledger is well formed ────────────────────────────────────────────────


def test_every_entry_has_a_name_owners_and_summary() -> None:
    for entry in CORE_SERVICES:
        assert entry.name.strip()
        assert entry.owners, f"{entry.name}: a service nothing writes is not one"
        for owner in entry.owners:
            assert owner.startswith("agent/"), f"{entry.name}: owner must be a module"
        assert entry.summary.strip(), f"{entry.name}: a service with no purpose untold"


def test_no_name_is_declared_twice() -> None:
    names = core_service_names()
    assert len(names) == len(set(names))


def test_every_entry_has_a_consumer() -> None:
    """A declared service nothing reads is vocabulary that should be deleted."""
    for entry in CORE_SERVICES:
        assert entry.consumers, f"{entry.name!r} is declared but nothing reads it"


def test_lookup_of_an_undeclared_name_raises() -> None:
    with pytest.raises(KeyError) as caught:
        entry_for("not_a_service")
    assert "CORE_SERVICES" in str(caught.value), "it says where to add it"


def test_lookup_of_a_declared_name_returns_it() -> None:
    assert entry_for("agent_core").name == "agent_core"


# ── The container already resolves them ──────────────────────────────────────


def test_the_container_resolves_a_declared_service() -> None:
    """Not new behaviour: ``services`` adopts the mapping, so this always worked.

    Asserted because the whole point of the declaration is that these names are
    *already* resolvable through the registry -- if that were false, this ledger
    would be describing a migration nobody had done.
    """
    components = {"agent_core": object()}
    assert RuntimeComponents(components).services.get("agent_core") is not None


def test_the_container_raises_for_a_missing_service() -> None:
    """What makes a typo visible: ``None`` is a legitimate value for a composition."""
    with pytest.raises(MissingServiceError):
        RuntimeComponents({}).services.get("agent_core")


def test_require_names_the_missing_service() -> None:
    with pytest.raises(KeyError, match="agent_core"):
        RuntimeComponents({"other": 1}).require("agent_core")


def test_providing_through_the_container_reaches_the_mapping() -> None:
    """So a replacement installed through the registry is visible to both sides."""
    components: dict = {}
    services = RuntimeComponents(components).services
    services.provide("agent_core", "REPLACEMENT")
    assert components["agent_core"] == "REPLACEMENT"


# ── The ledger agrees with the source ────────────────────────────────────────


def test_every_declared_owner_really_writes_the_service() -> None:
    """A wrong owner is worse than none: it reports a service as filled.

    Both directions are checked.  A claimed owner that does not write would make
    the escape hatch look supported where it is not; an **unlisted** writer is the
    scattered-construction debt itself -- a service whose value depends on who ran
    last, which is exactly what makes it unsafely replaceable.
    """
    writers, _readers = _scan()
    wrong: list[str] = []
    for entry in CORE_SERVICES:
        actual = writers.get(entry.name, set())
        if not actual:
            wrong.append(f"{entry.name}: nothing was found writing it")
            continue
        for claimed in entry.owners:
            if claimed not in actual:
                wrong.append(f"{entry.name}: {claimed} does not write it")
        for found in sorted(actual):
            if found not in entry.owners:
                wrong.append(
                    f"{entry.name}: {found} writes it but is not listed as an owner"
                )
    assert not wrong, "the ledger and the source disagree:\n  " + "\n  ".join(wrong)


def test_every_declared_consumer_really_reads_the_service() -> None:
    _writers, readers = _scan()
    wrong: list[str] = []
    for entry in CORE_SERVICES:
        actual = readers.get(entry.name, set())
        for claimed in entry.consumers:
            if claimed not in actual:
                wrong.append(f"{entry.name}: {claimed} does not read it")
    assert not wrong, "the ledger and the source disagree:\n  " + "\n  ".join(wrong)


def test_the_scan_finds_reads_and_writes_at_all() -> None:
    """Control: a scanner that finds nothing passes every assertion above."""
    writers, readers = _scan()
    assert writers, "no writes found"
    assert readers, "no reads found"
    assert "agent_core" in writers
    assert "skill_catalog" in readers


def test_the_scan_detects_a_container_mediated_read() -> None:
    """Control for the *indirect* read, which the consumer check cannot catch.

    ``test_every_declared_consumer_really_reads_the_service`` only walks the
    claimed consumers; if the scanner finds no readers at all, that loop runs zero
    times and the test passes.  So a scanner blind to ``_component(context,
    "skill_catalog")`` would silently stop verifying the very migration it exists
    to protect -- which is what happened while this was being written.
    """
    _writers, readers = _scan()
    commands = "agent/commands/builtin.py"
    found = {name for name, files in readers.items() if commands in files}
    assert found, (
        f"{commands} reads core services through _component(...) and the scan "
        f"reported none of them; the scanner is blind to the indirect read"
    )
    assert "skill_catalog" in found, f"expected skill_catalog among {sorted(found)}"


def test_the_scan_detects_a_plugin_catalog_core_read() -> None:
    """Control for the fourth accessor, added because the guard caught its absence.

    After ``plugins/catalog.py`` was migrated, the consumer check reported it as
    no longer reading ``registry``/``skill_catalog``/``mcp_client`` -- correctly,
    because the scan did not know ``self._core(...)``.  That is the failure mode
    this control exists to make impossible to miss: the guard saying "the ledger is
    wrong" and reality saying "the scanner is blind" look the same until one of
    them is tested.
    """
    _writers, readers = _scan()
    catalog = "agent/plugins/catalog.py"
    found = {name for name, files in readers.items() if catalog in files}
    assert "skill_catalog" in found, (
        f"{catalog} reads core services through self._core(...); scan found "
        f"{sorted(found)}"
    )


def test_the_scan_detects_a_runtime_components_lookup() -> None:
    """Control for the *other* container accessor, which a scan can also miss.

    ``RuntimeComponents.lookup`` tolerates absence where ``require`` raises, so
    both are container-mediated reads -- and a scanner that knew only ``require``
    reported the runtime layer as no longer reading the services it resolves on
    every turn.
    """
    _writers, readers = _scan()
    runtime = "agent/runtime/contracts.py"
    found = {name for name, files in readers.items() if runtime in files}
    assert "agent_loop" in found, (
        f"{runtime} resolves the agent loop through the container; scan found "
        f"{sorted(found)}"
    )


def test_a_metadata_read_is_not_counted_as_a_service_read() -> None:
    """Control for the exclusion that removed seven false positives.

    ``core/subagent_context.py`` reads ``active_context.metadata.get(
    "skill_catalog")`` -- a per-turn copy, not the service -- and nothing else for
    that name.  Counting it put the file in the ledger as a consumer of a service
    it never resolves, and because the ledger is generated from this scan the error
    was invisible: an inflated consumer list fails no functional test.

    A real file is used rather than a synthetic one so the control cannot pass
    while the rule fails on the code that motivated it.
    """
    _writers, readers = _scan()
    metadata_only = "agent/core/subagent_context.py"
    assert metadata_only not in readers.get("skill_catalog", set()), (
        f"{metadata_only} reads skill_catalog only from per-turn metadata, so it "
        f"is not a consumer of the service"
    )


def test_the_metadata_rule_accepts_both_access_shapes() -> None:
    """The rule itself, for both shapes -- the first version covered only one.

    Subscripts were excluded first and seven ``.get`` calls were left behind, so
    the two are asserted together: a rule that handles one shape is how that
    happened.
    """
    import ast

    from tests.test_core_services import _is_metadata_receiver

    def receivers(source: str) -> list[bool]:
        tree = ast.parse(source)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                out.append(_is_metadata_receiver(node.value))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                out.append(_is_metadata_receiver(node.func.value))
        return out

    assert receivers("ctx.metadata['skill_catalog']") == [True]
    assert receivers("ctx.metadata.get('skill_catalog')") == [True]
    assert receivers("components['skill_catalog']") == [False]
    assert receivers("components.get('skill_catalog')") == [False]


def test_the_scan_detects_the_shared_resolve_helper() -> None:
    """Control for the shared accessors, which replaced two local copies.

    ``resolve``/``require_service`` were lifted out of the runtime layer so the
    channel runner could use them rather than growing a third copy.  A scanner
    that did not follow the rename would report every migrated file as no longer
    reading the services it resolves on every turn.
    """
    _writers, readers = _scan()
    channel = "agent/channels/base.py"
    found = {name for name, files in readers.items() if channel in files}
    assert found, (
        f"{channel} resolves core services through resolve(...); scan found none"
    )
    assert "context_manager" in found, f"expected context_manager among {sorted(found)}"


# ── The vocabulary is closed ─────────────────────────────────────────────────


def test_declared_names_match_the_documented_set() -> None:
    """The eight names this step covers, so an accidental removal is caught."""
    assert set(core_service_names()) == {
        "agent",
        "agent_core",
        "context_manager",
        "mcp_client",
        "memory",
        "plugin_catalog",
        "registry",
        "skill_catalog",
        "agent_loop",
        "llm",
    }


def test_an_entry_is_frozen() -> None:
    with pytest.raises(Exception):
        CORE_SERVICES[0].name = "other"  # type: ignore[misc]


def test_a_service_can_be_replaced_through_the_container() -> None:
    """The claim this whole step exists to make true, in one assertion.

    A composition that wants a different AgentCore provides one under the same
    name; every consumer that resolves through the container gets it.
    """
    class _FakeCore:
        pass

    components = {"agent_core": object()}
    container = RuntimeComponents(components)
    original = container.services.get("agent_core")

    container.services.provide("agent_core", _FakeCore(), replace=True)

    assert container.services.get("agent_core") is not original
    assert isinstance(container.services.get("agent_core"), _FakeCore)
    assert components["agent_core"] is container.services.get("agent_core"), (
        "the mapping and the container must not diverge"
    )


def test_a_core_service_entry_is_not_blank() -> None:
    """Control for the dataclass itself."""
    entry = CoreService(
        name="x", owners=("agent/x.py",), consumers=("agent/y.py",), summary="s"
    )
    assert entry.name == "x"


# ── Clause (d): the mapping must not be the only channel ─────────────────────

#: Files that may read a core service straight from a component mapping, with the
#: reason.  Everything else resolves through a container -- see
#: :func:`agent.runtime.services.resolve` and the per-package accessors listed in
#: ``_scan``.
_RAW_MAPPING_ALLOWED: dict[str, str] = {
    "agent/bootstrap.py": (
        "The producer. It reads the mapping it is building, before a container "
        "over it could consistently exist."
    ),
}

#: Functions that read the mapping *while assembling* components.  Assembly is not
#: consumption: there is no alternative implementation to substitute at
#: construction time, and resolving through the container there would raise for
#: services the same function is about to create.
_ASSEMBLY_ALLOWED: dict[str, str] = {
    "agent/cli.py": (
        "_build_scheduler_service, _build_cli_context_manager, "
        "_cli_session_header, _agent_core_for_components: all assemble or report "
        "on components rather than consuming a service during a turn."
    ),
}

def _raw_mapping_service_reads() -> dict[str, list[str]]:
    """Core-service reads that go through a component mapping, by file.

    The counterpart of :func:`_scan`, which deliberately counts container-mediated
    reads *and* raw ones.  This one counts only the raw ones, which is what clause
    (d) is about: a service reachable only through a mapping is not declared.
    """
    names = set(core_service_names())
    found: dict[str, list[str]] = {}
    for path in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        targets = _is_assignment_target(tree)
        for node in ast.walk(tree):
            key = receiver = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    key = first.value
                    receiver = ast.unparse(node.func.value)
            elif isinstance(node, ast.Subscript) and id(node) not in targets:
                if isinstance(node.slice, ast.Constant):
                    key = node.slice.value
                    receiver = ast.unparse(node.value)
            if key not in names or receiver is None:
                continue
            if _is_metadata_receiver(node):
                continue
            # Only receivers that *are* a component mapping; a service object that
            # happens to have a same-named key is not this question.
            if not any(t in receiver for t in ("components", "runtime")):
                continue
            found.setdefault(rel, []).append(f"L{node.lineno}: {receiver}[{key!r}]")
    return found


def test_no_undeclared_file_reads_a_service_from_the_raw_mapping() -> None:
    """Clause (d): the mapping must not be the only channel.

    Every consumer either resolves through a container or appears in a list here
    with a reason someone wrote.  A new raw read is the failure this catches --
    it silently re-creates the unnamed string key that ``CORE_SERVICES`` exists to
    replace, and no functional test can see the difference.
    """
    raw = _raw_mapping_service_reads()
    allowed = set(_RAW_MAPPING_ALLOWED) | set(_ASSEMBLY_ALLOWED)
    offenders = {
        rel: hits for rel, hits in raw.items() if rel not in allowed
    }
    assert not offenders, (
        "these files read a core service straight from a component mapping:\n  "
        + "\n  ".join(
            f"{rel}: {', '.join(hits)}" for rel, hits in sorted(offenders.items())
        )
        + "\n\nUse resolve()/require_service() (agent/runtime/services.py), or add "
        "the file to the guard with a reason."
    )


def test_the_raw_read_guard_has_something_to_allow() -> None:
    """Control: an empty scan would pass every assertion above.

    Two allowlisted files must actually be found, or the guard is not looking at
    the code it claims to police.
    """
    raw = _raw_mapping_service_reads()
    assert "agent/bootstrap.py" in raw, "the producer must be detected"
    assert "agent/cli.py" in raw, "the assembly sites must be detected"


def test_every_allowlisted_file_still_needs_its_exemption() -> None:
    """A stale exemption hides the drift the guard exists to catch.

    Each allowlisted file must still have a raw read.  A file that was migrated
    keeps its name here otherwise, and the next genuine raw read in that file
    would be waved through.
    """
    raw = _raw_mapping_service_reads()
    stale = [
        rel
        for rel in set(_RAW_MAPPING_ALLOWED) | set(_ASSEMBLY_ALLOWED)
        if rel not in raw
    ]
    assert not stale, (
        "these files are exempted but no longer read a service from the raw "
        "mapping; remove the exemption:\n  " + "\n  ".join(sorted(stale))
    )


def test_the_vocabulary_covers_agent_itself() -> None:
    """``agent`` is the most fundamental service and was missing from the ledger.

    Found while checking an unrelated read: ``getattr(core, "_components",
    None)`` reaching for ``"agent"`` on a *different* object's mapping.  A service
    read that widely cannot be absent from the vocabulary that is supposed to
    enumerate every read point.
    """
    names = set(core_service_names())
    assert "agent" in names
    entry = entry_for("agent")
    assert entry.owners == ("agent/bootstrap.py",)
    assert "agent/commands/builtin.py" in entry.consumers


def test_the_scan_detects_a_provide_call_as_a_write() -> None:
    """Control: the fourth write mechanism is the one being relied on now.

    A plugin offering a service does it with ``ctx.provide("name", value)``, so a
    scanner that missed this shape would report every plugin-provided service as
    written by nothing -- and the guard would insist that a *correct* tree is wrong.
    Asserted on the key rule directly, because the failure is a rule that quietly
    stops matching, which no assertion about the tree would notice.
    """
    source = 'def setup(ctx):\n    ctx.provide("agent_loop", self, replace=True)\n'
    call = next(
        node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
    )
    assert _string_key(call) == "agent_loop"


def test_a_provide_of_a_non_literal_name_is_not_counted() -> None:
    """Only literal names are attributable; a computed one cannot be checked.

    The counterpart of the test above: without this, a rule that matched *any*
    ``provide`` and defaulted the key to something would look like it worked.
    """
    source = "def setup(ctx, name):\n    ctx.provide(name, 1)\n"
    call = next(
        node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
    )
    assert _string_key(call) is None


def test_an_event_bus_table_is_not_a_component_mapping() -> None:
    """Control for the exclusion that keeps ``llm`` out of the wrong ledger row.

    The domain table keeps ``{"turn": TURN_EVENTS, "llm": LLM_EVENTS}``, and ``llm``
    is now also a service name -- so the "every dict literal is a component mapping"
    rule listed ``agent/core/event_domains.py`` as a *writer* of the model adapter.
    A ledger that lists a file which does not write the service is worse than one
    that omits it: it tells the next reader that the domain table owns the model.
    """
    bus_table = ast.parse('_BY = {"llm": buses.LLM_EVENTS, "turn": buses.TURN_EVENTS}')
    literal = next(n for n in ast.walk(bus_table) if isinstance(n, ast.Dict))
    assert _is_event_bus_table(literal)


def test_a_component_mapping_is_not_mistaken_for_a_bus_table() -> None:
    """The exclusion has to be narrow, or it would hide real writers.

    Asserted on bootstrap's own shape: values that are clients and registries, not
    bus references.
    """
    mapping = ast.parse('c = {"llm": transport, "agent": agent}')
    literal = next(n for n in ast.walk(mapping) if isinstance(n, ast.Dict))
    assert not _is_event_bus_table(literal)


def test_an_empty_dict_is_not_a_bus_table() -> None:
    """An empty literal carries no evidence either way, so it stays a mapping.

    Asserted because ``all([])`` is ``True``: the predicate would otherwise classify
    every empty dict as a bus table and silently stop counting writes from one.
    """
    empty = ast.parse("c = {}")
    literal = next(n for n in ast.walk(empty) if isinstance(n, ast.Dict))
    assert not _is_event_bus_table(literal)


# ── An extraction must leave one implementation, not two ─────────────────────


def test_the_interjection_drain_lives_in_one_place() -> None:
    """The loop's drain is a module function and no longer an agent method.

    Guards the specific way an extraction goes wrong here: the method is rewritten
    to delegate, or kept "for compatibility", and two implementations drift.  The
    moved code is call-site-reached from the loop, so a stale copy would still be
    the one a caller outside the loop hit.

    Also asserts the dependency is a parameter: the function read ``self.registry``
    before, which made the dependency invisible in its signature.
    """
    agent_tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(agent_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_inject_pending_interjections" not in methods, (
        "the drain is still defined on BaseAgent as well as in "
        "agent/core/interjections.py; two implementations is the failure this "
        "extraction was supposed to avoid"
    )

    module_tree = ast.parse((REPO_ROOT / "agent/core/interjections.py").read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(module_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "inject_pending_interjections"
    )
    assert [arg.arg for arg in func.args.args] == ["ctx", "pending", "registry"], (
        "the registry has to be a parameter, not reached through a 'self'"
    )
