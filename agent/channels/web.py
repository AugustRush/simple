"""HTTP/WebSocket channel for a browser frontend.

The web frontend is just another channel: ``WebChannel`` implements the same
``Channel`` contract as Feishu, reuses the channel-runner message handler, and
bridges ``OutputSink`` events to JSON events over a WebSocket (or an HTTP
response for non-streaming calls).
"""

from __future__ import annotations
from agent.channels import web_signals
from agent.channels import web_uploads
from agent.channels import web_providers
from agent.channels import web_requests
from agent.channels import web_assets
from agent.channels import web_autonomy, web_home, web_memory

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from agent import shared
from agent.kernel.service import resolve
from agent.channels.base import Channel, IncomingMessage
from agent.channels import web_sink
from agent.channels.web_payloads import (
    attention_payload,
    attention_rows,
    scheduler_output_path,
    scheduler_output_url,
    scheduler_run_payload,
    scheduler_task_payload,
    workflow_payload,
)
from agent.channels.web_sink import WebOutputSink, _normalize_confirm_decision
from agent.channels.web_provider_config import (
    _mask_api_keys,
    _restore_masked_api_keys,
)
from agent.channels.web_session_paths import (
    _web_session_dir,
    _web_session_output_dir,
    _web_session_upload_dir,
)
from agent.commands.composition import ensure_command_router
from agent.core.attachments import MessageAttachment, attachment_kind_for_mime
from agent.core.output import OutputSink
from agent.pathing import path_contains
from agent.scheduler.models import parse_task_signal
from agent.session_service import SessionService

logger = logging.getLogger(__name__)


