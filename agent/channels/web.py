"""HTTP/WebSocket channel for a browser frontend.

The web frontend is just another channel: ``WebChannel`` implements the same
``Channel`` contract as Feishu, reuses the channel-runner message handler, and
bridges ``OutputSink`` events to JSON events over a WebSocket (or an HTTP
response for non-streaming calls).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent import shared
from agent.channels.base import Channel, IncomingMessage
from agent.core.output import OutputSink
from agent.session_service import SessionService


@dataclass
class WebConfig:
    """Configuration for the web channel.

    ``cors_origins`` accepts a list (from JSON config) or tuple; the channel
    normalises it to a tuple internally.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    auth_token: str = ""
    cors_origins: tuple[Any, ...] = field(default_factory=tuple)


class WebOutputSink(OutputSink):
    """Serialises agent output events to JSON for a web client.

    With ``websocket`` set, events are queued and sent as JSON objects by a
    background sender task; call ``flush()`` after each turn and ``close()``
    when the socket is done.  Without a websocket, events are simply collected
    for the non-streaming HTTP path.
    """

    def __init__(self, websocket: Any = None, *, collect: bool = False) -> None:
        self._websocket = websocket
        self._collect = collect or websocket is None
        self._events: list[dict[str, Any]] = []
        self._attachments: list[str] = []
        self._turn_complete_emitted = False
        self.full_text = ""
        self._queue: Optional[asyncio.Queue[dict[str, Any] | None]] = None
        self._sender_task: Optional[asyncio.Task[Any]] = None
        if not self._collect:
            self._queue = asyncio.Queue()
            self._sender_task = asyncio.create_task(self._sender())

    # -- OutputSink overrides -------------------------------------------------

    def on_stream_chunk(self, chunk: str) -> None:
        self.full_text += chunk
        self._emit({"type": "stream_chunk", "chunk": chunk})

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        if full_text:
            self.full_text = full_text
        self._turn_complete_emitted = True
        self._emit(
            {
                "type": "turn_complete",
                "text": self.full_text,
                "tool_calls": list(tool_calls),
            }
        )

    def mark_turn_start(self) -> None:
        """Reset the per-message completion marker used by WebSocket delivery."""
        self._turn_complete_emitted = False

    @property
    def turn_complete_emitted(self) -> bool:
        return self._turn_complete_emitted

    def on_tool_start(self, name: str, inputs: dict) -> None:
        self._emit({"type": "tool_start", "name": name, "inputs": dict(inputs)})

    def on_tool_end(self, name: str, result: str) -> None:
        self._emit({"type": "tool_end", "name": name, "result": result})

    def on_tool_progress(self, name: str, progress: Any) -> None:
        self._emit(
            {
                "type": "tool_progress",
                "name": name,
                "progress": _jsonable(progress),
            }
        )

    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._emit({"type": "tool_blocked", "name": name, "reason": reason})

    def on_status(self, text: str, *, level: str = "info") -> None:
        self._emit({"type": "status", "text": text, "level": level})

    def on_error(self, error: str) -> None:
        self._emit({"type": "error", "error": error})

    def on_info(self, content: Any) -> None:
        self._emit({"type": "info", "content": _jsonable(content)})

    def on_notification(self, title: str, body: str, *, level: str = "info") -> None:
        self._emit({"type": "notification", "title": title, "body": body, "level": level})

    def on_subagent_event(self, event: Any) -> None:
        self._emit(
            {
                "type": "subagent_event",
                "agent": str(getattr(event, "agent", "") or ""),
                "event": str(getattr(event, "event", "") or ""),
                "detail": _jsonable(getattr(event, "detail", "") or ""),
            }
        )

    def on_heartbeat(
        self,
        *,
        elapsed_seconds: float,
        current_op: str,
        op_detail: str = "",
        pending_messages: int = 0,
    ) -> None:
        self._emit(
            {
                "type": "heartbeat",
                "elapsed_seconds": elapsed_seconds,
                "current_op": current_op,
                "op_detail": op_detail,
                "pending_messages": pending_messages,
            }
        )

    def queue_attachment(self, path: Any) -> object | None:
        """Queue an attachment path for the next attachment flush."""
        self._attachments.append(str(path))
        return str(path)

    async def flush_attachments(self) -> None:
        """Emit queued attachment events and clear the queue."""
        if not self._attachments:
            return
        for path in self._attachments:
            name = Path(path).name
            self._emit(
                {"type": "attachment", "path": path, "name": name}
            )
        self._attachments.clear()
        await self.flush()

    def defer_temporary_attachment_cleanup(self, receipt: object) -> bool:
        return True

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope: Any,
    ) -> bool:
        """Ask a connected web client for approval; deny when unavailable."""
        if self._collect or self._websocket is None:
            return False
        self._emit(
            {
                "type": "confirm_request",
                "name": name,
                "command": command,
                "risk_level": risk_level,
                "reason": reason,
                "confirmation_token": confirmation_token,
            }
        )
        await self.flush()
        try:
            data = await asyncio.wait_for(self._websocket.receive_json(), timeout=120)
        except Exception:
            return False
        if not isinstance(data, dict) or data.get("type") != "confirm_response":
            return False
        return bool(data.get("approved", False))

    # -- collection helpers ----------------------------------------------------

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    async def flush(self, timeout: float = 30.0) -> None:
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        if self._queue is not None and self._sender_task is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._sender_task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._sender_task.cancel()
        self._sender_task = None
        self._queue = None

    # -- internals ------------------------------------------------------------

    def _emit(self, event: dict[str, Any]) -> None:
        if self._collect:
            self._events.append(event)
            return
        if self._queue is not None:
            self._queue.put_nowait(event)

    async def _sender(self) -> None:
        assert self._queue is not None and self._websocket is not None
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            try:
                await self._websocket.send_json(event)
            except Exception:
                pass
            finally:
                self._queue.task_done()


