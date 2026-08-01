"""Focused tests for the shared file access policy and file service."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent.tools.files import (
    DEFAULT_FILE_ACCESS,
    FileAccessLimits,
    FileAccessPolicy,
    FilePolicyConfigError,
    MAX_LIST_RESULTS_CEILING,
    MAX_READ_BYTES_CEILING,
    MAX_READ_LINES_CEILING,
    MAX_REPLACEMENTS_CEILING,
    MAX_SNAPSHOT_BYTES_CEILING,
    MAX_WRITE_BYTES_CEILING,
    resolve_file_access_config,
)


# ── Defaults and validation ────────────────────────────────────────────────


def test_file_access_defaults_are_safe(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    policy = resolve_file_access_config(
        {},
        workspace_root=workspace,
        output_dir=output,
    )

    assert policy.workspace_read is True
    assert policy.workspace_write is False
    assert policy.limits == FileAccessLimits()
    assert DEFAULT_FILE_ACCESS["workspace"] == {"read": True, "write": False}


@pytest.mark.parametrize(
    "output",
    ["workspace", "workspace/out", "parent"],
)
def test_policy_rejects_overlapping_roots(tmp_path, output):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = tmp_path
    targets = {
        "workspace": workspace,
        "workspace/out": workspace / "out",
        "parent": parent,
    }
    with pytest.raises(FilePolicyConfigError, match="disjoint"):
        FileAccessPolicy.from_config(
            DEFAULT_FILE_ACCESS,
            workspace_root=workspace,
            output_root=targets[output],
        )


def test_policy_rejects_symlink_alias_overlap(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)

    with pytest.raises(FilePolicyConfigError, match="disjoint"):
        FileAccessPolicy.from_config(
            DEFAULT_FILE_ACCESS,
            workspace_root=workspace,
            output_root=alias,
        )


def test_policy_rejects_unknown_file_access_keys(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(FilePolicyConfigError, match="unknown 'file_access' key"):
        FileAccessPolicy.from_config(
            {"workspace_read": True},
            workspace_root=workspace,
            output_root=tmp_path / "output",
        )


def test_policy_rejects_non_boolean_workspace_permissions(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for key in ("read", "write"):
        with pytest.raises(FilePolicyConfigError, match="must be a boolean"):
            FileAccessPolicy.from_config(
                {"workspace": {"read": True, "write": False, key: "yes"}},
                workspace_root=workspace,
                output_root=tmp_path / "output",
            )


def test_policy_rejects_unknown_workspace_keys(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(FilePolicyConfigError, match="unknown 'file_access.workspace'"):
        FileAccessPolicy.from_config(
            {"workspace": {"read": True, "write": False, "delete": False}},
            workspace_root=workspace,
            output_root=tmp_path / "output",
        )


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("max_read_lines", 0),
        ("max_read_bytes", -1),
        ("max_snapshot_bytes", 0),
        ("max_write_bytes", -5),
        ("max_replacements", 0),
        ("max_list_results", 0),
    ],
)
def test_limits_reject_non_positive_integers(tmp_path, key, bad_value):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = {key: bad_value}
    with pytest.raises(FilePolicyConfigError, match="positive integer"):
        FileAccessPolicy.from_config(
            cfg,
            workspace_root=workspace,
            output_root=tmp_path / "output",
        )


@pytest.mark.parametrize("key", list(FileAccessLimits().__dict__))
def test_limits_reject_bools(tmp_path, key):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(FilePolicyConfigError, match="positive integer"):
        FileAccessLimits.from_config({key: True})


def test_limits_reject_unknown_keys():
    with pytest.raises(FilePolicyConfigError, match="unknown 'file_access' limit"):
        FileAccessLimits.from_config({"max_lines": 10})


@pytest.mark.parametrize(
    ("key", "ceiling"),
    [
        ("max_read_lines", MAX_READ_LINES_CEILING),
        ("max_read_bytes", MAX_READ_BYTES_CEILING),
        ("max_snapshot_bytes", MAX_SNAPSHOT_BYTES_CEILING),
        ("max_write_bytes", MAX_WRITE_BYTES_CEILING),
        ("max_replacements", MAX_REPLACEMENTS_CEILING),
        ("max_list_results", MAX_LIST_RESULTS_CEILING),
    ],
)
def test_limits_reject_values_above_ceiling(tmp_path, key, ceiling):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(FilePolicyConfigError, match="must be between"):
        FileAccessPolicy.from_config(
            {key: ceiling + 1},
            workspace_root=workspace,
            output_root=tmp_path / "output",
        )


def test_policy_accepts_valid_custom_values(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    policy = FileAccessPolicy.from_config(
        {
            "workspace": {"read": False, "write": True},
            "max_read_lines": 10,
            "max_read_bytes": 2048,
            "max_snapshot_bytes": 4096,
            "max_write_bytes": 1024,
            "max_replacements": 5,
            "max_list_results": 20,
        },
        workspace_root=workspace,
        output_root=output,
    )

    assert policy.workspace_read is False
    assert policy.workspace_write is True
    assert policy.limits.max_read_lines == 10
    assert policy.limits.max_read_bytes == 2048
    assert policy.limits.max_snapshot_bytes == 4096
    assert policy.limits.max_write_bytes == 1024
    assert policy.limits.max_replacements == 5
    assert policy.limits.max_list_results == 20


def test_policy_resolves_roots_and_is_immutable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    policy = FileAccessPolicy.from_config(
        {},
        workspace_root=str(workspace),
        output_root=str(output),
    )

    assert policy.workspace_root == workspace.resolve()
    assert policy.output_root == output.resolve()
    with pytest.raises(FrozenInstanceError):
        policy.workspace_read = False


def test_resolve_file_access_config_rejects_non_dict_section(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(FilePolicyConfigError, match="must be an object"):
        resolve_file_access_config(
            {"file_access": "deny"},
            workspace_root=workspace,
            output_dir=tmp_path / "output",
        )


def test_symlinked_workspace_root_identity_is_compared_resolved(tmp_path):
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    output = tmp_path / "output"
    alias = tmp_path / "ws-link"
    alias.symlink_to(real_workspace, target_is_directory=True)

    policy = FileAccessPolicy.from_config(
        {},
        workspace_root=alias,
        output_root=output,
    )
    assert policy.workspace_root == real_workspace.resolve()


def test_default_config_section_matches_policy_defaults(tmp_path):
    policy = FileAccessPolicy.from_config(
        DEFAULT_FILE_ACCESS,
        workspace_root=tmp_path / "workspace",
        output_root=tmp_path / "output",
    )
    assert policy.limits == FileAccessLimits()
