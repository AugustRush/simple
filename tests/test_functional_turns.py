"""Functional turns: the product, driven the way a person drives it.

Why this file exists
--------------------
Two rounds in a row shipped a defect that every unit test in this suite passed
through:

* the agent was constructed with ``ctx.get("registry")`` -- a *plugin-scoped
  view* -- and its turn loop writes the registry's shared context before the
  first model call, so every turn raised ``'ScopedTools' object has no attribute
  'set_context'``.  The web UI showed a message and then nothing at all, and the
  CLI was dead the same way;
* ``build_routing_transport`` built a client for every provider that owns a model,
  so an unset ``$ENV`` in a section nobody routed to stopped the process starting.

Neither was subtle; both were invisible, because the tests that looked like they
covered the ground replaced the very thing that was broken.  ``test_agent_plugin``
handed the plugin a mapping whose registry was a plain ``object()`` -- no
``scoped`` method, so the double could not tell a view from a service.  The CLI
integration tests replace ``agent.send_message``, so the turn loop they "drive"
never runs.

What is real here, and what is not
----------------------------------
Real: the composition root (``_build_components_async``), the plugin catalog and
its plugins, the agent, ``LoopDeps``, the turn loop, tool dispatch, the command
coordinator, the channel and its WebSocket, the CLI command and its options, and
an OpenAI-compatible endpoint over a real socket -- so the SDK client, the
OpenAI transport and the SSE parse are the production ones.

Not real: the model's *answers*.  They are scripted, one per request, because a
test that needs a specific reply from a specific model is a test about that
model.

The rule this file is built on: **a double must not be simpler than the thing it
stands in for, in the way that matters.**  Every double here has the shape of
the real object on the axis the test is about.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent import shared

# ── A local, scripted, OpenAI-compatible provider ────────────────────────────


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-func",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "scripted-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )
        + "\n\n"
    ).encode()


def _usage_chunk() -> bytes:
    """The final usage-only chunk a stream ends with when usage is requested.

    Sent because the real providers send it and the transport reads it: a fake
    that omits it would leave the usage path unexercised, which is exactly the
    kind of gap this file exists to close.
    """
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-func",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "scripted-model",
                "choices": [],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 2,
                    "total_tokens": 13,
                },
            }
        )
        + "\n\n"
    ).encode()


class _ScriptedProvider:
    """An OpenAI-shaped endpoint that answers from a script, over a real socket.

    Each POST consumes the next scripted reply, so a test can say what the model
    does *this* call: answer, or ask for a tool and answer on the next one.
    """

    def __init__(self, replies: list[dict]) -> None:
        self._replies = list(replies)
        self.requests: list[dict] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw)
                provider.requests.append(body)
                reply = (
                    provider._replies.pop(0)
                    if provider._replies
                    else {"text": "no scripted reply left"}
                )
                if body.get("stream"):
                    self._stream(reply)
                else:
                    self._json(reply, body)

            def _json(self, reply: dict, body: dict) -> None:
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": reply.get("text", ""),
                }
                finish = "stop"
                if reply.get("tool"):
                    name, arguments = reply["tool"]
                    message["content"] = None
                    message["tool_calls"] = [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                    finish = "tool_calls"
                payload = json.dumps(
                    {
                        "id": "chatcmpl-func",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body.get("model", "scripted-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": finish,
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 2,
                            "total_tokens": 13,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _stream(self, reply: dict) -> None:
                # A reply may be told to take its time, which is how a test gets a turn
                # that is still running when the next message arrives.
                time.sleep(float(reply.get("delay", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if reply.get("tool"):
                    name, arguments = reply["tool"]
                    self.wfile.write(
                        _chunk(
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": name, "arguments": ""},
                                    }
                                ],
                            }
                        )
                    )
                    self.wfile.write(
                        _chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": json.dumps(arguments)
                                        },
                                    }
                                ]
                            }
                        )
                    )
                    self.wfile.write(_chunk({}, finish="tool_calls"))
                else:
                    self.wfile.write(_chunk({"role": "assistant", "content": ""}))
                    for piece in _pieces(str(reply.get("text", ""))):
                        self.wfile.write(_chunk({"content": piece}))
                    self.wfile.write(_chunk({}, finish="stop"))
                self.wfile.write(_usage_chunk())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _pieces(text: str) -> list[str]:
    """Several deltas rather than one, so streaming is streaming.

    A single-chunk reply would pass through a path that never has to accumulate,
    and accumulating is where a streamed answer goes wrong.
    """
    if len(text) < 2:
        return [text]
    middle = len(text) // 2
    return [text[:middle], text[middle:]]


def scripted(*replies: dict) -> _ScriptedProvider:
    return _ScriptedProvider(list(replies))


# ── An isolated home and a config that points at the scripted provider ────────


@pytest.fixture
def home(tmp_path):
    """A throwaway agent home, switched with the product's own switch.

    ``shared._set_agent_home`` is what ``--name`` and ``SIMPLE_AGENT_HOME`` use,
    so the paths the composition reads are rebound exactly as a real run rebinds
    them -- and restored afterwards, because they are module globals.
    """
    previous = shared.AGENT_HOME
    home_dir = tmp_path / "agent-home"
    home_dir.mkdir(parents=True, exist_ok=True)
    shared._set_agent_home(home_dir)
    try:
        yield home_dir
    finally:
        shared._set_agent_home(previous)


def write_config(home_dir, provider: _ScriptedProvider, **extra: Any) -> dict:
    """A one-provider config pointing at *provider*, written where the CLI reads it."""
    cfg = {
        "active_provider": "scripted",
        "providers": {
            "scripted": {
                "api_format": "openai",
                "api_key": "scripted-key",
                "base_url": provider.base_url,
                "default_model": "scripted-model",
                "models": ["scripted-model"],
            }
        },
        "channels": {"web": {"enabled": False}, "feishu": {"enabled": False}},
        "plugins": {},
        "memory": {},
        "orchestration": {},
        "context": {},
        "mcp_servers": [],
    }
    cfg.update(extra)
    (home_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cfg


# ── 1. The CLI, which is the entry point a person actually types ─────────────


def test_cli_chat_answers(home):
    """``simple chat "..."`` prints the model's answer.

    This is the test that would have caught the scoped-registry defect: the CLI
    builds the real composition, so the real agent and the real turn loop run,
    and ``prepare_turn`` writes the registry's shared context before anything
    else happens.
    """
    provider = scripted({"text": "pong"})
    write_config(home, provider)
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        result = CliRunner().invoke(app, ["chat", "reply with one word"])

        assert result.exit_code == 0, result.output
        assert "pong" in result.output, result.output
        assert provider.requests, "the composition must have called the provider"
    finally:
        provider.close()


def test_cli_chat_sends_the_configured_model(home):
    """The model in the config is the model on the wire.

    A separate assertion because "it answered" and "it answered from the model
    you configured" are different facts, and only the second one is the product.
    """
    provider = scripted({"text": "ok"})
    write_config(home, provider)
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        result = CliRunner().invoke(app, ["chat", "hi"])

        assert result.exit_code == 0, result.output
        assert provider.requests[0]["model"] == "scripted-model"
    finally:
        provider.close()


# ── 2. A tool call, all the way round ────────────────────────────────────────


def test_a_tool_call_goes_out_and_its_result_comes_back(home):
    """Two model calls: one asking for a tool, one answering with its result.

    The second request is asserted to contain the tool's *output*, which is what
    distinguishes a tool round trip from a model that merely mentioned one.
    """
    provider = scripted(
        {"tool": ("read_file", {"path": "missing-file-for-the-test.txt"})},
        {"text": "the tool said so"},
    )
    write_config(home, provider)
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        result = CliRunner().invoke(app, ["chat", "read that file"])

        assert result.exit_code == 0, result.output
        assert "the tool said so" in result.output, result.output
        assert len(provider.requests) >= 2, (
            "the tool call has to come back to the model, or this is not a round trip"
        )
        # The tool result travels back as a `tool` role message naming the tool.
        second = provider.requests[1]["messages"]
        assert any(
            message.get("role") == "tool" for message in second
        ), [message.get("role") for message in second]
    finally:
        provider.close()


# ── 3. The web channel, over a real WebSocket ────────────────────────────────

WEB_CHANNELS = {
    "web": {"enabled": True, "host": "127.0.0.1", "port": 0},
    "feishu": {"enabled": False},
}


async def _start_web_channel(components, cfg):
    """The gateway's own wiring, without uvicorn's blocking serve loop.

    The channel, the runner's handler factory, ``bind_runtime`` and the ASGI app
    are the production ones -- this only runs them in the test's event loop so the
    test can hold the server handle and stop it.
    """
    import asyncio
    import uvicorn

    from agent.channels.base import ChannelRunner
    from agent.channels.web import WebChannel, WebConfig

    channel = WebChannel(WebConfig(host="127.0.0.1", port=0))
    sessions: dict[str, Any] = {}
    runner = ChannelRunner([channel], components, cfg)
    channel.bind_runtime(sessions, components)
    channel._handler = runner._make_message_handler(sessions)

    server = uvicorn.Server(
        uvicorn.Config(
            channel._build_app(), host="127.0.0.1", port=0, log_level="warning"
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, serving, port


async def _ask(socket, text: str, message_id: str) -> list[dict]:
    """One message, and every event until its turn is complete."""
    import asyncio
    import json as _json

    await socket.send(
        _json.dumps({"type": "message", "text": text, "message_id": message_id})
    )
    events: list[dict] = []
    while True:
        event = _json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
        events.append(event)
        if event.get("type") == "turn_complete":
            return events


def _answer(events: list[dict]) -> str:
    completion = next(e for e in events if e["type"] == "turn_complete")
    return str(completion.get("text", ""))


def test_the_web_socket_streams_a_reply(home):
    """A browser's own path: send a message, receive the answer as it is written.

    The composition, the channel, the command coordinator, the per-session runtime
    and the WebSocket are the production ones; the server is really listening and
    the client really connects.  What was reported as "the web chat does nothing
    at all" is the failure this asserts against: a broken turn produced a
    ``turn_complete`` with an empty text and no error event, so the page had
    nothing to show and nothing to report.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "pong from the web"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-functional/stream"
                ) as socket:
                    return await _ask(socket, "hi", "m1")
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        kinds = [event["type"] for event in events]
        assert "turn_complete" in kinds, kinds
        completion = next(e for e in events if e["type"] == "turn_complete")
        assert "pong from the web" in completion.get("text", ""), events
        assert any(event["type"] == "stream_chunk" for event in events), (
            "the web path streams: a completion with no chunk before it means the "
            "streamed answer never reached the socket"
        )
        assert not [e for e in events if e["type"] == "error"], events
        assert provider.requests, "the provider was called"
    finally:
        provider.close()


