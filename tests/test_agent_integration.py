"""Tests for MCP/skill wiring and capability-aware prompts."""

import asyncio
import json
import logging
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
    assert "schedule_create" in registry.list_tools()
    assert "schedule_list" in registry.list_tools()
    assert "send_file" in registry.list_tools()
    assert "Available skills:" in prompt
    assert "quality/review" in prompt
    assert "Review code changes" in prompt
    assert skill_body not in prompt
    assert "Loaded skill tools" not in prompt
    assert "schedule_create" in prompt
    assert "use the schedule tools instead of saying you cannot act in the future" in prompt
    assert "Do not pretend the scheduled action has already run" in prompt
    assert "use `send_file` with the resolved file path" in prompt


def test_orchestration_planner_exposes_policy_from_skill_metadata(tmp_path):
    from agent.orchestration.planner import OrchestrationPlanner
    from agent.skills.catalog import SkillCatalog

    builtin_root = tmp_path / "builtin-skills"
    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        builtin_root,
        "multi-agent-orchestration",
        """---
name: Multi-Agent Orchestration
description: Decide orchestration mode
user-invocable: false
disable-model-invocation: true
planner-policy: orchestration
default-mode: direct
parallel-keywords: ["分别", "各自", "parallel", "multiple perspectives"]
pipeline-leading-keywords: ["先", "first"]
pipeline-followup-keywords: ["再", "然后", "then"]
pipeline-keywords: ["分阶段", "step by step"]
rendezvous-keywords: ["辩论", "debate", "正反", "多轮"]
max-rendezvous-rounds: 2
---
Policy body.
""",
    )

    catalog = SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()
    planner = OrchestrationPlanner.from_skill_catalog(catalog)

    decision = planner.decide(
        "请做一轮正反辩论，再给最终判断",
        tools_enabled=True,
        has_spawn_agent=True,
    )

    assert decision.mode == "rendezvous"
    assert "辩论" in decision.reason
    assert decision.max_rendezvous_rounds == 2


def test_send_message_executes_parallel_spawn_calls_via_internal_runtime(
    monkeypatch, tmp_path
):
    import agent as agent_module

    builtin_root = tmp_path / "builtin-skills"
    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        builtin_root,
        "multi-agent-orchestration",
        """---
name: Multi-Agent Orchestration
description: Decide orchestration mode
user-invocable: false
disable-model-invocation: true
planner-policy: orchestration
default-mode: direct
parallel-keywords: ["分别", "各自", "parallel", "multiple perspectives"]
pipeline-leading-keywords: ["先", "first"]
pipeline-followup-keywords: ["再", "然后", "then"]
pipeline-keywords: ["分阶段", "step by step"]
rendezvous-keywords: ["辩论", "debate", "正反", "多轮"]
max-rendezvous-rounds: 2
---
Policy body.
""",
    )

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    agent.register_spawn_capability("base system prompt")

    spawn_tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps(
                    {
                        "role": "researcher",
                        "task": "inspect performance",
                        "capability_profile": "read_only",
                    }
                ),
            ),
        ),
        agent_module._OAITC(
            "call-2",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps(
                    {
                        "role": "critic",
                        "task": "inspect correctness",
                        "capability_profile": "read_only",
                    }
                ),
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
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final answer", None))]
            ),
        ]
    )

    observed = {"calls": 0, "specs": []}

    async def fake_create(ctx, tools):
        observed["calls"] += 1
        observed["system_prompt"] = ctx.system_prompt
        return next(responses)

    async def fake_run_parallel_subtasks(specs, max_concurrency=None):
        observed["specs"] = [
            (spec.role, spec.task, spec.capability_profile) for spec in specs
        ]
        return [
            agent_module.SubtaskResult(
                id=spec.id,
                ok=True,
                content=f"content:{spec.role}",
                summary=f"summary:{spec.role}",
                tool_calls_made=["spawn_agent"],
            )
            for spec in specs
        ]

    async def fail_direct_spawn(tool_name, tool_input):
        raise AssertionError("spawn_agent should be executed via internal runtime")

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "run_parallel_subtasks", fake_run_parallel_subtasks)
    monkeypatch.setattr(registry, "call", fail_direct_spawn)

    ctx = agent_module.AgentContext(system_prompt="system")
    ctx.metadata["skill_catalog"] = catalog
    result = asyncio.run(
        agent.send_message(ctx, "请分别从性能和正确性两个角度 review 方案")
    )

    assert result.error is None
    assert result.content == "final answer"
    assert observed["calls"] == 2
    assert observed["specs"] == [
        ("researcher", "inspect performance", "read_only"),
        ("critic", "inspect correctness", "read_only"),
    ]
    assert "Planner selected orchestration mode" not in observed["system_prompt"]


def test_send_message_ignores_keyword_hint_when_spawn_plan_is_explicitly_parallel(
    monkeypatch, tmp_path
):
    import agent as agent_module

    builtin_root = tmp_path / "builtin-skills"
    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        builtin_root,
        "multi-agent-orchestration",
        """---
name: Multi-Agent Orchestration
description: Coordination policy
user-invocable: false
disable-model-invocation: true
max-rendezvous-rounds: 2
---
Policy body.
""",
    )

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    agent.register_spawn_capability("base system prompt")

    spawn_tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "researcher", "task": "inspect performance"}),
            ),
        ),
        agent_module._OAITC(
            "call-2",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "critic", "task": "inspect correctness"}),
            ),
        ),
    ]
    responses = iter(
        [
            agent_module._OAIResponse(
                [agent_module._OAIChoice("tool_calls", agent_module._OAIMsg("", spawn_tool_calls))]
            ),
            agent_module._OAIResponse(
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final answer", None))]
            ),
        ]
    )

    observed = {}

    async def fake_create(ctx, tools):
        return next(responses)

    async def fake_run_parallel_subtasks(specs, max_concurrency=None):
        observed["parallel"] = [spec.id for spec in specs]
        return [
            agent_module.SubtaskResult(
                id=spec.id,
                ok=True,
                content=f"content:{spec.id}",
                summary=f"summary:{spec.id}",
                tool_calls_made=["spawn_agent"],
            )
            for spec in specs
        ]

    async def fail_pipeline(specs):
        raise AssertionError("pipeline mode should not be forced by user-message keywords")

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "run_parallel_subtasks", fake_run_parallel_subtasks)
    monkeypatch.setattr(agent, "run_pipeline_subtasks", fail_pipeline)

    ctx = agent_module.AgentContext(system_prompt="system")
    ctx.metadata["skill_catalog"] = catalog
    result = asyncio.run(agent.send_message(ctx, "先看性能，再看正确性"))

    assert result.error is None
    assert observed["parallel"] == ["call-1", "call-2"]


def test_builtin_skill_activation_text_uses_logical_bundle_root(tmp_path):
    import agent as agent_module

    builtin_root = tmp_path / "builtin-skills"
    _write_skill_bundle(
        builtin_root,
        "quality/review",
        """---
name: Review
description: Review code changes
user-invocable: true
---
Follow the review checklist.
""",
        {"template.md": "Checklist template"},
    )

    catalog = agent_module.SkillCatalog(
        user_root=tmp_path / "user-skills", builtin_root=builtin_root
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    registry.set_context("output_dir", str(tmp_path / "output"))
    catalog.register_tools(registry)

    text = catalog.activation_text("quality/review", explicit=True)

    assert text is not None
    assert "Bundle root: builtin://quality/review" in text
    assert str(builtin_root) not in text


# ── Skill management tool tests ──────────────────────────────────────────────


def test_create_skill_creates_bundle_in_user_root(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "create_skill",
                {
                    "skill_id": "my-review",
                    "name": "My Review",
                    "description": "Custom review skill",
                    "instructions": "Review all code carefully.",
                },
            )
        )
    )

    assert result["ok"] is True
    assert result["skill_id"] == "my-review"
    assert (user_root / "my-review" / "SKILL.md").exists()

    # Verify the skill is discoverable after creation
    bundle = catalog.get("my-review")
    assert bundle is not None
    assert bundle.name == "My Review"
    assert bundle.description == "Custom review skill"
    assert bundle.source == "user"
    assert bundle.body == "Review all code carefully."


def test_create_skill_rejects_duplicate_id(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

    _write_skill_bundle(
        user_root,
        "existing",
        "---\nname: Existing\n---\nBody.",
    )

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "create_skill",
                {"skill_id": "existing", "name": "Duplicate"},
            )
        )
    )

    assert result["ok"] is False
    assert "already exists" in result["error"]


def test_create_skill_validates_skill_id(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    # Empty ID
    result = json.loads(
        asyncio.run(registry.call("create_skill", {"skill_id": "", "name": "Bad"}))
    )
    assert result["ok"] is False

    # ID with spaces
    result = json.loads(
        asyncio.run(
            registry.call("create_skill", {"skill_id": "has space", "name": "Bad"})
        )
    )
    assert result["ok"] is False

    # ID with ..
    result = json.loads(
        asyncio.run(
            registry.call("create_skill", {"skill_id": "../escape", "name": "Bad"})
        )
    )
    assert result["ok"] is False


def test_create_skill_with_nested_id(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "builtin"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "create_skill",
                {
                    "skill_id": "quality/lint",
                    "name": "Lint",
                    "description": "Run linters",
                    "instructions": "Lint the code.",
                },
            )
        )
    )

    assert result["ok"] is True
    assert (user_root / "quality" / "lint" / "SKILL.md").exists()
    bundle = catalog.get("quality/lint")
    assert bundle is not None
    assert bundle.name == "Lint"


def test_update_skill_modifies_user_skill(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

    _write_skill_bundle(
        user_root,
        "my-tool",
        "---\nname: Old Name\ndescription: Old desc\n---\nOld body.",
    )

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "update_skill",
                {
                    "skill_id": "my-tool",
                    "name": "New Name",
                    "description": "New desc",
                    "instructions": "New body.",
                },
            )
        )
    )

    assert result["ok"] is True
    bundle = catalog.get("my-tool")
    assert bundle is not None
    assert bundle.name == "New Name"
    assert bundle.description == "New desc"
    assert bundle.body == "New body."


def test_update_skill_partial_update_preserves_unset_fields(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        user_root,
        "partial",
        "---\nname: Keep Me\ndescription: Also keep\n---\nOriginal body.",
    )

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    # Only update description
    result = json.loads(
        asyncio.run(
            registry.call(
                "update_skill",
                {"skill_id": "partial", "description": "Updated desc only"},
            )
        )
    )

    assert result["ok"] is True
    bundle = catalog.get("partial")
    assert bundle.name == "Keep Me"
    assert bundle.description == "Updated desc only"
    assert bundle.body == "Original body."


def test_update_skill_empty_instructions_preserves_existing_body(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        user_root,
        "partial",
        "---\nname: Keep Me\ndescription: Also keep\n---\nOriginal body.",
    )

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "update_skill",
                {"skill_id": "partial", "instructions": ""},
            )
        )
    )

    assert result["ok"] is True
    bundle = catalog.get("partial")
    assert bundle is not None
    assert bundle.body == "Original body."


def test_update_skill_rejects_builtin(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

    _write_skill_bundle(
        builtin_root,
        "builtin-only",
        "---\nname: BuiltIn\n---\nBuilt-in body.",
    )

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "update_skill",
                {"skill_id": "builtin-only", "name": "Hacked"},
            )
        )
    )

    assert result["ok"] is False
    assert "built-in" in result["error"].lower()


