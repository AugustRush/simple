"""Tests for SkillCatalog-registered runtime tools."""

import asyncio
import json
from pathlib import Path

from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import ToolRegistry


def _catalog_with_files(tmp_path, skill_id: str, files: list[str]) -> SkillCatalog:
    root = tmp_path / "skills"
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        "user-invocable: true\n"
        "---\n"
        "Instructions.\n",
        encoding="utf-8",
    )
    for rel in files:
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("data", encoding="utf-8")
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    return catalog


def _call(registry: ToolRegistry, name: str, args: dict) -> dict:
    return json.loads(asyncio.run(registry.call(name, args)))


def test_list_skill_files_accepts_optional_path_filter(tmp_path):
    registry = ToolRegistry()
    catalog = _catalog_with_files(
        tmp_path,
        "demo",
        ["scripts/a.py", "scripts/sub/b.py", "README.md"],
    )
    catalog.register_tools(registry)

    whole = _call(registry, "list_skill_files", {"skill_name": "demo"})
    assert whole["ok"] is True
    assert whole["path"] == "."
    assert whole["files"] == ["README.md", "scripts/a.py", "scripts/sub/b.py"]

    filtered = _call(
        registry,
        "list_skill_files",
        {"skill_name": "demo", "path": "scripts"},
    )
    assert filtered["ok"] is True
    assert filtered["path"] == "scripts"
    assert filtered["files"] == ["scripts/a.py", "scripts/sub/b.py"]

    dot = _call(
        registry,
        "list_skill_files",
        {"skill_name": "demo", "path": "."},
    )
    assert dot["files"] == whole["files"]


def test_list_skill_files_rejects_escapes_and_unknown_fields(tmp_path):
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", ["a.txt"])
    catalog.register_tools(registry)

    escaped = _call(
        registry,
        "list_skill_files",
        {"skill_name": "demo", "path": "../secret"},
    )
    assert escaped["ok"] is False
    assert "escapes" in escaped["error"]

    unknown = _call(
        registry,
        "list_skill_files",
        {"skill_name": "demo", "bogus": 1},
    )
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "invalid_request"

    missing = _call(registry, "list_skill_files", {"skill_name": "nope"})
    assert missing["ok"] is False
    assert "not found" in missing["error"]


def test_skill_overwrites_are_durable_replacements(tmp_path, monkeypatch):
    """Both writers overwrite files a user may have authored by hand.

    A truncating write can destroy the previous version outright, so each has to
    replace all-or-nothing: a reader sees the old file or the new one.
    """
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", ["notes.md"])
    catalog.register_tools(registry)

    skill_md = tmp_path / "skills" / "demo" / "SKILL.md"
    notes = tmp_path / "skills" / "demo" / "notes.md"

    replaced: list[str] = []
    real_replace = Path.replace

    def observing_replace(self, target):
        replaced.append(Path(target).name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", observing_replace)

    updated = _call(
        registry,
        "update_skill",
        {"skill_id": "demo", "instructions": "Revised instructions."},
    )
    assert updated["ok"] is True
    assert "Revised instructions." in skill_md.read_text(encoding="utf-8")

    written = _call(
        registry,
        "write_skill_file",
        {"skill_name": "demo", "path": "notes.md", "content": "fresh data"},
    )
    assert written["ok"] is True
    assert notes.read_text(encoding="utf-8") == "fresh data"

    # Both went through the durable primitive, not an in-place truncation.
    assert replaced == ["SKILL.md", "notes.md"]
    leftovers = [
        p.name
        for p in (tmp_path / "skills" / "demo").iterdir()
        if p.name.startswith(".")
    ]
    assert leftovers == []


def _install_skill_on_disk(root: Path, skill_id: str, body: str = "Instructions.") -> None:
    """Drop a bundle into the skills root, as an external install would."""
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: {skill_id} helper\n"
        "user-invocable: true\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_skill_copied_in_during_session_is_found_without_restart(tmp_path):
    """The catalog must observe the directory, not a startup snapshot.

    A skill installed while the process runs (copying a directory, a plugin,
    or another agent writing files) used to stay invisible until restart:
    activate_skill returned "not found".
    """
    root = tmp_path / "skills"
    root.mkdir()
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    assert catalog.get("late-skill") is None

    _install_skill_on_disk(root, "late-skill")

    bundle = catalog.get("late-skill")
    assert bundle is not None
    assert bundle.id == "late-skill"


def test_skill_copied_in_during_session_refreshes_prompt_listing(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    assert catalog.consume_dirty() is False

    _install_skill_on_disk(root, "late-skill")

    assert catalog.consume_dirty() is True
    listing = "\n".join(catalog.summary_lines())
    assert "late-skill" in listing


def test_activate_skill_tool_resolves_externally_installed_bundle(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    registry = ToolRegistry()
    catalog.register_tools(registry)

    _install_skill_on_disk(root, "late-skill", body="Do the late thing.")

    result = _call(registry, "activate_skill", {"skill_name": "late-skill"})
    assert result["ok"] is True
    assert "Do the late thing." in json.dumps(result, ensure_ascii=False)


def test_user_skill_refresh_keeps_plugin_bundled_skills(tmp_path):
    """A disk refresh must not drop skills PluginCatalog attached.

    The user layer is re-read on staleness; plugin roots are attached after
    load_all() and would be lost by a full reload.
    """
    root = tmp_path / "skills"
    root.mkdir()
    plugin_root = tmp_path / "plugin-skills"
    plugin_dir = plugin_root / "bundled"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "SKILL.md").write_text(
        "---\nname: bundled\n---\nPlugin instructions.\n", encoding="utf-8"
    )
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    catalog._load_root(plugin_root, source="plugin:demo")
    # Plugin skills are namespaced by plugin name.
    assert catalog.get("demo:bundled") is not None

    _install_skill_on_disk(root, "late-skill")

    assert catalog.get("late-skill") is not None
    assert catalog.get("demo:bundled") is not None
