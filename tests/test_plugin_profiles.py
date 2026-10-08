"""Which plugins load, declared rather than inferred.

The shape is borrowed from ``agent/scheduler/profiles.py``: a frozen dataclass per
row, a table keyed by name, a ``to_payload()`` so no UI hardcodes the fields.  What
differs is the failure mode -- a permission typo falls back to read-only, while an
unknown *profile name* raises, because loading the default set when a composition
was asked for is a configuration bug, not a safer default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.plugins.profiles import (
    DEFAULT_PROFILE,
    DEFAULT_PROFILE_NAME,
    KnownProfileError,
    PluginEntry,
    PluginProfile,
    PROFILES,
    profile_names,
    resolve_plugin_state,
    resolve_profile,
)


def _profile(*entries: PluginEntry) -> PluginProfile:
    return PluginProfile(
        key="test", label="T", summary="s", entries=tuple(entries)
    )


# ── The table ────────────────────────────────────────────────────────────────


def test_there_is_a_default_profile() -> None:
    assert DEFAULT_PROFILE_NAME in PROFILES
    assert DEFAULT_PROFILE is PROFILES[DEFAULT_PROFILE_NAME]


def test_the_default_profile_turns_nothing_on_by_itself() -> None:
    """It exists to give a name to "no opinion", not to curate."""
    assert DEFAULT_PROFILE.entries == ()
    assert DEFAULT_PROFILE.plugins == ()


def test_a_profile_payload_is_serialisable() -> None:
    """So the UI lists profiles without hardcoding them."""
    payload = DEFAULT_PROFILE.to_payload()
    assert set(payload) == {"key", "label", "summary", "plugins", "core"}
    assert isinstance(payload["plugins"], list)
    # `core` is the composition's own plugins, which the entries above do not describe:
    # they decide what is *discovered*, this decides what the root *mounts*.  None means
    # all of them.
    assert payload["core"] is None


def test_an_entry_payload_is_serialisable() -> None:
    payload = PluginEntry(plugin="p", enabled=False, config={"a": 1}).to_payload()
    assert payload == {"plugin": "p", "enabled": False, "config": {"a": 1}}


def test_profile_names_are_in_declaration_order() -> None:
    assert profile_names() == tuple(PROFILES)


def test_resolving_a_known_profile_returns_it() -> None:
    assert resolve_profile(DEFAULT_PROFILE_NAME) is DEFAULT_PROFILE


def test_resolving_an_unknown_profile_raises() -> None:
    """Not a fallback: a composition that was asked for must be delivered."""
    with pytest.raises(KnownProfileError) as caught:
        resolve_profile("nope")
    assert "nope" in str(caught.value)
    assert DEFAULT_PROFILE_NAME in str(caught.value), "it names the known ones"


# ── The default is what the code did before ──────────────────────────────────


def test_no_config_and_no_profile_loads_everything() -> None:
    """The historical behaviour, and the only safe default."""
    state = resolve_plugin_state("anything")
    assert state.enabled is True


def test_an_unmentioned_plugin_stays_on() -> None:
    state = resolve_plugin_state("p", config={"other": {"enabled": False}})
    assert state.enabled is True


def test_an_explicitly_enabled_plugin_is_on() -> None:
    state = resolve_plugin_state("p", config={"p": {"enabled": True}})
    assert state.enabled is True


def test_an_explicitly_disabled_plugin_is_off() -> None:
    state = resolve_plugin_state("p", config={"p": {"enabled": False}})
    assert state.enabled is False


def test_a_non_boolean_enabled_is_ignored() -> None:
    """A string ``"false"`` is not a decision; treating it as one is a guess."""
    state = resolve_plugin_state("p", config={"p": {"enabled": "false"}})
    assert state.enabled is True


def test_a_malformed_entry_does_not_crash_the_load() -> None:
    state = resolve_plugin_state("p", config={"p": "not-a-mapping"})
    assert state.enabled is True


# ── Disabled always wins ─────────────────────────────────────────────────────


def test_a_profile_cannot_override_a_disabled_plugin() -> None:
    """A profile is a composition within what is permitted, not around it."""
    profile = _profile(PluginEntry(plugin="p", enabled=True))
    state = resolve_plugin_state(
        "p", config={"p": {"enabled": False}}, profile=profile
    )
    assert state.enabled is False


def test_a_profile_can_disable_an_enabled_plugin() -> None:
    profile = _profile(PluginEntry(plugin="p", enabled=False))
    state = resolve_plugin_state("p", config={"p": {"enabled": True}}, profile=profile)
    assert state.enabled is False


def test_a_profile_turns_a_plugin_on() -> None:
    profile = _profile(PluginEntry(plugin="p", enabled=True))
    assert resolve_plugin_state("p", profile=profile).enabled is True


# ── Profiles are additive ────────────────────────────────────────────────────


def test_a_plugin_the_profile_omits_is_still_loaded() -> None:
    """Overriding, not replacing: an omission is not a disable."""
    profile = _profile(PluginEntry(plugin="chosen", enabled=True))
    assert resolve_plugin_state("other", profile=profile).enabled is True


def test_disabling_is_expressible() -> None:
    """The project genuinely wants this -- see config.example.json."""
    profile = _profile(PluginEntry(plugin="unwanted", enabled=False))
    assert resolve_plugin_state("unwanted", profile=profile).enabled is False


# ── Configuration merging ────────────────────────────────────────────────────


def test_a_profile_config_is_carried_through() -> None:
    profile = _profile(PluginEntry(plugin="p", config={"level": "high"}))
    state = resolve_plugin_state("p", profile=profile)
    assert state.config == {"level": "high"}


def test_explicit_config_outranks_a_profile_default() -> None:
    """The operator's file is the more specific statement about this install."""
    profile = _profile(PluginEntry(plugin="p", config={"level": "high"}))
    state = resolve_plugin_state(
        "p", config={"p": {"level": "low"}}, profile=profile
    )
    assert state.config == {"level": "low"}


