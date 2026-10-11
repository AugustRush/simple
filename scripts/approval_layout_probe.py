#!/usr/bin/env python3
"""Measure the chat footer's two jobs against the real page.

The footer holds the composer and, when a tool call needs a decision, the
approval bar that covers it.  It has two jobs that pull in opposite directions:

* the bar is a **cover** -- it stands where the composer was, so the reader is
  not offered an input box that is not the thing to answer;
* the footer is **in the page**, so the space it takes comes out of the
  transcript above it rather than being drawn on top of it.

The shipped page satisfied the first and broke the second: the cover was
absolutely positioned with ``inset: 0``, so a bar taller than the composer (a
head, a reason, a command block and three buttons is taller than one input row)
overflowed *upward* out of its box and painted over the last rows of the
conversation -- the live reply, its 「思考过程 思考中」 strip and its typing dots,
which is what the screenshot showed.

No unit test can see this: it is geometry in a live layout, and the frontend has
no test runner.  So this probe drives the **shipped bundle** in a real browser
against a real gateway, replays the frames a running turn sends (thinking, a
chunk, then the approval prompt), and measures the boxes with
``getBoundingClientRect``.

Usage
-----
    uv run --with playwright python scripts/approval_layout_probe.py
    uv run --with playwright python scripts/approval_layout_probe.py --keep-home

The check *skips* (not fails) when no browser is installed.  Exit code is 0 when
nothing failed.
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

#: What the page measures, in the browser's own pixels.
MEASURE = """
() => {
  const box = selector => {
    const element = document.querySelector(selector)
    if (!element) return null
    const rect = element.getBoundingClientRect()
    return {
      top: Math.round(rect.top),
      bottom: Math.round(rect.bottom),
      height: Math.round(rect.height),
    }
  }
  return {
    bar: box('.approval-bar'),
    cover: box('.approval-cover'),
    composer: box('.composer'),
    scroll: box('.chat-scroll'),
    lastRow: box('.chat-scroll .message-row:last-of-type'),
    thinking: box('.chat-scroll .trace-toggle, .chat-scroll .message-streaming'),
    viewport: { top: 0, bottom: window.innerHeight, height: window.innerHeight },
  }
}
"""


def state_payload() -> dict[str, Any]:
    return {
        "live": True,
        "operation_state": "active",
        "queue": {"pending": 0, "interjections": 0, "restarts": 0, "items": []},
        "task": None,
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

        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        await context.add_init_script(
            f"localStorage.setItem('agent_active_session', {json.dumps(session_id)});"
        )
        page = await context.new_page()
        body = dict(state_payload())
        body["session_id"] = session_id

        async def answer_state(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(body, ensure_ascii=False),
            )

        holder: dict[str, Any] = {}

        async def handle_socket(socket: Any) -> None:
            holder["socket"] = socket
            socket.on_message(lambda message: holder.setdefault("sent", []).append(message))

        await page.route(f"**/api/sessions/{session_id}/state*", answer_state)
        await page.route_web_socket(
            f"**/api/sessions/{session_id}/stream*", handle_socket
        )
        await page.goto(base, wait_until="domcontentloaded")
        await page.wait_for_selector(".composer-stage", timeout=15000)
        for _ in range(50):
            if "socket" in holder:
                break
            await page.wait_for_timeout(100)

        socket = holder.get("socket")
        if socket is None:
            results.add("approval layout", SKIP, "no session socket")
            await context.close()
            await browser.close()
            return

        # A turn that is running: thinking, then an answer being written.  This
        # is the state the screenshot was taken in -- the last rows of the
        # conversation are live and being watched when the prompt arrives.
        socket.send(
            json.dumps({"type": "reasoning_chunk", "chunk": "先看一下这个命令要做什么。"})
        )
        socket.send(
            json.dumps({"type": "stream_chunk", "chunk": "这条命令要改工作区里的文件,我先把范围确认一下。"})
        )
        await page.wait_for_timeout(1200)

        # 高风险, and the reason is long enough to push the bar past the
        # composer's own height -- which is the case that broke.
        socket.send(
            json.dumps(
                {
                    "type": "confirm_request",
                    "name": "shell",
                    "command": "shell control operator ';' requires confirmation",
                    "risk_level": "high",
                    "reason": "命令里出现了 shell 控制符,可能一次执行多条指令。",
                    "confirmation_token": "tok-probe",
                    "allow_session": True,
                    "timeout_seconds": 300,
                }
            )
        )
        await page.wait_for_selector(".approval-bar", timeout=8000)
        # Let the bar settle (its own entry animation) before measuring.
        await page.wait_for_timeout(900)

        measured = await page.evaluate(MEASURE)

        # 4. The bar is a control, so the covered composer must not be
        #    swallowing the click.  This is the half a page probe caught once
        #    before (允许本次 was unclickable while the bar was a child of the
        #    composer) and no unit test can see.
        await page.click(".approval-bar button:has-text('允许本次')")
        await page.wait_for_timeout(400)
        sent = [
            json.loads(item)
            for item in holder.get("sent", [])
            if isinstance(item, str) and item.startswith("{")
        ]
        answers = [
            item
            for item in sent
            if item.get("type") == "confirm_response"
            and item.get("decision") == "allow_once"
        ]
        gone = await page.query_selector(".approval-bar") is None
        await context.close()
        await browser.close()

        bar, cover, composer = measured["bar"], measured["cover"], measured["composer"]
        scroll, viewport = measured["scroll"], measured["viewport"]
        if not bar or not cover or not composer or not scroll:
            results.add(
                "approval layout",
                SKIP,
                f"a box was missing: {json.dumps(measured, ensure_ascii=False)}",
            )
            return

        # 1. The cover's job: stand where the composer was, so the hidden input
        #    leaves no hole behind it.
        covers = (
            cover["top"] <= composer["top"] + 1
            and cover["bottom"] >= composer["bottom"] - 1
        )
        results.add(
            "the cover still stands where the composer was",
            OK if covers else FAIL,
            f"cover {cover['top']}..{cover['bottom']}, composer "
            f"{composer['top']}..{composer['bottom']}",
        )

        # 2. The footer's job: take its space out of the page rather than off
        #    the transcript.  The bar's top edge must not sit above the bottom
        #    of the scroller it would be painting over.
        overlap = scroll["bottom"] - bar["top"]
        results.add(
            "the bar does not cover the transcript",
            FAIL if overlap > 1 else OK,
            f"bar top {bar['top']}, transcript viewport ends {scroll['bottom']}"
            + (f" -> {overlap}px painted over the last rows" if overlap > 1 else ""),
        )

        # 3. And a decision has to be answerable: the whole bar is on screen.
        on_screen = bar["top"] >= viewport["top"] - 1 and bar["bottom"] <= viewport["bottom"] + 1
        results.add(
            "the whole bar is on screen",
            OK if on_screen else FAIL,
            f"bar {bar['top']}..{bar['bottom']} in viewport "
            f"{viewport['top']}..{viewport['bottom']}",
        )

        results.add(
            "允许本次 reaches the server",
            OK if answers and gone else FAIL,
            f"decision frames={len(answers)}, bar gone={gone}",
        )


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
