"""The autonomy page's HTTP surface: what the agent would do, and what you said.

Why a page and not a command
----------------------------
``simple autonomy review`` answers the same question, and it is the wrong shape
for the daily use of this feature.  A person does not want to run a command to
find out whether the agent has been thinking; they want to glance.  The page is
where "what would it do" and "no, not that" have to be one gesture -- reading a
proposal and answering it are the same visit, and a CLI makes them two.

What this page may change
-------------------------
Only the *proposals*: accept, decline, and nothing else.  There is no "run it"
here, and that is not an omission -- this handler has no execution path.  The
patrol executes reversible candidates itself (under the executor's permission
profile), and an irreversible one is prepared for a person; a form that could
write ``acted`` would be a second way to start work, beside the admission check
and the executor's walls.  Accepting a proposal records that a person would have
agreed, which is exactly what the calibration needs.

Why every handler takes ``store``
---------------------------------
So the caller decides which store answers.  ``WebChannel`` opens the default one;
a test passes one directly.  A handler that reached for a module-level store
would make "the patrol is off" a crash instead of an empty list, and an empty
list is the honest answer.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "_autonomy_overview",
    "_autonomy_proposals",
    "_autonomy_decide",
]


def _unavailable() -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "autonomy is not available"}, status_code=503)


def _counts(store: Any) -> dict[str, int]:
    try:
        return store.counts()
    except Exception:
        return {}


async def _autonomy_overview(request: Any, *, authorized, store) -> Any:
    """The numbers the page leads with, from one read.

    ``counts`` and ``live`` come from the same store in the same visit, so "3 条
    待处理" on the header and three rows below it cannot disagree -- the number is
    derived from the rows, not counted separately.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    try:
        counts = _counts(store)
        live = store.list_proposals(state="proposed", limit=50)
        # What was answered, and what happened to it. A page that only shows the
        # queue is a page where approving is a leap of faith repeated every time:
        # the run's result would exist and nobody would ever read it.
        answered = [
            item
            for state in ("acted", "accepted", "declined")
            for item in store.list_proposals(state=state, limit=20)
        ]
        # The funnel's own numbers, read with the rows: a page about proposals
        # that cannot say how much was looked at to find them invites the reader
        # to guess whether "nothing proposed" means "nothing to do".
        from datetime import datetime, timedelta, timezone

        funnel = store.tick_stats(since=datetime.now(timezone.utc) - timedelta(days=7))
    except Exception as exc:
        return JSONResponse({"error": f"读取提议失败：{exc}"}, status_code=500)
    return JSONResponse(
        {
            "funnel": funnel,
            "counts": counts,
            # The rows the badge counts, in the order they were raised: newest
            # first is what a person reads, oldest first is what they act on.
            # Newest first here, because the page is a review queue, not an inbox.
            "live": [_proposal_payload(item) for item in live],
            "answered": [_proposal_payload(item) for item in answered],
        }
    )


async def _autonomy_proposals(request: Any, *, authorized, store) -> Any:
    """Every proposal, optionally filtered by state."""
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if store is None:
        return _unavailable()
    state = str(request.query_params.get("state") or "").strip() or None
    try:
        rows = store.list_proposals(state=state, limit=200)
        counts = _counts(store)
    except Exception as exc:
        return JSONResponse({"error": f"读取提议失败：{exc}"}, status_code=500)
    return JSONResponse(
        {"proposals": [_proposal_payload(item) for item in rows], "counts": counts}
    )


async def _autonomy_decide(request: Any, *, authorized, store, registry=None) -> Any:
    """Answer one proposal: accepted or declined.

    Both answers are final and both are *kept*.  A decline is the fact this whole
    table exists to remember -- the same suggestion must not come back in new
    words, and the only thing that can tell a rephrasing model otherwise is a
    record somebody already said no.

    There is deliberately no "acted": this page cannot act, and a state the
    process cannot reach should not be writable from a form.
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

    proposal_id = str(body.get("id") or "").strip()
    state = str(body.get("state") or "").strip()
    if not proposal_id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    if state not in {"accepted", "declined"}:
        return JSONResponse(
            {
                "error": (
                    "state must be accepted or declined; this page cannot act on "
                    "a proposal"
                )
            },
            status_code=400,
        )
    try:
        proposal = store.get_proposal(proposal_id) if state == "accepted" else None
    except Exception:
        proposal = None
    try:
        ok = store.decide(proposal_id, state, by="page")
    except Exception as exc:
        return JSONResponse({"error": f"记录决定失败：{exc}"}, status_code=500)
    if not ok:
        # Already decided, or expired.  Not an error the caller should retry --
        # the row has an answer and that answer is the state it is in.
        return JSONResponse({"ok": False, "reason": "已经答复过或已过期"}, status_code=409)
    # A "yes" to a prepared proposal carries out the call that was written down.
    # Anything else would be a consent form with no consequence: the row said
    # "accepted" and the machine stayed exactly as it was.
    applied: dict[str, Any] = {}
    if state == "accepted" and proposal is not None:
        from agent.autonomy.execute import apply_prepared

        try:
            applied = await apply_prepared(registry, proposal)
        except Exception as exc:  # pragma: no cover - the applier already answers
            applied = {"applied": False, "reason": f"执行失败：{exc}"}
    return JSONResponse({"ok": True, "id": proposal_id, "state": state, **applied})


def _proposal_payload(proposal: Any) -> dict[str, Any]:
    """One proposal, as the page reads it.

    ``admission`` is flattened out of ``context``: the page's whole job is to show
    what would happen and why, and making the client reach into a context bag for
    the verdict is how two readers end up disagreeing about it.
    """
    context = getattr(proposal, "context", {}) or {}
    admission = context.get("admission") if isinstance(context, dict) else None
    return {
        "id": proposal.id,
        "source": proposal.source,
        "reason": proposal.reason,
        "action": proposal.action,
        "tool": proposal.tool,
        "reversibility": proposal.reversibility,
        "confidence": round(float(proposal.confidence), 2),
        "state": proposal.state,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else "",
        "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else "",
        "decided_at": (
            proposal.decided_at.isoformat() if proposal.decided_at else ""
        ),
        "decided_by": proposal.decided_by,
        "admission": admission if isinstance(admission, dict) else None,
        "rule": (context.get("rule") if isinstance(context, dict) else "") or "",
        # What happened after somebody approved it. Null while a proposal has not
        # been acted on, and a run that is still queued reports "queued" rather
        # than pretending to be an outcome.
        "outcome": (
            context.get("outcome") if isinstance(context, dict) else None
        ),
    }
