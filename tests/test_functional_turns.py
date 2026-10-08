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
                if reply.get("status"):
                    # A provider that refuses: the wire format a real one uses, so the
                    # SDK's own error class is what the product's error handling sees.
                    payload = json.dumps(
                        reply.get(
                            "body",
                            {"error": {"message": "refused", "type": "invalid_request_error"}},
                        )
                    ).encode()
                    self.send_response(int(reply["status"]))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    if reply.get("retry_after") is not None:
                        self.send_header("Retry-After", str(reply["retry_after"]))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if body.get("stream"):
                    self._stream(reply, body)
                else:
                    self._json(reply, body)

            def _text_for(self, reply: dict, body: dict) -> str:
                if reply.get("echo_model"):
                    return f"answer for {body.get('model', '?')}"
                return str(reply.get("text", ""))

            def _json(self, reply: dict, body: dict) -> None:
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": self._text_for(reply, body),
                }
                finish = str(reply.get("finish", "stop"))
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

            def _stream(self, reply: dict, body: dict) -> None:
                # A reply may be told to take its time, which is how a test gets a turn
                # that is still running when the next message arrives.
                time.sleep(float(reply.get("delay", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if reply.get("no_tools") and reply.get("finish") == "tool_calls":
                    # A gateway that announces a tool call and sends none.  Nothing in the
                    # protocol forbids it, and the loop must not wedge on it.
                    self.wfile.write(_chunk({}, finish="tool_calls"))
                elif reply.get("tool"):
                    name, arguments = reply["tool"]
                    # A real provider *streams* the arguments: the name arrives first and
                    # the JSON body arrives in fragments, which the client has to
                    # concatenate.  Sending it whole would leave that assembly untested --
                    # and a client that replaced instead of appended would look identical
                    # on a fake that never split anything.
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
                    for fragment in _fragments(json.dumps(arguments)):
                        self.wfile.write(
                            _chunk(
                                {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": fragment},
                                        }
                                    ]
                                }
                            )
                        )
                    self.wfile.write(_chunk({}, finish="tool_calls"))
                else:
                    self.wfile.write(_chunk({"role": "assistant", "content": ""}))
                    for piece in _pieces(self._text_for(reply, body)):
                        self.wfile.write(_chunk({"content": piece}))
                    # A provider that ran out of room says "length", and the product has a
                    # continuation path for it; a provider that finished says "stop".
                    self.wfile.write(_chunk({}, finish=str(reply.get("finish", "stop"))))
                # Two things a real gateway does that a tidy fake would not: it may report
                # no usage at all (``stream_options`` is not universally supported), and it
                # may close the stream without the ``[DONE]`` terminator.
                if not reply.get("no_usage"):
                    self.wfile.write(_usage_chunk())
                if not reply.get("no_done"):
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


def _fragments(payload: str, parts: int = 3) -> list[str]:
    """Split a tool call's JSON body the way a provider streams it.

    In pieces that do not line up with the JSON's structure, because a real one makes no
    such promise: ``{"pa`` / ``th": "a.tx`` / ``t"}`` is what the wire actually carries.
    """
    if len(payload) < parts:
        return [payload]
    size = max(1, len(payload) // parts)
    return [payload[index : index + size] for index in range(0, len(payload), size)]


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


async def _drain_after_completion(socket, events: list[dict], grace: float = 0.5) -> None:
    """Collect what follows a completion, without waiting long for it.

    The product reports a failed turn as a completion *and then* the reason, so a reader
    that stops at the completion sees an empty answer and no error -- which is what "the
    page did nothing" looks like from the client side.  A short grace rather than the full
    timeout keeps the other tests fast.
    """
    import asyncio
    import json as _json

    while True:
        try:
            event = _json.loads(
                await asyncio.wait_for(socket.recv(), timeout=grace)
            )
        except asyncio.TimeoutError:
            return
        events.append(event)


def _shown_text(events: list[dict]) -> str:
    """Everything a person would read on the page, including status lines.

    A command that answers with a ``status`` and an empty completion is invisible to a
    reader that only looks at chunk/text -- which would let "the reply names it" pass on a
    build that said nothing.
    """
    return " ".join(
        str(event.get("chunk") or event.get("text") or event.get("error") or "")
        for event in events
    )


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


# ── 16. Choosing a folder for one conversation ──────────────────────────────


def test_switching_a_sessions_workspace_moves_where_its_tools_write(home, tmp_path):
    """``/workspace <dir>`` is how a person pins a conversation to a folder.

    The command has to do two things that only a running session can show: persist the
    choice where a restart reads it (``.session.json``), and repoint the *live* runtime --
    this session's registry fork, its file policy and its file service -- so the next turn
    works in the new directory.  A command that only did the first would look right until
    the next message.

    Asserted by where the next turn's file lands, and by what the manifest says: the two
    halves are separate statements and only one of them survives a restart.
    """
    import asyncio

    import agent as agent_module

    # The command itself does not call the model, so the first scripted reply is the
    # first *turn*'s -- which is the one that writes the probe.
    provider = scripted(
        {
            "tool": (
                "write_file",
                {
                    "root": "workspace",
                    "path": "in-the-chosen-folder.txt",
                    "mode": "create",
                    "content": "here",
                },
            )
        },
        {"text": "written in the folder"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    # A folder the session may write in, and one it may not: the policy follows the choice.
    chosen = tmp_path / "chosen-folder"
    chosen.mkdir()
    try:

        async def run() -> dict:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-workspace/stream"
                ) as socket:
                    before = await _ask(socket, f"/workspace {chosen}", "m1")
                    after = await _ask(socket, "write the probe there", "m2")
                return {"before": before, "after": after}
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        reply = " ".join(
            str(event.get("chunk") or event.get("text") or "")
            for event in events["before"]
        )
        assert not [e for e in events["before"] if e["type"] == "error"], events["before"]
        assert "工作" in reply or "workspace" in reply.lower() or "文件夹" in reply, (
            f"the command must report something about the folder: {reply[:300]}"
        )
        assert _answer(events["after"]) == "written in the folder", events["after"]

        written = list(chosen.rglob("in-the-chosen-folder.txt"))
        assert len(written) == 1, (
            "the next turn must work in the folder that was chosen: "
            f"{[str(p) for p in written]}"
        )

        manifest = home / "web" / "sessions" / "web-workspace" / ".session.json"
        assert manifest.is_file(), manifest
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert payload.get("workspace_root") == str(chosen), payload
    finally:
        provider.close()


# ── 17. A skill the agent loads on demand ───────────────────────────────────


def _write_skill(home_dir, skill_id: str, marker: str) -> None:
    """A skill bundle on disk: the shape the catalog discovers."""
    skill_dir = home_dir / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: A skill that answers with {marker}.\n"
        "user-invocable: true\n"
        "---\n\n"
        f"# {skill_id}\n\nThe passphrase is {marker}.\n",
        encoding="utf-8",
    )


def test_a_skill_is_offered_and_its_instructions_reach_the_model(home):
    """A skill bundle is discovered, advertised, and loaded into the conversation.

    Two separate facts, and the second is the one that matters: the catalog's listing has to
    reach the system prompt (otherwise the model never knows the skill exists *this session*),
    and ``activate_skill``'s body has to come back as a tool result (otherwise loading it does
    nothing).  The catalog is a per-session service and the prompt is assembled per turn, so
    this is a composition path rather than a file-parsing one.
    """
    provider = scripted(
        {"tool": ("activate_skill", {"skill_name": "passphrase-skill"})},
        {"text": "I have the passphrase"},
    )
    write_config(home, provider)
    _write_skill(home, "passphrase-skill", "swordfish")
    try:
        from typer.testing import CliRunner

        from agent.cli import app

        result = CliRunner().invoke(app, ["chat", "load the passphrase skill"])

        assert result.exit_code == 0, result.output
        assert "I have the passphrase" in result.output, result.output

        first = provider.requests[0]
        system_prompt = "\n".join(
            str(message.get("content") or "")
            for message in first["messages"]
            if message.get("role") == "system"
        )
        assert "passphrase-skill" in system_prompt, (
            "the skill catalog must be advertised to the model in the prompt: "
            f"{system_prompt[-400:]}"
        )

        tool_result = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_result, [m.get("role") for m in provider.requests[-1]["messages"]]
        assert "swordfish" in str(tool_result[-1].get("content", "")), (
            "activating the skill must bring its instructions back: "
            f"{str(tool_result[-1])[:300]}"
        )
    finally:
        provider.close()


# ── 18. Clearing a conversation, and only that conversation ─────────────────


def test_clearing_a_session_forgets_its_turns_and_nothing_else(home):
    """``/clear`` empties this session's context -- not the process's, not the memory.

    The context a session clears is the one its ``RuntimeSessionState`` holds, which is
    built per session by the channel runner; the durable memory and the palace live
    elsewhere and must survive.  Asserted on the *next request*, because that is what the
    model is actually handed: the earlier exchange must be gone from it.

    A command that cleared the wrong object would still reply "已清除", which is why the
    reply is not the evidence here.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "first reply"}, {"text": "second reply"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> tuple[list[dict], list[dict]]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-clear/stream"
                ) as socket:
                    first = await _ask(socket, "remember the word pineapple", "m1")
                    cleared = await _ask(socket, "/clear", "m2")
                    second = await _ask(socket, "what did I ask you to remember", "m3")
                return first, cleared, second
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        first, cleared, second = asyncio.run(run())

        assert _answer(first) == "first reply", first
        assert not [e for e in cleared if e["type"] == "error"], cleared
        assert _answer(second) == "second reply", second

        # The turn before the command is in the request; the one after it is not.
        after_clear = json.dumps(provider.requests[-1]["messages"], ensure_ascii=False)
        assert "pineapple" not in after_clear, (
            "the cleared conversation must be gone from what the model is handed: "
            f"{after_clear[:400]}"
        )
    finally:
        provider.close()


def test_without_clearing_the_earlier_turn_is_still_in_the_request(home):
    """Control for the test above: the conversation *is* carried, until it is cleared.

    Without this, "pineapple is absent after /clear" would also pass on a build that never
    carried the conversation at all -- a validation that cannot fail, which is the thing
    this file exists to avoid.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "first reply"}, {"text": "second reply"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-noclear/stream"
                ) as socket:
                    await _ask(socket, "remember the word pineapple", "m1")
                    return await _ask(socket, "and now", "m2")
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        assert _answer(events) == "second reply", events
        carried = json.dumps(provider.requests[-1]["messages"], ensure_ascii=False)
        assert "pineapple" in carried, (
            "a conversation that was not cleared must still be in the request: "
            f"{carried[:400]}"
        )
    finally:
        provider.close()


# ── 19. Two sessions talking at once ────────────────────────────────────────


def test_two_sessions_answer_their_own_requests_concurrently(home):
    """Two conversations in one gateway, running at the same time.

    Each session carries its own runtime -- its own agent, registry, context and model
    override -- and the turns run concurrently in one event loop, which is what a browser
    with two tabs does.  The failure this guards against is not exotic: anything the
    per-session path shares by accident (a sink, a context variable, a bus, a client) makes
    one conversation's answer appear in the other, or sends an answer to the wrong model.

    Distinguished by the model each session asked for -- the endpoint echoes it -- because
    both answers come from one process and one scripted queue.  The gather is inside the
    coroutine, so both connections are opened on the loop that serves them.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"echo_model": True}, {"echo_model": True})
    cfg = write_config(
        home,
        provider,
        channels=WEB_CHANNELS,
        providers={
            "scripted": {
                "api_format": "openai",
                "api_key": "scripted-key",
                "base_url": provider.base_url,
                "default_model": "alpha-model",
                "models": ["alpha-model", "beta-model"],
            }
        },
    )
    try:

        async def everything() -> list[list[dict]]:
            import json as _json
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:

                async def one(session: str, model: str, message_id: str) -> list[dict]:
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}/api/sessions/{session}/stream"
                    ) as socket:
                        await socket.send(
                            _json.dumps(
                                {
                                    "type": "message",
                                    "text": f"which model are you, {session}?",
                                    "message_id": message_id,
                                    "model": model,
                                }
                            )
                        )
                        events: list[dict] = []
                        while True:
                            event = _json.loads(
                                await asyncio.wait_for(socket.recv(), timeout=30)
                            )
                            events.append(event)
                            if event.get("type") == "turn_complete":
                                return events

                return list(
                    await asyncio.gather(
                        one("web-alpha", "alpha-model", "m1"),
                        one("web-beta", "beta-model", "m2"),
                    )
                )
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        alpha, beta = asyncio.run(everything())

        assert _answer(alpha) == "answer for alpha-model", alpha
        assert _answer(beta) == "answer for beta-model", beta
        assert not [e for e in alpha + beta if e["type"] == "error"], alpha + beta

        asked = [request.get("model") for request in provider.requests]
        assert sorted(asked) == ["alpha-model", "beta-model"], asked
    finally:
        provider.close()


# ── 20. A provider that refuses ─────────────────────────────────────────────


def test_an_unfunded_account_is_reported_as_such_and_not_retried(home):
    """The user's own provider's refusal, driven end to end.

    ``opencode.ai`` answers a spent account with

        402 {'error': {'message': 'Upstream request failed: Insufficient account funds'}}

    which is a *permanent* condition: retrying lands in the same closed account.  The
    product knows this distinction -- the retry loop refuses to replay it, the scheduler
    refuses to schedule a retry, and the agent has a message that tells the user to switch
    provider or top up instead of quoting the body -- but all three read one vocabulary
    list, and this phrasing was not in it.

    So this asserts the three things that vocabulary decides, from the outside: the request
    is made **once**, and what the user is told is the actionable sentence rather than the
    provider's body.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "status": 402,
            "body": {
                "error": {
                    "message": "Upstream request failed: Insufficient account funds",
                    "type": "server_error",
                }
            },
        }
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-unfunded/stream"
                ) as socket:
                    events = await _ask(socket, "say hello", "m1")
                    await _drain_after_completion(socket, events)
                    return events
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        assert len(provider.requests) == 1, (
            "an account that cannot serve must not be asked twice: "
            f"{len(provider.requests)} request(s)"
        )
        shown = " ".join(
            str(event.get("error") or event.get("text") or event.get("chunk") or "")
            for event in events
        )
        assert "账户不可用" in shown or "配额" in shown, (
            "the user must be told the account is the problem, not handed the raw body: "
            f"{shown[:300]!r} (events: {[e['type'] for e in events]})"
        )
        assert "Insufficient account funds" in shown, (
            "the provider's own words are still quoted, so the user can see what it "
            f"actually said: {shown[:300]!r}"
        )
    finally:
        provider.close()


