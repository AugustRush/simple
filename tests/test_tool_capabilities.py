"""A capability a plugin contributes, and the undo that comes with it.

The plan's model is ``Plugin → Service / Event / Capability``, where a capability is
what a **service** exposes as callable rather than a universal abstraction.  A tool is
the clearest example: the plugin does not *become* a tool, it registers one on the
registry, and the registration goes away with the plugin.

The plan's own example is what these tests drive:

    class GitHubPlugin:
        inject = ["registry"]

        def setup(self, ctx):
            ctx.registry.register(CreateIssue(...))
            ctx.registry.register(CreatePR(...))

so the acceptance question is not "does the registry work" -- it has its own tests --
but "does reaching it through a context make the registrations reversible, and can a
plugin author tell what they registered".

A note on the service's name
----------------------------
The plan writes ``ctx.tools``; this codebase calls the service ``registry``.  The name
was left alone: it is read as a service in seven places but appears as a component key
in eighty-nine, and the *requirement* -- a plugin registers a capability on a service,
reversibly, without inventing a source string -- does not depend on the spelling.  The
plan's ``ctx.tools`` is illustrative of the shape, and the shape is what is tested
here.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.kernel.plugin import PluginHost
from agent.ralph.plugin import RalphPlugin
from agent.tools.runtime import ToolRegistry, ToolView


class _Tool:
    """A tool object: the shape a plugin contributes."""

    def __init__(self, name: str, *, description: str = "", capabilities=None) -> None:
        self.name = name
        self.description = description
        self.parameters = {"type": "object", "properties": {}}
        self.capabilities = capabilities

    def fn(self, **kwargs):
        return f"{self.name} ran"


def _host():
    registry = ToolRegistry()
    host = PluginHost()
    # The service the composition provides. `registry` is the key the runtime uses.
    host.services.provide("registry", registry)
    return host, registry


class _ToolPlugin:
    """A plugin that contributes tools the way the plan's example does."""

    name = "tool_plugin"
    inject = ("registry",)

    def __init__(self, *tool_names: str) -> None:
        self._names = tool_names

    def setup(self, ctx):
        self.ctx = ctx
        for tool_name in self._names:
            ctx.registry.register(_Tool(tool_name))


# ── The capability is contributed ────────────────────────────────────────────


def test_a_plugin_registers_a_tool_through_its_context() -> None:
    """The plan's example, verbatim in shape."""
    host, registry = _host()
    host.load(_ToolPlugin("create_issue"))
    assert "create_issue" in registry.list_tools()


def test_the_source_is_derived_from_the_plugin_not_invented() -> None:
    """``register`` takes a source string; a plugin author should not have to.

    The registry needs one to answer "who registered this" and to remove a group.
    Through a context it is the plugin's name, so the two cannot disagree -- which is
    the failure the string form invites: a source that says one thing while the
    unregister call says another.
    """
    host, registry = _host()
    host.load(_ToolPlugin("create_issue"))
    assert registry.tool_source("create_issue") == "plugin:tool_plugin"


def test_a_tool_object_carries_its_own_description_and_capabilities() -> None:
    host, registry = _host()

    class _Fancy(_Tool):
        def __init__(self):
            super().__init__("fancy", description="does fancy things", capabilities=["io"])

    class _P:
        name = "p"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Fancy())

    host.load(_P())
    assert set(registry.tool_capabilities("fancy")) == {"io"}


def test_a_tool_without_a_name_is_refused() -> None:
    """Named at registration, not at call time: a nameless tool is unreachable."""
    host, registry = _host()

    class _P:
        name = "p"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(object())

    install = host.load(_P())
    assert install.error, "a nameless tool must fail the plugin, not land silently"
    assert list(registry.list_tools()) == []


# ── The undo comes with it ───────────────────────────────────────────────────


def test_unloading_the_plugin_removes_its_tools() -> None:
    """The whole point: the plugin never names a source, and the tools still go."""
    host, registry = _host()
    host.load(_ToolPlugin("create_issue", "create_pr"))
    assert {"create_issue", "create_pr"}.issubset(set(registry.list_tools()))

    host.unload("tool_plugin")
    assert "create_issue" not in registry.list_tools()
    assert "create_pr" not in registry.list_tools()


