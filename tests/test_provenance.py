"""Where a capability came from, asked of the composition rather than declared per site.

The design that asked for this
(`docs/superpowers/specs/2026-10-12-capability-provenance-and-adapter-translators-design.md`)
proposed passing ``provenance=`` at every ``provide`` call.  These tests exist mostly to
hold the *corrected* shape in place: the kernel already knows the declaring plugin at
``provide`` time, the loader already knows the source, so provenance is a record on the
plugin and a capability's provenance is **derived**.  A test that asserted thirty call
sites passed a keyword would have locked in the mirror instead.
"""

from __future__ import annotations

import pathlib

import pytest

from agent.runtime.provenance import CORE, IMPORT, PLUGIN, Execution, Provenance


def _write_plugin(root: pathlib.Path, name: str, *, manifest: bool = True) -> pathlib.Path:
    """A plugin directory in the shape the native adapter reads."""
    plugin_dir = root / name
    if manifest:
        (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            f'{{"name": "{name}"}}', encoding="utf-8"
        )
    else:
        plugin_dir.mkdir(parents=True, exist_ok=True)
    # ``register()`` is the adapter's convention: a module-level function returning the
    # plugin.  A bare class in ``__init__.py`` is ignored, and with a manifest present the
    # plugin silently becomes declarative-only -- which is how the first version of these
    # tests "loaded" a plugin whose ``setup`` never ran.
    (plugin_dir / "__init__.py").write_text(
        f"class P:\n"
        f"    name = {name!r}\n"
        f"    inject = ()\n"
        f"\n"
        f"    def setup(self, ctx):\n"
        f"        pass\n"
        f"\n"
        f"\n"
        f"def register():\n"
        f"    return P()\n",
        encoding="utf-8",
    )
    return plugin_dir


# ── the types ────────────────────────────────────────────────────────────────


def test_a_provenance_is_frozen() -> None:
    """A record of a decision made earlier must not drift from what it describes."""
    record = Provenance(source="core:memory", kind=CORE)

    with pytest.raises(Exception):
        record.source = "elsewhere"  # type: ignore[misc]


def test_an_execution_describes_itself_for_a_human() -> None:
    """The rendering a confirmation prompt needs, and nothing more.

    ``kind`` names the mechanism rather than the ecosystem: two formats that both spawn a
    process over stdio produce the same kind, because a person being asked is answering
    the same question either way.
    """
    assert Execution(kind="mcp-stdio", payload={"command": ["npx", "-y", "srv"]}).describe() == (
        "mcp-stdio: npx -y srv"
    )
    assert Execution(kind="python-entry", payload={"path": "a/b.py"}).describe() == (
        "python-entry: a/b.py"
    )
    # Nothing to run: the mechanism is the whole answer, and an empty trailing colon
    # would read like a missing value.
    assert Execution(kind="npm-package").describe() == "npm-package"


def test_the_three_kinds_are_named_not_guessed() -> None:
    """``import`` exists as vocabulary before anything produces it."""
    assert (CORE, PLUGIN, IMPORT) == ("core", "plugin", "import")
    assert Provenance(source="core:memory", kind=CORE).is_core is True
    assert Provenance(source="/tmp/x").is_core is False


# ── where a plugin came from ─────────────────────────────────────────────────


def test_a_builtin_plugin_is_core_and_says_so_by_name(tmp_path) -> None:
    """Core is a *label a human reads*, not only a boolean: ``core:memory``.
    """
    from agent import PluginCatalog

    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "alpha")
    catalog = PluginCatalog(builtin_dir=builtin)
    try:
        assert catalog.discover_and_load() == ["alpha"]

        record = catalog.provenance("alpha")

        assert record is not None
        assert record.kind == CORE
        assert record.source == "core:alpha"
        assert record.is_core is True
        assert record.declared_by == "", "discovery loads roots; there is no declaring plugin"
    finally:
        catalog.close()


def test_a_user_plugin_says_where_it_is(tmp_path) -> None:
    """A path a person can open, not an opaque id -- that is the point of provenance."""
    from agent import PluginCatalog

    user = tmp_path / "user"
    plugin_dir = _write_plugin(user, "acme")
    catalog = PluginCatalog(builtin_dir=tmp_path / "builtin", user_dir=user)
    try:
        assert catalog.discover_and_load() == ["acme"]

        record = catalog.provenance("acme")

        assert record is not None
        assert record.kind == PLUGIN
        assert record.source == str(plugin_dir)
        assert pathlib.Path(record.source).is_dir(), "the source has to be a place"
    finally:
        catalog.close()