def test_delete_skill_removes_user_skill(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        user_root,
        "to-delete",
        "---\nname: Doomed\n---\nGoodbye.",
        {"extra.txt": "extra content"},
    )

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    assert catalog.get("to-delete") is not None

    result = json.loads(
        asyncio.run(registry.call("delete_skill", {"skill_id": "to-delete"}))
    )

    assert result["ok"] is True
    assert not (user_root / "to-delete").exists()
    assert catalog.get("to-delete") is None


def test_delete_skill_rejects_builtin(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

    _write_skill_bundle(
        builtin_root,
        "protected",
        "---\nname: Protected\n---\nDo not delete.",
    )

    catalog = agent_module.SkillCatalog(user_root=user_root, builtin_root=builtin_root)
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(registry.call("delete_skill", {"skill_id": "protected"}))
    )

    assert result["ok"] is False
    assert "built-in" in result["error"].lower()
    assert (builtin_root / "protected" / "SKILL.md").exists()


def test_delete_skill_returns_error_for_unknown(tmp_path):
    import agent as agent_module

    catalog = agent_module.SkillCatalog(
        user_root=tmp_path / "u", builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(registry.call("delete_skill", {"skill_id": "nonexistent"}))
    )

    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_write_skill_file_creates_supporting_file(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        user_root,
        "writable",
        "---\nname: Writable\n---\nBody.",
    )

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "write_skill_file",
                {
                    "skill_name": "writable",
                    "path": "templates/checklist.md",
                    "content": "# Checklist\n- [ ] Item 1",
                },
            )
        )
    )

    assert result["ok"] is True
    written = (user_root / "writable" / "templates" / "checklist.md").read_text()
    assert "# Checklist" in written

    # Verify the file shows up in supporting files after reload
    bundle = catalog.get("writable")
    assert "templates/checklist.md" in bundle.supporting_files


def test_write_skill_file_rejects_builtin_skill(tmp_path):
    import agent as agent_module

    builtin_root = tmp_path / "builtin-skills"
    _write_skill_bundle(
        builtin_root,
        "locked",
        "---\nname: Locked\n---\nDo not modify.",
    )

    catalog = agent_module.SkillCatalog(
        user_root=tmp_path / "u", builtin_root=builtin_root
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "write_skill_file",
                {
                    "skill_name": "locked",
                    "path": "hack.txt",
                    "content": "pwned",
                },
            )
        )
    )

    assert result["ok"] is False
    assert "built-in" in result["error"].lower()


def test_write_skill_file_rejects_skill_md_override(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        user_root,
        "guarded",
        "---\nname: Guarded\n---\nOriginal.",
    )

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "write_skill_file",
                {
                    "skill_name": "guarded",
                    "path": "SKILL.md",
                    "content": "Overwritten!",
                },
            )
        )
    )

    assert result["ok"] is False
    assert "update_skill" in result["error"]


def test_write_skill_file_rejects_path_escape(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(
        user_root,
        "sandboxed",
        "---\nname: Sandboxed\n---\nBody.",
    )

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    result = json.loads(
        asyncio.run(
            registry.call(
                "write_skill_file",
                {
                    "skill_name": "sandboxed",
                    "path": "../../etc/passwd",
                    "content": "bad",
                },
            )
        )
    )

    assert result["ok"] is False
    assert "escape" in result["error"].lower()


# ── Hot-reload tests ─────────────────────────────────────────────────────────


def test_consume_dirty_returns_true_after_reload(tmp_path):
    import agent as agent_module

    catalog = agent_module.SkillCatalog(
        user_root=tmp_path / "u", builtin_root=tmp_path / "b"
    )
    catalog.load_all()

    # Initial load_all does not set dirty (only reload does)
    assert catalog.consume_dirty() is False

    # reload() sets dirty
    catalog.reload()
    assert catalog.consume_dirty() is True
    # consume clears the flag
    assert catalog.consume_dirty() is False


def test_create_skill_sets_dirty_flag(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    assert catalog.consume_dirty() is False

    asyncio.run(
        registry.call(
            "create_skill",
            {"skill_id": "hot-test", "name": "Hot Test", "instructions": "Test body."},
        )
    )

    # create_skill calls reload() which sets dirty
    assert catalog.consume_dirty() is True
    assert catalog.get("hot-test") is not None


def test_update_skill_sets_dirty_flag(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(user_root, "mutable", "---\nname: Mutable\n---\nBody.")

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    assert catalog.consume_dirty() is False

    asyncio.run(
        registry.call(
            "update_skill",
            {"skill_id": "mutable", "description": "Updated"},
        )
    )

    assert catalog.consume_dirty() is True


def test_delete_skill_sets_dirty_flag(tmp_path):
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    _write_skill_bundle(user_root, "doomed", "---\nname: Doomed\n---\nGone.")

    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    assert catalog.consume_dirty() is False

    asyncio.run(registry.call("delete_skill", {"skill_id": "doomed"}))

    assert catalog.consume_dirty() is True
    assert catalog.get("doomed") is None


def test_hot_reload_updates_summary_lines(tmp_path):
    """After create_skill, summary_lines reflects the new skill."""
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    catalog = agent_module.SkillCatalog(
        user_root=user_root, builtin_root=tmp_path / "b"
    )
    catalog.load_all()
    registry = agent_module.ToolRegistry()
    catalog.register_tools(registry)

    # No skills initially
    assert catalog.summary_lines() == []

    asyncio.run(
        registry.call(
            "create_skill",
            {
                "skill_id": "fresh",
                "name": "Fresh",
                "description": "A freshly created skill",
            },
        )
    )

    # summary_lines should now include the new skill
    summary = "\n".join(catalog.summary_lines())
    assert "fresh" in summary
    assert "A freshly created skill" in summary


def test_skill_manager_builtin_skill_is_discovered(tmp_path):
    """The skill-manager built-in skill should be discovered from the package builtin skills dir."""
    import agent as agent_module

    # Use the package-exported builtin skill root instead of the old single-file path.
    real_builtin = agent_module.BUILTIN_SKILLS_DIR
    catalog = agent_module.SkillCatalog(
        user_root=tmp_path / "user-skills", builtin_root=real_builtin
    )
    catalog.load_all()

    bundle = catalog.get("skill-manager")
    assert bundle is not None
    assert bundle.source == "builtin"
    assert bundle.name == "Skill Manager"
    assert (
        "create" in bundle.description.lower() or "manage" in bundle.description.lower()
    )
    assert bundle.user_invocable is True
    # Should discover bundled scripts as supporting files
    assert any("scripts/init_skill.py" in f for f in bundle.supporting_files)
    assert any("scripts/quick_validate.py" in f for f in bundle.supporting_files)


def test_build_components_registers_skill_management_tools(monkeypatch, tmp_path):
    """Verify skill management tools are registered alongside runtime tools."""
    import agent as agent_module

    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"

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

    # All skill management tools should be registered
    tool_names = registry.list_tools()
    assert "create_skill" in tool_names
    assert "update_skill" in tool_names
    assert "delete_skill" in tool_names
    assert "write_skill_file" in tool_names


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


def test_build_components_exposes_multi_agent_orchestration_skill(monkeypatch, tmp_path):
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
    skill_catalog = components["skill_catalog"]
    bundle = skill_catalog.get("multi-agent-orchestration")

    assert bundle is not None
    assert bundle.source == "builtin"
    assert "parallel" in bundle.body.lower()
    assert "rendezvous" in bundle.body.lower()
    assert "multi-agent-orchestration" in "\n".join(skill_catalog.summary_lines())


def test_system_prompt_keeps_spawn_agent_as_only_public_delegation_tool(
    monkeypatch, tmp_path
):
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
    prompt = components["system_prompt"]

    assert "spawn_agent" in prompt
    assert "team_run" not in prompt
    assert "full_history" not in prompt
    assert "repeat until positions converge" not in prompt
    assert "lead-controlled" in prompt


def test_sub_agents_do_not_receive_orchestration_public_surface(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    observed = {}

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        observed["system_prompt"] = ctx.system_prompt
        return agent_module.AgentResult(agent_id="sub", content="ok")

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    asyncio.run(
        registry.call(
            "spawn_agent",
            {"role": "critic", "task": "inspect"},
        )
    )

    assert "spawn_agent" not in observed["system_prompt"]
    assert "team_run" not in observed["system_prompt"]


def test_orchestration_runtime_exports_minimal_types():
    from agent.orchestration import SubtaskResult, SubtaskSpec

    spec = SubtaskSpec(id="s1", role="reviewer", task="inspect the change")
    result = SubtaskResult(
        id="s1",
        ok=True,
        content="summary",
        tool_calls_made=["read_file"],
    )

    assert spec.id == "s1"
    assert spec.depends_on == []
    assert spec.write_scope == []
    assert spec.output_contract == {}
    assert result.ok is True
    assert result.summary == ""
    assert result.structured_content is None
    assert result.error is None


def test_run_parallel_subtasks_executes_independent_specs():
    import asyncio

    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_parallel_subtasks

    async def run():
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def executor(spec: SubtaskSpec) -> SubtaskResult:
            if spec.id == "first":
                first_started.set()
                await second_started.wait()
            else:
                second_started.set()
                await first_started.wait()
            return SubtaskResult(
                id=spec.id,
                ok=True,
                content=f"done:{spec.id}",
                tool_calls_made=[],
            )

        return await asyncio.wait_for(
            run_parallel_subtasks(
                [
                    SubtaskSpec(id="first", role="reviewer", task="a"),
                    SubtaskSpec(id="second", role="reviewer", task="b"),
                ],
                executor=executor,
                max_concurrency=2,
            ),
            timeout=1,
        )

    results = asyncio.run(run())

    assert [result.id for result in results] == ["first", "second"]
    assert all(result.ok for result in results)


def test_run_parallel_subtasks_preserves_sibling_results_on_failure():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_parallel_subtasks

    async def executor(spec: SubtaskSpec) -> SubtaskResult:
        if spec.id == "boom":
            raise RuntimeError("failed")
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"done:{spec.id}",
            tool_calls_made=[],
        )

    results = asyncio.run(
        run_parallel_subtasks(
            [
                SubtaskSpec(id="ok", role="reviewer", task="a"),
                SubtaskSpec(id="boom", role="reviewer", task="b"),
            ],
            executor=executor,
            max_concurrency=2,
        )
    )

    assert len(results) == 2
    assert results[0].id == "ok"
    assert results[0].ok is True
    assert results[1].id == "boom"
    assert results[1].ok is False
    assert results[1].error == "failed"


def test_run_parallel_subtasks_reports_telemetry():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_parallel_subtasks

    telemetry = {}

    async def executor(spec: SubtaskSpec) -> SubtaskResult:
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"done:{spec.id}",
            tool_calls_made=[],
        )

    results = asyncio.run(
        run_parallel_subtasks(
            [
                SubtaskSpec(id="a", role="reviewer", task="a"),
                SubtaskSpec(id="b", role="reviewer", task="b"),
            ],
            executor=executor,
            max_concurrency=2,
            telemetry=telemetry,
        )
    )

    assert [result.id for result in results] == ["a", "b"]
    assert telemetry["execution_mode"] == "parallel"
    assert telemetry["spec_count"] == 2
    assert telemetry["max_concurrency"] == 2
    assert telemetry["write_scope_count"] == 0
    assert telemetry["write_scope_check_seconds"] >= 0
    assert telemetry["duration_seconds"] >= telemetry["write_scope_check_seconds"]


def test_run_pipeline_subtasks_executes_in_dependency_order():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_pipeline_subtasks

    execution_order = []

    async def executor(spec: SubtaskSpec, upstream_summaries: dict[str, str]) -> SubtaskResult:
        execution_order.append(spec.id)
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}",
            summary=f"summary:{spec.id}",
            tool_calls_made=[],
        )

    results = asyncio.run(
        run_pipeline_subtasks(
            [
                SubtaskSpec(id="a", role="researcher", task="a"),
                SubtaskSpec(id="b", role="reviewer", task="b", depends_on=["a"]),
            ],
            executor=executor,
        )
    )

    assert execution_order == ["a", "b"]
    assert [result.id for result in results] == ["a", "b"]


