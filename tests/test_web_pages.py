"""Whole pages a plugin contributes, and the sandbox that makes them safe.

Two ways to let a plugin draw a page: render its schema with our components, or
host its HTML in a frame. The first makes safety a property of every field
mapping -- one that lets a `javascript:` URL through is a cross-site script. The
second makes it a property of the browser. These tests pin the second, because
the failure mode of a sandbox is that somebody removes one attribute.
"""

from __future__ import annotations

import pytest

from agent.channels.web_pages import WebPageRegistry


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


def test_a_plugin_can_declare_a_page_and_get_it_back() -> None:
    registry = WebPageRegistry()
    registry.scoped(_Ctx("screener")).add(
        "screener", title="选股", html="<h1>hi</h1>", description="按条件筛股"
    )

    page = registry.page("screener")
    assert page is not None
    assert page["title"] == "选股"
    assert page["html"] == "<h1>hi</h1>"

    (listed,) = registry.listing()
    assert listed == {"id": "screener", "title": "选股", "description": "按条件筛股"}
    assert "html" not in listed, "the navigation asks for names, never bodies"


def test_two_plugins_cannot_share_a_page_id() -> None:
    registry = WebPageRegistry()
    registry.scoped(_Ctx("a")).add("x", title="A", html="a")

    with pytest.raises(ValueError, match="already provided by a"):
        registry.scoped(_Ctx("b")).add("x", title="B", html="b")


def test_an_id_that_is_not_a_url_segment_is_refused() -> None:
    registry = WebPageRegistry()
    for bad in ("a/b", "a b", "", "a?b"):
        with pytest.raises(ValueError):
            registry.register(bad, title="t", html="h")


def test_a_page_needs_a_title_and_some_html() -> None:
    registry = WebPageRegistry()
    with pytest.raises(ValueError, match="title"):
        registry.register("x", title="  ", html="h")
    with pytest.raises(ValueError, match="html"):
        registry.register("x", title="t", html=123)


def test_unloading_a_plugin_removes_only_its_own_page() -> None:
    registry = WebPageRegistry()
    one, two = _Ctx("one"), _Ctx("two")
    registry.scoped(one).add("a", title="A", html="a")
    registry.scoped(two).add("b", title="B", html="b")

    one.unload()

    assert [entry["id"] for entry in registry.listing()] == ["b"]


def test_the_server_serves_a_page_with_a_sandbox_header() -> None:
    """A person who opens the URL directly must get a sandboxed document too.

    The frame attribute protects the app; this header protects the same URL when
    it is fetched outside the frame -- the case where a page could otherwise read
    the app's cookies with the app's own origin.
    """
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "channels" / "web.py"
    ).read_text(encoding="utf-8")
    start = source.index("async def _plugin_page(")
    body = source[start : start + 4000]

    assert "Content-Security-Policy" in body
    assert "sandbox allow-scripts allow-forms" in body
    assert "no-store" in body


def test_the_frame_never_gets_allow_same_origin() -> None:
    """With `allow-same-origin` plus `allow-scripts` the page can remove its own
    sandbox attribute. That pair is not a weaker sandbox, it is no sandbox --
    and "just for a moment, so it can read localStorage" is how it returns."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "views"
        / "plugin_page.tsx"
    ).read_text(encoding="utf-8")

    assert 'sandbox="allow-scripts allow-forms"' in source
    assert "allow-same-origin" not in source.split("sandbox=")[1].split(">")[0], (
        "the sandbox attribute must not carry allow-same-origin"
    )


def test_the_theme_travels_in_the_url_not_a_channel() -> None:
    """Reading which theme is on needs one direction; a two-way channel is a
    second API that nothing in this step requires."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "views"
        / "plugin_page.tsx"
    ).read_text(encoding="utf-8")

    assert "?theme=" in source
    # Asserted on the code, not the file: the docstring explains why there is no
    # postMessage channel, so a file-wide scan would fail on its own explanation.
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("*", "/*", "//"))
    )
    assert "postMessage" not in code


def test_a_page_replacement_chain_does_not_resurrect_an_unloaded_plugin() -> None:
    """Same rule as contributed routes: a page only comes back while the plugin
    that declared it is still loaded."""
    registry = WebPageRegistry()
    a, b = _Ctx("a"), _Ctx("b")

    registry.scoped(a).add("x", title="A", html="a")
    registry.scoped(b).add("x", title="B", html="b", replace=True)

    a.unload()
    b.unload()

    assert registry.listing() == []


def test_a_page_renderer_that_returns_bytes_renders_the_html_not_a_repr() -> None:
    """`HTMLResponse(str(body))` on bytes yields `b'<h1>…'` -- the page showed a
    Python repr. Decoded instead; a non-text return is a plugin bug and gets an
    error rather than a blank 200."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "channels" / "web.py"
    ).read_text(encoding="utf-8")
    start = source.index("async def _plugin_page(")
    body = source[start : start + 4000]

    assert 'isinstance(body, (bytes, bytearray))' in body
    assert 'decode("utf-8", "replace")' in body
    assert "isinstance(body, str)" in body
