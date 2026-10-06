"""Selecting the plugin profile a composition loads under.

The table in :mod:`agent.plugins.profiles` describes compositions; this is how a
composition is *chosen*.  Without it the mechanism would be a table nothing reads
-- the shape the plugin contribution surface had before ``provide`` existed.

Where the name comes from
-------------------------
The ``plugins`` section of the configuration, under a reserved key:

    "plugins": {
        "_profile": "default",
        "evolution": {"enabled": true}
    }

A leading underscore is the project's existing convention for a companion key
that sits beside real entries and is not itself one -- ``_readme`` and
``_example_disabled`` in ``config.example.json``, ``_readme`` beside each
provider.  It is reserved here for the same reason: a profile is a property of the
*section*, not of any plugin in it, and giving it a plugin-shaped name would make
``plugins.my-profile`` look like a plugin.

An unknown name raises
----------------------
At load time, loudly, rather than falling back.  ``resolve_permission_profile``
fails closed to read-only because a permissions typo must not widen authority;
here the risk runs the other way -- a typo would silently load the default set,
and a run under a deliberately narrowed composition would quietly get everything.
That is worth failing startup for.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from agent.plugins.profiles import DEFAULT_PROFILE_NAME, PluginProfile, resolve_profile

__all__ = ["PROFILE_KEY", "profile_name_from_config", "profile_from_config"]

#: Reserved key naming the composition, inside the ``plugins`` section.
PROFILE_KEY = "_profile"


def profile_name_from_config(config: Optional[Mapping[str, Any]]) -> str:
    """The profile name the ``plugins`` section asks for, or the default.

    A blank or non-string value is treated as unstated rather than as an error:
    an empty string is what a form leaves behind, and it means "no choice", not
    "a profile called ''".
    """
    if not config:
        return DEFAULT_PROFILE_NAME
    value = config.get(PROFILE_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_PROFILE_NAME


def profile_from_config(config: Optional[Mapping[str, Any]]) -> PluginProfile:
    """Resolve the composition the ``plugins`` section asks for.

    Raises :class:`~agent.plugins.profiles.KnownProfileError` for a name that does
    not exist -- see the module docstring for why this is not a fallback.
    """
    return resolve_profile(profile_name_from_config(config))