def test_run_pipeline_subtasks_passes_summary_only():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_pipeline_subtasks

    observed = {}

    async def executor(spec: SubtaskSpec, upstream_summaries: dict[str, str]) -> SubtaskResult:
        observed[spec.id] = dict(upstream_summaries)
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"raw-content:{spec.id}",
            summary=f"summary:{spec.id}",
            tool_calls_made=[],
        )

    asyncio.run(
        run_pipeline_subtasks(
            [
                SubtaskSpec(id="a", role="researcher", task="a"),
                SubtaskSpec(id="b", role="reviewer", task="b", depends_on=["a"]),
            ],
            executor=executor,
        )
    )

    assert observed["a"] == {}
    assert observed["b"] == {"a": "summary:a"}


def test_run_pipeline_subtasks_can_pass_upstream_results_when_executor_accepts_them():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_pipeline_subtasks

    observed = {}

    async def executor(
        spec: SubtaskSpec,
        upstream_summaries: dict[str, str],
        *,
        upstream_results: dict[str, SubtaskResult],
    ) -> SubtaskResult:
        observed[spec.id] = {
            "summaries": dict(upstream_summaries),
            "structured": {
                dep: result.structured_content
                for dep, result in upstream_results.items()
            },
        }
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"raw-content:{spec.id}",
            summary=f"summary:{spec.id}",
            structured_content={"id": spec.id},
            tool_calls_made=[],
        )

    asyncio.run(
        run_pipeline_subtasks(
            [
                SubtaskSpec(id="a", role="researcher", task="a"),
                SubtaskSpec(id="b", role="reviewer", task="b", depends_on=["a"]),
            ],
            executor=executor,
        )
    )

    assert observed["b"] == {
        "summaries": {"a": "summary:a"},
        "structured": {"a": {"id": "a"}},
    }


def test_run_pipeline_subtasks_stops_after_upstream_failure():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_pipeline_subtasks

    executed = []

    async def executor(spec: SubtaskSpec, upstream_summaries: dict[str, str]) -> SubtaskResult:
        executed.append(spec.id)
        if spec.id == "a":
            return SubtaskResult(
                id="a",
                ok=False,
                content="partial",
                summary="",
                tool_calls_made=[],
                error="upstream failed",
            )
        raise AssertionError("downstream stage should not execute after upstream failure")

    results = asyncio.run(
        run_pipeline_subtasks(
            [
                SubtaskSpec(id="a", role="researcher", task="a"),
                SubtaskSpec(id="b", role="reviewer", task="b", depends_on=["a"]),
            ],
            executor=executor,
        )
    )

    assert executed == ["a"]
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == "upstream failed"


def test_run_pipeline_subtasks_executes_ready_stage_in_parallel():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_pipeline_subtasks

    async def run():
        b_started = asyncio.Event()
        c_started = asyncio.Event()

        async def executor(
            spec: SubtaskSpec, upstream_summaries: dict[str, str]
        ) -> SubtaskResult:
            if spec.id == "b":
                b_started.set()
                await c_started.wait()
            elif spec.id == "c":
                c_started.set()
                await b_started.wait()
            return SubtaskResult(
                id=spec.id,
                ok=True,
                content=f"content:{spec.id}",
                summary=f"summary:{spec.id}",
                tool_calls_made=[],
            )

        return await asyncio.wait_for(
            run_pipeline_subtasks(
                [
                    SubtaskSpec(id="a", role="researcher", task="a"),
                    SubtaskSpec(id="b", role="reviewer", task="b", depends_on=["a"]),
                    SubtaskSpec(id="c", role="critic", task="c", depends_on=["a"]),
                ],
                executor=executor,
            ),
            timeout=1,
        )

    results = asyncio.run(run())

    assert [result.id for result in results] == ["a", "b", "c"]


def test_run_rendezvous_round_uses_lead_summary_for_followup():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_rendezvous_round

    observed_round_inputs = {}

    async def executor(
        spec: SubtaskSpec,
        *,
        round_index: int,
        lead_summary: str,
    ) -> SubtaskResult:
        observed_round_inputs[(spec.id, round_index)] = lead_summary
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}:round:{round_index}",
            summary=f"summary:{spec.id}:round:{round_index}",
            tool_calls_made=[],
        )

    def summarize(results):
        return "lead-summary"

    results = asyncio.run(
        run_rendezvous_round(
            [
                SubtaskSpec(id="a", role="researcher", task="a"),
                SubtaskSpec(id="b", role="critic", task="b"),
            ],
            executor=executor,
            summarize=summarize,
            max_rounds=2,
        )
    )

    assert len(results) == 4
    assert observed_round_inputs[("a", 1)] == ""
    assert observed_round_inputs[("b", 1)] == ""
    assert observed_round_inputs[("a", 2)] == "lead-summary"
    assert observed_round_inputs[("b", 2)] == "lead-summary"


def test_run_rendezvous_round_can_stop_or_narrow_followup():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import RendezvousDirective, run_rendezvous_round

    observed_rounds = []

    async def executor(
        spec: SubtaskSpec,
        *,
        round_index: int,
        lead_summary: str,
    ) -> SubtaskResult:
        observed_rounds.append((spec.id, round_index, lead_summary))
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}:round:{round_index}",
            summary=f"summary:{spec.id}:round:{round_index}",
            tool_calls_made=[],
        )

    def summarize(results):
        if len(results) == 2:
            return RendezvousDirective(
                summary="focus-on-a",
                continue_with=["a"],
            )
        return RendezvousDirective(summary="done", stop=True)

    results = asyncio.run(
        run_rendezvous_round(
            [
                SubtaskSpec(id="a", role="researcher", task="a"),
                SubtaskSpec(id="b", role="critic", task="b"),
            ],
            executor=executor,
            summarize=summarize,
            max_rounds=3,
        )
    )

    assert [result.id for result in results] == ["a", "b", "a"]
    assert observed_rounds == [
        ("a", 1, ""),
        ("b", 1, ""),
        ("a", 2, "focus-on-a"),
    ]


def test_run_rendezvous_round_executes_each_round_in_parallel():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_rendezvous_round

    async def run():
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def executor(
            spec: SubtaskSpec,
            *,
            round_index: int,
            lead_summary: str,
        ) -> SubtaskResult:
            if round_index == 1:
                if spec.id == "a":
                    first_started.set()
                    await second_started.wait()
                else:
                    second_started.set()
                    await first_started.wait()
            return SubtaskResult(
                id=spec.id,
                ok=True,
                content=f"content:{spec.id}:{round_index}",
                summary=f"summary:{spec.id}:{round_index}",
                tool_calls_made=[],
            )

        return await asyncio.wait_for(
            run_rendezvous_round(
                [
                    SubtaskSpec(id="a", role="researcher", task="a"),
                    SubtaskSpec(id="b", role="critic", task="b"),
                ],
                executor=executor,
                summarize=lambda results: "lead-summary",
                max_rounds=2,
            ),
            timeout=1,
        )

    results = asyncio.run(run())

    assert len(results) == 4


def test_run_rendezvous_round_enforces_round_limit():
    from agent.orchestration import SubtaskResult, SubtaskSpec
    from agent.orchestration.runtime import run_rendezvous_round

    async def executor(
        spec: SubtaskSpec,
        *,
        round_index: int,
        lead_summary: str,
    ) -> SubtaskResult:
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content="content",
            summary=f"summary:{round_index}",
            tool_calls_made=[],
        )

    results = asyncio.run(
        run_rendezvous_round(
            [SubtaskSpec(id="a", role="researcher", task="a")],
            executor=executor,
            summarize=lambda results: "summary",
            max_rounds=1,
        )
    )

    assert len(results) == 1


def test_parallel_orchestration_rejects_overlapping_write_scope():
    from agent.orchestration import SubtaskSpec
    from agent.orchestration.runtime import run_parallel_subtasks

    async def executor(spec):
        raise AssertionError("executor should not be called when write scopes overlap")

    with pytest.raises(ValueError, match="overlapping write_scope"):
        asyncio.run(
            run_parallel_subtasks(
                [
                    SubtaskSpec(
                        id="a",
                        role="implementer",
                        task="a",
                        write_scope=["agent/core"],
                    ),
                    SubtaskSpec(
                        id="b",
                        role="reviewer",
                        task="b",
                        write_scope=[str(Path.cwd() / "agent" / "core" / "agent.py")],
                    ),
                ],
                executor=executor,
                max_concurrency=2,
            )
        )


def test_workspace_path_helpers_share_canonical_scope_semantics(tmp_path):
    from agent.pathing import canonicalize_user_path, path_contains, paths_overlap, resolve_workspace_path

    scope_path = canonicalize_user_path("src", base_dir=tmp_path)
    target_path, root_kind = resolve_workspace_path(
        str(tmp_path / "src" / "main.py"),
        workspace_root=tmp_path,
    )

    assert root_kind == "workspace"
    assert path_contains(scope_path, target_path) is True
    assert paths_overlap(scope_path, target_path) is True


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
    # B3 fix: sub-agent must be built from base_system_prompt + sub-registry
    # capabilities, NOT from the parent's LTM-mutated active context prompt.
    # The composed prompt starts with the base and appends "## Active Capabilities".
    assert observed["system_prompt"].startswith("base system prompt")
    assert "## Active Capabilities" in observed["system_prompt"]
    # spawn_agent must NOT be listed in sub-agent capabilities (it was excluded).
    assert "spawn_agent" not in observed["system_prompt"]
    assert observed["user_message"] == "inspect the output"
    assert payload["ok"] is False
    assert payload["role"] == "critic"
    assert payload["error"] == "sub-agent failed"
    assert payload["content"] == "partial output"


def test_spawn_agent_restricts_subagent_tools_for_read_only_profile(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    registry.register(
        "read_file",
        "Read files",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"ok": True},
        source="test",
        capabilities=("read",),
    )
    registry.register(
        "write_file",
        "Write files",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"ok": True},
        source="test",
        capabilities=("workspace_write",),
    )
    registry.register(
        "shell",
        "Run shell commands",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"ok": True},
        source="test",
        capabilities=("shell",),
    )
    registry.register(
        "dangerous_mutator",
        "Custom mutating tool without declared capabilities",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"ok": True},
        source="plugin:test",
    )
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    observed = {}

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        observed["tools"] = sorted(self.registry.list_tools())
        return agent_module.AgentResult(agent_id="sub", content="ok")

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "critic",
                    "task": "inspect",
                    "capability_profile": "read_only",
                },
            )
        )
    )

    assert payload["ok"] is True
    assert observed["tools"] == ["read_file"]


