"""The unattended envelope: what a scheduled task is allowed to do, and what
it is told when it asks for more.

These tests pin three properties that are easy to lose in a refactor and
expensive to lose in production:

1. a profile applies exactly the config overrides it advertises, and applies
   them without mutating the caller's dict;
2. an unknown profile fails *closed* (to ``read_only``) rather than silently
   inheriting whatever the process was started with;
3. the unattended sink refuses every consent request and records it, because
   its refusal is the only thing standing between a background run and an
   irreversible action nobody agreed to.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    PERMISSION_PROFILES,
    UnattendedAudit,
    UnattendedOutputSink,
)
from agent.scheduler.profiles import (
    PERMISSION_PROFILES,
    FALLBACK_PROFILE_KEY,
    apply_profile_to_config,
    describe_profile_for_prompt,
    profile_payloads,
    resolve_permission_profile,
)


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp``.

    Overrides pytest's own fixture, which fails in this environment: its
    per-run root under the system temp directory cannot be created when the
    session is sandboxed, so every test that asks for ``tmp_path`` errors out
    before it runs.  ``/tmp`` is writable and just as disposable.
    """
    path = Path("/tmp") / f"simple-prof-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def _base_config() -> dict:
    return {
        "file_access": {"workspace": {"read": True, "write": False}},
        "permissions": {"shell_level": "ask", "shell_sandbox": "read_all"},
    }


# ── profiles ────────────────────────────────────────────────────────────────


def test_inherit_profile_applies_no_overrides():
    profile = PERMISSION_PROFILES["inherit"]

    assert apply_profile_to_config(_base_config(), profile) == _base_config()
    assert profile.overrides_config is False


def test_read_only_profile_denies_writes_and_pins_shell_to_ask():
    resolved = apply_profile_to_config(
        _base_config(), PERMISSION_PROFILES["read_only"]
    )

    assert resolved["file_access"]["workspace"]["write"] is False
    assert resolved["file_access"]["workspace"]["read"] is True
    # Pinning the level is a deliberate strengthening: leaving it inherited
    # meant a `read_only` task on a machine configured with `shell_level:
    # high` could delete the workspace through the shell.
    assert resolved["permissions"]["shell_level"] == "ask"
    assert resolved["permissions"]["shell_sandbox"] == "read_all"


def test_workspace_write_profile_grants_writes_without_asking():
    resolved = apply_profile_to_config(
        _base_config(), PERMISSION_PROFILES["workspace_write"]
    )

    assert resolved["file_access"]["workspace"]["write"] is True
    assert resolved["permissions"]["shell_level"] == "high"
    assert resolved["permissions"]["shell_sandbox"] == "read_all"


def test_applying_a_profile_does_not_mutate_the_caller_config():
    original = _base_config()

    apply_profile_to_config(original, PERMISSION_PROFILES["workspace_write"])

    assert original == _base_config()


def test_unknown_profile_fails_closed_to_read_only():
    profile = resolve_permission_profile("full_access_please")

    assert profile.key == FALLBACK_PROFILE_KEY == "read_only"
    assert profile.workspace_write is False


def test_blank_profile_keeps_the_legacy_default():
    assert resolve_permission_profile("").key == "inherit"
    assert resolve_permission_profile(None).key == "inherit"


def test_only_write_capable_profiles_demand_an_explicit_workspace():
    demanding = {
        key for key, item in PERMISSION_PROFILES.items()
        if item.requires_workspace_root
    }

    assert demanding == {"workspace_write"}


def test_profile_payloads_hide_internal_flags():
    payloads = profile_payloads()

    assert [item["key"] for item in payloads] == [
        "inherit", "read_only", "workspace_write"
    ]
    for item in payloads:
        # Which config keys a profile writes is part of its contract and is
        # worth showing; the flags that only drive this module's own logic
        # are not.
        assert "overrides_config" not in item
        assert "requires_workspace_root" not in item
        assert item["label"] and item["summary"]
        assert isinstance(item["workspace_write"], bool)
        assert item["shell_level"] in {"ask", "medium", "high", "full"}


def test_prompt_text_names_the_profile_and_forbids_retrying_refusals():
    text = describe_profile_for_prompt(PERMISSION_PROFILES["workspace_write"])

    assert "workspace_write" in text
    assert "unattended" in text
    assert "refused" in text


