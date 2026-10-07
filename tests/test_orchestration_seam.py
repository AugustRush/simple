"""Why Orchestration is not a composition-level service, pinned so the claim can be checked.

The objective lists Orchestration among the things to turn into a plugin.  It is not one,
and the reason is not size or boundaries -- it is that **its input is per-call state**.

``plan_orchestration`` builds an ``OrchestrationPlanner`` on every turn from
``ctx.metadata["skill_catalog"]``, and that key is written by four different driving paths:
the runtime's per-turn context, Ralph's loop, the CLI loop, and a channel session.  A
composition-time ``orchestration`` service would be built against one of them, and the
other three would be silently ignored -- a behaviour change, not a refactor.

These tests exist so that the claim is falsifiable.  If somebody later lifts the planner to
composition time, one of them has to be edited deliberately, and the edit is where the
question "what did the other three paths lose" gets asked.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _turn_loop_source() -> str:
    return (REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8")


def test_the_planner_is_built_per_call_from_the_call_s_own_metadata() -> None:
    """Not a service: the catalog comes from the context being served."""
    source = _turn_loop_source()
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "plan_orchestration"
    )
    reads = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "metadata"
        and any(
            isinstance(arg, ast.Constant) and arg.value == "skill_catalog"
            for arg in node.args
        )
    ]
    assert reads, (
        "plan_orchestration must read the call's metadata; if the planner no longer does, "
        "this measurement is out of date and the reason Orchestration is not a service "
        "has to be re-established"
    )
    assert "OrchestrationPlanner.from_skill_catalog" in source


def test_more_than_one_path_writes_that_metadata_key() -> None:
    """Which is what makes a composition-time service wrong rather than merely different.

    One writer would mean the planner could be built once for it.  Four writers, each on a
    different driving path, mean any single choice silently drops the others.
    """
    writers = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if 'metadata["skill_catalog"]' in source:
            writers.append(path.relative_to(REPO_ROOT).as_posix())
    assert len(writers) >= 4, (
        f"expected the four driving paths to set the catalog; found {writers}"
    )
    assert "agent/core/turn_loop.py" not in writers, (
        "the loop must *read* the catalog, not set it -- setting it there would make the "
        "planner's input look like loop state rather than the caller's"
    )


def test_it_is_called_from_inside_the_loop() -> None:
    """Per turn, not per composition: the call sits in the turn body."""
    source = _turn_loop_source()
    tree = ast.parse(source)
    callers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "plan_orchestration"
    ]
    assert callers, "the scan must find the call"
    assert len(callers) == 1


# ── and it *is* composable: a plugin changes the policy, through a service ─────


def _plugin_with_policy(tmp_path):
    """A plugin shipping a `multi-agent-orchestration` bundle, and the catalog holding it."""
    import json

    from agent import PluginCatalog
    from agent.skills.catalog import SkillCatalog

    plugin_dir = tmp_path / "policy-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "def register():\n"
        "    class P:\n"
        "        name = 'policy-plugin'\n"
        "    return P()\n",
        encoding="utf-8",
    )
    bundle = plugin_dir / "skills" / "multi-agent-orchestration"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\n"
        "name: Multi-Agent Orchestration\n"
        "description: Decide orchestration mode\n"
        "user-invocable: false\n"
        "disable-model-invocation: true\n"
        "default-mode: pipeline\n"
        "parallel-keywords: ['in parallel']\n"
        "pipeline-keywords: ['stage by stage']\n"
        "max-rendezvous-rounds: 5\n"
        "---\n"
        "Policy body.\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "policy-plugin", "skills": "./skills/"}), encoding="utf-8"
    )

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    skill_catalog = SkillCatalog(
        user_root=tmp_path / "none", builtin_root=tmp_path / "none"
    )
    skill_catalog.load_all()
    for name, root in catalog.get_bundled_skills():
        skill_catalog._load_root(root, source=f"plugin:{name}")
    return skill_catalog


def test_a_plugin_bundle_cannot_be_reached_by_the_bare_name(tmp_path) -> None:
    """The finding, which is a deliberate position rather than an oversight.

    A plugin's skill is namespaced ``plugin:<bundle>`` and gets no short alias, because one
    plugin must not hijack a name another one means.  ``from_skill_catalog`` looks up the
    bare id, so **the built-in bundle is what decides orchestration even when a plugin
    ships one with the same name** -- which is the safe default, since that decision applies
    to every turn in the process.

    Asserted rather than assumed: the first version of this file claimed the opposite, and
    the chain only showed it when the whole thing was run end to end.
    """
    from agent.orchestration.planner import OrchestrationPlanner

    catalog = _plugin_with_policy(tmp_path)

    bare = OrchestrationPlanner.from_skill_catalog(catalog)
    assert bare.default_mode == "direct", (
        "the built-in policy must still apply; a plugin's bundle must not take it over"
    )
    assert bare.max_rendezvous_rounds == 2


def test_a_composition_can_name_a_plugins_policy_explicitly(tmp_path) -> None:
    """And the way to choose it is to say so, in full.

    ``orchestration.policy_skill_id`` reaches the planner through the agent and
    ``LoopDeps``.  Naming the full id is what makes this a decision instead of a takeover:
    the composition knows which plugin it is pointing at, and nothing changes unless it
    does.
    """
    from agent.orchestration.planner import OrchestrationPlanner

    catalog = _plugin_with_policy(tmp_path)

    chosen = OrchestrationPlanner.from_skill_catalog(
        catalog, policy_skill_id="policy-plugin:multi-agent-orchestration"
    )
    assert chosen.default_mode == "pipeline"
    assert chosen.max_rendezvous_rounds == 5
    assert "stage by stage" in chosen.pipeline_keywords
    assert chosen.policy_skill_id == "policy-plugin:multi-agent-orchestration"


def test_the_config_key_reaches_the_planner() -> None:
    """The wiring, asserted where it is: config -> agent -> LoopDeps -> planner.

    Three hops and no test would have meant a knob that is read and then dropped, which
    looks exactly like a knob that works.
    """
    import ast
    import pathlib as _pathlib

    root = _pathlib.Path(__file__).resolve().parents[1]
    plugin_src = (root / "agent/core/agent_plugin.py").read_text(encoding="utf-8")
    loop_src = (root / "agent/core/turn_loop.py").read_text(encoding="utf-8")
    agent_src = (root / "agent/core/agent.py").read_text(encoding="utf-8")

    assert "policy_skill_id" in plugin_src, "the plugin must read the config key"
    assert "orchestration_policy_skill_id" in agent_src, "the agent must hold it"
    tree = ast.parse(loop_src)
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LoopDeps"
    )
    fields = {
        f.target.id
        for f in cls.body
        if isinstance(f, ast.AnnAssign) and isinstance(f.target, ast.Name)
    }
    assert "orchestration_policy_skill_id" in fields, (
        "LoopDeps must carry it, or the loop cannot pass it on"
    )
    assert "policy_skill_id=getattr(" in loop_src, "the loop must pass it to the planner"


def test_without_the_bundle_the_policy_is_the_default(tmp_path) -> None:
    """Control: the tests above would pass on a planner that always said 'pipeline'."""
    from agent.orchestration.planner import OrchestrationPlanner

    planner = OrchestrationPlanner.from_skill_catalog(None)
    assert planner.default_mode == "direct"
    assert planner.max_rendezvous_rounds == 2