# ── 4. A user plugin, loaded by the real catalog ─────────────────────────────

_PLUGIN = '''
import json
import pathlib

HERE = pathlib.Path(__file__).parent
MARKER = "%(marker)s"


def _note(kind, **fields):
    with (HERE / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "marker": MARKER, **fields}) + "\\n")


class _Tool:
    name = "plugin_echo"
    description = "Echo a word back."
    parameters = {"type": "object", "properties": {"word": {"type": "string"}}}
    capabilities = None

    def fn(self, word="", **kwargs):
        _note("tool", word=word)
        return "the plugin says " + str(word)


def register():
    class Recorder:
        name = "recorder"
        # The service is called ``registry`` in this codebase; the plan's ``ctx.tools``
        # is the same shape under a different name.
        inject = ("registry",)

        def setup(self, ctx):
            from agent.core.event_domains import TurnCompleted

            ctx.registry.register(_Tool())
            ctx.on(TurnCompleted, lambda event: _note("turn", response=event.response))
            # Written **last**, so the marker appearing at all is evidence that setup
            # ran to completion -- a note written first would still be there after the
            # rest of setup raised, which is how this test first passed for the wrong
            # reason.
            _note("setup")

    return Recorder()
'''


def write_plugin(home_dir, marker: str = "v1") -> Any:
    """A user plugin in the home the composition discovers plugins from."""
    plugin_dir = home_dir / "plugins" / "recorder"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    init = plugin_dir / "__init__.py"
    init.write_text(_PLUGIN % {"marker": marker}, encoding="utf-8")
    return init