def test_spawn_agent_validates_expected_output_contract(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        return agent_module.AgentResult(agent_id="sub", content="plain text without contract")

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "researcher",
                    "task": "summarize the change",
                    "expected_output": "A concise summary paragraph.",
                },
            )
        )
    )

    assert payload["ok"] is False
    assert "expected output contract" in payload["error"].lower()


def test_spawn_agent_extracts_deliverable_from_expected_output_contract(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        return agent_module.AgentResult(
            agent_id="sub",
            content=(
                "Some working notes.\n"
                "<deliverable>\n"
                "Final summary paragraph.\n"
                "</deliverable>\n"
                "Ignored tail."
            ),
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "researcher",
                    "task": "summarize the change",
                    "expected_output": "A concise summary paragraph.",
                },
            )
        )
    )

    assert payload["ok"] is True
    assert payload["content"] == "Final summary paragraph."


def test_spawn_agent_validates_json_output_contract_required_keys(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        return agent_module.AgentResult(
            agent_id="sub",
            content='<deliverable>{"summary":"done"}</deliverable>',
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "researcher",
                    "task": "summarize the change",
                    "output_contract": {
                        "format": "json",
                        "required_keys": ["summary", "risks"],
                    },
                },
            )
        )
    )

    assert payload["ok"] is False
    assert "required deliverable keys" in payload["error"].lower()


def test_spawn_agent_accepts_json_output_contract(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        return agent_module.AgentResult(
            agent_id="sub",
            content='<deliverable>{"summary":"done","risks":[]}</deliverable>',
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "researcher",
                    "task": "summarize the change",
                    "output_contract": {
                        "format": "json",
                        "required_keys": ["summary", "risks"],
                    },
                },
            )
        )
    )

    assert payload["ok"] is True
    assert json.loads(payload["content"]) == {"summary": "done", "risks": []}


def test_spawn_agent_validates_required_output_files(monkeypatch, tmp_path):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt", workspace_root=tmp_path)

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        return agent_module.AgentResult(
            agent_id="sub",
            content="<deliverable>done</deliverable>",
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "implementer",
                    "task": "build artifact",
                    "write_scope": ["artifact.json"],
                    "output_contract": {
                        "required_files": ["artifact.json"],
                    },
                },
            )
        )
    )

    assert payload["ok"] is False
    assert "required output file" in payload["error"].lower()


def test_spawn_agent_accepts_file_only_output_contract_without_deliverable(
    monkeypatch, tmp_path
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt", workspace_root=tmp_path)

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        (tmp_path / "artifact.json").write_text('{"ok": true}', encoding="utf-8")
        return agent_module.AgentResult(
            agent_id="sub",
            content="artifact written",
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "implementer",
                    "task": "build artifact",
                    "write_scope": ["artifact.json"],
                    "output_contract": {
                        "required_files": ["artifact.json"],
                    },
                },
            )
        )
    )

    assert payload["ok"] is True
    assert payload["content"] == "artifact written"


def test_spawn_agent_requires_deliverable_when_json_and_files_are_both_requested(
    monkeypatch, tmp_path
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt", workspace_root=tmp_path)

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        (tmp_path / "artifact.json").write_text("{}", encoding="utf-8")
        return agent_module.AgentResult(
            agent_id="sub",
            content="artifact written without deliverable block",
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "implementer",
                    "task": "build artifact",
                    "write_scope": ["artifact.json"],
                    "output_contract": {
                        "format": "json",
                        "required_keys": ["summary"],
                        "required_files": ["artifact.json"],
                    },
                },
            )
        )
    )

    assert payload["ok"] is False
    assert "missing <deliverable> block" in payload["error"].lower()


