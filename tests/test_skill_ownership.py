"""The ownership boundary on skill writes.

A SKILL.md is not merely data: it is injected into the system prompt, so rewriting
one rewrites the agent's own instructions.  That makes the interesting question not
"may the agent write skills?" (it must -- writing skills is how it extends itself)
but "whose skill is it?".  The rule these tests pin down:

* a bundle ``create_skill`` made is the agent's and stays freely editable;
* a bundle a person wrote (or installed from elsewhere) may only be rewritten with
  a stated ``intent``, and a refusal says why and how to ask again.

Everything goes through ``registry.call`` rather than a direct handler call: the
gate is the tool's *authorizer*, so a direct call would test everything except the
thing under test.

The provenance mark lives in the SKILL.md frontmatter (``created-by: agent``), which
is why the persistence tests build a **new** catalog over the same directory -- the
mark has to survive a reload, not merely a dict in the process that wrote it.
"""

import asyncio
import json

from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import ToolRegistry


def _call(registry: ToolRegistry, name: str, args: dict) -> dict:
    return json.loads(asyncio.run(registry.call(name, args)))


def _registry(catalog: SkillCatalog) -> ToolRegistry:
    registry = ToolRegistry()
    catalog.register_tools(registry)
    return registry


def _catalog(tmp_path, extra_frontmatter: str = "") -> SkillCatalog:
    """A user root with one bundle that was written by hand, as a person would."""
    root = tmp_path / "skills"
    bundle = root / "handwritten"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\n"
        "name: Hand Written\n"
        "description: written by a person, not by the agent\n"
        f"{extra_frontmatter}"
        "user-invocable: true\n"
        "---\n"
        "Original instructions.\n",
        encoding="utf-8",
    )
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    return catalog


SPECIFIC_INTENT = (
    "用户要求把这份技能的步骤顺序改成先跑 lint 再跑测试，因为上一轮就是顺序错了"
)


# ── the freedom that is kept: the agent's own bundles ────────────────────────


def test_a_bundle_the_agent_created_is_freely_editable(tmp_path):
    """No intent, no confirmation: the agent's own scratchpad stays its own.

    This is the half of the boundary that must *not* regress -- gating a skill the
    agent authored would be the same mistake in the other direction.
    """
    catalog = SkillCatalog(user_root=tmp_path / "skills", builtin_root=tmp_path / "b")
    catalog.load_all()
    registry = _registry(catalog)

    created = _call(
        registry,
        "create_skill",
        {
            "skill_id": "my-notes",
            "name": "My Notes",
            "instructions": "First draft.",
        },
    )
    assert created["ok"] is True

    # The marker is written at creation, into the file, not into memory.
    assert "created-by: agent" in (
        tmp_path / "skills" / "my-notes" / "SKILL.md"
    ).read_text(encoding="utf-8")

    updated = _call(
        registry, "update_skill", {"skill_id": "my-notes", "instructions": "Second draft."}
    )
    assert updated["ok"] is True
    assert catalog.find_any("my-notes").body == "Second draft."

    written = _call(
        registry,
        "write_skill_file",
        {"skill_name": "my-notes", "path": "notes.md", "content": "data"},
    )
    assert written["ok"] is True

    removed = _call(registry, "delete_skill", {"skill_id": "my-notes"})
    assert removed["ok"] is True
    assert catalog.find_any("my-notes") is None


def test_the_origin_mark_survives_a_reload(tmp_path):
    """Provenance has to be on disk, or the next process sees a stranger's skill.

    A fresh catalog over the same directory stands in for the next turn's process:
    if the mark were only in memory, this bundle would read as hand-written and the
    agent would be locked out of what it just created.
    """
    root = tmp_path / "skills"
    first = SkillCatalog(user_root=root, builtin_root=tmp_path / "b")
    first.load_all()
    _call(
        _registry(first),
        "create_skill",
        {"skill_id": "mine", "name": "Mine", "instructions": "Draft."},
    )

    second = SkillCatalog(user_root=root, builtin_root=tmp_path / "b")
    second.load_all()
    bundle = second.find_any("mine")
    assert bundle is not None
    assert bundle.agent_authored is True

    updated = _call(_registry(second), "update_skill", {"skill_id": "mine", "instructions": "Revised."})
    assert updated["ok"] is True
    assert second.find_any("mine").body == "Revised."


