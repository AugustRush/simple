"""Tests for EvolutionEngine provider-specific scoring behavior."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeOpenAIResponse:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]


class _FakeOpenAICompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeOpenAIResponse('{"score": 8, "critique": "solid", "improvements": ["less tool use"]}')


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = type(
            "Chat",
            (),
            {"completions": _FakeOpenAICompletions()},
        )()


def test_score_session_uses_openai_chat_api(tmp_path):
    import asyncio
    from agent import EvolutionEngine, MemoryPalace

    client = _FakeOpenAIClient()
    engine = EvolutionEngine(
        client=client,
        model="qwen",
        memory=MemoryPalace(),
        api_format="openai",
    )

    result = asyncio.run(
        engine.score_session(
            messages=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
            prompt_version="default",
            tools_used=[],
        )
    )

    assert result["score"] == 8
    assert client.chat.completions.calls


def test_rewrite_system_prompt_uses_openai_chat_api(tmp_path, monkeypatch):
    import asyncio
    import agent as agent_module
    from agent import EvolutionEngine, MemoryPalace

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    sessions_file = tmp_path / "sessions.jsonl"
    sessions_file.write_text(
        '{"score": 4, "critique": "too verbose", "improvements": ["be concise"]}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(agent_module, "PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(agent_module, "SESSIONS_FILE", sessions_file)

    client = _FakeOpenAIClient()
    engine = EvolutionEngine(
        client=client,
        model="qwen",
        memory=MemoryPalace(
            base_dir=tmp_path / "memory",
            context_dir=tmp_path / "context",
        ),
        api_format="openai",
    )

    new_prompt = asyncio.run(engine.rewrite_system_prompt())

    assert "solid" in new_prompt
    assert client.chat.completions.calls
    assert list(prompts_dir.glob("system_v*.md"))


def test_generate_tool_uses_openai_chat_api(tmp_path, monkeypatch):
    import asyncio
    import agent as agent_module
    from agent import EvolutionEngine, MemoryPalace, ToolRegistry

    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(agent_module, "TOOLS_DIR", tools_dir)

    client = _FakeOpenAIClient()
    engine = EvolutionEngine(
        client=client,
        model="qwen",
        memory=MemoryPalace(
            base_dir=tmp_path / "memory",
            context_dir=tmp_path / "context",
        ),
        api_format="openai",
    )

    result = asyncio.run(engine.generate_tool("hello world tool", ToolRegistry()))

    assert "Tool generated and saved" in result
    assert client.chat.completions.calls
    assert list(tools_dir.glob("auto_*.py"))
