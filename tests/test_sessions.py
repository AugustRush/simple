"""Tests for the named-session model (``--name`` + shared default config).

These exercise the small session-discovery layer (``agent.sessions``) and the
``/sessions`` command surface.  The old live-registry ``/new`` / ``/switch``
commands are intentionally gone: a process owns exactly one session, selected
by ``--name``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent import shared
from agent.commands import CommandContext, CommandRouter, register_builtin_commands
from types import SimpleNamespace


def _run_command(text, tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", tmp_path / ".agent")
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    monkeypatch.setattr(shared, "SESSIONS_FILE", tmp_path / ".agent" / "rl" / "sessions.jsonl")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    router = CommandRouter()
    register_builtin_commands(router)
    route = router.classify(text, channel_name="cli", session_id="cli")
    assert route.kind == "command", route
    state = SimpleNamespace()
    context = CommandContext(
        {},
        {},
        state,
        object(),
        channel_name="cli",
        session_id="cli",
    )
    return asyncio.run(router.execute(route, context))


def test_session_home_and_iter_discovery(tmp_path, monkeypatch):
    default = tmp_path / ".agent"
    prod = tmp_path / ".agent-prod"
    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", default)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert shared.session_home("") == default
    assert shared.session_home("prod") == prod

    prod.mkdir()
    (tmp_path / ".agent-not-a-dir").write_text("x")

    homes = dict(shared.iter_session_homes())
    assert set(homes) == {"default", "prod"}
    assert homes["default"] == default
    assert homes["prod"] == prod


def test_list_sessions_shows_config_source_and_current(tmp_path, monkeypatch):
    default = tmp_path / ".agent"
    prod = tmp_path / ".agent-prod"
    (prod / "context").mkdir(parents=True)
    (prod / "context" / "palace.db").write_text("x")
    (prod / "config.json").write_text("{}")

    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", default)
    monkeypatch.setattr(shared, "AGENT_HOME", prod)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    from agent.sessions import list_sessions

    infos = {info.name: info for info in list_sessions()}
    assert "default" in infos
    assert infos["prod"].config_source == "session"
    assert infos["prod"].has_palace_db is True
    assert infos["prod"].is_current is True
    assert infos["default"].is_current is False
    assert infos["default"].config_source == "shared"


def test_sessions_command_lists_sessions(tmp_path, monkeypatch):
    default = tmp_path / ".agent"
    prod = tmp_path / ".agent-prod"
    prod.mkdir()
    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", default)
    monkeypatch.setattr(shared, "AGENT_HOME", prod)
    monkeypatch.setattr(shared, "SESSIONS_FILE", tmp_path / "sessions.jsonl")

    result = _run_command("/sessions", tmp_path, monkeypatch)
    assert result.response_text is not None
    assert "Sessions" in result.response_text
    assert "prod" in result.response_text
    assert "(current)" in result.response_text


def test_new_and_switch_are_not_commands(tmp_path, monkeypatch):
    router = CommandRouter()
    register_builtin_commands(router)

    for text in ("/new tax planning", "/switch cli"):
        route = router.classify(text, channel_name="cli", session_id="cli")
        assert route.kind != "command", text


def test_resolve_config_file_prefers_session_config(tmp_path, monkeypatch):
    default = tmp_path / ".agent"
    prod = tmp_path / ".agent-prod"
    prod_config = prod / "config.json"
    prod_config.parent.mkdir(parents=True)
    prod_config.write_text('{"active_provider": "openai"}', encoding="utf-8")
    default_config = default / "config.json"
    default_config.parent.mkdir(parents=True)
    default_config.write_text('{"active_provider": "anthropic"}', encoding="utf-8")

    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", default)
    monkeypatch.setattr(shared, "DEFAULT_CONFIG_FILE", default_config)
    monkeypatch.setattr(shared, "AGENT_HOME", prod)
    monkeypatch.setattr(shared, "CONFIG_FILE", prod_config)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert shared.resolve_config_file() == prod_config


def test_resolve_config_file_falls_back_to_shared(tmp_path, monkeypatch):
    default = tmp_path / ".agent"
    prod = tmp_path / ".agent-prod"
    prod.mkdir()
    default_config = default / "config.json"
    default_config.parent.mkdir(parents=True)
    default_config.write_text('{"active_provider": "anthropic"}', encoding="utf-8")

    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", default)
    monkeypatch.setattr(shared, "DEFAULT_CONFIG_FILE", default_config)
    monkeypatch.setattr(shared, "AGENT_HOME", prod)
    monkeypatch.setattr(shared, "CONFIG_FILE", prod / "config.json")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert shared.resolve_config_file() == default_config

    from agent.config import load_config

    cfg, first_run = load_config()
    assert cfg["active_provider"] == "anthropic"
    assert first_run is False


def test_load_config_default_session_uses_own_config(tmp_path, monkeypatch):
    home = tmp_path / ".agent"
    config_file = home / "config.json"
    config_file.parent.mkdir(parents=True)

    monkeypatch.setattr(shared, "DEFAULT_AGENT_HOME", home)
    monkeypatch.setattr(shared, "DEFAULT_CONFIG_FILE", config_file)
    monkeypatch.setattr(shared, "AGENT_HOME", home)
    monkeypatch.setattr(shared, "CONFIG_FILE", config_file)

    from agent.config import load_config

    cfg, first_run = load_config()
    assert first_run is True
    assert cfg["active_provider"] == "anthropic"  # DEFAULT_CONFIG value
    assert config_file.exists()
