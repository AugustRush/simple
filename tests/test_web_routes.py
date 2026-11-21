"""HTTP endpoints a plugin contributes.

The web surface was the last kind of contribution that was not a capability
service: `agent/channels/web.py` holds its routes as a literal list, so a plugin
that wanted to answer a request had to edit that file. Measured on the question
that started this -- a stock-screener plugin could be a tool, a skill, a
scheduled job or a self-contained HTML report, and could not be a page with an
endpoint of its own.
"""

from __future__ import annotations

import pytest

from agent.channels.web_routes import WebRouteRegistry


class _Ctx:
    def __init__(self, name: str) -> None:
        self.name = name
        self._undos: list = []

    def on_unwind(self, undo) -> None:
        self._undos.append(undo)

    def unload(self) -> None:
        for undo in reversed(self._undos):
            undo()
        self._undos.clear()


async def _handler(request):
    return None


def test_a_plugin_can_add_a_route_another_cannot_take_over() -> None:
    registry = WebRouteRegistry()
    registry.scoped(_Ctx("screener")).add("GET", "/api/plugin/screen", _handler)

    with pytest.raises(ValueError) as excinfo:
        registry.scoped(_Ctx("other")).add("GET", "/api/plugin/screen", _handler)

    assert "already provided by screener" in str(excinfo.value)


def test_the_apps_own_namespace_is_refused() -> None:
    """Not a style rule: the frontend is written against those paths, and a
    plugin that could replace one would make every frontend bug a question of
    which plugin was loaded."""
    registry = WebRouteRegistry()

    for path in ("/api/home", "/api/sessions", "/screen"):
        with pytest.raises(ValueError, match="/api/plugin/"):
            registry.register("GET", path, _handler)


def test_two_methods_on_one_path_are_two_routes() -> None:
    """A REST-shaped contribution is ordinary, and the key is (method, path)."""
    registry = WebRouteRegistry()
    registry.scoped(_Ctx("a")).add("GET", "/api/plugin/thing", _handler)
    registry.scoped(_Ctx("a")).add("POST", "/api/plugin/thing", _handler)

    assert [(verb, path) for verb, path, _h, _p in registry.routes()] == [
        ("GET", "/api/plugin/thing"),
        ("POST", "/api/plugin/thing"),
    ]


def test_unloading_a_plugin_removes_only_its_own_route() -> None:
    registry = WebRouteRegistry()
    one, two = _Ctx("one"), _Ctx("two")
    registry.scoped(one).add("GET", "/api/plugin/a", _handler)
    registry.scoped(two).add("GET", "/api/plugin/b", _handler)

    one.unload()

    assert [path for _v, path, _h, _p in registry.routes()] == ["/api/plugin/b"]


def test_a_route_is_private_unless_it_says_otherwise() -> None:
    """The default is the one an operator can rely on without reading the plugin."""
    registry = WebRouteRegistry()
    registry.scoped(_Ctx("a")).add("GET", "/api/plugin/a", _handler)

    (_verb, _path, _handler_, public) = registry.routes()[0]

    assert public is False


