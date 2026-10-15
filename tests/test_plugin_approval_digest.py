"""An approval is about the code that was shown, not about the address it came from.

The design put the problem exactly (``2026-10-12-capability-provenance-and-adapter-
translators-design.md``): approval was "keyed by the *source string* a human was shown",
and therefore "(d) attached to nothing that ends up running".  These tests hold the fix in
place: the durable answer is keyed by a digest of what would run, while the *question* is
still shown under the source a person recognises.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent.runtime.provenance import Execution, digest_of


class _Scope:
    def __init__(self, session="s-1", channel="cli", user="u-1"):
        self.session_id = session
        self.channel_name = channel
        self.user_id = user


def _executions(**payload):
    return (
        Execution(kind=payload.pop("kind", "hook-command"), payload=payload),
    )


# ── the digest itself ───────────────────────────────────────────────────────


def test_the_digest_is_stable_and_order_independent() -> None:
    """Two runs over the same plugin have to agree, or every start asks again."""
    first = Execution(kind="python-entry", payload={"marker": "__init__.py", "digest": "aa"})
    second = Execution(kind="mcp-stdio", payload={"marker": ".mcp.json", "digest": "bb"})

    assert digest_of([first, second]) == digest_of([first, second])
    assert digest_of([first, second]) == digest_of([second, first]), (
        "the order the formats happen to be declared in is not part of what runs"
    )


def test_the_digest_ignores_where_the_plugin_lives() -> None:
    """Moving a directory does not change what it runs.

    A digest that changed on a move would re-ask a question already answered -- which is
    how a person learns to approve without reading.
    """
    here = Execution(
        kind="hook-command",
        payload={"marker": "hooks/hooks.json", "digest": "cc", "path": "/a/plugin"},
    )
    there = Execution(
        kind="hook-command",
        payload={"marker": "hooks/hooks.json", "digest": "cc", "path": "/b/elsewhere"},
    )

    assert digest_of([here]) == digest_of([there])


def test_the_digest_changes_when_the_content_does() -> None:
    """The property the whole step exists for: an edit is not the same approval."""
    before = _executions(marker="hooks/hooks.json", digest="cc")
    after = _executions(marker="hooks/hooks.json", digest="dd")

    assert digest_of(before) != digest_of(after)


def test_a_plugin_that_runs_nothing_has_no_digest() -> None:
    """``""`` because there is no question behind the number."""
    assert digest_of([]) == ""


def test_editing_a_python_entry_point_changes_the_digest(tmp_path) -> None:
    """The hole a name-only digest would leave, closed at the level that matters.

    A digest over marker *names* says an edited ``__init__.py`` is the plugin that was
    approved -- and for arbitrary code that is the entire risk.
    """
    from agent.plugins.formats import executions

    root = tmp_path / "widget"
    root.mkdir()
    (root / "__init__.py").write_text("print('one')\n", encoding="utf-8")
    before = digest_of(executions(root))

    (root / "__init__.py").write_text("print('two')\n", encoding="utf-8")

    assert digest_of(executions(root)) != before


def test_editing_an_mcp_command_changes_the_digest(tmp_path) -> None:
    """Same, for the declaration shape -- the command is what runs."""
    from agent.plugins.formats import executions

    root = tmp_path / "widget"
    root.mkdir()
    manifest = root / ".mcp.json"
    manifest.write_text(
        json.dumps({"mcpServers": {"a": {"command": "npx", "args": ["-y", "one"]}}}),
        encoding="utf-8",
    )
    before = digest_of(executions(root))

    manifest.write_text(
        json.dumps({"mcpServers": {"a": {"command": "npx", "args": ["-y", "two"]}}}),
        encoding="utf-8",
    )

    assert digest_of(executions(root)) != before, (
        "a server pointed somewhere else is not the server that was approved"
    )


def test_a_script_added_to_a_hook_directory_changes_the_digest(tmp_path) -> None:
    """The directory case: the content of every file, not the twenty names shown."""
    from agent.plugins.formats import executions

    root = tmp_path / "widget"
    hooks = root / ".claude-plugin" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "check.sh").write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    before = digest_of(executions(root))

    (hooks / "extra.sh").write_text("#!/bin/sh\nrm -rf /\n", encoding="utf-8")

    assert digest_of(executions(root)) != before


# ── the key: shown by source, remembered by content ──────────────────────────


@pytest.fixture()
def approval(tmp_path, monkeypatch):
    """The real adapter, pointed at a ledger in *tmp_path* rather than ``~/.agent``."""
    from agent.security import consent, plugin_approval

    store = consent.ConsentStore(ledger_root=tmp_path / "trust")
    monkeypatch.setattr(consent, "SHELL", store)
    plugin_approval.plugin_install_clear_all()
    return plugin_approval, consent, tmp_path / "trust"


def test_a_durable_approval_is_bound_to_the_digest(approval) -> None:
    """The claim, in three assertions."""
    plugin_approval, consent, _ = approval
    scope = _Scope()

    plugin_approval.plugin_install_mark_approved(
        scope, "https://github.com/acme/widget", lifetime=consent.ALWAYS, digest="aa"
    )

    assert plugin_approval.plugin_install_was_approved(scope, "https://github.com/acme/widget", "aa")
    assert not plugin_approval.plugin_install_was_approved(
        scope, "https://github.com/acme/widget", "bb"
    ), "the same URL serving different code is a different question"


def test_a_durable_grant_written_before_digests_does_not_stand_in(approval) -> None:
    """A pre-digest ``always`` is not consent to whatever that URL serves now.

    It is not *invalidated* -- the record still says what it said, and the plain key is
    still read for short lifetimes.  It simply does not answer a question about content it
    never saw.
    """
    plugin_approval, consent, _ = approval
    scope = _Scope()

    plugin_approval.plugin_install_mark_approved(
        scope, "https://github.com/acme/widget", lifetime=consent.ALWAYS
    )

    assert not plugin_approval.plugin_install_was_approved(
        scope, "https://github.com/acme/widget", "aa"
    )


def test_a_single_answer_is_read_at_the_key_the_question_was_shown_under(approval) -> None:
    """A pending record answered later must still work.

    The question is shown under the source (so the chat card does not print a hash), and a
    ``once`` answer is recorded there.  It cannot outlive the session, so reading it for a
    digest-qualified ask is not a hole -- and without it, every redeemed question would
    come straight back.
    """
    plugin_approval, consent, _ = approval
    scope = _Scope()

    plugin_approval.plugin_install_mark_approved(
        scope, "https://github.com/acme/widget", lifetime=consent.ONCE
    )

    assert plugin_approval.plugin_install_was_approved(
        scope, "https://github.com/acme/widget", "aa"
    )


def test_the_pending_card_still_shows_the_source_not_a_hash(approval) -> None:
    """``Pending.what`` is the rule's value, so the digest must not live in ``rule``."""
    plugin_approval, _, _ = approval
    scope = _Scope()

    plugin_approval.plugin_install_record_pending(
        scope, "https://github.com/acme/widget", reason="ships a hook", digest="deadbeef"
    )

    assert plugin_approval.plugin_install_pending_for_scope(scope) == [
        "https://github.com/acme/widget"
    ]


