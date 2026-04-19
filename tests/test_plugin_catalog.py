"""Tests for the Plugin System: PluginCatalog, event dataclasses, and hooks."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _write_plugin(plugin_dir: Path, code: str) -> None:
    """Write a minimal plugin __init__.py to *plugin_dir*."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")


# ─── PluginCatalog discovery ──────────────────────────────────────────────────


def test_discover_loads_valid_plugin(tmp_path):
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "alpha",
        """
        def register():
            class P:
                name = "alpha"
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    loaded = catalog.discover_and_load()
    assert loaded == ["alpha"]


def test_discover_skips_missing_register(tmp_path):
    from agent import PluginCatalog

    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "__init__.py").write_text("# no register", encoding="utf-8")

    catalog = PluginCatalog(builtin_dir=tmp_path)
    loaded = catalog.discover_and_load()
    assert loaded == []


def test_discover_survives_broken_plugin(tmp_path):
    """A plugin that raises on import must not abort startup."""
    from agent import PluginCatalog

    _write_plugin(tmp_path / "broken", "raise RuntimeError('boom')")
    _write_plugin(
        tmp_path / "ok",
        """
        def register():
            class P:
                name = "ok"
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    loaded = catalog.discover_and_load()
    assert "ok" in loaded
    assert "broken" not in loaded


