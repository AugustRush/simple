"""Every name the Claude-Code map translates must be either dispatched or recorded."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Internal event names that actually have a command-hook dispatcher, and where.
#:
#: A name in the translation map with no entry here is a hook an operator can declare,
#: that the catalog will translate, store -- and **never run**.  That is what the rest of
#: this file is about.
_DISPATCHED = {
    "on_pre_tool": "PluginCatalog.fire_pre_tool",
    "on_post_tool": "PluginCatalog.fire_post_tool",
    "on_prompt_submit": "PluginCatalog.fire_prompt_submit",
    "on_turn_end": "PluginCatalog.fire_turn_end",
}

#: Translated but not dispatched, with the reason.  Recorded rather than deleted: the
#: table is Claude Code compatibility, and dropping a name would turn "this hook does
#: nothing yet" into "this hook is not recognised" -- a different and louder failure.
_NOT_DISPATCHED: dict[str, str] = {
    # Lifecycle.  The catalog announces sessions through typed events now
    # (`SessionFinished` on the `session` domain), which is how a plugin subscribes; the
    # command-hook dispatchers for these were never written.
    "on_session_start": "no command-hook dispatcher; SessionFinished covers plugins",
    "on_session_end": "no command-hook dispatcher",
    "on_setup": "no command-hook dispatcher",
    # The rest are Claude Code events this codebase has no counterpart for at all: it
    # has no compaction, no permission prompts, no sub-agent registry and no task board
    # at the Claude Code level.  Translating them keeps a plugin.json loadable.
    "on_prompt_expansion": "no counterpart in this codebase",
    "on_post_tool_failure": "no counterpart",
    "on_post_tool_batch": "no counterpart",
    "on_permission_request": "no counterpart",
    "on_permission_denied": "no counterpart",
    "on_turn_failure": "no counterpart",
    "on_pre_compact": "no counterpart",
    "on_post_compact": "no counterpart",
    "on_notification": "no counterpart",
    "on_message_display": "no counterpart",
    "on_subagent_start": "no counterpart",
    "on_subagent_stop": "no counterpart",
    "on_task_created": "no counterpart",
    "on_task_completed": "no counterpart",
    "on_config_change": "no counterpart",
    "on_cwd_changed": "no counterpart",
    "on_file_changed": "no counterpart",
    "on_instructions_loaded": "no counterpart",
    "on_elicitation": "no counterpart",
    "on_elicitation_result": "no counterpart",
    "on_teammate_idle": "no counterpart",
    "on_worktree_create": "no counterpart",
    "on_worktree_remove": "no counterpart",
}


def _cc_event_map() -> dict[str, str]:
    tree = ast.parse((REPO_ROOT / "agent/plugins/catalog.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_CC_EVENT_MAP" for t in node.targets
        ):
            return {
                k.value: v.value
                for k, v in zip(node.value.keys, node.value.values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            }
    raise AssertionError("_CC_EVENT_MAP not found")


def test_every_translated_event_is_dispatched_or_recorded() -> None:
    """The gap this file exists for, made checkable.

    The translation map names ~30 events a ``plugin.json`` may declare hooks for.  Four
    of them have a dispatcher.  The other ~26 are stored on the plugin's metadata and
    never run -- so a ``SessionStart`` command hook does nothing, silently, and has done
    since before this table was last touched.

    That is a **pre-existing** gap, measured rather than assumed: the four dispatchers
    were the same four in the commit before the hook protocol was removed.  What was
    missing was any statement of it, so a name could be added to the map and look wired
    because it sits beside wired ones.

    A new name must now be added to one list or the other, and the second list has to say
    why -- which is the whole difference between an invisible promise and a recorded one.
    """
    translated = set(_cc_event_map().values())
    recorded = set(_DISPATCHED) | set(_NOT_DISPATCHED)

    unrecorded = sorted(translated - recorded)
    assert not unrecorded, (
        "these events are translated from plugin.json and neither dispatched nor "
        "recorded as not dispatched:\n  " + "\n  ".join(unrecorded)
    )

    stale = sorted(recorded - translated)
    assert not stale, f"recorded but no longer in the map: {stale}"


def test_the_dispatchers_named_here_exist() -> None:
    """Control: the list of wired events must point at real methods.

    An entry naming a method that was deleted is the same defect one level down -- it
    would record a hook as wired after the dispatcher went away.
    """
    from agent.plugins.catalog import PluginCatalog

    for event, target in _DISPATCHED.items():
        owner, _, method = target.partition(".")
        assert owner == "PluginCatalog"
        assert hasattr(PluginCatalog, method), f"{event} names a missing {target}"


def test_the_dispatched_set_is_not_empty() -> None:
    """Control: the scan reads the map, and an empty map would pass everything above."""
    assert len(_cc_event_map()) >= 20