def test_two_plugins_contribute_independently() -> None:
    """Each view records on its own plugin's lifetime, so unloads do not interact."""
    host, registry = _host()

    class _Other:
        name = "other"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Tool("other_tool"))

    host.load(_ToolPlugin("mine"))
    host.load(_Other())
    assert {"mine", "other_tool"}.issubset(set(registry.list_tools()))

    host.unload("tool_plugin")
    assert "mine" not in registry.list_tools()
    assert "other_tool" in registry.list_tools(), "the other plugin kept its own"


def test_a_plugin_that_raises_halfway_keeps_none_of_its_tools() -> None:
    """A failed setup disposes the scope, so the first tool goes with the second."""
    host, registry = _host()

    class _HalfBroken:
        name = "half"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Tool("first"))
            raise RuntimeError("setup exploded")

    install = host.load(_HalfBroken())
    assert install.error
    assert list(registry.list_tools()) == [], "a half-registered plugin is not a state"


def test_reloading_replaces_rather_than_colliding() -> None:
    """A plugin claiming its own name again is the ordinary reload, not a conflict.

    The registry refuses a duplicate by default, to catch two *different* sources
    claiming one name.  A plugin replacing itself is not that, so the scoped view
    defaults to replacing -- otherwise every plugin author would write ``replace=True``
    to say something the context already knows.
    """
    host, registry = _host()
    host.load(_ToolPlugin("create_issue"))
    host.load(_ToolPlugin("create_issue"), replace=True)
    assert "create_issue" in registry.list_tools()


def test_a_second_plugin_claiming_the_name_still_collides() -> None:
    """The registry's guard is not weakened by the scoped view's default."""
    host, registry = _host()
    host.load(_ToolPlugin("shared"))

    class _Thief:
        name = "thief"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Tool("shared"))

    install = host.load(_Thief())
    assert install.error, "a different source claiming the name must be refused"
    assert registry.tool_source("shared") == "plugin:tool_plugin"


# ── The contribution service delegates rather than duplicating ───────────────


# ── Every contribution is scope-owned, not name-filtered ─────────────────────


