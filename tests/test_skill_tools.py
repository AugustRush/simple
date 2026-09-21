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


def test_skill_file_tools_accept_a_bundle_beneath_a_symlinked_root(tmp_path):
    """A resolved child must be compared with its resolved bundle root.

    On macOS ``/var`` itself is a symlink to ``/private/var``.  The old guard
    resolved the requested child but not ``bundle.path``, so a perfectly local
    file appeared outside its own bundle.  Test all three guarded operations:
    list, read, and write.
    """
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    registry = ToolRegistry()
    catalog = _catalog_with_files(linked_root, "demo", ["scripts/a.py"])
    catalog.register_tools(registry)

    listed = _call(
        registry,
        "list_skill_files",
        {"skill_name": "demo", "path": "scripts"},
    )
    assert listed["ok"] is True
    assert listed["files"] == ["scripts/a.py"]

    read = _call(
        registry,
        "read_skill_file",
        {"skill_name": "demo", "path": "scripts/a.py"},
    )
    assert read["ok"] is True
    assert read["content"] == "data"

    written = _call(
        registry,
        "write_skill_file",
        {"skill_name": "demo", "path": "scripts/a.py", "content": "updated"},
    )
    assert written["ok"] is True
    assert (real_root / "skills" / "demo" / "scripts" / "a.py").read_text() == "updated"


def test_skill_file_tools_still_refuse_a_link_that_points_outside(tmp_path):
    """Resolving the bundle root must not cost the containment guarantee.

    The guard exists so a path inside a bundle cannot reach the filesystem
    outside it.  Resolving both sides is what keeps that true when the escape
    is a symlink rather than a ``../``: the link resolves to its target, which
    is outside the root, and is refused.
    """
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", [])
    catalog.register_tools(registry)

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not read me", encoding="utf-8")
    (tmp_path / "skills" / "demo" / "escape").symlink_to(outside)

    read = _call(registry, "read_skill_file", {"skill_name": "demo", "path": "escape"})
    assert read["ok"] is False
    assert "escapes" in read["error"]

    written = _call(
        registry,
        "write_skill_file",
        {"skill_name": "demo", "path": "escape", "content": "clobbered"},
    )
    assert written["ok"] is False
    assert "escapes" in written["error"]
    assert outside.read_text(encoding="utf-8") == "do not read me"


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


def test_list_skills_observes_installs_on_disk(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    assert [b.id for b in catalog.list_skills()] == []

    _install_skill_on_disk(root, "late-skill")

    assert [b.id for b in catalog.list_skills()] == ["late-skill"]


# --- the skill switch -------------------------------------------------------
#
# A built-in skill ships with the package, so there is no deleting one the
# person does not want.  The switch is the only answer, and it has to be a
# *decision about a bundle* rather than a property of it: the bundle stays
# loaded so the screen that offers the switch can still find it, while every
# surface the model can reach acts as if it were not there.


def _catalog_with_switch(tmp_path, *skill_ids, skill_config=None) -> SkillCatalog:
    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)
    for skill_id in skill_ids:
        _install_skill_on_disk(root, skill_id)
    catalog = SkillCatalog(
        user_root=root,
        builtin_root=tmp_path / "builtin",
        skill_config=skill_config,
    )
    catalog.load_all()
    return catalog


def test_a_skill_is_on_until_someone_says_otherwise(tmp_path):
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})

    assert catalog.is_enabled("review") is True
    assert catalog.get("review") is not None
    assert [b.id for b in catalog.list_skills()] == ["review"]


def test_a_config_written_before_the_switch_existed_reads_as_all_on(tmp_path):
    """No "skills" section means nobody has an opinion, not "all off"."""
    catalog = _catalog_with_switch(tmp_path, "review", skill_config=None)

    assert catalog.is_enabled("review") is True
    assert [b.id for b in catalog.list_skills()] == ["review"]


def test_a_saved_switch_is_honoured_at_construction(tmp_path):
    catalog = _catalog_with_switch(
        tmp_path, "review", skill_config={"review": {"enabled": False}}
    )

    assert catalog.is_enabled("review") is False
    assert catalog.get("review") is None
    assert [b.id for b in catalog.list_all_skills()] == ["review"]


def test_switching_a_skill_off_takes_it_out_of_the_model_s_reach(tmp_path):
    catalog = _catalog_with_switch(tmp_path, "review", "other", skill_config={})
    catalog.set_enabled("review", False)

    assert catalog.get("review") is None
    assert [b.id for b in catalog.list_skills()] == ["other"]
    listing = "\n".join(catalog.summary_lines())
    assert "- review " not in listing
    assert "- other " in listing


def test_a_switched_off_skill_stays_findable_for_the_screen_that_lists_it(tmp_path):
    """Otherwise switching one off is a one-way door: nothing left to click."""
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})
    catalog.set_enabled("review", False)

    assert [b.id for b in catalog.list_all_skills()] == ["review"]
    bundle = catalog.find_any("review")
    assert bundle is not None
    assert catalog.is_enabled(bundle) is False


def test_a_switched_off_skill_can_be_switched_back_on(tmp_path):
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})

    catalog.set_enabled("review", False)
    assert catalog.get("review") is None

    catalog.set_enabled("review", True)
    assert catalog.get("review") is not None
    assert [b.id for b in catalog.list_skills()] == ["review"]


def test_switching_a_skill_off_asks_for_the_prompt_to_be_rebuilt(tmp_path):
    """The switch has to land without restarting the agent.

    The runtime recomposes the prompt whenever ``consume_dirty`` reports a
    change, so marking the catalog dirty *is* the whole of taking effect.
    """
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})
    assert catalog.consume_dirty() is False

    catalog.set_enabled("review", False)

    assert catalog.consume_dirty() is True
    assert catalog.consume_dirty() is False


