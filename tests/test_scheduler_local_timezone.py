"""A wall clock with no zone means the zone the person is in.

The bug these pin down: a task created by the agent with "每天 8 点" and no
``timezone_name`` was stored as UTC, because every layer from the tool schema
downwards defaulted to it.  08:00 then fired at 16:00 in Shanghai, and no part
of the record said so -- it read "UTC", which looks like an answer somebody
chose rather than a default nobody noticed.

So there are two halves to get right, and they pull in opposite directions:

* nothing may silently become UTC, and
* an explicit UTC has to stay UTC, because a caller who wrote it down meant it.

``TZ`` is how the machine's own zone is read, so these tests set it instead of
stubbing the lookup -- that exercises the real resolution, and it keeps the
assertions exact on a CI box that happens to run in UTC.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


SHANGHAI = "Asia/Shanghai"


@pytest.fixture(autouse=True)
def _isolate_scheduler_state(monkeypatch, tmp_path):
    """Keep the scheduler store inside tmp_path, as the other tool tests do."""
    import agent.shared as shared_module
    from agent.security.shell import shell_session_allowlist_clear

    agent_home = tmp_path / ".agent"
    monkeypatch.setattr(shared_module, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", agent_home / "output")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", agent_home / "tasks")
    monkeypatch.setattr(
        shared_module, "SCHEDULER_DB_FILE", agent_home / "tasks" / "scheduler.db"
    )
    shell_session_allowlist_clear()


@pytest.fixture
def in_shanghai(monkeypatch):
    """Put this machine in Shanghai without moving the real one."""
    monkeypatch.setenv("TZ", SHANGHAI)
    return ZoneInfo(SHANGHAI)


def make_registry(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = BuiltinTools(
        memory=MemoryPalace(
            base_dir=tmp_path / "memory", context_dir=tmp_path / "context"
        ),
        registry=registry,
        workspace_root=workspace,
    )
    return tools, registry


def create_settlement(registry, **overrides):
    """Ask the agent tool for a weekday 08:00 settlement, naming no zone."""
    body = {
        "name": "A股模拟盘每日结算",
        "trigger_type": "weekdays",
        "action_type": "agent_task",
        "prompt": "结算模拟盘",
        "time_of_day": "08:00",
    }
    body.update(overrides)
    return json.loads(asyncio.run(registry.call("schedule_create", body)))


def stored_task(payload):
    from agent.scheduler import SchedulerStore

    store = SchedulerStore(db_path=Path(payload["task"]["db_path"]))
    try:
        return store.get_task(payload["task"]["id"])
    finally:
        store.close()


def timezone_defaults(node):
    """Every ``timezone_name`` property in a JSON schema, however it nests.

    The workflow tool's steps are a list of objects under one property, so a
    reader that only looks at the top level would miss the second place a
    caller is told what an omitted zone means.
    """
    found = []
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict) and "timezone_name" in properties:
            found.append(properties["timezone_name"])
        for value in node.values():
            found.extend(timezone_defaults(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(timezone_defaults(item))
    return found


def test_the_machine_zone_is_read_from_the_environment(monkeypatch):
    """The lookup has to produce an IANA name, not just an offset.

    ``datetime.now().astimezone().tzinfo`` is a fixed offset on macOS, and no
    ``ZoneInfo`` can be built from it -- which is how a zone ends up quietly
    falling back to UTC instead of to the right answer.
    """
    from agent.scheduler.models import local_timezone_name

    monkeypatch.setenv("TZ", "Europe/Berlin")

    assert local_timezone_name() == "Europe/Berlin"
    ZoneInfo(local_timezone_name())


def test_a_task_created_without_a_zone_keeps_the_local_one(tmp_path, in_shanghai):
    """The tool's default is "the local zone", not UTC."""
    _tools, registry = make_registry(tmp_path)

    task = stored_task(create_settlement(registry))

    assert task.trigger.trigger_type == "weekdays"
    assert task.trigger.payload["timezone_name"] == SHANGHAI


def test_eight_oclock_means_eight_where_the_person_is(tmp_path, in_shanghai):
    """The symptom itself, in the units the user complained about.

    "每天 8 点" has to be 08:00 on the wall the human reads.  Read back in
    UTC it is 00:00, and that is the assertion that fails when the zone
    silently becomes UTC: the clock lands on 16:00 local instead.
    """
    _tools, registry = make_registry(tmp_path)
    task = stored_task(create_settlement(registry))

    fires_at = task.trigger.instantiate().next_after(datetime.now(timezone.utc))
    local = fires_at.astimezone(in_shanghai)

    assert (local.hour, local.minute) == (8, 0), (
        "08:00 must be 08:00 where the human is; "
        f"it landed at {local.hour:02d}:{local.minute:02d} local"
    )
    assert fires_at == fires_at.astimezone(timezone.utc)


def test_an_explicit_utc_is_still_taken_at_its_word(tmp_path, in_shanghai):
    """Nobody who asks for UTC should be quietly moved either.

    Same bug in the other direction: "defaults to local" must not become
    "local unless it is a zone we think you did not mean".
    """
    _tools, registry = make_registry(tmp_path)

    task = stored_task(create_settlement(registry, timezone_name="UTC"))

    assert task.trigger.payload["timezone_name"] == "UTC"
    fires_at = task.trigger.instantiate().next_after(datetime.now(timezone.utc))
    assert fires_at.hour == 8  # 08:00 UTC, exactly as written


def test_no_tool_schema_says_an_omitted_zone_means_utc(tmp_path, in_shanghai):
    """A schema is how the model learns the rule, so it has to state it.

    The default is what the model fills in when the user says "每天 8 点" and
    no zone, so a schema reading ``"UTC"`` is not documentation of a fallback
    -- it is the instruction that produced the wrong clock.
    """
    _tools, registry = make_registry(tmp_path)

    schemas = {
        tool["name"]: tool["input_schema"] for tool in registry.to_anthropic_format()
    }
    assert "timezone_name" in schemas["schedule_create"]["properties"]

    offenders = sorted(
        name
        for name, schema in schemas.items()
        for prop in timezone_defaults(schema)
        if str(prop.get("default", "")).upper() == "UTC"
    )

    assert offenders == []
