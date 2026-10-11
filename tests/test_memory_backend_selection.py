"""Selecting a memory backend is a declaration, not a patch.

This is the other half of ``test_memory_contract.py``.  That file proves a second
implementation can *satisfy* the contract; this one proves it can be *selected*
-- with a profile, through the kernel, with no edit to the composition:

* the alternative backend ships as an opt-in core plugin, because by default the
  built-in memory plugin provides the same two services and two providers of one
  name is a collision the kernel refuses;
* naming it as the replacement for ``ltm_store``/``memory`` mounts it and drops
  the plugin it displaces;
* what it provides is a store **and** a palace built over that store, which is
  the pair the rest of the memory system needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.plugins.core import core_plugin_names, core_plugins
from agent.plugins.profiles import PROFILES, SQLITE_FTS_PROFILE_NAME

REPLACEMENT = {"ltm_store": SQLITE_FTS_PROFILE_NAME, "memory": SQLITE_FTS_PROFILE_NAME}


def test_a_default_composition_is_unchanged() -> None:
    """Control: the opt-in plugin must not appear unless something asks for it."""
    names = [plugin.name for plugin in core_plugins()]

    assert names == [
        "ralph",
        "memory",
        "session",
        "agent",
        "tools",
        "agent_loop",
    ]
    assert SQLITE_FTS_PROFILE_NAME in core_plugin_names()


def test_selecting_it_displaces_the_built_in_memory_plugin() -> None:
    names = [plugin.name for plugin in core_plugins(replacements=REPLACEMENT)]

    assert SQLITE_FTS_PROFILE_NAME in names, "naming it as the replacement mounts it"
    assert "memory" not in names, "and drops the plugin whose services it replaced"
    assert "session" in names, "only the displaced plugin goes"


def test_a_replacement_plugin_that_is_not_ours_is_left_to_the_loader() -> None:
    """A discovered plugin is mounted by the catalog; this list must not invent it."""
    names = [plugin.name for plugin in core_plugins(replacements={"ltm_store": "acme.memory"})]

    assert "acme.memory" not in names
    assert "memory" not in names
    assert "session" in names


def test_the_profile_declares_the_replacement() -> None:
    profile = PROFILES[SQLITE_FTS_PROFILE_NAME]

    assert profile.service_replacements == REPLACEMENT
    assert profile.to_payload()["service_replacements"] == REPLACEMENT


@pytest.fixture()
def memory_home(tmp_path, monkeypatch):
    """Point the store and the palace at a temporary home, as the plugin reads shared."""
    from agent import shared

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()
    return tmp_path


def _mount_replacement(mapping: dict) -> None:
    from agent.kernel.plugin import PluginHost
    from agent.kernel.service import ServiceRegistry
    from agent.memory.backends.sqlite_fts import SqliteFtsMemoryPlugin

    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(SqliteFtsMemoryPlugin())
    assert host.pending() == (), "the alternative needs nothing but cfg"


def test_the_plugin_provides_the_store_and_a_palace_over_it(memory_home) -> None:
    from agent.memory.backends.sqlite_fts import SqliteFtsStore
    from agent.memory.palace import MemoryPalace

    mapping: dict = {"cfg": {}}
    _mount_replacement(mapping)

    store = mapping["ltm_store"]
    palace = mapping["memory"]
    assert isinstance(store, SqliteFtsStore)
    assert isinstance(palace, MemoryPalace)
    assert palace.store is store, "the palace reads the replaced store, not a new one"
    # The palace writes through the store, which is what makes the pair coherent.
    palace.write("identity", "user", "prefers tea")
    assert palace.read("identity", "user") == "prefers tea"


def test_the_selected_store_meets_the_same_contract(memory_home) -> None:
    """The whole point, in one call: the contract suite, run on the replacement."""
    from test_memory_contract import run_store_contract

    mapping: dict = {"cfg": {}}
    _mount_replacement(mapping)

    run_store_contract(mapping["ltm_store"])


def test_the_profile_swaps_the_store_in_the_real_composition(monkeypatch, tmp_path) -> None:
    """The whole path: one profile line, the real composition, the other store.

    Everything above tests a piece -- the declaration, the selection, the plugin.
    This builds the composition the way a deployment does, from a config that
    names the profile, and asks what actually ended up behind ``ltm_store``.
    """
    import asyncio

    import agent as agent_module
    from agent.memory.backends.sqlite_fts import SqliteFtsStore
    from test_agent_integration import _minimal_cfg

    cfg = _minimal_cfg()
    cfg["plugins"] = {"_profile": SQLITE_FTS_PROFILE_NAME}
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg, announce=True: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = asyncio.run(agent_module._build_components_async(cfg, announce=False))

    store = components["ltm_store"]
    assert isinstance(store, SqliteFtsStore), (
        "the profile names sqlite-fts-memory; the composition mounted something else"
    )
    catalog = components["plugin_catalog"]
    assert catalog.who_provides("ltm_store") == SQLITE_FTS_PROFILE_NAME
    assert catalog.who_provides("memory") == SQLITE_FTS_PROFILE_NAME
    # The palace the rest of the application uses is built over the replaced store.
    assert components["memory"].store is store
    # And the session plugin took it, which is what "the application uses it" means.
    assert components["context_manager"].store is store


def test_the_replacement_survives_a_turn_shaped_write(memory_home) -> None:
    """A write shaped like a real turn, through the palace/store pair."""
    mapping: dict = {"cfg": {}}
    _mount_replacement(mapping)
    store = mapping["ltm_store"]

    result = store.write_conversation_exchange(
        session_id="s-1",
        user_content="hello",
        assistant_content="hi",
        message_id="m-1",
        assistant_message_id="m-1:run:abc:completion:1",
    )

    assert (result.user_created, result.assistant_created) == (True, True)
    assert [turn.role for turn in store.recent_conversation_turns(session_id="s-1")] == [
        "user",
        "assistant",
    ]
    assert Path(store.memory_dir) == memory_home / "memory"