def test_concurrent_send_message_isolates_spawn_parent_context(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    agent.register_spawn_capability("base system prompt")

    first_entered_create = asyncio.Event()
    second_entered_create = asyncio.Event()
    first_can_return_tool_call = asyncio.Event()
    captured_sub_contexts = []
    create_calls_by_context = {}

    def _tool_response(task: str):
        return agent_module._OAIResponse(
            [
                agent_module._OAIChoice(
                    "tool_calls",
                    agent_module._OAIMsg(
                        "",
                        [
                            agent_module._OAITC(
                                f"call-{task}",
                                agent_module._OAIFunc(
                                    "spawn_agent",
                                    json.dumps({"role": "worker", "task": task}),
                                ),
                            )
                        ],
                    ),
                )
            ]
        )

    def _final_response(label: str):
        return agent_module._OAIResponse(
            [agent_module._OAIChoice("stop", agent_module._OAIMsg(label, None))]
        )

    async def fake_create(ctx, tools):
        calls = create_calls_by_context.get(ctx.agent_id, 0)
        create_calls_by_context[ctx.agent_id] = calls + 1
        if calls == 0 and ctx.metadata["label"] == "first":
            first_entered_create.set()
            await second_entered_create.wait()
            await first_can_return_tool_call.wait()
            return _tool_response("first-task")
        if calls == 0 and ctx.metadata["label"] == "second":
            second_entered_create.set()
            await first_entered_create.wait()
            first_can_return_tool_call.set()
            return _tool_response("second-task")
        return _final_response(f"final-{ctx.metadata['label']}")

    async def fake_sub_send_message(self, ctx, user_message, stream_callback=None):
        captured_sub_contexts.append(
            {
                "task": user_message,
                "required_skills": list(ctx.metadata.get("required_skills", [])),
            }
        )
        return agent_module.AgentResult(agent_id=ctx.agent_id, content="ok")

    monkeypatch.setattr(agent, "_create", fake_create)
    original_send_message = agent_module.BaseAgent.send_message

    async def send_message_dispatch(self, ctx, user_message, stream_callback=None):
        if self is agent:
            return await original_send_message(self, ctx, user_message, stream_callback)
        return await fake_sub_send_message(self, ctx, user_message, stream_callback)

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", send_message_dispatch)

    first_ctx = agent_module.AgentContext(system_prompt="system")
    first_ctx.metadata["label"] = "first"
    first_ctx.metadata["required_skills"] = ["skill:first"]
    second_ctx = agent_module.AgentContext(system_prompt="system")
    second_ctx.metadata["label"] = "second"
    second_ctx.metadata["required_skills"] = ["skill:second"]

    async def run_concurrent():
        return await asyncio.gather(
            agent.send_message(first_ctx, "first message"),
            agent.send_message(second_ctx, "second message"),
        )

    results = asyncio.run(run_concurrent())

    assert [result.error for result in results] == [None, None]
    assert sorted(captured_sub_contexts, key=lambda item: item["task"]) == [
        {"task": "first-task", "required_skills": ["skill:first"]},
        {"task": "second-task", "required_skills": ["skill:second"]},
    ]

def test_spawn_agent_reports_events_to_active_sink(monkeypatch):
    import agent as agent_module

    class _Sink:
        def __init__(self):
            self.events = []

        def on_subagent_event(self, event):
            self.events.append(event)

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt")

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        return agent_module.AgentResult(
            agent_id="sub",
            content="done",
            tool_calls_made=["bash"],
        )

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    sink = _Sink()
    token = agent_module._active_sink.set(sink)
    try:
        payload = json.loads(
            asyncio.run(
                registry.call(
                    "spawn_agent",
                    {"role": "researcher", "task": "inspect code"},
                )
            )
        )
    finally:
        agent_module._active_sink.reset(token)

    assert payload["ok"] is True
    assert [event.kind for event in sink.events] == [
        "agent_started",
        "agent_finished",
    ]
    assert sink.events[0].role == "researcher"
    assert sink.events[1].message


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


def test_base_agent_internal_orchestration_preserves_partial_content(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    async def fake_execute(role, task, **kwargs):
        return {
            "ok": False,
            "role": role,
            "task": task,
            "timed_out": True,
            "partial_content": "partial-result",
            "error": "timed out",
        }

    monkeypatch.setattr(agent, "_execute_agent", fake_execute)

    result = asyncio.run(
        agent._execute_subtask_spec(
            agent_module.SubtaskSpec(id="a", role="researcher", task="inspect")
        )
    )

    assert result.ok is False
    assert result.content == "partial-result"
    assert result.summary == "partial-result"
    assert result.error == "timed out"


def test_base_agent_internal_orchestration_preserves_structured_content(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    async def fake_execute(role, task, **kwargs):
        return {
            "ok": True,
            "role": role,
            "task": task,
            "content": '{"summary":"done"}',
            "structured_content": {"summary": "done"},
            "tool_calls_made": [],
        }

    monkeypatch.setattr(agent, "_execute_agent", fake_execute)

    result = asyncio.run(
        agent._execute_subtask_spec(
            agent_module.SubtaskSpec(id="a", role="researcher", task="inspect")
        )
    )

    assert result.ok is True
    assert result.structured_content == {"summary": "done"}


def test_execute_subtask_spec_passes_expected_output_and_constraints_to_spawn_agent(
    monkeypatch,
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed = {}

    async def fake_execute(role, task, **kwargs):
        observed["role"] = role
        observed["task"] = task
        observed["kwargs"] = dict(kwargs)
        return {"ok": True, "content": "done", "tool_calls_made": []}

    monkeypatch.setattr(agent, "_execute_agent", fake_execute)

    asyncio.run(
        agent._execute_subtask_spec(
            agent_module.SubtaskSpec(
                id="a",
                role="implementer",
                task="patch the file",
                expected_output="Return a concise diff summary.",
                output_contract={
                    "format": "json",
                    "required_keys": ["summary"],
                },
                write_scope=["agent/core/agent.py"],
                capability_profile="implementation",
            )
        )
    )

    assert observed["role"] == "implementer"
    assert observed["task"] == "patch the file"
    assert observed["kwargs"] == {
        "expected_output": "Return a concise diff summary.",
        "output_contract": {
            "format": "json",
            "required_keys": ["summary"],
        },
        "write_scope": ["agent/core/agent.py"],
        "capability_profile": "implementation",
        "handoff": None,
    }


def test_spawn_agent_enforces_write_scope_for_write_file(monkeypatch, tmp_path):
    import agent as agent_module

    class _FakeMemory:
        def write(self, *args, **kwargs):
            return None

        def read(self, *args, **kwargs):
            return ""

        def search(self, *args, **kwargs):
            return []

        def read_index(self):
            return ""

    registry = agent_module.ToolRegistry()
    agent_module.BuiltinTools(_FakeMemory(), registry, workspace_root=tmp_path)
    parent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    parent.register_spawn_capability("base system prompt", workspace_root=tmp_path)

    async def fake_send_message(self, ctx, user_message, stream_callback=None):
        content = await self.registry.call(
            "write_file",
            {"path": "forbidden.txt", "content": "blocked"},
        )
        return agent_module.AgentResult(agent_id="sub", content=content)

    monkeypatch.setattr(agent_module.BaseAgent, "send_message", fake_send_message)

    payload = json.loads(
        asyncio.run(
            registry.call(
                "spawn_agent",
                {
                    "role": "implementer",
                    "task": "edit a file",
                    "capability_profile": "implementation",
                    "write_scope": ["allowed.txt"],
                },
            )
        )
    )
    write_payload = json.loads(payload["content"])

    assert payload["ok"] is True
    assert write_payload["ok"] is False
    assert "write scope" in write_payload["error"].lower()


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

    async def fake_execute_agent(role, task, **kwargs):
        calls.append(("spawn_agent", {"role": role, "task": task}))
        return {"ok": True, "role": role, "task": task, "content": "ok", "tool_calls_made": []}

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "_execute_agent", fake_execute_agent)

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


def test_send_message_emits_spawn_batch_events(monkeypatch):
    import agent as agent_module

    class _Sink:
        def __init__(self):
            self.events = []

        def on_subagent_event(self, event):
            self.events.append(event)

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
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final", None))]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    async def fake_call(tool_name, tool_input):
        await asyncio.sleep(0)
        return json.dumps({"ok": True, "role": tool_input["role"]})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    sink = _Sink()
    token = agent_module._active_sink.set(sink)
    try:
        ctx = agent_module.AgentContext(system_prompt="system")
        result = asyncio.run(agent.send_message(ctx, "run parallel agents"))
    finally:
        agent_module._active_sink.reset(token)

    assert result.error is None
    kinds = [event.kind for event in sink.events]
    assert "batch_started" in kinds
    assert "batch_finished" in kinds


def test_send_message_emits_structured_orchestration_metrics(monkeypatch):
    import agent as agent_module

    class _Sink:
        def __init__(self):
            self.events = []

        def on_subagent_event(self, event):
            self.events.append(event)

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
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final", None))]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    async def fake_call(tool_name, tool_input):
        await asyncio.sleep(0)
        return json.dumps(
            {
                "ok": True,
                "role": tool_input["role"],
                "content": "done",
                "tool_calls_made": [],
            }
        )

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    sink = _Sink()
    token = agent_module._active_sink.set(sink)
    try:
        ctx = agent_module.AgentContext(system_prompt="system")
        result = asyncio.run(agent.send_message(ctx, "run parallel agents"))
    finally:
        agent_module._active_sink.reset(token)

    assert result.error is None
    batch_finished = next(event for event in sink.events if event.kind == "batch_finished")
    assert batch_finished.metrics["execution_mode"] == "parallel"
    assert batch_finished.metrics["spec_count"] == 2
    assert batch_finished.metrics["max_parallel_agents"] == 2
    assert batch_finished.metrics["write_scope_check_seconds"] >= 0
    assert batch_finished.metrics["duration_seconds"] >= 0


def test_send_message_emits_parallel_batch_progress_for_orchestrated_spawn_calls(
    monkeypatch,
):
    import agent as agent_module

    class _Sink:
        def __init__(self):
            self.events = []

        def on_subagent_event(self, event):
            self.events.append(event)

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
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final", None))]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    async def fake_call(tool_name, tool_input):
        await asyncio.sleep(0)
        return json.dumps(
            {
                "ok": True,
                "role": tool_input["role"],
                "content": "done",
                "tool_calls_made": [],
            }
        )

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    sink = _Sink()
    token = agent_module._active_sink.set(sink)
    try:
        ctx = agent_module.AgentContext(system_prompt="system")
        result = asyncio.run(agent.send_message(ctx, "run parallel agents"))
    finally:
        agent_module._active_sink.reset(token)

    assert result.error is None
    progress_events = [event for event in sink.events if event.kind == "batch_progress"]
    assert progress_events
    assert progress_events[-1].metrics["execution_mode"] == "parallel"
    assert progress_events[-1].completed == 2
    assert progress_events[-1].total == 2


def test_send_message_emits_latency_trace(monkeypatch, caplog):
    import agent as agent_module

    monkeypatch.setenv("SIMPLE_TRACE_LATENCY", "1")
    caplog.set_level(logging.WARNING, logger="agent.core.agent")

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc(
                "spawn_agent",
                json.dumps({"role": "researcher", "task": "first"}),
            ),
        )
    ]
    responses = iter(
        [
            agent_module._OAIResponse(
                [
                    agent_module._OAIChoice(
                        "tool_calls", agent_module._OAIMsg("", tool_calls)
                    )
                ]
            ),
            agent_module._OAIResponse(
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final", None))]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    async def fake_call(tool_name, tool_input):
        return json.dumps({"ok": True, "role": tool_input["role"]})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "run parallel agents"))

    assert result.error is None
    assert "latency_trace component=agent stage=send_message_started" in caplog.text
    assert "latency_trace component=agent stage=model_response_received" in caplog.text
    assert "latency_trace component=agent stage=tool_uses_finished" in caplog.text
    assert "latency_trace component=agent stage=send_message_finished" in caplog.text
    assert f"agent_id={ctx.agent_id}" in caplog.text


def test_send_message_emits_interaction_logs(monkeypatch, caplog):
    import agent as agent_module

    caplog.set_level(logging.INFO, logger="agent.core.agent")

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    responses = iter(
        [
            agent_module._OAIResponse(
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("final", None))]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello world"))

    assert result.error is None
    assert "interaction component=agent event=turn_started" in caplog.text
    assert "interaction component=agent event=turn_finished" in caplog.text
    assert f"agent_id={ctx.agent_id}" in caplog.text
    assert "message_len=11" in caplog.text
    assert "content_len=5" in caplog.text


def test_send_message_synthesizes_schedule_confirmation_when_model_returns_empty(
    monkeypatch,
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc(
                "current_time",
                json.dumps({}),
            ),
        ),
        agent_module._OAITC(
            "call-2",
            agent_module._OAIFunc(
                "schedule_create",
                json.dumps(
                    {
                        "name": "测试消息",
                        "trigger_type": "once",
                        "prompt": "测试一下",
                        "at": "2026-04-20T10:02:00+08:00",
                        "timezone_name": "Asia/Shanghai",
                    }
                ),
            ),
        ),
    ]
    responses = iter(
        [
            agent_module._OAIResponse(
                [agent_module._OAIChoice("tool_calls", agent_module._OAIMsg("", tool_calls))]
            ),
            agent_module._OAIResponse(
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("", None))]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    async def fake_call(tool_name, tool_input):
        if tool_name == "current_time":
            return json.dumps({"ok": True, "local_time": "2026-04-20T10:00:00+08:00"})
        if tool_name == "schedule_create":
            return json.dumps(
                {
                    "ok": True,
                    "task": {
                        "id": "task-1",
                        "name": "测试消息",
                        "delivery_mode": "channel",
                        "next_run_at": "2026-04-20T02:02:00+00:00",
                    },
                    "summary_text": "已设置定时任务：两分钟后在当前对话发送“测试一下”。",
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"unexpected tool {tool_name}")

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "两分钟后给我发一条消息"))

    assert result.error is None
    assert result.content == "已设置定时任务：两分钟后在当前对话发送“测试一下”。"


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

    async def fake_execute_agent(role, task, **kwargs):
        nonlocal concurrent, max_concurrent
        call_order.append(role)
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return {"ok": True, "role": role, "task": task, "content": "ok", "tool_calls_made": []}

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "_execute_agent", fake_execute_agent)

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


def test_send_message_reports_terminal_error_when_openai_length_stays_truncated(
    monkeypatch,
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("回答到一半", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("", None),
                    )
                ]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.content == "回答到一半"
    assert result.error == "Model response remained truncated after 2 auto-continue attempts"


def test_base_agent_runs_internal_parallel_orchestration_without_public_tool_exposure(
    monkeypatch,
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed = []

    async def fake_spawn(spec):
        observed.append((spec.id, spec.role, spec.task))
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}",
            summary=f"summary:{spec.id}",
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    specs = [
        agent_module.SubtaskSpec(id="a", role="researcher", task="inspect a"),
        agent_module.SubtaskSpec(id="b", role="critic", task="inspect b"),
    ]

    results = asyncio.run(agent.run_parallel_subtasks(specs))

    assert [result.id for result in results] == ["a", "b"]
    assert observed == [
        ("a", "researcher", "inspect a"),
        ("b", "critic", "inspect b"),
    ]
    assert "run_parallel_subtasks" not in registry.list_tools()
    assert "run_pipeline_subtasks" not in registry.list_tools()
    assert "run_rendezvous_round" not in registry.list_tools()


def test_build_components_marks_multi_agent_orchestration_skill_non_model_invocable(
    monkeypatch, tmp_path
):
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
    bundle = components["skill_catalog"].get("multi-agent-orchestration")

    assert bundle is not None
    assert bundle.disable_model_invocation is True


def test_base_agent_runs_internal_pipeline_with_summary_only(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed_tasks = {}

    async def fake_spawn(spec):
        observed_tasks[spec.id] = spec.task
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}",
            summary=f"summary:{spec.id}",
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    results = asyncio.run(
        agent.run_pipeline_subtasks(
            [
                agent_module.SubtaskSpec(id="first", role="researcher", task="collect"),
                agent_module.SubtaskSpec(
                    id="second",
                    role="reviewer",
                    task="judge",
                    depends_on=["first"],
                ),
            ]
        )
    )

    assert [result.id for result in results] == ["first", "second"]
    assert observed_tasks["first"] == "collect"
    assert observed_tasks["second"] == "judge\n\nUpstream summaries:\n- first: summary:first"


def test_base_agent_runs_internal_pipeline_with_structured_upstream_results(
    monkeypatch,
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed_tasks = {}

    async def fake_spawn(spec):
        observed_tasks[spec.id] = spec.task
        structured = {"summary": "done", "risks": []} if spec.id == "first" else None
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}",
            summary=f"summary:{spec.id}",
            structured_content=structured,
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    results = asyncio.run(
        agent.run_pipeline_subtasks(
            [
                agent_module.SubtaskSpec(id="first", role="researcher", task="collect"),
                agent_module.SubtaskSpec(
                    id="second",
                    role="reviewer",
                    task="judge",
                    depends_on=["first"],
                ),
            ]
        )
    )

    assert [result.id for result in results] == ["first", "second"]
    assert observed_tasks["second"] == (
        "judge\n\n"
        "Upstream summaries:\n"
        "- first: summary:first\n\n"
        "Upstream structured results:\n"
        '- first: {"risks": [], "summary": "done"}'
    )


def test_base_agent_runs_internal_rendezvous_with_lead_summary(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed_tasks = []

    async def fake_spawn(spec):
        observed_tasks.append((spec.id, spec.task))
        round_label = "round-2" if "Lead summary" in spec.task else "round-1"
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}:{round_label}",
            summary=f"summary:{spec.id}:{round_label}",
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    results = asyncio.run(
        agent.run_rendezvous_subtasks(
            [
                agent_module.SubtaskSpec(id="a", role="researcher", task="analyze"),
                agent_module.SubtaskSpec(id="b", role="critic", task="challenge"),
            ],
            max_rounds=2,
        )
    )

    assert len(results) == 4
    assert observed_tasks[:2] == [
        ("a", "analyze"),
        ("b", "challenge"),
    ]
    assert observed_tasks[2][1].startswith("analyze\n\nLead summary:\n")
    assert observed_tasks[3][1].startswith("challenge\n\nLead summary:\n")


def test_base_agent_emits_pipeline_phase_events(monkeypatch):
    import agent as agent_module

    class _Sink:
        def __init__(self):
            self.events = []

        def on_subagent_event(self, event):
            self.events.append(event)

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    async def fake_spawn(spec):
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}",
            summary=f"summary:{spec.id}",
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    sink = _Sink()
    token = agent_module._active_sink.set(sink)
    try:
        results = asyncio.run(
            agent.run_pipeline_subtasks(
                [
                    agent_module.SubtaskSpec(id="first", role="researcher", task="collect"),
                    agent_module.SubtaskSpec(
                        id="second",
                        role="reviewer",
                        task="judge",
                        depends_on=["first"],
                    ),
                ]
            )
        )
    finally:
        agent_module._active_sink.reset(token)

    assert [result.id for result in results] == ["first", "second"]
    phase_events = [event for event in sink.events if event.kind == "phase_started"]
    assert len(phase_events) == 2
    assert phase_events[0].metrics["execution_mode"] == "pipeline"
    assert phase_events[0].metrics["phase_kind"] == "stage"
    assert phase_events[0].metrics["phase_index"] == 1
    assert phase_events[0].metrics["ready_roles"] == ["researcher"]
    assert phase_events[1].metrics["phase_index"] == 2
    assert phase_events[1].metrics["ready_roles"] == ["reviewer"]


