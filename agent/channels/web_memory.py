"""The memory page's HTTP surface: what the store holds, and the edits a person owns.

Why a page and not a command
----------------------------
The agent already has ``memory_read`` / ``memory_search`` / ``memory_write``, and
they are the wrong shape for the question this page answers.  A tool is used
*inside* a turn, by the model, about whatever the turn is about; the page is used
from outside every turn, by the person, about the corpus itself.  "What do you
know about me" asked through a tool costs a turn and returns whatever the ranking
felt like.  Asked through a page it is a list you can read, sort, correct, and
delete -- and a wrong memory is finally something you can *fix* rather than
argue with.

What the page may change, and what it may not
---------------------------------------------
Entries are prose this store keeps, so they are editable and deletable outright.
Facts are *inferences* over an assertion ledger, so they are not: editing one by
hand would be undone by the next consolidation pass, and deleting the row would
leave the evidence behind to resolve it again.  The page offers "forget" instead,
which archives the assertions and drops the resolved belief -- rejecting a belief
is a different act from editing a note, and the two are separate verbs here
because they are separate acts in the data.

Why every handler takes ``store``
---------------------------------
So the caller decides which store answers.  ``WebChannel`` resolves it from the
composition (``ltm_store``); a test passes one directly.  A handler that reached
for a module-level store would make "memory is disabled" a crash rather than a
503, and the page is the one surface that must keep working when memory is off
(long enough, at least, to say so).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

__all__ = [
    "_memory_clear",
    "_memory_delete_entries",
    "_memory_overview",
    "_memory_entries",
    "_memory_create_entry",
    "_memory_update_entry",
    "_memory_delete_entry",
    "_memory_facts",
    "_memory_forget_fact",
]

#: A single entry longer than this is almost certainly a paste of a document
#: into a memory form.  Refusing it says so; silently truncating would store a
#: memory that reads as complete and is not.
_MAX_CONTENT_CHARS = 4000
_DEFAULT_CATEGORY = "concepts"


def _now() -> str:
    """The store's timestamp spelling, so rows sort with the ones it wrote."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def _unavailable():
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "记忆未启用（memory 服务不可用）"}, status_code=503)


def _parse_limit(raw: Any, default: int = 50, maximum: int = 200) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