def test_a_transient_rate_limit_is_retried_and_the_turn_answers(home):
    """Control: the same machinery must still replay what *is* worth replaying.

    A 429 is the case the retry exists for, and the two decisions share one predicate --
    so a vocabulary that grew too eager would be visible here as a single request and a
    failure instead of an answer.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "status": 429,
            "retry_after": 0,
            "body": {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
        },
        {"text": "answered after the retry"},
    )
    cfg = write_config(
        home,
        provider,
        channels=WEB_CHANNELS,
        llm_max_retries=2,
        llm_retry_base_delay=0.1,
    )
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-retry/stream"
                ) as socket:
                    return await _ask(socket, "say hello", "m1")
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        assert _answer(events) == "answered after the retry", events
        assert len(provider.requests) == 2, len(provider.requests)
    finally:
        provider.close()


# ── 21. An answer the provider ran out of room for ──────────────────────────


def test_a_truncated_answer_is_continued_not_left_half_written(home):
    """``finish_reason: length`` is an everyday event with a path written for it.

    The provider stops mid-sentence, so the turn asks it to continue and merges the two
    halves.  The continuation carries the turn's own tools and the continuation prompt; a
    build that dropped either would still produce text, which is why the *request* is
    asserted and not only the merged answer.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {"text": "the answer so far", "finish": "length"},
        {"text": " and the rest of it"},
    )
    cfg = write_config(
        home,
        provider,
        channels=WEB_CHANNELS,
        max_truncation_continuations=2,
    )
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-truncated/stream"
                ) as socket:
                    return await _ask(socket, "tell me something long", "m1")
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        answer = _answer(events)
        assert "the answer so far" in answer and "the rest of it" in answer, (
            f"both halves must reach the user: {answer!r}"
        )
        assert len(provider.requests) == 2, (
            f"one continuation, so two calls: {len(provider.requests)}"
        )
        continuation = json.dumps(
            provider.requests[1]["messages"], ensure_ascii=False
        )
        assert "Continue exactly from where you left off" in continuation, (
            "the continuation must use the product's own prompt: "
            f"{continuation[-400:]}"
        )
        offered = provider.requests[1].get("tools") or []
        assert offered, "and it must carry the turn's tools, or the answer cannot use one"
    finally:
        provider.close()


