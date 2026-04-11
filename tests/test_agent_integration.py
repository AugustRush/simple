"""Tests for MCP/skill wiring and capability-aware prompts."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeMCPClient:
    instances = []

    def __init__(self, registry):
        self.registry = registry
        self.connected = False
        self.closed = False
        type(self).instances.append(self)

    async def connect_from_config(self, config, extra_env=None):
        self.connected = True
        self.extra_env = extra_env
        self.registry.register(
            "mcp_demo_echo",
            "Echo from MCP",
            {"type": "object", "properties": {}, "required": []},
            lambda: {"ok": True},
            source="mcp:demo",
        )

    async def close(self):
        self.closed = True

    def status_summary(self):
        return {
            "configured_servers": 1,
            "connected_servers": 1,
            "failed_servers": 0,
            "registered_tools": 1,
        }


class _LoopBoundMCPClient:
    instances = []

    def __init__(self, registry):
        import asyncio

        self.registry = registry
        self.connected_loop = None
        self.closed_loop = None
        self.registered = False
        type(self).instances.append(self)

    async def connect_from_config(self, config, extra_env=None):
        import asyncio

        self.connected_loop = id(asyncio.get_running_loop())
        self.extra_env = extra_env

        async def mcp_ping():
            current_loop = id(asyncio.get_running_loop())
            if current_loop != self.connected_loop:
                raise RuntimeError("MCP tool used from a different event loop")
            return {"ok": True, "loop": current_loop}

        self.registry.register(
            "mcp_demo_ping",
            "Loop-bound MCP tool",
            {"type": "object", "properties": {}, "required": []},
            mcp_ping,
            source="mcp:demo",
        )
        self.registered = True

    async def close(self):
        import asyncio

        self.closed_loop = id(asyncio.get_running_loop())

    def status_summary(self):
        return {
            "configured_servers": 1,
            "connected_servers": 1,
            "failed_servers": 0,
            "registered_tools": 1,
        }


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


def _write_skill_bundle(root: Path, relative_dir: str, skill_text: str, extra_files: dict[str, str] | None = None):
    bundle_dir = root / relative_dir
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "SKILL.md").write_text(skill_text, encoding="utf-8")
    for rel_path, content in (extra_files or {}).items():
        file_path = bundle_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return bundle_dir


def test_skill_catalog_discovers_bundles_and_user_overrides_builtin(tmp_path, monkeypatch):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

    _write_skill_bundle(
        builtin_root,
        "quality/review",
        """---
name: Built-in Review
description: Built-in reviewer
user-invocable: true
---
Built-in instructions.
""",
    )
    _write_skill_bundle(
        user_root,
        "quality/review",
        """---
name: User Review
description: User override
user-invocable: true
---
User instructions.
""",
        {"examples/sample.md": "example"},
    )

    monkeypatch.setattr(agent_module, "SKILLS_DIR", user_root)
    monkeypatch.setattr(agent_module, "BUILTIN_SKILLS_DIR", builtin_root)

    catalog = agent_module.SkillCatalog()
    catalog.load_all()

    review = catalog.get("quality/review")
    assert review is not None
    assert review.name == "User Review"
    assert review.description == "User override"
    assert review.source == "user"
    assert "examples/sample.md" in review.supporting_files


def test_parse_explicit_skill_request_supports_slash_and_natural_language():
    from agent import parse_explicit_skill_request

    parsed = parse_explicit_skill_request("/quality/review tighten the output")
    assert parsed is not None
    assert parsed.skill_ref == "quality/review"
    assert parsed.remaining_text == "tighten the output"

    parsed = parse_explicit_skill_request("/skill quality/review tighten the output")
    assert parsed is not None
    assert parsed.skill_ref == "quality/review"
    assert parsed.remaining_text == "tighten the output"

    parsed = parse_explicit_skill_request("use quality/review tighten the output")
    assert parsed is not None
    assert parsed.skill_ref == "quality/review"
    assert parsed.remaining_text == "tighten the output"

    parsed = parse_explicit_skill_request("使用 quality/review 优化输出")
    assert parsed is not None
    assert parsed.skill_ref == "quality/review"
    assert parsed.remaining_text == "优化输出"


def test_build_components_registers_skill_runtime_tools_and_uses_progressive_disclosure(
    monkeypatch, tmp_path
):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"
    skill_body = "Follow the exact review checklist."
    _write_skill_bundle(
        builtin_root,
        "quality/review",
        f"""---
