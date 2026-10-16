"""Attachments coming in over HTTP: reading the upload, parsing what came with it, and the file roots a scheduled task may draw from. Moved out of WebChannel (see the remaining-big-files survey work order). The token check arrives as a capability."""

from __future__ import annotations
import uuid

from agent import shared

from agent.channels.web_payloads import scheduler_output_path

from agent.channels.web_session_paths import _web_session_upload_dir

from pathlib import Path

from agent.core.attachments import MessageAttachment, attachment_kind_for_mime

from typing import Any

__all__ = [
    "_upload_attachment",
    "_parse_uploaded_attachments",
    "_schedule_file_roots",
]


async def _upload_attachment(
    request: Any,
    *,
    authorized,
) -> Any:
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    session_id = str(request.path_params["session_id"])
    # The same predicate the storage layer uses, so an id this endpoint
    # accepts is always one ``web_session_home`` can resolve.  Its own
    # regex used to allow up to 128 characters while the home allowed 64:
    # a 65-character id was accepted here and then written into the shared
    # ``unknown`` directory, silently sharing it with every other such id.
    if not shared.is_valid_web_session_id(session_id):
        return JSONResponse({"error": "invalid session id"}, status_code=400)
    try:
        form = await request.form()
        uploads = form.getlist("files") if hasattr(form, "getlist") else []
    except Exception as exc:
        return JSONResponse({"error": f"invalid multipart body: {exc}"}, status_code=400)
    if not uploads:
        return JSONResponse({"error": "files are required"}, status_code=400)
    if len(uploads) > 12:
        return JSONResponse({"error": "最多上传 12 个文件"}, status_code=400)
    root = _web_session_upload_dir(session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    result: list[dict[str, Any]] = []
    max_size = 25 * 1024 * 1024
    for upload in uploads:
        filename = Path(str(getattr(upload, "filename", "") or "附件")).name
        if not filename or filename in {".", ".."}:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        content_type = str(getattr(upload, "content_type", "") or "application/octet-stream")
        target = (root / f"{uuid.uuid4().hex[:12]}-{filename}").resolve()
        if root not in target.parents:
            return JSONResponse({"error": "invalid upload path"}, status_code=400)
        size = 0
        try:
            with target.open("wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_size:
                        target.unlink(missing_ok=True)
                        return JSONResponse({"error": "单个文件不能超过 25 MB"}, status_code=413)
                    out.write(chunk)
        except Exception as exc:
            target.unlink(missing_ok=True)
            return JSONResponse({"error": f"upload failed: {exc}"}, status_code=500)
        result.append({
            "id": target.name,
            "filename": filename,
            "mime_type": content_type,
            "kind": attachment_kind_for_mime(content_type),
            "path": str(target),
            "size_bytes": size,
        })
    return JSONResponse({"attachments": result})


def _schedule_file_roots(
    task_id: str,
    run_id: str,
) -> list[Path]:
    """The recorded output of one scheduled run, verified against the store.

        The path is never taken from the query string; it is re-read from the
        run so a forged ``path`` cannot point at an unrelated local file.
        """
    from agent.scheduler import open_store

    store = open_store()
    try:
        run = store.get_run(task_id, run_id)
    except Exception:
        return []
    finally:
        store.close()
    if run is None:
        return []
    output = scheduler_output_path(task_id, run)
    if output is None or not output.is_file():
        return []
    return [output]


def _parse_uploaded_attachments(
    session_id: str,
    payload: Any,
) -> tuple[MessageAttachment, ...]:
    if not isinstance(payload, list):
        return ()
    root = _web_session_upload_dir(session_id).resolve()
    out: list[MessageAttachment] = []
    for raw in payload[:12]:
        if not isinstance(raw, dict):
            continue
        try:
            path = Path(str(raw.get("path", ""))).resolve()
            if root not in path.parents or not path.is_file():
                continue
            mime = str(raw.get("mime_type", "application/octet-stream"))
            out.append(MessageAttachment(
                kind=str(raw.get("kind") or attachment_kind_for_mime(mime)),
                mime_type=mime,
                local_path=path,
                filename=Path(str(raw.get("filename") or path.name)).name,
                source="web",
                source_ref=str(raw.get("id") or path.name),
                size_bytes=path.stat().st_size,
            ))
        except (OSError, ValueError):
            continue
    return tuple(out)