def plugin_notes(plugin_dir) -> list[dict]:
    path = plugin_dir / "calls.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_a_user_plugin_contributes_a_tool_and_hears_the_turn(home):
    """Four subsystems in one turn, through the product rather than around it.

    Plugin discovery, ``ctx.tools.register`` (which goes through the scoped view,
    so the registration is an effect on the plugin's scope), the agent *calling*
    the tool it contributed, and ``ctx.on(TurnCompleted, ...)`` -- the single-type
    subscription that is routed to the turn bus by the composition.

    Each of those has unit tests.  What none of them covers is that the four are
    wired to each other in a running composition, which is the only place the
    wiring exists.
    """
    provider = scripted(
        {"tool": ("plugin_echo", {"word": "marco"})},
        {"text": "polo"},
    )
    write_config(home, provider)
    write_plugin(home, "v1")
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        result = CliRunner().invoke(app, ["chat", "use the echo tool"])

        assert result.exit_code == 0, result.output
        notes = plugin_notes(home / "plugins" / "recorder")
        kinds = [note["kind"] for note in notes]
        assert "setup" in kinds, notes
        assert "tool" in kinds, (
            "the agent must be able to call the tool the plugin contributed: "
            f"{notes}"
        )
        assert "turn" in kinds, (
            "ctx.on(TurnCompleted, ...) must be delivered in a real composition: "
            f"{notes}"
        )
        assert [n for n in notes if n["kind"] == "turn"][0]["response"] == "polo"

        # And it was *offered* to the model, which is what makes it callable.
        offered = [
            tool["function"]["name"]
            for tool in provider.requests[0].get("tools", [])
        ]
        assert "plugin_echo" in offered, offered
    finally:
        provider.close()


def test_reloading_a_plugin_runs_its_new_code(home):
    """A plugin edit takes effect, including one Python cannot see by itself.

    ``MARKER`` changes from ``v1`` to ``v2``: same file size, and the mtime is
    restored to the byte, which is exactly the pair CPython validates a ``.pyc``
    against.  Without dropping the cached bytecode the loader imports the *old*
    module, so the second run reports ``v1`` -- a hot-reload path that silently
    keeps old code.

    Driven as two real CLI runs rather than a synthetic reload call, because that
    is how a person reloads a plugin: they edit the file and run it again.  The
    two runs share this process, so the import cache is shared too.
    """
    provider = scripted({"text": "first"}, {"text": "second"})
    write_config(home, provider)
    init = write_plugin(home, "v1")
    stat = init.stat()
    plugin_dir = home / "plugins" / "recorder"
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        runner = CliRunner()
        first = runner.invoke(app, ["chat", "one"])
        assert first.exit_code == 0, first.output

        # Same length, same mtime: the edit CPython's .pyc check cannot see.
        init.write_text(_PLUGIN % {"marker": "v2"}, encoding="utf-8")
        import os

        os.utime(init, (stat.st_atime, stat.st_mtime))
        assert init.stat().st_size == stat.st_size, "the size must be unchanged"
        assert init.stat().st_mtime == stat.st_mtime, "and so must the mtime"

        second = runner.invoke(app, ["chat", "two"])
        assert second.exit_code == 0, second.output

        markers = [
            note["marker"]
            for note in plugin_notes(plugin_dir)
            if note["kind"] == "setup"
        ]
        assert markers == ["v1", "v2"], (
            "the second run must have executed the edited plugin, not its cached "
            f"bytecode: {markers}"
        )
    finally:
        provider.close()


