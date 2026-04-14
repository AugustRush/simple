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
    """The skill-manager built-in skill should be discovered from the repo's skills/ dir."""
    import agent as agent_module

    # Use the real builtin_root (the repo's skills/ directory)
    real_builtin = Path(agent_module.__file__).resolve().parent / "skills"
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
