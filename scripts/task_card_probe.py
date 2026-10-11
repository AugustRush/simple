#!/usr/bin/env python3
"""Ask the real page when the task-guidance card is allowed to appear.

Why this exists
---------------
The card that offers 继续任务 / 放弃任务 above the composer makes a claim: "this
task has stopped, and only you can restart it".  The client assembled that claim
from *two* facts that can disagree -- the durable ``status`` the memory layer
projected for the task, and whether the client believed a turn was running --
and the user's own session has the disagreement in it:

* ``turn_finished`` seq 2757 carried a truncation error, so the projection
  stamped the task ``failed`` and the client read "stopped";
* seq 2758 started *the same message* again and ran 30+ tool calls, all narrated
  on the same socket -- and no tool frame ever set the client's stream flag, so
  it went on reading "stopped" for minutes and offered to continue a task the
  server was executing.

No unit test can see this.  It is a visibility rule over a live DOM, the
frontend has no test runner, and the two halves are individually correct.  So
this probe drives the **shipped bundle** in a real browser against a real
gateway and answers the question the way a reader does: is the card in the
document or not.  The ``/state`` payload and the socket frames are replayed,
because the rule under probe is exactly "given the server said this, may the
card be shown".

Usage
-----
    uv run --with playwright python scripts/task_card_probe.py
    uv run --with playwright python scripts/task_card_probe.py --keep-home

The check *skips* (not fails) when no browser is installed, so it never turns a
missing environment into a red result.  Exit code is 0 when nothing failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from functional_smoke import (  # noqa: E402
    FAIL,
    OK,
    SKIP,
    Results,
    free_port,
    make_scratch_home,
    start_gateway,
    wait_for_health,
)

CARD = ".task-guidance-card"
#: The frame a turn whose response was cut off sends.  The working-state
#: projection turns the same error into ``status: failed``.
TRUNCATED = "Model response remained truncated after 6 auto-continue attempts"
TASK = {
    "task_id": "task-probe",
    "active_goal": "做一个故事类型的，比如未解之谜之类，选一个合适的风格",
    "status": "failed",
    "progress": "写 A 卷五镜",
    "next_action": "继续写 B 卷",
    "last_error": TRUNCATED,
    "artifacts": [],
}


def state_payload(*, operation_state: str, pending: int) -> dict[str, Any]:
    return {
        "live": True,
        "operation_state": operation_state,
        "queue": {
            "pending": pending,
            "interjections": pending,
            "restarts": 0,
            "items": [],
        },
        "task": dict(TASK),
        "usage": {},
        "workspace_root": "",
        "workspace_status": "unset",
        "workspace_exists": False,
    }


async def probe(base: str, session_id: str, results: Results) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        results.add(
            "browser probe",
            SKIP,
            "playwright is not importable (run with: uv run --with playwright python ...)",
        )
        return

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            results.add("browser probe", SKIP, f"no chromium: {str(exc)[:90]}")
            return

        async def render(
            box: dict[str, Any],
            *,
            after_load: Any = None,
            settle_ms: int = 3200,
        ) -> bool:
            """Open the chat over a canned snapshot; report whether the card is there.

            ``box["body"]`` is the payload every ``/state`` read answers with, so
            a case can change what the server says halfway through.
            """
            context = await browser.new_context()
            # Remember the session before the app boots: a refresh re-opens the
            # last conversation, and that is the view the card lives in.
            await context.add_init_script(
                f"localStorage.setItem('agent_active_session', {json.dumps(session_id)});"
            )
            page = await context.new_page()
            holder: dict[str, Any] = {}

            async def answer_state(route: Any) -> None:
                body = dict(box["body"])
                body["session_id"] = session_id
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body, ensure_ascii=False),
                )

            async def handle_socket(socket: Any) -> None:
                holder["socket"] = socket
                socket.on_message(lambda _message: None)

            await page.route(f"**/api/sessions/{session_id}/state*", answer_state)
            await page.route_web_socket(
                f"**/api/sessions/{session_id}/stream*", handle_socket
            )

            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_selector(".composer-stage", timeout=15000)
            if after_load is not None:
                # The socket opens when the app selects the session, which can be
                # a round trip after the composer is on screen. Waiting for it
                # here keeps a case that sends frames from racing that.
                for _ in range(50):
                    if "socket" in holder:
                        break
                    await page.wait_for_timeout(100)
                await after_load(page, holder)
            # Longer than two poll intervals: a rule that only *flickers* the
            # card away is still a card over a working session.
            await page.wait_for_timeout(settle_ms)
            shown = await page.query_selector(CARD) is not None
            await context.close()
            return shown

        # 1. The server says the session is stopped and the task is interrupted:
        #    the card is the point of the feature and must be there.
        shown = await render({"body": state_payload(operation_state="idle", pending=0)})
        results.add(
            "card shows on a stopped task",
            OK if shown else FAIL,
            "idle + status=failed -> card in the DOM"
            if shown
            else "idle + status=failed -> no card (the resume prompt is gone)",
        )

        # 2. The reported sequence, frame for frame.  The response was cut off and
        #    said so; the snapshot the client holds still says the task is
        #    interrupted and nothing is running, so the card is up -- that part
        #    is the feature.  Then the server picks the work back up itself (the
        #    restart-queue drain that followed that truncated turn) and narrates
        #    30+ tool calls without a single text chunk.  The card must yield to
        #    those frames: a card over a working session is the bug.
        observed: dict[str, Any] = {}
        box: dict[str, Any] = {"body": state_payload(operation_state="idle", pending=0)}

        async def resume_work(page: Any, holder: dict[str, Any]) -> None:
            observed["before"] = await page.query_selector(CARD) is not None
            socket = holder.get("socket")
            if socket is None:
                observed["socket"] = False
                return
            observed["socket"] = True
            socket.send(json.dumps({"type": "error", "error": TRUNCATED}))
            await page.wait_for_timeout(800)
            box["body"] = state_payload(operation_state="active", pending=0)
            socket.send(
                json.dumps(
                    {"type": "tool_start", "name": "shell", "operation_id": "op-1"}
                )
            )
            socket.send(
                json.dumps(
                    {
                        "type": "tool_progress",
                        "name": "shell",
                        "progress": "渲染第 7/40 帧",
                    }
                )
            )
            await page.wait_for_timeout(2500)
            observed["after"] = await page.query_selector(CARD) is not None

        await render(box, after_load=resume_work)
        if not observed.get("socket"):
            results.add("card yields to narrated work", SKIP, "no session socket")
        else:
            results.add(
                "card yields to narrated work",
                FAIL
                if observed.get("after") or not observed.get("before")
                else OK,
                f"stopped -> card={observed.get('before')}, after the server "
                f"narrated a turn -> card={observed.get('after')}",
            )

        # 3. And it has to come back.  A flag that only ever goes up would hide
        #    the prompt for good, which is the failure mode of fixing (2) by
        #    refusing to render; the sequence here is stopped -> working ->
        #    stopped, and the card is expected at both ends.
        returned: dict[str, Any] = {}
        box = {"body": state_payload(operation_state="idle", pending=0)}

        async def work_then_stop(page: Any, holder: dict[str, Any]) -> None:
            returned["before"] = await page.query_selector(CARD) is not None
            socket = holder.get("socket")
            if socket is None:
                returned["socket"] = False
                return
            returned["socket"] = True
            box["body"] = state_payload(operation_state="active", pending=0)
            socket.send(
                json.dumps(
                    {"type": "tool_start", "name": "shell", "operation_id": "op-1"}
                )
            )
            await page.wait_for_timeout(2200)
            returned["during"] = await page.query_selector(CARD) is not None
            # The turn is over: the snapshot goes idle and no further frame
            # arrives, which is the only way the client can hear it.
            box["body"] = state_payload(operation_state="idle", pending=0)
            await page.wait_for_timeout(4000)
            returned["after"] = await page.query_selector(CARD) is not None

        await render(box, after_load=work_then_stop)
        if not returned.get("socket"):
            results.add("card returns when work is over", SKIP, "no session socket")
        else:
            results.add(
                "card returns when work is over",
                OK
                if returned.get("before")
                and not returned.get("during")
                and returned.get("after")
                else FAIL,
                f"stopped -> card={returned.get('before')}, working -> "
                f"card={returned.get('during')}, stopped again -> card={returned.get('after')}",
            )

        # 4. Queued work is the coordinator's to run, not the reader's to
        #    restart, so an idle session holding a queue wait must not nag
        #    either.
        shown = await render(
            {"body": state_payload(operation_state="idle", pending=1)}
        )
        results.add(
            "card stays away while work is queued",
            FAIL if shown else OK,
            "queue.pending=1 -> no card"
            if not shown
            else "queue.pending=1 -> card while the queue is the plan",
        )

        await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-home", action="store_true")
    args = parser.parse_args()

    import httpx

    results = Results()
    port = free_port()
    home, _ = make_scratch_home(port)
    base = f"http://127.0.0.1:{port}"
    process = start_gateway(home, port)
    try:
        await wait_for_health(base, process)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{base}/api/sessions")
            response.raise_for_status()
            session_id = str(response.json()["session_id"])
            try:
                await probe(base, session_id, results)
            finally:
                await client.delete(f"{base}/api/sessions/{session_id}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
        if not args.keep_home:
            import shutil

            shutil.rmtree(home, ignore_errors=True)
        else:
            print(f"scratch home kept at {home}")

    return results.report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