def _mask_api_keys(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``cfg`` with provider API keys masked for display."""
    import copy

    masked = copy.deepcopy(cfg)
    providers = masked.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict) and provider.get("api_key"):
                provider["api_key"] = "******"
    return masked


def _restore_masked_api_keys(new_cfg: dict[str, Any], current_cfg: dict[str, Any]) -> None:
    """Keep existing API keys when the UI saved the masked placeholder back."""
    providers = new_cfg.get("providers")
    current_providers = current_cfg.get("providers")
    if not isinstance(providers, dict) or not isinstance(current_providers, dict):
        return
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        if provider.get("api_key") == "******":
            current = current_providers.get(name)
            if isinstance(current, dict) and current.get("api_key"):
                provider["api_key"] = current["api_key"]


def _jsonable(value: Any) -> Any:
    """Best-effort conversion for values that are not JSON-native."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)
    return str(value)


class WebChannel(Channel):
    """Starlette-based channel for a browser frontend."""

    def __init__(self, config: WebConfig) -> None:
        self._config = config
        self._handler: Optional[
            Callable[[IncomingMessage, OutputSink], Any]
        ] = None
        self._sessions: dict[str, Any] = {}
        self._components: dict[str, Any] = {}
        self._session_service: Optional[SessionService] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._server_task: Optional[asyncio.Task[Any]] = None
        self._app = self._build_app()

    @property
    def app(self) -> Any:
        return self._app

    @staticmethod
    def _web_dist_dir() -> Path:
        """Resolve the frontend bundle for source checkouts and packages.

        Source builds live in ``frontend/dist``; packaged installations use
        the bundled ``agent/_builtin/web/dist`` copy refreshed by the release
        build command.
        """
        package_dist = Path(__file__).resolve().parent.parent / "_builtin" / "web" / "dist"
        source_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
        if (source_dist / "index.html").is_file():
            return source_dist
        return package_dist

    def bind_runtime(
        self,
        sessions: dict[str, Any],
        components: dict[str, Any],
    ) -> None:
        """Receive the channel runner's live-session dict and components."""
        self._sessions = sessions
        self._components = components
        base_ctx_mgr = components.get("context_manager")
        store = getattr(base_ctx_mgr, "store", None)
        self._session_service = SessionService(
            store=store,
            live_states=sessions,
            store_factory=components.get("session_store_factory"),
        )

    def _service(self) -> SessionService:
        if self._session_service is None:
            self._session_service = SessionService(live_states=self._sessions)
        return self._session_service

    def _authorized(self, request: Any) -> bool:
        token = self._config.auth_token.strip()
        if not token:
            return True
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip() == token
        if request.headers.get("x-auth-token", "").strip() == token:
            return True
        query = getattr(request, "query_params", None)
        if query is not None:
            try:
                if str(query.get("token", "") or "").strip() == token:
                    return True
            except Exception:
                pass
        return False

    def _ensure_router(self) -> Any:
        router = self._components.get("command_router")
        if router is not None:
            return router
        from agent.commands import CommandRouter, register_builtin_commands

        router = CommandRouter(skill_catalog=self._components.get("skill_catalog"))
        register_builtin_commands(router)
        plugin_catalog = self._components.get("plugin_catalog")
        if plugin_catalog is not None and hasattr(
            plugin_catalog, "get_slash_commands"
        ):
            router.register_plugin_catalog(plugin_catalog)
        self._components["command_router"] = router
        return router

    # -- HTTP endpoints -------------------------------------------------------

    async def _index(self, request: Any) -> Any:
        from pathlib import Path
        from starlette.responses import HTMLResponse

        dist = self._web_dist_dir() / "index.html"
        if dist.is_file():
            return HTMLResponse(dist.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h1>Simple Agent API</h1>"
            "<p>This is the API endpoint for web integrations.</p>"
            "</body></html>"
        )

    async def _health(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True, "home": str(shared.AGENT_HOME)})

    async def _commands(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            router = self._ensure_router()
            descriptors = router.visible_descriptors("web")
            commands = [
                {
                    "name": d.name,
                    "aliases": list(d.aliases),
                    "usage": d.usage or f"/{d.name}",
                    "description": d.description,
                    "concurrency": d.concurrency,
                }
                for d in descriptors
            ]
        except Exception:
            commands = []
        return JSONResponse({"commands": commands})

    async def _config_get(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.config import load_config

        cfg, _ = load_config()
        masked = _mask_api_keys(cfg)
        return JSONResponse({"config": masked})

    async def _config_save(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        from agent.config import load_config, save_config

        current, _ = load_config()
        new_cfg = body.get("config", body)
        if not isinstance(new_cfg, dict):
            return JSONResponse({"error": "config must be a dict"}, status_code=400)
        _restore_masked_api_keys(new_cfg, current)
        try:
            save_config(new_cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        return JSONResponse({"ok": True})

    async def _plugins(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        plugins: list[dict[str, Any]] = []
        catalog = self._components.get("plugin_catalog")
        if catalog is not None:
            list_plugins = getattr(catalog, "list_plugins", None)
            if callable(list_plugins):
                try:
                    for meta in list_plugins():
                        plugins.append(
                            {
                                "name": getattr(meta, "name", "unknown"),
                                "version": getattr(meta, "version", "") or "",
                                "description": getattr(meta, "description", "") or "",
                                "source": getattr(meta, "source", "") or "",
                                "enabled": bool(getattr(meta, "enabled", True)),
                            }
                        )
                except Exception:
                    plugins = []
        return JSONResponse({"plugins": plugins})

    async def _skills(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        skills: list[dict[str, Any]] = []
        catalog = self._components.get("skill_catalog")
        if catalog is not None:
            list_skills = getattr(catalog, "list_skills", None)
            if callable(list_skills):
                try:
                    for bundle in list_skills():
                        skills.append(
                            {
                                "id": getattr(bundle, "id", "unknown"),
                                "name": getattr(bundle, "name", "") or "",
                                "description": getattr(bundle, "description", "") or "",
                                "source": getattr(bundle, "source", "") or "",
                                "user_invocable": bool(
                                    getattr(bundle, "user_invocable", True)
                                ),
                                "disable_model_invocation": bool(
                                    getattr(bundle, "disable_model_invocation", False)
                                ),
                                "path": str(getattr(bundle, "path", "") or ""),
                            }
                        )
                except Exception:
                    skills = []
        return JSONResponse({"skills": skills})

    async def _context_stats(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        base_ctx_mgr = self._components.get("context_manager")
        stats = {}
        if base_ctx_mgr is not None:
            stats_method = getattr(base_ctx_mgr, "stats", None)
            if callable(stats_method):
                try:
                    stats = stats_method()
                except Exception:
                    stats = {}
        return JSONResponse({"stats": stats})

    async def _rename_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        title = str(body.get("title", "") or "").strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        session_id = request.path_params["session_id"]
        ok = self._service().rename_session(session_id, title)
        if not ok:
            return JSONResponse({"error": "rename failed"}, status_code=500)
        return JSONResponse({"ok": True, "title": title})

    async def _toggle_plugin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        plugin_name = request.path_params["plugin_name"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or "enabled" not in body:
            return JSONResponse({"error": "enabled is required"}, status_code=400)
        desired = bool(body["enabled"])

        from agent.config import load_config, save_config

        cfg, _ = load_config()
        plugins_cfg = cfg.get("plugins")
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {}
            cfg["plugins"] = plugins_cfg
        plugin_entry = plugins_cfg.get(plugin_name)
        if not isinstance(plugin_entry, dict):
            plugin_entry = {}
            plugins_cfg[plugin_name] = plugin_entry
        plugin_entry["enabled"] = desired
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

        catalog = self._components.get("plugin_catalog")
        if catalog is not None:
            try:
                catalog._plugin_config = plugins_cfg  # noqa: SLF001
            except Exception:
                pass
            reload = getattr(catalog, "reload", None)
            if callable(reload):
                try:
                    self._ensure_router()
                    await reload(self._components)
                except Exception as exc:
                    return JSONResponse(
                        {"error": f"reload failed: {exc}", "saved": True},
                        status_code=500,
                    )
        return JSONResponse({"ok": True, "name": plugin_name, "enabled": desired})

    async def _delete_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        ok = self._service().delete_session(session_id)
        if not ok:
            return JSONResponse({"error": "delete failed"}, status_code=500)
        return JSONResponse({"ok": True})

    async def _reveal_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.path_params["session_id"]
        target = self._service().session_data_path(session_id)
        if target is None:
            return JSONResponse(
                {"error": "session data is not available"}, status_code=404
            )

        # Reuse the same platform-aware launcher as the CLI /reveal command.
        # It receives an argv list, so session ids can never become shell code.
        from agent.commands.builtin import _launch_path

        result = await _launch_path(target, reveal=True)
        response_text = str(getattr(result, "response_text", "") or "")
        if getattr(result, "level", "info") == "error":
            return JSONResponse({"error": response_text or "unable to reveal session"}, status_code=500)
        return JSONResponse({"ok": True, "path": str(target)})

    async def _file(self, request: Any) -> Any:
        from starlette.responses import FileResponse, JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw_path = request.query_params.get("path", "")
        if not raw_path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        candidate = Path(raw_path).expanduser()
        try:
            resolved = candidate.resolve()
            allowed_roots = [shared.AGENT_HOME.resolve()]
            try:
                allowed_roots.extend(
                    home.resolve()
                    for home in Path.home().glob(".agent-*")
                    if (home / ".web-session").is_file()
                )
            except OSError:
                pass
            if not any(
                resolved == root or root in resolved.parents
                for root in allowed_roots
            ):
                return JSONResponse({"error": "forbidden path"}, status_code=403)
            if not resolved.is_file():
                return JSONResponse({"error": "file not found"}, status_code=404)
        except OSError:
            return JSONResponse({"error": "invalid path"}, status_code=400)
        return FileResponse(resolved)

    async def _list_sessions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"sessions": self._service().list_sessions()})

    async def _create_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = self._service().create_session()
        return JSONResponse({"session_id": session_id})

    async def _get_messages(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        try:
            limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        return JSONResponse(
            {
                "session_id": session_id,
                "messages": self._service().get_messages(session_id, limit=limit),
            }
        )

    async def _get_session_permissions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            PERMISSION_LEVELS,
            ShellAuthorizationScope,
            shell_session_permission_get,
            shell_session_sandbox_get,
        )
        from agent.security.filesystem_sandbox import SANDBOX_MODES, effective_sandbox_mode
        from agent.config import load_config

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        scope = ShellAuthorizationScope(session_id, "web", "")
        cfg, _ = load_config()
        permissions = cfg.get("permissions") if isinstance(cfg, dict) else {}
        permissions = permissions if isinstance(permissions, dict) else {}
        configured_level = str(permissions.get("shell_level", "ask") or "ask")
        configured_sandbox = str(permissions.get("shell_sandbox", "read_all") or "read_all")
        session_level = shell_session_permission_get(scope)
        session_sandbox = shell_session_sandbox_get(scope)
        level = session_level or configured_level
        sandbox = effective_sandbox_mode(session_sandbox or configured_sandbox, level)
        return JSONResponse({
            "session_id": session_id,
            "level": level,
            "sandbox": sandbox,
            "session_level": session_level,
            "session_sandbox": session_sandbox,
            "levels": list(PERMISSION_LEVELS),
            "sandbox_modes": list(SANDBOX_MODES),
        })

    async def _patch_session_permissions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            PERMISSION_LEVELS,
            ShellAuthorizationScope,
            shell_session_permission_set,
            shell_session_sandbox_set,
        )
        from agent.security.filesystem_sandbox import SANDBOX_MODES

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        level = str(body.get("level", "") or "").strip().casefold()
        sandbox = str(body.get("sandbox", "") or "").strip().casefold()
        if level and level not in PERMISSION_LEVELS:
            return JSONResponse({"error": "invalid permission level"}, status_code=400)
        if sandbox and sandbox not in SANDBOX_MODES:
            return JSONResponse({"error": "invalid sandbox mode"}, status_code=400)
        if sandbox == "none" and level not in ("", "full"):
            return JSONResponse({"error": "sandbox none requires full permission"}, status_code=400)
        session_id = str(request.path_params["session_id"])
        scope = ShellAuthorizationScope(session_id, "web", "")
        if level:
            shell_session_permission_set(scope, level)
        if sandbox:
            shell_session_sandbox_set(scope, sandbox)
        return await self._get_session_permissions(request)

    async def _post_message(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self._handler is None:
            return JSONResponse({"error": "channel not started"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)

        text = str(body.get("text", "") or "").strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        session_id = request.path_params["session_id"]
        message_id = str(body.get("message_id") or uuid.uuid4().hex)
        sink = WebOutputSink(collect=True)
        await self._handle_text(session_id, text, sink, message_id=message_id)
        return JSONResponse(
            {
                "session_id": session_id,
                "message_id": message_id,
                "text": sink.full_text,
                "events": sink.events,
            }
        )

    async def _stream(self, websocket: Any) -> None:
        await websocket.accept()
        if not self._authorized(websocket):
            await websocket.close(code=4401)
            return
        if self._handler is None:
            await websocket.send_json({"type": "error", "error": "channel not started"})
            await websocket.close(code=1011)
            return

        session_id = websocket.path_params["session_id"]
        sink = WebOutputSink(websocket=websocket)
        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                if data.get("type") != "message":
                    continue
                text = str(data.get("text", "") or "").strip()
                if not text:
                    await websocket.send_json(
                        {"type": "error", "error": "text is required"}
                    )
                    continue
                message_id = str(data.get("message_id") or uuid.uuid4().hex)
                sink.mark_turn_start()
                await self._handle_text(
                    session_id, text, sink, message_id=message_id
                )
                await sink.flush()
                if not sink.turn_complete_emitted:
                    sink.on_turn_complete("", [])
                    await sink.flush()
        except Exception:
            pass
        finally:
            await sink.close()

    async def _handle_text(
        self,
        session_id: str,
        text: str,
        sink: OutputSink,
        *,
        message_id: str,
    ) -> None:
        assert self._handler is not None
        msg = IncomingMessage(
            text=text,
            session_id=session_id,
            channel_name="web",
            metadata={"message_id": message_id},
        )
        await self._handler(msg, sink)

    # -- Channel contract -------------------------------------------------------

    async def start(
        self,
        handler: Callable[[IncomingMessage, OutputSink], Any],
    ) -> None:
        """Start the HTTP/WebSocket server and block until ``stop`` is called."""
        self._handler = handler
        self._stop_event = asyncio.Event()

        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "uvicorn is not installed. Run: uv sync --extra web"
            ) from exc

        config = uvicorn.Config(
            self._app,
            host=self._config.host or "127.0.0.1",
            port=int(self._config.port or 8787),
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())
        try:
            await self._stop_event.wait()
        finally:
            server.should_exit = True
            if self._server_task is not None and not self._server_task.done():
                await self._server_task
            self._server_task = None
            self._handler = None

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    def create_sink(self, msg: IncomingMessage) -> OutputSink:
        return WebOutputSink(collect=True)

    # -- app factory ------------------------------------------------------------

    def _build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware
        from starlette.routing import Mount, Route, WebSocketRoute
        from starlette.staticfiles import StaticFiles

        dist_dir = self._web_dist_dir()

        middleware: list[Middleware] = []
        cors_origins = list(self._config.cors_origins or ())
        if cors_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=cors_origins,
                    allow_methods=["*"],
                    allow_headers=["Authorization", "X-Auth-Token", "Content-Type"],
                )
            )

        routes: list[Any] = [
            Route("/", self._index, methods=["GET"]),
            Route("/api/health", self._health, methods=["GET"]),
            Route("/api/commands", self._commands, methods=["GET"]),
            Route("/api/config", self._config_get, methods=["GET"]),
            Route("/api/config", self._config_save, methods=["POST"]),
            Route("/api/plugins", self._plugins, methods=["GET"]),
            Route("/api/skills", self._skills, methods=["GET"]),
            Route("/api/context", self._context_stats, methods=["GET"]),
            Route(
                "/api/sessions/{session_id}",
                self._rename_session,
                methods=["PATCH"],
            ),
            Route(
                "/api/sessions/{session_id}",
                self._delete_session,
                methods=["DELETE"],
            ),
            Route(
                "/api/sessions/{session_id}/reveal",
                self._reveal_session,
                methods=["POST"],
            ),
            Route(
                "/api/plugins/{plugin_name}/toggle",
                self._toggle_plugin,
                methods=["POST"],
            ),
            Route("/api/files", self._file, methods=["GET"]),
            Route("/api/sessions", self._list_sessions, methods=["GET"]),
            Route("/api/sessions", self._create_session, methods=["POST"]),
            Route(
                "/api/sessions/{session_id}/messages",
                self._get_messages,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/messages",
                self._post_message,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/permissions",
                self._get_session_permissions,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/permissions",
                self._patch_session_permissions,
                methods=["PATCH"],
            ),
            WebSocketRoute(
                "/api/sessions/{session_id}/stream",
                self._stream,
            ),
        ]
        if dist_dir.is_dir():
            routes.append(Mount("/assets", StaticFiles(directory=dist_dir / "assets")))
        return Starlette(routes=routes, middleware=middleware)