name: Review
description: Review code changes
user-invocable: true
---
{skill_body}
""",
        {
            "template.md": "Checklist template",
            "examples/sample.md": "Example output",
        },
    )

    cfg = _minimal_cfg()

    monkeypatch.setattr(
        agent_module.ModelClientFactory, "from_config", lambda cfg: (object(), "fake-model", 1024)
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", user_root)
    monkeypatch.setattr(agent_module, "BUILTIN_SKILLS_DIR", builtin_root)
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)
    registry = components["registry"]
    prompt = components["system_prompt"]

    assert "activate_skill" in registry.list_tools()
    assert "list_skill_files" in registry.list_tools()
    assert "read_skill_file" in registry.list_tools()
    assert "Available skills:" in prompt
    assert "quality/review" in prompt
    assert "Review code changes" in prompt
    assert skill_body not in prompt
    assert "Loaded skill tools" not in prompt

    payload = json.loads(
        asyncio.run(registry.call("activate_skill", {"skill_name": "quality/review"}))
    )
    assert payload["ok"] is True
    assert payload["skill"]["id"] == "quality/review"
    assert payload["skill"]["instructions"] == skill_body
    assert "template.md" in payload["skill"]["supporting_files"]

    files_payload = json.loads(
        asyncio.run(registry.call("list_skill_files", {"skill_name": "quality/review"}))
    )
    assert files_payload["ok"] is True
    assert "template.md" in files_payload["files"]

    file_payload = json.loads(
        asyncio.run(
            registry.call(
                "read_skill_file",
                {"skill_name": "quality/review", "path": "template.md"},
            )
        )
    )
    assert file_payload["ok"] is True
    assert file_payload["content"] == "Checklist template"


def test_build_components_connects_mcp_and_registers_tools(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["mcp_servers"] = [{"name": "demo", "command": "fake"}]

    monkeypatch.setattr(agent_module.ModelClientFactory, "from_config", lambda cfg: (object(), "fake-model", 1024))
    monkeypatch.setattr(agent_module, "MCPClient", _FakeMCPClient)
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)

    assert _FakeMCPClient.instances[-1].connected is True
    assert "mcp_demo_echo" in components["registry"].list_tools()
    assert components["mcp_client"] is _FakeMCPClient.instances[-1]


def test_build_components_exposes_mcp_status_and_prints_summary(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["mcp_servers"] = [{"name": "demo", "command": "fake"}]
    console_messages = []

    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "MCPClient", _FakeMCPClient)
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        agent_module.CONSOLE,
        "print",
        lambda *args, **kwargs: console_messages.append(" ".join(str(arg) for arg in args)),
    )

    components = agent_module._build_components(cfg)

    assert components["mcp_status"] == {
        "configured_servers": 1,
        "connected_servers": 1,
        "failed_servers": 0,
        "registered_tools": 1,
    }
    assert any("MCP active" in message for message in console_messages)


def test_system_prompt_reflects_registered_capabilities(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["mcp_servers"] = [{"name": "demo", "command": "fake"}]

    monkeypatch.setattr(agent_module.ModelClientFactory, "from_config", lambda cfg: (object(), "fake-model", 1024))
    monkeypatch.setattr(agent_module, "MCPClient", _FakeMCPClient)
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)
    prompt = components["system_prompt"]

    assert "shell" in prompt
    assert "context_retrieve" in prompt
    assert "spawn_agent" in prompt
    assert "mcp_demo_echo" in prompt
    assert "Echo from MCP" in prompt
    assert "workspace" in prompt.lower()
    assert "when the user asks what you can do" in prompt.lower()


def test_async_component_lifecycle_keeps_mcp_on_one_event_loop(monkeypatch, tmp_path):
    import asyncio
    import json
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["mcp_servers"] = [{"name": "demo", "command": "fake"}]

    monkeypatch.setattr(agent_module.ModelClientFactory, "from_config", lambda cfg: (object(), "fake-model", 1024))
    monkeypatch.setattr(agent_module, "MCPClient", _LoopBoundMCPClient)
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    async def run():
        components = await agent_module._build_components_async(cfg)
        payload = json.loads(await components["registry"].call("mcp_demo_ping", {}))
        await agent_module._close_components(components)
        return components, payload

    components, payload = asyncio.run(run())

    client = _LoopBoundMCPClient.instances[-1]
    assert client.registered is True
    assert payload["ok"] is True
    assert payload["loop"] == client.connected_loop
    assert client.closed_loop == client.connected_loop


@pytest.mark.skipif(
    os.environ.get("SIMPLE_RUN_REAL_MCP_TEST") != "1",
    reason="set SIMPLE_RUN_REAL_MCP_TEST=1 to run the real MCP smoke test",
)
def test_real_mcp_smoke_registers_tools_from_config():
    import asyncio
    import agent as agent_module

    cfg, _ = agent_module.load_config()
    if not cfg.get("mcp_servers"):
        pytest.skip("no MCP servers configured in ~/.agent/config.json")

    original_factory = agent_module.ModelClientFactory.from_config
    agent_module.ModelClientFactory.from_config = staticmethod(
        lambda cfg: (object(), "fake-model", 1024)
    )

    async def run():
        components = await agent_module._build_components_async(cfg)
        try:
            mcp_tools = [
                name
                for name in components["registry"].list_tools()
                if name.startswith("mcp_")
            ]
            assert components["mcp_status"]["connected_servers"] >= 1
            assert components["mcp_status"]["registered_tools"] >= 1
            assert mcp_tools
        finally:
            await agent_module._close_components(components)

    try:
        asyncio.run(run())
    finally:
        agent_module.ModelClientFactory.from_config = original_factory