# ── the unattended sink ─────────────────────────────────────────────────────


def test_unattended_sink_never_claims_a_human_can_be_asked():
    sink = UnattendedOutputSink()

    assert sink.interactive_confirmation is False
    # A scheduled run has no live UI; pretending to stream wastes the turn.
    assert sink.streaming is False


def test_unattended_sink_refuses_every_consent_request_and_records_it():
    audit = UnattendedAudit(task_id="t1", run_id="r1")
    sink = UnattendedOutputSink(audit=audit)

    async def ask(tool: str, command: str) -> bool:
        return await sink.on_tool_confirmation(
            tool,
            command=command,
            risk_level="high",
            reason="needs a person",
            confirmation_token="tok",
            scope=None,
        )

    first = asyncio.run(ask("shell", "pip install requests"))
    second = asyncio.run(ask("install_plugin", "./evil"))

    assert first is False
    assert second is False
    assert audit.denied_count == 2
    assert [decision.tool for decision in audit.decisions] == [
        "shell", "install_plugin"
    ]


def test_audit_stays_silent_when_nothing_happened():
    audit = UnattendedAudit(task_id="t1", run_id="r1")

    assert audit.has_findings() is False
    assert audit.report() == ""
    assert audit.write_report(Path("/tmp/never-written.md")) is None


def test_audit_report_lists_refusals_with_the_profile(tmp_path):
    audit = UnattendedAudit(
        task_id="t1",
        run_id="r1",
        profile=PERMISSION_PROFILES["workspace_write"],
    )
    audit.note_consent_request(
        tool="shell",
        command="pip install requests",
        risk_level="high",
        reason="environment mutation is confirmed at every level",
    )

    report = audit.report()
    written = audit.write_report(tmp_path / "artifacts" / "unattended-audit.md")

    assert "workspace_write" in report
    assert "pip install requests" in report
    assert written is not None
    assert written.read_text(encoding="utf-8") == report


def test_audit_records_a_substituted_profile_as_a_finding():
    audit = UnattendedAudit(profile=PERMISSION_PROFILES["read_only"])
    audit.note("未知的权限档位 `nope`，已按更保守的 `read_only` 执行。")

    assert audit.has_findings() is True
    assert "read_only" in audit.report()


def test_audit_bounds_what_it_keeps():
    audit = UnattendedAudit()
    for index in range(120):
        audit.note_consent_request(
            tool="shell",
            command=f"cmd-{index}",
            risk_level="high",
            reason="needs a person",
        )

    assert len(audit.decisions) <= 50
    assert audit.denied_count == 120
    assert "未逐条记录" in audit.report()


def test_audit_survives_an_unwritable_report_path(tmp_path):
    audit = UnattendedAudit()
    audit.note("something happened")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    # An audit that cannot be persisted must not fail an otherwise good run.
    assert audit.write_report(blocker / "nested" / "audit.md") is None


# ── the API surface ─────────────────────────────────────────────────────────


def _schedule_body(**overrides) -> dict:
    body = {
        "name": "nightly",
        "action_type": "agent_task",
        "prompt": "run the test suite and report",
        "trigger_type": "once",
        "at": "2099-01-01T00:00:00+00:00",
        "timezone_name": "UTC",
        "delivery_mode": "standalone",
    }
    body.update(overrides)
    return body


def _web_channel_stub():
    from agent.channels.web import WebChannel

    channel = object.__new__(WebChannel)
    channel._components = {}
    return channel


def test_write_profile_requires_the_user_to_choose_a_directory():
    channel = _web_channel_stub()

    with pytest.raises(ValueError, match="项目文件夹"):
        channel._schedule_from_body(_schedule_body(permission_profile="workspace_write"))


def test_write_profile_is_accepted_with_an_explicit_workspace(tmp_path):
    channel = _web_channel_stub()

    task = channel._schedule_from_body(
        _schedule_body(
            permission_profile="workspace_write",
            workspace_root=str(tmp_path),
        )
    )

    assert task.permission_profile == "workspace_write"
    assert Path(task.workspace_root) == tmp_path.resolve()


def test_inherit_profile_still_tolerates_a_missing_workspace():
    channel = _web_channel_stub()

    task = channel._schedule_from_body(_schedule_body())

    assert task.permission_profile == "inherit"
    assert task.workspace_root


