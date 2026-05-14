"""Tests for the output_dir feature."""

import os
import time
from pathlib import Path

import pytest


# ── _resolve_output_dir ─────────────────────────────────────────────────────


def test_resolve_output_dir_uses_default_when_not_configured(monkeypatch, tmp_path):
    import agent as agent_module

    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    result = agent_module._resolve_output_dir({})
    assert result == tmp_path / "output"
    assert result.is_dir()


def test_resolve_output_dir_uses_config_value(tmp_path):
    import agent as agent_module

    target = tmp_path / "custom_output"
    result = agent_module._resolve_output_dir({"output_dir": str(target)})
    assert result == target.resolve()
    assert result.is_dir()


def test_resolve_output_dir_expands_tilde(monkeypatch, tmp_path):
    import agent as agent_module

    monkeypatch.setenv("HOME", str(tmp_path))
    result = agent_module._resolve_output_dir({"output_dir": "~/my_output"})
    assert result == (tmp_path / "my_output").resolve()
    assert result.is_dir()


def test_resolve_output_dir_expands_env_var(monkeypatch, tmp_path):
    import agent as agent_module

    monkeypatch.setenv("MY_OUTPUT", str(tmp_path / "env_output"))
    result = agent_module._resolve_output_dir({"output_dir": "$MY_OUTPUT"})
    assert result == (tmp_path / "env_output").resolve()
    assert result.is_dir()


# ── ToolRegistry context ────────────────────────────────────────────────────


def test_registry_set_and_get_context():
    from agent import ToolRegistry

    reg = ToolRegistry()
    reg.set_context("output_dir", "/tmp/test")
    assert reg.get_context("output_dir") == "/tmp/test"
    assert reg.get_context("nonexistent") is None
    assert reg.get_context("nonexistent", "fallback") == "fallback"


# ── clean_output tool ───────────────────────────────────────────────────────


def test_clean_output_deletes_all_files(tmp_path):
    from agent import ToolRegistry, BuiltinTools

    output = tmp_path / "output"
    output.mkdir()
    (output / "a.txt").write_text("a")
    (output / "sub").mkdir()
    (output / "sub" / "b.txt").write_text("b")

    reg = ToolRegistry()
    # BuiltinTools needs a memory object; use a minimal stub
    mem = _StubMemory()
    bt = BuiltinTools(mem, reg, output_dir=output)

    assert "clean_output" in reg.list_tools()
    result = bt._clean_output(max_age_hours=0)
    assert result["ok"] is True
    assert result["deleted"] == 2
    # Files gone, empty subdirs cleaned up
    assert not (output / "a.txt").exists()
    assert not (output / "sub" / "b.txt").exists()


def test_clean_output_respects_max_age(tmp_path):
    from agent import ToolRegistry, BuiltinTools

    output = tmp_path / "output"
    output.mkdir()

    old_file = output / "old.txt"
    old_file.write_text("old")
    # Set mtime to 2 hours ago
    old_mtime = time.time() - 7200
    os.utime(old_file, (old_mtime, old_mtime))

    new_file = output / "new.txt"
    new_file.write_text("new")

    reg = ToolRegistry()
    bt = BuiltinTools(_StubMemory(), reg, output_dir=output)

    result = bt._clean_output(max_age_hours=1)
    assert result["ok"] is True
    assert result["deleted"] == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_clean_output_subdir_only(tmp_path):
    from agent import ToolRegistry, BuiltinTools

    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    (output / "tmp").mkdir()
    (output / "tmp" / "delete.txt").write_text("delete")

    reg = ToolRegistry()
    bt = BuiltinTools(_StubMemory(), reg, output_dir=output)

    result = bt._clean_output(max_age_hours=0, subdir="tmp")
    assert result["ok"] is True
    assert result["deleted"] == 1
    assert (output / "keep.txt").exists()
    assert not (output / "tmp" / "delete.txt").exists()


def test_clean_output_nonexistent_dir(tmp_path):
    from agent import ToolRegistry, BuiltinTools

    output = tmp_path / "output"
    output.mkdir()

    reg = ToolRegistry()
    bt = BuiltinTools(_StubMemory(), reg, output_dir=output)

    result = bt._clean_output(subdir="nonexistent")
    assert result["ok"] is True
    assert result["deleted"] == 0


def test_clean_output_not_registered_when_no_output_dir():
    from agent import ToolRegistry, BuiltinTools

    reg = ToolRegistry()
    BuiltinTools(_StubMemory(), reg, output_dir=None)
    assert "clean_output" not in reg.list_tools()


# ── Build components integration ────────────────────────────────────────────


def test_build_components_wires_output_dir(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)

    # output_dir in components
    assert components["output_dir"] == tmp_path / "output"
    assert (tmp_path / "output").is_dir()

    # output_dir in registry context
    assert components["registry"].get_context("output_dir") == str(tmp_path / "output")

    # output_dir in system prompt
    assert str(tmp_path / "output") in components["system_prompt"]

    # clean_output registered
    assert "clean_output" in components["registry"].list_tools()


def test_build_components_wires_shell_blocked_commands(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["shell_blocked_commands"] = ["python"]
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)

    assert components["registry"].get_context("shell_blocked_commands") == ["python"]


def test_build_components_wires_audio_transcription_command(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["audio"] = {"transcription_command": "python transcribe.py {path}"}
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)

    assert (
        components["registry"].get_context("audio_transcription_command")
        == "python transcribe.py {path}"
    )


def test_build_components_passes_output_dir_to_mcp_env(monkeypatch, tmp_path):
    """MCP servers receive AGENT_OUTPUT_DIR in their environment."""
    import asyncio
    import agent as agent_module

    class _CaptureMCPClient:
        instances = []

        def __init__(self, registry):
            self.registry = registry
            type(self).instances.append(self)

        async def connect_from_config(self, config, extra_env=None):
            self.extra_env = extra_env

        def status_summary(self):
            return {
                "configured_servers": 1,
                "connected_servers": 1,
                "failed_servers": 0,
                "registered_tools": 0,
            }

        async def close(self):
            pass

    cfg = _minimal_cfg()
    cfg["mcp_servers"] = [{"name": "demo", "command": "fake"}]
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "MCPClient", _CaptureMCPClient)
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    async def run():
        components = await agent_module._build_components_async(cfg)
        await components["mcp_task"]

    asyncio.run(run())

    client = _CaptureMCPClient.instances[-1]
    assert client.extra_env["AGENT_OUTPUT_DIR"] == str(tmp_path / "output")


def test_build_components_bootstraps_assistant_identity_fact(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["assistant_identity"] = {
        "name": "Afu",
        "role": "coding assistant",
    }
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)

    resolved_name = components["context_manager"].store.read_resolved_facts(
        subject="assistant",
        predicate="name",
    )
    resolved_role = components["context_manager"].store.read_resolved_facts(
        subject="assistant",
        predicate="role",
    )

    assert [fact.value for fact in resolved_name] == ["Afu"]
    assert [fact.value for fact in resolved_role] == ["coding assistant"]


# ── Helpers ──────────────────────────────────────────────────────────────────


class _StubMemory:
    """Minimal memory stub for BuiltinTools initialization."""

    def write(self, *a, **kw):
        pass

    def read(self, *a, **kw):
        return ""

    def search(self, *a, **kw):
        return []

    def read_index(self):
        return ""


def _minimal_cfg():
    return {
        "active_provider": "fake",
        "providers": {
            "fake": {
                "api_format": "openai",
                "default_model": "fake-model",
                "max_tokens": 1024,
            }
        },
        "memory": {},
        "orchestration": {},
        "context": {},
        "mcp_servers": [],
    }
