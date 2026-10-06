"""Overriding configuration by path, so a composition can be named.

Why this exists
---------------
A "bundle" is a named set of configuration overrides: ``headless`` might turn the
web channel off and shorten the scheduler lease; a test bundle might point the
data directory somewhere temporary.  Expressing that as a table of
``path -> value`` pairs is what makes it *declarative* -- the alternative is a
branch somewhere that asks which composition this is, which is the shape
``agent/scheduler/profiles.py`` was written to replace ("adding a profile meant
adding a branch, and two places to keep in sync").

The precedence that makes this safe
-----------------------------------
A patch never invents a key.  It overrides one that the default configuration
already declares, or it fails.  This is the rule that keeps a typo from becoming a
silently-accepted setting: ``orchestration.max_parallel_agent`` (missing ``s``)
would otherwise sit in the file looking authoritative while the real setting kept
its default.  ``allow_new`` exists for a caller that genuinely owns a key -- a
test bundle writing ``storage.path`` -- and has to say so.

Deep, not shallow, for mappings
-------------------------------
``{"memory": {"tidy_interval_seconds": 60}}`` must not erase the rest of the
``memory`` section.  Shallow replacement of a section is how a patch silently
drops every sibling setting, and it is invisible without reading the merged
result.

``None`` means remove
---------------------
Because JSON has a null and the natural way to say "drop this key" in a data file
is to set it to null.  A caller that genuinely wants ``None`` as a value has no
use for that in this configuration -- every documented key here is a string,
number, boolean, list or mapping.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping, Optional

__all__ = [
    "PatchEntry",
    "UnknownConfigPath",
    "deep_merge",
    "apply_patch",
    "entry",
]


class UnknownConfigPath(KeyError):
    """Raised when a patch names a path the defaults do not declare."""

    def __init__(self, path: str, root: Mapping[str, Any]) -> None:
        top = path.split(".", 1)[0]
        near = sorted(
            k for k in root if isinstance(k, str) and k.split(".")[0].startswith(top[:4])
        )[:8]
        super().__init__(
            f"config path {path!r} is not declared by the defaults "
            f"(nearby top-level sections: {', '.join(near) or 'none'})"
        )
        self.path = path


@dataclass(frozen=True)
class PatchEntry:
    """One override: a dotted path and the value to set, or ``None`` to remove."""

    path: str
    value: Any = None
    #: Permit a path the defaults do not declare.  Off by default so a typo
    #: fails instead of becoming a setting nobody reads.
    allow_new: bool = False


def entry(path: str, value: Any = None, *, allow_new: bool = False) -> PatchEntry:
    """Shorthand, so a table of patches reads as data."""
    return PatchEntry(path=path, value=value, allow_new=allow_new)


def _path_exists(root: Mapping[str, Any], path: str) -> bool:
    node: Any = root
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True


def _set_path(root: MutableMapping[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: Any = root
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, MutableMapping):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _remove_path(root: MutableMapping[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = root
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, MutableMapping):
            return  # already absent: removal is idempotent
        node = nxt
    node.pop(parts[-1], None)


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """*overlay* on top of *base*, recursing into mappings.

    Lists are replaced wholesale rather than concatenated: every list in this
    configuration is a decision ("these channels", "these allowed commands"), and
    appending to an operator's list from a patch would make the result depend on
    declaration order.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def apply_patch(
    base: Mapping[str, Any],
    entries: Iterable[PatchEntry],
) -> dict[str, Any]:
    """*base* with every entry applied, in order.

    Returns a new mapping: the caller's configuration is input, not scratch
    space, and a bundle that mutated it would change what every later reader
    sees -- including the file on disk if someone writes that object back.

    The copy is deep.  A shallow one is not enough and the failure is quiet:
    ``_set_path`` writes into nested mappings, and a shallow copy shares those
    with the original, so applying a patch would edit the caller's configuration
    while returning something that looks like a fresh object.
    """
    result: dict[str, Any] = copy.deepcopy(dict(base))
    for item in entries:
        if item.value is None:
            # A declared path may legitimately be removed; an undeclared one is
            # still a typo, so the existence check applies to removals too.
            if not item.allow_new and not _path_exists(result, item.path):
                raise UnknownConfigPath(item.path, base)
            _remove_path(result, item.path)
            continue
        if not item.allow_new and not _path_exists(result, item.path):
            raise UnknownConfigPath(item.path, base)
        _set_path(result, item.path, item.value)
    return result