def test_contributed_routes_are_mounted_after_the_built_ins() -> None:
    """Mounting is done in `bind_runtime`, because the app is built in `__init__`
    before the components arrive -- measured: mounting at build time found no
    registry, and the plugin's route was silently missing while the plugin itself
    reported `active`."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "channels" / "web.py"
    ).read_text(encoding="utf-8")

    assert "self._mount_contributed_routes()" in source
    bind = source[source.index("def bind_runtime(") :][:1200]
    assert "_mount_contributed_routes()" in bind, (
        "the mount must happen where the components exist"
    )
    build = source[source.index("def _build_app(") :][:6000]
    assert "routes.extend(self._contributed_routes(" not in build, (
        "mounting at build time is the bug: components are empty there"
    )


def test_unloading_a_replacement_restores_the_auth_setting_too() -> None:
    """Measured with a two-plugin repro: B replaces A's *private* route with a
    public one, then unloads. The handler went back to A's, the flag did not --
    so A's endpoint stayed reachable without the auth check it was declared with.
    The public flag is part of what a replacement replaces.
    """
    registry = WebRouteRegistry()
    a, b = _Ctx("a"), _Ctx("b")

    async def private_handler(request):
        return None

    async def public_handler(request):
        return None

    registry.scoped(a).add("GET", "/api/plugin/x", private_handler)
    registry.scoped(b).add(
        "GET", "/api/plugin/x", public_handler, replace=True, public=True
    )
    assert registry.routes()[0][2] is public_handler

    b.unload()

    _verb, _path, handler, public = registry.routes()[0]
    assert handler is private_handler
    assert public is False, "A's route must not come back published"


# ── regressions found by review ─────────────────────────────────────────────


def test_a_replacement_chain_does_not_resurrect_an_unloaded_plugin() -> None:
    """A registers, B replaces, A unloads first, then B unloads.

    B's undo would put A's handler back -- from a plugin that is gone, and
    publicly if A had said so. Measured with a two-plugin repro.
    """
    registry = WebRouteRegistry()
    a, b = _Ctx("a"), _Ctx("b")

    async def handler_a(request):
        return None

    async def handler_b(request):
        return None

    registry.scoped(a).add("GET", "/api/plugin/x", handler_a)
    registry.scoped(b).add("GET", "/api/plugin/x", handler_b, replace=True)

    a.unload()
    b.unload()

    assert registry.routes() == []


def test_the_whole_api_plugin_namespace_belongs_to_plugins() -> None:
    """`/api/plugin/pages` used to be a built-in route, so a plugin registering
    it was silently shadowed. The built-ins moved to `/api/pages`; the promise
    that this prefix is the plugin's own is true again.
    """
    registry = WebRouteRegistry()

    # The path that used to be reserved is registrable again.
    registry.register("GET", "/api/plugin/pages", _handler)
    assert [(verb, path) for verb, path, _h, _p in registry.routes()] == [
        ("GET", "/api/plugin/pages")
    ]


def test_the_built_in_page_routes_are_not_in_the_plugin_namespace() -> None:
    """Otherwise "this prefix is yours" is a promise the app breaks itself."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "channels" / "web.py"
    ).read_text(encoding="utf-8")

    assert 'Route("/api/pages", self._plugin_page_list' in source
    assert '"/api/pages/{page_id}"' in source
    assert '"/api/plugin/pages' not in source, (
        "a built-in route is back inside the namespace plugins register under"
    )


def test_a_path_that_starlette_cannot_compile_is_refused_at_registration() -> None:
    """A typo used to surface inside `bind_runtime`, where it takes the whole
    web channel down instead of failing that one plugin."""
    registry = WebRouteRegistry()

    for path in ("/api/plugin/{x}/{x}", "/api/plugin/{x:unknown}"):
        with pytest.raises(ValueError, match="not a valid path"):
            registry.register("GET", path, _handler)


def test_public_must_be_a_real_boolean() -> None:
    """`bool("false")` is True: a plugin reading the flag out of JSON config
    would have published a route it meant to keep private."""
    registry = WebRouteRegistry()

    for value in ("false", 0, 1, None):
        with pytest.raises(ValueError, match="must be True or False"):
            registry.register("GET", "/api/plugin/x", _handler, public=value)


def test_remounting_replaces_the_previous_routes_instead_of_appending() -> None:
    """The mount used to be a one-time snapshot: an unloaded plugin's endpoint
    kept answering, and a reload appended a second Route for the same path that
    the first, stale one shadowed."""
    import types

    from agent.channels import web as web_module

    async def first(request):
        return None

    async def second(request):
        return None

    channel = object.__new__(web_module.WebChannel)
    channel._app = types.SimpleNamespace(routes=["builtin"])
    channel._contributed_route_objects = []
    channel._components = {}

    state = {"handler": first}
    channel._contributed_routes = types.MethodType(
        lambda self, route_class: [("route", state["handler"])]
        if state["handler"]
        else [],
        channel,
    )
    channel._mount_contributed_routes()
    assert channel._app.routes == ["builtin", ("route", first)]

    # Simulate the plugin being unloaded: the registry now has nothing.
    state["handler"] = None
    channel._mount_contributed_routes()
    assert channel._app.routes == ["builtin"], "the stale route was not removed"

    state["handler"] = second
    channel._mount_contributed_routes()
    assert channel._app.routes == ["builtin", ("route", second)]


def test_each_composition_gets_its_own_registry() -> None:
    """Two compositions in one process must not share contributions.

    `bootstrap.channels_only()` builds a second PluginHost with the same core
    plugins, so a process-wide singleton meant a route one composition's plugin
    added would answer in the other's channel. Same rule (and same measured
    reason) as `DeliveryTargetsPlugin` and `SystemJobsPlugin`.
    """
    from agent.channels.web_routes_plugin import WebRoutesPlugin
    from agent.kernel.plugin import PluginHost

    first, second = PluginHost(), PluginHost()
    first.load(WebRoutesPlugin())
    second.load(WebRoutesPlugin())

    one = first.services.get("web_routes")
    two = second.services.get("web_routes")

    assert one is not None and two is not None
    assert one is not two, "two compositions shared one registry"

    one.register("GET", "/api/plugin/only-in-one", _handler)
    assert one.routes()
    assert two.routes() == []