def test_an_answer_that_stays_truncated_says_so(home):
    """Control for the other outcome: when continuing does not help, the user is told.

    Every continuation comes back truncated, so the loop gives up -- and the failure has to
    be *visible*.  Silence here would be a half-written answer presented as a whole one.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {"text": "still cut off", "finish": "length"},
        {"text": "still cut off", "finish": "length"},
        {"text": "still cut off", "finish": "length"},
    )
    cfg = write_config(
        home,
        provider,
        channels=WEB_CHANNELS,
        max_truncation_continuations=1,
    )
    try:

        async def run() -> list[dict]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-still-truncated/stream"
                ) as socket:
                    events = await _ask(socket, "tell me something long", "m1")
                    await _drain_after_completion(socket, events)
                    return events
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        shown = " ".join(
            str(event.get("error") or event.get("text") or event.get("chunk") or "")
            for event in events
        )
        assert "truncated" in shown.lower(), (
            "a response that never finished must be reported as such: "
            f"{shown[:300]!r} (events: {[e['type'] for e in events]})"
        )
    finally:
        provider.close()


# ── 22. A wire that is not as tidy as the protocol ──────────────────────────


async def _ask_once(home, cfg, session: str, text: str = "say hello") -> list[dict]:
    """One turn on a session of its own, returning every event it produced."""
    import agent as agent_module

    components = await agent_module._build_components_async(cfg, announce=False)
    server, serving, port = await _start_web_channel(components, cfg)
    try:
        import websockets

        async with websockets.connect(
            f"ws://127.0.0.1:{port}/api/sessions/{session}/stream"
        ) as socket:
            return await _ask(socket, text, "m1")
    finally:
        server.should_exit = True
        await serving
        await agent_module._close_components(components)


def test_a_stream_that_never_terminates_still_delivers_its_answer(home):
    """``data: [DONE]`` is a convention, not a guarantee.

    A gateway that closes the stream after the last chunk is behaving acceptably, and the
    answer must still arrive.  A fake that always sent the terminator could not tell the
    difference between a client that handles a closed stream and one that waits for a line
    that never comes -- the second looks exactly like "the page did nothing".
    """
    import asyncio

    provider = scripted({"text": "answer without a terminator", "no_done": True})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:
        events = asyncio.run(_ask_once(home, cfg, "web-no-done"))
        assert _answer(events) == "answer without a terminator", events
        assert not [e for e in events if e["type"] == "error"], events
    finally:
        provider.close()


def test_a_stream_with_no_usage_report_still_delivers_its_answer(home):
    """``stream_options`` is not universally supported, and usage must not be required.

    The transport asks for a usage report because a stream that reports nothing cannot be
    recorded; a gateway that ignores the request must cost the *accounting*, not the answer.
    """
    import asyncio

    provider = scripted({"text": "answer without usage", "no_usage": True})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:
        events = asyncio.run(_ask_once(home, cfg, "web-no-usage"))
        assert _answer(events) == "answer without usage", events
        assert not [e for e in events if e["type"] == "error"], events
    finally:
        provider.close()


def test_a_tool_call_announced_and_never_sent_does_not_wedge_the_turn(home):
    """``finish_reason: tool_calls`` with no tool calls: the loop has to end somehow.

    Nothing in the protocol forbids it, and the failure mode is the worst kind -- a turn
    that waits for a step that will never arrive.  Asserted as "the turn finishes and says
    something", which is the property; what it says is the product's business.
    """
    import asyncio

    provider = scripted({"text": "", "finish": "tool_calls", "no_tools": True})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:
        events = asyncio.run(_ask_once(home, cfg, "web-no-tools"))
        kinds = [event["type"] for event in events]
        assert "turn_complete" in kinds, kinds
        shown = " ".join(
            str(event.get("error") or event.get("text") or event.get("chunk") or "")
            for event in events
        ).strip()
        assert shown, (
            "a turn that produced nothing must say so rather than complete silently: "
            f"{kinds}"
        )
    finally:
        provider.close()


# ── 23. The one path that repairs a conversation instead of reporting it ────


def test_a_content_filter_rejection_is_recovered_without_the_user_seeing_it(home):
    """A provider's policy rejection rolls the turn back and re-asks, once.

    This is the only mechanism in the product that *repairs* a refusal rather than
    reporting it: the offending round is rolled back, the tool results are replaced with
    safe summaries, and the call is retried.  A slip here shows up as the user being handed
    the provider's policy message for a request that could have succeeded.

    Driven with the phrasing the predicate matches, because that is the path as written;
    the phrasings it does *not* match are recorded in the status document as a candidate
    rather than guessed at here.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "status": 400,
            "body": {
                "error": {
                    "message": "Content Exists Risk",
                    "type": "invalid_request_error",
                    "code": "content_filter",
                }
            },
        },
        {"text": "recovered answer"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:
        events = asyncio.run(_ask_once(home, cfg, "web-filtered"))
        assert _answer(events) == "recovered answer", events
        assert not [e for e in events if e["type"] == "error"], events
        assert len(provider.requests) >= 2, (
            "the recovery is a re-ask, so the provider sees the request again: "
            f"{len(provider.requests)}"
        )
    finally:
        provider.close()


# ── 24. An image the user attaches ──────────────────────────────────────────

#: The smallest valid PNG: one transparent pixel.  Real bytes, because the product reads
#: the file, base64-encodes it and puts it on the wire -- a stand-in string would test none
#: of that.
_ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)