def test_the_plugin_in_the_tree_is_the_one_reported(tmp_path) -> None:
    """A user plugin replacing a builtin of the same name replaces its provenance too.

    Answering ``core:alpha`` here would describe a plugin that is not loaded, which is the
    class of lie the whole step is about.
    """
    from agent import PluginCatalog

    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    _write_plugin(builtin, "alpha")
    user_dir = _write_plugin(user, "alpha")
    catalog = PluginCatalog(builtin_dir=builtin, user_dir=user)
    try:
        catalog.discover_and_load()

        record = catalog.provenance("alpha")

        assert record is not None and record.kind == PLUGIN
        assert record.source == str(user_dir)
    finally:
        catalog.close()


def test_an_undiscovered_plugin_has_no_provenance_rather_than_a_placeholder(tmp_path) -> None:
    """``None`` and "unknown" must not be the same answer.

    A placeholder would be indistinguishable from a plugin whose origin genuinely is
    unknown -- which is the state this step exists to make impossible.
    """
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:
        catalog.discover_and_load()

        assert catalog.provenance("not_a_plugin") is None
        assert catalog.provenance_of("not_a_service") is None
        assert catalog.provenance_report() == {}
    finally:
        catalog.close()


def test_unloading_a_plugin_takes_its_provenance_with_it(tmp_path) -> None:
    """Provenance is bookkeeping *about a plugin*; it goes when the plugin does."""
    from agent import PluginCatalog

    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "alpha")
    catalog = PluginCatalog(builtin_dir=builtin)
    try:
        catalog.discover_and_load()
        assert catalog.provenance("alpha") is not None

        catalog.unload_plugin("alpha")

        assert catalog.provenance("alpha") is None
        assert catalog.provenance_report() == {}
    finally:
        catalog.close()


def test_a_plugin_loaded_by_another_plugin_inherits_and_names_its_parent(tmp_path) -> None:
    """A child has no directory of its own, so it inherits -- and says whose code it is.

    Not a guess: the kernel recorded the parent link when it loaded the child, and the
    child's code genuinely arrived with its parent.
    """
    from agent import PluginCatalog

    builtin = tmp_path / "builtin"
    _write_plugin(builtin, "parent")
    catalog = PluginCatalog(builtin_dir=builtin)
    try:
        catalog.discover_and_load()

        class _Child:
            name = "child"
            inject = ()

            def setup(self, ctx) -> None:
                pass

        # Loaded the way ``ctx.plugin(child)`` loads it: no directory of its own, and a
        # parent link the kernel records.
        catalog._host.load(_Child(), parent="parent")

        inherited = catalog.provenance("child")

        assert inherited is not None
        assert inherited.source == "core:parent", "the code came with the parent"
        assert inherited.declared_by == "parent", "and the parent is who declared it"
    finally:
        catalog.close()


# ── where a capability came from (derived) ───────────────────────────────────


def test_a_service_resolves_to_the_plugin_that_provided_it(tmp_path) -> None:
    """The derived half: capabilities do not carry provenance; the composition derives it."""
    from agent import PluginCatalog

    builtin = tmp_path / "builtin"
    plugin_dir = _write_plugin(builtin, "alpha")
    (plugin_dir / "__init__.py").write_text(
        "class P:\n"
        "    name = 'alpha'\n"
        "    inject = ()\n"
        "\n"
        "    def setup(self, ctx):\n"
        "        ctx.provide('alpha_service', object())\n"
        "\n"
        "\n"
        "def register():\n"
        "    return P()\n",
        encoding="utf-8",
    )
    catalog = PluginCatalog(builtin_dir=builtin)
    try:
        assert catalog.discover_and_load() == ["alpha"]
        catalog.register_core_services({"alpha_service": object()})

        record = catalog.provenance_of("alpha_service")

        assert record is not None
        assert record.source == "core:alpha"
    finally:
        catalog.close()


