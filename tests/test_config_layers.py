"""Layers are named, ordered, and applied by the one applier.

Layering already happened -- defaults, then the file, then a bundle, then a profile -- but
through **two** mechanisms.  The scheduler's run config was the clearest case:

```python
run_cfg = dict(current_cfg)                  # base
run_cfg["workspace_root"] = str(workspace)   # a raw dict write
run_cfg["output_dir"] = str(run_output)      # a raw dict write
run_cfg = apply_profile_to_config(run_cfg, profile)   # a patch, validated
```

The two raw writes are layers too -- just layers `apply_patch` never saw, so they were not
validated against the declared paths, not undoable, and not attributable.
"""

from __future__ import annotations

import pytest

from agent.config_layers import ConfigLayer, resolve_layers
from agent.config_patch import UnknownConfigPath, entry


def test_later_layers_win() -> None:
    """The only rule an ordered list needs."""
    base = {"a": 1, "b": {"c": 2}}

    result = resolve_layers(
        base,
        [
            ConfigLayer("first", (entry("a", 10),)),
            ConfigLayer("second", (entry("a", 20),)),
        ],
    )

    assert result["a"] == 20
    assert base["a"] == 1, "the caller's configuration is input, not scratch space"


def test_a_layer_is_validated_like_any_other_patch() -> None:
    """The point of routing the raw writes through the applier.

    A typo used to become a setting nobody reads.  Now it is an error that names the path --
    and, because the layer has a name, the caller has somewhere to look.
    """
    with pytest.raises(UnknownConfigPath):
        resolve_layers({}, [ConfigLayer("typo", (entry("nope.not_here", 1),))])


def test_a_layer_that_legitimately_adds_a_path_can_say_so() -> None:
    """`allow_new` is the opt-in, and it is per entry rather than per layer.

    A run legitimately adds `workspace_root` and `output_dir`, which the defaults do not
    declare.  Making that a property of the *entry* keeps the strict rule for every other
    patch in the same layer.
    """
    result = resolve_layers(
        {},
        [
            ConfigLayer(
                "run paths",
                (
                    entry("workspace_root", "/tmp/w", allow_new=True),
                    entry("output_dir", "/tmp/o", allow_new=True),
                ),
            )
        ],
    )

    assert result == {"workspace_root": "/tmp/w", "output_dir": "/tmp/o"}


def test_an_empty_layer_changes_nothing() -> None:
    """Control: a named layer with no patches is legal and inert."""
    base = {"a": 1}
    assert resolve_layers(base, [ConfigLayer("nothing")]) == base


def test_the_scheduler_names_its_layers() -> None:
    """Asserted on the source, because the failure mode is the raw writes coming back.

    They would work, and would be invisible as layers -- which is exactly how they got there.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent/scheduler/build.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "resolve_layers" in source, "the scheduler must go through the layer applier"

    raw_writes = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == "run_cfg"
    ]
    assert not raw_writes, (
        "these write into the run config outside the patch applier, so they are layers no "
        f"applier saw: lines {raw_writes}"
    )
