"""Per-server MCP lifecycle: disconnect, reconnect, and the self-heal tool.

What this pins
--------------
An MCP server used to have two states -- attempted once at startup, and gone at
process exit.  One server crashing meant restarting the gateway, and a plugin that
a reload removed left its stdio child running until the process died.  The fix is
a lifetime per server, and the rule that makes it safe is the one the old comment
in ``agent/tools/runtime.py`` states:

    **AnyIO cancel scopes must be exited by the task that entered them.**

So nothing here cancels a server task.  ``disconnect`` sets that server's own stop
event and awaits the task, which leaves ``async with AsyncExitStack()`` itself.
The fake contexts below make that checkable: every exit asserts it is running in
the same task that entered, and every exit is counted, so "the process really
stopped" is a number rather than an intention.

No real MCP server is spawned: ``stdio_client`` and ``ClientSession`` are the two
seams the client uses, and the fakes are the same shape as the one in
``tests/test_output_dir.py``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from agent.tools import runtime as runtime_module


class _FakeBackend:
    """Stands in for the processes behind a set of MCP servers."""

    def __init__(self) -> None:
        self.tools: dict[str, list[str]] = {}
        self.alive: dict[str, bool] = {}
        self.enter_counts: dict[str, int] = {}
        self.exit_counts: dict[str, int] = {}
        self.enter_tasks: dict[str, object] = {}
        self.exit_tasks: dict[str, object] = {}
        #: Servers whose process refuses to start, so a failed connect is reachable.
        self.fail_start: set[str] = set()
        #: Servers that stop answering pings -- a child that died without the session
        #: noticing, which is exactly the state the liveness probe exists to catch.
        self.silent: set[str] = set()
        #: Pings served, per server: the probe must be observable, not assumed.
        self.ping_counts: dict[str, int] = {}

    def add(self, name: str, *tool_names: str) -> None:
        self.tools[name] = list(tool_names)
        self.alive[name] = False
        self.enter_counts[name] = 0
        self.exit_counts[name] = 0

    # -- the two MCP SDK seams the client uses -------------------------------

    def stdio_client(self, params, *, errlog=None):
        return _StdioContext(self, params.args[0])

    def client_session(self, read, write):
        return _SessionContext(self, read)


class _StdioContext:
    """The ``stdio_client`` context: entering means the child is running."""

    def __init__(self, backend: _FakeBackend, name: str) -> None:
        self._backend = backend
        self._name = name

    async def __aenter__(self):
        backend = self._backend
        if self._name in backend.fail_start:
            raise RuntimeError(f"cannot start {self._name}")
        backend.alive[self._name] = True
        backend.enter_counts[self._name] += 1
        backend.enter_tasks[self._name] = asyncio.current_task()
        # The read/write streams: the fake session only needs to know which
        # server it belongs to.
        return (self._name, self._name)

    async def __aexit__(self, exc_type, exc, tb):
        backend = self._backend
        backend.exit_tasks[self._name] = asyncio.current_task()
        assert asyncio.current_task() is backend.enter_tasks[self._name], (
            "the stdio context was exited by a task that never entered it"
        )
        backend.alive[self._name] = False
        backend.exit_counts[self._name] += 1
        return False


class _SessionContext:
    def __init__(self, backend: _FakeBackend, name: str) -> None:
        self._backend = backend
        self._name = name

    async def __aenter__(self):
        return _FakeSession(self._backend, self._name)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, backend: _FakeBackend, name: str) -> None:
        self._backend = backend
        self._name = name

    async def initialize(self) -> None:
        return None

    async def send_ping(self) -> None:
        backend = self._backend
        backend.ping_counts[self._name] = backend.ping_counts.get(self._name, 0) + 1
        if self._name in backend.silent:
            raise RuntimeError(f"{self._name} is not answering")

    async def list_tools(self):
        tools = [
            SimpleNamespace(
                name=tool,
                description=f"{self._name} tool {tool}",
                inputSchema={"type": "object", "properties": {}, "required": []},
            )
            for tool in self._backend.tools.get(self._name, [])
        ]
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name: str, arguments=None):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"{self._name}:{name}")],
            isError=False,
            structuredContent=None,
        )


@pytest.fixture
def backend(monkeypatch) -> _FakeBackend:
    fake = _FakeBackend()
    monkeypatch.setattr(runtime_module.mcp, "stdio_client", fake.stdio_client)
    monkeypatch.setattr(runtime_module.mcp, "ClientSession", fake.client_session)
    return fake


def _client(registry=None):
    from agent.tools.runtime import MCPClient, ToolRegistry

    if registry is None:
        registry = ToolRegistry()
    return MCPClient(registry), registry


def _config(*names: str) -> dict:
    """One config entry per name; the name doubles as the command and its args.

    ``_FakeBackend.stdio_client`` reads the server name off ``params.args[0]``,
    which is the same information the real child would see on its command line.
    """
    return {
        "mcp_servers": [
            {"name": name, "command": name, "args": [name]} for name in names
        ]
    }


def _server_tools(registry) -> list[str]:
    """The tools that belong to a server, not the client's own handle."""
    return sorted(
        name for name in registry.list_tools() if name != "mcp_reconnect"
    )


