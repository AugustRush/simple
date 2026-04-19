from pathlib import Path


def test_agent_imports_from_package():
    import agent as agent_module

    agent_path = Path(agent_module.__file__).resolve()

    assert agent_path.name == "__init__.py"
    assert agent_path.parent.name == "agent"


def test_agent_exports_typer_app():
    import agent as agent_module

    assert agent_module.app is not None


def test_builtin_resources_live_under_agent_package():
    import agent as agent_module

    package_root = Path(agent_module.__file__).resolve().parent

    assert package_root in Path(agent_module.BUILTIN_SKILLS_DIR).resolve().parents
    assert package_root in Path(agent_module.PLUGINS_DIR).resolve().parents
