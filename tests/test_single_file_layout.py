from pathlib import Path


def test_agent_is_self_contained_single_file():
    import agent as agent_module

    root = Path(agent_module.__file__).resolve().parent
    source = Path(agent_module.__file__).read_text(encoding="utf-8")

    assert "from tool_runtime import" not in source
    assert "import tool_runtime" not in source
    assert "from memory_projection import" not in source
    assert "import memory_projection" not in source
    assert not (root / "tool_runtime.py").exists()
    assert not (root / "memory_projection.py").exists()