# ── disconnect ──────────────────────────────────────────────────────────────


def test_disconnect_stops_one_server_and_takes_only_its_tools(backend) -> None:
    """The core claim: one server's process ends, its tools go, the rest stays."""
    backend.add("alpha", "echo", "ping")
    backend.add("beta", "search")
    client, registry = _client()

    async def scenario() -> dict:
        await client.connect_from_config(_config("alpha", "beta"))
        assert client.status_summary() == {
            "configured_servers": 2,
            "connected_servers": 2,
            "failed_servers": 0,
            "registered_tools": 3,
        }
        assert _server_tools(registry) == [
            "mcp_alpha_echo",
            "mcp_alpha_ping",
            "mcp_beta_search",
        ]
        result = await client.disconnect("alpha")
        try:
            # The process is gone, and gone the way it started: the same task
            # entered and left the stdio context (the fake asserts that too).
            assert backend.alive["alpha"] is False
            assert backend.exit_counts["alpha"] == 1
            assert backend.exit_tasks["alpha"] is backend.enter_tasks["alpha"]
            # Its tools are gone, the other server's are not.
            assert _server_tools(registry) == ["mcp_beta_search"]
            assert backend.alive["beta"] is True, (
                "disconnecting one server must not touch another"
            )
            assert backend.exit_counts["beta"] == 0
            # A tool call on the stopped server is an honest "not found" rather
            # than a call into a closed session.
            gone = json.loads(await registry.call("mcp_alpha_echo", {}))
            assert gone["ok"] is False
            # The surviving server still serves.
            served = json.loads(await registry.call("mcp_beta_search", {}))
            assert served["text"] == "beta:search"
            # The counts describe the servers that are left.
            assert client.status_summary() == {
                "configured_servers": 1,
                "connected_servers": 1,
                "failed_servers": 0,
                "registered_tools": 1,
            }
            # Asking twice is not an error: there is simply nothing to stop.
            again = await client.disconnect("alpha")
            return result, again
        finally:
            await client.close()

    result, again = asyncio.run(scenario())

    assert result == {
        "ok": True,
        "server": "alpha",
        "stopped": True,
        "was_connected": True,
        "tools_removed": 2,
    }
    assert again["ok"] is True and again["tools_removed"] == 0


def test_disconnect_of_a_server_that_was_never_configured_says_so(backend) -> None:
    backend.add("alpha", "echo")
    client, _ = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        try:
            return await client.disconnect("nope")
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert result["ok"] is False
    assert "unknown MCP server" in result["error"]


def test_a_server_name_is_accepted_the_way_the_config_spells_it(backend) -> None:
    """``disconnect`` normalises: the config name and the tool prefix are the same server."""
    backend.add("my server", "echo")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(
            {
                "mcp_servers": [
                    {"name": "my server", "command": "my", "args": ["my server"]}
                ]
            }
        )
        assert _server_tools(registry) == ["mcp_my_server_echo"]
        try:
            return await client.disconnect("my server")
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert result == {
        "ok": True,
        "server": "my_server",
        "stopped": True,
        "was_connected": True,
        "tools_removed": 1,
    }
    assert backend.alive["my server"] is False


def test_close_stops_every_server_through_its_own_task(backend) -> None:
    """The composition's teardown is the same mechanism, once per server."""
    backend.add("alpha", "echo")
    backend.add("beta", "search")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha", "beta"))
        await client.close()

    asyncio.run(scenario())

    assert backend.alive == {"alpha": False, "beta": False}
    assert backend.exit_counts == {"alpha": 1, "beta": 1}
    assert client._server_tasks == [], "no task is left unowned"
    assert client.status_summary() == {
        "configured_servers": 2,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }
    assert _server_tools(registry) == []