def _parse_offset(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _parse_unit(raw: Any) -> Optional[float]:
    """A 0..1 number, or ``None`` when the caller did not send one."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, value))


def _entry_payload(entry: Any) -> dict[str, Any]:
    payload = entry.to_dict()
    payload["importance"] = round(float(payload.get("importance") or 0.0), 3)
    payload["confidence"] = round(float(payload.get("confidence") or 0.0), 3)
    payload["pinned"] = bool(payload.get("pinned"))
    return payload


def _fact_payload(fact: Any) -> dict[str, Any]:
    return {
        "fact_key": fact.fact_key,
        "subject": fact.subject,
        "predicate": fact.predicate,
        "value": fact.value,
        "value_type": fact.value_type,
        "scope": fact.scope,
        "confidence": round(float(fact.confidence or 0.0), 3),
        "source_kind": fact.source_kind,
        "source_session": fact.source_session,
        "channel": fact.channel,
        "asserted_at": fact.asserted_at,
        "updated_at": fact.updated_at,
    }


async def _memory_overview(request: Any, *, authorized, store) -> Any:
    """The counts and the category list, from one read.

    The three numbers come from the same two queries the list endpoints use, so
    "12 条" on the header and twelve rows below it cannot disagree -- the header
    is derived from the rows, not counted separately.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        entries = store.all_entries()
        facts = store.read_resolved_facts()
        categories = store.list_categories()
    except Exception as exc:
        return JSONResponse({"error": f"读取记忆失败：{exc}"}, status_code=500)
    return JSONResponse(
        {
            "enabled": True,
            "entry_count": len(entries),
            "fact_count": len(facts),
            "pinned_count": sum(1 for entry in entries if getattr(entry, "pinned", 0)),
            "categories": [
                {
                    "name": category.name,
                    "entry_count": int(category.entry_count or 0),
                    "avg_importance": round(float(category.avg_importance or 0.0), 3),
                    "last_updated": category.last_updated,
                }
                for category in categories
            ],
        }
    )


async def _memory_entries(request: Any, *, authorized, store) -> Any:
    """One page of entries, filtered by category and (optionally) search terms.

    Paging is done after the filter rather than in SQL because both candidate
    sets are already bounded by ``all_entries`` / ``search_entries``, and doing
    it here keeps one definition of "what the page shows" for the search and
    non-search paths -- a paginated SQL variant that ranked differently from the
    search would make the row count change when you typed.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    query = getattr(request, "query_params", None)
    text = str(query.get("q", "") or "").strip() if query is not None else ""
    category = str(query.get("category", "") or "").strip() if query is not None else ""
    limit = _parse_limit(query.get("limit") if query is not None else None)
    offset = _parse_offset(query.get("offset") if query is not None else None)
    try:
        if text:
            # The search path ranks, so it is given a wider window than one
            # page: slicing the top ``limit`` results before paginating would
            # make "page 2" a different ranking rather than the next rows.
            rows = list(
                store.search_entries(
                    text,
                    categories=[category] if category else None,
                    limit=min(200, offset + limit + 50),
                )
            )
        else:
            rows = list(store.all_entries())
    except Exception as exc:
        return JSONResponse({"error": f"读取记忆失败：{exc}"}, status_code=500)
    if category:
        rows = [row for row in rows if row.category == category]
    total = len(rows)
    page = rows[offset : offset + limit]
    return JSONResponse(
        {
            "entries": [_entry_payload(entry) for entry in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


async def _memory_create_entry(request: Any, *, authorized, store) -> Any:
    """A memory the person wrote, through the same write path the agent uses.

    ``add_entry`` rather than a second insert: it normalizes the category,
    normalizes the entity against it, and folds a duplicate into the row that
    already exists.  A page with its own insert would be a second answer to
    "what happens when I remember something twice".
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    content = str(body.get("content", "") or "").strip()
    if not content:
        return JSONResponse({"error": "记忆内容不能为空"}, status_code=400)
    if len(content) > _MAX_CONTENT_CHARS:
        return JSONResponse(
            {"error": f"记忆太长（{len(content)} 字符，上限 {_MAX_CONTENT_CHARS}）"},
            status_code=400,
        )
    from agent.memory.models import LTMEntry

    stamp = _now()
    entry = LTMEntry(
        id=f"web-{uuid.uuid4().hex[:12]}",
        content=content,
        importance=_parse_unit(body.get("importance")) or 0.8,
        category=str(body.get("category", "") or _DEFAULT_CATEGORY).strip(),
        entity=str(body.get("entity", "") or "").strip(),
        memory_type="fact",
        scope="global",
        status="active",
        # Not a session id: the page has to be able to say "you wrote this"
        # rather than attribute a hand-written memory to whichever conversation
        # happened to be open.
        source_session="manual",
        confidence=1.0,
        created_at=stamp,
        updated_at=stamp,
    )
    try:
        store.add_entry(entry)
    except Exception as exc:
        return JSONResponse({"error": f"写入记忆失败：{exc}"}, status_code=500)
    return JSONResponse({"ok": True, "entry": _entry_payload(entry)})


async def _memory_update_entry(request: Any, *, authorized, store) -> Any:
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    entry_id = str(request.path_params.get("entry_id", "") or "").strip()
    if not entry_id:
        return JSONResponse({"error": "missing entry id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    content = body.get("content", None)
    if content is not None:
        content = str(content).strip()
        if not content:
            return JSONResponse({"error": "记忆内容不能为空"}, status_code=400)
        if len(content) > _MAX_CONTENT_CHARS:
            return JSONResponse(
                {"error": f"记忆太长（{len(content)} 字符，上限 {_MAX_CONTENT_CHARS}）"},
                status_code=400,
            )
    category = body.get("category", None)
    pinned = body.get("pinned", None)
    if pinned is not None:
        pinned = bool(pinned)
    try:
        updated = store.update_entry(
            entry_id,
            content=content,
            importance=_parse_unit(body.get("importance")),
            category=str(category).strip() if category is not None else None,
            entity=(str(body.get("entity")).strip() if body.get("entity") is not None else None),
            pinned=pinned,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"更新记忆失败：{exc}"}, status_code=500)
    if updated is None:
        return JSONResponse({"error": "没有这条记忆（可能已被删除）"}, status_code=404)
    return JSONResponse({"ok": True, "entry": _entry_payload(updated)})


async def _memory_delete_entry(request: Any, *, authorized, store) -> Any:
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    entry_id = str(request.path_params.get("entry_id", "") or "").strip()
    if not entry_id:
        return JSONResponse({"error": "missing entry id"}, status_code=400)
    try:
        removed = store.delete_entry(entry_id)
    except Exception as exc:
        return JSONResponse({"error": f"删除记忆失败：{exc}"}, status_code=500)
    if not removed:
        return JSONResponse({"error": "没有这条记忆（可能已被删除）"}, status_code=404)
    return JSONResponse({"ok": True, "id": entry_id})


async def _memory_delete_entries(request: Any, *, authorized, store) -> Any:
    """Delete several entries in one request.

    Multi-select is not a UI convenience here: the entries are independent rows
    with no relations, so running twelve deletes from the page would be twelve
    round trips that can half-fail -- and a half-cleaned memory is the worst of
    the three outcomes. One request reports what it removed *and* what it could
    not, so the page can say which.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
    ids = [str(item).strip() for item in raw if str(item).strip()]
    if not ids:
        return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)

    removed: list[str] = []
    missing: list[str] = []
    for entry_id in ids:
        try:
            if store.delete_entry(entry_id):
                removed.append(entry_id)
            else:
                missing.append(entry_id)
        except Exception:
            missing.append(entry_id)
    return JSONResponse({"ok": True, "removed": removed, "missing": missing})


async def _memory_clear(request: Any, *, authorized, store) -> Any:
    """Erase everything the model can retrieve about the person.

    Separate from the bulk delete because it is a different act, not a longer
    list: this one removes *facts* as well as notes, and the store's own method is
    what decides how far that reaches -- a page that reimplemented "clear" with a
    loop over entries would leave every resolved belief behind.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        deleted = store.clear_persistent_memory()
    except Exception as exc:
        return JSONResponse({"error": f"清空记忆失败：{exc}"}, status_code=500)
    return JSONResponse(
        {
            "ok": True,
            "deleted": int(sum((deleted or {}).values())),
            "deleted_by_store": deleted or {},
        }
    )


async def _memory_facts(request: Any, *, authorized, store) -> Any:
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        facts = store.read_resolved_facts()
    except Exception as exc:
        return JSONResponse({"error": f"读取事实失败：{exc}"}, status_code=500)
    return JSONResponse({"facts": [_fact_payload(fact) for fact in facts]})


async def _memory_forget_fact(request: Any, *, authorized, store) -> Any:
    """Stop believing one fact key.  Archives the evidence, drops the belief."""
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    subject = str(body.get("subject", "") or "").strip()
    predicate = str(body.get("predicate", "") or "").strip()
    scope = str(body.get("scope", "") or "global").strip() or "global"
    if not subject or not predicate:
        return JSONResponse({"error": "subject 和 predicate 必填"}, status_code=400)
    try:
        retired = store.retract_facts(subject, predicate, scope)
    except Exception as exc:
        return JSONResponse({"error": f"忘记失败：{exc}"}, status_code=500)
    if not retired:
        # Nothing was asserted under that key, or it was already archived.
        # Answering 200 here would tell the page a belief went away when it may
        # never have existed, and the list would not change.
        return JSONResponse({"error": "没有可忘记的证据（可能已被忘记）"}, status_code=404)
    return JSONResponse({"ok": True, "retired": int(retired)})
