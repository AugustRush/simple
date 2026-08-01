"""Focused tests for the shared file access policy and file service."""

from dataclasses import FrozenInstanceError
import hashlib
import os
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


# ── FileService: rooted reads and stable snapshots ─────────────────────────


def _service(tmp_path, **file_access):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    cfg = dict(DEFAULT_FILE_ACCESS)
    cfg.update(file_access)
    policy = FileAccessPolicy.from_config(
        cfg,
        workspace_root=workspace,
        output_root=output,
    )
    from agent.tools.files import FileService

    return FileService(policy), workspace, output


def test_read_file_returns_content_revision_and_paging(tmp_path):
    service, workspace, _ = _service(tmp_path)
    target = workspace / "agent" / "config.py"
    target.parent.mkdir()
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = service.read_file("workspace", "agent/config.py", start_line=2, line_count=1)

    assert result["ok"] is True
    assert result["root"] == "workspace"
    assert result["path"] == "agent/config.py"
    assert result["content"] == "line2\n"
    assert result["start_line"] == 2
    assert result["end_line"] == 2
    assert result["total_lines"] == 3
    assert result["next_start_line"] == 3
    assert result["size_bytes"] == target.stat().st_size
    assert result["returned_bytes"] == len("line2\n".encode())
    assert result["encoding"] == "utf-8"
    assert result["bom"] is False
    assert result["newline"] == "lf"
    assert result["revision"] == "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()


def test_read_file_pages_to_end_of_file(tmp_path):
    service, workspace, _ = _service(tmp_path)
    target = workspace / "note.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    first = service.read_file("workspace", "note.txt", start_line=1, line_count=1)
    second = service.read_file("workspace", "note.txt", start_line=first["next_start_line"], line_count=1)

    assert first["content"] == "a\n"
    assert first["next_start_line"] == 2
    assert second["content"] == "b\n"
    assert second["next_start_line"] is None


def test_read_file_default_line_count_is_bounded_and_conservative(tmp_path):
    service, workspace, _ = _service(tmp_path)
    target = workspace / "big.txt"
    target.write_text("\n".join(f"line{i}" for i in range(500)) + "\n", encoding="utf-8")

    result = service.read_file("workspace", "big.txt")

    assert result["total_lines"] == 500
    assert result["end_line"] == 200
    assert result["next_start_line"] == 201


def test_read_file_strips_bom_but_hashes_exact_bytes(tmp_path):
    service, workspace, _ = _service(tmp_path)
    target = workspace / "note.txt"
    target.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta")

    result = service.read_file("workspace", "note.txt", start_line=1, line_count=1)

    assert result["content"] == "alpha\r\n"
    assert result["bom"] is True
    assert result["newline"] == "crlf"
    assert result["revision"] == "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()


def test_read_file_newline_metadata(tmp_path):
    service, workspace, _ = _service(tmp_path)
    cases = {
        "cr.txt": ("a\rb\r", "cr"),
        "mixed.txt": ("a\nb\r\n", "mixed"),
        "none.txt": ("plain text", "none"),
        "no-final-newline.txt": ("a\nb", "lf"),
    }
    for name, (data, expected) in cases.items():
        (workspace / name).write_text(data, encoding="utf-8")

    for name, (data, expected) in cases.items():
        result = service.read_file("workspace", name)
        assert result["newline"] == expected, name
        assert result["total_lines"] == len(data.splitlines()) or (
            result["total_lines"] == 1 and "\n" not in data and "\r" not in data
        )


def test_read_file_empty_file_is_sole_empty_range_success(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "empty.txt").write_text("", encoding="utf-8")

    result = service.read_file("workspace", "empty.txt", start_line=1)
    assert result["ok"] is True
    assert result["content"] == ""
    assert result["total_lines"] == 0
    assert result["end_line"] is None
    assert result["next_start_line"] is None
    assert result["returned_bytes"] == 0
    assert result["newline"] == "none"

    error = service.read_file("workspace", "empty.txt", start_line=2)
    assert error["ok"] is False
    assert error["error"]["code"] == "invalid_request"


def test_read_file_rejects_invalid_utf8_and_nul_bytes(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "bad.txt").write_bytes(b"\xff\xfe")
    (workspace / "nul.txt").write_bytes(b"a\x00b")
    (workspace / "utf16.txt").write_bytes(b"\xff\xfea\x00")

    for name in ("bad.txt", "nul.txt", "utf16.txt"):
        result = service.read_file("workspace", name)
        assert result["ok"] is False
        assert result["error"]["code"] == "unsupported_encoding", name


def test_read_file_rejects_absolute_and_traversal_paths(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "a.txt").write_text("a", encoding="utf-8")

    for bad_path in ("/etc/passwd", "../outside", "a/../b", "a/./b", "a//b", "a\x00b", ""):
        result = service.read_file("workspace", bad_path)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_path", bad_path


def test_read_file_rejects_unknown_root(tmp_path):
    service, workspace, _ = _service(tmp_path)
    result = service.read_file("bogus", "a.txt")
    assert result["error"]["code"] == "invalid_path"