# ── reconnect ───────────────────────────────────────────────────────────────


def test_reconnect_brings_the_tools_back_from_the_same_config(backend) -> None:
    backend.add("alpha", "echo", "ping")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        await client.disconnect("alpha")
        assert _server_tools(registry) == []
        result = await client.reconnect("alpha")
        try:
            assert backend.alive["alpha"] is True
            assert backend.enter_counts["alpha"] == 2, "the process was started again"
            assert backend.exit_counts["alpha"] == 1, "and the old one was reaped"
            assert _server_tools(registry) == ["mcp_alpha_echo", "mcp_alpha_ping"]
            assert json.loads(await registry.call("mcp_alpha_echo", {}))["text"] == (
                "alpha:echo"
            )
            assert client.status_summary() == {
                "configured_servers": 1,
                "connected_servers": 1,
                "failed_servers": 0,
                "registered_tools": 2,
            }
            return result
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert result == {
        "ok": True,
        "server": "alpha",
        "connected": True,
        "tools": ["mcp_alpha_echo", "mcp_alpha_ping"],
    }


def test_reconnect_of_a_running_server_restarts_it(backend) -> None:
    backend.add("alpha", "echo")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        try:
            result = await client.reconnect("alpha")
            assert backend.enter_counts["alpha"] == 2
            assert backend.exit_counts["alpha"] == 1, "the first process was stopped first"
            assert _server_tools(registry) == ["mcp_alpha_echo"]
            return result
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert result["ok"] is True


def test_a_failed_reconnect_is_reported_as_a_failure_not_as_the_old_state(backend) -> None:
    """The old session is gone by the time the new one fails: saying "ok" would lie."""
    backend.add("alpha", "echo")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        backend.fail_start.add("alpha")
        failed = await client.reconnect("alpha")
        try:
            assert failed["ok"] is False
            assert failed["connected"] is False
            assert "cannot start alpha" in failed["error"]
            assert backend.alive["alpha"] is False
            assert _server_tools(registry) == [], "no live tool points at a dead session"
            assert client.status_summary() == {
                "configured_servers": 1,
                "connected_servers": 0,
                "failed_servers": 1,
                "registered_tools": 0,
            }
            # The config is still known, so a later attempt is a plain retry.
            backend.fail_start.discard("alpha")
            retried = await client.reconnect("alpha")
            assert retried["ok"] is True
            assert client.status_summary()["connected_servers"] == 1
            return failed, retried
        finally:
            await client.close()

    failed, retried = asyncio.run(scenario())

    assert failed["server"] == "alpha"
    assert retried["tools"] == ["mcp_alpha_echo"]


def test_reconnect_of_an_unknown_server_is_a_failure(backend) -> None:
    client, _ = _client()

    async def scenario():
        try:
            return await client.reconnect("nope")
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert result["ok"] is False
    assert "unknown MCP server" in result["error"]


# ── the model's self-heal tool ──────────────────────────────────────────────


def test_the_reconnect_tool_is_registered_and_says_what_it_costs(backend) -> None:
    backend.add("alpha", "echo")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        try:
            return None
        finally:
            await client.close()

    asyncio.run(scenario())

    assert "mcp_reconnect" in registry.list_tools()
    assert registry.tool_capabilities("mcp_reconnect") == frozenset({"state_write"})
    assert registry.tool_source("mcp_reconnect") == "mcp", (
        "not under a server's prefix: it has to outlive the server it restarts"
    )
    declared = next(
        tool
        for tool in registry.to_anthropic_format()
        if tool["name"] == "mcp_reconnect"
    )
    description = declared["description"].lower()
    assert "restart" in description
    assert "external process" in description, "the model is told what it costs"


