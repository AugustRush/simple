"""The second producer of a report: another application's profile, adopted with consent.

The provenance design called this "the item that was blocked on consent" and predicted that
after the digest step it would need no new concept.  These tests are that prediction being
checked rather than asserted: the import builds a report, asks through the *same* rule the
installer uses, and writes a plugin the loader already knows how to read -- so the profile's
servers arrive in the composition with a provenance like everything else.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from test_dsh_adapter import PATCH, _write_profile


def _profile(root: pathlib.Path, *, patch: str = PATCH) -> pathlib.Path:
    """A DSH profile directory.  ``_write_profile`` writes into an existing directory."""
    root.mkdir(parents=True, exist_ok=True)
    return _write_profile(root, patch=patch)


class _Scope:
    def __init__(self, session="s-1", channel="cli", user="u-1"):
        self.session_id = session
        self.channel_name = channel
        self.user_id = user


class _Sink:
    """A sink that answers the way a person did, and remembers what it was asked."""

    def __init__(self, answer=True):
        self.answer = answer
        self.asked: list[dict] = []

    async def on_tool_confirmation(self, tool, **kwargs):
        self.asked.append({"tool": tool, **kwargs})
        return self.answer


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A ledger under tmp_path, so no test can write a grant into the real ``~/.agent``."""
    from agent.core import output
    from agent.security import consent, plugin_approval

    monkeypatch.setattr(
        consent, "SHELL", consent.ConsentStore(ledger_root=tmp_path / "trust")
    )
    plugin_approval.plugin_install_clear_all()
    sink = _Sink()
    token = output._active_sink.set(sink)  # type: ignore[attr-defined]
    yield pathlib.Path, sink, tmp_path
    output._active_sink.reset(token)  # type: ignore[attr-defined]
    plugin_approval.plugin_install_clear_all()


# ── the plan: it describes, and it does not adopt ────────────────────────────


def test_the_plan_reports_every_server_it_would_run(tmp_path) -> None:
    """One execution per server with the command line, not one blob for the profile."""
    from agent.plugins.dsh_import import plan_dsh_import

    plan = plan_dsh_import(_profile(tmp_path))

    described = [item.describe() for item in plan.report.would_run]
    assert described == ["mcp-stdio: npx -y @modelcontextprotocol/server-filesystem /tmp"]
    assert plan.report.source.kind == "import"
    assert plan.report.source.source == str(tmp_path)
    assert plan.report.executes is True


def test_the_plan_carries_the_declarations_it_cannot_honour(tmp_path) -> None:
    """An sse transport and an unevaluated expression, each with its reason.

    Dropped silently they would read as "imported everything", which is the failure a
    partial import is most likely to be believed through.
    """
    from agent.plugins.dsh_import import plan_dsh_import

    plan = plan_dsh_import(_profile(tmp_path))

    assert len(plan.report.unsupported) == 2
    assert any("sse" in reason for reason in plan.report.unsupported)
    assert any("!!js" in reason or "unevaluated" in reason for reason in plan.report.unsupported)


def test_the_plan_writes_nothing(tmp_path) -> None:
    """Planning is a read.  The directory it would use does not exist afterwards."""
    from agent.plugins.dsh_import import plan_dsh_import

    destination = tmp_path / "plugins"
    plan = plan_dsh_import(_profile(tmp_path / "profile"), destination=destination)

    assert plan.destination == destination
    assert not destination.exists()


def test_a_profile_with_no_servers_runs_nothing(tmp_path) -> None:
    """The control: nothing to consent to, and no digest pretending otherwise."""
    from agent.plugins.dsh_import import plan_dsh_import

    profile = tmp_path / "plain"
    profile.mkdir()
    (profile / "cordis.yml").write_text("[]\n", encoding="utf-8")

    plan = plan_dsh_import(profile)

    assert plan.report.would_run == ()
    assert plan.report.digest == ""
    assert plan.report.executes is False


def test_editing_a_server_changes_the_digest(tmp_path) -> None:
    """The property every adoption path inherits: an edit is a new question."""
    from agent.plugins.dsh_import import plan_dsh_import

    profile = _profile(tmp_path)
    before = plan_dsh_import(profile).report.digest

    (profile / "cordis.patch.yml").write_text(
        PATCH.replace('command: "npx"', 'command: "bunx"'), encoding="utf-8"
    )

    assert plan_dsh_import(profile).report.digest != before


def test_the_plugin_name_is_safe_and_recognisable(tmp_path) -> None:
    """A profile directory is named for the *application*, so do not import under that name."""
    from agent.plugins.dsh_import import imported_plugin_name

    assert imported_plugin_name("/home/x/.dsh/profiles/desktop") == "dsh-desktop"
    # A name the loader would refuse, made safe rather than passed through.
    assert imported_plugin_name("/home/x/.dsh/profiles/my profile (v2)") == (
        "dsh-my-profile-v2"
    )