def test_api_rejects_a_profile_it_does_not_know():
    channel = _web_channel_stub()

    with pytest.raises(ValueError, match="不支持的权限策略"):
        channel._schedule_from_body(_schedule_body(permission_profile="root"))


# ── A profile's effect on config is patches, and the paths are declared ──────


def test_every_profile_patch_names_a_declared_path() -> None:
    """The check bundles have had, and profiles did not.

    A profile's config effect used to be four hand-written assignments.  A guessed key
    was invisible for the same reason a guessed bundle path was: assigning to a mapping
    that never had the key succeeds, and the setting silently does nothing.  Bundles got
    ``test_every_patch_names_a_declared_path`` after two invented paths slipped through;
    this is that check, and it is only *possible* now that a profile's effect is data.
    """
    from agent.config import DEFAULT_CONFIG
    from agent.config_patch import UnknownConfigPath

    for key, profile in PERMISSION_PROFILES.items():
        for patch in profile.config_patches:
            node: Any = DEFAULT_CONFIG
            try:
                for part in patch.path.split("."):
                    node = node[part]
            except (KeyError, TypeError) as exc:
                pytest.fail(f"profile {key!r} patches {patch.path!r}: {exc}")


def test_a_profile_that_overrides_nothing_has_no_patches() -> None:
    """``inherit`` applies nothing, and says so as an empty tuple rather than a flag.

    The old form returned early on ``overrides_config``; the patches form makes the same
    statement as data, so a caller cannot apply the fields while forgetting the flag.
    """
    inherit = PERMISSION_PROFILES["inherit"]
    assert inherit.overrides_config is False
    assert inherit.config_patches == ()


def test_applying_a_profile_is_pure() -> None:
    """Nested sections are copied, so a run's override cannot leak into the process.

    The property the hand-written version promised in its docstring and got by copying
    each section it touched.  ``apply_patch`` copies deeply, which is strictly stronger:
    a section the profile does not touch is also not shared.
    """
    from agent.config import DEFAULT_CONFIG
    from agent.scheduler.profiles import apply_profile_to_config

    before = copy.deepcopy(DEFAULT_CONFIG)
    result = apply_profile_to_config(DEFAULT_CONFIG, PERMISSION_PROFILES["workspace_write"])

    assert DEFAULT_CONFIG == before, "the input must not be mutated"
    assert result is not DEFAULT_CONFIG
    assert result["file_access"] is not DEFAULT_CONFIG["file_access"], (
        "a shared section is how a run-level override leaks back"
    )


def test_the_patches_say_what_the_hand_written_version_did() -> None:
    """The four effects, asserted individually rather than as a config equality.

    A config equality would pass while the mechanism was the hand-written one, which is
    exactly the state this replaced.  Naming the four makes the statement about the
    profile's *posture*, which is what a reader is checking.
    """
    from agent.config import DEFAULT_CONFIG
    from agent.scheduler.profiles import apply_profile_to_config

    result = apply_profile_to_config(DEFAULT_CONFIG, PERMISSION_PROFILES["workspace_write"])
    assert result["file_access"]["workspace"]["read"] is True
    assert result["file_access"]["workspace"]["write"] is True
    assert result["permissions"]["shell_level"] == "high"
    assert result["permissions"]["shell_sandbox"] == "read_all"

    read_only = apply_profile_to_config(DEFAULT_CONFIG, PERMISSION_PROFILES["read_only"])
    assert read_only["file_access"]["workspace"]["read"] is True
    assert read_only["file_access"]["workspace"]["write"] is False
    assert read_only["permissions"]["shell_level"] == "ask"


def test_a_profile_applies_to_a_config_that_lacks_the_sections() -> None:
    """``allow_new``, for the reason bundles need it.

    A caller's fallback config need not contain every section a profile touches -- the
    scheduler's run path can be handed a last-known-good config from anywhere -- and
    refusing there would drop the posture exactly when the config was unreadable.
    """
    from agent.scheduler.profiles import apply_profile_to_config

    result = apply_profile_to_config({}, PERMISSION_PROFILES["read_only"])
    assert result["file_access"]["workspace"]["write"] is False
    assert result["permissions"]["shell_level"] == "ask"
