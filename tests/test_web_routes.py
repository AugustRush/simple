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