def test_an_attached_image_reaches_the_model_as_an_image_block(home):
    """Drag a file into the chat: upload, attach, and the request carries the image.

    Four things have to line up, and each is somewhere different: the upload endpoint stores
    the file under the session, the message's attachment list is parsed back into a
    ``MessageAttachment``, the turn decides an image is a *direct* block only when the
    provider claims vision support, and the transport encodes it for its own wire format.

    Asserted on the wire -- the base64 the model would actually receive is the file's bytes --
    because that is where a path that "attached something" but sent its name instead would
    show up.
    """
    import asyncio
    import base64

    import agent as agent_module

    provider = scripted({"text": "I can see it"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    cfg["providers"]["scripted"]["supports_vision"] = True
    (home / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:

        async def run() -> tuple[list[dict], dict]:
            import httpx
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"http://127.0.0.1:{port}/api/sessions/web-attach/attachments",
                        files={"files": ("pixel.png", _ONE_PIXEL_PNG, "image/png")},
                    )
                assert response.status_code == 200, response.text
                uploaded = response.json()["attachments"]
                assert len(uploaded) == 1, uploaded
                assert uploaded[0]["kind"] == "image", uploaded[0]

                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-attach/stream"
                ) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "type": "message",
                                "text": "what is in this image?",
                                "message_id": "m1",
                                "attachments": uploaded,
                            }
                        )
                    )
                    events: list[dict] = []
                    while True:
                        event = json.loads(
                            await asyncio.wait_for(socket.recv(), timeout=30)
                        )
                        events.append(event)
                        if event.get("type") == "turn_complete":
                            break
                return events, uploaded[0]
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events, _uploaded = asyncio.run(run())

        assert _answer(events) == "I can see it", events
        assert not [e for e in events if e["type"] == "error"], events

        sent = json.dumps(provider.requests[0]["messages"])
        expected = base64.b64encode(_ONE_PIXEL_PNG).decode()
        assert "image_url" in sent, (
            f"the request must carry an image block: {sent[:400]}"
        )
        assert expected in sent, (
            "and the block must carry the file's own bytes, base64-encoded: "
            f"{sent[:400]}"
        )
        assert "data:image/png;base64," in sent, sent[:400]
    finally:
        provider.close()


