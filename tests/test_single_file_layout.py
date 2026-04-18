from pathlib import Path


def test_agent_imports_from_package():
    import agent as agent_module

    agent_path = Path(agent_module.__file__).resolve()

    assert agent_path.name == "__init__.py"
    assert agent_path.parent.name == "agent"


def test_agent_exports_typer_app():
    import agent as agent_module

    assert agent_module.app is not None