def test_unload_takes_every_kind_of_contribution(tmp_path) -> None:
    """Not by filtering lists on a name -- by the scope the contributions recorded on.

    ``_remove_plugin_owned_state`` used to filter skills, MCP configs and agent
    definitions by plugin name, and the slash commands and tools before that.  Those
    filters were removed once each contribution recorded its own removal, and this is
    what says the removal actually covers them: without it, a filter that stopped
    matching and a scope that never recorded anything would look the same, and the
    symptom would be a plugin whose assets outlive it.

    One plugin, all five kinds, one unload.
    """
    import textwrap

    from agent.plugins.catalog import PluginCatalog

    root = tmp_path / "plugins"
    plugin = root / "contrib"
    (plugin / "skills").mkdir(parents=True)
    (plugin / "agents").mkdir()
    (plugin / "agents" / "auditor.md").write_text(
        "---\nname: auditor\n---\nbody\n", encoding="utf-8"
    )
    (plugin / "plugin.json").write_text(
        '{"name": "contrib", "mcpServers": [{"name": "contrib-mcp", "command": "true"}]}',
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(textwrap.dedent('''
        class _T:
            name = "contrib_tool"
            description = "a tool"
            parameters = {"type": "object", "properties": {}}
            def fn(self, **kw):
                return "ok"

        def register():
            class P:
                name = "contrib"
                inject = ("commands", "registry")
                def setup(self, ctx):
                    ctx.commands.add("contrib_cmd", lambda **kw: "ok")
                    ctx.registry.register(_T())
            return P()
    '''), encoding="utf-8")

    catalog = PluginCatalog(builtin_dir=root)
    registry = ToolRegistry()
    # Provided as a *service*, which is how a composition offers it and how the
    # plugin's ``inject`` finds it.  ``_tool_registry`` was the old private channel,
    # used only by the contribution service that is now gone.
    catalog._host.services.provide("registry", registry)
    # The skills capability too: a bundle contributes its skills root through it in its
    # own setup, so without one the bundle stays pending (see `test_plugin_catalog`).
    from agent.skills.catalog import SkillCatalog

    skills = SkillCatalog(
        user_root=tmp_path / "user-skills", builtin_root=tmp_path / "builtin-skills"
    )
    skills.load_all()
    catalog._host.services.provide("skill_catalog", skills)
    try:
        assert catalog.discover_and_load() == ["contrib"]

        # Everything the plugin contributed is present.
        assert catalog._commands.has("contrib_cmd")
        assert "contrib_tool" in registry.list_tools()
        assert catalog._agent_defs.get("plugin:contrib:auditor") is not None
        assert skills.contributed_roots(), "the skills root was contributed"
        assert catalog.get_bundled_mcp(), "the mcp config was registered"

        catalog.unload_plugin("contrib")

        # And every one of them is gone, without anything having filtered by name.
        assert not catalog._commands.has("contrib_cmd"), "the slash command"
        assert "contrib_tool" not in registry.list_tools(), "the tool"
        assert (
            catalog._agent_defs.get("plugin:contrib:auditor") is None
        ), "the agent definition"
        assert skills.contributed_roots() == [], "the skills root"
        assert not [
            entry for entry in catalog.get_bundled_mcp() if entry[0] == "contrib"
        ], "the mcp config"
    finally:
        catalog.close()


# ── Sub-agent roles: looked up at runtime, so replaceable matters ────────────


def _role_host():
    from agent.core.agent_registry import AgentRegistry

    registry = AgentRegistry()
    host = PluginHost()
    host.services.provide("agents", registry)
    return host, registry


class _RolePlugin:
    name = "role_plugin"
    inject = ("agents",)

    def __init__(self, *names: str) -> None:
        self._names = names

    def setup(self, ctx):
        for name in self._names:
            ctx.agents.add(name, {"description": f"the {name} role", "body": f"# {name}"})


def test_a_plugin_declares_a_role_through_its_context() -> None:
    host, registry = _role_host()
    host.load(_RolePlugin("reviewer"))
    assert registry.get("plugin:role_plugin:reviewer") is not None


def test_the_key_and_the_plugin_stamp_are_derived_together() -> None:
    """The drift the old form allowed: a key assembled in one place, a stamp in another.

    The loader built ``plugin:<name>:<stem>`` itself and stored ``"plugin": name``
    beside it; the Python contribution did the same by hand.  Two spellings of one
    fact, and the runtime consumer had to parse the key to learn what the stamp
    already said.
    """
    host, registry = _role_host()
    host.load(_RolePlugin("reviewer"))
    record = registry.get("plugin:role_plugin:reviewer")
    assert record["plugin"] == "role_plugin"
    assert record["body"] == "# reviewer"


def test_unloading_the_plugin_removes_its_roles() -> None:
    host, registry = _role_host()
    host.load(_RolePlugin("reviewer", "auditor"))
    assert set(registry.keys()) == {
        "plugin:role_plugin:reviewer",
        "plugin:role_plugin:auditor",
    }
    host.unload("role_plugin")
    assert registry.keys() == ()


def test_a_plugin_declaring_two_roles_removes_both() -> None:
    """Two registrations, two removals -- asserted because the hand-written version
    needed a per-iteration default (``lambda k=key: ...``) to get this right, and a
    late-bound key removes only the last one.

    What this test establishes is the *behaviour*.  The reason the behaviour is now
    hard to get wrong is structural and not something a test can show: ``add`` returns
    the undo, so there is no lambda over a loop variable to bind wrongly.  Stated here
    rather than claimed as proven.
    """
    host, registry = _role_host()
    host.load(_RolePlugin("one", "two"))
    assert len(registry.keys()) == 2
    host.unload("role_plugin")
    assert registry.keys() == ()


def test_a_role_is_listed_with_its_key_for_the_agents_listing() -> None:
    host, registry = _role_host()
    host.load(_RolePlugin("reviewer"))
    listed = registry.all()
    assert len(listed) == 1
    assert listed[0]["key"] == "plugin:role_plugin:reviewer"


def test_a_role_snapshot_does_not_alias_the_live_record() -> None:
    """A failed reload restores the snapshot; an aliased record would restore the
    broken state it had already written into."""
    host, registry = _role_host()
    host.load(_RolePlugin("reviewer"))
    snapshot = registry.snapshot()

    registry.add("plugin:role_plugin:reviewer", {"body": "changed", "plugin": "someone"})
    registry.restore(snapshot)

    assert registry.get("plugin:role_plugin:reviewer")["body"] == "# reviewer"
    assert registry.get("plugin:role_plugin:reviewer")["plugin"] == "role_plugin"


def test_the_role_key_format_has_one_home() -> None:
    """The runtime consumer recognises roles by prefix; the format is written down.

    ``subagent_prepare`` checks ``role.startswith("plugin:")`` and then asks the
    catalog for the definition.  A format that two modules agree on needs one place
    that states it, or the day it changes is the day one of them does not.
    """
    from agent.core.agent_registry import role_key

    assert role_key("github", "reviewer") == "plugin:github:reviewer"
    assert role_key("github", "reviewer").startswith("plugin:")


def test_the_capability_services_are_provided_by_the_catalog() -> None:
    """A plugin reaches ``ctx.commands`` because the catalog provides it.

    The measurement behind this: the ``assets`` contribution service existed so a
    plugin could contribute, and **no plugin ever called it** -- its only caller was
    the catalog's own loader, and two of its five methods had no caller at all.  The
    capability services replaced it, so what has to hold is that they are *reachable*
    by name: a service nothing provides is the same as one that does not exist.
    """
    import tempfile
    from pathlib import Path

    from agent.plugins.catalog import PluginCatalog

    with tempfile.TemporaryDirectory() as tmp:
        catalog = PluginCatalog(builtin_dir=Path(tmp))
        try:
            for name, service in (("commands", catalog._commands), ("agents", catalog._agent_defs)):
                assert catalog._host.services.get(name, None) is service, (
                    f"{name} must be the composition's service, reachable by a "
                    f"plugin's inject"
                )
            assert catalog._host.services.get("assets", None) is None, (
                "the contribution service is gone; a plugin contributes through the "
                "capability registries"
            )
        finally:
            catalog.close()


def test_a_plugin_registers_a_command_and_a_role_by_injecting_them(tmp_path) -> None:
    """The plan's idiom, end to end: declare the services, use them, unload cleanly."""
    import textwrap

    from agent.plugins.catalog import PluginCatalog

    root = tmp_path / "plugins"
    plugin = root / "uses_capabilities"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(textwrap.dedent('''
        def register():
            class P:
                name = "uses-capabilities"
                inject = ("commands", "agents")
                def setup(self, ctx):
                    ctx.commands.add("cap_cmd", lambda **kw: "ok")
                    ctx.agents.add("reviewer", {"body": "# reviewer"})
            return P()
    '''), encoding="utf-8")

    catalog = PluginCatalog(builtin_dir=root)
    try:
        assert catalog.discover_and_load() == ["uses-capabilities"]
        assert catalog._commands.owner("cap_cmd") == "uses-capabilities"
        assert catalog._agent_defs.get("plugin:uses-capabilities:reviewer") is not None

        catalog.unload_plugin("uses-capabilities")
        assert catalog._commands.names() == ()
        assert catalog._agent_defs.keys() == ()
    finally:
        catalog.close()


# ── An execution piece as a plugin: Ralph ────────────────────────────────────


def _ralph_deps(**overrides):
    """The services Ralph declares, so a test can mount it without a whole runtime."""
    values = {
        "agent": object(),
        "system_prompt": "sys",
        "skill_catalog": object(),
        "context_manager": object(),
        "workspace_root": "/tmp",
        "output_dir": "/tmp",
        "cfg": {},
    }
    values.update(overrides)
    host = PluginHost()
    for name, value in values.items():
        if value is not None:
            host.services.provide(name, value)
    return host


def test_an_execution_piece_is_absent_until_it_is_mounted() -> None:
    """The point of Ralph being a plugin: a composition can not have one.

    It was already a *service* -- ``commands/builtin.py`` resolves ``ralph_service`` --
    but ``bootstrap`` built it inline, so every process got a Ralph, its task store and
    its verifier whether or not anybody ran ``/ralph``.  Now the absence is a state a
    composition can be in, and ``/ralph`` reports a missing service instead of
    half-working.
    """
    host = _ralph_deps()
    assert host.services.get("ralph_service", None) is None

    host.load(RalphPlugin())
    assert type(host.services.get("ralph_service", None)).__name__ == "RalphService"


def test_unloading_ralph_takes_the_service_away() -> None:
    host = _ralph_deps()
    host.load(RalphPlugin())
    host.unload("ralph")
    assert host.services.get("ralph_service", None) is None


def test_ralph_waits_rather_than_half_activating() -> None:
    """Six dependencies unmet, so it registers nothing.

    A composition with no workspace cannot verify anything, and a Ralph that activated
    anyway would fail at the first check -- which is the failure mode ``inject`` exists
    to move from turn time to composition time.
    """
    host = _ralph_deps(system_prompt=None, skill_catalog=None, context_manager=None,
                       workspace_root=None, output_dir=None, cfg=None)
    host.load(RalphPlugin())
    assert host.pending() == ("ralph",)
    assert host.services.get("ralph_service", None) is None
    assert set(host.waiting_for()["ralph"]) == {
        "system_prompt", "skill_catalog", "context_manager",
        "workspace_root", "output_dir", "cfg",
    }


def test_ralph_declares_everything_it_reaches_for() -> None:
    """A short list would mean it reached for the rest behind the context's back.

    Ralph composes the others: it drives turns through the agent, verifies against the
    filesystem, and builds its iteration context from the system prompt and the skill
    catalog.  Each of those is a name in ``inject``, so a missing one is visible.
    """
    assert set(RalphPlugin.inject) == {
        "agent", "system_prompt", "skill_catalog", "context_manager",
        "workspace_root", "output_dir", "cfg",
    }
    assert RalphPlugin.name == "ralph"


def _scoped_view(registry, name="demo"):
    """A view built the way the kernel builds one, so `owner` is real."""
    from agent.kernel.plugin import PluginHost
    from agent.kernel.service import ServiceRegistry

    class _Plugin:
        pass

    _Plugin.name = name

    def setup(ctx):
        ctx.provide("tools_view", ctx.get("registry"))

    _Plugin.setup = staticmethod(setup)
    _Plugin.inject = ("registry",)

    mapping = {"registry": registry}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(_Plugin())
    return mapping["tools_view"]


def test_a_scoped_view_accepts_the_registry_positional_convention() -> None:
    """A view that stands where the registry stands must be usable as one.

    ``BaseAgent.register_spawn_capability`` registers through the registry's own
    convention -- ``register(name, description, parameters, fn, source=...)``.  Handed a
    scoped view, that call raised ``TypeError``: the view accepted only a tool object, so
    it was *almost* a registry, and the near-miss appeared only where the two were swapped.
    """
    registry = ToolRegistry()
    view = _scoped_view(registry)

    view.register(
        "spawn_agent",
        "Spawn a sub-agent.",
        {"type": "object", "properties": {}},
        lambda **_: None,
        source="runtime:spawn",
    )

    assert "spawn_agent" in registry.list_tools()
    assert registry._tools["spawn_agent"].source == "runtime:spawn"


def test_a_scoped_view_still_takes_a_tool_object() -> None:
    """Control: the plan's form keeps working, and its source is the plugin's name."""
    class _Tool:
        name = "create_issue"
        description = "Open one."
        parameters = {"type": "object", "properties": {}}

        def fn(self, **_):
            return "ok"

    registry = ToolRegistry()
    view = _scoped_view(registry, name="github")
    view.register(_Tool())

    assert "create_issue" in registry.list_tools()
    assert registry._tools["create_issue"].source == "plugin:github"


def test_the_scoped_view_accepts_every_keyword_the_registry_does() -> None:
    """The structural fix for a bug that arrived twice.

    `ScopedTools.register` stands where `ToolRegistry.register` stands.  Twice now it was
    *almost* that: it refused `source=` (which the agent's `spawn_agent` passes) and then
    `authorizer=` (which `BuiltinTools` passes for file mutations), and both times the
    symptom was a plugin failing to load rather than a test failing.

    So the requirement is stated as a comparison instead of a list: every keyword the
    registry accepts, the view accepts.  A new parameter on the registry now fails **here**,
    which is the moment to decide what the view should do with it -- rather than in whatever
    plugin happens to be holding a view when somebody calls it.
    """
    import inspect

    from agent.tools.runtime import ToolRegistry
    from agent.tools.scoped import ScopedTools

    registry_params = inspect.signature(ToolRegistry.register).parameters
    view_params = inspect.signature(ScopedTools.register).parameters

    registry_keywords = {
        name
        for name, param in registry_params.items()
        if name != "self" and param.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
    } | {
        name
        for name, param in registry_params.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    view_keywords = {
        name
        for name, param in view_params.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }

    missing = sorted(registry_keywords - view_keywords)
    assert not missing, (
        f"the scoped view does not accept {missing}, which the registry does; a call "
        "written against the registry would fail when handed a view"
    )


def test_the_view_forwards_the_authorizer_it_was_given() -> None:
    """Control for the comparison above: accepting a keyword is not the same as using it.

    An accepted-and-dropped `authorizer` would look identical from the signature and would
    silently disable the file-mutation gate.
    """
    from agent.tools.runtime import ToolRegistry
    from agent.tools.scoped import ScopedTools

    registry = ToolRegistry()
    view = _scoped_view(registry)
    seen: list = []

    def authorizer(call, reg):
        seen.append(call)
        return None

    view.register(
        "write_file",
        "Write one.",
        {"type": "object", "properties": {}},
        lambda **_: "ok",
        authorizer=authorizer,
    )

    tool = registry._tools["write_file"]
    assert tool.authorizer is authorizer


# ── The caller's context reaches a contributed tool ──────────────────────────


class _ContextReader:
    """Reads the registry context the way the built-in tools do.

    ``BuiltinTools`` reads ``workspace_root``, ``output_dir``, ``file_access_policy``,
    ``write_scope`` and the shell policies this way, so a tool that answers with one of
    them is a faithful stand-in for the largest implementation in the project.
    """

    def __init__(self, registry, key: str = "write_scope") -> None:
        self.registry = registry
        self.name = "context_reader"
        self.description = "Reports one registry context value."
        self.parameters = {"type": "object", "properties": {}}
        self.capabilities = None
        self._key = key

    def fn(self):
        return self.registry.get_context(self._key, "unset")


class _ReaderPlugin:
    """Contributes the reader, holding the registry *view* -- as ``ToolsPlugin`` does."""

    name = "reader_plugin"
    inject = ("registry",)

    def __init__(self, key: str = "write_scope", holdings=None) -> None:
        self._key = key
        self._holdings = holdings

    def setup(self, ctx):
        registry = ctx.registry if self._holdings is None else self._holdings(ctx)
        ctx.registry.register(_ContextReader(registry, self._key))


def test_a_derived_context_reaches_a_tool_contributed_through_a_view() -> None:
    """A narrower scope has to reach the tool, or it is not a scope.

    ``ToolRegistry.call`` merges the *caller's* context with the owner registry's and
    installs the result as a per-call override, because the tool reads its context from
    the registry it holds -- which, for a contributed tool, is a plugin's view, not the
    registry that was asked.  When the override is installed on the wrong object the merge
    does not happen and the tool reads the *composition's* context instead.

    What that costs is not cosmetic: a sub-agent spawned with a narrowed ``write_scope``
    and a Web session with its own workspace both read the composition's values, so a
    restriction that was asked for is silently not applied.
    """
    host, registry = _host()
    host.load(_ReaderPlugin("write_scope"))

    narrowed = registry.derive(ToolView(), {"write_scope": ["only/here"]})
    seen = asyncio.run(narrowed.call("context_reader", {}))
    assert seen == json.dumps(["only/here"]), (
        f"the tool read {seen!r}: the caller's context never reached it"
    )


def test_the_composition_context_is_still_read_when_the_caller_has_none() -> None:
    """Control: merging must not replace the owner's context with nothing.

    A tool called on the registry that owns it has no caller context to merge, and must
    keep reading what the composition set.
    """
    host, registry = _host()
    host.load(_ReaderPlugin("write_scope"))
    registry.set_context("write_scope", ["composition"])

    seen = asyncio.run(registry.call("context_reader", {}))
    assert seen == json.dumps(["composition"]), seen


def test_the_write_scope_gate_reads_the_caller_registry_by_hand() -> None:
    """Control, and the reason the context-merge defect was not a security hole.

    ``_file_mutation_authorizer`` asks the registry it is handed for
    ``capability_profile`` and ``write_scope`` -- and ``ToolRegistry.call`` hands it the
    **calling** registry, not the tool's owner.  So the gate reads the caller's scope even
    when the per-call context merge is not installed on the owner, which is what kept a
    sub-agent's narrowed scope enforced while every *other* context read was silently
    falling back to the composition's.

    Worth pinning because the two paths look alike: both read ``write_scope``, and only one
    of them can be broken by a view standing in for the registry.
    """
    from agent.tools.builtin_tools import BuiltinTools

    host, registry = _host()
    tools = BuiltinTools(memory=None, registry=host.services.get("registry"))
    narrowed = registry.derive(
        ToolView(),
        {"capability_profile": "implementation", "write_scope": ["allowed/only"]},
    )

    denial = tools._file_tools.file_mutation_authorizer(
        {"root": "workspace", "path": "outside.txt"}, narrowed
    )

    assert denial is not None, "a write outside the caller's write_scope is refused"
    assert "write scope" in json.dumps(denial), denial

    # Control: the same tool, called by a registry whose scope does contain the path.
    permitted = registry.derive(
        ToolView(),
        {"capability_profile": "implementation", "write_scope": ["outside.txt"]},
    )
    assert (
        tools._file_tools.file_mutation_authorizer(
            {"root": "workspace", "path": "outside.txt"}, permitted
        )
        is None
    ), "the gate must not refuse a path the caller's scope allows"
