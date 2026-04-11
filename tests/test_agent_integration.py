"""Tests for MCP/skill wiring and capability-aware prompts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeMCPClient:
    instances = []

    def __init__(self, registry):
        self.registry = registry
        self.connected = False
        self.closed = False
        type(self).instances.append(self)

    async def connect_from_config(self, config):
        self.connected = True
        self.registry.register(
            "mcp_demo_echo",
            "Echo from MCP",
            {"type": "object", "properties": {}, "required": []},
            lambda: {"ok": True},
            source="mcp:demo",
        )

    async def close(self):
        self.closed = True


class _LoopBoundMCPClient:
    instances = []

    def __init__(self, registry):
        import asyncio

        self.registry = registry
        self.connected_loop = None
        self.closed_loop = None
        self.registered = False
        type(self).instances.append(self)

    async def connect_from_config(self, config):
        import asyncio

        self.connected_loop = id(asyncio.get_running_loop())

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


def test_skill_loader_reload_removes_deleted_tools(tmp_path, monkeypatch):
    from agent import SkillLoader, ToolRegistry

    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    skill_file = skill_dir / "demo.py"
    skill_file.write_text(
        """
def register_tool(name, description, parameters):
    def decorator(fn):
        fn._tool_meta = {"name": name, "description": description, "parameters": parameters}
        return fn
    return decorator

@register_tool("demo_tool", "demo", {"type": "object", "properties": {}, "required": []})
def demo_tool():
    return "ok"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.SKILLS_DIR", skill_dir)

    registry = ToolRegistry()
    loader = SkillLoader(registry)

    loader.load_all()
    assert "demo_tool" in registry.list_tools()

    skill_file.unlink()
    loader.reload()

    assert "demo_tool" not in registry.list_tools()


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

    components = agent_module._build_components(cfg)

    assert _FakeMCPClient.instances[-1].connected is True
    assert "mcp_demo_echo" in components["registry"].list_tools()
    assert components["mcp_client"] is _FakeMCPClient.instances[-1]


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