async def _pick_workspace_directory() -> str | None:
    """Open the host OS folder picker and return the selected directory."""
    import sys

    if sys.platform == "darwin":
        script = (
            'set pickedFolder to choose folder with prompt "选择 Agent 项目文件夹"\n'
            'POSIX path of pickedFolder'
        )
        args = ["osascript", "-e", script]
    elif sys.platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
            "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}"
        )
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        args = ["zenity", "--file-selection", "--directory", "--title=选择 Agent 项目文件夹"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    selected = stdout.decode("utf-8", errors="replace").strip()
    return selected or None


#: How many artifacts one listing returns.
_ARTIFACT_RESPONSE_LIMIT = 500

#: How many directory entries one listing is willing to look at.  The cap above
#: bounds the response; this bounds the work, because a run that cloned a
#: repository can leave a tree nobody wants walked to show 500 names.  Trees up
#: to this size keep the exact order they always had.
_ARTIFACT_WALK_LIMIT = 20_000


def _list_run_artifacts(root: Path, task_id: str, run_id: str) -> list[dict[str, Any]]:
    """Describe the files a run left under *root*, at most the response limit.

    Two rules that the endpoint used to carry inline and unguarded:

    - **An entry that vanishes mid-listing is skipped, not fatal.**  A run may
      still be writing, and a file the walk saw a moment ago is allowed to be
      gone by the time its size is asked for.
    - **The walk is bounded.**  Sorting the whole traversal first materialised
      every path under the run's output to then return at most 500 of them.
    """
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    for path in root.rglob("*"):
        candidates.append(path)
        if len(candidates) >= _ARTIFACT_WALK_LIMIT:
            logger.warning(
                "run artifact listing stopped at %d entries under %s",
                _ARTIFACT_WALK_LIMIT,
                root,
            )
            break
    artifacts: list[dict[str, Any]] = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            size_bytes = path.stat().st_size
        except OSError:
            continue
        artifacts.append(
            {
                "path": relative,
                "name": path.name,
                "mime_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "size_bytes": size_bytes,
                "url": (
                    f"/api/schedules/{quote(task_id, safe='')}/runs/"
                    f"{quote(run_id, safe='')}/artifacts/{quote(relative, safe='/')}"
                ),
            }
        )
        if len(artifacts) >= _ARTIFACT_RESPONSE_LIMIT:
            break
    return artifacts


def _web_session_upload_dir(session_id: str) -> Path:
    """The session's upload directory: where the browser's uploads land."""
    return _web_session_dir(session_id, "uploads")



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




#: Header names whose value is a credential.  A header is just a name, so the
#: only thing that can be said about one is whether its *name* reads like a
#: secret -- which is what a masking pass has to go on, and is why the list is
#: a pattern rather than an enumeration.


def _session_token_budget(cfg: Any) -> int:
    """The configured per-session ceiling, or 0 when there is none.

    Read from the *composition's* config each call rather than from the channel's
    own: ``budget`` is an agent setting, the channel never stores it, and a
    hot-reloaded config has to reach the page without a restart.
    """
    budget = cfg.get("budget") if isinstance(cfg, dict) else None
    if not isinstance(budget, dict):
        return 0
    try:
        return max(0, int(budget.get("max_session_tokens", 0) or 0))
    except (TypeError, ValueError):
        return 0


class WebChannel(Channel):
    """Starlette-based channel for a browser frontend."""

    def __init__(self, config: WebConfig) -> None:
        self._config = config
        self._handler: Optional[
            Callable[[IncomingMessage, OutputSink], Any]
        ] = None
        self._sessions: dict[str, Any] = {}
        self._live_sinks: dict[str, set[WebOutputSink]] = {}
        #: Sockets by session, kept for *idle* pushes. ``_live_sinks`` holds
        #: one sink per in-flight turn and is empty the rest of the time, so a
        #: sentence that arrives between turns (a scheduled run finishing) had
        #: nowhere to go: the report was journalled and never pushed.
        self._live_sockets: dict[str, set[Any]] = {}
        # Pending approval answers, keyed by session then confirmation token.
        # The waiter outlives the connection that created it: a tab switch,
        # a refresh, or a reconnect hands the session a new socket while the
        # approval keeps waiting, and the reply can arrive on any of that
        # session's connections -- see ``wait_for_confirmation`` and the
        # ``confirm_response`` branch of the socket reader.
        self._confirmation_waiters: dict[str, dict[str, asyncio.Future[str]]] = {}
        self._components: dict[str, Any] = {}
        self._session_service: Optional[SessionService] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._server_task: Optional[asyncio.Task[Any]] = None
        #: The uvicorn server, once ``start`` has created it.  ``started`` on it
        #: is the only trustworthy answer to "is this port *ours*" -- a TCP
        #: connect cannot tell this server from whatever else holds the port.
        self._server: Any = None
        self._app = self._build_app()

    @property
    def app(self) -> Any:
        return self._app

    @property
    def public_url(self) -> str:
        """The address a person can paste into a browser, or '' when disabled.

        The gateway opens this at startup, so it has to be the address the
        *reader* can reach rather than the one the server binds: a wildcard
        bind (``0.0.0.0`` / ``::``) is not a URL, and ``0.0.0.0:8787`` in a
        browser is a connection to whatever the OS does with that address.
        Localhost is the right answer for both -- a gateway on another machine
        was already reachable by its own name, and this value is only used for
        the browser launched beside it.
        """
        if not bool(getattr(self._config, "enabled", True)):
            return ""
        host = str(getattr(self._config, "host", "") or "127.0.0.1").strip()
        if host in {"", "0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        try:
            port = int(getattr(self._config, "port", 0) or 8787)
        except (TypeError, ValueError):
            port = 8787
        return f"http://{host}:{port}/"

    @staticmethod
    def _web_dist_dir() -> Path:
        """Resolve the frontend bundle used by the gateway."""
        return web_assets._web_dist_dir()

    def bind_runtime(
        self,
        sessions: dict[str, Any],
        components: dict[str, Any],
    ) -> None:
        """Receive the channel runner's live-session dict and components."""
        self._sessions = sessions
        self._components = components
        # The store **as a service**.  Both real implementations build their context manager
        # from this same service (``session_plugin`` and ``record_only`` both inject
        # ``ltm_store``), so this is the identical object -- and it is the one with a declared
        # contract (``MemoryStore``) instead of an attribute the ``ContextService`` protocol
        # deliberately does not declare.  A replacement memory system provides the service;
        # asking for it is also what keeps this line from depending on the *inside* of
        # another service.
        store = resolve(components, "ltm_store")
        # Runs speak into the conversation. The scheduler publishes; this is
        # the consumer that can both journal the sentence and push it to a page
        # that has the conversation open -- the two halves have to be the same
        # sentence, or a reload shows what the push never did.
        from agent import reports

        reports.subscribe(self._on_run_report)
        self._session_service = SessionService(
            store=store,
            live_states=sessions,
            store_factory=components.get("session_store_factory"),
            runtime_cleanup=lambda sid: (
                self._components.get("session_runtime_cleanup")(sid)
                if callable(self._components.get("session_runtime_cleanup"))
                else None
            ),
        )

    def _on_run_report(self, report: dict) -> None:
        """Journal one run report into the conversation, then push it live.

        The journal first: a push for a row that failed to record would be a
        message the reader can never see again. If nothing is recorded yet (a
        gateway that has never been opened), there is no conversation to speak
        into and the sentence is dropped -- a report with no home is not worth a
        second store to hold it.
        """
        from agent.main_session import read_main_session_id

        session_id = read_main_session_id()
        if not session_id:
            return
        row = self._service().append_report(session_id, report)
        if not row:
            return
        frame = {"type": "run_report", "message": row}
        for websocket in tuple(self._live_sockets.get(session_id, set())):
            try:
                # A schedule on its own loop: this runs inside the scheduler's
                # task, which is not the socket's. `create_task` keeps the send
                # from blocking the run's completion on a slow tab.
                asyncio.create_task(websocket.send_json(frame))
            except Exception:
                logger.debug("failed to push a run report", exc_info=True)
        for live_sink in tuple(self._live_sinks.get(session_id, set())):
            try:
                live_sink.on_report(row)
            except Exception:
                logger.debug("failed to push a run report to a turn's sink", exc_info=True)

    def _service(self) -> SessionService:
        if self._session_service is None:
            self._session_service = SessionService(live_states=self._sessions)
        return self._session_service

    def _scheduler(self) -> Any:
        """The mounted scheduler, or None when this composition has none.

        By its **declared** name.  The scheduler is a mounted plugin and offers itself
        as ``scheduler`` (``agent/runtime/services.py``), but these four routes asked
        for ``scheduler_service`` -- a name nothing has provided since the plugin
        switch -- so ``GET /api/scheduler/health`` answered "offline" on a healthy
        gateway and run-now, retry and cancel all answered 503.  The screen built on
        that read is what the browser showed: a red "调度器离线" and every 立即运行
        button disabled, on a scheduler that was running the whole time.

        Absence is tolerated rather than raised: a composition that mounts no
        scheduler is a real one (a channel-only embed), and the honest answer to it
        is 503, not an exception.
        """
        return self._components.get("scheduler")

    @staticmethod
    def _resolve_model_override(raw_model: Any) -> str | None:
        """Validate a model selected by the browser."""
        return web_requests._resolve_model_override(
            raw_model=raw_model,
        )


    def _authorized(self, request: Any) -> bool:
        return web_providers._authorized(
            request=request, config=self._config
        )


    def _ensure_router(self) -> Any:
        return ensure_command_router(self._components)

    # -- HTTP endpoints -------------------------------------------------------

    async def _index(self, request: Any) -> Any:
        return await web_assets._index(request=request)

    async def _health(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        # Every other /api route checks this.  The one route that does not is a
        # hole in the gate, not a convenience: with a token configured the
        # operator closed the surface deliberately, and liveness is exactly what
        # an unauthenticated caller wants to learn.
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Liveness only.  The response used to carry the absolute AGENT_HOME
        # path, which names the user and the layout of their disk; nothing --
        # frontend, tests, docs -- ever read it.
        return JSONResponse({"ok": True})

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
            catalog = resolve(self._components, "skill_catalog")
            if catalog is not None:
                commands.extend({
                    "name": bundle.id,
                    "aliases": [],
                    "usage": f"/{bundle.id}",
                    "description": bundle.description or "技能",
                    "concurrency": "queue",
                    "kind": "skill",
                } for bundle in catalog.list_skills() if getattr(bundle, "user_invocable", True))
        except Exception:
            commands = []
        return JSONResponse({"commands": commands})

    async def _config_get(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.config import load_config, provider_fields_payload
        from agent.shared import THINKING_EFFORTS

        cfg, _ = load_config()
        masked = _mask_api_keys(cfg)
        # The vocabulary the settings page renders from, sent for the same
        # reason `thinking_efforts` is: the page draws what this backend
        # accepts instead of keeping its own copy of the fields, so a provider
        # key added here appears in the form without a frontend change and one
        # removed here disappears from it.
        return JSONResponse({
            "config": masked,
            "thinking_efforts": list(THINKING_EFFORTS),
            "provider_fields": provider_fields_payload(),
        })

    def _bump_config_revision(self) -> None:
        """Make the next turn rebuild against the config just written."""
        self._components["config_revision"] = (
            int(self._components.get("config_revision", 0)) + 1
        )

    @staticmethod
    def _provider_write_error(provider_name: str, provider: dict[str, Any]) -> Optional[str]:
        """Why this provider block cannot be written, or None."""
        return web_providers._provider_write_error(
            provider_name=provider_name,
            provider=provider, config=self._config
        )


    async def _provider_save(self, request: Any) -> Any:
        """Create or update one provider, leaving every other key alone."""
        return await web_providers._provider_save(
            request=request, config=self._config, bump_config_revision=self._bump_config_revision
        )


    async def _provider_delete(self, request: Any) -> Any:
        """Remove a provider, refusing the two that would brick the config."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params.get("provider_name") or "").strip()
        from agent.config import load_config, save_config

        cfg, _ = load_config()
        providers = cfg.get("providers")
        if not isinstance(providers, dict) or name not in providers:
            return JSONResponse({"error": f"找不到 provider '{name}'"}, status_code=404)
        if str(cfg.get("active_provider") or "") == name:
            return JSONResponse(
                {"error": "这是当前使用的 provider，请先切换到别的再删除"},
                status_code=400,
            )
        if len([k for k in providers if not str(k).startswith("_")]) <= 1:
            return JSONResponse({"error": "至少要保留一个 provider"}, status_code=400)
        providers.pop(name, None)
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        self._bump_config_revision()
        return JSONResponse({"ok": True})

    async def _provider_activate(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params.get("provider_name") or "").strip()
        from agent.config import load_config, save_config

        cfg, _ = load_config()
        providers = cfg.get("providers")
        if not isinstance(providers, dict) or name not in providers:
            return JSONResponse({"error": f"找不到 provider '{name}'"}, status_code=404)
        cfg["active_provider"] = name
        # No top-level ``model`` rewrite here.  One used to follow the switch,
        # re-pointing the legacy key at the new group's default whenever it did
        # not belong to that group -- a band-aid over the resolution order that
        # let a top-level ``model`` outrank the group it was sent to.  The group
        # wins now, so the id a turn carries follows the switch on its own and
        # the key is nothing but tolerated legacy input.
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        self._bump_config_revision()
        # The model the session will actually send with, not the raw `model`
        # key: that key may be absent, in which case the provider's
        # `default_model` is what a turn resolves to -- and the page has to show
        # the id it is about to put on the wire.  `active_model_and_tokens` is
        # the one place that answers "which model does this config name", so
        # this asks it instead of deciding again.
        from agent.config import ModelClientFactory

        effective_model, _tokens = ModelClientFactory.active_model_and_tokens(cfg)
        return JSONResponse(
            {"ok": True, "active_provider": name, "model": effective_model}
        )

    async def _provider_test(self, request: Any) -> Any:
        """Send one minimal request to a provider, through the real path."""
        return await web_providers._provider_test(
            request=request, config=self._config
        )


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
        # Existing turns continue with their captured components; the next turn will
        # rebuild each session runtime against the new global config.  That covers a
        # provider edit, a prompt edit, a shell posture -- everything sessions re-read.
        #
        # A change to the *composition* is not that kind of change: no session rebuild
        # mounts a different memory backend.  So this asks what the save would change,
        # and applies it here when it can -- which is what makes "save the profile, no
        # restart" true from the page.  When a turn is running it says so instead, and
        # the change stays on disk for a later recomposition; the catalog is left
        # running the composition it actually has.
        catalog = resolve(self._components, "plugin_catalog")
        preview = (
            catalog.catch_up(apply=False)
            if callable(getattr(catalog, "catch_up", None))
            else {"restart_required": []}
        )
        owed = list(preview.get("restart_required") or ())
        if owed:
            busy = self._busy_sessions()
            if busy:
                self._bump_config_revision()
                return JSONResponse(
                    {
                        "ok": True,
                        "saved": True,
                        "applied": False,
                        "busy_sessions": busy,
                        "restart_required": owed,
                        "retryable": True,
                    }
                )
            result = await self._recompose_now()
            if not result.get("ok"):
                return JSONResponse(
                    {
                        "ok": True,
                        "saved": True,
                        "applied": False,
                        "restart_required": owed,
                        "composition": result,
                    }
                )
            return JSONResponse(
                {"ok": True, "saved": True, "applied": True, "composition": result}
            )
        self._bump_config_revision()
        return JSONResponse({"ok": True, "saved": True, "applied": True})

    def _scheduler_store(self) -> Any:
        """The scheduler's store, or None when this process has none.

        None is a real answer: a home page with no scheduler is "nothing is
        scheduled", not an error.
        """
        try:
            from agent.scheduler import open_store

            return open_store()
        except Exception:
            return None

    async def _activity_feed(self, request: Any) -> Any:
        sessions: list[dict[str, Any]] = []
        try:
            sessions = list(self._service().list_sessions(limit=50))
        except Exception:
            sessions = []
        scheduler_store = self._scheduler_store()
        try:
            return await web_home._activity_feed(
                request,
                authorized=self._authorized,
                scheduler_store=scheduler_store,
                sessions=sessions,
            )
        finally:
            if scheduler_store is not None:
                scheduler_store.close()

    async def _home_overview(self, request: Any) -> Any:
        # The session list is the same one the sidebar reads, so "recent threads"
        # has one answer in this process rather than two that can disagree.
        sessions: list[dict[str, Any]] = []
        try:
            sessions = list(self._service().list_sessions(limit=20))
        except Exception:
            sessions = []
        scheduler_store = self._scheduler_store()
        try:
            return await web_home._home_overview(
                request,
                authorized=self._authorized,
                scheduler_store=scheduler_store,
                proposal_store=self._proposal_store(),
                sessions=sessions,
            )
        finally:
            if scheduler_store is not None:
                scheduler_store.close()

    async def _autonomy_overview(self, request: Any) -> Any:
        return await web_autonomy._autonomy_overview(
            request, authorized=self._authorized, store=self._proposal_store()
        )

    async def _autonomy_proposals(self, request: Any) -> Any:
        return await web_autonomy._autonomy_proposals(
            request, authorized=self._authorized, store=self._proposal_store()
        )

    async def _autonomy_decide(self, request: Any) -> Any:
        return await web_autonomy._autonomy_decide(
            request, authorized=self._authorized, store=self._proposal_store()
        )

    def _proposal_store(self) -> Any:
        """The proposals store, or None when the patrol has never run.

        A missing database is not an error: it means this process has not
        patrolled yet, and the page's honest answer to that is "nothing yet".
        """
        try:
            from agent.autonomy.store import open_proposal_store

            return open_proposal_store()
        except Exception:
            return None

    async def _plugins(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        plugins: list[dict[str, Any]] = []
        catalog = resolve(self._components, "plugin_catalog")
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
        catalog = resolve(self._components, "skill_catalog")
        if catalog is not None:
            # The listing that manages skills, not the one the model reads:
            # a switched-off skill has to stay visible, otherwise the switch
            # that turned it off could never be found again.
            list_skills = getattr(catalog, "list_all_skills", None)
            is_enabled = getattr(catalog, "is_enabled", None)
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
                                "enabled": (
                                    bool(is_enabled(bundle)) if callable(is_enabled) else True
                                ),
                                "path": str(getattr(bundle, "path", "") or ""),
                            }
                        )
                except Exception:
                    skills = []
        return JSONResponse({"skills": skills})

    async def _upload_attachment(self, request: Any) -> Any:
        return await web_uploads._upload_attachment(
            request=request, authorized=self._authorized
        )


    def _feishu_ready(self) -> bool:
        return web_requests._feishu_ready()


    def _signal_problem(self, name: str) -> Optional[str]:
        """Why *name* can never fire, or None when it can."""
        return web_requests._signal_problem(
            name=name,
        )


    def _edit_context(self):
        """What building a task definition needs from this process.

        Named in one place because two builders ask for it -- a standalone task
        and a workflow's step -- and a step that resolved the folder, the skill
        catalogue or the model list differently from a task would be the same
        definition accepted through one door and refused through another.
        """
        from agent.scheduler import EditContext

        chosen = self._components.get("workspace_root")
        return EditContext(
            chosen_workspace_root=Path(chosen) if chosen else None,
            fallback_workspace_root=Path.cwd().resolve(),
            skill_catalog=resolve(self._components, "skill_catalog"),
            model_validator=self._resolve_model_override,
            feishu_ready=self._feishu_ready,
            signal_problem=self._signal_problem,
        )

    def _trigger_from_body(self, body: dict[str, Any], existing: Any = None):
        """A ``TriggerSpec`` from a body, filling gaps from what was stored."""
        return web_requests._trigger_from_body(
            body=body,
            existing=existing,
        )


    def _delivery_from_body(self, body: dict[str, Any], existing: Any = None):
        """Delivery mode and target, from the body or from what the task had."""
        return web_requests._delivery_from_body(
            body=body,
            existing=existing,
        )


    def _feishu_channel_config(self) -> dict[str, Any]:
        """The global Feishu channel config, as delivery reads it at run time."""
        return web_requests._feishu_channel_config()


    def _schedule_from_body(
        self, body: dict[str, Any], existing: Any = None, *, keep_trigger: bool = False
    ):
        """The whole task a body leaves behind, given the task it edits.

        A field the body does not mention keeps the value the stored task had.
        That is the convention the graph editor has always used -- written down
        at ``_workflow_step_from_body``, where the reason is spelled out -- and
        it is the one place the task editor did not follow it.  Because
        ``update_task`` rewrites every column from the spec it is handed, a
        field the builder left out was not *skipped*: it was written back as
        the dataclass default.  Saving a task's name through the interface
        cleared the files it had declared it would produce, and the run went on
        failing against a promise its own row no longer contained.
        """
        from agent.scheduler import task_from_body

        return task_from_body(
            body,
            existing,
            context=self._edit_context(),
            keep_trigger=keep_trigger,
        )

    def _borrowed_trigger(
        self, raw: dict[str, Any], key: str, existing_by_key: dict[str, Any]
    ) -> Any:
        """The trigger a step asks to take over from another step, if it asks."""
        return web_requests._borrowed_trigger(
            raw=raw,
            key=key,
            existing_by_key=existing_by_key,
        )


    def _workflow_step_from_body(
        self,
        raw: dict[str, Any],
        index: int,
        existing: Any = None,
        borrowed_trigger: Any = None,
    ):
        """One step of a workflow, from its JSON object."""
        return web_requests._workflow_step_from_body(
            raw=raw,
            index=index,
            existing=existing,
            borrowed_trigger=borrowed_trigger,
        )


    def _workflow_from_body(self, body: dict[str, Any], existing: Any = None):
        return web_requests._workflow_from_body(
            body=body,
            existing=existing,
        )


    def _workflow_response(self, store: Any, workflow: Any) -> Any:
        """A workflow plus the state of the tasks behind it."""
        from starlette.responses import JSONResponse

        tasks = store.step_tasks(workflow.id)
        latest = {task.id: store.latest_run(task.id) for task in tasks.values()}
        return JSONResponse(
            {
                "workflow": workflow_payload(
                    workflow,
                    tasks,
                    store.unacknowledged_attention_counts(),
                    latest,
                )
            }
        )

    async def _workflows(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store
        from agent.scheduler.profiles import profile_payloads

        store = open_store()
        try:
            unseen = store.unacknowledged_attention_counts()
            workflows = []
            for workflow in store.list_workflows():
                tasks = store.step_tasks(workflow.id)
                latest = {
                    task.id: store.latest_run(task.id) for task in tasks.values()
                }
                workflows.append(
                    workflow_payload(workflow, tasks, unseen, latest)
                )
            return JSONResponse(
                {
                    "workflows": workflows,
                    # Travelled with the schedules list too, for the same
                    # reason: the interface renders what this backend accepts.
                    "permission_profiles": profile_payloads(),
                }
            )
        finally:
            store.close()

    async def _create_workflow(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import open_store

            workflow = self._workflow_from_body(body)
            store = open_store()
            try:
                created = store.create_workflow(workflow)
                return self._workflow_response(store, created)
            finally:
                store.close()
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _update_workflow(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import open_store

            workflow_id = str(request.path_params["workflow_id"])
            store = open_store()
            try:
                existing = store.get_workflow(workflow_id)
                if existing is None:
                    return JSONResponse(
                        {"error": "workflow not found"}, status_code=404
                    )
                workflow = self._workflow_from_body(body, existing)
                updated = store.update_workflow(workflow_id, workflow)
                return self._workflow_response(store, updated)
            finally:
                store.close()
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _delete_workflow(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        workflow_id = str(request.path_params["workflow_id"])
        store = open_store()
        try:
            if store.get_workflow(workflow_id) is None:
                return JSONResponse({"error": "workflow not found"}, status_code=404)
            # Deleting a workflow stops it; it does not erase what it ran.
            # Refused while a step is mid-run for the same reason deleting a
            # task is: the run would be left with nothing to write home to.
            running = [
                task.id
                for task in store.step_tasks(workflow_id).values()
                if task.active_run_id
            ]
            if running:
                return JSONResponse(
                    {"error": "有步骤正在运行，请先取消运行"}, status_code=409
                )
            disabled = store.delete_workflow(workflow_id)
            return JSONResponse({"ok": True, "disabled_task_ids": disabled})
        finally:
            store.close()

    async def _schedules(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store
        from agent.scheduler.profiles import profile_payloads

        store = open_store()
        try:
            # One snapshot for the whole response: the per-task counts on the
            # cards, the badge number, and the rows behind it all read from
            # the same query, so the page can add up its own rows and get the
            # number the navigation is showing.
            snapshot = store.attention_snapshot()
            unseen = snapshot["counts"]
            # The patrol's own one-off errands are not this list's business: they
            # exist to run once, and their result lives on the proposal that
            # started them (see 自主). Read through `automation_rows`' one
            # definition so the two endpoints cannot disagree about what counts.
            from agent.scheduler.automations import own_errand_task_ids

            hidden = own_errand_task_ids(store)
            tasks = []
            for task in store.list_tasks():
                if task.id in hidden:
                    continue
                latest_run = store.latest_run(task.id)
                tasks.append(
                    scheduler_task_payload(
                        task, latest_run, unseen.get(task.id, 0)
                    )
                )
            # The profile list travels with the data so the UI renders what
            # this backend accepts instead of keeping its own copy of the set
            # -- the two would drift the moment a profile is added.
            return JSONResponse(
                {
                    "tasks": tasks,
                    "permission_profiles": profile_payloads(),
                    # Failures that finished while nobody was watching.  The
                    # scheduler runs precisely when no client is connected, so
                    # this has to be part of the data rather than a push event
                    # that only reaches whoever happened to be looking.  The
                    # runs come along so the page can point at them: a count
                    # with nothing to click is the question this answers.
                    "unseen_attention": snapshot["total"],
                    "attention_runs": attention_rows(store, snapshot),
                    "latest_run_by_task": snapshot["latest_by_task"],
                }
            )
        finally:
            store.close()

    async def _automations(self, request: Any) -> Any:
        """Tasks and chains in one shape, from one snapshot.

        The two existing endpoints stay: they carry the editors' fields, and the page's
        detail panels still call them.  This one answers the *list* question -- what do I
        have, what needs me, when does it next start -- where "task or workflow" was
        never a real distinction to a reader.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store
        from agent.scheduler.automations import automation_rows
        from agent.scheduler.profiles import profile_payloads

        store = open_store()
        try:
            # One snapshot for the badge, the rows and the counts, for the reason the
            # attention snapshot documents: a number the page cannot reproduce is worse
            # than no number.
            snapshot = store.attention_snapshot()
            return JSONResponse(
                {
                    "automations": automation_rows(store, snapshot),
                    "permission_profiles": profile_payloads(),
                    "unseen_attention": snapshot["total"],
                    "attention_runs": attention_rows(store, snapshot),
                    # Sent for the same reason /api/schedules sends it: the client has
                    # one writer for the badge and the rows behind it, and a second
                    # response shape would give it a second, drifting one.
                    "latest_run_by_task": snapshot["latest_by_task"],
                }
            )
        finally:
            store.close()

    async def _schedule_runs(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        task_id = str(request.path_params["task_id"])
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 100))
        except ValueError:
            limit = 50
        store = open_store()
        try:
            task = store.get_task(task_id)
            if task is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            runs = list(reversed(store.list_runs(task_id)))[:limit]
            return JSONResponse(
                {
                    "task": scheduler_task_payload(
                        task,
                        store.latest_run(task.id),
                        store.unacknowledged_attention_counts().get(task_id, 0),
                    ),
                    "runs": [scheduler_run_payload(run) for run in runs],
                }
            )
        finally:
            store.close()

    async def _schedule_run_output(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        task_id = str(request.path_params["task_id"])
        run_id = str(request.path_params["run_id"])
        store = open_store()
        try:
            if store.get_task(task_id) is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            run = store.get_run(task_id, run_id)
            if run is None:
                return JSONResponse({"error": "run not found"}, status_code=404)
        finally:
            store.close()
        if not run.output_path:
            return JSONResponse(
                {"run_id": run.id, "available": False, "content": "", "truncated": False}
            )
        output_path = scheduler_output_path(task_id, run)
        if output_path is None:
            return JSONResponse({"error": "invalid run output path"}, status_code=403)
        if not output_path.is_file():
            return JSONResponse(
                {"run_id": run.id, "available": False, "content": "", "truncated": False}
            )
        try:
            max_bytes = 2 * 1024 * 1024
            with output_path.open("rb") as handle:
                raw = handle.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            content = raw[:max_bytes].decode("utf-8", errors="replace")
        except OSError as exc:
            return JSONResponse({"error": f"unable to read run output: {exc}"}, status_code=500)
        return JSONResponse(
            {
                "run_id": run.id,
                "available": True,
                "content": content,
                "truncated": truncated,
                "output_url": scheduler_output_url(
                    task_id, str(getattr(run, "id", "") or run_id), str(output_path)
                ),
            }
        )

    def _schedule_artifact_root(self, task_id: str, run: Any) -> Path:
        output_path = str(getattr(run, "output_path", "") or "").strip()
        if output_path:
            return Path(output_path).expanduser().resolve().parent / run.id / "artifacts"
        output_root = Path(
            self._components.get("output_dir") or shared.DEFAULT_OUTPUT_DIR
        ).expanduser().resolve()
        return output_root / "scheduler" / task_id / run.id / "artifacts"

    async def _schedule_run_artifacts(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        task_id = str(request.path_params["task_id"])
        run_id = str(request.path_params["run_id"])
        store = open_store()
        try:
            run = store.get_run(task_id, run_id)
        finally:
            store.close()
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        root = self._schedule_artifact_root(task_id, run)
        return JSONResponse(
            {"artifacts": _list_run_artifacts(root, task_id, run_id)}
        )

    async def _schedule_run_artifact(self, request: Any) -> Any:
        from starlette.responses import FileResponse, JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        task_id = str(request.path_params["task_id"])
        run_id = str(request.path_params["run_id"])
        store = open_store()
        try:
            run = store.get_run(task_id, run_id)
        finally:
            store.close()
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        root = self._schedule_artifact_root(task_id, run)
        try:
            target = (root / str(request.path_params["artifact_path"])).resolve()
            if root != target and root not in target.parents:
                return JSONResponse({"error": "invalid artifact path"}, status_code=403)
            if not target.is_file():
                return JSONResponse({"error": "artifact not found"}, status_code=404)
        except OSError:
            return JSONResponse({"error": "invalid artifact path"}, status_code=400)
        return FileResponse(target, filename=target.name)

    async def _create_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import open_store

            task = self._schedule_from_body(body)
            store = open_store()
            try:
                created = store.create_task(task)
            finally:
                store.close()
            return JSONResponse({"task": scheduler_task_payload(created)})
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _delete_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store
        store = open_store()
        try:
            task_id = str(request.path_params["task_id"])
            task = store.get_task(task_id)
            ok = task is not None
            try:
                store.delete_task(task_id)
            except ValueError as exc:
                # A running task, or a step of a workflow that still exists:
                # both are the store's call, and both are conflicts rather
                # than bad requests -- the id is right, the moment is not.
                return JSONResponse({"error": str(exc)}, status_code=409)
        finally: store.close()
        return JSONResponse({"ok": bool(ok)})

    async def _patch_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        try: body = await request.json()
        except Exception: return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or "enabled" not in body: return JSONResponse({"error": "enabled is required"}, status_code=400)
        from agent.scheduler import open_store
        store = open_store()
        try:
            task_id = str(request.path_params["task_id"])
            try:
                store.set_enabled(task_id, bool(body["enabled"]))
            except ValueError as exc:
                # A step of a workflow that still exists: its switch is the
                # workflow's to write, so the refusal says where to go.  The
                # store decides, because the same question is asked by the
                # bulk endpoint and by the agent's tools, and one rule should
                # not have three copies to drift.
                return JSONResponse({"error": str(exc)}, status_code=409)
        finally: store.close()
        return JSONResponse({"ok": True, "enabled": bool(body["enabled"])})

    async def _update_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import open_store, mirror_step_edit, step_owns_no_trigger

            store = open_store()
            try:
                task_id = str(request.path_params["task_id"])
                existing = store.get_task(task_id)
                if existing is None:
                    return JSONResponse({"error": "task not found"}, status_code=404)
                spec = self._schedule_from_body(
                    body,
                    existing,
                    # A step that waits on upstreams does not own its trigger:
                    # the graph says it waits for them, and the task's own
                    # signal trigger is that answer written down.  Letting the
                    # task editor set a second one would either replace the
                    # edge with a clock or leave one of the two lying, and the
                    # editor has no field that could express "after A and B
                    # succeed" anyway.  The store decides, so the two doors
                    # cannot disagree.
                    keep_trigger=step_owns_no_trigger(store, existing),
                )
                updated = store.update_task(task_id, spec)
                if updated is not None:
                    # The task row is what was written; the graph is what the
                    # next save of the workflow rebuilds it from.  Without
                    # this the edit survives until somebody moves an edge.
                    mirror_step_edit(store, updated)
                unseen = store.unacknowledged_attention_counts().get(task_id, 0)
            finally:
                store.close()
            if updated is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            return JSONResponse(
                {"task": scheduler_task_payload(updated, None, unseen)}
            )
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _bulk_schedules(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        ids = list(dict.fromkeys(
            str(item).strip() for item in body.get("task_ids", []) if str(item).strip()
        ))
        if not ids or len(ids) > 200:
            return JSONResponse({"error": "task_ids must contain 1 to 200 ids"}, status_code=400)
        action = str(body.get("action", "")).strip().lower()
        if action not in {"enable", "disable", "delete"}:
            return JSONResponse({"error": "unsupported bulk action"}, status_code=400)
        from agent.scheduler import open_store

        completed: list[str] = []
        # One convention for both kinds of skip: the sentence that says why.
        # A code would need a table on the other side to become a sentence, and
        # the only reader here counts them -- while the reasons worth having
        # are the ones the store writes, which are already sentences.
        skipped: list[dict[str, str]] = []
        store = open_store()
        try:
            for task_id in ids:
                task = store.get_task(task_id)
                if task is None:
                    skipped.append({"id": task_id, "reason": "任务不存在"})
                    continue
                try:
                    if action == "delete":
                        store.delete_task(task_id)
                    else:
                        store.set_enabled(task_id, action == "enable")
                except ValueError as exc:
                    # The store's own refusal -- a running task, or a step
                    # whose workflow still exists.
                    skipped.append({"id": task_id, "reason": str(exc)})
                    continue
                completed.append(task_id)
        finally:
            store.close()
        return JSONResponse({"completed": completed, "skipped": skipped})

    async def _run_schedule_now(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        service = self._scheduler()
        if service is None:
            return JSONResponse({"error": "scheduler is offline"}, status_code=503)
        claimed = await service.run_task_now(str(request.path_params["task_id"]))
        if claimed is None:
            return JSONResponse(
                {"error": "任务不存在或已有运行中的实例"}, status_code=409
            )
        return JSONResponse({"run": scheduler_run_payload(claimed.run)}, status_code=202)

    async def _retry_schedule_run(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        service = self._scheduler()
        if service is None:
            return JSONResponse({"error": "scheduler is offline"}, status_code=503)
        claimed = await service.retry_run(
            str(request.path_params["task_id"]),
            str(request.path_params["run_id"]),
            use_latest=bool(body.get("use_latest", False)),
        )
        if claimed is None:
            return JSONResponse(
                {"error": "运行记录不存在、仍在运行，或任务正忙"}, status_code=409
            )
        return JSONResponse({"run": scheduler_run_payload(claimed.run)}, status_code=202)

    async def _cancel_schedule_run(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        service = self._scheduler()
        if service is None:
            return JSONResponse({"error": "scheduler is offline"}, status_code=503)
        cancelled = await service.cancel_run(
            str(request.path_params["task_id"]),
            str(request.path_params["run_id"]),
        )
        if not cancelled:
            return JSONResponse({"error": "运行不存在或已经结束"}, status_code=409)
        return JSONResponse({"ok": True}, status_code=202)

    async def _acknowledge_schedule_run(self, request: Any) -> Any:
        """Record that a person has now seen one run's failure."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        store = open_store()
        try:
            acknowledged = store.acknowledge_run(
                str(request.path_params["task_id"]),
                str(request.path_params["run_id"]),
            )
            # Returning the fresh total lets the caller correct its badge
            # without a second round trip, which is where a stale count would
            # come from.
            return JSONResponse(
                {
                    "ok": True,
                    "acknowledged": acknowledged,
                    # Same payload as the poll, so the page that just cleared
                    # one run shows the same list it would have fetched.
                    **attention_payload(store),
                }
            )
        finally:
            store.close()

    async def _schedule_attention(self, request: Any) -> Any:
        """The count for a badge polled from every view, and the runs behind it.

        Separate from ``GET /api/schedules`` because the indicator has to work
        for someone who never opens the schedules page -- making them load
        every task and run just to find out whether anything failed would be
        the opposite of a notification.

        The runs ride along rather than arriving on a second request, because
        the two numbers that used to disagree were a count from here and a
        list assembled somewhere else.  One payload cannot disagree with
        itself, and at this poll rate the extra rows cost nothing.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import open_store

        store = open_store()
        try:
            return JSONResponse(attention_payload(store))
        finally:
            store.close()

    async def _clear_schedule_attention(self, request: Any) -> Any:
        """Mark every unseen failure as seen; optionally one task's only."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        task_id = str(body.get("task_id") or "").strip()
        from agent.scheduler import open_store

        store = open_store()
        try:
            cleared = store.acknowledge_attention(task_id or None)
            return JSONResponse(
                {
                    "ok": True,
                    "cleared": cleared,
                    **attention_payload(store),
                }
            )
        finally:
            store.close()

    async def _scheduler_health(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        service = self._scheduler()
        if service is None:
            return JSONResponse({"status": "offline", "active_runs": 0})
        return JSONResponse(service.health())

    async def _signals(self, request: Any) -> Any:
        """What can be waited for, so a subscription is chosen rather than typed."""
        return await web_signals._signals(
            request=request, authorized=self._authorized
        )


    async def _toggle_skill(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or "enabled" not in body:
            return JSONResponse({"error": "enabled is required"}, status_code=400)
        desired = bool(body["enabled"])

        catalog = resolve(self._components, "skill_catalog")
        if catalog is None:
            return JSONResponse({"error": "skill catalog is not available"}, status_code=503)
        skill_id = str(request.path_params["skill_id"])
        # The switch is thrown on a skill that may well be off already, so the
        # lookup has to see the ones that are off -- otherwise the only
        # position a switch could not be moved from is "off".
        bundle = catalog.find_any(skill_id)
        if bundle is None:
            return JSONResponse({"error": "skill not found"}, status_code=404)

        from agent.config import load_config, save_config

        cfg, _ = load_config()
        skills_cfg = cfg.get("skills")
        if not isinstance(skills_cfg, dict):
            skills_cfg = {}
            cfg["skills"] = skills_cfg
        entry = skills_cfg.get(bundle.id)
        if not isinstance(entry, dict):
            entry = {}
            skills_cfg[bundle.id] = entry
        entry["enabled"] = desired
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

        # Applied in memory too, and that is the whole of it. Unlike a plugin,
        # whose tools and MCP servers have to be rebuilt, a skill switch only
        # changes what the prompt says is available: marking the catalog dirty
        # recomposes the prompt before the next turn, for sessions that are
        # already open, without rebuilding their runtime underneath them.
        catalog.set_enabled(bundle.id, desired)
        return JSONResponse({"ok": True, "id": bundle.id, "enabled": desired})

    async def _delete_skill(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        catalog = resolve(self._components, "skill_catalog")
        # Management, so switched-off skills count: deleting one is how a
        # person gets rid of a skill they turned off and no longer want.
        bundle = catalog.find_any(str(request.path_params["skill_id"])) if catalog is not None else None
        if bundle is None: return JSONResponse({"error": "skill not found"}, status_code=404)
        if getattr(bundle, "source", "") != "user": return JSONResponse({"error": "内置技能不能删除"}, status_code=403)
        try:
            import shutil
            path = Path(bundle.path).resolve()
            root = Path(getattr(catalog, "user_root", shared.SKILLS_DIR)).resolve()
            if root not in path.parents: return JSONResponse({"error": "invalid skill path"}, status_code=400)
            shutil.rmtree(path)
            catalog.forget(bundle.id)
            self._forget_skill_config(bundle.id)
            catalog.reload()
            self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
            return JSONResponse({"ok": True, "id": bundle.id})
        except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)

    @staticmethod
    def _forget_skill_config(skill_id: str) -> None:
        """Drop a deleted skill's switch from config.json.

        The id can be reused by a skill created later, and a stale "off" entry
        would meet it with a state nobody chose and nothing on screen to
        explain. Best-effort: the directory is already gone, and failing to
        tidy the config must not turn a completed deletion into an error.
        """
        from agent.config import load_config, save_config

        try:
            cfg, _ = load_config()
            skills_cfg = cfg.get("skills")
            if isinstance(skills_cfg, dict) and skill_id in skills_cfg:
                del skills_cfg[skill_id]
                save_config(cfg)
        except Exception:
            pass

    async def _delete_plugin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params["plugin_name"])
        catalog = resolve(self._components, "plugin_catalog")
        meta = next((item for item in (catalog.list_plugins() if catalog else []) if getattr(item, "name", "") == name), None)
        if meta is None: return JSONResponse({"error": "plugin not found"}, status_code=404)
        if getattr(meta, "source", "") != "user": return JSONResponse({"error": "内置插件不能删除"}, status_code=403)
        try:
            import shutil
            root = Path(shared.USER_PLUGINS_DIR).resolve(); path = Path(meta.path).resolve()
            if root not in path.parents: return JSONResponse({"error": "invalid plugin path"}, status_code=400)
            shutil.rmtree(path)
            reload_fn = getattr(catalog, "reload", None)
            if callable(reload_fn): await reload_fn(self._components)
            self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
            return JSONResponse({"ok": True, "name": name})
        except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)

    async def _context_stats(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        base_ctx_mgr = resolve(self._components, "context_manager")
        stats = {}
        if base_ctx_mgr is not None:
            stats_method = getattr(base_ctx_mgr, "stats", None)
            if callable(stats_method):
                try:
                    stats = stats_method()
                except Exception:
                    stats = {}
        return JSONResponse({"stats": stats})

    # -- memory ----------------------------------------------------------------

    def _memory_store(self) -> Any:
        """The long-term store, or ``None`` when this deployment has no memory.

        Resolved per call rather than captured at construction: the memory
        plugin can be hot-swapped, and a channel holding the store it saw at
        startup would keep answering from a store that has been unloaded.
        """
        return resolve(self._components, "ltm_store")

    async def _memory_overview(self, request: Any) -> Any:
        return await web_memory._memory_overview(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_entries(self, request: Any) -> Any:
        return await web_memory._memory_entries(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_create_entry(self, request: Any) -> Any:
        return await web_memory._memory_create_entry(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_update_entry(self, request: Any) -> Any:
        return await web_memory._memory_update_entry(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_delete_entries(self, request: Any) -> Any:
        return await web_memory._memory_delete_entries(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_clear(self, request: Any) -> Any:
        return await web_memory._memory_clear(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_delete_entry(self, request: Any) -> Any:
        return await web_memory._memory_delete_entry(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_facts(self, request: Any) -> Any:
        return await web_memory._memory_facts(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _memory_forget_fact(self, request: Any) -> Any:
        return await web_memory._memory_forget_fact(
            request, authorized=self._authorized, store=self._memory_store()
        )

    async def _session_usage(self, request: Any) -> Any:
        """What one session has spent, and the ceiling it is measured against.

        The page's number and the loop's step-boundary check read the same
        ledger through the same service verb, so "12.3k / 200k" beside the
        composer and "the turn stopped for budget" cannot disagree about which
        side of the limit the session is on.

        A deployment with no memory service answers with zeroes and a ceiling
        of 0 (no ceiling) rather than 503: usage is accounting, and a page that
        cannot show its own cost is worse than one showing "0".
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        store = self._memory_store()
        usage: dict[str, Any] = {}
        if store is not None:
            try:
                usage = dict(store.usage_summary(session_id) or {})
            except Exception as exc:
                return JSONResponse({"error": f"读取用量失败：{exc}"}, status_code=500)
        limit = _session_token_budget(resolve(self._components, "cfg") or {})
        used = int(usage.get("input_tokens", 0) or 0) + int(
            usage.get("output_tokens", 0) or 0
        )
        return JSONResponse(
            {
                "session_id": session_id,
                "calls": int(usage.get("calls", 0) or 0),
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "cached_input_tokens": int(usage.get("cached_input_tokens", 0) or 0),
                "budget": {
                    "limit": limit,
                    "used": used,
                    "remaining": max(0, limit - used) if limit > 0 else 0,
                    "exhausted": bool(limit > 0 and used >= limit),
                },
            }
        )

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

        catalog = resolve(self._components, "plugin_catalog")
        reload_result: dict = {}
        if catalog is not None:
            # No private poke: the reload re-reads the `plugins` section itself now
            # (`PluginCatalog.catch_up`), which is what makes this endpoint's
            # own save the thing that takes effect.  It used to assign
            # `catalog._plugin_config` from here -- a second writer for a value the
            # catalog already knows how to read.
            reload = getattr(catalog, "reload", None)
            if callable(reload):
                try:
                    self._ensure_router()
                    reload_result = await reload(self._components) or {}
                except Exception as exc:
                    return JSONResponse(
                        {"error": f"reload failed: {exc}", "saved": True},
                        status_code=500,
                    )
        self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
        # `restart_required` is reported rather than swallowed: a switch that changes the
        # *composition* (not just a plugin's enabled flag) cannot be applied to a running
        # gateway, and the page has to be able to say so.
        return JSONResponse(
            {
                "ok": True,
                "name": plugin_name,
                "enabled": desired,
                "restart_required": list(reload_result.get("restart_required") or ()),
            }
        )

    def _busy_sessions(self) -> list[str]:
        """Sessions with a turn in flight, by id.

        The channel's own answer to "may I do something that rearranges the world", and
        the one thing a recomposition cannot do without: between unmounting a service and
        mounting its replacement there is a window in which the service does not exist.
        A turn landing in that window fails as though data had been lost, which is a much
        worse outcome than "try again in a moment".
        """
        return sorted(
            str(session_id)
            for session_id, state in (self._sessions or {}).items()
            if str(getattr(state, "operation_state", "idle") or "idle") != "idle"
        )

    async def _composition_reload(self, request: Any) -> Any:
        """Rebuild the composition in place: a profile change without a restart.

        The operation the roadmap called "the last restart wall", and the reason it is a
        route rather than a tool: it is allowed only when **nothing is running**.  Three
        steps, in this order, and each one is load-bearing:

        1. **Quiesce.**  Every session must be idle.  A recomposition unmounts services
           before it remounts them, so a turn in flight would see one disappear; that
           failure looks like data loss, and no amount of rollback fixes it after the
           fact.  A busy process is told to try again (409, not 500).
        2. **Drop the session runtimes.**  Each one holds a fork of the registry and
           references to the services about to be replaced; the recomposition is below
           them, so they cannot survive it.
        3. **Rebind this channel.**  ``bind_runtime`` resolved ``ltm_store`` into a
           ``SessionService`` at startup.  After a memory-backend swap that object holds
           the *old* store -- the new one would be open, healthy, and unused.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        catalog = resolve(self._components, "plugin_catalog")
        recompose = getattr(catalog, "recompose", None)
        if not callable(recompose):
            return JSONResponse(
                {"error": "this composition cannot rebuild itself"},
                status_code=500,
            )
        busy = self._busy_sessions()
        if busy:
            return JSONResponse(
                {
                    "error": (
                        "a turn is running, and a recomposition replaces the services "
                        "underneath it"
                    ),
                    "busy_sessions": busy,
                    "retryable": True,
                },
                status_code=409,
            )

        result = await self._recompose_now()
        if not result.get("ok"):
            return JSONResponse(result, status_code=500)
        return JSONResponse(result)

    async def _recompose_now(self) -> dict:
        """The recomposition itself, with the caller having established that it may run.

        Split out because there are two entry points -- the explicit route and the
        config save that made the change -- and the steps between them are the ones that
        must not diverge: drop the session runtimes, rebuild, rebind this channel, bump
        the revision.
        """
        catalog = resolve(self._components, "plugin_catalog")
        cleanup = self._components.get("session_runtime_cleanup")
        for session_id in list(self._sessions or {}):
            if callable(cleanup):
                await cleanup(session_id)

        result = await catalog.recompose()
        if not result.get("ok"):
            return result

        # Re-resolve what this channel captured, then tell open pages to reload: the
        # session runtimes are gone, so the next message builds against the new tree.
        self.bind_runtime(self._sessions or {}, self._components)
        self._bump_config_revision()
        return result

    async def _delete_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        ok = await self._service().delete_session_async(session_id)
        if not ok:
            return JSONResponse({"error": "delete failed"}, status_code=500)
        return JSONResponse({"ok": True})

    async def _delete_sessions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("session_ids"), list):
            return JSONResponse({"error": "session_ids must be an array"}, status_code=400)
        session_ids = body["session_ids"]
        if len(session_ids) > 200:
            return JSONResponse({"error": "too many sessions"}, status_code=400)
        if not all(isinstance(item, str) and item.strip() for item in session_ids):
            return JSONResponse({"error": "session_ids must contain non-empty strings"}, status_code=400)
        result = await self._service().delete_sessions_async(session_ids)
        # The pointer to the one conversation is authoritative (see
        # agent/main_session.py), so deletion is the only thing that can make it
        # point at nothing -- and it has to clear it here, or the next boot hands
        # the app a thread whose history was just deleted.
        from agent.main_session import clear_main_session_id, read_main_session_id

        main_id = read_main_session_id()
        if main_id and main_id in set(session_ids):
            clear_main_session_id()
        return JSONResponse({"ok": not result["failed"], **result})

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

    def _file_roots_for(self, request: Any) -> list[Path]:
        """Directories ``GET /api/files`` may serve for *this* request.

        Serving a file is granting a capability to read one path, so the
        gateway authorises each request against the owner of the file instead
        of keeping one global allowlist of every directory it has ever seen.
        The owner is named explicitly by the caller and there are exactly two
        kinds:

        * ``session_id`` — the Web session that produced the file. Its sandbox
          is its own home (``output/``, ``uploads/``) plus the workspace folder
          the user picked for it, which is exactly what the agent's
          ``send_file`` tool was already allowed to write to.
        * ``task_id`` + ``run_id`` — a scheduled run whose output the gateway
          itself recorded.

        A request that names neither owner gets nothing, which is the point:
        without an owner there is no way to tell whether the caller is entitled
        to the file.
        """
        session_id = str(request.query_params.get("session_id") or "").strip()
        if session_id:
            return self._session_file_roots(session_id)
        task_id = str(request.query_params.get("task_id") or "").strip()
        run_id = str(request.query_params.get("run_id") or "").strip()
        if task_id and run_id:
            return self._schedule_file_roots(task_id, run_id)
        return []

    def _session_file_roots(self, session_id: str) -> list[Path]:
        """The sandbox one Web session is allowed to read attachments from."""
        roots: list[Path] = []

        def add(value: Any) -> None:
            if not value:
                return
            try:
                candidate = Path(str(value)).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)

        # A session id that does not match a real home resolves to a directory
        # that is not there, so ``add`` drops it and the request is refused.
        try:
            add(shared.web_session_home(session_id))
        except (OSError, ValueError):
            pass
        add(self._session_workspace_root(session_id))
        return roots

    def _session_workspace_root(self, session_id: str) -> str:
        """The workspace folder recorded for one session, live or restored.

        A live session keeps the value in context metadata; a session restored
        from disk keeps it in its own ``.session.json`` manifest. Reading the
        manifest only for the session being asked about keeps this per-request
        lookup from ever widening into a scan of other sessions' folders.
        """
        state = self._sessions.get(session_id)
        metadata = getattr(getattr(state, "ctx", None), "metadata", None)
        if isinstance(metadata, dict):
            value = str(metadata.get("workspace_root") or "").strip()
            if value:
                return value
        try:
            manifest = shared.web_session_home(session_id) / ".session.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ""
        if isinstance(payload, dict):
            return str(payload.get("workspace_root") or "").strip()
        return ""

    def _schedule_file_roots(self, task_id: str, run_id: str) -> list[Path]:
        """The recorded output of one scheduled run, verified against the store."""
        return web_uploads._schedule_file_roots(
            task_id=task_id,
            run_id=run_id,
        )


    async def _file(self, request: Any) -> Any:
        from starlette.responses import FileResponse, JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw_path = request.query_params.get("path", "")
        if not raw_path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except OSError:
            return JSONResponse({"error": "invalid path"}, status_code=400)
        roots = self._file_roots_for(request)
        if not roots or not any(path_contains(root, resolved) for root in roots):
            return JSONResponse({"error": "forbidden path"}, status_code=403)
        if not resolved.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(resolved)

    async def _list_sessions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"sessions": self._service().list_sessions()})

    async def _main_session(self, request: Any) -> Any:
        """The conversation the app opens, creating it the first time.

        A POST, not a GET, because this may mint a session -- and a GET that
        writes is the kind of thing that turns a prefetch into a second
        conversation. The path is a singular ``/api/session/main`` on purpose: it
        names *the* conversation, not a list of them.

        Re-validated against the live list every time. A deleted conversation is
        not the main one, and a stale pointer would boot the app into a thread
        whose socket cannot be opened -- the failure the restore path already had
        once and answered by silently starting over. This answers it by
        re-creating on purpose.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.main_session import resolve_main_session

        service = self._service()
        session_id, created = resolve_main_session(create=service.create_session)
        return JSONResponse({"session_id": session_id, "created": created})

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
            limit = max(1, min(int(request.query_params.get("limit", "200")), 500))
        except ValueError:
            limit = 200
        # The cursor for paging further back. Absent or zero means "the newest
        # window"; anything else is the oldest sequence the client already has.
        try:
            before_seq = max(0, int(request.query_params.get("before_seq", "0")))
        except ValueError:
            before_seq = 0
        return JSONResponse(
            {
                "session_id": session_id,
                **self._service().get_messages(
                    session_id, limit=limit, before_seq=before_seq
                ),
            }
        )

    async def _get_session_state(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        return JSONResponse(self._service().get_session_state(session_id))

    async def _rewind_session(self, request: Any) -> Any:
        """Take the newest turn back so it can be asked again or edited.

        The turn's text comes back with the answer: the client needs the words
        to resend them, and it must not be the client that guesses which message
        was cut.  A running turn is refused rather than queued -- cutting history
        out from under the loop would leave the in-flight request answering a
        conversation that no longer exists.
        """
        from starlette.responses import JSONResponse
        from agent.session_service import SessionBusyError

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        try:
            result = self._service().rewind_last_turn(session_id)
        except SessionBusyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        if result is None:
            return JSONResponse({"error": "没有可以回退的一轮"}, status_code=404)
        return JSONResponse({"ok": True, **result})

    async def _dismiss_task_guidance(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        session_id = str(request.path_params["session_id"])
        task_id = str(body.get("task_id") or "").strip()
        ok = self._service().dismiss_task_guidance(session_id, task_id)
        if not ok:
            return JSONResponse({"ok": False, "dismissed": False, "error": "task not found"}, status_code=404)
        return JSONResponse({"ok": True, "dismissed": True, "task_id": task_id})

    async def _pick_workspace(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self._handler is None:
            return JSONResponse({"error": "channel not started"}, status_code=503)
        session_id = str(request.path_params["session_id"])
        live_state = self._sessions.get(session_id)
        if live_state is not None and str(getattr(live_state, "operation_state", "idle")) != "idle":
            return JSONResponse(
                {
                    "ok": False,
                    "cancelled": False,
                    "error": "当前会话仍在执行任务，请等待任务结束或先停止任务后再切换项目文件夹",
                    "busy": True,
                },
                status_code=409,
            )
        selected = await _pick_workspace_directory()
        if not selected:
            return JSONResponse({"cancelled": True, "workspace_root": ""})
        # Reuse the transport-neutral command so filesystem policy, prompt,
        # and the session manifest are updated exactly as in CLI/Web commands.
        sink = WebOutputSink(collect=True)
        await self._handle_text(
            session_id,
            f"/workspace {selected}",
            sink,
            message_id=uuid.uuid4().hex,
        )
        errors = [
            str(event.get("error") or "项目文件夹切换失败")
            for event in sink.events
            if isinstance(event, dict) and event.get("type") == "error"
        ]
        errors.extend(
            str(event.get("text") or "项目文件夹切换失败")
            for event in sink.events
            if isinstance(event, dict)
            and event.get("type") == "status"
            and str(event.get("level") or "").casefold() == "error"
        )
        if errors:
            return JSONResponse(
                {
                    "ok": False,
                    "cancelled": False,
                    "error": errors[-1],
                    "events": sink.events,
                },
                status_code=409,
            )
        state = self._service().get_session_state(session_id)
        actual_root = str(state.get("workspace_root") or "")
        if actual_root != str(Path(selected).expanduser().resolve()):
            return JSONResponse(
                {
                    "ok": False,
                    "cancelled": False,
                    "error": "项目文件夹切换未生效",
                    "workspace_root": actual_root,
                    "events": sink.events,
                },
                status_code=409,
            )
        workspace_state = self._service().get_session_state(session_id)
        for live_sink in tuple(self._live_sinks.get(session_id, set())):
            try:
                live_sink.on_workspace_changed(
                    actual_root,
                    workspace_read=bool(workspace_state.get("workspace_read", True)),
                    workspace_write=bool(workspace_state.get("workspace_write", False)),
                    status=str(workspace_state.get("workspace_status") or "ready"),
                )
            except Exception:
                logger.debug("failed to broadcast workspace change", exc_info=True)
        return JSONResponse({
            "ok": True,
            "cancelled": False,
            "workspace_root": actual_root,
            "text": sink.full_text,
            "events": sink.events,
        })

    async def _pick_directory(self, request: Any) -> Any:
        """The OS folder picker, for a form field instead of a session.

        Session picking goes through ``/api/sessions/{id}/workspace/pick``
        because it has to reroute the session afterwards; a form just wants
        the path back.  Same native dialog either way, so both places see the
        same folders the user sees in Finder/Explorer.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        selected = await _pick_workspace_directory()
        if not selected:
            return JSONResponse({"cancelled": True, "workspace_root": ""})
        return JSONResponse({"cancelled": False, "workspace_root": selected})

    @staticmethod
    def _list_feishu_chats(feishu_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """The chats the bot sits in, page by page."""
        return web_signals._list_feishu_chats(
            feishu_cfg=feishu_cfg,
        )


    async def _feishu_chats(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        feishu = self._feishu_channel_config()
        if not feishu.get("app_id") or not feishu.get("app_secret"):
            return JSONResponse(
                {"error": "还没有配置飞书应用（app_id / app_secret），请先在设置里填写"},
                status_code=400,
            )
        try:
            chats = await asyncio.to_thread(self._list_feishu_chats, feishu)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"chats": chats})

    @staticmethod
    def _send_feishu_text(feishu_cfg: dict[str, Any], chat_id: str, text: str) -> None:
        """One plain text message, sent the same way a run's delivery sends."""
        from agent.channels.feishu import FeishuConfig, build_feishu_client
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        client = build_feishu_client(FeishuConfig(**feishu_cfg))
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(
                f"飞书消息发送失败：code={resp.code} msg={resp.msg}"
            )

    async def _feishu_test(self, request: Any) -> Any:
        """A short test message to one chat, before the user trusts the form.

        Whether the app credentials carry the send permission is not knowable
        from the config alone, and finding out at 3am from a failed run is
        the worst answer.  Finding out now, from a button, is the best one.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        chat_id = str(body.get("chat_id", "")).strip()
        if not chat_id:
            return JSONResponse({"error": "缺少 chat_id"}, status_code=400)
        feishu = self._feishu_channel_config()
        if not feishu.get("app_id") or not feishu.get("app_secret"):
            return JSONResponse(
                {"error": "还没有配置飞书应用（app_id / app_secret），请先在设置里填写"},
                status_code=400,
            )
        try:
            await asyncio.to_thread(
                self._send_feishu_text,
                feishu,
                chat_id,
                "测试消息：定时任务的飞书投递配置正确，这条由设置页发出。",
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True})

    async def _get_session_permissions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            PERMISSION_LEVELS,
            ShellAuthorizationScope,
            shell_session_allowlist_commands,
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
            # Commands the human approved for this session, so "always allow"
            # answers are auditable and revocable rather than invisible.
            "approved_commands": shell_session_allowlist_commands(scope=scope),
        })

    async def _delete_session_approvals(self, request: Any) -> Any:
        """Revoke every session-scoped command approval for one session."""
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            ShellAuthorizationScope,
            shell_session_allowlist_clear_scope,
        )

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        scope = ShellAuthorizationScope(session_id, "web", "")
        removed = shell_session_allowlist_clear_scope(scope)
        return JSONResponse({
            "ok": True,
            "session_id": session_id,
            "removed": removed,
            "approved_commands": [],
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
        if not text and not body.get("attachments"):
            return JSONResponse({"error": "text is required"}, status_code=400)

        session_id = str(request.path_params["session_id"])
        # Refuse rather than rewrite.  The id names a directory, and the old
        # sanitise-and-truncate turned an unusable id into a *different* valid
        # one: the turn then ran in another session's home (or the shared
        # ``unknown`` one) while answering this request.
        if not shared.is_valid_web_session_id(session_id):
            return JSONResponse({"error": "invalid session id"}, status_code=400)
        message_id = str(body.get("message_id") or uuid.uuid4().hex)
        sink = WebOutputSink(
            collect=True,
            on_attachment=lambda path, name: self._record_attachment(
                session_id, path, name, turn_id=message_id
            ),
            output_dir=_web_session_output_dir(session_id),
            turn_id=message_id,
        )
        sink.mark_turn_start()
        try:
            model = self._resolve_model_override(body.get("model"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await self._handle_text(
            session_id,
            text,
            sink,
            message_id=message_id,
            model_override=model,
            attachments=self._parse_uploaded_attachments(session_id, body.get("attachments")),
        )
        await sink.flush_attachments()
        return JSONResponse(
            {
                "session_id": session_id,
                "message_id": message_id,
                "text": sink.full_text,
                "events": sink.events,
            }
        )

    async def _cancel_session(self, request: Any) -> Any:
        """Cancel the actively running turn for a session.

        Used by the frontend "stop" button.  Cancelling the session's
        ``cancel_token`` cooperatively stops the running agent turn (the
        registered cleanup aborts an in-flight model request) and the
        existing stream flow emits ``turn_complete`` so the client resets its
        streaming state.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        state = self._sessions.get(session_id)
        token = getattr(state, "cancel_token", None) if state is not None else None
        if token is None:
            return JSONResponse({"ok": True, "cancelled": False})
        token.cancel("force")
        return JSONResponse({"ok": True, "cancelled": True})

    async def _withdraw_queued_message(self, request: Any) -> Any:
        """Take back one message that is still waiting in a session's queue.

        The text is returned so the client can put it back in the composer
        and send an edited version.  A message that was already folded into
        the running turn is no longer in either queue, so it reports
        ``withdrawn: false`` rather than pretending it was taken back — the
        model has read it.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        message_id = request.path_params["message_id"]
        state = self._sessions.get(session_id)
        if state is None:
            return JSONResponse({"ok": True, "withdrawn": False, "text": ""})
        text = state.withdraw_queued(message_id)
        return JSONResponse(
            {
                "ok": True,
                "withdrawn": text is not None,
                "text": text or "",
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

        session_id = str(websocket.path_params["session_id"])
        if not shared.is_valid_web_session_id(session_id):
            # Same reason as the HTTP path above: an id that cannot name a
            # session must not be served from another one's home.
            await websocket.send_json({"type": "error", "error": "invalid session id"})
            await websocket.close(code=1008)
            return
        # Registered before any turn starts: a report may arrive with nothing
        # running, and this is the only handle on the page at that moment.
        self._live_sockets.setdefault(session_id, set()).add(websocket)
        # A previous tab may have been switched away while this session was
        # still generating. Rebind its sinks to the new socket and publish the
        # text generated so far before receiving new input.
        live_sinks = self._live_sinks.get(session_id, set())
        for live_sink in tuple(live_sinks):
            live_sink.set_websocket(websocket)
            snapshot = live_sink.streaming_snapshot
            if snapshot is not None:
                try:
                    await websocket.send_json(snapshot)
                except Exception:
                    pass
            # An approval that was in flight when the old tab went away is
            # re-offered here, carrying the seconds that actually remain, so
            # the new tab can answer it rather than watching a session that
            # looks idle while the decision runs out its clock.
            resend = live_sink.pending_confirmation_resend()
            if resend is not None:
                try:
                    await websocket.send_json(resend)
                except Exception:
                    pass
        active_tasks: set[asyncio.Task[Any]] = set()

        def wait_for_confirmation(token: str) -> asyncio.Future[str]:
            """Resolve to an ``allow_once`` / ``allow_session`` / ``deny`` string.

            A bare bool could not express "always allow", so the waiter carries
            the human's decision verbatim and lets the sink decide what it
            means for the pending record it is about to redeem.

            The waiter registers on the channel, per session, not in this
            connection's closure: a tab switch or a refresh hands the session
            a new socket while the approval keeps waiting, and a reply
            arriving on the new socket used to be dropped -- only this
            connection's reader ever looked for the token, so the decision
            silently ran out its clock and failed closed.
            """
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            self._confirmation_waiters.setdefault(session_id, {})[token] = future
            async def guarded() -> str:
                try:
                    return str(await asyncio.wait_for(
                        future, timeout=web_sink.CONFIRMATION_TIMEOUT_SECONDS
                    ))
                except asyncio.TimeoutError:
                    return "deny"
                finally:
                    waiters = self._confirmation_waiters.get(session_id)
                    if waiters is not None:
                        waiters.pop(token, None)
                        if not waiters:
                            self._confirmation_waiters.pop(session_id, None)
            return asyncio.ensure_future(guarded())

        async def process_message(
            text: str,
            message_id: str,
            model: str | None,
            attachment_payload: Any = None,
        ) -> None:
            # Each incoming message gets its own sink.  The coordinator owns
            # per-session serialization and can therefore queue a message
            # while an earlier turn is running; sharing one sink here would
            # reset its turn markers and make queued messages look completed.
            sink = WebOutputSink(
                websocket=websocket,
                output_dir=_web_session_output_dir(session_id),
                confirmation_handler=wait_for_confirmation,
                turn_id=message_id,
            )
            sink.on_attachment = lambda path, name: self._record_attachment(
                session_id, path, name, turn_id=message_id
            )
            sink.mark_turn_start()
            self._live_sinks.setdefault(session_id, set()).add(sink)
            try:
                await self._handle_text(
                    session_id,
                    text,
                    sink,
                    message_id=message_id,
                    model_override=model,
                    attachments=self._parse_uploaded_attachments(session_id, attachment_payload),
                )
                await sink.flush_attachments()
                # A queued message intentionally has no turn_complete event;
                # its eventual execution gets its own sink and completion.
                # Commands that produce no output still receive a completion
                # marker so the client can clear its transient state.
                if not sink.turn_complete_emitted:
                    state = self._sessions.get(session_id)
                    operation_state = str(getattr(state, "operation_state", "idle"))
                    if operation_state != "idle":
                        # The coordinator accepted this request into its
                        # restart queue. Keep this sink alive until that
                        # queued operation is actually executed; closing it
                        # here would silently drop all later stream events.
                        await sink.wait_for_completion()
                    final_state = self._sessions.get(session_id)
                    final_operation_state = str(
                        getattr(final_state, "operation_state", "idle")
                    )
                    # A retired sink means the queued message was withdrawn:
                    # there is no turn to report, and the idle session we now
                    # see is the *other* turn finishing, not this one.
                    if (
                        not sink.turn_complete_emitted
                        and not sink.retired
                        and final_operation_state == "idle"
                    ):
                        sink.on_turn_complete("", [])
                        await sink.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A message the client sent must reach a terminal state it can
                # see.  Without this the exception escaped as an unretrieved
                # task exception: asyncio reported it when the task was
                # collected -- long after the turn looked alive -- and the
                # browser was told nothing, which the user experiences as a
                # spinner that never stops.  An ``error`` event is what the
                # client treats as terminal (it clears ``isStreaming``).
                logger.exception(
                    "web message handling failed: session=%s message=%s",
                    session_id,
                    message_id,
                )
                with contextlib.suppress(Exception):
                    sink.on_error(f"{type(exc).__name__}: {exc}")
                    await sink.flush()
            finally:
                self._live_sinks.get(session_id, set()).discard(sink)
                await sink.close()

        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "confirm_response":
                    token = str(data.get("confirmation_token") or "")
                    # Routed through the session's registry rather than this
                    # connection's own dict: the reply is free to arrive on
                    # whichever connection of the session is current, and only
                    # this session's waiters may answer its tokens.
                    waiter = self._confirmation_waiters.get(session_id, {}).get(token)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(_normalize_confirm_decision(data))
                    continue
                if data.get("type") != "message":
                    continue
                text = str(data.get("text", "") or "").strip()
                if not text and not data.get("attachments"):
                    await websocket.send_json(
                        {"type": "error", "error": "text is required"}
                    )
                    continue
                message_id = str(data.get("message_id") or uuid.uuid4().hex)
                try:
                    model = self._resolve_model_override(data.get("model"))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})
                    continue
                task = asyncio.create_task(
                    process_message(text, message_id, model, data.get("attachments")),
                    name=f"web-message-{message_id}",
                )
                active_tasks.add(task)

                def _observe(finished: asyncio.Task) -> None:
                    """Retrieve the task's result, so nothing is left unreported.

                    ``process_message`` reports what it can through the sink, but
                    it cannot report a failure that happened while building the
                    sink.  Asking for the exception here is the difference
                    between a logged failure and one asyncio only mentions when
                    the task is collected.
                    """
                    active_tasks.discard(finished)
                    if finished.cancelled():
                        return
                    exc = finished.exception()
                    if exc is not None:
                        logger.error(
                            "web message task failed: session=%s message=%s",
                            session_id,
                            message_id,
                            exc_info=exc,
                        )

                task.add_done_callback(_observe)
        except Exception:
            pass
        finally:
            # A tab switch closes the old socket, but must not cancel the
            # session operation. Its sinks remain registered and are rebound
            # by the next WebSocket connection, allowing the reply to
            # continue and the partial snapshot to be restored.
            active_tasks.clear()
            sockets = self._live_sockets.get(session_id)
            if sockets is not None:
                sockets.discard(websocket)
                if not sockets:
                    self._live_sockets.pop(session_id, None)

    async def _handle_text(
        self,
        session_id: str,
        text: str,
        sink: OutputSink,
        *,
        message_id: str,
        model_override: str | None = None,
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> None:
        assert self._handler is not None
        service = self._service()
        service.touch_session(session_id, status="active")
        # A conversation's first message is the best title it will ever have, and
        # it is already paid for. Without this every row in the sidebar read
        # "未命名会话" beside a session id -- twenty conversations nobody could
        # tell apart. Set before the turn, so the sidebar is right while it runs;
        # `ensure_auto_title` leaves a name somebody chose alone.
        try:
            service.ensure_auto_title(session_id, text)
        except Exception:
            # A title is a convenience: never let it be the reason a turn fails.
            pass
        attachment_metadata = [
            {
                "id": attachment.source_ref or attachment.local_path.name,
                "filename": attachment.filename or attachment.local_path.name,
                "mime_type": attachment.mime_type,
                "kind": attachment.kind,
                "path": str(attachment.local_path),
                "size_bytes": attachment.size_bytes,
            }
            for attachment in attachments
        ]
        msg = IncomingMessage(
            text=text,
            session_id=session_id,
            channel_name="web",
            metadata={
                "message_id": message_id,
                "model_override": model_override,
                "attachments": attachment_metadata,
            },
            attachments=attachments,
        )
        try:
            await self._handler(msg, sink)
        finally:
            service.touch_session(session_id, status="idle")

    def _parse_uploaded_attachments(self, session_id: str, payload: Any) -> tuple[MessageAttachment, ...]:
        return web_uploads._parse_uploaded_attachments(
            session_id=session_id,
            payload=payload,
        )


    def _record_attachment(
        self,
        session_id: str,
        path: str,
        name: str,
        *,
        turn_id: str = "",
    ) -> None:
        try:
            self._service().record_attachment(session_id, path, name, turn_id=turn_id)
        except Exception:
            logger.exception("failed to journal attachment: %s", path)

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
        self._server = server
        self._server_task = asyncio.create_task(server.serve())
        # Wait for either "stop was asked for" or "the server finished".  The
        # second arm is not symmetry: a bind failure (port already in use) ends
        # serve() with SystemExit, and waiting only on the stop event left a
        # gateway that had printed an ERROR, opened no browser, and then sat
        # there looking healthy with nothing listening.  Awaiting the finished
        # task below re-raises that failure, so the process exits with it.
        stop_waiter = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                {self._server_task, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_waiter
            server.should_exit = True
            if self._server_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._server_task
            self._server_task = None
            self._server = None
            self._handler = None

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        """Whether the server is listening on its port, within ``timeout``.

        Asks uvicorn, not the socket.  A connect succeeds against *anything*
        holding the port, so a gateway launched onto an occupied port would
        open a browser pointing at a stranger's server and report success --
        while its own startup had already failed.  ``server.started`` is set
        only after startup() bound successfully, and a finished server task
        means it never will.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while loop.time() < deadline:
            server = self._server
            if server is not None and bool(getattr(server, "started", False)):
                return True
            task = self._server_task
            if task is not None and task.done():
                return False
            await asyncio.sleep(0.05)
        return False

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
            # The autonomy page: what the agent *would* do. Read and answer only --
            # this process has no execution path for a proposal.
            # The landing page: needs / running / activity in one read, because the
            # page's question ("is anything waiting for me") must have one answer.
            Route("/api/home", self._home_overview, methods=["GET"]),
            # Older activity for the timeline's "加载更早" link.
            Route("/api/activity", self._activity_feed, methods=["GET"]),
            Route("/api/autonomy", self._autonomy_overview, methods=["GET"]),
            Route("/api/autonomy/proposals", self._autonomy_proposals, methods=["GET"]),
            Route("/api/autonomy/decide", self._autonomy_decide, methods=["POST"]),
            Route("/api/config", self._config_get, methods=["GET"]),
            Route("/api/config", self._config_save, methods=["POST"]),
            # Provider-level edits.  Separate from /api/config because the
            # settings page should not have to send a whole config back to
            # change one endpoint, and because "the page sent what it was
            # shown" is how a field nobody looked at gets overwritten.
            Route(
                "/api/providers/{provider_name}",
                self._provider_save,
                methods=["POST"],
            ),
            Route(
                "/api/providers/{provider_name}",
                self._provider_delete,
                methods=["DELETE"],
            ),
            Route(
                "/api/providers/{provider_name}/activate",
                self._provider_activate,
                methods=["POST"],
            ),
            Route(
                "/api/providers/{provider_name}/test",
                self._provider_test,
                methods=["POST"],
            ),
            Route("/api/plugins", self._plugins, methods=["GET"]),
            Route("/api/skills", self._skills, methods=["GET"]),
            Route("/api/context", self._context_stats, methods=["GET"]),
            # The memory page.  Reads and edits of the *corpus*, not of a
            # session: this store is one per home, shared by every web session,
            # which is exactly why the page is the only place it can be
            # inspected as a whole.
            Route("/api/memory", self._memory_overview, methods=["GET"]),
            Route("/api/memory/entries", self._memory_entries, methods=["GET"]),
            Route("/api/memory/entries", self._memory_create_entry, methods=["POST"]),
            # Before the collection routes above can shadow them: Starlette
            # matches in declaration order, and ``/api/memory/entries`` is not a
            # prefix match, but keeping the item routes adjacent to their
            # collection is what makes that reasoning checkable at a glance.
            Route(
                "/api/memory/entries/{entry_id}",
                self._memory_update_entry,
                methods=["PATCH"],
            ),
            Route(
                "/api/memory/entries/{entry_id}",
                self._memory_delete_entry,
                methods=["DELETE"],
            ),
            # Bulk delete and clear. Both are one request because a page that
            # looped single deletes could stop halfway and leave a memory that is
            # neither cleared nor intact.
            Route(
                "/api/memory/entries/delete",
                self._memory_delete_entries,
                methods=["POST"],
            ),
            Route("/api/memory/clear", self._memory_clear, methods=["POST"]),
            Route("/api/memory/facts", self._memory_facts, methods=["GET"]),
            Route(
                "/api/memory/facts/forget",
                self._memory_forget_fact,
                methods=["POST"],
            ),
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
            # Only when idle; a busy process is told to retry.  Not a tool: it is an
            # operation on the *process*, and the model has no way to know whether a
            # turn is in flight in another tab.
            Route(
                "/api/composition/reload",
                self._composition_reload,
                methods=["POST"],
            ),
            Route("/api/plugins/{plugin_name}", self._delete_plugin, methods=["DELETE"]),
            # Before /api/skills/{skill_id:path}: the path converter is greedy,
            # so the toggle has to be offered the path first or a skill named
            # "x/toggle" would swallow it.
            Route(
                "/api/skills/{skill_id:path}/toggle",
                self._toggle_skill,
                methods=["POST"],
            ),
            Route("/api/skills/{skill_id:path}", self._delete_skill, methods=["DELETE"]),
            # One list for 自动化: a standalone task and a chain are the same mechanism
            # with one step or several (see agent/scheduler/automations.py).  Read-only
            # for now; the write paths are still two until the tool surface is unified.
            Route("/api/automations", self._automations, methods=["GET"]),
            Route("/api/schedules", self._schedules, methods=["GET"]),
            Route("/api/schedules", self._create_schedule, methods=["POST"]),
            Route("/api/schedules", self._bulk_schedules, methods=["PATCH"]),
            # Registered before /api/schedules/{task_id}: a literal path that
            # lost the race would be read as a task id.
            Route(
                "/api/schedules/attention",
                self._schedule_attention,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/attention",
                self._clear_schedule_attention,
                methods=["POST"],
            ),
            Route("/api/scheduler/health", self._scheduler_health, methods=["GET"]),
            Route("/api/fs/pick-directory", self._pick_directory, methods=["POST"]),
            Route("/api/feishu/chats", self._feishu_chats, methods=["GET"]),
            Route("/api/feishu/test", self._feishu_test, methods=["POST"]),
            Route("/api/signals", self._signals, methods=["GET"]),
            Route("/api/workflows", self._workflows, methods=["GET"]),
            Route("/api/workflows", self._create_workflow, methods=["POST"]),
            Route(
                "/api/workflows/{workflow_id}",
                self._update_workflow,
                methods=["PUT"],
            ),
            Route(
                "/api/workflows/{workflow_id}",
                self._delete_workflow,
                methods=["DELETE"],
            ),
            Route(
                "/api/schedules/{task_id}/run",
                self._run_schedule_now,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs",
                self._schedule_runs,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/retry",
                self._retry_schedule_run,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/cancel",
                self._cancel_schedule_run,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/acknowledge",
                self._acknowledge_schedule_run,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/output",
                self._schedule_run_output,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/artifacts",
                self._schedule_run_artifacts,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/artifacts/{artifact_path:path}",
                self._schedule_run_artifact,
                methods=["GET"],
            ),
            Route("/api/schedules/{task_id}", self._delete_schedule, methods=["DELETE"]),
            Route("/api/schedules/{task_id}", self._patch_schedule, methods=["PATCH"]),
            Route("/api/schedules/{task_id}", self._update_schedule, methods=["PUT"]),
            Route("/api/sessions/{session_id}/attachments", self._upload_attachment, methods=["POST"]),
            Route("/api/files", self._file, methods=["GET"]),
            Route("/api/sessions", self._list_sessions, methods=["GET"]),
            # The one conversation, as opposed to a list of them. See
            # agent/main_session.py for why the app has one at all.
            Route("/api/session/main", self._main_session, methods=["POST"]),
            Route("/api/sessions", self._create_session, methods=["POST"]),
            Route("/api/sessions", self._delete_sessions, methods=["DELETE"]),
            Route(
                "/api/sessions/{session_id}/messages",
                self._get_messages,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/state",
                self._get_session_state,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/usage",
                self._session_usage,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/task-guidance/dismiss",
                self._dismiss_task_guidance,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/workspace/pick",
                self._pick_workspace,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/messages",
                self._post_message,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/cancel",
                self._cancel_session,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/rewind",
                self._rewind_session,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/queue/{message_id}",
                self._withdraw_queued_message,
                methods=["DELETE"],
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
            Route(
                "/api/sessions/{session_id}/approvals",
                self._delete_session_approvals,
                methods=["DELETE"],
            ),
            WebSocketRoute(
                "/api/sessions/{session_id}/stream",
                self._stream,
            ),
        ]
        if (dist_dir / "assets").is_dir():
            routes.append(Mount("/assets", StaticFiles(directory=dist_dir / "assets")))
        return Starlette(routes=routes, middleware=middleware)
