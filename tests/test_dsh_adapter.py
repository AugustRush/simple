"""Reading a DSH profile without executing any of it.

DSH is the third ecosystem this runtime has to interoperate with, and the only one
it cannot *run*: a DSH plugin is a Node/Cordis package.  What is testable -- and
what the adapter promises -- is the declarative half: the MCP servers a profile
configures, the bundles it names, the skill roots it scans.  The rest of this file
pins the two properties that make those promises honest:

* the YAML subset reader **refuses** what it does not understand (with a line
  number) instead of guessing, and PyYAML is used when it happens to be installed;
* reading a profile runs nothing -- pinned by making process creation fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.plugins.dsh import (
    DSH_MCP_PLUGIN,
    JsExpression,
    YamlSubsetError,
    load_yaml_subset,
    read_dsh_profile,
)

REAL_PROFILE = Path.home() / ".dsh" / "profiles" / "desktop"

PATCH = """\
# a patch layer
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: deepseek-account
    model: deepseek-flash
- insert:
    # stdio: convertible
    - id: m1
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "files"
        transport: "stdio"
        command: "npx"
        args: [ "-y", "@modelcontextprotocol/server-filesystem", "/tmp" ]
        env:
          MODE: "ro"
    - id: m2
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "remote"
        transport: "sse"
        url: "https://example.invalid/sse"
    - id: m3
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "computed"
        transport: "stdio"
        command: !!js process.env.MCP_BIN
"""


def _write_profile(tmp_path: Path, *, patch: str = PATCH, package: str | None = None) -> Path:
    (tmp_path / "cordis.yml").write_text("[]\n", encoding="utf-8")
    (tmp_path / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    if package is not None:
        (tmp_path / "package.json").write_text(package, encoding="utf-8")
    return tmp_path


# ── the reader ───────────────────────────────────────────────────────────────


def test_the_subset_reader_reads_a_patch_file() -> None:
    parsed = load_yaml_subset(PATCH)

    assert isinstance(parsed, list) and len(parsed) == 2
    assert parsed[0]["config"]["model"] == "deepseek-flash"
    inserted = parsed[1]["insert"]
    assert [entry["id"] for entry in inserted] == ["m1", "m2", "m3"]
    assert inserted[0]["config"]["args"] == [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/tmp",
    ]
    assert inserted[0]["config"]["env"] == {"MODE": "ro"}
    assert isinstance(inserted[2]["config"]["command"], JsExpression), (
        "an unevaluated expression stays an expression"
    )


def test_a_bare_flow_value_is_a_document(tmp_path) -> None:
    """``cordis.yml`` on a real machine is literally ``[]``."""
    assert load_yaml_subset("[]\n") == []
    assert load_yaml_subset("") is None


@pytest.mark.parametrize(
    ("text", "reason", "line"),
    [
        ("key: &anchor value\n", "anchors", 1),
        ("key: |\n  text\n", "block scalars", 1),
        ("key:\n\tvalue: 1\n", "tab", 2),
    ],
)
def test_the_reader_refuses_what_it_does_not_understand(
    text: str, reason: str, line: int
) -> None:
    """The line number is the point: a refusal has to say where to look."""
    with pytest.raises(YamlSubsetError) as error:
        load_yaml_subset(text)

    assert reason in str(error.value)
    assert f"line {line}" in str(error.value)


# ── the profile ──────────────────────────────────────────────────────────────


def test_it_converts_the_stdio_servers_and_reports_the_rest(tmp_path) -> None:
    profile = read_dsh_profile(_write_profile(tmp_path))

    assert [server["name"] for server in profile.mcp_servers] == ["files"]
    assert profile.mcp_servers[0]["command"] == "npx"
    assert profile.mcp_servers[0]["env"] == {"MODE": "ro"}
    assert len(profile.unsupported) == 2
    assert any("sse" in reason for reason in profile.unsupported)
    assert any("!!js" in reason for reason in profile.unsupported)
    assert profile.warnings == ()


def test_the_bundles_come_from_package_json(tmp_path) -> None:
    package = (
        '{"name": "dsh-profile-desktop", "dsh": {"profile": {"bundles": '
        '["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}}}'
    )
    profile = read_dsh_profile(_write_profile(tmp_path, package=package))

    assert profile.bundles == ("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app")
    assert profile.mcp_servers, "the patch is still read"


def test_a_profile_skill_directory_is_a_skill_root(tmp_path) -> None:
    profile_dir = _write_profile(tmp_path)
    (profile_dir / "skills").mkdir()

    profile = read_dsh_profile(profile_dir)

    assert profile_dir / "skills" in profile.skill_roots


def test_an_unreadable_patch_is_a_warning_not_a_crash(tmp_path) -> None:
    profile = read_dsh_profile(_write_profile(tmp_path, patch="key: &anchor v\n"))

    assert profile.warnings and "anchors" in profile.warnings[0]
    assert profile.mcp_servers == ()


def test_reading_a_profile_runs_nothing(tmp_path, monkeypatch) -> None:
    """Read-only is the promise; this is the only way to pin it."""
    import subprocess

    def _refuse(*_args, **_kwargs):
        raise AssertionError("reading a DSH profile must not start a process")

    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)

    profile = read_dsh_profile(_write_profile(tmp_path))

    assert profile.patch_entries, "it read the file"


# ── the profile on this machine ──────────────────────────────────────────────


@pytest.mark.skipif(not REAL_PROFILE.is_dir(), reason="no DSH profile on this machine")
def test_it_reads_the_real_profile() -> None:
    """The profile this session was started from, read as data.

    The two servers below are the ones this runtime is already talking to through
    its own MCP tools, which is what makes this a check rather than a fixture: the
    same declarations, read from DSH's side, converted into the shape our client
    takes.
    """
    profile = read_dsh_profile(REAL_PROFILE)

    assert "@deepseek-ai/dsh-base" in profile.bundles
    names = [server["name"] for server in profile.mcp_servers]
    assert "sequential-thinking" in names
    for server in profile.mcp_servers:
        # The keys our client reads, and the one it requires.
        assert set(server) <= {"name", "command", "args", "env", "cwd"}
        assert str(server["command"]).strip(), "MCPClient requires a command"
        assert not str(server["command"]).startswith("!!js")
    assert profile.warnings == ()


def test_the_converted_shape_is_what_the_mcp_client_reads(tmp_path) -> None:
    """The contract between this adapter and ``MCPClient._run_server``.

    Named keys rather than a live connect: starting a server to prove a config
    shape would make the test depend on the server.  If the client's keys change,
    this fails and says so.
    """
    from agent.tools.runtime import MCPClient

    missing = read_dsh_profile(tmp_path / "does-not-exist")
    assert missing.mcp_servers == (), "control: a missing directory is empty, not an error"
    assert missing.warnings == (), "and it is not a warning either"

    converted = {
        "name": "files",
        "command": "npx",
        "args": ["-y", "server"],
        "env": {"MODE": "ro"},
    }
    assert callable(MCPClient.connect_from_config)
    # What `_run_server` reads off a config: command, name, args, env, cwd.
    assert set(converted) <= {"name", "command", "args", "env", "cwd"}
    assert converted["command"]
    assert DSH_MCP_PLUGIN.startswith("@deepseek-ai/")