def test_profile_config_fills_in_what_the_file_omits() -> None:
    profile = _profile(PluginEntry(plugin="p", config={"a": 1, "b": 2}))
    state = resolve_plugin_state("p", config={"p": {"b": 9}}, profile=profile)
    assert state.config == {"a": 1, "b": 9}


def test_the_enabled_key_is_not_forwarded_as_config() -> None:
    """It is consumed as a decision, not passed on as a setting."""
    state = resolve_plugin_state("p", config={"p": {"enabled": True, "x": 1}})
    assert state.config == {"x": 1}


def test_config_for_an_unmentioned_plugin_is_still_carried() -> None:
    state = resolve_plugin_state("p", config={"p": {"x": 1}})
    assert state.config == {"x": 1}


def test_a_profile_does_not_mutate_the_configuration() -> None:
    """The caller's dict is input, not scratch space."""
    config = {"p": {"level": "low"}}
    profile = _profile(PluginEntry(plugin="p", config={"level": "high"}))
    resolve_plugin_state("p", config=config, profile=profile)
    assert config == {"p": {"level": "low"}}


def test_the_profile_entries_are_not_mutated() -> None:
    entry = PluginEntry(plugin="p", config={"a": 1})
    profile = _profile(entry)
    resolve_plugin_state("p", config={"p": {"b": 2}}, profile=profile)
    assert entry.config == {"a": 1}


# ── The catalog actually uses it ─────────────────────────────────────────────


def test_the_catalog_resolves_through_this_module() -> None:
    """The seam must be live, not a table nothing consults."""
    import inspect

    import agent.plugins.catalog as catalog_module

    source = inspect.getsource(catalog_module.PluginCatalog._is_plugin_enabled)
    assert "resolve_plugin_state" in source, (
        "_is_plugin_enabled must ask the profile resolver, not read the raw dict"
    )


