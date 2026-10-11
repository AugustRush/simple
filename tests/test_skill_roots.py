"""Skills the user installed for another agent are skills the user has.

The catalog used to know exactly one user root -- this agent's own.  Which
*other* directories hold a user's skills is an ecosystem convention (Codex's
``~/.codex/skills``, the vendor-neutral ``~/.agents/skills``, DSH's
``~/.dsh/skills``), and conventions live in the format adapters, so the catalog
asks them.  These tests pin the three properties that make that real:

* the roots come from the adapters, and a fourth ecosystem is one ``SkillRoot``
  on its format -- no catalog edit;
* the shared layer is a **fallback** (the user's own skill wins a name collision)
  and skips the ecosystem's *own* bundled subtree, because Codex's ``.system``
  holds Codex's instructions, not the user's;
* a root that is not there is skipped silently, and the neutral root honours
  ``DSH_AGENTS_HOME`` the way DSH itself does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry
from agent.plugins.formats import (
    CLAUDE_CODE,
    CODEX,
    NATIVE,
    PluginFormat,
    SkillRoot,
    register_format,
    shared_skill_roots,
    user_skill_roots,
)
from agent.skills.plugin import SkillCatalogPlugin
from agent.tools.registry_plugin import RegistryPlugin

PATHS = {
    "resource_root": "/tmp/resources",
    "workspace_root": "/tmp/workspace",
    "user_skills_dir": "/tmp/resources/skills",
    "user_tools_dir": "/tmp/resources/tools",
    "user_plugins_dir": "/tmp/resources/plugins",
}


def _skill(root: Path, skill_id: str, *, name: str | None = None) -> None:
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {name or skill_id}\n"
        "user-invocable: true\n"
        "---\n"
        f"Instructions for {skill_id}.\n",
        encoding="utf-8",
    )


# ── the adapters ─────────────────────────────────────────────────────────────


def test_the_roots_come_from_the_formats() -> None:
    roots = {root.label: root for root in user_skill_roots()}

    assert roots["codex"].path.endswith("/.codex/skills")
    assert roots["codex"].system_dir == ".system", "Codex rewrites its own subtree there"
    assert roots["agents"].path.endswith("/.agents/skills")
    assert roots["agents"].system_dir == "", "DSH reads the neutral root without skipSystem"
    assert roots["dsh"].system_dir == ".system", "DSH marks its own root skipSystem"
    assert NATIVE.user_skill_roots == (), (
        "this project's own root is the catalog's primary root; listing it here "
        "would scan it twice under a second name"
    )


def test_a_fourth_ecosystem_is_one_field_on_its_format() -> None:
    """The acceptance test for this seam: no catalog edit, no plugin edit."""
    acme = PluginFormat(
        key="acme-skills",
        label="Acme",
        manifests=("acme.json",),
        user_skill_roots=(SkillRoot(label="acme", path="~/acme/skills"),),
    )
    register_format(acme, position=0)
    try:
        assert ("acme", str(Path.home() / "acme" / "skills")) in {
            (root.label, root.path) for root in user_skill_roots()
        }
    finally:
        from agent.plugins import formats

        formats.PLUGIN_FORMATS.remove(acme)


def test_a_shared_path_named_twice_is_scanned_once() -> None:
    """The neutral root is not read once per ecosystem that happens to use it."""
    codex = PluginFormat(
        key="codex-again",
        label="Codex (again)",
        manifests=("nothing.json",),
        user_skill_roots=(SkillRoot(label="codex-again", path="~/.agents/skills"),),
    )
    register_format(codex, position=0)
    try:
        paths = [root.path for root in user_skill_roots()]
        assert len(paths) == len(set(paths))
        agents = [root for root in user_skill_roots() if root.path.endswith("/.agents/skills")]
        assert len(agents) == 1
        assert agents[0].system_dir == "", "the first declaration with evidence wins"
    finally:
        from agent.plugins import formats

        formats.PLUGIN_FORMATS.remove(codex)


def test_the_neutral_root_honours_the_agents_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp_path / "agents-home"))

    roots = {root.label: root for root in user_skill_roots()}

    assert roots["agents"].path == str(tmp_path / "agents-home" / "skills")


def test_claude_code_declares_no_root_it_cannot_evidence() -> None:
    assert CLAUDE_CODE.user_skill_roots == ()
    assert CODEX.user_skill_roots  # the control: the field is used


def test_only_the_roots_that_exist_are_reported(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DSH_AGENTS_HOME", raising=False)
    assert shared_skill_roots() == (), "a home with none of them"

    _skill(tmp_path / ".agents" / "skills", "vendor")

    assert [root.label for root in shared_skill_roots()] == ["agents"]


# ── what the catalog then loads ──────────────────────────────────────────────


def _catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DSH_AGENTS_HOME", raising=False)
    own_root = tmp_path / "own-skills"
    own_root.mkdir()
    registry_mapping: dict = {"cfg": {}, **{**PATHS, "user_skills_dir": str(own_root)}}
    PluginHost(services=ServiceRegistry.adopt(registry_mapping)).load(RegistryPlugin())
    mapping: dict = {
        "cfg": {"skills": None},
        "registry": registry_mapping["registry"],
        "user_skills_dir": str(own_root),
    }
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(SkillCatalogPlugin())
    assert host.pending() == ()
    return mapping["skill_catalog"], own_root


def test_skills_from_another_agents_directory_are_loaded(tmp_path, monkeypatch) -> None:
    _skill(tmp_path / ".agents" / "skills", "vendor-skill")
    _skill(tmp_path / ".codex" / "skills", "codex-skill")
    catalog, _ = _catalog(tmp_path, monkeypatch)

    loaded = {skill.id: skill for skill in catalog.list_all_skills()}

    assert {"vendor-skill", "codex-skill"} <= set(loaded)
    assert loaded["codex-skill"].source == "shared:codex"
    assert loaded["vendor-skill"].source == "shared:agents"


def test_the_ecosystems_own_system_skills_are_not_adopted(tmp_path, monkeypatch) -> None:
    """Codex rewrites ``skills/.system`` from its embedded assets on startup."""
    _skill(tmp_path / ".codex" / "skills", "codex-skill")
    _skill(tmp_path / ".codex" / "skills" / ".system", "codex-internal")
    catalog, _ = _catalog(tmp_path, monkeypatch)

    loaded = {skill.id for skill in catalog.list_all_skills()}

    assert "codex-skill" in loaded
    assert "codex-internal" not in loaded, "another product's instructions, not the user's"


def test_the_users_own_skill_wins_a_name_collision(tmp_path, monkeypatch) -> None:
    _skill(tmp_path / ".agents" / "skills", "deploy", name="deploy")
    catalog, own_root = _catalog(tmp_path, monkeypatch)
    # A skill of the same id, installed here: the shared layer is a fallback.
    _skill(own_root, "deploy", name="deploy")
    catalog.load_all()
    assert catalog.get("deploy").source == "user"

    from agent.plugins import formats

    for skill_root in formats.shared_skill_roots():
        catalog._load_root(
            Path(skill_root.path),
            source=f"shared:{skill_root.label}",
            keep_existing=True,
            skip_dirs=(skill_root.system_dir,) if skill_root.system_dir else (),
        )

    assert catalog.get("deploy").source == "user"
    instructions = catalog.get("deploy")
    assert instructions is not None


def test_a_missing_shared_root_is_not_an_error(tmp_path, monkeypatch) -> None:
    catalog, _ = _catalog(tmp_path, monkeypatch)

    assert [
        skill for skill in catalog.list_all_skills() if skill.source.startswith("shared:")
    ] == [], "the builtin layer still loads; only the shared one is empty"


@pytest.mark.skipif(
    not Path("~/.codex/skills").expanduser().is_dir(),
    reason="no Codex home on this machine",
)
def test_the_real_codex_home_is_read_without_its_system_skills() -> None:
    """Read-only check on the machine this was written on."""
    from agent.skills.catalog import SkillCatalog

    roots = {root.label: root for root in shared_skill_roots()}
    if "codex" not in roots:
        pytest.skip("no Codex skill root")
    catalog = SkillCatalog(user_root=Path("/tmp/nonexistent-own-skills"))
    codex_root = roots["codex"]
    catalog._load_root(
        Path(codex_root.path),
        source="shared:codex",
        keep_existing=True,
        skip_dirs=(codex_root.system_dir,) if codex_root.system_dir else (),
    )

    loaded = {skill.id for skill in catalog.list_all_skills()}
    codex_system = {"review-agent", "skill-creator", "skill-installer"}
    assert not (codex_system & loaded), (
        f"Codex's own system skills must not be adopted; loaded: {sorted(loaded)}"
    )
