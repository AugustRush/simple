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
from agent.kernel.service import MissingServiceError

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


def _is_config_receiver(node: ast.AST) -> bool:
    """Whether *node* is the configuration mapping rather than a service container.

    ``cfg.get("memory")`` reads the ``memory`` *section* of the config, and the
    scanner cannot tell it from a read of the ``memory`` service without knowing
    the receiver.  Configuration receivers are named ``cfg``/``config`` by
    convention throughout this tree; services are read from ``components``,
    ``ctx``, ``registry`` or a service attribute.
    """
    if isinstance(node, ast.Call):
        node = node.func.value if isinstance(node.func, ast.Attribute) else node.func
    text = ast.unparse(node)
    return text in {"cfg", "config", "self._cfg", "self._config", "self.cfg", "self.config"}


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


def _is_service_passthrough(node: ast.Dict) -> bool:
    """Whether *node* is "pull these names out of a mapping" rather than a composition.

    The shape is exact and narrow: every value is ``something.get("<its own key>")``.
    ``{"memory": components.get("memory"), ...}`` is a helper handing four services on;
    it reads the mapping and provides nothing, and counting it as a writer made the
    ledger say ``cli.py`` constructs the memory service.

    A value-shape rule rather than a positional one -- "only dicts assigned to a
    ``*components*`` name" -- because the existing rule is deliberately broad: bootstrap
    delivers its component mapping from a ``return {...}``, so a scanner that looked only
    at assignments missed ``memory`` entirely.  Narrowing by position would restore that
    blind spot; narrowing by value does not.
    """
    if not node.keys:
        return False
    for key_node, value in zip(node.keys, node.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            return False
        call = value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == key_node.value
        ):
            return False
    return True


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
    #: Dict literals that *declare* which plugin provides a service rather than
    #: providing it (``PluginProfile(service_replacements={"ltm_store": "..."})``).
    #: A declaration names the same service the composition writes, so counting it
    #: as a writer made the ledger claim a profile constructs the store -- and a
    #: ledger that names a writer which only declares tells the next reader not to
    #: replace it.
    declarations: set[int] = set()

    def note(bucket: dict[str, set[str]], key: str | None, rel: str) -> None:
        if key in names:
            bucket.setdefault(key, set()).add(rel)

    for path in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Collected from the *same* tree the loop below walks: the exclusion is by
        # node identity, and a second ``ast.parse`` produces different objects.
        declarations = {
            id(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg in {"service_replacements", "service_replacement"}
        }
        assignment_targets = _is_assignment_target(tree)
        for node in ast.walk(tree):
            # **Every** dict literal, anywhere -- not only ones bound by an
            # assignment.  Bootstrap delivers the component mapping from a
            # ``return {...}``, and a scanner that looked only at assignments
            # missed ``memory`` entirely while reporting the ledger as correct,
            # because the guard and the ledger shared the same blind spot.
            if isinstance(node, ast.Dict):
                if id(node) in declarations:
                    continue
                # A *provider* is not "any dict that mentions a service name".  A helper
                # that pulls names out of a mapping is a reader, and counting it made the
                # ledger claim cli.py constructs the memory service -- a ledger that names
                # a writer which only reads tells the next reader not to replace it.
                if not _is_event_bus_table(node) and not _is_service_passthrough(node):
                    for key_node in node.keys:
                        if isinstance(key_node, ast.Constant):
                            note(writers, key_node.value, rel)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        # The same exclusion the read path makes, and its absence here
                        # was inflating the ledger: ``ctx.metadata["skill_catalog"] = x``
                        # copies a reference into a per-turn mapping, and counting it as
                        # a write made three files look like they construct the skill
                        # catalog.  A ledger that says a file owns a service it only
                        # references makes the next reader avoid replacing it.
                        if _is_metadata_receiver(target.value):
                            continue
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
                    # being checked.  Two receivers are excluded because they carry
                    # some of the same names without being component mappings:
                    # per-turn metadata (:func:`_is_metadata_receiver`) and the
                    # configuration, whose ``memory``/``sandbox`` keys are
                    # *sections* (:func:`_is_config_receiver`).
                    if not _is_metadata_receiver(func.value) and not _is_config_receiver(
                        func.value
                    ):
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


def test_every_reader_of_a_service_is_declared() -> None:
    """The direction that was missing, and the one that matters.

    The guard above checks declared -> actual: every file the ledger names really does read
    the service.  Nothing checked actual -> declared, so **a reader nobody listed was
    invisible**.  Measured when this was added: 22 reader/file pairs across 7 services, every
    one of them a plugin added by this refactor.

    That matters because the ledger's stated purpose is to enumerate *every read point* before
    anything is deleted.  A ledger that misses readers is not incomplete bookkeeping -- it is a
    false negative, and this session's notes say plainly what those do: "a check with false
    negatives is worse than no check when it gates a deletion: it does not merely miss a
    problem, it *authorises* one."
    """
    _, readers = _scan()
    ledger = {entry.name: entry for entry in CORE_SERVICES}

    undeclared = []
    for name, files in sorted(readers.items()):
        entry = ledger.get(name)
        if entry is None:
            undeclared.append(f"{name}: no ledger entry, read by {sorted(files)}")
            continue
        for path in sorted(files - set(entry.consumers)):
            undeclared.append(f"{name}: {path} reads it but is not a declared consumer")

    assert not undeclared, (
        "the ledger under-reports its readers:\n  " + "\n  ".join(undeclared)
    )


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
    """The names the vocabulary covers, so an accidental removal is caught.

    ``ltm_store`` joined when the memory contracts were written down: it was
    already a service (``agent/memory/plugin.py`` provides it, the session plugin
    and the composition root resolve it) and being *undeclared* was the reason it
    had no written interface to replace.
    """
    assert set(core_service_names()) == {
        "agent",
        "agent_core",
        "context_manager",
        "ltm_store",
        "mcp_client",
        "memory",
        "plugin_catalog",
        "registry",
        "skill_catalog",
        "agent_loop",
        "llm",
    }


def test_the_declared_contracts_exist() -> None:
    """A contract pointer has to name a real symbol, or it is decoration.

    Checked by reading the file rather than importing it: the pointer is written
    the way the owner and consumer lists are (a source path), and importing every
    contract module to check one name would make this test depend on the import
    graph it is supposed to describe.
    """
    for entry in CORE_SERVICES:
        if not entry.contract:
            continue
        relative, _, symbol = entry.contract.partition(":")
        source = REPO_ROOT / relative
        assert source.is_file(), f"{entry.name} points at a missing file: {relative}"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        found = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        } | {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        assert symbol in found, f"{entry.name} points at a missing {symbol} in {relative}"


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
#: :func:`agent.kernel.service.resolve` and the per-package accessors listed in
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
        "_build_cli_context_manager, _cli_session_header: both assemble or report "
        "on components rather than consuming a service during a turn."
    ),
    "agent/scheduler/build.py": (
        "Relocated from cli.py, where it was already allowlisted under the same "
        "reason: build_scheduler_service assembles a runtime for a scheduled run "
        "and its executor reads that run's own components. Assembly is not "
        "consumption -- there is no alternative implementation to substitute at "
        "construction time, and resolving through a container there would raise "
        "for services the same function is about to create."
    ),
    "agent/runtime/contracts.py": (
        "agent_core_for_components: returns the AgentCore for a mapping, wrapping "
        "it when none is present. It is the factory for that service, not a "
        "consumer of it."
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
        + "\n\nUse resolve()/require_service() (agent/kernel/service.py), or add "
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
    # The construction moved into `AgentPlugin`, so the owner moved with it -- and
    # `bootstrap` became a *consumer*, which is the shape this whole refactor is for: the
    # composition resolves the agent rather than assembling it.
    assert entry.owners == ("agent/core/agent_plugin.py", "agent/bootstrap.py")
    assert "agent/commands/builtin.py" in entry.consumers
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


def test_the_history_builders_take_the_adapter_as_a_parameter() -> None:
    """An extraction has to move the *dependency* out of ``self``, not just the code.

    Both builders used to read ``self._transport``, which made the adapter invisible
    in their signatures: nothing said that building a history entry involves the
    model adapter at all.  As parameters, the adapter is replaceable at the loop's
    edge as well as at the agent's -- which is the point of the ``llm`` service.

    Asserted on the signatures because a future edit that reaches back through a
    ``self`` would compile, pass, and quietly undo the seam.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8"))
    funcs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    for name, first_param in (
        ("assistant_message", "transport"),
        ("tool_result_messages", "transport"),
    ):
        assert name in funcs, f"{name} is not in agent/core/turn_loop.py"
        args = [arg.arg for arg in funcs[name].args.args]
        assert args[0] == first_param, (
            f"{name} must take the adapter first; got {args}"
        )
        assert "self" not in args, f"{name} must not be a method: {args}"


def test_the_history_builders_are_no_longer_agent_methods() -> None:
    """The other half, and the one an extraction fails: a delegating copy left behind.

    Two implementations drift, and the one a caller outside the loop reaches is the
    stale one.  Asserted as an absence because the failure is an absence of cleanup.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for stale in ("_assistant_message", "_tool_result_messages"):
        assert stale not in methods, (
            f"{stale} is still defined on BaseAgent as well as in "
            f"agent/core/turn_loop.py"
        )


# ── The loop's dependency surface, recomputed from the source ────────────────

#: Members of ``BaseAgent`` that only the turn loop uses, and therefore move with it
#: rather than being handed to it.  This is the list that shrinks as the loop moves:
#: every name here is a member that will leave ``BaseAgent``.
_LOOP_OWNED = (
    "_build_summarized_tool_results",
    "_create",
    "_parse_response",
    "_response_completion_error",
    "_build_user_message_content",
    "_compact_body_for_turn",
    "_continue_truncated_response",
    "_handle_end_turn",
    "_is_content_filter_block",
    "_is_llm_retryable",
    "_new_watchdog_state",
    "_next_structured_output_budget",
    "_plan_orchestration",
    "_prepare_turn",
    "_promote_orchestration_runs",
    "_recover_from_content_filter",
    "_recover_incomplete_tool_response",
    "_retrieval_token_budget",
    "_run_turn_loop",
    "_stream_response",
    "_synthesize_tool_only_response",
    "_retry_policy",
    "_model_call",
    "_image_content_block",
    "_inject_pending_interjections",
    "_assistant_message",
    "_tool_result_messages",
)


#: Members that were loop-owned and have already left ``BaseAgent``.  They stay in
#: ``_LOOP_OWNED`` because the loop's dependency surface is measured against what the
#: loop reads *now*: dropping them would silently change which members count as
#: shared, and the struct assertion would then pass on a wrong struct.
_ALREADY_MOVED = (
    "_build_continuation_context",
    "_run_turn_loop",
    "_compact_body_for_turn",
    "_promote_orchestration_runs",
    "_build_summarized_tool_results",
    "_continue_truncated_response",
    "_handle_end_turn",
    "_merge_continuation_text",
    "_parse_response",
    "_response_completion_error",
    "_synthesize_tool_only_response",
    "_build_user_message_content",
    "_next_structured_output_budget",
    "_recover_incomplete_tool_response",
    "_image_content_block",
    "_plan_orchestration",
    "_prepare_turn",
    "_retrieval_token_budget",
    "_inject_pending_interjections",
    "_assistant_message",
    "_tool_result_messages",
    "_with_llm_retry",
)


#: Members only the loop reads that nevertheless **stay on the agent**, and so are
#: carried by the struct rather than moving with the loop.  Each is a seam the suite
#: patches on the instance -- ``agent._create = fake`` appears thirty-eight times --
#: and a struct built from the transport instead would go around the patch, which is
#: how those tests would silently stop intercepting the model.
#:
#: Named explicitly because the guard's default rule is the opposite one: a member
#: only the loop reads is one the loop takes with it.
_AGENT_SEAMS = (
    "create",
    "recover_from_content_filter",
    "retry_policy",
    "stream_response",
)


#: Names a loop-owned agent method reads from ``self`` that are **not** dependencies
#: to inject, in three kinds:
#:
#: * how the struct is built and what builds it -- ``_loop_deps`` and ``_model_call``
#:   are the accessors the facades use, so counting them would make the struct
#:   declare its own constructor;
#: * the loop's entry point -- ``_run_turn_loop`` is called by ``send_message``, which
#:   is the caller rather than a dependency;
#: * free functions the loop calls directly -- ``is_content_filter_block``,
#:   ``is_llm_retryable`` and ``new_watchdog_state`` are module-level, already
#:   imported by ``turn_loop.py``, and injecting them would be injecting a module.
_STRUCT_BUILDERS = (
    "loop_deps",
    "model_call",
    "run_turn_loop",
    "is_content_filter_block",
    "is_llm_retryable",
    "new_watchdog_state",
)


def _loop_dependency_surface() -> tuple[set[str], set[str]]:
    """``(shared, loop_only)``: what the loop reads from the agent, split in two.

    Recomputed from the source rather than compared against a hand-written list, for
    the same reason the core-service ledger is: a list that is only ever read by the
    assertion that checks it can be edited to match anything.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    loop_names = set(_LOOP_OWNED) | {"send_message"}

    def touched(node: ast.AST) -> set[str]:
        # Stripped of one leading underscore, because that is the difference between
        # the two spellings: a member is ``_bounded_text`` on the agent and
        # ``bounded_text`` on the struct, and intersecting the two forms is empty --
        # which made the guard report every borrowed member as owned.
        return {
            x.attr.lstrip("_")
            for x in ast.walk(node)
            if isinstance(x, ast.Attribute)
            and isinstance(x.value, ast.Name)
            and x.value.id == "self"
        }

    needed: set[str] = set()
    for name in loop_names:
        node = methods.get(name)
        if node is not None:
            needed |= touched(node)

    # The loop's own code now spans two files: the cluster that moved to
    # ``agent/core/turn_loop.py`` reads its dependencies as ``deps.X``, and a scan
    # that only looked at ``agent.py`` would report those as no longer borrowed --
    # so the struct assertion would demand removing fields the loop still uses.
    loop_module = ast.parse(
        (REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(loop_module):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "deps":
            needed.add(node.attr.lstrip("_"))

    owned_elsewhere: set[str] = set()
    for name, node in methods.items():
        if name not in loop_names:
            owned_elsewhere |= touched(node)

    # ``_inject_pending_interjections`` and the two history builders have already
    # moved out, so they no longer appear as methods; their names stay in the list
    # above and simply contribute nothing.
    #
    # Order matters and was wrong the first time: what the loop *borrows* is the
    # intersection -- another method also uses it, so it stays on the agent.  The
    # difference is what only the loop uses, which moves with it.  Returning them
    # the other way made the guard report every borrowed member as owned and every
    # owned member as missing.
    shared = needed & owned_elsewhere
    loop_only = needed - owned_elsewhere
    # A seam stays on the agent, so it is borrowed by the loop even though nothing
    # else reads it.
    shared |= {name for name in _AGENT_SEAMS if name in needed}
    loop_only -= set(_AGENT_SEAMS)
    return shared, loop_only


def test_the_loop_dependency_struct_covers_what_the_loop_reads() -> None:
    """``LoopDeps`` must be exactly what the loop reads from the agent.

    Two directions, and they mean different things:

    * **missing** -- the loop reads something the struct does not declare, so the
      extraction would have to smuggle it in.  Smuggling is how a decoupling turns
      out not to be one.
    * **dead** -- the struct declares something the loop never reads.  That is a
      field kept "for later", and it makes the surface look wider than it is while
      hiding the fact that nothing tested it.

    What is deliberately *not* checked here is whether a borrowed member is also read
    by code outside the loop.  An earlier version required that, which made a member
    the loop reads and that nothing else does look like an error -- but that is the
    normal state of a loop-owned member that has not moved yet, and the set of those
    is what ``_LOOP_OWNED`` is for.  Treating it as an error here would have forced
    either a false "seam" label or a move this round was not doing.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8"))
    struct = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "LoopDeps"
    )
    declared = {
        stmt.target.id
        for stmt in struct.body
        if isinstance(stmt, ast.AnnAssign)
    }

    # The surface function splits what the loop reads into borrowed and owned; the
    # union is the whole of it, which is what the struct has to cover.
    borrowed, loop_owned = _loop_dependency_surface()
    needed = (borrowed | loop_owned) - set(_STRUCT_BUILDERS)
    assert len(needed) >= 15, f"the loop reads more than this: {sorted(needed)}"
    missing = sorted(needed - declared)
    dead = sorted(declared - needed)
    assert not missing and not dead, (
        f"LoopDeps is out of step with what the loop reads from the agent\n"
        f"  missing (the loop reads these; the struct must declare them): {missing}\n"
        f"  dead (declared but never read by the loop): {dead}"
    )