def test_discover_loads_from_both_dirs(tmp_path):
    from agent import PluginCatalog

    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    _write_plugin(
        builtin / "p1",
        """
        def register():
            class P: name = "p1"
            return P()
    """,
    )
    _write_plugin(
        user / "p2",
        """
        def register():
            class P: name = "p2"
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=builtin, user_dir=user)
    loaded = catalog.discover_and_load()
    assert "p1" in loaded and "p2" in loaded


# ─── compose_all_prompts ──────────────────────────────────────────────────────


def test_compose_appends_suffix(tmp_path):
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "rules",
        """
        def register():
            class P:
                name = "rules"
                def compose_system_prompt(self, base):
                    return "## Rules\\n- Always be concise."
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    result = catalog.compose_all_prompts("Base prompt.")
    assert "## Rules" in result
    assert "Always be concise" in result


def test_compose_returns_base_when_no_plugins(tmp_path):
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    assert catalog.compose_all_prompts("Hello") == "Hello"


def test_compose_skips_empty_suffix(tmp_path):
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "noop",
        """
        def register():
            class P:
                name = "noop"
                def compose_system_prompt(self, base):
                    return ""
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    assert catalog.compose_all_prompts("Base") == "Base"


# ─── Slash commands ───────────────────────────────────────────────────────────


def test_plugin_registers_slash_command(tmp_path):
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "cmd_plugin",
        """
        def register():
            class P:
                name = "cmd_plugin"
                def register_slash_commands(self):
                    async def handler(raw_cmd, components):
                        components["_called"] = True
                    return {"my-cmd": handler}
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    cmds = catalog.get_slash_commands()
    assert "my-cmd" in cmds

    components: dict = {}
    asyncio.run(cmds["my-cmd"]("my-cmd", components))
    assert components["_called"] is True


# ─── Lifecycle hooks ──────────────────────────────────────────────────────────


def test_fire_session_start_called(tmp_path):
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "starter",
        """
        def register():
            class P:
                name = "starter"
                started = False
                def on_session_start(self, components):
                    P.started = True
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    catalog.fire_session_start({})
    plugin_obj = catalog._plugins["starter"][0]
    assert plugin_obj.__class__.started is True


def test_fire_turn_end_async(tmp_path):
    from agent import PluginCatalog, TurnEvent

    _write_plugin(
        tmp_path / "listener",
        """
        def register():
            class P:
                name = "listener"
                last_event = None
                async def on_turn_end(self, event):
                    P.last_event = event
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    event = TurnEvent(user_input="hi", agent_response="hello", tool_calls=[])
    asyncio.run(catalog.fire_turn_end(event))
    plugin_obj = catalog._plugins["listener"][0]
    assert plugin_obj.__class__.last_event is event


def test_fire_pre_tool_block(tmp_path):
    from agent import PluginCatalog, PreToolEvent, HookResult

    _write_plugin(
        tmp_path / "blocker",
        """
        def register():
            class P:
                name = "blocker"
                async def on_pre_tool(self, event):
                    from agent import HookResult
                    return HookResult(action="block", message="not allowed")
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    event = PreToolEvent(tool_name="shell", tool_kwargs={"command": "rm -rf /"})
    result = asyncio.run(catalog.fire_pre_tool(event))
    assert result.action == "block"
    assert "not allowed" in result.message


def test_fire_pre_tool_noop_when_no_block(tmp_path):
    from agent import PluginCatalog, PreToolEvent

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    event = PreToolEvent(tool_name="read_file", tool_kwargs={"path": "foo.txt"})
    result = asyncio.run(catalog.fire_pre_tool(event))
    assert result.action == "noop"


def test_fire_session_end_async(tmp_path):
    from agent import PluginCatalog, SessionEvent

    _write_plugin(
        tmp_path / "ender",
        """
        def register():
            class P:
                name = "ender"
                ended = False
                async def on_session_end(self, event):
                    P.ended = True
            return P()
    """,
    )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()
    event = SessionEvent(messages=[], tools_used=[])
    asyncio.run(catalog.fire_session_end(event))
    plugin_obj = catalog._plugins["ender"][0]
    assert plugin_obj.__class__.ended is True


# ─── Plugin.json, enable/disable, dedup, skill bundling ──────────────────────


def test_plugin_json_metadata_is_read(tmp_path):
    """Plugin.json metadata should override plugin object attributes."""
    import json
    from agent import PluginCatalog

    plugin_dir = tmp_path / "beta"
    _write_plugin(
        plugin_dir,
        """
        def register():
            class P:
                name = "beta-code"
                version = "0.1.0"
            return P()
    """,
    )
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "beta",
                "version": "2.0.0",
                "description": "Beta plugin from manifest",
            }
        )
    )

    catalog = PluginCatalog(builtin_dir=tmp_path)
    loaded = catalog.discover_and_load()
    assert "beta" in loaded

    metas = catalog.list_plugins()
    assert len(metas) == 1
    assert metas[0].name == "beta"
    assert metas[0].version == "2.0.0"
    assert metas[0].description == "Beta plugin from manifest"


def test_plugin_disabled_in_config_not_loaded(tmp_path):
    """Plugins disabled in config should not be loaded."""
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "disabled",
        """
        def register():
            class P:
                name = "disabled"
            return P()
    """,
    )

    catalog = PluginCatalog(
        builtin_dir=tmp_path,
        plugin_config={"disabled": {"enabled": False}},
    )
    loaded = catalog.discover_and_load()
    assert "disabled" not in loaded
    assert catalog.list_plugins() == []


def test_user_plugin_overrides_builtin_same_name(tmp_path):
    """User plugins should override built-in plugins with the same name."""
    from agent import PluginCatalog

    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"

    _write_plugin(
        builtin_dir / "samename",
        """
        def register():
            class P:
                name = "samename"
                version = "1.0"
            return P()
    """,
    )
    _write_plugin(
        user_dir / "samename",
        """
        def register():
            class P:
                name = "samename"
                version = "2.0"
            return P()
    """,
    )

    catalog = PluginCatalog(builtin_dir=builtin_dir, user_dir=user_dir)
    catalog.discover_and_load()

    metas = catalog.list_plugins()
    assert len(metas) == 1
    assert metas[0].version == "2.0"
    assert metas[0].source == "user"


def test_plugin_bundles_skills(tmp_path):
    """Plugins with skills field in plugin.json should register bundled skills."""
    import json
    from agent import PluginCatalog

    plugin_dir = tmp_path / "skill-bundler"
    _write_plugin(
        plugin_dir,
        """
        def register():
            class P:
                name = "skill-bundler"
            return P()
    """,
    )
    # Create a bundled skill
    skills_dir = plugin_dir / "skills" / "bundled-review"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: Bundled Review\ndescription: A review skill\n---\nReview the code."
    )
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "skill-bundler", "skills": "./skills/"})
    )

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()

    bundled = catalog.get_bundled_skills()
    assert len(bundled) == 1
    assert bundled[0][0] == "skill-bundler"
    assert bundled[0][1].is_dir()


def test_plugin_bundles_mcp_config(tmp_path):
    """Plugins with mcp_servers in plugin.json should expose bundled MCP configs."""
    import json
    from agent import PluginCatalog

    plugin_dir = tmp_path / "mcp-bundler"
    _write_plugin(
        plugin_dir,
        """
        def register():
            class P:
                name = "mcp-bundler"
            return P()
    """,
    )
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "mcp-bundler",
                "mcp_servers": [
                    {"name": "test-server", "command": "echo", "args": ["hello"]}
                ],
            }
        )
    )

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()

    bundled = catalog.get_bundled_mcp()
    assert len(bundled) == 1
    assert bundled[0][0] == "mcp-bundler"
    assert bundled[0][1]["name"] == "test-server"


def test_list_plugins_returns_all_loaded(tmp_path):
    from agent import PluginCatalog

    _write_plugin(
        tmp_path / "p1",
        """
        def register():
            class P:
                name = "p1"
            return P()
    """,
    )
    _write_plugin(
        tmp_path / "p2",
        """
        def register():
            class P:
                name = "p2"
            return P()
    """,
    )

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog.discover_and_load()

    metas = catalog.list_plugins()
    names = {m.name for m in metas}
    assert "p1" in names
    assert "p2" in names


def test_auto_creates_user_plugins_dir(tmp_path):
    from agent import PluginCatalog

    user_dir = tmp_path / "new-user-plugins"
    assert not user_dir.exists()

    catalog = PluginCatalog(builtin_dir=tmp_path, user_dir=user_dir)
    catalog.discover_and_load()

    assert user_dir.exists()


def test_evolution_plugin_has_plugin_json():
    """Evolution plugin should have a valid plugin.json."""
    import json
    from agent import PLUGINS_DIR

    pj = PLUGINS_DIR / "evolution" / "plugin.json"
    assert pj.exists()
    data = json.loads(pj.read_text())
    assert data["name"] == "evolution"
    assert "version" in data
    assert "description" in data


# ─── Built-in evolution plugin ────────────────────────────────────────────────


def test_evolution_plugin_loads_via_catalog():
    """The evolution built-in plugin should load cleanly through PluginCatalog."""
    from agent import PluginCatalog, PLUGINS_DIR

    catalog = PluginCatalog(builtin_dir=PLUGINS_DIR)
    loaded = catalog.discover_and_load()
    assert "evolution" in loaded


def test_evolution_plugin_slash_commands_registered():
    """After loading, evolution plugin must expose evolve/generate-tool/stats."""
    from agent import PluginCatalog, PLUGINS_DIR

    catalog = PluginCatalog(builtin_dir=PLUGINS_DIR)
    catalog.discover_and_load()
    cmds = catalog.get_slash_commands()
    assert "evolve" in cmds
    assert "generate-tool" in cmds
    assert "stats" in cmds


# ─── CorrectionDetector ───────────────────────────────────────────────────────


def test_correction_detector_high_confidence_english(tmp_path):
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from detector import CorrectionDetector

    d = CorrectionDetector()
    # High-confidence pattern — single match fires.
    assert d.is_correction("No, that's wrong", "Here is my answer about X.")
    # Negative case.
    assert not d.is_correction("Thanks!", "Here is my answer about X.")


def test_correction_detector_low_confidence_needs_two_matches():
    """'actually' alone is ambiguous — should NOT fire."""
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from detector import CorrectionDetector

    d = CorrectionDetector()
    # "actually" alone → LOW, only 1 match → not correction
    assert not d.is_correction(
        "Actually, that's a great point!", "Here is my answer about X."
    )
    # Two LOW matches → fires
    assert d.is_correction(
        "Wait, actually I meant something else", "Here is my long answer about X."
    )


def test_correction_detector_returns_confidence():
    """detect() should return a CorrectionSignal with confidence."""
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from detector import CorrectionDetector

    d = CorrectionDetector()
    sig = d.detect("No, that's wrong", "Here is my answer about X.")
    assert sig.is_correction
    assert sig.confidence >= 0.8
    assert len(sig.matched_patterns) > 0

    sig2 = d.detect("Thanks!", "Here is my answer about X.")
    assert not sig2.is_correction
    assert sig2.confidence == 0.0


def test_correction_detector_detects_chinese():
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from detector import CorrectionDetector

    d = CorrectionDetector()
    # HIGH-confidence Chinese pattern
    assert d.is_correction("说错了，我的意思是另一件事", "这是我的回答关于X。")
    # Two LOW-confidence Chinese patterns → fires
    assert d.is_correction("不对，其实是另一件事", "这是我的回答关于X。")
    assert not d.is_correction("好的，谢谢", "这是我的回答。")


def test_correction_detector_ignores_short_prev_response():
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from detector import CorrectionDetector

    d = CorrectionDetector()
    # prev_response too short — should not detect
    assert not d.is_correction("No that's wrong", "OK")


# ─── RuleStore ────────────────────────────────────────────────────────────────


def test_rule_store_add_and_retrieve(tmp_path):
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore

    store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    rule = store.add_rule("Always show a diff first.", ["f-001", "f-002"])
    assert rule.status == "probation"
    active = store.get_active_rules()
    assert "Always show a diff first." in active


def test_rule_store_promotion_on_good_performance(tmp_path):
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore, EVAL_THRESHOLD

    store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    rule = store.add_rule("Be concise.", [], pre_correction_rate=0.30)
    for _ in range(EVAL_THRESHOLD):
        store.record_application(rule.id, was_corrected=False)
    rules = store._load()
    assert rules[0].status == "active"


def test_rule_store_retirement_on_no_improvement(tmp_path):
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore, EVAL_THRESHOLD

    store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    rule = store.add_rule("Bad rule.", [], pre_correction_rate=0.10)
    for _ in range(EVAL_THRESHOLD):
        store.record_application(rule.id, was_corrected=True)
    rules = store._load()
    assert rules[0].status == "retired"


def test_rule_store_get_active_rule_ids(tmp_path):
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore

    store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    r1 = store.add_rule("Rule A", [])
    r2 = store.add_rule("Rule B", [])
    ids = store.get_active_rule_ids()
    assert r1.id in ids and r2.id in ids


# ─── EvolutionPlugin core paths (P3-5) ───────────────────────────────────────


def test_evolution_plugin_on_turn_end_records_application(tmp_path):
    """on_turn_end must call record_application for every active rule."""
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore
    from agent import PluginCatalog, PLUGINS_DIR, TurnEvent

    catalog = PluginCatalog(builtin_dir=PLUGINS_DIR)
    catalog.discover_and_load()
    evo_plugin = next(
        p
        for p, _meta in catalog._plugins.values()
        if getattr(p, "name", "") == "evolution"
    )
    # Inject a test RuleStore.
    test_store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    rule = test_store.add_rule("Test rule", [])
    evo_plugin._rule_store = test_store
    evo_plugin._prev_response = "Here is a long previous response for testing."
    evo_plugin._engine = None  # no LLM for extraction

    # Simulate a non-correction turn.
    event = TurnEvent(
        user_input="Thanks!", agent_response="You're welcome.", tool_calls=[]
    )
    asyncio.run(catalog.fire_turn_end(event))
    rules = test_store._load()
    assert rules[0].applications == 1
    assert rules[0].corrections_after == 0


def test_evolution_plugin_on_turn_end_records_correction(tmp_path):
    """on_turn_end must detect corrections and increment corrections_after."""
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore
    from agent import PluginCatalog, PLUGINS_DIR, TurnEvent

    catalog = PluginCatalog(builtin_dir=PLUGINS_DIR)
    catalog.discover_and_load()
    evo_plugin = next(
        p
        for p, _meta in catalog._plugins.values()
        if getattr(p, "name", "") == "evolution"
    )
    test_store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    rule = test_store.add_rule("Test rule", [])
    evo_plugin._rule_store = test_store
    evo_plugin._prev_response = "Here is my previous answer which was very wrong."
    evo_plugin._engine = None

    # HIGH-confidence correction
    event = TurnEvent(
        user_input="No, that's wrong!", agent_response="Sorry.", tool_calls=[]
    )
    asyncio.run(catalog.fire_turn_end(event))
    rules = test_store._load()
    assert rules[0].applications == 1
    assert rules[0].corrections_after == 1


def test_evolution_plugin_compose_returns_rules(tmp_path):
    """compose_system_prompt should include active rules."""
    from agent import PLUGINS_DIR
    import sys

    sys.path.insert(0, str(PLUGINS_DIR / "evolution"))
    from rules import RuleStore
    from agent import PluginCatalog, PLUGINS_DIR

    catalog = PluginCatalog(builtin_dir=PLUGINS_DIR)
    catalog.discover_and_load()
    evo_plugin = next(
        p
        for p, _meta in catalog._plugins.values()
        if getattr(p, "name", "") == "evolution"
    )
    test_store = RuleStore(rules_file=tmp_path / "rules.jsonl")
    test_store.add_rule("Always be concise.", [])
    evo_plugin._rule_store = test_store

    suffix = evo_plugin.compose_system_prompt("base prompt")
    assert "Always be concise." in suffix
    assert "Learned Behavioral Rules" in suffix


def test_evolution_plugin_engine_none_is_safe():
    """All hooks must degrade gracefully when _engine is None."""
    from agent import PluginCatalog, PLUGINS_DIR, TurnEvent, SessionEvent

    catalog = PluginCatalog(builtin_dir=PLUGINS_DIR)
    catalog.discover_and_load()
    evo_plugin = next(
        p
        for p, _meta in catalog._plugins.values()
        if getattr(p, "name", "") == "evolution"
    )
    evo_plugin._engine = None

    # on_turn_end should not raise
    event = TurnEvent(user_input="hello", agent_response="hi", tool_calls=[])
    asyncio.run(catalog.fire_turn_end(event))

    # on_session_end should not raise
    session_event = SessionEvent(
        messages=[{"role": "user", "content": "hi"}], tools_used=[]
    )
    asyncio.run(catalog.fire_session_end(session_event))