def test_the_reconnect_tool_restarts_everything_by_default(backend) -> None:
    backend.add("alpha", "echo")
    backend.add("beta", "search")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha", "beta"))
        try:
            every = json.loads(await registry.call("mcp_reconnect", {}))
            assert backend.enter_counts == {"alpha": 2, "beta": 2}
            assert every["ok"] is True
            assert [item["server"] for item in every["results"]] == ["alpha", "beta"]
            assert every["status"]["connected_servers"] == 2

            one = json.loads(
                await registry.call("mcp_reconnect", {"server": "beta"})
            )
            assert [item["server"] for item in one["results"]] == ["beta"]
            assert backend.enter_counts == {"alpha": 2, "beta": 3}, (
                "naming one server must not restart the others"
            )
            assert one["ok"] is True
            return every, one
        finally:
            await client.close()

    every, one = asyncio.run(scenario())

    assert every["results"][0]["tools"] == ["mcp_alpha_echo"]
    assert one["results"][0]["tools"] == ["mcp_beta_search"]


def test_the_reconnect_tool_fails_honestly_when_a_server_will_not_start(backend) -> None:
    backend.add("alpha", "echo")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        backend.fail_start.add("alpha")
        try:
            return json.loads(await registry.call("mcp_reconnect", {}))
        finally:
            await client.close()

    payload = asyncio.run(scenario())

    assert payload["ok"] is False
    assert payload["results"][0]["ok"] is False
    assert payload["status"]["failed_servers"] == 1
    assert payload["status"]["connected_servers"] == 0


def test_the_reconnect_tool_validates_its_parameters(backend) -> None:
    backend.add("alpha", "echo")
    client, registry = _client()

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        try:
            wrong_type = json.loads(
                await registry.call("mcp_reconnect", {"server": 3})
            )
            unknown_field = json.loads(
                await registry.call("mcp_reconnect", {"servr": "alpha"})
            )
            return wrong_type, unknown_field
        finally:
            await client.close()

    wrong_type, unknown_field = asyncio.run(scenario())

    assert wrong_type["ok"] is False
    assert wrong_type["error"]["code"] == "invalid_request"
    assert "expected string" in wrong_type["error"]["message"]
    assert unknown_field["ok"] is False
    assert unknown_field["error"]["code"] == "invalid_request"
    assert "unknown field" in unknown_field["error"]["message"]
    assert backend.enter_counts["alpha"] == 1, "a rejected call restarts nothing"


# ── the published status ────────────────────────────────────────────────────


def test_a_bound_status_dict_follows_the_servers_after_a_self_heal(backend) -> None:
    """What the composition shows has to change when the model restarts a server."""
    backend.add("alpha", "echo")
    client, _ = _client()
    published = {
        "configured_servers": 1,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }
    client.bind_status(published)
    assert published["connected_servers"] == 0, "binding itself reports nothing"

    async def scenario():
        await client.connect_from_config(_config("alpha"))
        assert published == {
            "configured_servers": 1,
            "connected_servers": 1,
            "failed_servers": 0,
            "registered_tools": 1,
        }
        await client.disconnect("alpha")
        assert published == {
            "configured_servers": 0,
            "connected_servers": 0,
            "failed_servers": 0,
            "registered_tools": 0,
        }
        await client.close()

    asyncio.run(scenario())


# ── the reload path ─────────────────────────────────────────────────────────


def _bundled_plugin(tmp_path):
    """A plugin that declares one MCP server in its manifest."""
    plugin_dir = tmp_path / "mcp-bundler"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "def register():\n"
        "    class P:\n"
        "        name = 'mcp-bundler'\n"
        "    return P()\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "mcp-bundler",
                "mcp_servers": [
                    {"name": "bundled", "command": "bundled", "args": ["bundled"]}
                ],
            }
        ),
        encoding="utf-8",
    )
    return plugin_dir


