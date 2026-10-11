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

Where the skills come from
--------------------------
Three layers, in this order: the builtin root, this agent's own user root, then the
**shared** roots other ecosystems keep (``~/.codex/skills``, ``~/.agents/skills``,
``~/.dsh/skills`` -- declared by the format adapters, because the convention is
theirs).  The shared layer is a fallback: a skill installed here is not shadowed by
one found in another tool's directory, and the ecosystem's own bundled subtree
(Codex's ``.system``) is skipped, because those are its instructions, not the
user's.

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

        from agent.plugins import formats

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
        # Skills the user installed for another agent are skills the user has.  Which
        # directories those are is the *ecosystem's* convention, so it comes from the
        # format adapters rather than from a list here; and the load is a fallback
        # layer, so a skill installed in this agent's own root wins a name collision.
        for skill_root in formats.shared_skill_roots():
            catalog._load_root(
                Path(skill_root.path),
                source=f"shared:{skill_root.label}",
                keep_existing=True,
                skip_dirs=(skill_root.system_dir,) if skill_root.system_dir else (),
            )
        catalog.register_tools(ctx.get("registry"))
        ctx.provide("skill_catalog", catalog)
