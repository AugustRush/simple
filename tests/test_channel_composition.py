"""One place decides which channels a configuration runs.

Task 8 of the refactor asks for composition decisions to be findable in one
place.  For channels that was already *almost* true before this change: a single
function held both the ``web`` and the ``feishu`` switch.  But it lived in
``agent/channels/base.py`` -- the wrong layer, because which channels run is a
composition decision, not a channel implementation detail -- and the scheduler
has no equivalent at all, for a reason recorded below.

These tests hold two things in place: the decision lives with composition, and
nothing else constructs a channel.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Channel implementations that only the builder may construct.
_CHANNEL_CLASSES = ("WebChannel", "FeishuChannel", "CliChannel")

#: The one module allowed to name them in a call.
_BUILDER_MODULE = "agent/bootstrap.py"


def _call_sites(source: str) -> list[str]:
    """Names constructed via ``ClassName(...)`` in *source*, ignoring strings."""
    # A tiny AST pass rather than a regex: a regex would match the class
    # *definitions* and any mention inside a docstring, and those are prose.
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _CHANNEL_CLASSES:
            found.append(name)
    return found


def test_the_builder_lives_with_composition() -> None:
    """``build_gateway_channels`` is in the composition module, not the channel one."""
    from agent.bootstrap import build_gateway_channels

    assert callable(build_gateway_channels)
    assert build_gateway_channels.__module__ == "agent.bootstrap"


def test_the_channel_layer_re_exports_it() -> None:
    """Existing importers are unaffected, which is why the name is kept there."""
    from agent.channels.base import _build_gateway_channels
    from agent.bootstrap import build_gateway_channels

    assert _build_gateway_channels({}) == build_gateway_channels({})


def test_only_the_builder_constructs_a_channel() -> None:
    """A second construction site is how "one place decides" stops being true.

    Read from the source rather than probed at runtime, because a construction
    in a branch the tests do not exercise is exactly the case this guards.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel == _BUILDER_MODULE:
            continue
        for name in _call_sites(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}: {name}(...)")
    assert not offenders, (
        "channels constructed outside the composition point:\n  "
        + "\n  ".join(offenders)
    )


def test_the_builder_itself_does_construct_channels() -> None:
    """Control: the guard above is vacuous if the builder is not detected."""
    source = (REPO_ROOT / _BUILDER_MODULE).read_text(encoding="utf-8")
    found = set(_call_sites(source))
    assert {"WebChannel", "FeishuChannel"} <= found, found


def test_the_switch_reads_the_config_section() -> None:
    """Enabled/disabled is config, and an absent section means no channels."""
    from agent.bootstrap import build_gateway_channels

    assert build_gateway_channels({}) == []
    assert build_gateway_channels({"channels": {}}) == []
    assert build_gateway_channels(
        {"channels": {"web": {"enabled": False}, "feishu": {"enabled": False}}}
    ) == []


def test_unknown_channel_keys_do_not_raise() -> None:
    """A stale key in config.json is filtered, not fatal.

    Asserted for the disabled path so it needs no channel dependency; the
    enabled path is covered in the channel test modules.
    """
    from agent.bootstrap import build_gateway_channels

    assert build_gateway_channels(
        {"channels": {"web": {"enabled": False, "no_such_option": 1}}}
    ) == []


def test_the_scheduler_is_not_here_and_that_is_recorded() -> None:
    """Task 8's other half, as an assertion about the *reason* it is not here.

    ``SchedulerService`` needs an ``agent_executor`` built on ``AgentCore``,
    which bootstrap creates late in ``_build_components_async``.  So the
    scheduler cannot be constructed where the rest of composition happens, and
    that is an ordering constraint rather than a scattered decision.  This test
    fails the day that stops being true, so the note cannot quietly go stale.
    """
    from agent import bootstrap

    assert not hasattr(bootstrap, "build_scheduler_service"), (
        "the scheduler now has a composition entry point -- update the plan's "
        "Task 8 note, which says the ordering constraint prevents one"
    )