# ── 5. Switching provider mid-session, the way the settings page does ────────


def test_switching_provider_rebuilds_the_session_runtime(home):
    """A provider switch takes effect on the next turn of the same session.

    This is the path the settings page drives: ``POST
    /api/providers/{name}/activate`` writes the config, and the *existing* session
    has to notice -- it holds a runtime built from the old config, with a client
    for the old endpoint.  A session that kept it would answer from the provider
    the user just moved away from, which is the kind of wrong that looks like a
    working system.

    Both providers are real endpoints over real sockets, so "which provider
    answered" is answered by which server received the request.
    """
    import asyncio

    import agent as agent_module

    first = scripted({"text": "from the first provider"})
    second = scripted({"text": "from the second provider"})
    cfg = write_config(
        home,
        first,
        channels=WEB_CHANNELS,
        model="scripted-model",
        providers={
            "scripted": {
                "api_format": "openai",
                "api_key": "key-one",
                "base_url": first.base_url,
                "default_model": "scripted-model",
                "models": ["scripted-model"],
            },
            "other": {
                "api_format": "openai",
                "api_key": "key-two",
                "base_url": second.base_url,
                "default_model": "other-model",
                "models": ["other-model"],
            },
        },
    )
    try:

        async def run() -> tuple[list[dict], list[dict], dict]:
            import httpx
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-switch/stream"
                ) as socket:
                    before = await _ask(socket, "who are you", "m1")
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            f"http://127.0.0.1:{port}/api/providers/other/activate"
                        )
                    switched = response.json()
                    after = await _ask(socket, "and now", "m2")
                return before, after, switched
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        before, after, switched = asyncio.run(run())

        assert switched.get("active_provider") == "other", switched
        assert _answer(before) == "from the first provider", before
        # Which server answered is the evidence; the text is only its shadow.
        assert len(first.requests) == 1, (
            f"the first provider must be called exactly once, before the switch: "
            f"{[r.get('model') for r in first.requests]}"
        )
        assert second.requests, (
            "the session must rebuild against the config the switch wrote; the "
            f"second provider was never called.  Second turn: {after}"
        )
        assert second.requests[0]["model"] == "other-model", second.requests[0]["model"]
        assert _answer(after) == "from the second provider", after
    finally:
        first.close()
        second.close()


# ── 6. A session's own runtime, not the composition's ───────────────────────


def test_a_session_turn_writes_into_the_sessions_own_output_dir(home):
    """A per-session runtime is only real if the turn uses it.

    A web session builds its own container -- its own agent, its own registry fork
    and its own ``output_dir`` under ``web/sessions/<id>/`` -- so that sessions do
    not write over each other.  That was decorative while the turn ran through the
    composition's loop, which resolved the composition's ``agent``: the tools saw
    the composition's ``output_dir``.

    Asserted by where the file lands, which is the fact the isolation is made of.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "tool": (
                "write_file",
                {
                    "root": "output_dir",
                    "path": "isolation-probe.txt",
                    "mode": "create",
                    "content": "written by a session",
                },
            )
        },
        {"text": "written"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-isolation/stream"
                ) as socket:
                    return await _ask(socket, "write the probe file", "m1")
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())
        assert _answer(events) == "written", events

        written = list(home.rglob("isolation-probe.txt"))
        assert len(written) == 1, written
        where = written[0].relative_to(home).as_posix()
        assert where.startswith("web/sessions/web-isolation/output/"), (
            "the session's own output_dir must be what the tool writes into, not the "
            f"composition's: {where}"
        )
        # The composition's own output dir must be untouched: the isolation is not
        # "a copy also landed in the right place", it is "the wrong place was not used".
        assert not (home / "output" / "isolation-probe.txt").exists()

        # And the tool reported success, so the write that landed was the one the model
        # asked for -- not a fallback that happened to succeed elsewhere.
        tool_result = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_result, [m.get("role") for m in provider.requests[-1]["messages"]]
        assert '"ok": true' in str(tool_result[-1].get("content", "")), tool_result[-1]
    finally:
        provider.close()


# ── 7. The transcript a refreshed page restores ─────────────────────────────


def test_a_turns_transcript_is_recorded_and_served_to_a_reconnecting_client(home):
    """What a browser refresh reads: the conversation that already happened.

    The WebSocket carries a turn; the durable record is what survives the tab, and
    `/api/sessions/<id>/messages` is where the page gets it back.  Two halves of the
    refactor are only visible here: the append path (``SessionLog`` -- one writer) and the
    read path a restored page uses.  Nothing in the unit suite drives both through a turn,
    and the failure mode is the one the code comments about: a trace that disappears on
    refresh, with no error anywhere.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "I will remember that"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> dict:
            import httpx
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-history/stream"
                ) as socket:
                    events = await _ask(socket, "please remember the word marco", "m1")
                assert _answer(events) == "I will remember that", events
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"http://127.0.0.1:{port}/api/sessions/web-history/messages"
                    )
                assert response.status_code == 200, response.text
                return response.json()
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        payload = asyncio.run(run())

        transcript = json.dumps(payload, ensure_ascii=False)
        assert "marco" in transcript, (
            "the user's message must be in the durable transcript a refresh reads: "
            f"{transcript[:600]}"
        )
        assert "I will remember that" in transcript, (
            "and so must the reply: "
            f"{transcript[:600]}"
        )
    finally:
        provider.close()


