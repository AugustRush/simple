"""Testing and saving a provider configuration from the web UI, the message a rejected write gets, and the token check every write goes through. Moved out of WebChannel (see the remaining-big-files survey work order). The config arrives as a collaborator and the revision bump as a capability."""

from __future__ import annotations
import time
import re
import contextlib

from agent.channels.web_provider_config import _mask_api_keys, _restore_masked_api_keys

from typing import Any, Optional

__all__ = [
    "_provider_test",
    "_provider_save",
    "_provider_write_error",
    "_authorized",
]


def _authorized(
    request: Any,
    *,
    config,
) -> bool:
    token = config.auth_token.strip()
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


def _provider_write_error(
    provider_name: str,
    provider: dict[str, Any],
    *,
    config,
) -> Optional[str]:
    """Why this provider block cannot be written, or None.

        Structural problems are refused here rather than warned about, because
        a write is an assertion about what the agent will do next: a key the
        loader never reads, a format that does not exist, or a missing required
        field would be saved and then quietly ignored at runtime.  Warnings
        remain the mode for *reading* a config -- the file may already hold
        something imperfect and refusing to start over it would be worse.
        """
    from agent.config import PROVIDER_FIELDS, provider_config_errors

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(provider_name or "")):
        return (
            "provider 名字只能包含字母、数字、点、下划线和连字符，"
                "且不能以下划线开头"
        )
    for field in PROVIDER_FIELDS:
        if field.required and not str(provider.get(field.key) or "").strip():
            return f"{field.label}（{field.key}）不能为空"
    # The same checks the config reader warns about, refusing instead of
    # warning: a write is an assertion about what the agent will do next.
    # Reading the *messages* would mean matching their text, which is how a
    # header name that is not an HTTP token could be saved by a form and
    # then sent verbatim (a strict gateway rejects it, and the reason is
    # nowhere near the config that caused it).
    errors = provider_config_errors(provider_name, provider)
    if errors:
        return errors[0]
    return None


async def _provider_save(
    request: Any,
    *,
    config,
    bump_config_revision,
) -> Any:
    """Create or update one provider, leaving every other key alone."""
    from starlette.responses import JSONResponse

    if not _authorized(request, config=config):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = str(request.path_params.get("provider_name") or "").strip()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    incoming = body.get("provider") if isinstance(body, dict) else None
    if not isinstance(incoming, dict):
        return JSONResponse({"error": "body must be {'provider': {...}}"}, status_code=400)
    from agent.config import PROVIDER_FIELDS, load_config, save_config

    cfg, _ = load_config()
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        cfg["providers"] = providers
    current = providers.get(name)
    merged = dict(current) if isinstance(current, dict) else {}
    # Only declared fields are written: a patch that names a key the
    # loader never reads is a promise the agent would not keep, and the
    # `_readme` companions are documentation, not settings.
    known = {field.key for field in PROVIDER_FIELDS}
    unknown = [
        key for key in incoming if key not in known and not str(key).startswith("_")
    ]
    if unknown:
        return JSONResponse(
            {
                "error": (
                    f"未知的 provider 字段：{'、'.join(sorted(unknown))}；"
                        f"可用：{'、'.join(sorted(known))}"
                )
            },
            status_code=400,
        )
    for key, value in incoming.items():
        if key in known:
            merged[key] = value
    # A value the page was shown masked means "unchanged": it must be
    # resolved against what is on disk before anything is written, or a
    # save would store the asterisks over a working credential.
    _restore_masked_api_keys({"providers": {name: merged}}, cfg)
    error = _provider_write_error(name, merged, config=config)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    providers[name] = merged
    try:
        save_config(cfg)
    except Exception as exc:
        return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
    bump_config_revision()
    return JSONResponse({"ok": True, "provider": _mask_api_keys({"providers": {name: merged}})["providers"][name]})


async def _provider_test(
    request: Any,
    *,
    config,
) -> Any:
    """Send one minimal request to a provider, through the real path.

        Deliberately not a hand-rolled HTTP call: the point of the button is to
        prove that *this* provider's configuration works -- its base_url, its
        wire format, its auth, and its headers, including a per-conversation
        header (which falls back to the process id here, there being no
        conversation).  A check that builds its own request would pass while
        the agent's own requests failed.
        """
    from starlette.responses import JSONResponse

    if not _authorized(request, config=config):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = str(request.path_params.get("provider_name") or "").strip()
    from agent.config import load_config
    from agent.core.providers import build_client, build_transport
    from agent.core.transport import provider_stream_usage
    from agent.shared import provider_headers

    cfg, _ = load_config()
    providers = cfg.get("providers")
    if not isinstance(providers, dict) or name not in providers:
        return JSONResponse({"error": f"找不到 provider '{name}'"}, status_code=404)
    provider_cfg = providers.get(name) or {}
    if not isinstance(provider_cfg, dict):
        return JSONResponse({"error": f"provider '{name}' 不是一个对象"}, status_code=400)
    api_format = str(provider_cfg.get("api_format") or "openai")
    model = str(provider_cfg.get("default_model") or "")
    if not model:
        return JSONResponse({"error": "这个 provider 没有 default_model"}, status_code=400)

    started = time.perf_counter()
    try:
        # Named, because a section on disk does not carry the key it is filed under
        # and the credential error reports ``provider: <name>`` -- without this the
        # one message this test exists to produce says ``provider: ?``.
        client = build_client({**provider_cfg, "name": name}, api_format)
        transport = build_transport(
            api_format,
            client,
            None,
            provider_stream_usage(provider_cfg),
            headers=provider_headers(provider_cfg),
            provider_name=name,
        )
        # Small but not degenerate: a gateway is entitled to reject a
        # max_tokens of 1, and that rejection would say nothing about the
        # configuration being tested.
        await transport.create(
            model=model,
            max_tokens=16,
            system="You are a connectivity check. Reply with one word.",
            messages=[{"role": "user", "content": "ping"}],
            tools=[],
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "provider": name,
                "model": model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            },
            status_code=200,
        )
    finally:
        # This endpoint builds its own client (the routing transport's
        # cached one belongs to the running components), so it owns closing
        # it: an SDK client holds a connection pool, and a button that
        # leaves one behind per press leaks sockets.
        with contextlib.suppress(Exception):
            await client.close()
    return JSONResponse(
        {
            "ok": True,
            "provider": name,
            "model": model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": "",
        },
        status_code=200,
    )
