"""The skill catalog as a plugin: it provides ``skill_catalog``.

Why a plugin
------------
The catalog is read while plugins load -- to decide which skills a turn may use --
and it registers the skill-management tools on the registry, so it had to exist
before the catalog that discovers plugins.  Nothing about it *needs* to: it takes
a user root and the ``skills`` section of the config, both of which are values,
and it contributes tools to a service that another plugin provides.  Declaring
``registry`` in ``inject`` is what makes the ordering the kernel's problem rather
than the composition's: this plugin activates when the registry appears, wherever
in the declaration that happens.

What the composition keeps
--------------------------
Loading the skills a *plugin* bundled.  That happens after the plugins are
discovered -- the paths come from the loader -- and it is the same act that
``plugin reload`` performs, so it stays next to the loader rather than inside a
plugin that would have to be told about reloading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["SkillCatalogPlugin"]


class SkillCatalogPlugin:
    """Provides ``skill_catalog``."""

    name = "skill_catalog"

    inject = ("cfg", "registry", "user_skills_dir")

    def setup(self, ctx: Any) -> None:
        import agent as agent_module

        # Read through the package attribute so a substitution reaches it, and take
        # the config section as the catalog wants it: a non-dict section reads as
        # "no switches" rather than raising, because a hand-edited config must not
        # stop the agent from loading.
        # ``Path`` rather than whatever arrived: the value is a *path service*, and a
        # caller that hands over a string (a profile, a test) must not get a catalog
        # that fails on its first directory scan.
        catalog = agent_module.SkillCatalog(
            user_root=Path(str(ctx.get("user_skills_dir"))),
            skill_config=(ctx.get("cfg", None) or {}).get("skills"),
        )
        catalog.load_all()
        catalog.register_tools(ctx.get("registry"))
        ctx.provide("skill_catalog", catalog)
