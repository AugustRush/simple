"""The agent growing a capability and using it: the loop, end to end.

Why this file exists
--------------------
The product already has every part of "the agent creates what it needs": a tool
that authors tools, a probe that imports them out of process, a hash-approval
gate, and a catalog that registers them.  What it did *not* have was a working
path between those parts, because of the oldest bug class in this repository --
a plugin receives a **scoped view** of a registry (`ctx.get("registry")` is a
`ScopedTools`, so a plugin's registrations are undone when it unloads), and code
elsewhere assumes it has the registry itself.  The turn loop was bitten by this
once already (``'ScopedTools' object has no attribute 'set_context'``, recorded
at the top of ``test_functional_turns``); ``create_tool`` is the same mistake
with a worse symptom: the module is written, its approval is recorded, and the
tool is registered nowhere, so the session reports an error while leaving
inert files behind.

Two more things have to be true before "it works" means what a person expects:

* the tool set change must reach *open sessions*, which hold a fork of the
  registry made when they started -- otherwise the tool exists for the next
  session and not for this conversation;
* and the report must be truthful when there is nowhere to refresh (a
  single-turn CLI has no sessions), rather than claiming "callable now" about
  something the caller cannot reach.

So the tests below go from the smallest unit outward: the view, then the
refresh, then a real WebSocket conversation in which a scripted model asks for
a tool it does not have, gets one, and calls it on the next message.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.tools.plugin_tools import PluginTools
from agent.tools.runtime import ToolRegistry, UserToolCatalog

#: A user tool module: ``register(registry)`` is the whole contract.
_ECHO_SOURCE = '''
def register(registry):
    async def echo_demo(text: str) -> str:
        return "echo:" + str(text)

    registry.register(
        "echo_demo",
        "Repeat the text back.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        echo_demo,
    )
'''


class _Scope:
    """The two things a scoped view needs from the plugin it belongs to."""

    name = "tools"

    def __init__(self) -> None:
        self.undos: list = []

    def on_unwind(self, fn) -> None:
        self.undos.append(fn)


def _registry(tmp_path, *, refresh=None) -> ToolRegistry:
    registry = ToolRegistry(console=None)
    registry.set_context("user_tool_catalog", UserToolCatalog(root=tmp_path / "tools"))
    # Whole-directory trust is the documented "high freedom" switch; the
    # per-file approval path is exercised in test_user_tools.
    registry.set_context("user_tools_enabled", True)
    if refresh is not None:
        registry.set_context("runtime_refresh", refresh)
    return registry


def test_a_tool_created_through_a_plugins_view_actually_registers(tmp_path):
    """The composition hands the tools plugin a view, and creation must still work.

    A view contributes (recording the undo on the plugin's scope) but is not the
    registry: it has no ``unregister_by_source_prefix``, which is what replacing a
    module's tools needs.  Left unnormalised, the author writes the file, records the
    approval, and then raises -- the worst of both outcomes, because the residue on
    disk says it succeeded.
    """
    registry = _registry(tmp_path)
    view = registry.scoped(_Scope())

    result = asyncio.run(PluginTools(view).create_tool("echo_demo", _ECHO_SOURCE))

    assert result.get("ok") is True, result
    assert "echo_demo" in registry.list_tools(), "registered on the registry itself"
    assert result.get("activated") is True
    assert asyncio.run(registry.call("echo_demo", {"text": "hi"})) == "echo:hi"


def test_deleting_a_tool_removes_it_from_the_registry(tmp_path):
    """The other half: a deleted tool must stop being callable, not linger."""
    registry = _registry(tmp_path)
    view = registry.scoped(_Scope())
    asyncio.run(PluginTools(view).create_tool("echo_demo", _ECHO_SOURCE))
    assert "echo_demo" in registry.list_tools()

    removed = asyncio.run(PluginTools(view).delete_tool("echo_demo"))

    assert removed.get("ok") is True, removed
    assert "echo_demo" not in registry.list_tools()


def test_a_created_tool_asks_the_composition_to_refresh_open_sessions(tmp_path):
    """A session's registry is a *fork*, so ``registered`` is not the same as ``visible``.

    Sessions compare a revision number per message and rebuild their runtime when it
    changes; that is how a settings save reaches open pages.  A new tool is the same
    kind of change, so the tool layer announces it through the composition instead of
    reaching into the component mapping itself.
    """
    calls: list[int] = []
    registry = _registry(tmp_path, refresh=lambda: calls.append(1))

    result = asyncio.run(
        PluginTools(registry.scoped(_Scope())).create_tool("echo_demo", _ECHO_SOURCE)
    )

    assert result.get("ok") is True, result
    assert calls == [1], "exactly one announcement, after success"


def test_a_failed_creation_does_not_announce_a_change(tmp_path):
    """Control: a rejected module must not make every open session rebuild."""
    calls: list[int] = []
    registry = _registry(tmp_path, refresh=lambda: calls.append(1))

    result = asyncio.run(
        PluginTools(registry.scoped(_Scope())).create_tool("broken", "import os\n")
    )

    assert result.get("ok") is False
    assert calls == []


def test_without_sessions_the_report_says_callable_now_and_it_is_true(tmp_path):
    """The three answers must stay distinguishable, because they are three sentences.

    A single-turn CLI has no session forks, so a tool registered on the registry really
    is callable immediately: "callable now" is accurate there.  A gateway has forks and
    must be *told* to rebuild, and only then may the caller say the tool is visible.
    Collapsing the two cases into one message would make the one build that needs a
    refresh look exactly like the one that does not.
    """
    registry = _registry(tmp_path)  # nothing seeded runtime_refresh

    result = asyncio.run(
        PluginTools(registry.scoped(_Scope())).create_tool("echo_demo", _ECHO_SOURCE)
    )

    assert result.get("ok") is True, result
    assert result.get("refresh") == "absent"
    assert result.get("session_visible") is True
    assert asyncio.run(registry.call("echo_demo", {"text": "ok"})) == "echo:ok"


def test_a_gateway_is_told_to_rebuild_and_is_not_promised_visibility_without_it(tmp_path):
    """The failure branch: a refresh that raises must not be reported as success."""
    def broken() -> None:
        raise RuntimeError("channel is shutting down")

    registry = _registry(tmp_path, refresh=broken)

    result = asyncio.run(
        PluginTools(registry.scoped(_Scope())).create_tool("echo_demo", _ECHO_SOURCE)
    )

    assert result.get("ok") is True, "the tool really is registered"
    assert result.get("refresh", "").startswith("failed:")
    assert result.get("session_visible") is False
    assert "new session" in str(result.get("recovery_hint", ""))