def test_the_report_covers_mounted_plugins_not_only_discovered_ones(tmp_path) -> None:
    """The bug the first version had: a report over ``self._plugins`` listed 1 of 11.

    Core plugins are mounted by the composition, not discovered from a manifest, so they
    have an install and no adapter entry.  A report that only walked the discovered table
    under-reported exactly the plugins whose origin is most certain.
    """
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:

        class _Mounted:
            name = "mounted"
            inject = ()

            def setup(self, ctx) -> None:
                pass

        catalog.mount(
            _Mounted(), provenance=Provenance(source="core:mounted", kind=CORE)
        )

        assert list(catalog.provenance_report()) == ["mounted"]
    finally:
        catalog.close()


def test_a_caller_that_claims_nothing_gets_no_provenance(tmp_path) -> None:
    """``mount`` without *provenance* is "nobody said", not "assume core".

    A test double has not claimed an origin, and inventing one would be the placeholder
    the previous test refuses.
    """
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:

        class _Anonymous:
            name = "anonymous"
            inject = ()

            def setup(self, ctx) -> None:
                pass

        catalog.mount(_Anonymous())

        assert catalog.provenance("anonymous") is None
    finally:
        catalog.close()


# ── the composition: no anonymous capability ─────────────────────────────────


def test_no_capability_in_the_real_composition_is_anonymous(monkeypatch, tmp_path) -> None:
    """Every plugin, and every declared service the composition actually has, has an origin.

    This is the invariant, asked of the composition rather than of a fixture.  It is the
    test that would have caught the first version's gap -- ``provenance_report`` walked the
    *discovered* table and listed 1 plugin of 11, because core plugins are mounted by the
    composition and never see an adapter.
    """
    import asyncio

    import agent as agent_module
    from agent import shared
    from agent.runtime.services import SERVICES
    from test_agent_integration import _minimal_cfg

    cfg = _minimal_cfg()
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg, announce=True: (object(), "fake-model", 1024),
    )
    for name in (
        "CONTEXT_DIR",
        "MEMORY_DIR",
        "PROMPTS_DIR",
        "SKILLS_DIR",
        "DEFAULT_OUTPUT_DIR",
        "USER_PLUGINS_DIR",
    ):
        monkeypatch.setattr(shared, name, tmp_path / name.lower())
        monkeypatch.setattr(agent_module, name, tmp_path / name.lower())

    components = asyncio.run(agent_module._build_components_async(cfg, announce=False))
    try:
        catalog = components["plugin_catalog"]
        report = catalog.provenance_report()

        # Substantive, not merely non-empty: a guard that passes on an empty tree is the
        # kind of assertion this whole session has been removing.  These three names are
        # mounted by the composition and never discovered, which is the case the first
        # version of the report missed.
        assert {"memory", "registry", "agent"} <= set(report), sorted(report)
        assert catalog.provenance_of("memory").source == "core:memory"
        assert catalog.provenance_of("agent").source == "core:agent"

        for plugin, record in report.items():
            assert record.source, f"{plugin} has a provenance with no source"
            assert record.kind in (CORE, PLUGIN), f"{plugin}: unexpected kind {record.kind}"

        # The capability half: a name the composition *has* must resolve to who provided
        # it.  Absent names are skipped -- absent and anonymous are different states, and
        # `mcp_client` is legitimately absent in a composition with no servers.
        absent = object()
        anonymous = []
        for entry in SERVICES:
            if catalog._core(entry.name, absent) is absent:
                continue
            if catalog.provenance_of(entry.name) is None:
                anonymous.append(entry.name)
        assert not anonymous, (
            "these names are in the composition and nothing can say where they came from: "
            f"{anonymous}"
        )
    finally:
        asyncio.run(agent_module._close_components(components))


# ── what it would run ────────────────────────────────────────────────────────


