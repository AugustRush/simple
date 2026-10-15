"""``registry`` and ``skill_catalog`` are provided by plugins.

These were the last two services the composition root assembled.  They looked
like floors -- the loader resolves the registry, and the catalog is read while
plugins load -- but both take values rather than services, and both are now
declared with ``inject``, which is what makes the ordering the kernel's problem
instead of the composition's.

What has to keep being true:

* ``registry`` comes from the plugin, with the static context the tool
  implementations read at call time (the resource paths, the config-derived
  secrets) and with ``agent.ToolRegistry`` read at setup time, so a substitution
  reaches it;
* ``skill_catalog`` comes from the plugin, loads the user's skills and registers
  the skill-management tools on the registry -- which is why it declares
  ``registry`` in its inject list;
* the **runtime** context (sandbox posture, permissions, output dir) stays with
  the composition, and the sandbox warning is announced *after* the context it
  reads is written: it used to read ``shell_sandbox_mode`` before anything set it,
  so it announced the default posture whatever the operator configured.
"""

from __future__ import annotations

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry
from agent.plugins.core import core_plugins
from agent.skills.plugin import SkillCatalogPlugin
from agent.tools.registry_plugin import RegistryPlugin

PATHS = {
    "resource_root": "/tmp/resources",
    "workspace_root": "/tmp/workspace",
    "user_skills_dir": "/tmp/resources/skills",
    "user_tools_dir": "/tmp/resources/tools",
    "user_plugins_dir": "/tmp/resources/plugins",
}


def _load(plugin, mapping: dict) -> PluginHost:
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(plugin)
    return host


def test_the_registry_plugin_provides_the_registry_and_its_context() -> None:
    mapping: dict = {"cfg": {"tavily_api_key": "tvly-x", "web_proxy": "http://proxy"}, **PATHS}

    host = _load(RegistryPlugin(), mapping)

    assert host.pending() == ()
    registry = mapping["registry"]
    assert type(registry).__name__ == "ToolRegistry"
    assert registry.get_context("resource_home") == PATHS["resource_root"]
    assert registry.get_context("workspace_root") == PATHS["workspace_root"]
    assert registry.get_context("user_skills_dir") == PATHS["user_skills_dir"]
    assert registry.get_context("tavily_api_key") == "tvly-x"
    assert registry.get_context("web_proxy") == "http://proxy"


def test_a_secret_named_by_environment_is_resolved(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_FOR_TEST", "from-env")
    mapping: dict = {"cfg": {"tavily_api_key": "$TAVILY_FOR_TEST"}, **PATHS}

    _load(RegistryPlugin(), mapping)

    assert mapping["registry"].get_context("tavily_api_key") == "from-env"


def test_the_registry_class_is_read_at_setup_time(monkeypatch) -> None:
    """Substituting ``agent.ToolRegistry`` has to reach the construction."""
    import agent as agent_module

    made: list[dict] = []

    class _Registry:
        def __init__(self, **kwargs) -> None:
            made.append(kwargs)
            self._context: dict = {}

        def set_context(self, key, value) -> None:
            self._context[key] = value

    monkeypatch.setattr(agent_module, "ToolRegistry", _Registry)
    mapping: dict = {"cfg": {}, **PATHS}

    _load(RegistryPlugin(), mapping)

    assert isinstance(mapping["registry"], _Registry)
    assert made and "console" in made[0]


def test_the_skill_plugin_loads_skills_and_registers_their_tools(
    tmp_path, monkeypatch
) -> None:
    # A home of its own: the plugin also reads the *shared* roots other ecosystems
    # keep, so a test that used the real home would depend on what is installed on
    # the machine running it.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DSH_AGENTS_HOME", raising=False)
    root = tmp_path / "skills"
    skill_dir = root / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy\nuser-invocable: true\n---\nInstructions.\n", encoding="utf-8"
    )
    registry_mapping: dict = {"cfg": {}, **{**PATHS, "user_skills_dir": str(root)}}
    _load(RegistryPlugin(), registry_mapping)

    skill_mapping: dict = {
        "cfg": {"skills": None},
        "registry": registry_mapping["registry"],
        "user_skills_dir": str(root),
    }
    host = _load(SkillCatalogPlugin(), skill_mapping)

    assert host.pending() == ()
    catalog = skill_mapping["skill_catalog"]
    assert type(catalog).__name__ == "SkillCatalog"
    assert "deploy" in {skill.name for skill in catalog.list_all_skills()}
    # It registers the skill-management tools, which is why registry is injected.
    registered = set(skill_mapping["registry"].list_tools())
    assert {"activate_skill", "list_skill_files", "read_skill_file"} <= registered


def test_the_declaration_says_what_they_provide() -> None:
    provided = {plugin.name: plugin.provides for plugin in core_plugins()}

    assert provided["registry"] == ("registry",)
    assert provided["skill_catalog"] == ("skill_catalog",)


def test_a_profile_can_name_another_plugin_for_the_registry() -> None:
    names = [plugin.name for plugin in core_plugins(replacements={"registry": "acme.registry"})]

    assert "registry" not in names
    assert "skill_catalog" in names, "the catalog stays; it pends on whoever provides it"


def _compose(cfg_extra: dict, monkeypatch, tmp_path):
    import asyncio

    import agent as agent_module
    from test_agent_integration import _minimal_cfg

    cfg = _minimal_cfg()
    cfg.update(cfg_extra)
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg, announce=True: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    return asyncio.run(agent_module._build_components_async(cfg, announce=True))


def test_the_real_composition_gets_both_from_their_plugins(monkeypatch, tmp_path) -> None:
    components = _compose({}, monkeypatch, tmp_path)

    catalog = components["plugin_catalog"]
    assert catalog.who_provides("registry") == "registry"
    assert catalog.who_provides("skill_catalog") == "skill_catalog"
    assert components["registry"].get_context("resource_home")
    assert components["skill_catalog"] is not None
    catalog.close()


def test_the_sandbox_posture_is_announced_from_the_configured_mode(
    monkeypatch, tmp_path, capsys
) -> None:
    """The warning reads the context *after* the composition writes it.

    Before this moved, it read ``shell_sandbox_mode`` before anything had set it,
    so a deployment with the sandbox deliberately off was announced as if it were
    on -- a warning about a posture that was never the posture.
    """
    _compose({"permissions": {"shell_sandbox": "none"}}, monkeypatch, tmp_path)

    assert "shell sandbox is OFF" in capsys.readouterr().out


def test_the_warning_stays_quiet_for_a_sandboxed_posture(monkeypatch, tmp_path, capsys) -> None:
    _compose({"permissions": {"shell_sandbox": "read_all"}}, monkeypatch, tmp_path)

    assert "shell sandbox is OFF" not in capsys.readouterr().out


@pytest.mark.parametrize("service", ["registry", "skill_catalog"])
def test_the_plugin_is_what_provides_it(service: str, monkeypatch, tmp_path) -> None:
    """Asked of the *composition*, not of a hand-written owner list.

    The declaration used to carry an ``owners`` tuple, and this test read it -- which
    meant the answer came from the same table the claim was about.  The composition knows:
    ``who_provides`` reports which plugin actually offered the name.
    """
    from test_registry_and_skill_plugins import _compose

    components = _compose({}, monkeypatch, tmp_path)
    try:
        catalog = components["plugin_catalog"]
        provider = catalog.who_provides(service)
        assert provider, f"nothing provided {service}"
        assert "plugin" in provider or provider == service, (
            f"{service} is provided by {provider!r}; if the composition root builds it, a "
            f"reader is told it is replaceable when it is not"
        )
    finally:
        components["plugin_catalog"].close()