def test_the_catalog_still_honours_a_disabled_plugin(tmp_path) -> None:
    from agent import PluginCatalog

    plugin_dir = tmp_path / "plugins" / "off"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "def register():\n"
        "    class P:\n"
        "        name = 'off'\n"
        "        def setup(self, ctx):\n"
        "            pass\n"
        "    return P()\n",
        encoding="utf-8",
    )
    catalog = PluginCatalog(
        builtin_dir=tmp_path / "plugins",
        plugin_config={"off": {"enabled": False}},
    )
    try:
        assert catalog.discover_and_load() == []
    finally:
        catalog.close()


# ── Selecting a profile ──────────────────────────────────────────────────────


def test_the_profile_is_named_by_a_reserved_key() -> None:
    from agent.plugins.profile_selection import PROFILE_KEY, profile_name_from_config

    assert PROFILE_KEY.startswith("_"), "reserved, so it is not read as a plugin"
    assert profile_name_from_config({PROFILE_KEY: DEFAULT_PROFILE_NAME}) == DEFAULT_PROFILE_NAME


def test_no_config_means_the_default_profile() -> None:
    from agent.plugins.profile_selection import profile_name_from_config

    assert profile_name_from_config(None) == DEFAULT_PROFILE_NAME
    assert profile_name_from_config({}) == DEFAULT_PROFILE_NAME


def test_a_blank_name_means_unstated_not_a_profile_called_blank() -> None:
    """An empty string is what a form leaves behind."""
    from agent.plugins.profile_selection import PROFILE_KEY, profile_name_from_config

    assert profile_name_from_config({PROFILE_KEY: ""}) == DEFAULT_PROFILE_NAME
    assert profile_name_from_config({PROFILE_KEY: "   "}) == DEFAULT_PROFILE_NAME


def test_a_non_string_name_is_treated_as_unstated() -> None:
    from agent.plugins.profile_selection import PROFILE_KEY, profile_name_from_config

    assert profile_name_from_config({PROFILE_KEY: 7}) == DEFAULT_PROFILE_NAME


def test_the_name_is_trimmed() -> None:
    from agent.plugins.profile_selection import PROFILE_KEY, profile_name_from_config

    assert profile_name_from_config({PROFILE_KEY: "  default  "}) == DEFAULT_PROFILE_NAME


def test_an_unknown_selected_profile_raises_at_construction() -> None:
    """Not a fallback: a narrowed composition must not quietly get everything."""
    from agent import PluginCatalog

    with pytest.raises(KnownProfileError):
        PluginCatalog(
            builtin_dir=Path("/tmp/does-not-matter"),
            plugin_config={"_profile": "nope"},
        )


def test_a_known_selected_profile_is_accepted() -> None:
    from agent import PluginCatalog

    catalog = PluginCatalog(
        builtin_dir=Path("/tmp/does-not-matter"),
        plugin_config={"_profile": DEFAULT_PROFILE_NAME},
    )
    try:
        assert catalog._plugin_profile is DEFAULT_PROFILE
    finally:
        catalog.close()


def test_the_reserved_key_is_not_looked_up_as_a_plugin() -> None:
    """Looking up ``_profile`` as a plugin would be wrong the moment one is named that."""
    state = resolve_plugin_state("_profile", config={"_profile": "default"})
    assert state.enabled is True, "a reserved companion is not a disabled plugin"


def test_a_reserved_key_does_not_appear_as_config() -> None:
    state = resolve_plugin_state("_anything", config={"_anything": {"x": 1}})
    assert state.config == {}


def test_the_catalog_reads_the_reserved_key_end_to_end(tmp_path) -> None:
    """The whole path: config -> profile -> which plugins load."""
    from agent import PluginCatalog

    plugin_dir = tmp_path / "plugins" / "p"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "def register():\n"
        "    class P:\n"
        "        name = 'p'\n"
        "        def setup(self, ctx):\n"
        "            pass\n"
        "    return P()\n",
        encoding="utf-8",
    )
    catalog = PluginCatalog(
        builtin_dir=tmp_path / "plugins",
        plugin_config={"_profile": DEFAULT_PROFILE_NAME},
    )
    try:
        assert catalog.discover_and_load() == ["p"]
    finally:
        catalog.close()
