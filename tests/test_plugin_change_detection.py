"""Code that appears in the user's plugin directory is noticed before it runs.

What this is, and what it is not
--------------------------------
Not a security boundary: the trust ledger is a plain 0644 JSON file in the same home as the
plugin directory, so anything that can write code into one can write a record into the
other.  What it is: **change detection**, which raises the cost of the generic vector -- a
sync tool, a package install, a clone, another application's profile copying files into a
directory this runtime scans -- from "write a file in a conventional data directory" to
"know this application's ledger path, format and workspace key".

The first look at a machine is a *blessing of what is already there*, printed and recorded
rather than silent, because the alternative is a cliff: everything inert until re-approved.
After that, only a change is loud.

**The friction this creates, stated rather than discovered later:** a plugin *author* who
edits their own plugin under ``~/.agent/plugins/`` is, to this mechanism, indistinguishable
from a sync tool updating files behind them -- the content changed, so it does not load until
it is approved or observed again.  Two tests in ``tests/test_functional_turns.py`` have to
record the content they write for exactly this reason, and neither of them is about consent.
The escape hatch for that workflow (a directory the user names as their own, which is
*pathed* rather than *content-pinned*) is not built; it is recorded here and in the delivery
document's backlog, because shipping the strict default first is the safe order and the hatch
is a decision about what "mine" means, not an implementation detail.
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture()
def ledger(monkeypatch):
    """An empty in-memory store, so no test touches the real ``~/.agent/trust``."""
    from agent.security import consent

    monkeypatch.setattr(consent, "SHELL", consent.ConsentStore(ledger_root=None))
    return consent.SHELL


def _code_plugin(root: pathlib.Path, name: str, *, body: str = "") -> pathlib.Path:
    """A user plugin whose module writes *body* when it is imported.

    A top-level side effect on purpose: it is the only way to assert that the code did **not**
    run, rather than that the plugin is merely absent from a list.
    """
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        f'{{"name": "{name}"}}', encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(
        "import pathlib\n"
        f"pathlib.Path(__file__).with_name('ran.txt').write_text({body!r})\n"
        "\n"
        "class P:\n"
        f"    name = {name!r}\n"
        "    inject = ()\n"
        "\n"
        "    def setup(self, ctx):\n"
        "        pass\n"
        "\n"
        "\n"
        "def register():\n"
        "    return P()\n",
        encoding="utf-8",
    )
    return plugin_dir


def _declarative_plugin(root: pathlib.Path, name: str) -> pathlib.Path:
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        f'{{"name": "{name}"}}', encoding="utf-8"
    )
    commands = plugin_dir / "commands"
    commands.mkdir()
    (commands / "go.md").write_text("---\ndescription: d\n---\nbody", encoding="utf-8")
    return plugin_dir


def _catalog(tmp_path, *, user_dir, detect=True):
    from agent import PluginCatalog

    return PluginCatalog(
        builtin_dir=tmp_path / "builtin",
        user_dir=user_dir,
        detect_plugin_changes=detect,
    )


def _ran(plugin_dir: pathlib.Path) -> bool:
    return (plugin_dir / "ran.txt").exists()


# ── the first look ───────────────────────────────────────────────────────────


def test_the_first_look_records_what_is_already_there(tmp_path, ledger, capsys) -> None:
    """An existing tree is not a cliff: it is looked at, printed, and recorded once."""
    user = tmp_path / "user"
    plugin = _code_plugin(user, "alpha", body="v1")

    catalog = _catalog(tmp_path, user_dir=user)
    try:
        assert catalog.discover_and_load() == ["alpha"]
        assert _ran(plugin), "the first look blesses what is already here"
        assert catalog.unapproved_report() == {}

        printed = capsys.readouterr().out
        assert "first look" in printed
        assert "alpha" in printed and "python-entry" in printed, (
            "the notice names the plugin and what it runs"
        )
    finally:
        catalog.close()


def test_the_second_run_is_quiet_and_still_loads_it(tmp_path, ledger, capsys) -> None:
    """Recorded once means not reported again: continuity, not nagging."""
    user = tmp_path / "user"
    plugin = _code_plugin(user, "alpha", body="v1")
    first = _catalog(tmp_path, user_dir=user)
    try:
        first.discover_and_load()
    finally:
        first.close()
    capsys.readouterr()

    second = _catalog(tmp_path, user_dir=user)
    try:
        assert second.discover_and_load() == ["alpha"]
        assert _ran(plugin)
        assert "first look" not in capsys.readouterr().out
    finally:
        second.close()


# ── a change ─────────────────────────────────────────────────────────────────


def test_code_that_appears_later_does_not_run(tmp_path, ledger, capsys) -> None:
    """The case this exists for: after the first look, new code is not executed.

    And the assertion is the side effect, not a list membership: the plugin must not be in
    ``_plugins`` *and* its module must never have been imported.
    """
    user = tmp_path / "user"
    first = _catalog(tmp_path, user_dir=user)
    try:
        first.discover_and_load()  # takes the baseline over an empty tree
    finally:
        first.close()
    capsys.readouterr()

    plugin = _code_plugin(user, "planted", body="v1")
    catalog = _catalog(tmp_path, user_dir=user)
    try:
        assert catalog.discover_and_load() == []
        assert not _ran(plugin), "the module was imported despite being unapproved"

        report = catalog.unapproved_report()
        assert list(report) == ["planted"]
        assert report["planted"]["would_run"] == ["python-entry: __init__.py"]
        assert "NOT loaded" in capsys.readouterr().out
    finally:
        catalog.close()


def test_editing_a_plugin_that_was_already_seen_blocks_it_again(tmp_path, ledger, capsys) -> None:
    """An edit is not the content that ran.  Same rule as the install path, same reason."""
    user = tmp_path / "user"
    plugin = _code_plugin(user, "alpha", body="v1")
    first = _catalog(tmp_path, user_dir=user)
    try:
        first.discover_and_load()
        assert _ran(plugin)
    finally:
        first.close()
    (plugin / "ran.txt").unlink()
    capsys.readouterr()

    # A change that keeps the marker names identical -- the whole reason the digest is over
    # content and not over the file list.
    (plugin / "__init__.py").write_text(
        (plugin / "__init__.py").read_text(encoding="utf-8").replace("v1", "v2"),
        encoding="utf-8",
    )

    catalog = _catalog(tmp_path, user_dir=user)
    try:
        assert catalog.discover_and_load() == []
        assert not _ran(plugin)
        assert list(catalog.unapproved_report()) == ["alpha"]
    finally:
        catalog.close()


def test_the_interactive_fallback_gates_the_user_s_plugin_directory(
    tmp_path, ledger, monkeypatch, capsys
) -> None:
    """The one catalog the CLI builds itself must ask what the composition asks.

    ``_interactive_loop_body`` builds its own catalog when the composition registered none,
    and it hands that catalog the **user's** plugin directory.  Built without change
    detection it loads whatever is there, which is the gate's whole subject.
    """
    from agent import cli

    user = tmp_path / "user"
    plugin = _code_plugin(user, "alpha", body="v1")
    first = _catalog(tmp_path, user_dir=user)
    try:
        first.discover_and_load()
        assert _ran(plugin)
    finally:
        first.close()
    (plugin / "ran.txt").unlink()
    capsys.readouterr()
    (plugin / "__init__.py").write_text(
        (plugin / "__init__.py").read_text(encoding="utf-8").replace("v1", "v2"),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli.shared, "USER_PLUGINS_DIR", user)
    monkeypatch.setattr(cli.shared, "PLUGINS_DIR", tmp_path / "builtin")
    catalog = cli._fallback_plugin_catalog({}, {})
    try:
        assert not _ran(plugin), "the fallback ran a plugin the gate would have refused"
    finally:
        catalog.close()


# ── what is deliberately out of scope ───────────────────────────────────────


def test_a_declarative_plugin_is_never_gated(tmp_path, ledger) -> None:
    """Nothing runs, so there is nothing to notice.  Most plugins are this."""
    user = tmp_path / "user"
    _declarative_plugin(user, "docs")
    catalog = _catalog(tmp_path, user_dir=user)
    try:
        catalog.discover_and_load()  # baseline
    finally:
        catalog.close()

    _declarative_plugin(user, "docs-two")
    catalog = _catalog(tmp_path, user_dir=user)
    try:
        assert catalog.discover_and_load() == ["docs", "docs-two"]
        assert catalog.unapproved_report() == {}
    finally:
        catalog.close()


def test_the_runtime_s_own_plugins_are_not_gated(tmp_path, ledger) -> None:
    """The distribution's plugins are the release's business, not a change to notice.

    Gating them would mean the agent blocking its own upgrade -- and re-approving `evolution`
    on every release.
    """
    builtin = tmp_path / "builtin"
    _code_plugin(builtin, "shipped")
    catalog = _catalog(tmp_path, user_dir=tmp_path / "user")
    try:
        catalog.discover_and_load()  # baseline over nothing
    finally:
        catalog.close()

    # Same plugin, now with different code: still a builtin.
    _code_plugin(builtin, "shipped", body="v2")
    catalog = _catalog(tmp_path, user_dir=tmp_path / "user")
    try:
        assert catalog.discover_and_load() == ["shipped"]
    finally:
        catalog.close()


def test_a_catalog_that_did_not_ask_for_this_loads_what_it_is_given(tmp_path, ledger) -> None:
    """Off by default: an embedder or a test loading a directory is not a threat model."""
    user = tmp_path / "user"
    plugin = _code_plugin(user, "alpha", body="v1")
    catalog = _catalog(tmp_path, user_dir=user, detect=False)
    try:
        assert catalog.discover_and_load() == ["alpha"]
        assert _ran(plugin)
        assert catalog.unapproved_report() == {}
    finally:
        catalog.close()


# ── the other door: a human said yes ─────────────────────────────────────────


def test_an_approval_is_enough_without_ever_being_seen(tmp_path, ledger) -> None:
    """Approval is recorded content-addressed, so the load path can honour it.

    This is what keeps the *supported* update path working: a plugin installed or imported
    through a path that asked a human must load, on a machine whose baseline was taken long
    ago, without a second question.
    """
    from agent.plugins.formats import executions
    from agent.runtime.provenance import digest_of
    from agent.security import plugin_approval

    user = tmp_path / "user"
    plugin = _code_plugin(user, "installed", body="v1")
    plugin_approval.record_seen_baseline()  # a machine that has already had its first look
    digest = digest_of(executions(plugin))

    blocked = _catalog(tmp_path, user_dir=user)
    try:
        assert blocked.discover_and_load() == []
    finally:
        blocked.close()
    assert not _ran(plugin)

    plugin_approval.plugin_install_mark_approved(
        None, str(plugin), lifetime="always", digest=digest
    )

    approved = _catalog(tmp_path, user_dir=user)
    try:
        assert approved.discover_and_load() == ["installed"]
        assert _ran(plugin)
        assert approved.unapproved_report() == {}
    finally:
        approved.close()


def test_the_two_rule_kinds_do_not_impersonate_each_other(tmp_path, ledger) -> None:
    """"Who approved this" has to stay answerable.

    A single rule kind would make a machine's habit indistinguishable from a person's
    decision, which is the one thing a consent record must not do.
    """
    from agent.security import plugin_approval

    assert plugin_approval.plugin_content_was_seen("abc") is False
    plugin_approval.record_plugin_content_seen("abc")

    assert plugin_approval.plugin_content_was_seen("abc") is True
    assert plugin_approval.plugin_install_was_approved(None, "/some/plugin", "abc") is False
    # And the marker is its own thing, not a side effect of seeing a plugin.
    assert plugin_approval.seen_baseline_taken() is False
    plugin_approval.record_seen_baseline()
    assert plugin_approval.seen_baseline_taken() is True


# ── the author's door ────────────────────────────────────────────────────────


def test_an_author_s_directory_is_trusted_by_path_not_by_content(tmp_path, ledger) -> None:
    """The escape hatch for the workflow the strict default interrupts.

    A local directory the user *names* is a different claim from a URL: "the code in here is
    mine, and I will read it" rather than "always for whatever is at this address" -- which
    is the hole the old approval key had, and the reason this is configuration rather than a
    consent record.  Under it, an author's edit loads without being re-approved; outside it,
    nothing changes.
    """
    from agent import PluginCatalog

    work = tmp_path / "work"
    plugin = _code_plugin(work, "mine", body="v1")
    trusted = PluginCatalog(
        builtin_dir=tmp_path / "builtin",
        user_dir=work,
        plugin_config={"trusted_paths": [str(work)]},
        detect_plugin_changes=True,
    )
    try:
        assert trusted.discover_and_load() == ["mine"]
        assert _ran(plugin)
    finally:
        trusted.close()

    # The author edits their own plugin.  No re-approval, and no change notice.
    (plugin / "ran.txt").unlink()
    (plugin / "__init__.py").write_text(
        (plugin / "__init__.py").read_text(encoding="utf-8").replace("v1", "v2"),
        encoding="utf-8",
    )
    again = PluginCatalog(
        builtin_dir=tmp_path / "builtin",
        user_dir=work,
        plugin_config={"trusted_paths": [str(work)]},
        detect_plugin_changes=True,
    )
    try:
        assert again.discover_and_load() == ["mine"], (
            "a trusted path is trusted for its content too"
        )
        assert _ran(plugin)
        assert again.unapproved_report() == {}
    finally:
        again.close()


def test_a_trusted_path_does_not_leak_to_its_neighbours(tmp_path, ledger) -> None:
    """Trusting one directory must not trust the tree it happens to sit in."""
    from agent import PluginCatalog

    mine = _code_plugin(tmp_path / "work", "mine", body="v1")
    neighbour = _code_plugin(tmp_path / "elsewhere", "theirs", body="v1")

    from agent.security import plugin_approval

    plugin_approval.record_seen_baseline()
    catalog = PluginCatalog(
        builtin_dir=tmp_path / "builtin",
        user_dir=tmp_path / "work",
        plugin_config={"trusted_paths": [str(mine.parent)]},
        detect_plugin_changes=True,
    )
    try:
        assert catalog.discover_and_load() == ["mine"]
        assert catalog._is_trusted_path(neighbour.parent) is False
    finally:
        catalog.close()


def test_a_string_trusted_path_is_accepted_like_a_list(tmp_path, ledger) -> None:
    """One directory is a common thing to write, and config is read by people."""
    from agent import PluginCatalog

    work = tmp_path / "work"
    plugin = _code_plugin(work, "mine", body="v1")
    catalog = PluginCatalog(
        builtin_dir=tmp_path / "builtin",
        user_dir=work,
        plugin_config={"trusted_paths": str(work)},
        detect_plugin_changes=True,
    )
    try:
        assert catalog.discover_and_load() == ["mine"]
        assert _ran(plugin)
    finally:
        catalog.close()