def test_editing_an_agent_bundle_keeps_it_the_agents(tmp_path):
    """An edit is not a claim of authorship: the mark is carried through, not set."""
    catalog = SkillCatalog(user_root=tmp_path / "skills", builtin_root=tmp_path / "b")
    catalog.load_all()
    registry = _registry(catalog)
    _call(registry, "create_skill", {"skill_id": "mine", "name": "Mine"})

    _call(registry, "update_skill", {"skill_id": "mine", "description": "changed"})

    bundle = catalog.find_any("mine")
    assert bundle.agent_authored is True
    # Still free on the next write, without an intent -- the round trip did not
    # lose the mark.
    assert _call(registry, "update_skill", {"skill_id": "mine", "name": "Renamed"})["ok"] is True


# ── the boundary: bundles a person wrote ─────────────────────────────────────


def test_a_hand_written_skill_is_not_overwritten_without_an_intent(tmp_path):
    """The default has to be protection: no flag, no setting, no opt-in."""
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)
    skill_md = tmp_path / "skills" / "handwritten" / "SKILL.md"
    before = skill_md.read_bytes()

    result = _call(
        registry,
        "update_skill",
        {"skill_id": "handwritten", "instructions": "The agent's instructions now."},
    )

    assert result["ok"] is False
    assert result["intent_required"] is True
    # Byte-identical, not merely "the body did not parse differently": nothing was
    # written, so not even an archive copy of the user's file was left behind.
    assert skill_md.read_bytes() == before
    assert catalog.find_any("handwritten").body == "Original instructions."


def test_the_refusal_names_the_owner_and_the_way_back_in(tmp_path):
    """A denial the model can retell: whose file it is, and what unlocks it."""
    registry = _registry(_catalog(tmp_path))
    result = _call(registry, "update_skill", {"skill_id": "handwritten"})
    error = result["error"]

    assert "user" in error  # it is the user's, not the agent's
    assert "input.intent" in error  # how to be allowed
    assert "create_skill" in error  # and an honest alternative
    # Not a bare policy word: the reason (the SKILL.md is injected into the prompt)
    # is what makes the refusal make sense to whoever reads it.
    assert "instructions" in error


def test_a_stated_intent_unlocks_a_hand_written_skill(tmp_path):
    """With a reason on the record the edit happens -- the gate is not a wall."""
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)

    result = _call(
        registry,
        "update_skill",
        {
            "skill_id": "handwritten",
            "instructions": "Reordered instructions.",
            "intent": SPECIFIC_INTENT,
        },
    )

    assert result["ok"] is True
    assert catalog.find_any("handwritten").body == "Reordered instructions."


def test_an_edit_does_not_launder_a_hand_written_skill_into_the_agents(tmp_path):
    """Otherwise one allowed edit would buy unlimited silent rewrites after it."""
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)
    _call(
        registry,
        "update_skill",
        {"skill_id": "handwritten", "instructions": "Edited once.", "intent": SPECIFIC_INTENT},
    )

    again = _call(
        registry,
        "update_skill",
        {"skill_id": "handwritten", "instructions": "Edited silently."},
    )

    assert again["ok"] is False
    assert catalog.find_any("handwritten").body == "Edited once."


def test_a_vague_intent_is_not_a_reason(tmp_path):
    """ "do it" is a rubber stamp; the reason has to be specific enough to read."""
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)

    result = _call(
        registry,
        "update_skill",
        {"skill_id": "handwritten", "instructions": "x", "intent": "do it"},
    )

    assert result["ok"] is False
    assert catalog.find_any("handwritten").body == "Original instructions."


def test_a_hand_written_skill_is_not_deleted_without_an_intent(tmp_path):
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)

    result = _call(registry, "delete_skill", {"skill_id": "handwritten"})

    assert result["ok"] is False
    assert result["intent_required"] is True
    assert (tmp_path / "skills" / "handwritten" / "SKILL.md").exists()


