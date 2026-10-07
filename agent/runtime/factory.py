"""Building a runtime for a config, as a service rather than an import.

The composition root builds components; that has always been true.  What was missing is
a **name** for it that something inside a composition can ask for.  Six call sites reach
``agent._build_components_async`` directly, and five of them are the root building
itself -- the sixth is a scheduled run, which is not the root and should not have to
import it.

That sixth one is why this exists.  A scheduled run may find that the config on disk has
changed since the process started (a key, a base_url, the headers a gateway demands),
and then it needs a runtime built from *that* config -- a fresh composition, per run.  A
plugin doing that by importing the composition root is a plugin reaching behind the
container it was given, which is the coupling the kernel exists to remove.

Why a method may be async when ``setup`` may not
------------------------------------------------
The kernel calls a plugin's ``setup`` synchronously, so an ``async def setup`` is refused
-- see :mod:`agent.kernel.plugin`.  That constraint is on the *lifecycle*, not on the
service: ``build`` here is a coroutine because building a runtime genuinely is one, and
the caller awaits it in its own time.  A service whose methods are asynchronous is
ordinary; a lifecycle that is asynchronous would change every caller of ``load``.

The default is the composition root
----------------------------------
Constructed with no arguments it delegates to ``agent._build_components_async``, so the
service is a *name* for the existing behaviour rather than a second implementation of
it.  A composition that wants a different one substitutes it at ``runtime_factory`` --
which is the point, and which the import made impossible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["RuntimeFactory"]


class RuntimeFactory:
    """Builds a component mapping from a config."""

    __slots__ = ("_builder", "_closer")

    def __init__(self, builder: Any = None, closer: Any = None) -> None:
        #: Held rather than looked up per call, so a test or a profile can substitute the
        #: builder without patching a module global.
        self._builder = builder
        self._closer = closer

    async def build(
        self,
        cfg: dict,
        *,
        announce: bool = True,
        resource_home: Path | None = None,
    ) -> dict:
        """The components *cfg* describes.

        The keyword arguments are the ones the six existing call sites actually vary:
        ``announce`` because a run's build must not reprint the session header,
        ``resource_home`` because a gateway's resource directories are global to its
        sessions.  Naming them here is what lets a caller inside a composition ask for a
        runtime without also knowing the signature of the root.
        """
        builder = self._builder
        if builder is None:
            # Imported per call: ``agent/__init__`` imports the runtime package.
            from agent import _build_components_async

            builder = _build_components_async
        return await builder(cfg, announce=announce, resource_home=resource_home)

    async def close(self, components: dict) -> None:
        """Release what :meth:`build` returned.

        On the factory because a runtime built by one thing and closed by another is the
        arrangement that produces a leak when the two disagree -- and because a caller
        that was given the ability to build should not have to import the ability to
        close.
        """
        closer = self._closer
        if closer is None:
            from agent import _close_components

            closer = _close_components
        await closer(components)