def test_a_refusal_says_which_kind_of_missing_it_is(tmp_path):
    """"not found" and "switched off" send the reader to different places."""
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})
    registry = ToolRegistry()
    catalog.register_tools(registry)
    catalog.set_enabled("review", False)

    off = _call(registry, "activate_skill", {"skill_name": "review"})
    assert off["ok"] is False
    assert "review" in off["error"]
    assert "switched off" in off["error"]

    typo = _call(registry, "activate_skill", {"skill_name": "revieq"})
    assert typo["ok"] is False
    assert "not found" in typo["error"]


def test_the_tools_that_read_a_bundle_refuse_a_switched_off_one(tmp_path):
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})
    registry = ToolRegistry()
    catalog.register_tools(registry)
    catalog.set_enabled("review", False)

    files = _call(registry, "list_skill_files", {"skill_name": "review"})
    assert files["ok"] is False
    assert "switched off" in files["error"]

    read = _call(registry, "read_skill_file", {"skill_name": "review", "path": "SKILL.md"})
    assert read["ok"] is False
    assert "switched off" in read["error"]


def test_a_switched_off_skill_can_still_be_edited_and_deleted(tmp_path):
    """Managing a skill and using it are different questions."""
    catalog = _catalog_with_switch(tmp_path, "review", skill_config={})
    registry = ToolRegistry()
    catalog.register_tools(registry)
    catalog.set_enabled("review", False)

    renamed = _call(
        registry,
        "update_skill",
        {"skill_id": "review", "description": "still editable"},
    )
    assert renamed["ok"] is True
    assert catalog.find_any("review").description == "still editable"

    removed = _call(registry, "delete_skill", {"skill_id": "review"})
    assert removed["ok"] is True
    assert catalog.find_any("review") is None


def test_deleting_a_skill_throws_its_switch_away_with_it(tmp_path):
    """An id can be reused; the new skill must not arrive already off."""
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    _install_skill_on_disk(root, "review")
    switches: dict = {}
    catalog = SkillCatalog(
        user_root=root, builtin_root=tmp_path / "builtin", skill_config=switches
    )
    catalog.load_all()
    registry = ToolRegistry()
    catalog.register_tools(registry)
    catalog.set_enabled("review", False)

    _call(registry, "delete_skill", {"skill_id": "review"})
    assert "review" not in switches

    _install_skill_on_disk(root, "review", body="A different review.")
    assert catalog.get("review") is not None
    assert catalog.is_enabled("review") is True


def test_update_skill_archives_the_version_it_replaces(tmp_path):
    """An edit is a hypothesis about what the skill should say, and a
    hypothesis needs a way back -- otherwise a change that made the skill worse
    is indistinguishable from one that made it better, because the evidence it
    was worse is gone."""
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", [])
    catalog.register_tools(registry)

    skill_dir = tmp_path / "skills" / "demo"
    before = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    updated = _call(
        registry,
        "update_skill",
        {"skill_id": "demo", "instructions": "Revised instructions."},
    )
    assert updated["ok"] is True
    archived = updated["archived"]
    assert archived is not None
    # The whole file, frontmatter included -- that is what a restore puts back.
    assert (skill_dir / archived).read_text(encoding="utf-8") == before
    assert "Revised instructions." in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_archived_versions_are_history_not_supporting_material(tmp_path):
    """Offering an old SKILL.md beside the live instructions would invite the
    model to follow the version it was just handed a replacement for."""
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", [])
    catalog.register_tools(registry)

    archived = _call(
        registry,
        "update_skill",
        {"skill_id": "demo", "instructions": "Revised instructions."},
    )["archived"]

    bundle = catalog.find_any("demo")
    assert bundle.supporting_files == []
    assert bundle.versions == [archived]
    assert archived not in catalog.activation_text("demo", explicit=True)

    # Reachable on demand, though, or there would be no way back.
    listed = _call(registry, "list_skill_files", {"skill_name": "demo"})
    assert listed["files"] == []
    assert listed["versions"] == [archived]
    read = _call(registry, "read_skill_file", {"skill_name": "demo", "path": archived})
    assert read["ok"] is True
    assert "Instructions." in read["content"]

    payload = _call(registry, "activate_skill", {"skill_name": "demo"})
    assert payload["skill"]["versions"] == [archived]
    assert "rollback" in payload["hints"]


def test_update_skill_keeps_every_version_it_replaces(tmp_path):
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", [])
    catalog.register_tools(registry)

    for index in range(3):
        _call(
            registry,
            "update_skill",
            {"skill_id": "demo", "instructions": f"Body {index}"},
        )

    versions = catalog.find_any("demo").versions
    assert len(versions) == 3
    assert versions == sorted(versions), "oldest first"
    # _datestamp() is second-resolution, so three edits in one second collide
    # unless the name is disambiguated.
    assert len(set(versions)) == 3


def test_archived_versions_are_not_loaded_as_separate_skills(tmp_path):
    """The loader matches the exact name SKILL.md, so history must not
    reappear as a bundle of its own."""
    registry = ToolRegistry()
    catalog = _catalog_with_files(tmp_path, "demo", [])
    catalog.register_tools(registry)
    for index in range(3):
        _call(
            registry,
            "update_skill",
            {"skill_id": "demo", "instructions": f"Body {index}"},
        )

    assert [bundle.id for bundle in catalog.list_all_skills()] == ["demo"]
    assert catalog.find_any("SKILL.md") is None
