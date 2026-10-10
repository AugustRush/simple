#!/usr/bin/env python3
"""Drive a real gateway and check that whole features actually work.

Why this exists
---------------
Four bugs in a row were found by *using* the app, not by the test suite: a turn's
narration vanished when it completed, thinking was never persisted, a slash
command's answer flashed for a few milliseconds because it went out as a
transient status, and the whole plugin tree was unreadable to the shell so a
skill could not run its own script.  Every one of them lived in the seam between
two well-tested halves, where a unit test that fakes one side cannot see it.

So this runner exercises the seams the way a browser does: it starts the real
gateway on a free port over a **scratch agent home**, drives it with real HTTP
and a real WebSocket, and asserts on what a client would actually receive --
frames on the socket, and rows in the transcript the client re-reads the moment
a turn ends.

It never touches the real agent home.  Provider keys are copied from the real
``config.json`` so turns can run; when there is no usable provider the
model-dependent checks are *skipped* and said so, rather than passing quietly.

Usage
-----
    uv run python scripts/functional_smoke.py
    uv run python scripts/functional_smoke.py --keep-home   # leave the scratch home

Exit code is 0 when nothing failed (skips are not failures).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

OK = "ok"
FAIL = "fail"
SKIP = "skip"


class Results:
    """What each check found, and the evidence a reader needs to believe it."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, evidence: str) -> None:
        self.rows.append((status, name, evidence.replace("\n", " ")[:110]))

    @property
    def failed(self) -> int:
        return sum(1 for status, _, _ in self.rows if status == FAIL)

    def report(self) -> int:
        for status, name, evidence in self.rows:
            print(f"{status:4s} {name:34s} {evidence}")
        passed = sum(1 for status, _, _ in self.rows if status == OK)
        skipped = sum(1 for status, _, _ in self.rows if status == SKIP)
        print(f"\n{passed} ok, {self.failed} failed, {skipped} skipped")
        return 1 if self.failed else 0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_scratch_home(port: int) -> tuple[Path, bool]:
    """A private agent home with the web channel enabled.

    Returns the home and whether a provider key came with it: without one the
    checks that need a model report themselves as skipped.
    """
    home = Path(tempfile.mkdtemp(prefix="agent-smoke-"))
    real = Path.home() / ".agent" / "config.json"
    config: dict[str, Any] = {}
    if real.is_file():
        try:
            config = json.loads(real.read_text(encoding="utf-8"))
        except ValueError:
            config = {}
    config["channels"] = {"web": {"enabled": True, "port": port}}
    (home / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    has_provider = any(
        isinstance(section, dict) and section.get("api_key")
        for section in (config.get("providers") or {}).values()
    )
    return home, has_provider


def start_gateway(home: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["SIMPLE_AGENT_HOME"] = str(home)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from agent.cli import app; app()",
            "gateway",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process


async def wait_for_health(base: str, process: subprocess.Popen, seconds: float = 40) -> None:
    import httpx

    deadline = time.time() + seconds
    async with httpx.AsyncClient(timeout=5) as client:
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise RuntimeError(f"gateway exited ({process.returncode}):\n{output[-1500:]}")
            try:
                if (await client.get(f"{base}/api/health")).status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.4)
    raise RuntimeError(f"gateway did not answer on {base}/api/health within {seconds}s")


async def ws_turn(base: str, session_id: str, text: str, timeout: float = 180) -> list[dict]:
    """One message over the session's stream, up to its terminal frame."""
    import websockets

    frames: list[dict] = []
    url = base.replace("http://", "ws://") + f"/api/sessions/{session_id}/stream"
    async with websockets.connect(url, max_size=None) as socket_:
        await socket_.send(json.dumps({"type": "message", "text": text}))
        while True:
            try:
                frame = json.loads(await asyncio.wait_for(socket_.recv(), timeout=timeout))
            except asyncio.TimeoutError:
                frames.append({"type": "(timeout)"})
                return frames
            frames.append(frame)
            if frame.get("type") in ("turn_complete", "error"):
                return frames


async def new_session(client: Any, base: str) -> str:
    response = await client.post(f"{base}/api/sessions")
    response.raise_for_status()
    return str(response.json()["session_id"])


async def records(client: Any, base: str, session_id: str) -> list[dict]:
    response = await client.get(f"{base}/api/sessions/{session_id}/messages")
    response.raise_for_status()
    return list(response.json().get("messages") or [])


async def check_endpoints(client: Any, base: str, results: Results) -> None:
    paths = (
        "/api/health",
        "/api/config",
        "/api/commands",
        "/api/context",
        "/api/plugins",
        "/api/skills",
        "/api/scheduler/health",
        "/api/schedules",
        "/api/sessions",
    )
    bad: list[str] = []
    for path in paths:
        response = await client.get(f"{base}{path}")
        if response.status_code != 200:
            bad.append(f"{path} -> {response.status_code}")
    results.add(
        "read-only surface answers",
        FAIL if bad else OK,
        "; ".join(bad) if bad else f"{len(paths)} endpoints, all 200",
    )


async def check_session_lifecycle(client: Any, base: str, results: Results) -> None:
    session_id = await new_session(client, base)
    try:
        listed = [s["session_id"] for s in (await client.get(f"{base}/api/sessions")).json()["sessions"]]
        renamed = await client.patch(f"{base}/api/sessions/{session_id}", json={"title": "smoke"})
        deleted = await client.delete(f"{base}/api/sessions/{session_id}")
        again = await client.delete(f"{base}/api/sessions/{session_id}")
        gone = session_id not in [
            s["session_id"] for s in (await client.get(f"{base}/api/sessions")).json()["sessions"]
        ]
        problems = []
        if session_id not in listed:
            problems.append("created session is not listed")
        if renamed.status_code != 200:
            problems.append(f"rename -> {renamed.status_code}")
        if deleted.json().get("ok") is not True:
            problems.append(f"delete -> {deleted.text[:60]}")
        if again.status_code != 200:
            problems.append(f"second delete -> {again.status_code} (not idempotent)")
        if not gone:
            problems.append("deleted session is still listed")
        results.add(
            "session lifecycle",
            FAIL if problems else OK,
            "; ".join(problems) if problems else "create/list/rename/delete/delete-again",
        )
    finally:
        await client.delete(f"{base}/api/sessions/{session_id}")


async def check_slash_command(client: Any, base: str, results: Results) -> None:
    """A command's answer must be a reply, and must survive a reload.

    It went out as a *status* -- the line saying what the agent is doing right
    now -- which the frame ending the command then cleared, so the answer was
    visible for milliseconds while the server had run it and logged it.
    """
    session_id = await new_session(client, base)
    try:
        frames = await ws_turn(base, session_id, "/help")
        command_frames = [f for f in frames if f.get("type") == "command_result"]
        text = str(command_frames[0].get("text") or "") if command_frames else ""
        rows = await records(client, base, session_id)
        command_rows = [r for r in rows if r.get("role") == "command"]
        problems = []
        if not command_frames:
            problems.append(f"no command_result frame (saw {sorted({f.get('type') for f in frames})})")
        if not any(str(r.get("content") or "").strip() == text.strip() for r in command_rows):
            problems.append("the answer is not in the record the client re-reads")
        results.add(
            "slash command is a reply",
            FAIL if problems else OK,
            "; ".join(problems) if problems else f"/help -> {len(text)} chars, journalled",
        )
    finally:
        await client.delete(f"{base}/api/sessions/{session_id}")


async def check_workspace_is_not_the_cwd(client: Any, base: str, results: Results) -> None:
    """A server conversation must not inherit the directory it was launched from.

    The gateway's cwd is wherever the shell happened to be, and every session
    that had not chosen a folder used to inherit it -- reading, and (once a
    folder was granted) writing, whatever that happened to be.
    """
    session_id = await new_session(client, base)
    try:
        frames = await ws_turn(base, session_id, "/workspace")
        text = " ".join(
            str(f.get("text") or "") for f in frames if f.get("type") == "command_result"
        )
        workspace = text.split("`")[1] if "`" in text else ""
        problems = []
        if not workspace:
            # This check reads the workspace through ``/workspace``, and that is
            # the only truthful source: it reports what the *runtime* resolved.
            # So a broken command path turns this red too, and says why.
            problems.append(
                "the /workspace command reported nothing "
                f"(command output: {text[:60]!r})"
            )
        elif Path(workspace).resolve() == Path.cwd().resolve():
            problems.append(f"workspace is the cwd ({workspace})")
        results.add(
            "workspace is not the cwd",
            FAIL if problems else OK,
            "; ".join(problems) if problems else workspace,
        )
    finally:
        await client.delete(f"{base}/api/sessions/{session_id}")


async def check_window_is_whole_turns(client: Any, base: str, home: Path, results: Results) -> None:
    """A turn's tool calls must not push the conversation out of its own window.

    Measured on a real session: five conversation rows plus one turn making a
    hundred tool calls answered ``limit=200`` with 101 tool rows and *no*
    assistant row -- the reply sat 1200 positions behind the cursor, so a reader
    had to page back through every tool call to see the answer.  The rows here
    are written through the same store the gateway reads, so this is the real
    read path with a real long turn in it.
    """
    from agent.memory.store import LTMStore

    session_id = await new_session(client, base)
    store = LTMStore(context_dir=home / "context", memory_dir=home / "memory")
    reply = "Recon done: 44 styles"
    store.write_conversation_exchange(
        session_id=session_id,
        user_content="融合这两个技能",
        assistant_content=reply,
        channel="web",
        message_id="turn-1",
        assistant_message_id="turn-1",
    )
    for index in range(120):
        for event_type in ("tool_started", "tool_completed"):
            store.append_agent_event(
                session_id=session_id,
                event_type=event_type,
                payload={"operation_id": f"op-{index}", "tool_name": "shell"},
                turn_id="turn-2",
            )
    rows = await records(client, base, session_id)
    answers = [r for r in rows if r.get("role") == "assistant"]
    problems = []
    if not answers:
        problems.append(
            f"the first window holds {len(rows)} rows and no reply "
            f"(roles: {sorted({r.get('role') for r in rows})})"
        )
    elif reply not in str(answers[-1].get("content") or ""):
        problems.append(f"the reply came back as {str(answers[-1].get('content'))[:40]!r}")
    results.add(
        "window is whole turns",
        FAIL if problems else OK,
        "; ".join(problems) if problems else f"120 tool calls, reply still in the first window",
    )


async def check_turn_record_holds_the_stream(
    client: Any, base: str, results: Results
) -> None:
    """Everything the socket showed for a turn must be in the record.

    The client re-reads the record the moment a turn ends and replaces what it
    streamed with it, so a record narrower than the stream is content
    disappearing in front of the reader.
    """
    session_id = await new_session(client, base)
    try:
        frames = await ws_turn(
            base,
            session_id,
            "先用一句话说明你打算做什么，然后运行 shell 命令 echo smoke，最后用一句话总结。",
        )
        streamed = "".join(f.get("chunk", "") for f in frames if f.get("type") == "stream_chunk")
        reasoning = "".join(
            f.get("chunk", "") for f in frames if f.get("type") == "reasoning_chunk"
        )
        rows = await records(client, base, session_id)
        assistant_text = "".join(
            str(r.get("content") or "") for r in rows if r.get("role") == "assistant"
        )
        assistant_reasoning = "".join(
            str(r.get("reasoning") or "") for r in rows if r.get("role") == "assistant"
        )
        problems = []
        if not streamed.strip():
            problems.append("the turn streamed nothing (cannot judge the record)")
        elif not assistant_text.strip().startswith(streamed.strip()):
            problems.append(
                f"the record is missing what was streamed "
                f"(stream={len(streamed)} record={len(assistant_text)})"
            )
        if reasoning.strip() and not assistant_reasoning.strip():
            problems.append(f"thinking was streamed ({len(reasoning)} chars) but not recorded")
        results.add(
            "record holds the stream",
            FAIL if problems else OK,
            "; ".join(problems)
            if problems
            else f"text {len(streamed)}, thinking {len(reasoning)} both in the record",
        )
    finally:
        await client.delete(f"{base}/api/sessions/{session_id}")


async def run_checks(
    base: str, results: Results, has_provider: bool, home: Path
) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=90) as client:
        await wait_for_health(base, CURRENT_PROCESS)
        await check_endpoints(client, base, results)
        await check_session_lifecycle(client, base, results)
        await check_slash_command(client, base, results)
        await check_workspace_is_not_the_cwd(client, base, results)
        await check_window_is_whole_turns(client, base, home, results)
        if has_provider:
            await check_turn_record_holds_the_stream(client, base, results)
        else:
            results.add(
                "record holds the stream",
                SKIP,
                "no provider key in the real config: a turn cannot run",
            )


CURRENT_PROCESS: subprocess.Popen  # set by main(); the health wait needs it


def main() -> int:
    global CURRENT_PROCESS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-home",
        action="store_true",
        help="leave the scratch agent home behind for inspection",
    )
    args = parser.parse_args()

    port = free_port()
    home, has_provider = make_scratch_home(port)
    base = f"http://127.0.0.1:{port}"
    results = Results()
    print(f"scratch home: {home}\ngateway:      {base}\n")

    process = start_gateway(home, port)
    CURRENT_PROCESS = process
    try:
        asyncio.run(run_checks(base, results, has_provider, home))
    except Exception as exc:  # noqa: BLE001 - the report is the point
        results.add("harness", FAIL, f"{type(exc).__name__}: {exc}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        if not args.keep_home:
            shutil.rmtree(home, ignore_errors=True)

    return results.report()


if __name__ == "__main__":
    raise SystemExit(main())