def test_a_reload_that_removes_a_plugin_stops_its_mcp_process(backend, tmp_path) -> None:
    """The leak this task was opened for, asserted end to end.

    A bundle contributes a server config, the composition connects it, the bundle
    is deleted from disk and reloaded.  Before this change the *tool rows* went and
    the process stayed; now ``_connect_new_mcp`` disconnects it, so the fake
    backend records an exit -- in the task that entered the context.
    """
    from agent import PluginCatalog
    from agent.tools.mcp_servers import MCPServerRegistry
    from agent.tools.runtime import ToolRegistry

    backend.add("bundled", "echo")
    registry = ToolRegistry()
    client, _ = _client(registry)
    contributed = MCPServerRegistry()

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._host.services.provide("registry", registry)
    catalog._host.services.provide("mcp_client", client)
    catalog._host.services.provide("contributed_mcp_servers", contributed)
    plugin_dir = _bundled_plugin(tmp_path)
    components = {
        "registry": registry,
        "mcp_client": client,
        "contributed_mcp_servers": contributed,
    }

    async def scenario():
        catalog.discover_and_load()
        assert [config["name"] for _, config in contributed.records()] == ["bundled"]
        await client.connect_from_config(
            {"mcp_servers": [config for _, config in contributed.records()]}
        )
        assert _server_tools(registry) == ["mcp_bundled_echo"]
        assert backend.alive["bundled"] is True

        shutil.rmtree(plugin_dir)
        result = await catalog.reload(components)
        try:
            assert "mcp-bundler" in result["removed_plugins"]
            assert backend.alive["bundled"] is False, (
                "the removed plugin's stdio child was left running"
            )
            assert backend.exit_counts["bundled"] == 1
            assert backend.exit_tasks["bundled"] is backend.enter_tasks["bundled"]
            assert _server_tools(registry) == [], "and its tools went with it"
            assert client.status_summary()["connected_servers"] == 0
            return result
        finally:
            await client.close()

    result = asyncio.run(scenario())

    assert result["newly_connected_mcp"] == []


def test_a_reload_reports_only_the_servers_that_actually_connected(backend, tmp_path) -> None:
    """``newly_connected_mcp`` is published as "connected MCP", so it has to be true."""
    from agent.tools.mcp_servers import MCPServerRegistry
    from agent.tools.runtime import ToolRegistry
    from agent.plugins.reload import ReloadTransaction

    backend.add("good", "ping")
    backend.fail_start.add("broken")
    registry = ToolRegistry()
    client, _ = _client(registry)
    arrived = MCPServerRegistry()
    arrived.add(
        {"name": "good", "command": "good", "args": ["good"]}, owner="acme"
    )
    arrived.add(
        {"name": "broken", "command": "broken", "args": ["broken"]}, owner="acme"
    )

    async def scenario():
        # No catalog needed: the method under test takes the registry and the
        # client, and that is exactly the seam a reload hands it.
        transaction = ReloadTransaction(None)
        connected = await transaction._connect_new_mcp(
            arrived, set(), {"acme"}, client, registry
        )
        try:
            assert connected == ["good"]
            assert client.is_connected("good") is True
            assert client.is_connected("broken") is False
            assert client.status_summary()["failed_servers"] == 1
            return connected
        finally:
            await client.close()

    asyncio.run(scenario())


def test_a_reload_falls_back_to_unregistering_for_a_client_that_cannot_disconnect(
    backend, tmp_path
) -> None:
    """A substituted client still gets what the reload could always give it."""
    from agent.plugins.reload import ReloadTransaction
    from agent.tools.mcp_servers import MCPServerRegistry
    from agent.tools.runtime import ToolRegistry

    class _OldClient:
        """No ``disconnect``: the shape the reload had to support before this task."""

        def __init__(self):
            self.calls: list[tuple] = []

        async def connect_from_config(self, config, extra_env=None):
            self.calls.append(("connect", config))

    registry = ToolRegistry()
    registry.register(
        "mcp_gone_echo",
        "echo",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"ok": True},
        source="mcp:gone",
    )
    client = _OldClient()
    after = MCPServerRegistry()

    async def scenario():
        transaction = ReloadTransaction(None)
        return await transaction._connect_new_mcp(
            after, {("acme", "gone")}, {"acme"}, client, registry
        )

    connected = asyncio.run(scenario())

    assert connected == []
    assert "mcp_gone_echo" not in registry.list_tools()


def test_the_client_works_through_the_plugins_scoped_view(backend) -> None:
    """The composition hands this client ``ctx.get("registry")``, which is a *view*.

    A view contributes -- and records the undo on the plugin's scope -- but it is not
    the registry, so it has no ``unregister_by_source_prefix``.  Code that guards that
    call with ``getattr(..., None)`` therefore does nothing at all here, and the
    failure is the quiet kind: the server's process stops while its tools stay
    registered, so the next call lands on a closed session.  Normalising the view in
    the constructor is what keeps "disconnect" meaning what it says; the composition is
    the only caller that passes a view, which is why a test with a bare registry could
    not have caught it.
    """
    backend.add("alpha", "echo", "ping")

    from agent.tools.runtime import MCPClient, ToolRegistry

    class _Scope:
        name = "mcp"

        def __init__(self) -> None:
            self.undos: list = []

        def on_unwind(self, fn) -> None:
            self.undos.append(fn)

    underlying = ToolRegistry()
    client = MCPClient(underlying.scoped(_Scope()))

    async def scenario() -> dict:
        await client.connect_from_config(_config("alpha"))
        assert _server_tools(underlying) == ["mcp_alpha_echo", "mcp_alpha_ping"]
        return await client.disconnect("alpha")

    result = asyncio.run(scenario())

    assert result["tools_removed"] == 2
    assert _server_tools(underlying) == [], (
        "the tools must be gone from the registry the view wraps, not only from a "
        "count the client keeps"
    )


