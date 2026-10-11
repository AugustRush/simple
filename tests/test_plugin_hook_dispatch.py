"""Every name the Claude-Code map translates must be either dispatched or recorded."""

import ast
import json
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
    """The Claude Code vocabulary, read from the format adapter that owns it.

    It used to be a dict literal in ``catalog.py``; the loader no longer knows any
    ecosystem's event names, so the guard follows the table to
    ``agent/plugins/formats.py``.  Reading it as source rather than importing it keeps
    the check independent of the module's own import side effects.
    """
    tree = ast.parse(
        (REPO_ROOT / "agent/plugins/formats.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(
            isinstance(t, ast.Name) and t.id == "CLAUDE_CODE_HOOK_EVENTS"
            for t in targets
        ):
            value = node.value
            if not isinstance(value, ast.Dict):
                raise AssertionError("CLAUDE_CODE_HOOK_EVENTS is no longer a literal")
            return {
                k.value: v.value
                for k, v in zip(value.keys, value.values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            }
    raise AssertionError("CLAUDE_CODE_HOOK_EVENTS not found")


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


def _dispatched_from_source() -> set[str]:
    """The event names that actually reach the command-hook runner.

    Read out of ``catalog.py``: ``COMMAND_HOOK_EVENTS`` is a claim about the code,
    and this is what makes the claim checkable.  A new ``fire_*`` method that
    passes a different name to ``_run_command_hooks`` shows up here at once.
    """
    tree = ast.parse((REPO_ROOT / "agent/plugins/catalog.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "_run_command_hooks":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            found.add(str(node.args[0].value))
    return found


def test_the_recorded_dispatchers_are_the_ones_in_the_source() -> None:
    """No hand-list can drift from the code it describes."""
    from agent.plugins.catalog import COMMAND_HOOK_EVENTS

    dispatched = _dispatched_from_source()

    assert dispatched, "control: the scan found the runner's call sites"
    assert dispatched == set(_DISPATCHED), (
        "the record and the source disagree about which command hooks run"
    )
    assert set(COMMAND_HOOK_EVENTS) == dispatched, (
        "the runtime's own list of runnable events must be the same list"
    )


def _hooks_fixture(tmp_path: Path) -> Path:
    """A Claude Code plugin whose hooks file declares one live and one inert event."""
    plugin_dir = tmp_path / "hooked"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "hooked"}', encoding="utf-8"
    )
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [
                            {"type": "command", "command": "echo pre"}
                        ]}
                    ],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo start"}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return plugin_dir


def test_a_declared_but_undispatched_hook_is_reported(tmp_path) -> None:
    """The silent promise, made audible.

    A plugin declaring ``SessionStart`` loads, validates and does nothing when a
    session starts.  The catalog now says so where the plugin becomes real.
    """
    from agent import PluginCatalog
    from agent.plugins.catalog import inert_command_hooks, plugin_inert_hooks

    plugin_dir = _hooks_fixture(tmp_path)

    assert plugin_inert_hooks(plugin_dir) == ("on_session_start",)

    catalog = PluginCatalog(builtin_dir=plugin_dir)
    catalog.discover_and_load()
    catalog.flush_pending()

    meta = catalog._plugins["hooked"][1]
    assert set(meta.hooks_config) == {"on_pre_tool", "on_session_start"}
    assert inert_command_hooks(meta.hooks_config) == ("on_session_start",)


def test_the_report_names_the_events(tmp_path, capsys) -> None:
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=_hooks_fixture(tmp_path))
    catalog.discover_and_load()
    catalog.flush_pending()

    # The console wraps to the terminal width, so compare on flattened whitespace.
    flat = " ".join(capsys.readouterr().out.split())
    assert "have no dispatcher in this runtime and will not run" in flat
    assert "on_session_start" in flat
    assert "on_pre_tool" not in flat.replace("on_session_start", ""), (
        "the dispatched event must not be reported as dead"
    )


def test_the_installer_says_it_before_activation(tmp_path) -> None:
    """The approval message carries it, so nobody approves a dead hook believing otherwise."""
    from agent.tools.plugin_tools import _inert_hooks_note

    note = _inert_hooks_note(("on_session_start", "on_task_created"))

    assert "will not run" in note
    assert "on_session_start" in note and "on_task_created" in note
    assert _inert_hooks_note(()) == ""


def test_the_install_approval_says_a_declared_hook_will_not_run(
    tmp_path, monkeypatch, capsys
) -> None:
    """The consent moment, end to end.

    A plugin whose hooks file declares an event with no dispatcher must not be
    approved as if it worked: the message the human reads names the dead events, and
    the payload the caller receives says it again.  Nothing here starts a process --
    the hook is declared, never run.
    """
    from test_plugin_catalog import (
        _FakeConfirmationSink,
        _install_plugin_harness,
        _run_install_with_sink,
    )

    source = _hooks_fixture(tmp_path)
    tools, _registry, _catalog, _user_plugins = _install_plugin_harness(tmp_path, monkeypatch)
    sink = _FakeConfirmationSink(True)

    result = _run_install_with_sink(tools, str(source), sink=sink, name="hooked")

    assert result.get("ok") is True, result
    assert result["inert_hooks"] == ["on_session_start"]
    assert sink.asked, "executable content still requires approval"
    reason = " ".join(str(sink.asked[0]["reason"]).split())
    assert "will not run" in reason and "on_session_start" in reason


def test_a_long_list_is_truncated_not_dumped() -> None:
    from agent.plugins.catalog import inert_command_hooks
    from agent.tools.plugin_tools import _inert_hooks_note

    many = {f"on_event_{index}": [{"command": "echo x"}] for index in range(9)}
    events = inert_command_hooks(many)
    note = _inert_hooks_note(events)

    assert len(events) == 9
    assert note.count("on_event_") == 4
    assert note.rstrip().endswith("…)")


def test_the_dispatched_set_is_not_empty() -> None:
    """Control: the scan reads the map, and an empty map would pass everything above."""
    assert len(_cc_event_map()) >= 20