def test_an_image_sent_to_a_provider_without_vision_is_described_instead(home):
    """Control: an attachment must not vanish when the model cannot see.

    The turn decides what an attachment becomes: a direct image block for a provider that
    claims vision, and otherwise a line of text describing the file.  Silently dropping it
    would be the quiet failure -- the user attached something and the model never learned it
    existed.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "noted the file"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    cfg["providers"]["scripted"]["supports_vision"] = False
    (home / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:

        async def run() -> list[dict]:
            import httpx
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"http://127.0.0.1:{port}/api/sessions/web-novision/attachments",
                        files={"files": ("pixel.png", _ONE_PIXEL_PNG, "image/png")},
                    )
                uploaded = response.json()["attachments"]
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-novision/stream"
                ) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "type": "message",
                                "text": "what is this?",
                                "message_id": "m1",
                                "attachments": uploaded,
                            }
                        )
                    )
                    events: list[dict] = []
                    while True:
                        event = json.loads(
                            await asyncio.wait_for(socket.recv(), timeout=30)
                        )
                        events.append(event)
                        if event.get("type") == "turn_complete":
                            break
                return events
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        events = asyncio.run(run())

        assert _answer(events) == "noted the file", events
        sent = json.dumps(provider.requests[0]["messages"], ensure_ascii=False)
        assert "image_url" not in sent, "no vision, no image block"
        assert "pixel.png" in sent, (
            "the attachment must be described in words instead of disappearing: "
            f"{sent[:400]}"
        )
    finally:
        provider.close()


# ── 25. Picking a model from inside the conversation ────────────────────────


def test_the_model_command_switches_the_session_and_refuses_a_typo(home):
    """``/model`` is the per-session switch; the settings page is the process-wide one.

    Measured before this test existed: the command replied "Switched to model: beta-model
    (session only)" and the next turn went to **alpha** -- because every message from the web
    channel carries the key with a ``None`` value (the client sent no per-message model), and
    the runner read "the key is present" as "this message chooses".  So a message that says
    nothing about the model cleared the choice, every time, forever.

    Both halves are asserted: the switch takes effect, and a refusal leaves it alone.  A
    failed switch that half-applied would send the next turn to a model the endpoint does not
    serve, which arrives as a 400 listing the endpoint's own models rather than as anything
    about the typo.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"echo_model": True}, {"echo_model": True}, {"echo_model": True})
    cfg = write_config(
        home,
        provider,
        channels=WEB_CHANNELS,
        providers={
            "scripted": {
                "api_format": "openai",
                "api_key": "scripted-key",
                "base_url": provider.base_url,
                "default_model": "alpha-model",
                "models": ["alpha-model", "beta-model"],
            }
        },
    )
    try:

        async def run() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-model/stream"
                ) as socket:
                    switched = await _ask(socket, "/model beta-model", "m1")
                    after_switch = await _ask(socket, "which model are you?", "m2")
                    typo = await _ask(socket, "/model gamma-model", "m3")
                    after_typo = await _ask(socket, "and now?", "m4")
                return switched, after_switch, typo, after_typo
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        switched, after_switch, typo, after_typo = asyncio.run(run())

        assert "beta-model" in _shown_text(switched), switched
        assert _answer(after_switch) == "answer for beta-model", (
            "the switch must reach the next turn's request -- that is the whole command: "
            f"{after_switch}"
        )
        assert "gamma-model" in _shown_text(typo), (
            f"the refusal must name what was asked for: {typo}"
        )
        assert "alpha-model" in _shown_text(typo) and "beta-model" in _shown_text(typo), (
            f"and what is available, so the typo is fixable from the reply: {_shown_text(typo)}"
        )
        assert _answer(after_typo) == "answer for beta-model", (
            "and a refused switch must not move the session off the last good choice: "
            f"{after_typo}"
        )
        assert [request.get("model") for request in provider.requests] == [
            "beta-model",
            "beta-model",
        ], [request.get("model") for request in provider.requests]
    finally:
        provider.close()


# ── 26. Allowing a command for good ─────────────────────────────────────────