def _claude_plugin(tmp_path: pathlib.Path, *, mcp: bool = True, hooks: bool = True):
    """A plugin directory in Claude Code's layout, with code in every place it declares."""
    import json

    root = tmp_path / "widget"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "widget"}', encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    if mcp:
        (root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "one": {"command": "npx", "args": ["-y", "one-server"]},
                        "two": {"command": "uvx", "args": ["two-server"]},
                    }
                }
            ),
            encoding="utf-8",
        )
    if hooks:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(
            json.dumps(
                {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./check.sh"}]}]}}
            ),
            encoding="utf-8",
        )
    return root


def test_every_executable_location_is_reported_not_only_the_first(tmp_path) -> None:
    """The defect this step exists to fix, stated as a test.

    ``executable_content`` returned the first marker that matched -- the class of miss its
    own docstring calls worse than no gate, in the function the gate reads.  A plugin with
    a Python entry point *and* two MCP servers asked for consent to run one of the three
    things it ships.
    """
    from agent.plugins.formats import executions

    found = executions(_claude_plugin(tmp_path))
    markers = {item.payload["marker"] for item in found}

    assert "__init__.py" in markers, "the Python entry point"
    assert ".mcp.json" in markers, "the MCP servers"
    assert "hooks/hooks.json" in markers, "the hook commands"


def test_the_command_is_read_out_of_the_plugin_s_own_files(tmp_path) -> None:
    """What a person is asked about, not where it is written down.

    Showing ``.mcp.json`` asks them to go and open it; the file already says what runs.
    """
    from agent.plugins.formats import executions

    by_kind = {item.kind: item for item in executions(_claude_plugin(tmp_path))}

    assert by_kind["mcp-stdio"].command_summary() == "npx -y one-server (+1 more)"
    assert by_kind["hook-command"].command_summary() == "./check.sh"
    # A Python file runs its path; the payload says which, and there is no command line
    # to invent from it.
    assert by_kind["python-entry"].command_summary() == ""
    assert by_kind["python-entry"].describe() == "python-entry: __init__.py"


def test_the_count_is_not_silently_truncated(tmp_path) -> None:
    """"(+2 more)" is the difference between "runs one command" and "runs three"."""
    from agent.plugins.formats import executions

    mcp = next(
        item for item in executions(_claude_plugin(tmp_path)) if item.kind == "mcp-stdio"
    )

    assert "(+1 more)" in mcp.command_summary()


def test_the_installer_prompt_names_every_executable_and_keeps_its_wording(
    tmp_path,
) -> None:
    """One scan, one rendering, and the localized label is not lost to the refactor.

    The label is this CLI's wording and the structure is the report's; routing the prompt
    through the report's own English rendering silently replaced "MCP server" with
    "mcp-stdio" -- a regression no test noticed, because none of them pinned the words.
    This one does.
    """
    from agent.plugins.report import capability_report
    from agent.tools.plugin_tools import _plugin_executable_content, render_executions

    root = _claude_plugin(tmp_path)
    report = capability_report(root, source=str(root))

    rendered = render_executions(report.would_run)

    assert "MCP server (.mcp.json): npx -y one-server (+1 more)" in rendered
    assert "hooks (hooks/hooks.json): ./check.sh" in rendered
    assert "Python 入口 (__init__.py)" in rendered
    # The standalone entry point answers the same question, from its own scan.
    assert _plugin_executable_content(root) == rendered


def test_a_declarative_plugin_runs_nothing_and_says_so(tmp_path) -> None:
    """The control: no executables, no reason, nothing to confirm."""
    from agent.plugins.report import capability_report

    root = tmp_path / "plain"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "plain"}', encoding="utf-8")

    report = capability_report(root, source=str(root))

    assert report.would_run == ()
    assert report.executes is False
    assert report.reason() == ""


def test_a_report_carries_the_source_a_human_was_shown(tmp_path) -> None:
    """``source`` is what the person chose between, not an internal id."""
    from agent.plugins.report import capability_report

    report = capability_report(
        _claude_plugin(tmp_path),
        source="https://github.com/acme/widget",
        inert=("SessionStart",),
        approved=True,
    )

    assert report.source.source == "https://github.com/acme/widget"
    assert report.source.kind == PLUGIN
    assert report.executes is True
    assert report.approved is True
    # Inert declarations are reported, not hidden: nothing runs for them, and the person
    # should not be left believing otherwise.
    assert report.inert == ("SessionStart",)
    assert "inert" in report.reason()


def test_the_report_is_json_able_for_a_tool_result(tmp_path) -> None:
    """A producer that is not this CLI (the DSH import, an npm source) needs to ship it."""
    import json

    from agent.plugins.report import capability_report

    summary = capability_report(_claude_plugin(tmp_path), source="core:widget").summary()

    assert json.loads(json.dumps(summary))["source"] == "core:widget"
    assert len(summary["would_run"]) == 3
    assert all("kind" in entry for entry in summary["would_run"])