def test_read_file_not_found_and_not_regular_file(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "sub").mkdir()

    assert service.read_file("workspace", "missing.txt")["error"]["code"] == "not_found"
    assert service.read_file("workspace", "sub")["error"]["code"] == "not_regular_file"


def test_read_file_rejects_symlinks(tmp_path):
    service, workspace, output = _service(tmp_path)
    (workspace / "real.txt").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    (workspace / "inside-link").symlink_to(workspace / "real.txt")

    assert (
        service.read_file("workspace", "escape/secret.txt")["error"]["code"]
        == "invalid_path"
    )
    assert (
        service.read_file("workspace", "inside-link")["error"]["code"]
        == "invalid_path"
    )


def test_read_file_workspace_read_denial_and_output_always_readable(tmp_path):
    service, workspace, output = _service(tmp_path, workspace={"read": False, "write": False})
    (workspace / "a.txt").write_text("ws", encoding="utf-8")
    (output / "b.txt").write_text("out", encoding="utf-8")

    denied = service.read_file("workspace", "a.txt")
    assert denied["ok"] is False
    assert denied["error"]["code"] == "access_denied"
    assert denied["error"]["retryable"] is False

    allowed = service.read_file("output_dir", "b.txt")
    assert allowed["ok"] is True


def test_read_file_rejects_invalid_ranges_and_line_counts(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")

    for bad in (
        {"start_line": 0},
        {"start_line": True},
        {"start_line": 4},
        {"line_count": 0},
        {"line_count": -1},
        {"line_count": "10"},
        {"line_count": 401},
    ):
        result = service.read_file("workspace", "a.txt", **bad)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_request", bad


def test_read_file_byte_bound_stops_at_complete_line_boundary(tmp_path):
    service, workspace, _ = _service(
        tmp_path, max_read_bytes=3, max_read_lines=10
    )
    (workspace / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")

    first = service.read_file("workspace", "a.txt", start_line=1, line_count=10)
    assert first["content"] == "a\n"
    assert first["end_line"] == 1
    assert first["next_start_line"] == 2


def test_read_file_line_too_large(tmp_path):
    service, workspace, _ = _service(tmp_path, max_read_bytes=4, max_read_lines=10)
    (workspace / "a.txt").write_text("a" * 20 + "\n", encoding="utf-8")

    result = service.read_file("workspace", "a.txt")
    assert result["ok"] is False
    assert result["error"]["code"] == "line_too_large"


def test_read_file_file_too_large(tmp_path):
    service, workspace, _ = _service(tmp_path, max_snapshot_bytes=16)
    (workspace / "a.txt").write_text("x" * 32, encoding="utf-8")

    result = service.read_file("workspace", "a.txt")
    assert result["ok"] is False
    assert result["error"]["code"] == "file_too_large"


def test_read_file_detects_mutation_during_read(tmp_path):
    from agent.tools.files import FileService

    service, workspace, _ = _service(tmp_path)
    target = workspace / "a.txt"
    target.write_text("original\n", encoding="utf-8")

    class _MutatingService(FileService):
        def __init__(self, policy, mutate_target):
            super().__init__(policy)
            self.mutate_target = mutate_target
            self.mutated = False

        def _read_chunk(self, fd, size):
            data = super()._read_chunk(fd, size)
            if data and not self.mutated:
                self.mutated = True
                with open(self.mutate_target, "a", encoding="utf-8") as fh:
                    fh.write("appended")
            return data

    result = _MutatingService(service.policy, target).read_file("workspace", "a.txt")
    assert result["ok"] is False
    assert result["error"]["code"] == "revision_conflict"
    assert result["error"]["retryable"] is True


# ── FileService: bounded deterministic directory enumeration ───────────────


def _make_tree(workspace: Path) -> None:
    (workspace / "a.txt").write_text("aaaaaaaaaa", encoding="utf-8")
    (workspace / "b").mkdir()
    (workspace / "b" / "c.txt").write_text("cc", encoding="utf-8")
    (workspace / "b" / "d").mkdir()
    (workspace / "b" / "d" / "e.txt").write_text("e", encoding="utf-8")
    (workspace / "z.txt").write_text("z", encoding="utf-8")


def test_list_files_non_recursive_basic(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    result = service.list_files("workspace", ".")

    assert result["ok"] is True
    assert result["count"] == 3
    assert [item["path"] for item in result["items"]] == ["a.txt", "b", "z.txt"]
    assert result["items"][0] == {
        "path": "a.txt",
        "kind": "file",
        "size_bytes": 10,
    }
    assert result["items"][1] == {
        "path": "b",
        "kind": "directory",
        "size_bytes": None,
    }
    assert result["truncated"] is False
    assert result["next_cursor"] is None


def test_list_files_recursive_deterministic_order(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    result = service.list_files("workspace", ".", recursive=True)

    assert [item["path"] for item in result["items"]] == [
        "a.txt",
        "b",
        "b/c.txt",
        "b/d",
        "b/d/e.txt",
        "z.txt",
    ]


def test_list_files_request_path_prefix_is_included(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    result = service.list_files("workspace", "b", recursive=True)

    assert [item["path"] for item in result["items"]] == [
        "b/c.txt",
        "b/d",
        "b/d/e.txt",
    ]
    assert result["path"] == "b"


def test_list_files_pattern_matches_basename_only(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    result = service.list_files("workspace", ".", recursive=True, pattern="*.txt")

    assert [item["path"] for item in result["items"]] == [
        "a.txt",
        "b/c.txt",
        "b/d/e.txt",
        "z.txt",
    ]


def test_list_files_missing_and_non_directory(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    missing = service.list_files("workspace", "nope")
    assert missing["error"]["code"] == "not_found"

    not_dir = service.list_files("workspace", "a.txt")
    assert not_dir["error"]["code"] == "not_directory"


def test_list_files_invalid_paths_and_patterns(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    for bad_path in ("/abs", "a/..", "a/./b", "a//b", ""):
        result = service.list_files("workspace", bad_path)
        assert result["error"]["code"] == "invalid_path", bad_path

    for bad_pattern in ("", "a/b"):
        result = service.list_files("workspace", ".", pattern=bad_pattern)
        assert result["error"]["code"] == "invalid_request", bad_pattern

    for bad_max in (0, -1, "2", True, 1001):
        result = service.list_files("workspace", ".", max_results=bad_max)
        assert result["error"]["code"] == "invalid_request", bad_max


def test_list_files_pagination_with_cursor(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    first = service.list_files(
        "workspace", ".", recursive=True, max_results=3
    )
    assert first["truncated"] is True
    assert first["next_cursor"] is not None
    assert [item["path"] for item in first["items"]] == ["a.txt", "b", "b/c.txt"]

    second = service.list_files(
        "workspace", ".", recursive=True, max_results=3, cursor=first["next_cursor"]
    )
    assert second["truncated"] is False
    assert second["next_cursor"] is None
    assert [item["path"] for item in second["items"]] == [
        "b/d",
        "b/d/e.txt",
        "z.txt",
    ]

    full = [item["path"] for item in first["items"] + second["items"]]
    assert full == ["a.txt", "b", "b/c.txt", "b/d", "b/d/e.txt", "z.txt"]


def test_list_files_cursor_is_bound_to_request_parameters(tmp_path):
    service, workspace, _ = _service(tmp_path)
    _make_tree(workspace)

    first = service.list_files(
        "workspace", ".", recursive=True, pattern="*.txt", max_results=2
    )
    cursor = first["next_cursor"]
    assert cursor is not None

    for kwargs in (
        {"recursive": False},
        {"pattern": "*"},
        {"path": "b"},
    ):
        result = service.list_files(
            "workspace",
            kwargs.get("path", "."),
            recursive=kwargs.get("recursive", True),
            pattern=kwargs.get("pattern", "*.txt"),
            cursor=cursor,
        )
        assert result["error"]["code"] == "invalid_request", kwargs

    garbage = service.list_files("workspace", ".", cursor="not-a-cursor")
    assert garbage["error"]["code"] == "invalid_request"


def test_list_files_workspace_read_denial_and_output_listing(tmp_path):
    service, workspace, output = _service(
        tmp_path, workspace={"read": False, "write": False}
    )
    (output / "out.txt").write_text("x", encoding="utf-8")

    denied = service.list_files("workspace", ".")
    assert denied["error"]["code"] == "access_denied"

    allowed = service.list_files("output_dir", ".")
    assert allowed["ok"] is True
    assert [item["path"] for item in allowed["items"]] == ["out.txt"]


def test_list_files_symlink_kind_and_non_traversal(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "b").mkdir()
    (workspace / "b" / "inner.txt").write_text("x", encoding="utf-8")
    (workspace / "link").symlink_to("b", target_is_directory=True)

    flat = service.list_files("workspace", ".")
    assert {item["path"]: item["kind"] for item in flat["items"]} == {
        "b": "directory",
        "link": "symlink",
    }

    recursive = service.list_files("workspace", ".", recursive=True)
    assert [item["path"] for item in recursive["items"]] == [
        "b",
        "b/inner.txt",
        "link",
    ]


def test_list_files_other_entry_kinds(tmp_path):
    service, workspace, _ = _service(tmp_path)
    (workspace / "fifo").mkdir()
    os.mkfifo(workspace / "fifo" / "pipe")

    result = service.list_files("workspace", ".", recursive=True)

    assert result["items"] == [
        {"path": "fifo", "kind": "directory", "size_bytes": None},
        {"path": "fifo/pipe", "kind": "other", "size_bytes": None},
    ]


def test_list_files_respects_configured_max_results_limit(tmp_path):
    service, workspace, _ = _service(tmp_path, max_list_results=5)
    _make_tree(workspace)

    result = service.list_files("workspace", ".", recursive=True, max_results=6)
    assert result["error"]["code"] == "invalid_request"

    ok = service.list_files("workspace", ".", recursive=True, max_results=5)
    assert ok["ok"] is True
    assert ok["count"] == 5
    assert ok["truncated"] is True
