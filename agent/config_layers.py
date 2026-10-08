"""Named, ordered configuration layers, applied by the one patch applier.

Why this exists
---------------
Layering already happened -- defaults, then the file, then a bundle, then a profile -- but
through **two** mechanisms.  The scheduler's run config is the clearest case:

```python
run_cfg = dict(current_cfg)                  # base
run_cfg["workspace_root"] = str(workspace)   # a raw dict write
run_cfg["output_dir"] = str(run_output)      # a raw dict write
run_cfg = apply_profile_to_config(run_cfg, profile)   # a patch, validated
```

The two raw writes are layers too.  They are just layers that
:func:`agent.config_patch.apply_patch` never saw, so they are not validated against the
declared paths, not undoable, and not attributable -- "which layer set `output_dir`?" has no
answer, and a typo in one of them becomes a setting nobody reads instead of an error.

This module is not a new mechanism.  It is the existing one with the layers **named and
ordered**, so a caller says what it is doing rather than interleaving two ways of doing it.

What it deliberately does not do
--------------------------------
It does not decide the layers for a caller, and it does not carry a "the file", "the CLI"
vocabulary: those names belong to whoever assembles them.  `resolve_layers` applies what it is
given, in the order given, and later layers win -- which is the only rule an ordered list
needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from agent.config_patch import PatchEntry, apply_patch

__all__ = ["ConfigLayer", "resolve_layers"]


@dataclass(frozen=True)
class ConfigLayer:
    """One named layer: where it came from, and what it changes."""

    #: For an error message and a diagnostic.  "the acme bundle" beats "layer 3".
    name: str
    patches: tuple[PatchEntry, ...] = ()

    def apply(self, base: Mapping[str, Any]) -> dict[str, Any]:
        return apply_patch(base, self.patches)


def resolve_layers(
    base: Mapping[str, Any], layers: Iterable[ConfigLayer]
) -> dict[str, Any]:
    """*base* with every layer applied in order.

    Returns a new mapping, because the caller's configuration is input rather than scratch
    space -- a layer that mutated it would change what every later reader sees.

    Applied through :func:`agent.config_patch.apply_patch` rather than by writing into the
    copy, so each layer gets the same existence check, the same ``allow_new`` opt-in, and the
    same failure for a path nobody declares.
    """
    result: dict[str, Any] = dict(base)
    for layer in layers:
        result = layer.apply(result)
    return result