# ── liveness: noticing a child that died ────────────────────────────────────


def test_health_checks_are_off_by_default(backend) -> None:
    """A background timer is a decision, and the default answer is "no timer"."""
    backend.add("alpha", "echo")
    client, _registry_ = _client()

    async def scenario() -> None:
        await client.connect_from_config(_config("alpha"))

    asyncio.run(scenario())

    assert client._health_task is None
    assert backend.ping_counts == {}, "nothing should be pinging unless asked to"


def test_a_silent_server_is_noticed_and_reconnected_once(backend) -> None:
    """The SDK reports no exit, so a dead child is invisible until a tool call fails.

    The probe is the only way to notice without one; one automatic reconnect is what
    turns "my MCP tools stopped working" into a blip.  The second half of the assertion
    is the one that matters: the tools are registered again, on a *new* child -- a
    reconnect that reported success while leaving the session closed would pass a test
    that only counted attempts.
    """
    backend.add("alpha", "echo")
    client, registry = _client()
    client.configure_health_checks(0.05)

    async def scenario() -> None:
        await client.connect_from_config(_config("alpha"))
        assert backend.ping_counts.get("alpha", 0) == 0, "no probe before the interval"
        backend.silent.add("alpha")  # the child dies without telling the session
        await asyncio.sleep(0.15)
        # The reconnect happened; make the *new* child healthy so the recovery is what
        # the assertions below are about rather than the fake's own state.
        backend.silent.clear()
        await asyncio.sleep(0.2)
        assert backend.enter_counts["alpha"] == 2, backend.enter_counts
        assert backend.ping_counts["alpha"] >= 1, "the probe must actually have run"
        assert client.is_connected("alpha") is True
        assert "mcp_alpha_echo" in registry.list_tools()
        await client.close()

    asyncio.run(scenario())


def test_a_server_that_stays_dead_is_retried_only_once(backend) -> None:
    """Bounded on purpose: a timer restarting a corpse forever is the loop this cap prevents.

    One automatic attempt, then silence until something asks again -- and asking
    *again* is allowed, because a person or a model asking is new information that a
    timer does not have.
    """
    backend.add("alpha", "echo")
    client, registry = _client()
    client.configure_health_checks(0.03)

    async def scenario() -> None:
        await client.connect_from_config(_config("alpha"))
        backend.silent.add("alpha")
        await asyncio.sleep(0.5)  # many probes, one budget
        assert backend.enter_counts["alpha"] == 2, (
            "one initial connect plus exactly one automatic reconnect, "
            f"got {backend.enter_counts['alpha']} starts"
        )
        assert client.is_connected("alpha") is True, (
            "the retry did connect; it is the *next* failure the budget covers"
        )

        # A manual reconnect clears the mark: somebody asking is not the timer.
        backend.silent.clear()
        result = await client.reconnect("alpha")
        assert result["ok"] is True, result
        assert backend.enter_counts["alpha"] == 3, backend.enter_counts
        assert "mcp_alpha_echo" in registry.list_tools()
        await client.close()

    asyncio.run(scenario())


def test_closing_cancels_the_monitor(backend) -> None:
    """The monitor is the client's own task, so it may -- and must -- be cancelled here."""
    backend.add("alpha", "echo")
    client, _registry_ = _client()
    client.configure_health_checks(0.05)

    async def scenario() -> None:
        await client.connect_from_config(_config("alpha"))
        monitor = client._health_task
        assert monitor is not None
        await client.close()
        await asyncio.sleep(0.05)
        assert monitor.done(), "a closed client must not keep pinging"

    asyncio.run(scenario())