# ── 8. A scheduled run, which is an agent turn with nobody watching ─────────


def test_a_scheduled_run_executes_a_turn_and_records_the_answer(home):
    """The unattended path: a due task runs a real turn and reports what came back.

    A scheduled run is where the composition is built *again* -- by the scheduler's
    runtime factory, from the config on disk -- so it exercises a different entry point
    from the ones above while still going through the agent, the loop and the provider.

    Asserted on the recorded run rather than on a console: a run has nobody watching, so
    the store is the only place its outcome exists.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone

    import agent as agent_module
    from agent.scheduler import DeliveryTarget, NewScheduledTask, TriggerSpec
    from agent.scheduler.plugin import SchedulerPlugin
    from agent.scheduler.store import scheduler_store
    from agent.kernel.service import require_service

    provider = scripted({"text": "the scheduled answer"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS, scheduler={"enabled": True})
    try:
        # The task is created with a store of the test's own and then closed, so the
        # store the plugin owns is the only handle while the run happens.
        setup = scheduler_store()
        try:
            task = setup.create_task(
                NewScheduledTask(
                    name="functional-run",
                    kind="agent_prompt",
                    trigger=TriggerSpec.once(
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ),
                    payload={"prompt": "answer the scheduled question"},
                    delivery_mode="standalone",
                    delivery_target=DeliveryTarget.standalone(),
                )
            )
        finally:
            setup.close()

        async def run() -> list:
            components = await agent_module._build_components_async(cfg, announce=False)
            try:
                components["plugin_catalog"].mount(SchedulerPlugin())
                service = require_service(components, "scheduler")
                await service.run_once()
            finally:
                await agent_module._close_components(components)
            runs = scheduler_store()
            try:
                return runs.list_runs(task.id)
            finally:
                runs.close()

        recorded = asyncio.run(run())

        assert recorded, "the due task must have produced a run"
        finished = [item for item in recorded if item.finished_at is not None]
        assert finished, [item.status for item in recorded]
        last = finished[-1]
        assert last.status == "succeeded", last
        assert "the scheduled answer" in (last.summary or ""), (
            "the run's record must carry what the turn produced, because a run has "
            f"nobody watching it: {last.summary!r}"
        )
        assert provider.requests, "the run must have reached the provider"
    finally:
        provider.close()


# ── 9. A sub-agent, which is a derived container of its own ─────────────────


def test_a_spawned_sub_agent_runs_with_its_own_restricted_tool_set(home):
    """The parent's call spawns a real sub-agent, whose request offers fewer tools.

    A sub-agent is built from a *derived* registry (``capability_profile``,
    ``write_scope``, and the orchestration capability excluded so it cannot spawn
    further).  Three model calls happen -- the parent asking for a spawn, the sub-agent
    answering, the parent answering -- and the evidence that the derived registry was the
    one used is in what the *middle* request offered: ``spawn_agent`` must not be there.

    Asserted on the wire rather than on the objects, because that is where a sub-agent that
    inherited the parent's registry would show up: a sub-agent that can spawn is a
    recursion the exclusion exists to prevent.
    """
    provider = scripted(
        {
            "tool": (
                "spawn_agent",
                {"role": "researcher", "task": "answer the sub-question"},
            )
        },
        {"text": "the sub answer"},
        {"text": "the parent answer"},
    )
    write_config(home, provider)
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        result = CliRunner().invoke(app, ["chat", "delegate this"])

        assert result.exit_code == 0, result.output
        assert "the parent answer" in result.output, result.output
        assert len(provider.requests) >= 3, (
            "expected a spawn, a sub-agent call and a parent call: "
            f"{[r.get('model') for r in provider.requests]}"
        )

        offered = [
            sorted(
                tool["function"]["name"] for tool in request.get("tools", [])
            )
            for request in provider.requests
        ]
        # The first request is the parent's (it has the spawner); the second is the
        # sub-agent's; the third is the parent's again.
        assert "spawn_agent" in offered[0], offered[0][:12]
        assert "spawn_agent" not in offered[1], (
            "a sub-agent must not be offered the spawner -- that is what makes the "
            f"derived registry observable: {offered[1][:12]}"
        )
        assert len(offered[1]) < len(offered[0]), (
            "and its tool set must be the restricted one, not the parent's"
        )
    finally:
        provider.close()


# ── 10. Loading and unloading a plugin in a *running* composition ────────────


async def _turn_in_process(components: dict, text: str) -> str:
    """One turn in this process, the way the CLI command runs one.

    For tests that need the composition to stay alive between turns -- a plugin that is
    unloaded between them, a config that changes -- which a fresh ``chat`` invocation
    cannot express.
    """
    import agent as agent_module
    from agent.core.agent import AgentContext
    from agent.core.output import CliOutputSink
    from agent.runtime import RuntimeSessionState, TurnInput
    from agent.runtime.contracts import agent_core_for_components

    sink = CliOutputSink(shared.CONSOLE)
    sink.begin_turn()
    state = RuntimeSessionState(ctx=AgentContext(system_prompt=components["system_prompt"]))
    await agent_core_for_components(components).handle_turn(
        TurnInput.from_text(text, channel_name="cli"), state, sink=sink
    )
    return sink.text if hasattr(sink, "text") else ""


def test_unloading_a_plugin_takes_its_tool_with_it(home):
    """Registrations are effects: a plugin that goes away leaves nothing behind.

    Loaded and unloaded in a **running** composition, with a turn either side.  The
    first turn's request offers the plugin's tool; after ``unload`` the second turn's
    does not, and the composition still answers -- which is the whole promise of "the
    plugin is the unit of removal", and something a fresh process per turn cannot show.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "with the plugin"}, {"text": "without the plugin"})
    cfg = write_config(home, provider)
    write_plugin(home, "v1")
    plugin_dir = home / "plugins" / "recorder"
    try:

        async def run() -> tuple[list[dict], bool]:
            components = await agent_module._build_components_async(cfg, announce=False)
            try:
                catalog = components["plugin_catalog"]
                assert "recorder" in catalog.get_loaded_names_for_directory(
                    home / "plugins" / "recorder"
                ) or "recorder" in catalog._plugins, "the user plugin must be loaded"
                await _turn_in_process(components, "use the echo tool")
                catalog.unload_plugin("recorder")
                await _turn_in_process(components, "and now")
                return provider.requests, catalog.failed_report()
            finally:
                await agent_module._close_components(components)

        requests, failures = asyncio.run(run())

        assert not failures, failures
        first = [t["function"]["name"] for t in requests[0].get("tools", [])]
        second = [t["function"]["name"] for t in requests[1].get("tools", [])]
        assert "plugin_echo" in first, first
        assert "plugin_echo" not in second, (
            "the unloaded plugin's tool must be gone from the registry: "
            f"{second[:12]}"
        )
        assert len(second) == len(first) - 1, (len(first), len(second))
    finally:
        provider.close()


