"""Tests for MCP/skill wiring and capability-aware prompts."""

import asyncio
import json
import os
from pathlib import Path

import pytest


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


@pytest.fixture(autouse=True)
def _clear_fake_mcp_instances():
    """Reset class-level instance trackers between tests."""
    _FakeMCPClient.instances.clear()
    _LoopBoundMCPClient.instances.clear()
    yield
    _FakeMCPClient.instances.clear()
    _LoopBoundMCPClient.instances.clear()


def _write_skill_bundle(
    root: Path,
    relative_dir: str,
    skill_text: str,
    extra_files: dict[str, str] | None = None,
):
    bundle_dir = root / relative_dir
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "SKILL.md").write_text(skill_text, encoding="utf-8")
    for rel_path, content in (extra_files or {}).items():
        file_path = bundle_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return bundle_dir


def test_skill_catalog_discovers_bundles_and_user_overrides_builtin(
    tmp_path, monkeypatch
):
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
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
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

    components = agent_module._build_components(cfg)

    assert _FakeMCPClient.instances[-1].connected is True
    assert "mcp_demo_echo" in components["registry"].list_tools()
    assert components["mcp_client"] is _FakeMCPClient.instances[-1]


def test_build_components_loads_user_tool_plugins(monkeypatch, tmp_path):
    import asyncio
    import json
    import agent as agent_module

    cfg = _minimal_cfg()
    user_tools_root = tmp_path / "tools"
    user_tools_root.mkdir()
    (user_tools_root / "demo_tool.py").write_text(
        """
def register(registry):
    async def demo_tool(name: str = "world"):
        return {"ok": True, "message": f"hello {name}"}

    registry.register(
        "demo_tool",
        "Say hello from a user plugin",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": [],
        },
        demo_tool,
    )
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "TOOLS_DIR", user_tools_root)
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = agent_module._build_components(cfg)

    assert "demo_tool" in components["registry"].list_tools()
    payload = json.loads(
        asyncio.run(components["registry"].call("demo_tool", {"name": "codex"}))
    )
    assert payload == {"ok": True, "message": "hello codex"}


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
        lambda *args, **kwargs: console_messages.append(
            " ".join(str(arg) for arg in args)
        ),
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

    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg: (object(), "fake-model", 1024),
    )
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


def test_build_components_applies_orchestration_limits(monkeypatch, tmp_path):
    import agent as agent_module

    cfg = _minimal_cfg()
    cfg["orchestration"] = {
        "max_parallel_agents": 2,
        "sub_agent_timeout_seconds": 9,
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

    assert components["agent"].max_parallel_agents == 2
    assert components["agent"].sub_agent_timeout_seconds == 9


def test_spawn_agent_surfaces_error_and_inherits_parent_context(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    sentinel_context_manager = object()
    parent.context_manager = sentinel_context_manager
    parent.register_spawn_capability("base system prompt")

    parent_ctx = agent_module.AgentContext(system_prompt="augmented prompt")
    parent_ctx.metadata["skill_catalog"] = "catalog"
    parent_ctx.metadata["required_skills"] = ["quality/review"]
    parent._context_stack.append(parent_ctx)

    observed = {}

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        observed["context_manager"] = self.context_manager
        observed["required_skills"] = ctx.metadata.get("required_skills")
        observed["skill_catalog"] = ctx.metadata.get("skill_catalog")
        observed["system_prompt"] = ctx.system_prompt
        observed["user_message"] = user_message
        return agent_module.AgentResult(
            agent_id="sub",
            content="partial output",
            error="sub-agent failed",
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {"role": "critic", "task": "inspect the output"},
            )
        )
    )

    parent._context_stack.pop()

    assert observed["context_manager"] is sentinel_context_manager
    assert observed["required_skills"] == ["quality/review"]
    assert observed["skill_catalog"] == "catalog"
    assert observed["system_prompt"] == "augmented prompt"
    assert observed["user_message"] == "inspect the output"
    assert payload["ok"] is False
    assert payload["role"] == "critic"
    assert payload["error"] == "sub-agent failed"
    assert payload["content"] == "partial output"


def test_spawn_agent_enforces_timeout(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.sub_agent_timeout_seconds = 0.01
    parent.register_spawn_capability("base system prompt")

    async def slow_send_message(self, ctx, user_message, stream_callback=None):
        await asyncio.sleep(0.05)
        return agent_module.AgentResult(agent_id="sub", content="late")

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", slow_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {"role": "researcher", "task": "look it up"},
            )
        )
    )

    assert payload["ok"] is False
    assert payload["timed_out"] is True
    assert payload["role"] == "researcher"


def test_send_message_batches_spawn_calls_when_parallel_limit_is_one(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    agent.max_parallel_agents = 1

    spawn_tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "researcher", "task": "first"}),
            ),
        ),
        agent_module._OAITC(
            "call-2",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "critic", "task": "second"}),
            ),
        ),
    ]
    responses = iter(
        [
            agent_module._OAIResponse(
                [
                    agent_module._OAIChoice(
                        "tool_calls", agent_module._OAIMsg("", spawn_tool_calls)
                    )
                ]
            ),
            agent_module._OAIResponse(
                [
                    agent_module._OAIChoice(
                        "stop", agent_module._OAIMsg("final answer", None)
                    )
                ]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    calls = []

    async def fake_call(tool_name, tool_input):
        calls.append((tool_name, tool_input))
        return json.dumps({"ok": True})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "run parallel agents"))

    tool_messages = [msg for msg in ctx.messages if msg["role"] == "tool"]

    assert result.error is None
    assert result.content == "final answer"
    assert calls == [
        ("spawn_agent", {"role": "researcher", "task": "first"}),
        ("spawn_agent", {"role": "critic", "task": "second"}),
    ]
    assert len(tool_messages) == 2
    assert all("max_parallel_agents" not in msg["content"] for msg in tool_messages)


def test_send_message_batches_excess_parallel_spawn_calls(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    agent.max_parallel_agents = 2

    spawn_tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "researcher", "task": "first"}),
            ),
        ),
        agent_module._OAITC(
            "call-2",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "critic", "task": "second"}),
            ),
        ),
        agent_module._OAITC(
            "call-3",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "judge", "task": "third"}),
            ),
        ),
    ]
    responses = iter(
        [
            agent_module._OAIResponse(
                [
                    agent_module._OAIChoice(
                        "tool_calls", agent_module._OAIMsg("", spawn_tool_calls)
                    )
                ]
            ),
            agent_module._OAIResponse(
                [
                    agent_module._OAIChoice(
                        "stop", agent_module._OAIMsg("final answer", None)
                    )
                ]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    call_order = []
    concurrent = 0
    max_concurrent = 0

    async def fake_call(tool_name, tool_input):
        nonlocal concurrent, max_concurrent
        call_order.append(tool_input["role"])
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return json.dumps({"ok": True, "role": tool_input["role"]})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "run parallel agents"))

    tool_messages = [msg for msg in ctx.messages if msg["role"] == "tool"]

    assert result.error is None
    assert result.content == "final answer"
    assert call_order == ["researcher", "critic", "judge"]
    assert max_concurrent == 2
    assert len(tool_messages) == 3
    assert all("max_parallel_agents" not in msg["content"] for msg in tool_messages)


def test_send_message_classifies_request_timeout(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    async def fake_create(ctx, tools):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.content == ""
    assert result.error == "Model request timed out"


def test_memory_tidy_uses_force_tidy(monkeypatch):
    import agent as agent_module

    calls = {"force_tidy": 0, "tidy": 0, "closed": 0}

    class _FakeMemory:
        def force_tidy(self):
            calls["force_tidy"] += 1

        async def tidy(self, client, model):
            calls["tidy"] += 1

    async def fake_build_components_async(cfg):
        return {
            "memory": _FakeMemory(),
            "client": object(),
            "model": "fake-model",
        }

    async def fake_close_components(components):
        calls["closed"] += 1

    monkeypatch.setattr(agent_module, "load_config", lambda: ({}, False))
    monkeypatch.setattr(
        agent_module, "_build_components_async", fake_build_components_async
    )
    monkeypatch.setattr(agent_module, "_close_components", fake_close_components)

    agent_module.memory_tidy()

    assert calls == {"force_tidy": 1, "tidy": 1, "closed": 1}


def test_stream_response_propagates_stream_failures():
    import agent as agent_module

    class _BrokenAnthropicClient:
        class messages:
            @staticmethod
            def stream(**kwargs):
                class _BrokenStream:
                    async def __aenter__(self):
                        raise RuntimeError("stream exploded")

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                return _BrokenStream()

    agent = agent_module.BaseAgent(
        _BrokenAnthropicClient(),
        agent_module.ToolRegistry(),
        model="fake-model",
        api_format="anthropic",
    )
    ctx = agent_module.AgentContext(system_prompt="system")

    with pytest.raises(RuntimeError, match="stream exploded"):
        asyncio.run(agent._stream_response(ctx, [], lambda chunk: None))


def test_interactive_loop_does_not_auto_generate_tool_on_keyword_match(
    monkeypatch, tmp_path
):
    import agent as agent_module

    class _FakeAgent:
        api_format = "openai"
        max_tokens = 1024
        model = "fake-model"

        async def send_message(self, ctx, user_message, stream_callback=None):
            return agent_module.AgentResult(agent_id="agent", content="explained only")

    class _FakeMemory:
        def list_chapters(self):
            return []

    class _FakeEvolution:
        def __init__(self):
            self.calls = 0

        async def generate_tool(self, description, registry):
            self.calls += 1
            return "generated"

        def get_stats(self):
            return {"total": 0, "avg_score": 0}

    class _FakeSkillCatalog:
        def list_skills(self):
            return []

    class _FakeUserToolCatalog:
        def load_into_registry(self, registry):
            raise AssertionError("tool reload should not run")

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", prompts_dir)

    answers = iter(["Please explain how to generate a tool safely", "/quit"])
    monkeypatch.setattr(
        agent_module.Prompt,
        "ask",
        lambda *_args, **_kwargs: next(answers),
    )

    evolution = _FakeEvolution()
    components = {
        "agent": _FakeAgent(),
        "memory": _FakeMemory(),
        "evolution": evolution,
        "system_prompt": "system",
        "skill_catalog": _FakeSkillCatalog(),
        "user_tool_catalog": _FakeUserToolCatalog(),
        "registry": agent_module.ToolRegistry(),
        "output_dir": tmp_path / "output",
    }

    asyncio.run(agent_module._interactive_loop(components, _minimal_cfg()))

    assert evolution.calls == 0


def test_save_config_uses_atomic_replace(monkeypatch, tmp_path):
    import agent as agent_module

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(agent_module, "AGENT_HOME", tmp_path)
    monkeypatch.setattr(agent_module, "CONFIG_FILE", config_file)

    replace_calls: list[tuple[str, str]] = []
    real_replace = Path.replace

    def recording_replace(self, target):
        replace_calls.append((str(self), str(target)))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", recording_replace)

    agent_module.save_config({"active_provider": "fake"})

    assert json.loads(config_file.read_text(encoding="utf-8")) == {
        "active_provider": "fake"
    }
    assert replace_calls