def test_the_loop_owned_list_matches_the_source() -> None:
    """Control for the list above: each name must still be a ``BaseAgent`` method.

    A name that has already moved is not a member any more, and leaving it in the
    list would let the surface be under-counted -- the loop-only set is subtracted
    from what the loop needs, so a stale entry quietly removes a real dependency
    from ``LoopDeps`` and the assertion above would pass on a wrong struct.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    already_moved = [
        name for name in _LOOP_OWNED
        if name not in methods
        and name not in _ALREADY_MOVED
    ]
    assert not already_moved, (
        f"these are in _LOOP_OWNED but no longer exist on BaseAgent, so they have "
        f"moved and the list must say so: {already_moved}"
    )


def test_the_loop_surface_is_not_empty() -> None:
    """Control: a scan that found nothing would satisfy every assertion above."""
    shared, loop_only = _loop_dependency_surface()
    assert len(shared) >= 15, f"the loop borrows more than this: {sorted(shared)}"
    assert loop_only, "the loop owns nothing? the split is not being computed"


def test_loop_deps_can_be_built_from_a_real_agent() -> None:
    """The struct's names must resolve, not merely match.

    The guard above compares *names* against the source, which a typo would pass:
    ``LoopDeps.of`` could name an attribute that does not exist and nothing would
    notice until the loop ran.  This builds one from a real agent, so every field is
    resolved at least once, and asserts the two properties the docstring claims --
    that bound methods stay bound to that agent, and that the struct is frozen.
    """
    from dataclasses import FrozenInstanceError

    from agent.core.agent import BaseAgent
    from agent.core.turn_loop import LoopDeps
    from agent.tools.runtime import ToolRegistry

    agent = BaseAgent(object(), ToolRegistry(), model="m", api_format="anthropic")
    deps = LoopDeps.of(agent)

    assert deps.transport is agent._transport
    assert deps.effective_model.__self__ is agent, "the method came from the agent"
    assert deps.registry is agent.registry
    assert deps.max_tokens == agent.max_tokens

    with pytest.raises(FrozenInstanceError):
        deps.max_tokens = 1  # type: ignore[misc]


def test_the_turn_context_helpers_are_defined_once() -> None:
    """The loop needs these, and importing them from ``agent.py`` would be a cycle.

    ``agent`` imports the turn loop, so the loop cannot import the active-context
    helpers back from it.  They live lower now, and ``agent.py`` re-exports them
    under their old private names so the twelve call sites across tools, security
    and the scheduler did not have to change.

    Asserted as an absence on ``agent.py``, because the failure mode is a *second*
    definition left behind: ``agent.py``'s own copy would shadow the import, the
    module-level ``ContextVar`` would become two different variables, and the
    symptom would be "the agent context is sometimes None" -- which reads as a
    concurrency bug rather than a duplicate definition.
    """
    agent_tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(agent_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("_active_agent_id", "_schedule_domain_event", "_emit_domain_event"):
        assert name not in defined, (
            f"{name} is defined in agent/core/agent.py as well as in "
            f"agent/core/turn_context.py; the local definition shadows the import"
        )
    assigned = {
        target.id
        for node in ast.walk(agent_tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert "_active_agent_context" not in assigned, (
        "a second ContextVar of that name would shadow the one the tools read"
    )


def test_the_turn_context_module_owns_the_contextvar() -> None:
    """And it is the module the rest of the codebase resolves it from."""
    from agent.core import turn_context
    from agent.core.agent import _active_agent_context

    assert _active_agent_context is turn_context.active_agent_context, (
        "the re-export must be the same object, not a copy: two ContextVars with "
        "one name is the failure this move was supposed to make impossible"
    )


def test_the_retry_leg_takes_a_policy_not_an_agent() -> None:
    """The retry leg is the case that showed a wide struct was the wrong shape.

    The first attempt handed it the whole ``LoopDeps`` (twenty-three members), which
    made ``agent/core/llm_attempt.py`` hold a complete agent surface -- a module whose
    docstring promises it does not import ``agent.core.agent``.  A part of the loop
    must be able to declare *its* dependencies and no more.
    """
    from agent.core.retry import RetryPolicy, with_llm_retry

    params = list(with_llm_retry.__code__.co_varnames[: with_llm_retry.__code__.co_argcount])
    assert params[0] == "policy", params
    assert "agent" not in params, (
        f"the retry leg must not take an agent: {params}"
    )
    fields = set(RetryPolicy.__dataclass_fields__)
    assert fields <= {"max_retries", "base_delay", "event_bus", "is_retryable"}, (
        f"the policy is meant to stay small; it has grown to {sorted(fields)}"
    )


def test_the_retry_predicate_is_part_of_the_policy() -> None:
    """Not a module constant the call site reaches for.

    "Is this transient?" is a judgement the composition makes: a provider with a
    different notion of transient supplies its own.  When this lived only in
    ``llm_errors``, a test that wanted a permissive predicate had to override a
    method on the object under test -- reaching inside the thing being tested to
    arrange the test.
    """
    from agent.core.retry import RetryPolicy

    assert "is_retryable" in RetryPolicy.__dataclass_fields__
    policy = RetryPolicy(max_retries=0, base_delay=0.0, is_retryable=lambda exc: True)
    assert policy.is_retryable(ValueError("x")) is True


def test_the_retry_method_is_no_longer_on_the_agent() -> None:
    """And the agent reaches the module function through a policy it builds."""
    tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_with_llm_retry" not in methods, (
        "_with_llm_retry is still defined on BaseAgent as well as in "
        "agent/core/retry.py"
    )
    assert "_retry_policy" in methods, (
        "the agent has to offer its policy for the retry leg to be callable"
    )


def test_the_model_call_facade_stays_a_facade() -> None:
    """``_create`` may exist, but only as one line of delegation.

    This is the counterweight to the rule the other extractions followed (remove the
    method, migrate the call sites).  ``_create`` is a seam the suite genuinely uses:
    thirty-eight tests replace it to stand in for the provider, so removing it would
    have been removing the test's way of answering "how does this agent call the
    model" without building a transport.

    What must not happen is a second implementation growing here.  The logic lives in
    ``agent/core/model_call.py``, and this asserts the body is exactly a return of
    that function -- so "a facade" is enforced rather than trusted.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_create"
    )
    body = [
        stmt for stmt in method.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    assert len(body) == 1, (
        f"_create has {len(body)} statements; it is meant to be a facade over "
        f"agent/core/model_call.py, not a second implementation"
    )
    stmt = body[0]
    assert isinstance(stmt, ast.Return), ast.dump(stmt)
    source = ast.unparse(stmt)
    assert "create_turn_response(" in source, source
    assert "self._model_call()" in source, source


def test_every_deliberate_facade_is_only_a_delegation() -> None:
    """The facades are a named list, and each must be one statement.

    ``_create``, ``_stream_response``, ``_recover_from_content_filter``,
    ``_prepare_provider_context`` and ``_observe_provider_usage`` all stay on
    ``BaseAgent`` because tests reach them there -- patching or calling.  The rule the
    other extractions followed (remove the method, migrate the call sites) was wrong
    for them: for ``_create`` alone, thirty-eight sites replace it.

    A facade is not a second implementation, and this is what keeps that true.  A
    body of more than one statement is how a second implementation starts, and it
    would drift from the module function with no test able to see the difference --
    the two would agree until they did not.
    """
    tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, target in (
        ("_create", "create_turn_response"),
        ("_stream_response", "stream_response"),
        ("_recover_from_content_filter", "recover_from_content_filter"),
        ("_prepare_provider_context", "prepare_provider_context"),
        ("_observe_provider_usage", "observe_provider_usage"),
    ):
        assert name in methods, f"{name} is the facade tests reach; it must exist"
        body = [
            stmt for stmt in methods[name].body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        ]
        assert len(body) == 1 and isinstance(body[0], ast.Return), (
            f"{name} has {len(body)} statements; a facade is one return of {target}"
        )
        source = ast.unparse(body[0])
        assert target in source, source


def test_the_model_call_surface_is_six_members() -> None:
    """And it stays narrow, for the reason the retry policy is narrow.

    The first attempt handed every helper the whole loop surface, which is what made
    ``agent/core/llm_attempt.py`` hold a complete agent against its own docstring.
    """
    from agent.core.model_call import ModelCall

    fields = set(ModelCall.__dataclass_fields__)
    assert fields == {
        "transport",
        "effective_model",
        "configured_output_cap",
        "prepare_provider_context",
        "observe_provider_usage",
        "event_bus",
    }, sorted(fields)


def test_the_loop_body_lives_in_the_loop_module() -> None:
    """``_run_turn_loop`` moved whole, and ``send_message`` calls it.

    Two assertions, because the extraction has two ways to be wrong.  A delegating
    copy left on ``BaseAgent`` is the first: it would keep the body reachable and the
    move would be a duplicate rather than an extraction.  ``send_message`` still
    running its own copy is the second, and it is invisible from the method list --
    the name would simply be absent and the loop would be dead code.

    The ``try``/``finally`` matters here specifically: it restores three contextvars
    and clears the turn's markers, and a loop that returned early without it is the
    failure this function was extracted *as a unit* to avoid.
    """
    agent_tree = ast.parse((REPO_ROOT / "agent/core/agent.py").read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(agent_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_run_turn_loop" not in methods, (
        "_run_turn_loop is still on BaseAgent; the body belongs to "
        "agent/core/turn_loop.py"
    )

    send = next(
        node for node in ast.walk(agent_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_message"
    )
    calls = {
        ast.unparse(call.func)
        for call in ast.walk(send)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    assert "run_turn_loop" in calls, (
        f"send_message must call the moved loop; it calls {sorted(calls)}"
    )

    loop_tree = ast.parse((REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(loop_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_turn_loop"
    )
    assert any(isinstance(stmt, ast.Try) for stmt in fn.body), (
        "the loop and its teardown are one unit: the try/finally must be in the "
        "moved function, not left behind in the caller"
    )


def test_the_superseded_plugin_interface_is_gone() -> None:
    """One plugin interface, not two.

    ``PluginContext`` and ``PluginInstall`` were the catalog's own context object.  A
    plugin is handed the kernel's ``Context`` now, so they had no live caller -- but
    they survived a whole phase: re-exported from ``agent/plugins/__init__.py``,
    referenced by docstrings, and constructed by four tests that drove an API
    (``ctx.on("turn", handler)``) which had been replaced by typed subscription.

    That is the shape of a deletion that comes back: nothing fails while it lingers,
    and a new plugin author reading the package's exports cannot tell which context
    is the real one.  Asserted as an absence, on both the module and the export list.
    """
    assert not (REPO_ROOT / "agent/plugins/context.py").exists(), (
        "agent/plugins/context.py is back; plugins are handed the kernel's Context"
    )

    init = (REPO_ROOT / "agent/plugins/__init__.py").read_text(encoding="utf-8")
    for name in ("PluginContext", "PluginInstall"):
        assert f'"{name}"' not in init, (
            f"{name} is exported again; it is the superseded interface"
        )

    offenders = []
    plugin_pkg = REPO_ROOT / "agent/plugins"
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        # A mention in prose is fine -- several docstrings explain the history.  What
        # would make it live again is an import, in either form.  The relative form is
        # only this package's own module: ``agent/memory/context.py`` is a different
        # file, and matching bare ``from .context import`` flagged three files there.
        if "from agent.plugins.context import" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
        elif "from .context import" in text and path.parent == plugin_pkg:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"the superseded context is imported again: {offenders}"


# ── The component mapping is a closed, reviewed set ──────────────────────────

#: Every key ``bootstrap`` puts into a component mapping, and who reads it.
#:
#: A **closed list** rather than a scan for readers, and the difference matters: an
#: earlier version of this guard searched the source for reads and required each key to
#: have one.  It reported ``client_provider`` as write-only and the key was deleted --
#: but ``_build_web_session_components`` writes it and a test reads it back off the
#: returned mapping, through a variable named ``result`` that no reader-scan would
#: recognise.  The deletion was caught by the test suite, not by the guard.
#:
#: A check with false negatives is worse than no check when it gates a deletion: it
#: does not merely miss a problem, it *authorises* one.  So this asserts the set
#: instead.  Adding a key or removing one is now a deliberate edit to this table,
#: which is where the question "who reads it" gets asked by a person.
_COMPONENT_KEYS: dict[str, str] = {
    # identity and model
    # Two writers, which is why this key is *not* in `_PLUGIN_PROVIDED` below:
    # `AgentPlugin` provides the process-wide agent, and a session builds its own against
    # the config on disk -- that is what a rebuild is for.  The ledger's `owners` carries
    # both; this table describes the key.
    "agent": "the agent a turn runs on",
    "agent_core": "the command coordinator's core",
    "base_system_prompt": "the head of the system prompt",
    "client": "the provider client",
    "client_cache_key": "keys the shared client cache",
    "client_provider": "the active provider's name; read back off a web session",
    "provider_client_cache": (
        "one SDK client per provider configuration for the process; provided by the "
        "`model` plugin and resolved by every web session's transport rebuild"
    ),
    "model": "the model name in effect",
    # Written for every composition and, as of this table's writing, **not read back off
    # a mapping**: the provider format is read from config dicts (``provider_cfg``) and
    # from the agent's own ``api_format`` attribute.  Recorded rather than deleted
    # because the previous deletion in this area was wrong -- a reader-scan missed a
    # mapping handed to a caller and read through a local name -- and the honest state
    # of a key like this is "described, reader unknown" rather than "removed".
    "api_format": "the provider's format; no mapping reader found (see the note)",
    "max_tokens": "the output ceiling the session was built with",
    "llm": "the model adapter service",
    # services
    # Two writers, so *not* in `_PLUGIN_PROVIDED`: the `registry` plugin provides
    # the process-wide one, and a web session forks its own into its mapping (a
    # fork is what a session's tool boundary is).  The ledger's `owners` carries
    # both.
    "registry": "the tool registry",
    "context_manager": "the session log",
    "skill_catalog": "the skill catalog",
    "plugin_catalog": "the plugin catalog",
    # The client and its status come from ``agent/tools/mcp_plugin.py`` now (see
    # `_PLUGIN_PROVIDED`); they stay in this table for the same reason ``evolution``
    # does -- it documents what a composition can *contain*, and the entry moving
    # from "bootstrap writes it" to "a plugin provides it" is the change this table
    # exists to make visible.
    "mcp_client": "the MCP client",
    "mcp_status": "what the MCP connection attempts did",
    "mcp_task": "the background MCP connect task",
    "channels": (
        "the channels a composition runs; provided so a plugin can contribute one, which"
        " was the contribution it could not make"
    ),
    "memory": "the memory palace; provided by MemoryPlugin",
    "ltm_store": (
        "the long-term store; provided by MemoryPlugin, resolved by ContextManager and "
        "by the web channel's session-store factory"
    ),
    "runtime_factory": (
        "builds a runtime for a config; read by a scheduled run, which needs one built "
        "from the config on disk rather than the one it started in"
    ),
    # Mounted as a plugin now (agent/ralph/plugin.py), so bootstrap no longer writes
    # it into the mapping.  Kept in this table rather than removed: `_COMPONENT_KEYS`
    # documents what a composition can *contain*, and Ralph is one of those -- the
    # entry moving from "bootstrap writes it" to "a plugin provides it" is exactly the
    # kind of change this table exists to make visible.
    "evolution": "the evolution engine, or None",
    "system_prompt": "the composed system prompt",
    "user_tool_catalog": "user-contributed tools",
    "user_tools_enabled": "whether they are loaded",
    "file_service": "the file service",
    "file_access_policy": "the file policy",
    # construction and paths
    "cfg": "the config this composition was built against",
    # Written by the composition root at the end of a build: what did not load, and what
    # is waiting.  Both were computed before and neither was shown anywhere.
    "plugin_failures": "plugins whose setup raised, by name",
    "plugin_pending": "plugins waiting for a dependency the composition does not supply",
    "output_dir": "where output is written",
    "workspace_root": "the workspace",
    # The paths the `registry` and `skill_catalog` plugins build their services from.
    # They are values rather than services: the CLI's `--name`/`--resource` choose
    # them, so a plugin must be told, not left to derive its own.
    "resource_root": "where resource definitions (skills, tools, plugins) live",
    "user_skills_dir": "the user's skills root",
    "user_tools_dir": "the user's tools root",
    "user_plugins_dir": "the user's plugins root",
    "config_revision": "bumped to force a session rebuild",
    "command_coordinator_factory": "builds a session's coordinator",
    "session_components_factory": "builds a session's components",
    "session_store_factory": "builds a session's store",
    "use_memory_worker_pool": "whether to share the memory worker pool",
    "_shares_global_runtime": "marks a session mapping as a copy of the global one",
}


#: Keys a plugin provides rather than the composition root writing them into a
#: mapping.  Named separately so the "written by bootstrap" set stays a set of what
#: bootstrap builds, while the table above can still describe the whole composition.
_PLUGIN_PROVIDED: dict[str, str] = {
    "ralph_service": "agent/ralph/plugin.py",
    "evolution": "agent/_builtin/plugins/evolution/__init__.py",
    "memory": "agent/memory/plugin.py",
    "ltm_store": "agent/memory/plugin.py",
    "context_manager": "agent/memory/session_plugin.py",
    # ``llm``/``model`` are *not* here: bootstrap still writes both -- the model name
    # into the mapping, and a web session's own routing transport under ``llm``.  The
    # plugin provides the process-wide transport; the key has two writers.
    "provider_client_cache": "agent/core/model_plugin.py",
    "mcp_client": "agent/tools/mcp_plugin.py",
    "mcp_server_configs": "agent/tools/mcp_plugin.py",
    "mcp_status": "agent/tools/mcp_plugin.py",
    "skill_catalog": "agent/skills/plugin.py",
}


def _component_mapping_keys() -> set[str]:
    """Keys ``bootstrap`` puts into a component mapping.

    Only the mappings it *builds*: the literals assigned to a ``*components*`` name
    and the ``update({...})`` calls that extend one.
    """
    tree = ast.parse((REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8"))
    keys: set[str] = set()

    def collect(literal: ast.Dict) -> None:
        keys.update(
            key.value for key in literal.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )

    for node in ast.walk(tree):
        # ``AnnAssign`` as well as ``Assign``: ``_partial_components: dict = {...}`` is
        # annotated, and a scan that knew only the bare form missed the largest
        # mapping in the file -- it reported memory, skill_catalog and
        # user_tool_catalog as "described but not written", which is the scan being
        # wrong rather than the table.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            assigned = node.targets if isinstance(node, ast.Assign) else [node.target]
            targets = [t.id for t in assigned if isinstance(t, ast.Name)]
            if any("components" in name for name in targets) and isinstance(node.value, ast.Dict):
                collect(node.value)
            # ``components["x"] = ...`` -- one key at a time, and the form most of the
            # services are added in.
            for target in assigned:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and "components" in target.value.id
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and "components" in node.func.value.id
        ):
            for node_arg in node.args:
                if isinstance(node_arg, ast.Dict):
                    collect(node_arg)
    return keys


def test_the_component_mapping_is_a_closed_set() -> None:
    """Adding or removing a component is a deliberate edit, not a side effect.

    Two reasons this is asserted as a set rather than by scanning for readers.  The
    first is that the scan cannot see every reader -- a mapping handed to a caller and
    read back through a local name is invisible to it -- so it produces the *unsafe*
    kind of error when it is wrong.  The second is that the question worth asking is
    not "does any line read this" but "does this belong in the composition", and that
    is a question a person answers once, here, rather than a scan answering wrongly.

    A new key fails this test until it is described, which is the point.
    """
    keys = _component_mapping_keys()
    declared = set(_COMPONENT_KEYS) - set(_PLUGIN_PROVIDED)
    missing = sorted(keys - declared)
    stale = sorted(declared - keys)
    assert not missing and not stale, (
        "the component mapping and this table disagree\n"
        f"  written but undescribed: {missing}\n"
        f"  described but not written: {stale}\n\n"
        "Describe the new key and who reads it, or stop writing it."
    )


def test_the_component_scan_finds_the_mappings() -> None:
    """Control: a scan that found nothing would pass the assertion above.

    The failure mode of a source scan is a rule that quietly stops matching, and with
    ``missing`` and ``stale`` both empty that reads as a pass.
    """
    keys = _component_mapping_keys()
    assert len(keys) >= 28, f"the scan found too few keys: {sorted(keys)}"
    # Named keys, from each of the two write forms.
    # Names written into a mapping *literal* by the composition.  `agent` was one of them
    # and is not any more: `AgentPlugin` provides it, which the `ctx.provide` half of the
    # scan covers -- asserting it here would be asserting the old shape.
    for expected in ("registry", "cfg", "max_tokens", "llm"):
        assert expected in keys, f"{expected} is written by bootstrap; scan missed it"


def test_a_metadata_write_is_not_counted_as_a_service_write() -> None:
    """Control for the exclusion that corrected four false owners.

    ``ctx.metadata["skill_catalog"] = x`` copies a reference into a per-turn mapping.
    The read path excluded metadata receivers and the write path did not, so three
    files were listed as *owners* of ``skill_catalog`` and one of ``context_manager``
    on the strength of a copy.  A ledger that says a file constructs a service it only
    references makes the next reader avoid replacing it -- which is the opposite of
    what the ledger is for.
    """
    source = 'ctx.metadata["skill_catalog"] = x\ncomponents["skill_catalog"] = y\n'
    tree = ast.parse(source)
    counted = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    if _is_metadata_receiver(target.value):
                        continue
                    counted.append(_string_key(target))
    assert counted == ["skill_catalog"], (
        "the metadata write must be skipped and the component write counted; "
        f"counted {counted}"
    )


def test_the_agent_is_given_only_the_names_it_looks_up() -> None:
    """``BaseAgent`` resolves ``llm`` by name; that is the whole of what it needs.

    It used to be handed the **component mapping** -- the composition's whole vocabulary
    -- and looked ``llm`` up inside it.  The lookup is what makes the model adapter a
    seam a composition can substitute without editing the code that builds the agent, so
    the lookup stays; what changed is how much it can reach through.  A mapping is a
    channel that answers "what does this class need?" with "everything".

    Asserted against the source, because the width of an argument is only visible where
    it is passed.  A future edit that hands the mapping back should have to argue with
    this test.
    """
    source = (REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BaseAgent"
    ]
    assert constructions, "the scan must find the agent being built"

    for node in constructions:
        services = next((k for k in node.keywords if k.arg == "services"), None)
        if services is None:
            continue  # a construction that passes no mapping claims no names
        assert isinstance(services.value, ast.Dict), (
            "the agent's `services` must be a literal naming what it resolves, not a "
            f"variable holding the composition: {ast.unparse(services.value)[:60]}"
        )
        names = {
            key.value
            for key in services.value.keys
            if isinstance(key, ast.Constant)
        }
        assert names <= {"llm"}, (
            f"the agent resolves {sorted(names)} by name; declare what it needs here "
            "rather than handing it the mapping"
        )


def test_the_service_primitive_has_exactly_one_import_path() -> None:
    """The re-export was kept "only while the callers are migrated"; the migration is done.

    `agent/runtime/services.py` existed to make the move verifiable as behaviour-preserving
    -- a suite that stayed green across it distinguishes "I broke the registry" from "I
    moved it" -- and it said in its own docstring that it would be deleted once nothing
    imported it.  Eighteen modules imported it, so "nothing" was a long way off; all of them
    now import the kernel directly.

    Asserted structurally, because the failure mode is a *new* file reaching for the old
    path out of habit, and an import that resolves is not an error anybody notices.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    assert not (root / "agent/runtime/services.py").exists(), (
        "the re-export is gone; nothing should recreate it"
    )

    # Parsed, not searched for a substring: the first version of this matched its own
    # source, because the assertion text names the module it is checking for.  A scan that
    # cannot tell a mention from an import is the kind of guard that reports itself.
    offenders = []
    for path in list((root / "agent").rglob("*.py")) + list((root / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8", errors="ignore"))):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if any(
                module == "agent.runtime.services"
                or module.startswith("agent.runtime.services.")
                for module in modules
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"these still import the removed re-export: {offenders}"