def test_allowing_a_command_takes_effect_on_the_next_turn(home):
    """``/allow`` writes the pre-approved list; the *next* shell call must read it.

    The switch spans four things: the command writes the config file and applies the list to
    the session's registry context, the tool call merges that context onto the registry its
    implementation reads, the classifier consults the list before the risky-command branches,
    and the prompt never appears.

    Measured with the command that *is* gated first (a prompt, refused), because a command
    that never prompts would make the second half pass for the wrong reason -- this is the
    product's own "it was confirmed, now it is not" transition, and the interesting direction
    is the one that could silently keep prompting.
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {
            "tool": (
                "shell",
                {
                    "command": "echo hi | sh",
                    "intent": "check the pre-approved list takes effect",
                },
            )
        },
        {"text": "first turn done"},
        {
            "tool": (
                "shell",
                {
                    "command": "echo hi | sh",
                    "intent": "the same command, now pre-approved",
                },
            )
        },
        {"text": "second turn done"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run():
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-allow/stream"
                ) as socket:
                    first, first_prompts = await _ask_with_approval(
                        socket, "run it", "m1", "deny"
                    )
                    allowed = await _ask(socket, "/allow echo hi | sh", "m2")
                    second, second_prompts = await _ask_with_approval(
                        socket, "run it again", "m3", "deny"
                    )
                return first, first_prompts, allowed, second, second_prompts
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        first, first_prompts, allowed, second, second_prompts = asyncio.run(run())

        assert first_prompts, (
            "the command must be gated before it is allowed, or the second half proves "
            f"nothing: {[e['type'] for e in first]}"
        )
        # The reply is markdown, so the pipe comes back escaped ("echo hi \| sh"); the
        # command itself is what has to be in there, unescaped.
        assert "echo hi | sh" in _shown_text(allowed).replace("\\", ""), allowed
        assert second_prompts == [], (
            "a pre-approved command must not ask again: "
            f"{[e['type'] for e in second]}"
        )

        tool_result = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_result, [m.get("role") for m in provider.requests[-1]["messages"]]
        content = str(tool_result[-1].get("content", ""))
        assert "requires_confirmation" not in content, (
            f"and it must have been allowed rather than re-gated: {content[:300]}"
        )

        stored = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert "echo hi | sh" in (stored.get("shell_allowed_commands") or []), (
            "the command promises persistence, so the file must carry it: "
            f"{stored.get('shell_allowed_commands')}"
        )
    finally:
        provider.close()


# ── 27. Auto-approving a session ────────────────────────────────────────────


def test_auto_approving_a_session_stops_the_questions(home):
    """``/auto-approve session on`` is keyed by session, and the tool has to find that key.

    The command stores a permission level under ``(session_id, channel, user_id)``; the shell
    classifier looks it up under a scope derived *later*, from the active agent's metadata.
    Two derivations of one key -- and if they disagree the user's "always allow this session"
    silently does nothing, which is the shape of the last defect found here.

    The command chosen is gated at the default level and *allowed* at the elevated one, and it
    writes zero bytes, so approving it runs nothing worth running.  The first turn is the
    control: the same command must ask before the setting exists.
    """
    import asyncio

    import agent as agent_module

    command = "dd if=/dev/zero of=/tmp/simple-autoapprove.bin bs=1 count=0"
    provider = scripted(
        {"tool": ("shell", {"command": command, "intent": "prove the gate is closed first"})},
        {"text": "first turn done"},
        {"tool": ("shell", {"command": command, "intent": "prove the gate opened"})},
        {"text": "second turn done"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run():
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-autoapprove/stream"
                ) as socket:
                    before = await _ask_with_approval(socket, "run it", "m1", "deny")
                    enabled = await _ask(socket, "/auto-approve session on", "m2")
                    after = await _ask_with_approval(socket, "run it again", "m3", "deny")
                return before, enabled, after
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        (_first_events, first_prompts), enabled, (second_events, second_prompts) = (
            asyncio.run(run())
        )

        assert first_prompts, (
            "control: this command must be gated before the setting exists, or the "
            f"second half proves nothing: {_shown_text(_first_events)}"
        )
        assert "自动放行" in _shown_text(enabled), enabled
        assert second_prompts == [], (
            "a session that auto-approves must not be asked again: "
            f"{_shown_text(second_events)}"
        )
        tool_result = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_result, [m.get("role") for m in provider.requests[-1]["messages"]]
        content = str(tool_result[-1].get("content", ""))
        assert "requires_confirmation" not in content, content[:300]
    finally:
        provider.close()


# ── 28. Approving from the conversation, for a sink that cannot ask ─────────


def test_a_gateway_style_approval_is_redeemed_by_a_chat_command(home):
    """The documented path for a sink with no dialog: a token, then ``/confirm``.

    A transport that cannot show a prompt (Feishu, a scheduled run, a collecting sink) is
    supposed to leave a *pending record* and report a token, and the human redeems it with
    ``/confirm <token>``.  Shell's pending tokens are keyed by ``(session_id, channel,
    user_id)`` minted at tool time, and the command derives the same key at command time --
    one key, two derivations, which is the shape that produced the last defect found here.

    Driven through a channel that holds the handler and collects sink events instead of
    showing them, because that is what makes the sink non-interactive: the prompt path is
    never available, so the fallback is the only way this turn can be approved.
    """
    import asyncio

    import agent as agent_module
    from agent.channels.base import ChannelRunner, IncomingMessage
    from agent.channels.web import WebOutputSink

    command = "dd if=/dev/zero of=/tmp/simple-confirm.bin bs=1 count=0"
    provider = scripted(
        {"tool": ("shell", {"command": command, "intent": "ask, with no dialog available"})},
        {"text": "waiting for approval"},
        {"tool": ("shell", {"command": command, "intent": "the approved retry"})},
        {"text": "done after approval"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[list[dict]]:
            components = await agent_module._build_components_async(cfg, announce=False)
            channel = _HandlerOnlyChannel()
            runner = ChannelRunner([channel], components, cfg)
            serving = asyncio.create_task(runner.run())
            try:
                for _ in range(200):
                    if channel.handler is not None:
                        break
                    await asyncio.sleep(0.01)
                assert channel.handler is not None

                async def turn(text: str, message_id: str) -> list[dict]:
                    sink = WebOutputSink(collect=True)
                    await channel.handler(
                        IncomingMessage(
                            text=text,
                            session_id="session-confirm",
                            channel_name="web",
                            metadata={"message_id": message_id},
                        ),
                        sink,
                    )
                    return sink.events

                first = await turn("run the command", "m1")
                # The token the sink could not ask about travels back to the model in the
                # tool's result; read it from the wire rather than from an internal store.
                tool_results = [
                    message
                    for message in provider.requests[-1]["messages"]
                    if message.get("role") == "tool"
                ]
                payload = json.loads(str(tool_results[-1]["content"]))
                assert payload.get("requires_confirmation") is True, payload
                token = str(payload.get("confirmation_token") or "")
                assert token, payload

                confirmed = await turn(f"/confirm {token}", "m2")
                retried = await turn("try it again", "m3")
                return [first, confirmed, retried]
            finally:
                await channel.stop()
                await serving
                await agent_module._close_components(components)

        first, confirmed, retried = asyncio.run(run())

        assert "requires_confirmation" in json.dumps(first), _shown_text(first)
        shown = _shown_text(confirmed)
        assert "Confirmation accepted" in shown, (
            f"the token must redeem for this session: {shown!r} {confirmed}"
        )
        executed = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert executed, "the retry must have run the tool"
        assert '"ok": true' in str(executed[-1].get("content", "")), (
            "and the approved command must actually run this time: "
            f"{str(executed[-1])[:300]}"
        )
    finally:
        provider.close()


def test_an_unattended_run_refuses_rather_than_leaving_a_token(home):
    """Control for the third answer: a *policy* refusal is not "nobody could ask".

    ``UnattendedOutputSink`` answers "may I?" with a recorded refusal on purpose -- its own
    docstring says so, and the audit trail is the point: an unattended run must not leave a
    token that somebody could redeem later for a command nobody watched.  So it returns
    ``False``, not ``None``, and the shell path retires the token.

    Pinned because the two are one line apart and look interchangeable: a sink that refuses
    by policy and a sink that *cannot* ask both decline, and only one of them should be able
    to have its command approved afterwards.
    """
    import asyncio

    from agent.scheduler.unattended import UnattendedOutputSink

    sink = UnattendedOutputSink()

    approved = asyncio.run(
        sink.on_tool_confirmation(
            "shell",
            command="dd if=/dev/zero of=/tmp/x bs=1 count=0",
            risk_level="high",
            reason="unattended",
            confirmation_token="token-unattended",
            scope=None,
        )
    )

    assert approved is False, (
        "an unattended run refuses by policy; None would leave its token redeemable"
    )
    assert sink.audit.decisions, (
        "and the refusal has to be recorded, or nobody learns it was asked"
    )


# ── 29. Turning a plugin off from the plugins page ──────────────────────────


def test_toggling_a_plugin_off_takes_its_tool_out_of_a_live_session(home):
    """The hot-swap promise, through the HTTP API a person clicks.

    Turning a plugin off writes the config, reloads the catalog, and bumps the config
    revision -- and the *session* has to notice: its runtime was built before the change and
    holds a registry.  Three turns on one socket, with the toggle between them, because the
    failure this guards against is state that only the *next* turn would reveal: the page
    says "disabled" while the conversation keeps offering the tool.
    """
    import asyncio

    import agent as agent_module

    provider = scripted({"text": "one"}, {"text": "two"}, {"text": "three"})
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    write_plugin(home, "v1")
    try:

        async def run() -> tuple[list[dict], dict, list[dict], dict, list[dict]]:
            import httpx
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-toggle/stream"
                ) as socket:
                    before = await _ask(socket, "first", "m1")
                    async with httpx.AsyncClient() as client:
                        off = (
                            await client.post(
                                f"http://127.0.0.1:{port}/api/plugins/recorder/toggle",
                                json={"enabled": False},
                            )
                        ).json()
                    during = await _ask(socket, "second", "m2")
                    async with httpx.AsyncClient() as client:
                        on = (
                            await client.post(
                                f"http://127.0.0.1:{port}/api/plugins/recorder/toggle",
                                json={"enabled": True},
                            )
                        ).json()
                    after = await _ask(socket, "third", "m3")
                return before, off, during, on, after
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        _before, off, _during, on, _after = asyncio.run(run())

        assert off.get("ok") is True, off
        assert on.get("ok") is True, on

        def _offered(index: int) -> list[str]:
            return [
                tool["function"]["name"]
                for tool in provider.requests[index].get("tools", [])
            ]

        assert "plugin_echo" in _offered(0), _offered(0)
        assert "plugin_echo" not in _offered(1), (
            "a disabled plugin's tool must leave the session's next turn: "
            f"{_offered(1)[:12]}"
        )
        assert "plugin_echo" in _offered(2), (
            "and turning it back on must bring it back: "
            f"{_offered(2)[:12]}"
        )
    finally:
        provider.close()


# ── 30. Switching a skill off from the skills page ──────────────────────────


def test_toggling_a_skill_off_changes_the_next_prompt_and_the_tool(home):
    """A skill switch is applied **without** rebuilding the session: the prompt is recomposed.

    The toggle endpoint is explicit that this is the whole of it -- "marking the catalog dirty
    recomposes the prompt before the next turn, for sessions that are already open, without
    rebuilding their runtime underneath them" -- so it exercises a path the plugin toggle does
    not: state that reaches an *open* session through the prompt rather than through a
    rebuild.

    Both halves are asserted: the switched-off skill must leave the prompt (otherwise the
    model keeps being told about a skill it cannot use) and ``activate_skill`` must refuse it
    by name (otherwise the switch is cosmetic).
    """
    import asyncio

    import agent as agent_module

    provider = scripted(
        {"text": "one"},
        {"tool": ("activate_skill", {"skill_name": "passphrase-skill"})},
        {"text": "refused"},
        {"text": "three"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    _write_skill(home, "passphrase-skill", "swordfish")
    try:

        async def run():
            import httpx
            import websockets

            components = await agent_module._build_components_async(cfg, announce=False)
            server, serving, port = await _start_web_channel(components, cfg)
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/sessions/web-skilltoggle/stream"
                ) as socket:
                    first = await _ask(socket, "first", "m1")
                    async with httpx.AsyncClient() as client:
                        off = (
                            await client.post(
                                f"http://127.0.0.1:{port}/api/skills/passphrase-skill/toggle",
                                json={"enabled": False},
                            )
                        ).json()
                    second = await _ask(socket, "load the skill", "m2")
                    async with httpx.AsyncClient() as client:
                        on = (
                            await client.post(
                                f"http://127.0.0.1:{port}/api/skills/passphrase-skill/toggle",
                                json={"enabled": True},
                            )
                        ).json()
                    third = await _ask(socket, "third", "m3")
                return first, off, second, on, third
            finally:
                server.should_exit = True
                await serving
                await agent_module._close_components(components)

        first, off, second, on, third = asyncio.run(run())

        assert off.get("ok") is True, off
        assert on.get("ok") is True, on
        assert _answer(first) == "one", first
        assert _answer(third) == "three", third

        def _prompt(index: int) -> str:
            return "\n".join(
                str(message.get("content") or "")
                for message in provider.requests[index]["messages"]
                if message.get("role") == "system"
            )

        assert "passphrase-skill" in _prompt(0), _prompt(0)[-400:]
        assert "passphrase-skill" not in _prompt(2), (
            "a switched-off skill must leave the prompt of an open session: "
            f"{_prompt(2)[-400:]}"
        )
        assert "passphrase-skill" in _prompt(3), (
            f"and come back when switched on: {_prompt(3)[-400:]}"
        )

        refused = [
            message
            for message in provider.requests[2]["messages"]
            if message.get("role") == "tool"
        ]
        assert refused, [m.get("role") for m in provider.requests[2]["messages"]]
        assert "switched off" in str(refused[-1].get("content", "")), (
            "and asking for it by name must say why it cannot be used: "
            f"{str(refused[-1])[:300]}"
        )
        assert "swordfish" not in str(refused[-1].get("content", "")), (
            "the disabled skill's instructions must not leak through the refusal"
        )
    finally:
        provider.close()


# ── 31. Approving by saying so, for a transport with no dialog ──────────────


def test_saying_approve_redeems_the_one_pending_command(home):
    """The chat-channel approval: a short reply approves the single pending record.

    Written for transports that cannot prompt at the moment of the side effect.  It has its
    own guards worth driving -- it must not run beside an interactive menu, it refuses when
    zero or several records are pending (a bare "yes" cannot choose between them), and on
    success it **rewrites the message** to tell the agent which command was approved.

    It also depends on the pending record surviving the turn that created it, which is the
    defect fixed just before this test: the shell path used to retire the token as it reported
    it, so there was nothing left for this mechanism to redeem.
    """
    import asyncio

    import agent as agent_module
    from agent.channels.base import ChannelRunner, IncomingMessage
    from agent.channels.web import WebOutputSink

    command = "dd if=/dev/zero of=/tmp/simple-phrase.bin bs=1 count=0"
    provider = scripted(
        {"tool": ("shell", {"command": command, "intent": "ask, with no dialog available"})},
        {"text": "waiting"},
        {"text": "ok, retrying"},
        {"tool": ("shell", {"command": command, "intent": "the approved retry"})},
        {"text": "done"},
    )
    cfg = write_config(home, provider, channels=WEB_CHANNELS)
    try:

        async def run() -> list[list[dict]]:
            components = await agent_module._build_components_async(cfg, announce=False)
            channel = _HandlerOnlyChannel()
            runner = ChannelRunner([channel], components, cfg)
            serving = asyncio.create_task(runner.run())
            try:
                for _ in range(200):
                    if channel.handler is not None:
                        break
                    await asyncio.sleep(0.01)
                assert channel.handler is not None

                async def turn(text: str, message_id: str) -> list[dict]:
                    sink = WebOutputSink(collect=True)
                    await channel.handler(
                        IncomingMessage(
                            text=text,
                            session_id="session-phrase",
                            channel_name="web",
                            metadata={"message_id": message_id},
                        ),
                        sink,
                    )
                    return sink.events

                asked = await turn("run the command", "m1")
                approved = await turn("批准", "m2")
                retried = await turn("try again", "m3")
                return [asked, approved, retried]
            finally:
                await channel.stop()
                await serving
                await agent_module._close_components(components)

        asked, approved, retried = asyncio.run(run())

        tool_result = [
            message
            for message in provider.requests[1]["messages"]
            if message.get("role") == "tool"
        ]
        assert "requires_confirmation" in str(tool_result[-1].get("content", "")), (
            "the command has to be pending before the approval can mean anything"
        )

        shown = _shown_text(approved)
        assert command in shown or "dd" in shown, (
            f"the approval must say which command it approved: {shown!r}"
        )
        # The rewrite is the mechanism: the agent is told, in the message, to retry.
        rewritten = json.dumps(provider.requests[2]["messages"], ensure_ascii=False)
        assert "已批准" in rewritten, (
            "the approval has to reach the agent as a message it can act on: "
            f"{rewritten[-400:]}"
        )

        executed = [
            message
            for message in provider.requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert '"ok": true' in str(executed[-1].get("content", "")), (
            f"and the retry must run: {str(executed[-1])[:300]}"
        )
        assert retried, retried
    finally:
        provider.close()