# ── 11. The approval a turn waits for ───────────────────────────────────────


async def _ask_with_approval(socket, text: str, message_id: str, decision: str):
    """Send a message and answer the first approval prompt with *decision*.

    Returns ``(events, prompts)``: every event up to the turn's completion, and the
    approval prompts that were answered along the way.  A turn that never prompts simply
    completes, which is what makes a wrongly-not-prompting build visible here.
    """
    import asyncio
    import json as _json

    await socket.send(
        _json.dumps({"type": "message", "text": text, "message_id": message_id})
    )
    events: list[dict] = []
    prompts: list[dict] = []
    while True:
        event = _json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
        events.append(event)
        if event.get("type") == "confirm_request":
            prompts.append(event)
            await socket.send(
                _json.dumps(
                    {
                        "type": "confirm_response",
                        "confirmation_token": event.get("confirmation_token", ""),
                        "decision": decision,
                    }
                )
            )
            continue
        if event.get("type") == "turn_complete":
            return events, prompts


def test_a_risky_tool_call_waits_for_the_browser_and_runs_when_approved(home):
    """The only place a turn stops and waits for the person.

    ``echo hi | sh`` is classified high-risk at the default ``permissions.shell_level:
    ask``, so the shell tool issues a pending confirmation and the sink asks the connected
    browser.  The reply travels back over the same socket, is redeemed against the token
    the server minted (a forged one is not accepted -- that is the pending record's job),
    and the command then runs.

    Evidence: the prompt arrives with a token, the turn completes, and the model's second
    request carries a tool result -- so the call was executed rather than short-circuited
    into the "confirmation required" reply a non-interactive sink would produce.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "tool": (
                "shell",
                {
                    "command": "echo hi | sh",
                    "intent": "check that a risky command waits for approval",
                },
            )
        },
        {"text": "approved and done"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run():
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-approval/stream"
                ) as socket:
                    return await _ask_with_approval(
                        socket, "check the shell", "m1", "allow_once"
                    )
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events, prompts = asyncio.run(run())

        assert prompts, (
            "a high-risk command must ask the browser: "
            f"{[event['type'] for event in events]}"
        )
        prompt = prompts[0]
        assert prompt["name"] == "shell", prompt
        assert prompt.get("confirmation_token"), prompt
        assert _answer(events) == "approved and done", events

        tool_result = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_result, [m.get("role") for m in provider.requests[-1]["messages"]]
        content = str(tool_result[-1].get("content", ""))
        assert '"ok": true' in content, (
            "the call must have been executed, not answered with the structured "
            f"'confirmation required' reply: {content[:300]}"
        )
    finally:
        provider.close()


def test_a_declined_approval_leaves_the_command_unrun(home):
    """The other half: a refusal is honoured, and the turn still finishes.

    A decline must reach the tool as "no", not as a timeout or an exception -- the model
    has to be able to read the outcome and continue.  Asserted on the tool result the
    model received, because that is what the next step of the turn is based on.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "tool": (
                "shell",
                {
                    "command": "echo hi | sh",
                    "intent": "check that a refused command does not run",
                },
            )
        },
        {"text": "understood, not running it"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run():
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-decline/stream"
                ) as socket:
                    return await _ask_with_approval(
                        socket, "run it anyway", "m1", "deny"
                    )
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events, prompts = asyncio.run(run())

        assert prompts, [event["type"] for event in events]
        assert _answer(events) == "understood, not running it", events

        tool_result = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_result, [m.get("role") for m in provider.requests[-1]["messages"]]
        content = str(tool_result[-1].get("content", ""))
        assert '"ok": true' not in content, (
            f"a declined command must not have run: {content[:300]}"
        )
        assert "declin" in content.lower() or "confirm" in content.lower(), content[:300]
    finally:
        provider.close()