# ── the import: consent, then a plugin the loader already understands ────────


def test_nobody_to_ask_means_nothing_is_written(sandbox) -> None:
    """Fail closed.  An import that ran because no dialog was available is the bug."""
    from agent.core import output
    from agent.tools.plugin_tools import import_dsh_profile

    _, _, tmp_path = sandbox
    token = output._active_sink.set(None)  # type: ignore[attr-defined]
    try:
        result = asyncio.run(
            import_dsh_profile(
                _profile(tmp_path / "profile"),
                scope=_Scope(),
                plugins_dir=tmp_path / "plugins",
            )
        )
    finally:
        output._active_sink.reset(token)  # type: ignore[attr-defined]

    assert result["ok"] is False
    assert not (tmp_path / "plugins").exists()
    assert result["would_run"], "and it says what it would have run"


def test_a_refused_import_writes_nothing(sandbox) -> None:
    from agent.tools.plugin_tools import import_dsh_profile

    _, sink, tmp_path = sandbox
    sink.answer = False

    result = asyncio.run(
        import_dsh_profile(
            _profile(tmp_path / "profile"),
            scope=_Scope(),
            plugins_dir=tmp_path / "plugins",
        )
    )

    assert result["ok"] is False
    assert not (tmp_path / "plugins").exists()
    assert sink.asked and sink.asked[0]["tool"] == "import_dsh_profile"
    assert "server-filesystem" in sink.asked[0]["reason"], (
        "the person is asked about the command, not about the profile's path"
    )


def test_an_approved_import_becomes_a_plugin_the_loader_contributes(tmp_path, sandbox) -> None:
    """End to end, and the point of the whole step.

    The imported directory is read by the *real* loader, which contributes the profile's
    MCP server to the composition's registry -- so a profile adopted from another
    application is not a special case anywhere downstream.
    """
    from agent import PluginCatalog
    from agent.tools.mcp_servers import MCPServerRegistry
    from agent.tools.plugin_tools import import_dsh_profile

    _, sink, work = sandbox
    profile = _profile(work / "profile")
    plugins_dir = work / "plugins"
    result = asyncio.run(
        import_dsh_profile(profile, scope=_Scope(), plugins_dir=plugins_dir)
    )

    assert result["ok"] is True, result
    assert sink.asked, "it was approved through a question"
    written = pathlib.Path(result["plugin"])
    assert written.is_dir()
    manifest = json.loads((written / "plugin.json").read_text(encoding="utf-8"))
    assert [server["name"] for server in manifest["mcpServers"]] == ["files"]

    registry = MCPServerRegistry()
    catalog = PluginCatalog(builtin_dir=tmp_path / "builtin", user_dir=plugins_dir)
    catalog._host.services.provide("contributed_mcp_servers", registry)
    try:
        assert catalog.discover_and_load() == [written.name]
        # ``records()`` yields ``(owner, config)`` pairs, not dicts.
        assert [config["name"] for _owner, config in registry.records()] == ["files"]
    finally:
        catalog.close()


def test_importing_twice_does_not_clobber(sandbox) -> None:
    """A second import meets the first, and says so instead of overwriting it."""
    from agent.tools.plugin_tools import import_dsh_profile

    _, _, work = sandbox
    profile = _profile(work / "profile")
    plugins_dir = work / "plugins"
    asyncio.run(import_dsh_profile(profile, scope=_Scope(), plugins_dir=plugins_dir))

    with pytest.raises(FileExistsError):
        asyncio.run(import_dsh_profile(profile, scope=_Scope(), plugins_dir=plugins_dir))


def test_the_second_import_is_not_asked_again_when_it_only_re_offers_the_same_code(
    sandbox,
) -> None:
    """The consent half, observed where it is observable.

    The write refuses a second time (see above), so what this checks is the *question*: the
    same profile, same content, same digest -- answered once, and the gate does not ask
    again.  That is the restart-surviving rule the installer also reads.
    """
    from agent.tools.plugin_tools import confirm_executable_content
    from agent.plugins.dsh_import import plan_dsh_import

    _, sink, work = sandbox
    plan = plan_dsh_import(_profile(work / "profile"))
    source = plan.report.source.source

    async def scenario():
        first = await confirm_executable_content(
            source=source, reason=plan.report.reason(), scope=_Scope(), digest=plan.report.digest
        )
        second = await confirm_executable_content(
            source=source, reason=plan.report.reason(), scope=_Scope(), digest=plan.report.digest
        )
        return first, second

    first, second = asyncio.run(scenario())

    assert first == (True, False)
    assert second == (True, False)
    assert len(sink.asked) == 1, "one question for one body of code"