def test_base_agent_default_rendezvous_does_not_rebroadcast_raw_structured_results(
    monkeypatch,
):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed_tasks = []

    async def fake_spawn(spec):
        observed_tasks.append((spec.id, spec.task))
        round_label = "round-2" if "Lead summary" in spec.task else "round-1"
        structured = (
            {"position": spec.id, "confidence": 0.8} if round_label == "round-1" else None
        )
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}:{round_label}",
            summary=f"summary:{spec.id}:{round_label}",
            structured_content=structured,
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    results = asyncio.run(
        agent.run_rendezvous_subtasks(
            [
                agent_module.SubtaskSpec(id="analyze", role="analyst", task="analyze"),
                agent_module.SubtaskSpec(id="challenge", role="critic", task="challenge"),
            ],
            max_rounds=2,
        )
    )

    assert len(results) == 4
    assert "Lead structured results:" not in observed_tasks[2][1]
    assert "Lead structured results:" not in observed_tasks[3][1]


def test_base_agent_emits_rendezvous_phase_events(monkeypatch):
    import agent as agent_module

    class _Sink:
        def __init__(self):
            self.events = []

        def on_subagent_event(self, event):
            self.events.append(event)

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    async def fake_spawn(spec):
        round_label = "round-2" if "Lead summary" in spec.task else "round-1"
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}:{round_label}",
            summary=f"summary:{spec.id}:{round_label}",
            tool_calls_made=["spawn_agent"],
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)

    sink = _Sink()
    token = agent_module._active_sink.set(sink)
    try:
        results = asyncio.run(
            agent.run_rendezvous_subtasks(
                [
                    agent_module.SubtaskSpec(id="a", role="researcher", task="analyze"),
                    agent_module.SubtaskSpec(id="b", role="critic", task="challenge"),
                ],
                max_rounds=2,
            )
        )
    finally:
        agent_module._active_sink.reset(token)

    assert len(results) == 4
    phase_started = [event for event in sink.events if event.kind == "phase_started"]
    assert len(phase_started) == 2
    assert phase_started[0].metrics["execution_mode"] == "rendezvous"
    assert phase_started[0].metrics["phase_kind"] == "round"
    assert phase_started[0].metrics["phase_index"] == 1
    assert phase_started[0].metrics["phase_total"] == 2
    assert phase_started[1].metrics["phase_index"] == 2
    phase_notes = [event for event in sink.events if event.kind == "phase_note"]
    assert len(phase_notes) == 1
    assert phase_notes[0].metrics["execution_mode"] == "rendezvous"
    assert phase_notes[0].metrics["phase_kind"] == "lead_summary"
    assert phase_notes[0].metrics["continue_count"] == 2


def test_base_agent_runs_internal_rendezvous_with_lead_selected_structured_results(
    monkeypatch,
):
    import agent as agent_module
    from agent.orchestration.runtime import RendezvousDirective

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    observed_tasks = []

    async def fake_spawn(spec):
        observed_tasks.append((spec.id, spec.task))
        round_label = "round-2" if "Lead summary" in spec.task else "round-1"
        structured = (
            {"position": spec.id, "confidence": 0.8} if round_label == "round-1" else None
        )
        return agent_module.SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"content:{spec.id}:{round_label}",
            summary=f"summary:{spec.id}:{round_label}",
            structured_content=structured,
            tool_calls_made=["spawn_agent"],
        )

    def fake_summarize(results):
        return RendezvousDirective(
            summary="focus the unresolved disagreement",
            structured_context={
                "focus": {
                    "winner": "analyze",
                    "needs_followup": True,
                }
            },
        )

    monkeypatch.setattr(agent, "_execute_subtask_spec", fake_spawn)
    monkeypatch.setattr(agent, "_summarize_rendezvous_round", fake_summarize)

    results = asyncio.run(
        agent.run_rendezvous_subtasks(
            [
                agent_module.SubtaskSpec(id="analyze", role="analyst", task="analyze"),
                agent_module.SubtaskSpec(id="challenge", role="critic", task="challenge"),
            ],
            max_rounds=2,
        )
    )

    assert len(results) == 4
    assert "Lead structured results:" in observed_tasks[2][1]
    assert '- focus: {"needs_followup": true, "winner": "analyze"}' in observed_tasks[2][1]
    assert '- analyze: {"confidence": 0.8, "position": "analyze"}' not in observed_tasks[2][1]
    assert '- challenge: {"confidence": 0.8, "position": "challenge"}' not in observed_tasks[2][1]


def test_send_message_auto_continues_openai_length_finish(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("第一段没有说完", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "stop",
                        agent_module.shared._OAIMsg("，这是续写完成。", None),
                    )
                ]
            ),
        ]
    )
    seen_messages = []

    async def fake_create(ctx, tools):
        seen_messages.append(list(ctx.messages))
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.error is None
    assert result.content == "第一段没有说完，这是续写完成。"
    assert len(seen_messages) == 2
    assert seen_messages[1][-2] == {"role": "assistant", "content": "第一段没有说完"}
    assert "Continue exactly from where you left off" in seen_messages[1][-1]["content"]


def test_send_message_stream_preserves_reasoning_content_for_openai_tool_loop(
    monkeypatch,
):
    import agent as agent_module

    class _DeltaToolFunction:
        def __init__(self, name=None, arguments=None):
            self.name = name
            self.arguments = arguments

    class _DeltaToolCall:
        def __init__(self, index, id=None, name=None, arguments=None):
            self.index = index
            self.id = id
            self.function = _DeltaToolFunction(name, arguments)

    class _Delta:
        def __init__(self, content=None, reasoning_content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls
            self.model_extra = {}
            if reasoning_content is not None:
                self.model_extra["reasoning_content"] = reasoning_content

    class _ChunkChoice:
        def __init__(self, delta, finish_reason=None):
            self.delta = delta
            self.finish_reason = finish_reason

    class _Chunk:
        def __init__(self, delta, finish_reason=None):
            self.choices = [_ChunkChoice(delta, finish_reason)]

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

    class _FakeCompletions:
        def __init__(self, owner):
            self._owner = owner

        async def create(self, **kwargs):
            self._owner.calls.append(kwargs)
            return next(self._owner.responses)

    class _FakeClient:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.calls = []
            self.chat = type("Chat", (), {})()
            self.chat.completions = _FakeCompletions(self)

    registry = agent_module.ToolRegistry()
    client = _FakeClient(
        [
            _FakeStream(
                [
                    _Chunk(
                        _Delta(
                            content="我来查一下",
                            reasoning_content="先判断是否需要工具。",
                            tool_calls=[
                                _DeltaToolCall(
                                    0,
                                    id="call-1",
                                    name="current_time",
                                    arguments="{",
                                )
                            ],
                        )
                    ),
                    _Chunk(
                        _Delta(
                            reasoning_content="调用时间工具后再继续回答。",
                            tool_calls=[_DeltaToolCall(0, arguments="}")],
                        ),
                        finish_reason="tool_calls",
                    ),
                ]
            ),
            _FakeStream(
                [
                    _Chunk(
                        _Delta(content="千岛湖今天晴。"),
                        finish_reason="stop",
                    )
                ]
            ),
        ]
    )
    agent = agent_module.BaseAgent(
        client, registry, model="fake-model", api_format="openai"
    )

    async def fake_call(tool_name, tool_input):
        assert tool_name == "current_time"
        return json.dumps({"ok": True, "local_time": "2026-04-24T15:20:28+08:00"})

    monkeypatch.setattr(registry, "call", fake_call)

    streamed = []
    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(
        agent.send_message(
            ctx,
            "下午好啊，今天千岛湖的天气如何",
            stream_callback=streamed.append,
        )
    )

    assert result.error is None
    assert result.content == "千岛湖今天晴。"
    assert streamed == ["我来查一下", "千岛湖今天晴。"]
    assert len(client.calls) == 2
    assistant_message = client.calls[1]["messages"][-2]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "我来查一下"
    assert assistant_message["reasoning_content"] == "先判断是否需要工具。调用时间工具后再继续回答。"
    assert assistant_message["tool_calls"][0]["function"]["name"] == "current_time"


def test_send_message_preserves_reasoning_content_for_openai_tool_loop(monkeypatch):
    import agent as agent_module

    class _ModelExtraMessage:
        def __init__(self, content, tool_calls=None, reasoning_content=None):
            self.content = content
            self.tool_calls = tool_calls
            self.model_extra = {}
            if reasoning_content is not None:
                self.model_extra["reasoning_content"] = reasoning_content

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc("current_time", json.dumps({})),
        )
    ]
    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "tool_calls",
                        _ModelExtraMessage(
                            "我来查一下",
                            tool_calls,
                            reasoning_content="先判断是否需要工具，再调用时间工具。",
                        ),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "stop",
                        agent_module.shared._OAIMsg("现在是下午三点二十。", None),
                    )
                ]
            ),
        ]
    )
    seen_messages = []

    async def fake_create(ctx, tools):
        seen_messages.append(list(ctx.messages))
        return next(responses)

    async def fake_call(tool_name, tool_input):
        assert tool_name == "current_time"
        return json.dumps({"ok": True, "local_time": "2026-04-24T15:20:28+08:00"})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "现在几点"))

    assert result.error is None
    assert result.content == "现在是下午三点二十。"
    assert len(seen_messages) == 2
    assistant_message = seen_messages[1][-2]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "我来查一下"
    assert assistant_message["reasoning_content"] == "先判断是否需要工具，再调用时间工具。"
    assert assistant_message["tool_calls"][0]["function"]["name"] == "current_time"


def test_send_message_preserves_generic_provider_extras_for_openai_tool_loop(
    monkeypatch,
):
    import agent as agent_module

    class _ModelExtraMessage:
        def __init__(self, content, tool_calls=None, model_extra=None):
            self.content = content
            self.tool_calls = tool_calls
            self.model_extra = dict(model_extra or {})

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    tool_calls = [
        agent_module._OAITC(
            "call-1",
            agent_module._OAIFunc("current_time", json.dumps({})),
        )
    ]
    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "tool_calls",
                        _ModelExtraMessage(
                            "我来查一下",
                            tool_calls,
                            model_extra={
                                "thinking_signature": "sig-123",
                                "provider_state": {
                                    "phase": "tool",
                                    "confidence": 0.8,
                                },
                            },
                        ),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "stop",
                        agent_module.shared._OAIMsg("现在是下午三点二十。", None),
                    )
                ]
            ),
        ]
    )
    seen_messages = []

    async def fake_create(ctx, tools):
        seen_messages.append(list(ctx.messages))
        return next(responses)

    async def fake_call(tool_name, tool_input):
        assert tool_name == "current_time"
        return json.dumps({"ok": True, "local_time": "2026-04-24T15:20:28+08:00"})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "现在几点"))

    assert result.error is None
    assistant_message = seen_messages[1][-2]
    assert assistant_message["thinking_signature"] == "sig-123"
    assert assistant_message["provider_state"] == {
        "phase": "tool",
        "confidence": 0.8,
    }