def test_a_stated_intent_lets_the_agent_delete_a_hand_written_skill(tmp_path):
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)

    result = _call(
        registry,
        "delete_skill",
        {"skill_id": "handwritten", "intent": SPECIFIC_INTENT},
    )

    assert result["ok"] is True
    assert not (tmp_path / "skills" / "handwritten").exists()
    assert catalog.find_any("handwritten") is None


def test_a_hand_written_bundles_resource_file_needs_an_intent(tmp_path):
    """write_skill_file creates and overwrites files, so it is on the same boundary."""
    catalog = _catalog(tmp_path)
    registry = _registry(catalog)

    refused = _call(
        registry,
        "write_skill_file",
        {"skill_name": "handwritten", "path": "notes.md", "content": "agent data"},
    )
    assert refused["ok"] is False
    assert refused["intent_required"] is True
    assert not (tmp_path / "skills" / "handwritten" / "notes.md").exists()

    allowed = _call(
        registry,
        "write_skill_file",
        {
            "skill_name": "handwritten",
            "path": "notes.md",
            "content": "agent data",
            "intent": SPECIFIC_INTENT,
        },
    )
    assert allowed["ok"] is True
    assert (
        tmp_path / "skills" / "handwritten" / "notes.md"
    ).read_text(encoding="utf-8") == "agent data"


def test_the_resource_path_rules_are_unchanged(tmp_path):
    """Ownership is a second gate, not a replacement: the path guards still fire.

    Checked on an agent-created bundle on purpose -- there the ownership gate
    cannot be the reason, so a green test means these refusals still come from the
    path rules they always came from.
    """
    catalog = SkillCatalog(user_root=tmp_path / "skills", builtin_root=tmp_path / "b")
    catalog.load_all()
    registry = _registry(catalog)
    _call(registry, "create_skill", {"skill_id": "mine", "name": "Mine"})

    skill_md = _call(
        registry,
        "write_skill_file",
        {"skill_name": "mine", "path": "SKILL.md", "content": "hijack"},
    )
    assert skill_md["ok"] is False
    assert "update_skill" in skill_md["error"]

    escape = _call(
        registry,
        "write_skill_file",
        {"skill_name": "mine", "path": "../outside.md", "content": "x"},
    )
    assert escape["ok"] is False
    assert "escapes the skill bundle" in escape["error"]

    absolute = _call(
        registry,
        "write_skill_file",
        {"skill_name": "mine", "path": "/tmp/outside.md", "content": "x"},
    )
    assert absolute["ok"] is False
    assert "relative" in absolute["error"]


def test_the_mark_is_the_switch_and_only_the_exact_value(tmp_path):
    """Documenting the mechanism: frontmatter decides, and a near-miss protects.

    The second half matters more than the first: a marker a person typed by hand
    (``created-by: user``) must not be mistaken for a key that unlocks the bundle.
    """
    agent_marked = _catalog(tmp_path / "a", "created-by: agent\n")
    result = _call(
        _registry(agent_marked),
        "update_skill",
        {"skill_id": "handwritten", "instructions": "free"},
    )
    assert result["ok"] is True

    user_marked = _catalog(tmp_path / "b", "created-by: user\n")
    refused = _call(
        _registry(user_marked),
        "update_skill",
        {"skill_id": "handwritten", "instructions": "not free"},
    )
    assert refused["ok"] is False
    assert user_marked.find_any("handwritten").body == "Original instructions."


def test_a_missing_skill_still_answers_not_found(tmp_path):
    """The gate must not shadow the handler's more useful sentence."""
    registry = _registry(_catalog(tmp_path))

    result = _call(registry, "update_skill", {"skill_id": "no-such-skill"})

    assert result["ok"] is False
    assert "not found" in result["error"]
    assert "intent" not in result["error"]


def test_a_builtin_skill_still_answers_builtin(tmp_path):
    """Builtins are read-only for a different reason, and say so."""
    builtin_root = tmp_path / "builtin"
    bundle = builtin_root / "shipped"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("---\nname: Shipped\n---\nBuilt in.\n", encoding="utf-8")
    catalog = SkillCatalog(user_root=tmp_path / "skills", builtin_root=builtin_root)
    catalog.load_all()
    registry = _registry(catalog)

    result = _call(registry, "update_skill", {"skill_id": "shipped"})

    assert result["ok"] is False
    assert "built-in" in result["error"]
    assert "intent_required" not in result