# ── 12. A tool that comes from an external server ───────────────────────────


class _ScriptedMCP:
    """Stands in for an MCP server over stdio: same client interface, scripted tools.

    The *client* is the boundary here -- everything on this side of it (the composition
    adopting the client, the tools it registers, the agent calling one, the result
    travelling back) is the production path, and that is what the test is about.
    """

    instances: list["_ScriptedMCP"] = []

    def __init__(self, registry) -> None:
        self.registry = registry
        self.calls: list[dict] = []
        self.closed = False
        type(self).instances.append(self)

    async def connect_from_config(self, config, extra_env=None):
        def echo(word: str = "") -> dict:
            self.calls.append({"word": word})
            return {"echoed": word}

        self.registry.register(
            "mcp_echo",
            "Echo a word back, from a server that is not part of this process.",
            {
                "type": "object",
                "properties": {"word": {"type": "string"}},
                "required": [],
            },
            echo,
            source="mcp:scripted",
        )

    async def close(self) -> None:
        self.closed = True

    def status_summary(self) -> dict:
        return {
            "configured_servers": 1,
            "connected_servers": 1,
            "failed_servers": 0,
            "registered_tools": 1,
        }


def test_a_tool_from_an_mcp_server_can_be_called_by_the_agent(home, monkeypatch):
    """An external server's tool, offered to the model and executed for real.

    The composition adopts the MCP client, the tools it registers join the same registry as
    everything else, and a turn can call one.  Asserted on both ends: the tool is in what
    the model was *offered*, and the server's own record shows it was asked.

    The connection is awaited first, which is what the interactive paths effectively do --
    the composition starts it in the background and goes on (see the note below the test).
    """
    import asyncio

    import agent as agent_module

    _ScriptedMCP.instances.clear()
    # Read off the package, which is where ``bootstrap`` resolves it from -- the
    # documented substitution point for the client class.
    monkeypatch.setattr(agent_module, "MCPClient", _ScriptedMCP)

    provider = scripted(
        {"tool": ("mcp_echo", {"word": "from-mcp"})},
        {"text": "the server said so"},
    )
    cfg = write_config(
        home,
        provider,
        mcp_servers=[{"name": "scripted", "command": "unused"}],
    )
    try:

        async def run() -> None:
            components = await agent_module._build_components_async(cfg, announce=False)
            try:
                await components["mcp_task"]
                await _turn_in_process(components, "use the external tool")
                # Still connected while the composition runs: closing it is the
                # composition's teardown, not something a turn does.
                assert not _ScriptedMCP.instances[-1].closed
            finally:
                await agent_module._close_components(components)

        asyncio.run(run())

        offered = [
            tool["function"]["name"] for tool in provider.requests[0].get("tools", [])
        ]
        assert "mcp_echo" in offered, offered

        assert _ScriptedMCP.instances, "the composition must have built a client"
        client = _ScriptedMCP.instances[-1]
        assert client.calls == [{"word": "from-mcp"}], (
            f"the server must have received the call with its arguments: {client.calls}"
        )
        assert client.closed, "and the composition's teardown closed it"
    finally:
        provider.close()


# ── 13. The task loop, driven as a command ──────────────────────────────────


