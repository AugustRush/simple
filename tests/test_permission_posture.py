"""The permission posture must be visible, honest, and recoverable.

These behaviours had no tests, and the gap showed up in the author's own
config: `shell_sandbox: none` had been set once for a GPU problem it did not
actually solve, and had survived for months because nothing anywhere said it
was on — not startup, not `/permissions`, not the status row.

Measured, not assumed: GPU (PyTorch MPS and MLX) works under `read_all` with
the default `shell_devices: true`. The knob for GPU is `shell_devices`; the
sandbox never had to be disabled for it.
"""

from __future__ import annotations

import pytest

from agent.security.filesystem_sandbox import (
    SANDBOX_MODE_NONE,
    SANDBOX_MODE_READ_ALL,
    SANDBOX_MODE_RESTRICTED,
    _DEVICE_SANDBOX_RULES,
    ShellSandboxRequest,
    _macos_seatbelt_profile,
    effective_sandbox_mode,
    looks_like_sandbox_denial,
    narrow_alternatives_hint,
    sandbox_downgrade_note,
    sandbox_posture_warning,
)


# ── Visibility ─────────────────────────────────────────────────────────────


def test_unsandboxed_posture_produces_a_warning():
    warning = sandbox_posture_warning(SANDBOX_MODE_NONE)
    assert warning
    assert "OFF" in warning
    # It must name the recovery, not merely scold.
    assert "read_all" in warning


def test_sandboxed_postures_produce_no_warning():
    """A warning on a safe posture trains people to ignore warnings."""
    assert sandbox_posture_warning(SANDBOX_MODE_READ_ALL) == ""
    assert sandbox_posture_warning(SANDBOX_MODE_RESTRICTED) == ""


def test_the_warning_says_gpu_does_not_require_disabling_the_sandbox():
    """The specific misconception that cost the author their sandbox."""
    assert "GPU" in sandbox_posture_warning(SANDBOX_MODE_NONE)
    assert "shell_devices" in sandbox_posture_warning(SANDBOX_MODE_NONE)


# ── Display must match enforcement ─────────────────────────────────────────


@pytest.mark.parametrize(
    "mode, level, expected",
    [
        (SANDBOX_MODE_NONE, "ask", SANDBOX_MODE_READ_ALL),
        (SANDBOX_MODE_NONE, "medium", SANDBOX_MODE_READ_ALL),
        (SANDBOX_MODE_NONE, "high", SANDBOX_MODE_READ_ALL),
        (SANDBOX_MODE_NONE, "full", SANDBOX_MODE_NONE),
        (SANDBOX_MODE_READ_ALL, "ask", SANDBOX_MODE_READ_ALL),
        (SANDBOX_MODE_RESTRICTED, "full", SANDBOX_MODE_RESTRICTED),
    ],
)
def test_effective_mode_applies_the_level_linkage(mode, level, expected):
    assert effective_sandbox_mode(mode, level) == expected


def test_a_silent_downgrade_is_explained():
    """`/permissions` used to report `none` while the shell ran `read_all`.

    A status display that disagrees with enforcement is worse than none at
    all, because it is the one the user trusts.
    """
    note = sandbox_downgrade_note(SANDBOX_MODE_NONE, "ask")
    assert note
    assert "full" in note and "read_all" in note

    assert sandbox_downgrade_note(SANDBOX_MODE_NONE, "full") == ""
    assert sandbox_downgrade_note(SANDBOX_MODE_READ_ALL, "ask") == ""


# ── Narrow alternatives at the point of failure ────────────────────────────


def test_denial_detection_distinguishes_sandbox_from_ordinary_failure():
    assert looks_like_sandbox_denial("cat: x: Operation not permitted")
    assert not looks_like_sandbox_denial("bash: frobnicate: command not found")
    assert not looks_like_sandbox_denial("")


def test_the_hint_names_narrow_knobs_before_the_big_switch():
    """`none` becomes the obvious fix only when nothing else is visible."""
    hint = narrow_alternatives_hint(SANDBOX_MODE_READ_ALL)
    for knob in ("shell_devices", "write_scope", "shell_secret_paths"):
        assert knob in hint
    # And it must say plainly that the big switch is usually wrong.
    assert "rarely" in hint


def test_no_hint_when_already_unsandboxed():
    """Nothing was denied by a sandbox that is not running."""
    assert narrow_alternatives_hint(SANDBOX_MODE_NONE) == ""


# ── The GPU claim, checked against the profile ─────────────────────────────


def _request(tmp_path, *, mode=SANDBOX_MODE_READ_ALL, devices=True):
    return ShellSandboxRequest(
        workspace_root=tmp_path / "workspace",
        output_root=tmp_path / "output",
        workspace_read=True,
        workspace_write=False,
        write_scope=(),
        scratch_dir=tmp_path / "output" / "sandbox" / "tmp",
        mode=mode,
        devices=devices,
        home_dir=tmp_path / "home",
    )


@pytest.mark.parametrize("mode", [SANDBOX_MODE_READ_ALL, SANDBOX_MODE_RESTRICTED])
def test_gpu_services_are_reachable_in_every_sandboxed_mode(tmp_path, mode):
    """Disabling the sandbox for GPU access is unnecessary, and this is why.

    Verified out of band against real workloads: PyTorch MPS and MLX both
    run under `read_all` with `shell_devices: true`, matching unsandboxed
    behaviour, and both fail with `devices: false`. `shell_devices` is the
    knob; `shell_sandbox: none` is not.
    """
    profile = _macos_seatbelt_profile(_request(tmp_path, mode=mode, devices=True))
    for rule in _DEVICE_SANDBOX_RULES:
        assert rule in profile


def test_devices_false_is_what_actually_removes_gpu(tmp_path):
    profile = _macos_seatbelt_profile(_request(tmp_path, devices=False))
    assert '(allow mach-lookup (global-name "com.apple.Metal"))' not in profile
    assert "(allow iokit-open)" not in profile