def test_send_message_stream_merges_generic_provider_extras_for_openai_tool_loop(
    monkeypatch,
):
    import agent as agent_module

    class _DeltaToolFunction:
        def __init__(self, name=None, arguments=None):
            self.name = name
            self.arguments = arguments

    class _DeltaToolCall:
        def __init__(self, index, id=None, name=None, arguments=None):
            self.index = index
            self.id = id
            self.function = _DeltaToolFunction(name, arguments)

    class _Delta:
        def __init__(self, content=None, tool_calls=None, model_extra=None):
            self.content = content
            self.tool_calls = tool_calls
            self.model_extra = dict(model_extra or {})

    class _ChunkChoice:
        def __init__(self, delta, finish_reason=None):
            self.delta = delta
            self.finish_reason = finish_reason

    class _Chunk:
        def __init__(self, delta, finish_reason=None):
            self.choices = [_ChunkChoice(delta, finish_reason)]

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

    class _FakeCompletions:
        def __init__(self, owner):
            self._owner = owner

        async def create(self, **kwargs):
            self._owner.calls.append(kwargs)
            return next(self._owner.responses)

    class _FakeClient:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.calls = []
            self.chat = type("Chat", (), {})()
            self.chat.completions = _FakeCompletions(self)

    registry = agent_module.ToolRegistry()
    client = _FakeClient(
        [
            _FakeStream(
                [
                    _Chunk(
                        _Delta(
                            content="我来查一下",
                            model_extra={
                                "thinking_signature": "sig-",
                                "provider_state": {"phase": "tool"},
                            },
                            tool_calls=[
                                _DeltaToolCall(
                                    0,
                                    id="call-1",
                                    name="current_time",
                                    arguments="{",
                                )
                            ],
                        )
                    ),
                    _Chunk(
                        _Delta(
                            model_extra={
                                "thinking_signature": "123",
                                "provider_state": {"step": 1},
                            },
                            tool_calls=[_DeltaToolCall(0, arguments="}")],
                        ),
                        finish_reason="tool_calls",
                    ),
                ]
            ),
            _FakeStream(
                [
                    _Chunk(
                        _Delta(content="现在是下午三点二十。"),
                        finish_reason="stop",
                    )
                ]
            ),
        ]
    )
    agent = agent_module.BaseAgent(
        client, registry, model="fake-model", api_format="openai"
    )

    async def fake_call(tool_name, tool_input):
        assert tool_name == "current_time"
        return json.dumps({"ok": True, "local_time": "2026-04-24T15:20:28+08:00"})

    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "现在几点", stream_callback=lambda _: None))

    assert result.error is None
    assistant_message = client.calls[1]["messages"][-2]
    assert assistant_message["thinking_signature"] == "sig-123"
    assert assistant_message["provider_state"] == {"phase": "tool", "step": 1}


def test_send_message_auto_continue_preserves_openai_provider_extras(monkeypatch):
    import agent as agent_module

    class _ModelExtraMessage:
        def __init__(self, content, tool_calls=None, model_extra=None):
            self.content = content
            self.tool_calls = tool_calls
            self.model_extra = dict(model_extra or {})

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        _ModelExtraMessage(
                            "第一段没有说完",
                            None,
                            model_extra={
                                "reasoning_content": "先内部思考。",
                                "thinking_signature": "sig-123",
                            },
                        ),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "stop",
                        agent_module.shared._OAIMsg("，这是续写完成。", None),
                    )
                ]
            ),
        ]
    )
    seen_messages = []

    async def fake_create(ctx, tools):
        seen_messages.append(list(ctx.messages))
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.error is None
    assert result.content == "第一段没有说完，这是续写完成。"
    assert len(seen_messages) == 2
    assert seen_messages[1] == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "第一段没有说完",
            "reasoning_content": "先内部思考。",
            "thinking_signature": "sig-123",
        },
        {
            "role": "user",
            "content": "Continue exactly from where you left off. Do not repeat previous text. Do not restart the answer.",
        },
    ]


def test_send_message_auto_continue_ignores_non_copyable_context_metadata(monkeypatch):
    import threading
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("第一段没有说完", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "stop",
                        agent_module.shared._OAIMsg("，这是续写完成。", None),
                    )
                ]
            ),
        ]
    )
    seen_messages = []

    async def fake_create(ctx, tools):
        seen_messages.append(list(ctx.messages))
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    ctx.metadata["unsafe_local"] = threading.local()
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.error is None
    assert result.content == "第一段没有说完，这是续写完成。"
    assert len(seen_messages) == 2
    assert seen_messages[1] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "第一段没有说完"},
        {
            "role": "user",
            "content": "Continue exactly from where you left off. Do not repeat previous text. Do not restart the answer.",
        },
    ]