def test_a_durable_approval_survives_a_restart(approval) -> None:
    """Written to the ledger, read by a *new* store -- the "restart" the design asks for.

    The store is the one place an answer is remembered, so this step inherits durability
    rather than building a second ledger beside it.
    """
    plugin_approval, consent, ledger = approval
    scope = _Scope()
    plugin_approval.plugin_install_mark_approved(
        scope, "https://github.com/acme/widget", lifetime=consent.ALWAYS, digest="aa"
    )

    # A fresh process: new store object over the same directory, nothing in memory.
    restarted = consent.ConsentStore(ledger_root=ledger)
    assert restarted.approved(
        consent.make_rule(consent.PLUGIN, "https://github.com/acme/widget@aa"),
        scope=scope,
        workspace="",
    ), "the durable grant has to be in the ledger, not in this process"


def test_the_question_is_only_asked_when_the_content_is_new(approval) -> None:
    """End to end through the installer's own helper, with and without an answer."""
    import asyncio

    from agent.tools.plugin_tools import PluginTools

    plugin_approval, consent, _ = approval
    scope = _Scope()
    source = "https://github.com/acme/widget"

    asked: list[str] = []

    class _Sink:
        async def on_tool_confirmation(self, *args, **kwargs):
            asked.append(str(kwargs.get("command", "")))
            return True

    from agent.core import output

    # ``confirm_plugin_install`` reads no instance state: it consults the sink and the
    # ledger, which is exactly the surface under test here.
    tool = object.__new__(PluginTools)

    async def scenario():
        token = output._active_sink.set(_Sink())  # type: ignore[attr-defined]
        try:
            first = await tool.confirm_plugin_install(
                source=source, reason="runs ./check.sh", scope=scope, digest="aa"
            )
            second = await tool.confirm_plugin_install(
                source=source, reason="runs ./check.sh", scope=scope, digest="aa"
            )
            third = await tool.confirm_plugin_install(
                source=source, reason="runs ./elsewhere.sh", scope=scope, digest="bb"
            )
            return first, second, third
        finally:
            output._active_sink.reset(token)  # type: ignore[attr-defined]

    first, second, third = asyncio.run(scenario())

    assert first == (True, False), "asked, and answered yes"
    assert second == (True, False), "the same content: not asked again"
    assert third == (True, False), "different content: asked again"
    assert len(asked) == 2, f"one question per distinct content, got {len(asked)}"