def test_the_ralph_task_loop_runs_through_the_command_router(home):
    """``/ralph`` runs the task loop in a session, against the composed agent.

    Ralph is the piece with the longest dependency list (seven services) and the one whose
    iterations each drive a *turn* through the agent, so this exercises the plugin
    activating, its store, and a loop that calls the model more than once -- through the
    command router, which is how a person starts it.

    Asserted on the task the store kept as well as on the reply: a loop that ran and did not
    record itself has lost the only durable trace of what it did.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "iteration done"}, {"text": "still done"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> tuple[list[dict], list]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-ralph/stream"
                ) as socket:
                    events = await _ask(
                        socket, "/ralph make the test pass --max 2", "m1"
                    )
                service = components.get("ralph_service")
                tasks = service.list_tasks() if service is not None else []
                return events, tasks
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events, tasks = asyncio.run(run())

        text = " ".join(
            str(event.get("chunk") or event.get("text") or "") for event in events
        )
        assert not [e for e in events if e["type"] == "error"], events
        assert tasks, (
            "the loop must record its task; the reply was: "
            f"{text[:300]}"
        )
        task = tasks[-1]
        assert task.goal == "make the test pass", task
        assert task.max_iterations == 2, task
        assert task.status == "max_iterations_reached", (
            f"two iterations of a goal nothing verifies end here: {task.status}"
        )
        assert task.current_iteration == 2, task
        assert len(provider.requests) >= 2, (
            "the loop must drive a turn per iteration, not one turn for the command: "
            f"{len(provider.requests)} request(s)"
        )
    finally:
        provider.close()


# ── 14. The end of a session, which only the runner's teardown reaches ──────

_SESSION_PLUGIN = '''
import json
import pathlib

HERE = pathlib.Path(__file__).parent


def _note(kind, **fields):
    with (HERE / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **fields}) + "\\n")


def register():
    class SessionRecorder:
        name = "session_recorder"

        def setup(self, ctx):
            from agent.core.event_domains import SessionFinished, TurnCompleted

            # One type each, subscribed through the context -- the routed form.  A session
            # ending is published on the composition's session bus by whoever dispatches
            # it, so this also pins that the two agree about where it lives.
            ctx.on(TurnCompleted, lambda event: _note("turn", response=event.response))
            ctx.on(
                SessionFinished,
                lambda event: _note(
                    "session_end",
                    turns=event.turn_count,
                    messages=len(event.messages),
                    session_id=event.session_id,
                ),
            )

    return SessionRecorder()
'''


class _HandlerOnlyChannel:
    """A channel that only holds the handler, so the runner's own teardown runs.

    ``ChannelRunner._run_channel`` is where a stopped channel's sessions are closed and
    their ends announced -- in a ``finally``, after ``channel.start`` returns.  Reaching it
    needs a real ``runner.run()`` and a channel that simply waits, which is what this is:
    a transport, not a server.
    """

    def __init__(self) -> None:
        self.handler = None
        self._stopped = None

    async def start(self, handler) -> None:
        import asyncio

        self.handler = handler
        self._stopped = asyncio.Event()
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    def create_sink(self, msg):
        from agent.channels.web import WebOutputSink

        return WebOutputSink(collect=True)


def test_a_sessions_end_reaches_the_plugins_watching_for_it(home):
    """The session boundary: announced once, to subscribers, with what the session was.

    ``fire_session_end`` is the single dispatch point, and it was publishing on the process
    bus while the plugin subscribed through the domain -- an event constructed, emitted and
    heard by nobody.  Nothing drove the path end to end, because it is only reached by the
    runner's teardown, which is to say by a channel *stopping*.

    Asserted on the plugin's own record: the turn it saw, and the session end carrying the
    transcript and the turn count.
    """
    import asyncio

    import agent as agent_module
    from agent.channels.base import ChannelRunner, IncomingMessage
    from agent.channels.web import WebOutputSink

    provider = scripted({"text": "noted"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    plugin_dir = home / "plugins" / "session_recorder"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(_SESSION_PLUGIN, encoding="utf-8")
    try:

        async def run() -> list[dict]:
            components = await agent_module._build_components_async(cfg, announce=False)
            channel = _HandlerOnlyChannel()
            runner = ChannelRunner([channel], components, cfg)
            serving = asyncio.create_task(runner.run())
            try:
                for _ in range(200):
                    if channel.handler is not None:
                        break
                    await asyncio.sleep(0.01)
                assert channel.handler is not None, "the runner never started the channel"

                await channel.handler(
                    IncomingMessage(
                        text="remember this",
                        session_id="session-end-one",
                        channel_name="web",
                        metadata={"message_id": "m1"},
                    ),
                    WebOutputSink(collect=True),
                )
                await channel.stop()
                await serving
            finally:
                await agent_module._close_components(components)
            return plugin_notes(plugin_dir)

        notes = asyncio.run(run())

        kinds = [note["kind"] for note in notes]
        assert "turn" in kinds, notes
        endings = [note for note in notes if note["kind"] == "session_end"]
        assert len(endings) == 1, (
            "a session's end is announced exactly once, at the boundary: "
            f"{kinds}"
        )
        ending = endings[0]
        assert ending["turns"] >= 1, ending
        assert ending["messages"] >= 2, (
            f"the transcript the plugin is handed must contain the turn: {ending}"
        )
        assert ending["session_id"] == "session-end-one", ending
    finally:
        provider.close()


# ── 15. A second message while the first turn is still running ──────────────


def test_a_second_message_sent_mid_turn_still_gets_its_own_answer(home):
    """Typing again before the first answer arrives must not lose either one.

    The coordinator owns per-session serialisation: the second message is queued against the
    running turn, gets its own sink and its own completion, and is answered in order.  The
    failure this guards against is the quiet one -- a message accepted into a queue and never
    run, which from the browser looks exactly like "nothing happened".

    The first reply is told to take its time, so the second message really does arrive during
    it.  The *assertion* does not depend on that timing: whatever the interleaving, both
    messages must be answered, in order.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {"text": "first answer", "delay": 0.4},
        {"text": "second answer"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[dict]:
            import json as _json
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-queue/stream"
                ) as socket:
                    await socket.send(
                        _json.dumps(
                            {"type": "message", "text": "first", "message_id": "m1"}
                        )
                    )
                    await socket.send(
                        _json.dumps(
                            {"type": "message", "text": "second", "message_id": "m2"}
                        )
                    )
                    events: list[dict] = []
                    completions = 0
                    while completions < 2:
                        event = _json.loads(
                            await asyncio.wait_for(socket.recv(), timeout=30)
                        )
                        events.append(event)
                        if event.get("type") == "turn_complete":
                            completions += 1
                    return events
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        answers = [
            str(event.get("text", ""))
            for event in events
            if event.get("type") == "turn_complete"
        ]
        assert answers == ["first answer", "second answer"], (
            f"both messages must be answered, in order: {answers}"
        )
        assert len(provider.requests) == 2, len(provider.requests)
        assert not [e for e in events if e["type"] == "error"], events
    finally:
        provider.close()