def test_send_message_stream_drops_non_serializable_openai_provider_extras(
    monkeypatch,
):
    import threading
    import agent as agent_module

    class _DeltaToolFunction:
        def __init__(self, name=None, arguments=None):
            self.name = name
            self.arguments = arguments

    class _DeltaToolCall:
        def __init__(self, index, id=None, name=None, arguments=None):
            self.index = index
            self.id = id
            self.function = _DeltaToolFunction(name, arguments)

    class _Delta:
        def __init__(self, content=None, tool_calls=None, model_extra=None):
            self.content = content
            self.tool_calls = tool_calls
            self.model_extra = dict(model_extra or {})

    class _ChunkChoice:
        def __init__(self, delta, finish_reason=None):
            self.delta = delta
            self.finish_reason = finish_reason

    class _Chunk:
        def __init__(self, delta, finish_reason=None):
            self.choices = [_ChunkChoice(delta, finish_reason)]

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

    class _FakeCompletions:
        def __init__(self, owner):
            self._owner = owner

        async def create(self, **kwargs):
            self._owner.calls.append(kwargs)
            return next(self._owner.responses)

    class _FakeClient:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.calls = []
            self.chat = type("Chat", (), {})()
            self.chat.completions = _FakeCompletions(self)

    registry = agent_module.ToolRegistry()
    client = _FakeClient(
        [
            _FakeStream(
                [
                    _Chunk(
                        _Delta(
                            content="我来查一下",
                            model_extra={
                                "reasoning_content": "先判断是否需要工具。",
                                "unsafe_local": threading.local(),
                            },
                            tool_calls=[
                                _DeltaToolCall(
                                    0,
                                    id="call-1",
                                    name="current_time",
                                    arguments="{}",
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            ),
            _FakeStream(
                [
                    _Chunk(
                        _Delta(content="现在是下午三点二十。"),
                        finish_reason="stop",
                    )
                ]
            ),
        ]
    )
    agent = agent_module.BaseAgent(
        client, registry, model="fake-model", api_format="openai"
    )

    async def fake_call(tool_name, tool_input):
        assert tool_name == "current_time"
        return json.dumps({"ok": True, "local_time": "2026-04-24T15:20:28+08:00"})

    monkeypatch.setattr(registry, "call", fake_call)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "现在几点", stream_callback=lambda _: None))

    assert result.error is None
    assistant_message = client.calls[1]["messages"][-2]
    assert assistant_message["reasoning_content"] == "先判断是否需要工具。"
    assert "unsafe_local" not in assistant_message


def test_send_message_auto_continue_trims_overlap(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("这是一个长回答，后半", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "stop",
                        agent_module.shared._OAIMsg("后半继续完成。", None),
                    )
                ]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.error is None
    assert result.content == "这是一个长回答，后半继续完成。"


def test_send_message_reports_error_after_auto_continue_budget(monkeypatch):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    responses = iter(
        [
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("第一段", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("第二段", None),
                    )
                ]
            ),
            agent_module.shared._OAIResponse(
                [
                    agent_module.shared._OAIChoice(
                        "length",
                        agent_module.shared._OAIMsg("第三段", None),
                    )
                ]
            ),
        ]
    )

    async def fake_create(ctx, tools):
        return next(responses)

    monkeypatch.setattr(agent, "_create", fake_create)

    ctx = agent_module.AgentContext(system_prompt="system")
    result = asyncio.run(agent.send_message(ctx, "hello"))

    assert result.content == "第一段第二段第三段"
    assert result.error == "Model response remained truncated after 2 auto-continue attempts"


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


def test_evolve_uses_qualified_component_cleanup(monkeypatch):
    import agent.cli as cli_module

    calls = {"closed": 0}

    class _FakeEvolution:
        async def rewrite_system_prompt(self):
            return "new prompt"

    async def fake_build_components_async(cfg):
        return {"evolution": _FakeEvolution()}

    async def fake_close_components(components):
        calls["closed"] += 1

    monkeypatch.setattr(cli_module.agent_module, "load_config", lambda: ({}, False))
    monkeypatch.setattr(
        cli_module.agent_module, "_build_components_async", fake_build_components_async
    )
    monkeypatch.setattr(cli_module.agent_module, "_close_components", fake_close_components)

    cli_module.evolve(rewrite=False, apply_best=False, stats=False)

    assert calls["closed"] == 1


def test_gateway_starts_background_scheduler(monkeypatch):
    import agent.cli as cli_module

    started = {"scheduler": 0, "stopped": 0, "runner": 0, "store_closed": 0, "sched_closed": 0}

    class _FakeService:
        async def run_forever(self):
            started["scheduler"] += 1
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                started["stopped"] += 1
                raise

    class _FakeStore:
        def close(self):
            started["store_closed"] += 1

    class _FakeRunner:
        def __init__(self, channels, components, cfg):
            self.channels = channels
            self.components = components
            self.cfg = cfg

        async def run(self):
            started["runner"] += 1
            await asyncio.sleep(0)

    async def fake_build_components_async(cfg):
        return {"agent": object()}

    async def fake_build_scheduler_service(
        cfg, poll_seconds, lease_seconds, max_concurrent_runs, components=None
    ):
        assert max_concurrent_runs == 3
        return _FakeService(), _FakeStore(), {"scheduler_components": True}

    async def fake_close_components(components):
        if components.get("scheduler_components"):
            started["sched_closed"] += 1

    monkeypatch.setattr(cli_module.agent_module, "load_config", lambda: ({}, False))
    monkeypatch.setattr(cli_module.agent_module, "_build_components_async", fake_build_components_async)
    monkeypatch.setattr(cli_module.agent_module, "_close_components", fake_close_components)
    monkeypatch.setattr(cli_module, "_build_scheduler_service", fake_build_scheduler_service)
    monkeypatch.setattr(cli_module, "_build_gateway_channels", lambda cfg: ["feishu"])
    monkeypatch.setattr(cli_module, "ChannelRunner", _FakeRunner)

    cli_module.gateway()

    assert started["scheduler"] == 1
    assert started["stopped"] == 1
    assert started["runner"] == 1
    assert started["store_closed"] == 1
    assert started["sched_closed"] == 1


def test_gateway_reuses_primary_components_for_scheduler(monkeypatch):
    import agent.cli as cli_module

    observed = {"primary": None, "scheduler": None}

    class _FakeService:
        async def run_forever(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

    class _FakeStore:
        def close(self):
            return None

    class _FakeRunner:
        def __init__(self, channels, components, cfg):
            observed["primary"] = components

        async def run(self):
            await asyncio.sleep(0)

    primary_components = {"agent": object(), "marker": "primary"}

    async def fake_build_components_async(cfg):
        return primary_components

    async def fake_build_scheduler_service(
        cfg, poll_seconds, lease_seconds, max_concurrent_runs, components=None
    ):
        assert max_concurrent_runs == 3
        observed["scheduler"] = components
        return _FakeService(), _FakeStore(), None

    async def fake_close_components(components):
        return None

    monkeypatch.setattr(cli_module.agent_module, "load_config", lambda: ({}, False))
    monkeypatch.setattr(
        cli_module.agent_module, "_build_components_async", fake_build_components_async
    )
    monkeypatch.setattr(cli_module.agent_module, "_close_components", fake_close_components)
    monkeypatch.setattr(cli_module, "_build_scheduler_service", fake_build_scheduler_service)
    monkeypatch.setattr(cli_module, "_build_gateway_channels", lambda cfg: ["feishu"])
    monkeypatch.setattr(cli_module, "ChannelRunner", _FakeRunner)

    cli_module.gateway()

    assert observed["primary"] is primary_components
    assert observed["scheduler"] is primary_components


def test_configure_runtime_logging_enables_interaction_loggers(monkeypatch):
    import agent.cli as cli_module

    calls = {}

    def fake_basic_config(**kwargs):
        calls.update(kwargs)

    root_logger = logging.getLogger()
    original_root_handlers = list(root_logger.handlers)
    original_levels = {
        name: logging.getLogger(name).level
        for name in (
            "agent.channels.base",
            "agent.core.agent",
            "channels.feishu",
        )
    }
    try:
        root_logger.handlers.clear()
        monkeypatch.setattr(cli_module.logging, "basicConfig", fake_basic_config)

        cli_module._configure_runtime_logging()

        assert calls["level"] == logging.INFO
        assert calls["format"]
        assert logging.getLogger("agent.channels.base").level == logging.INFO
        assert logging.getLogger("agent.core.agent").level == logging.INFO
        assert logging.getLogger("channels.feishu").level == logging.INFO
    finally:
        root_logger.handlers[:] = original_root_handlers
        for name, level in original_levels.items():
            logging.getLogger(name).setLevel(level)


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


def test_stream_response_supports_async_stream_callback():
    import agent as agent_module

    class _FakeFinalMessage:
        stop_reason = "end_turn"
        content = []

    class _FakeAnthropicStream:
        def __init__(self):
            self.text_stream = self
            self._chunks = iter(["hello", " world"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

        async def get_final_message(self):
            return _FakeFinalMessage()

    class _FakeAnthropicClient:
        class messages:
            @staticmethod
            def stream(**kwargs):
                return _FakeAnthropicStream()

    seen = []

    async def _async_callback(chunk: str):
        seen.append(chunk)

    agent = agent_module.BaseAgent(
        _FakeAnthropicClient(),
        agent_module.ToolRegistry(),
        model="fake-model",
        api_format="anthropic",
    )
    ctx = agent_module.AgentContext(system_prompt="system")

    response, text = asyncio.run(agent._stream_response(ctx, [], _async_callback))

    assert isinstance(response, _FakeFinalMessage)
    assert text == "hello world"
    assert seen == ["hello", " world"]


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

        def consume_dirty(self):
            return False

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


def test_interactive_loop_context_command_uses_dynamic_category_stats(
    monkeypatch, tmp_path
):
    import agent as agent_module

    class _FakeAgent:
        api_format = "openai"
        max_tokens = 1024
        model = "fake-model"

        async def send_message(self, ctx, user_message, stream_callback=None):
            return agent_module.AgentResult(agent_id="agent", content="unused")

    class _FakeMemory:
        def list_chapters(self):
            return []

    class _FakeEvolution:
        async def rewrite_system_prompt(self):
            return "unused"

        def get_stats(self):
            return {"total": 0, "avg_score": 0}

    class _FakeSkillCatalog:
        def list_skills(self):
            return []

        def consume_dirty(self):
            return False

    class _FakeUserToolCatalog:
        def load_into_registry(self, registry):
            return []

    class _FakeContextManager:
        class staging:
            session_id = "session-1"

        def record_turn(self, *, user_content, assistant_content="", channel="", **_kwargs):
            return None

        def stats(self):
            return {
                "dynamic_categories": 2,
                "total_categories": 8,
                "total_entries": 14,
                "category_names": ["identity", "projects"],
                "max_categories": 15,
                "needs_consolidation": False,
                "queued_jobs": 0,
                "staged_turns": 0,
                "idle_elapsed_s": 0,
                "idle_threshold_s": 300,
            }

        def should_session_end_sleep(self):
            return False

    class _FakeBackgroundMemoryWorker:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        async def wait(self):
            return None

    monkeypatch.setattr(
        agent_module, "BackgroundMemoryWorker", _FakeBackgroundMemoryWorker
    )
    monkeypatch.setattr(agent_module, "STAGING_DIR", tmp_path / "staging")

    answers = iter(["/context", "/quit"])
    monkeypatch.setattr(
        agent_module.Prompt,
        "ask",
        lambda *_args, **_kwargs: next(answers),
    )

    components = {
        "agent": _FakeAgent(),
        "memory": _FakeMemory(),
        "evolution": _FakeEvolution(),
        "system_prompt": "system-v1",
        "base_system_prompt": "system-v1",
        "skill_catalog": _FakeSkillCatalog(),
        "user_tool_catalog": _FakeUserToolCatalog(),
        "registry": agent_module.ToolRegistry(),
        "output_dir": tmp_path / "output",
        "context_manager": _FakeContextManager(),
        "client": object(),
        "model": "fake-model",
    }

    asyncio.run(agent_module._interactive_loop(components, _minimal_cfg()))


def test_interactive_loop_compaction_keeps_latest_system_prompt(monkeypatch, tmp_path):
    import agent as agent_module

    class _FakeAgent:
        api_format = "openai"
        max_tokens = 1024
        model = "fake-model"

        def __init__(self):
            self.ctx = None

        async def send_message(self, ctx, user_message, stream_callback=None):
            self.ctx = ctx
            return agent_module.AgentResult(agent_id="agent", content="reply")

    class _FakeMemory:
        def list_chapters(self):
            return []

    class _FakeEvolution:
        async def rewrite_system_prompt(self):
            return "system-v2"

        def get_stats(self):
            return {"total": 0, "avg_score": 0}

    class _FakeSkillCatalog:
        def list_skills(self):
            return []

        def consume_dirty(self):
            return False

    class _FakeUserToolCatalog:
        def load_into_registry(self, registry):
            return []

    class _FakeStaging:
        session_id = "session-1"

        def append(self, role, content):
            return None

        def count(self):
            return 0

    class _FakeContextManager:
        def __init__(self):
            self.staging = _FakeStaging()

        def mark_activity(self):
            pass

        def record_turn(self, *, user_content, assistant_content="", channel="", **_kwargs):
            pass

        def should_enqueue_consolidation(self):
            return False

        def enqueue_consolidation(self, reason):
            pass

        def should_compact_messages(self, messages, max_tokens):
            return True

        def compact_messages(self, messages):
            return messages

        def should_session_end_sleep(self):
            return False

    class _FakeBackgroundMemoryWorker:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        async def wait(self):
            return None

        def wake(self):
            pass

    monkeypatch.setattr(
        agent_module, "BackgroundMemoryWorker", _FakeBackgroundMemoryWorker
    )
    monkeypatch.setattr(agent_module, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(
        agent_module,
        "_compose_system_prompt",
        lambda base_prompt,
        registry,
        workspace_root,
        output_dir,
        skill_catalog=None,
        plugin_catalog=None: f"COMPOSED::{base_prompt}",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", prompts_dir)

    answers = iter(["first task", "/evolve", "second task", "/quit"])
    monkeypatch.setattr(
        agent_module.Prompt,
        "ask",
        lambda *_args, **_kwargs: next(answers),
    )

    fake_agent = _FakeAgent()
    components = {
        "agent": fake_agent,
        "memory": _FakeMemory(),
        "evolution": _FakeEvolution(),
        "system_prompt": "COMPOSED::system-v1",
        "base_system_prompt": "system-v1",
        "skill_catalog": _FakeSkillCatalog(),
        "user_tool_catalog": _FakeUserToolCatalog(),
        "registry": agent_module.ToolRegistry(),
        "output_dir": tmp_path / "output",
        "context_manager": _FakeContextManager(),
        "client": object(),
        "model": "fake-model",
    }

    asyncio.run(agent_module._interactive_loop(components, _minimal_cfg()))

    assert fake_agent.ctx.system_prompt.startswith("COMPOSED::system-v2")


def test_interactive_loop_queues_orphan_recovery_in_background(monkeypatch, tmp_path):
    import agent as agent_module

    orphan_dir = tmp_path / "staging"
    orphan_dir.mkdir()
    orphan_path = orphan_dir / "orphan-session.jsonl"
    orphan_path.write_text(
        '{"role":"user","content":"old turn","ts":"2026-04-13 00:00 UTC"}\n',
        encoding="utf-8",
    )

    class _FakeAgent:
        api_format = "openai"
        max_tokens = 1024
        model = "fake-model"

        async def send_message(self, ctx, user_message, stream_callback=None):
            return agent_module.AgentResult(agent_id="agent", content="unused")

    class _FakeMemory:
        def list_chapters(self):
            return []

    class _FakeEvolution:
        def get_stats(self):
            return {"total": 0, "avg_score": 0}

    class _FakeSkillCatalog:
        def list_skills(self):
            return []

        def consume_dirty(self):
            return False

    class _FakeUserToolCatalog:
        def load_into_registry(self, registry):
            return []

    class _FakeStaging:
        session_id = "current-session"

        def count(self):
            return 0

    class _FakeContextManager:
        def __init__(self):
            self.staging = _FakeStaging()
            self.queued = []

        def record_turn(self, *, user_content, assistant_content="", channel="", **_kwargs):
            return None

        def enqueue_staging_job(self, reason, staging):
            self.queued.append((reason, staging.path.name, staging.session_id))

        def should_session_end_sleep(self):
            return False

    wakes = []

    class _FakeBackgroundMemoryWorker:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        async def wait(self):
            return None

        def wake(self):
            wakes.append("wake")

    monkeypatch.setattr(
        agent_module, "BackgroundMemoryWorker", _FakeBackgroundMemoryWorker
    )
    monkeypatch.setattr(agent_module, "STAGING_DIR", orphan_dir)

    answers = iter(["/quit"])
    monkeypatch.setattr(
        agent_module.Prompt,
        "ask",
        lambda *_args, **_kwargs: next(answers),
    )

    fake_ctx_mgr = _FakeContextManager()
    components = {
        "agent": _FakeAgent(),
        "memory": _FakeMemory(),
        "evolution": _FakeEvolution(),
        "system_prompt": "system-v1",
        "base_system_prompt": "system-v1",
        "skill_catalog": _FakeSkillCatalog(),
        "user_tool_catalog": _FakeUserToolCatalog(),
        "registry": agent_module.ToolRegistry(),
        "output_dir": tmp_path / "output",
        "context_manager": fake_ctx_mgr,
        "client": object(),
        "model": "fake-model",
    }

    asyncio.run(agent_module._interactive_loop(components, _minimal_cfg()))

    assert fake_ctx_mgr.queued == [
        ("orphan_recovery", "orphan-session.jsonl", "orphan-session")
    ]
    assert wakes == ["wake"]


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
