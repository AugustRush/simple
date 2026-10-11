"""Which plugins load, declared rather than inferred.

The problem this replaces
-------------------------
"What loads" lived in one place, ``config.json``'s ``plugins`` section, read by
one method that understood exactly one key:

    pcfg = self._plugin_config.get(name, {})
    return pcfg.get("enabled", True)

That is not wrong, it is *unstated*.  The default is load-everything, the section
is per-plugin rather than per-composition, and the only way to know what a given
run will load is to read the file it happened to be handed.  The web channel even
reaches in and replaces ``catalog._plugin_config`` wholesale, so the answer can
change while the process runs.

The shape, borrowed from ``PERMISSION_PROFILES``
------------------------------------------------
``agent/scheduler/profiles.py`` already solved this problem for permissions: a
frozen dataclass per row, a table keyed by name, an explicit fail-closed fallback,
and a ``to_payload()`` so the UI never hardcodes the list.  This is the same shape
for plugins, which is why it is a table and a resolver rather than new logic in
the catalog:

- :class:`PluginEntry` -- one plugin's intent: enabled, disabled, or overridden
  with config;
- :data:`DEFAULT_PROFILE` -- what a composition gets when it names nothing, which
  is what happens today;
- :class:`PluginProfile` -- a named composition: a list of entries that override
  the default.

Additive on purpose
-------------------
A profile overrides; it does not replace.  Two reasons, both concrete: a profile
that omitted a plugin would be indistinguishable from one that disabled it, and
disabling is a thing this project genuinely wants (the ``_example_disabled`` key in
``config.example.json`` exists for it).  Overriding keeps the file's existing
meaning while making a composition nameable.

Disabled wins over enabled
--------------------------
When a plugin is turned off in the configuration, no profile re-enables it.  A
profile is a composition *within* what is permitted, not a way around an operator's
decision, and the alternative -- last write wins -- would make "off" depend on load
order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

__all__ = [
    "DEFAULT_PROFILE_NAME",
    "DEFAULT_PROFILE",
    "PluginEntry",
    "PluginProfile",
    "KnownProfileError",
    "resolve_plugin_state",
    "profile_names",
]

#: The profile a composition gets when it names none.
DEFAULT_PROFILE_NAME = "default"

#: The key the configuration uses for the plugins section, named once.
PLUGINS_KEY = "plugins"


class KnownProfileError(ValueError):
    """Raised for a profile *name* that does not exist.

    Deliberately not a fallback.  ``resolve_permission_profile`` fails closed to
    ``read_only`` because a permission typo must not widen authority; here a
    typo would silently load the default set, and "I asked for a profile and got
    a different one" is a configuration bug that should be loud.
    """

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        super().__init__(
            f"unknown plugin profile {name!r}; known: {', '.join(known) or '(none)'}"
        )
        self.name = name
        self.known = known


@dataclass(frozen=True)
class PluginEntry:
    """One plugin's intended state within a composition."""

    plugin: str
    enabled: bool = True
    config: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Public shape, so a UI never hardcodes the fields."""
        return {
            "plugin": self.plugin,
            "enabled": self.enabled,
            "config": dict(self.config),
        }


@dataclass(frozen=True)
class PluginProfile:
    """A named composition: which plugins load, and with what configuration."""

    key: str
    label: str
    summary: str
    entries: tuple[PluginEntry, ...] = ()

    #: Which of the composition's **own** plugins this profile includes, or ``None`` for all.
    #:
    #: The entries above decide the plugins found on disk; this decides the ones the
    #: composition root mounts itself.  They were five literal ``mount`` calls in
    #: ``bootstrap``, so "this deployment has no memory plugin" was a question a profile
    #: could not ask -- it could narrow what was discovered and not touch what was built.
    #:
    #: ``None`` is the safe default: every existing composition has all of them, and a
    #: default that silently dropped one would be a behaviour change nobody asked for.
    core: tuple[str, ...] | None = None

    #: Service name -> the plugin that provides it instead of the built-in one.
    #:
    #: This is the half `core` cannot express.  `core` says *which* of the composition's own
    #: plugins are present; this says that a particular **service** comes from somewhere
    #: else.  A deployment shipping its own memory plugin needs the second: "include memory"
    #: and "memory comes from acme" are different statements.
    #:
    #: The mechanisms already existed -- a plugin of the same name replaces via
    #: ``load(replace=True)``, and dependents are restored afterwards -- so this is the
    #: declaration, not a second implementation of replacement.
    service_replacements: Mapping[str, str] = field(default_factory=dict)

    @property
    def plugins(self) -> tuple[str, ...]:
        """The plugins this profile turns on, in declaration order."""
        return tuple(entry.plugin for entry in self.entries if entry.enabled)

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "summary": self.summary,
            "plugins": list(self.plugins),
            "core": list(self.core) if self.core is not None else None,
            "service_replacements": dict(self.service_replacements),
        }


#: The profile that swaps the built-in memory store for the SQLite FTS5 backend.
#: Its key is the plugin's name, so the declaration reads as "the service comes
#: from that plugin" without a second vocabulary.
SQLITE_FTS_PROFILE_NAME = "sqlite-fts-memory"

#: The profile that swaps the whole session service: a `ContextService` that records
#: and retrieves but never consolidates.  The store stays the built-in one, which is
#: the point -- the two layers are replaceable independently.
RECORD_ONLY_CONTEXT_PROFILE_NAME = "record-only-context"

#: The named compositions.  Three rows, and each one is the evidence the first
#: comment asked for: a profile is data, so a different backend (store *or* service)
#: is a table edit plus the plugin it names.
PROFILES: dict[str, PluginProfile] = {
    DEFAULT_PROFILE_NAME: PluginProfile(
        key=DEFAULT_PROFILE_NAME,
        label="默认",
        summary="加载所有未被配置禁用的插件。",
        entries=(),
    ),
    RECORD_ONLY_CONTEXT_PROFILE_NAME: PluginProfile(
        key=RECORD_ONLY_CONTEXT_PROFILE_NAME,
        label="只记录不整理的记忆",
        summary=(
            "用 record-only-context 插件提供的会话服务替换 context_manager："
            "对话与检索照常，整理与压缩不做（同一份契约测试）。"
        ),
        entries=(),
        service_replacements={
            "context_manager": RECORD_ONLY_CONTEXT_PROFILE_NAME,
        },
    ),
    SQLITE_FTS_PROFILE_NAME: PluginProfile(
        key=SQLITE_FTS_PROFILE_NAME,
        label="SQLite FTS5 记忆",
        summary=(
            "用 sqlite-fts-memory 插件提供的存储替换内置 memory/ltm_store；"
            "检索、整理、压缩仍由本项目提供，跑的是同一份契约测试。"
        ),
        entries=(),
        service_replacements={
            "ltm_store": SQLITE_FTS_PROFILE_NAME,
            "memory": SQLITE_FTS_PROFILE_NAME,
        },
    ),
}

#: What a composition gets when it names no profile.  Same object as the single
#: table row so "the default" cannot drift from "the profile called default".
DEFAULT_PROFILE = PROFILES[DEFAULT_PROFILE_NAME]


def profile_names() -> tuple[str, ...]:
    """Known profile names, in declaration order."""
    return tuple(PROFILES)


def _configured_entry(
    config: Optional[Mapping[str, Any]], plugin: str
) -> Any:
    """The configuration entry for *plugin*, ignoring reserved companions.

    A key starting with ``_`` is a companion to the section rather than a plugin
    in it -- ``_profile`` names the composition, ``_readme`` documents the shape.
    Looking one up as a plugin would be harmless today and wrong the moment a
    plugin is *named* ``_profile``, which the convention exists to prevent.
    """
    if not config or plugin.startswith("_"):
        return None
    return config.get(plugin)


def _entry_enabled(raw: Any) -> Optional[bool]:
    """The ``enabled`` flag from one configuration entry, or None if unstated."""
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("enabled")
    return value if isinstance(value, bool) else None


def resolve_plugin_state(
    plugin: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
    profile: Optional[PluginProfile] = None,
) -> PluginEntry:
    """The state one plugin should load in.

    ``config`` is the ``plugins`` section of the configuration; ``profile`` is the
    composition.  Both are optional, and their absence is meaningful rather than
    an error: a composition with neither loads everything, which is what the code
    did before this module existed and is therefore the only safe default.

    Disabled always wins -- see the module docstring.
    """
    active = profile if profile is not None else DEFAULT_PROFILE

    configured = _configured_entry(config, plugin)
    if _entry_enabled(configured) is False:
        # An operator turned this off.  A profile is a composition within what is
        # permitted, not a way around that.
        return PluginEntry(plugin=plugin, enabled=False)

    declared = next(
        (entry for entry in active.entries if entry.plugin == plugin), None
    )
    if declared is not None:
        merged = dict(declared.config)
        # Explicit configuration outranks a profile default: the operator's file
        # is the more specific statement about this installation.
        if isinstance(configured, Mapping):
            merged.update(
                {k: v for k, v in configured.items() if k != "enabled"}
            )
        return PluginEntry(
            plugin=plugin, enabled=declared.enabled, config=merged
        )

    # Not named by the profile and not disabled: keep the historical behaviour.
    inherited = (
        {k: v for k, v in configured.items() if k != "enabled"}
        if isinstance(configured, Mapping)
        else {}
    )
    return PluginEntry(plugin=plugin, enabled=True, config=inherited)


def resolve_profile(name: str) -> PluginProfile:
    """The profile called *name*, or raise.

    See :class:`KnownProfileError` for why this does not fall back.
    """
    try:
        return PROFILES[name]
    except KeyError:
        raise KnownProfileError(name, profile_names()) from None
