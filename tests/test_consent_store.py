"""The one consent store: three lifetimes, one record, and what it refuses.

These tests pin the properties the collapse depends on.  Each one is a thing
that used to be three different answers depending on which module was asked:

* ``once`` covers an identical repeat for five minutes and then lapses;
* ``session`` covers every matching request in the scope until the process exits;
* ``always`` covers the rule in that **workspace**, on disk, across restarts;
* the longest lifetime in force is the one reported, because the three nest;
* a question is answerable by number, because "several pending" used to mean
  "go and copy a token";
* an unreadable ledger is **no ledger** -- a corrupt file must not become a
  grant, or the failure mode of the persistence layer is silent permanent trust.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.security import consent
from agent.security.shell import ShellAuthorizationScope


def _scope(session: str = "s-1", channel: str = "cli", user: str = "") -> object:
    return ShellAuthorizationScope(session, channel, user)


class _Clock:
    """A wall clock a test can move."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def store(tmp_path: Path) -> consent.ConsentStore:
    return consent.ConsentStore(ledger_root=tmp_path / "trust", now=_Clock())


# ── the lifetimes ───────────────────────────────────────────────────────────


def test_a_fresh_store_has_answered_nothing(store: consent.ConsentStore) -> None:
    rule = consent.make_rule(consent.PROGRAM, "npm")

    assert store.approved(rule, scope=_scope(), workspace="/w") is False
    assert store.lifetime_of(rule, scope=_scope(), workspace="/w") == ""


def test_once_covers_an_identical_repeat_and_then_lapses(tmp_path: Path) -> None:
    clock = _Clock()
    store = consent.ConsentStore(ledger_root=tmp_path / "trust", now=clock)
    rule = consent.make_rule(consent.PROGRAM, "npm")
    scope = _scope()

    store.remember(rule, consent.ONCE, scope=scope, workspace="/w")
    assert store.lifetime_of(rule, scope=scope, workspace="/w") == consent.ONCE

    clock.advance(consent.PROMPT_TTL_SECONDS - 1)
    assert store.approved(rule, scope=scope, workspace="/w") is True

    clock.advance(2)
    assert store.approved(rule, scope=scope, workspace="/w") is False


def test_once_does_not_cross_a_scope_boundary(store: consent.ConsentStore) -> None:
    """One scope's answer is not another's: session, channel and user all count.

    The three fields are the whole reason a scope exists.  An answer that leaked
    between them would let one conversation's approval run in another's -- and
    a different *user* on the same channel is the case that matters.
    """
    rule = consent.make_rule(consent.COMMAND, "rm -rf /tmp/x")

    store.remember(rule, consent.ONCE, scope=_scope("s-1", "cli", "u-1"))
    assert store.approved(rule, scope=_scope("s-1", "cli", "u-1")) is True

    for other in (
        _scope("s-2", "cli", "u-1"),
        _scope("s-1", "feishu", "u-1"),
        _scope("s-1", "cli", "u-2"),
    ):
        assert store.approved(rule, scope=other) is False, other


def test_session_outlives_once_but_not_the_process(store: consent.ConsentStore) -> None:
    rule = consent.make_rule(consent.PROGRAM, "pytest")
    scope = _scope()

    store.remember(rule, consent.SESSION, scope=scope)
    assert store.lifetime_of(rule, scope=scope) == consent.SESSION

    # Forgetting the expiring answers must not touch a session answer.
    store._forget_expired()
    assert store.approved(rule, scope=scope) is True

    # A new process is a new store: nothing was written down.
    assert consent.ConsentStore(now=store.now).approved(rule, scope=scope) is False


def test_always_is_reported_ahead_of_the_weaker_lifetimes(
    store: consent.ConsentStore,
) -> None:
    """The three nest, so the answer is the longest one in force.

    Reporting ``once`` for a rule that is also ``always`` would describe an
    answer as about to lapse when it is not -- and the caller that decides
    whether to ask reads exactly this.
    """
    rule = consent.make_rule(consent.PROGRAM, "npm")
    scope = _scope()
    store.remember(rule, consent.ONCE, scope=scope, workspace="/w")
    store.remember(rule, consent.ALWAYS, scope=scope, workspace="/w")

    assert store.lifetime_of(rule, scope=scope, workspace="/w") == consent.ALWAYS


# ── persistence ─────────────────────────────────────────────────────────────


def test_always_survives_a_new_store_and_a_new_session(tmp_path: Path) -> None:
    """What "always" is for: tomorrow's conversation is not asked again.

    ``always`` is keyed by workspace rather than by scope precisely here -- a
    new conversation is a new session id, and an answer that only survived its
    own session would be asked again the next day.
    """
    ledger = tmp_path / "trust"
    rule = consent.make_rule(consent.PROGRAM, "npm")
    consent.ConsentStore(ledger_root=ledger).remember(
        rule, consent.ALWAYS, scope=_scope("s-1"), workspace="/w"
    )

    restarted = consent.ConsentStore(ledger_root=ledger)
    assert restarted.approved(rule, scope=_scope("s-1"), workspace="/w") is True
    # A different conversation on the same project: still answered.
    assert restarted.approved(rule, scope=_scope("s-9", "web", "u-2"), workspace="/w") is True
    # A different project: asked.
    assert restarted.approved(rule, scope=_scope("s-9"), workspace="/other") is False


def test_always_is_not_read_when_no_workspace_is_given(
    tmp_path: Path,
) -> None:
    """``workspace=None`` means "this question has no project"; ``""`` means global.

    The distinction has to exist, because an install lands in the agent's own
    plugin directory rather than in a project -- there is no workspace to key it
    by, and answering "always" for it is a fact about the machine.
    """
    ledger = tmp_path / "trust"
    rule = consent.make_rule(consent.PLUGIN, "https://example.invalid/p")
    store = consent.ConsentStore(ledger_root=ledger)
    store.remember(rule, consent.ALWAYS, scope=_scope(), workspace="")

    assert store.approved(rule, scope=_scope(), workspace="") is True
    assert store.approved(rule, scope=_scope()) is False
    assert store.approved(rule, scope=_scope(), workspace="/w") is False


def test_an_unreadable_ledger_is_no_ledger(tmp_path: Path) -> None:
    """A corrupt grant file must fail closed.

    The alternative is the failure mode a security-relevant persistence layer
    cannot have: a file nobody can parse that nonetheless keeps granting.
    """
    ledger = tmp_path / "trust"
    ledger.mkdir(parents=True)
    rule = consent.make_rule(consent.PROGRAM, "npm")
    target = ledger / f"{consent.workspace_key('/w')}.json"
    target.write_text("{not json", encoding="utf-8")

    store = consent.ConsentStore(ledger_root=ledger)
    assert store.approved(rule, scope=_scope(), workspace="/w") is False


def test_a_ledger_that_is_valid_json_but_the_wrong_shape_is_no_ledger(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "trust"
    ledger.mkdir(parents=True)
    (ledger / f"{consent.workspace_key('/w')}.json").write_text(
        json.dumps(["not", "a", "mapping"]), encoding="utf-8"
    )

    store = consent.ConsentStore(ledger_root=ledger)
    assert store.approved(
        consent.make_rule(consent.PROGRAM, "npm"), scope=_scope(), workspace="/w"
    ) is False


def test_a_store_with_no_ledger_writes_nothing(tmp_path: Path) -> None:
    """The default is in memory, so a test cannot grant into a real home."""
    store = consent.ConsentStore()
    rule = consent.make_rule(consent.PROGRAM, "npm")

    store.remember(rule, consent.ALWAYS, scope=_scope(), workspace="/w")

    assert store.approved(rule, scope=_scope(), workspace="/w") is True
    assert list(tmp_path.iterdir()) == []


def test_revoking_the_last_rule_removes_the_file(tmp_path: Path) -> None:
    """An empty grant file is a grant a reader has to interpret; delete it."""
    ledger = tmp_path / "trust"
    store = consent.ConsentStore(ledger_root=ledger)
    rule = consent.make_rule(consent.PROGRAM, "npm")
    store.remember(rule, consent.ALWAYS, scope=_scope(), workspace="/w")
    written = sorted(path.name for path in ledger.iterdir())
    assert written, "always should have written a ledger"

    assert store.revoke(rule, workspace="/w") == 1
    assert store.remembered() == []
    assert sorted(path.name for path in ledger.iterdir()) == []


# ── questions ───────────────────────────────────────────────────────────────


def test_an_identical_question_reuses_its_token(store: consent.ConsentStore) -> None:
    """A retry must be answerable by the answer to the first attempt.

    Minting a fresh token on every retry is why the human's approval could never
    be matched to the request that prompted it.
    """
    scope = _scope()
    rule = consent.make_rule(consent.COMMAND, "pip install markdown")

    first = store.ask(kind="shell", summary="pip install markdown", rule=rule, scope=scope)
    second = store.ask(kind="shell", summary="pip install markdown", rule=rule, scope=scope)

    assert first == second
    assert len(store.pending(scope)) == 1


def test_a_refusal_is_terminal_and_scoped(store: consent.ConsentStore) -> None:
    scope = _scope()
    other = _scope("s-other")
    token = store.ask(
        kind="shell",
        summary="rm -rf /",
        rule=consent.make_rule(consent.COMMAND, "rm -rf /"),
        scope=scope,
    )

    assert store.reject(token, scope=other) is False, "cross-scope reject"
    assert store.reject(token, scope=scope) is True
    assert store.pending(scope) == []
    assert store.approve(token, scope=scope) is None, "a refused token stays refused"
    assert store.reject(token, scope=scope) is False, "second reject is a no-op"


def test_a_reply_cannot_answer_another_scopes_question(
    store: consent.ConsentStore,
) -> None:
    token = store.ask(
        kind="shell",
        summary="rm -rf /tmp/x",
        rule=consent.make_rule(consent.COMMAND, "rm -rf /tmp/x"),
        scope=_scope("s-1", "cli", "u-1"),
    )

    assert store.approve(token, scope=_scope("s-1", "cli", "u-2")) is None
    assert store.approve(token, scope=_scope("s-1", "feishu", "u-1")) is None
    assert store.pending(_scope("s-1", "cli", "u-1")) != [], "still answerable"


def test_a_bare_yes_is_refused_when_it_has_no_single_referent(
    store: consent.ConsentStore,
) -> None:
    """Zero or several pending: an ambiguous reply is not guessed at."""
    scope = _scope()
    assert store.answer_single(scope) is None

    for name in ("a", "b"):
        store.ask(
            kind="shell",
            summary=f"run {name}",
            rule=consent.make_rule(consent.PROGRAM, name),
            scope=scope,
        )
    assert store.answer_single(scope) is None
    assert len(store.pending(scope)) == 2, "nothing was consumed by the refusal"

    store.approve(store.pending(scope)[0].token, scope=scope)
    answered = store.answer_single(scope)
    assert answered is not None and answered.what == "b"


def test_several_questions_are_answerable_by_number(
    store: consent.ConsentStore,
) -> None:
    """What replaces "go and copy a token"."""
    scope = _scope()
    for name in ("a", "b", "c"):
        store.ask(
            kind="shell",
            summary=f"run {name}",
            rule=consent.make_rule(consent.PROGRAM, name),
            scope=scope,
        )

    assert store.summary(scope) == ["1. run a", "2. run b", "3. run c"]
    assert store.answer_at(0, scope) is None, "1-based, so 0 is not a question"
    assert store.answer_at(9, scope) is None

    chosen = store.answer_at(2, scope)
    assert chosen is not None and chosen.what == "b"
    assert store.summary(scope) == ["1. run a", "2. run c"]


def test_the_chosen_lifetime_is_what_a_yes_records(
    store: consent.ConsentStore,
) -> None:
    """The card knows the human's intent; the redemption knows the record."""
    scope = _scope()
    rule = consent.make_rule(consent.PROGRAM, "npm")
    token = store.ask(kind="shell", summary="npm test", rule=rule, scope=scope)

    assert store.note_lifetime(token, consent.SESSION) is True
    approved = store.approve(token, scope=scope)

    assert approved is not None
    assert store.lifetime_of(rule, scope=scope) == consent.SESSION


def test_an_unknown_lifetime_is_not_silently_stronger(
    store: consent.ConsentStore,
) -> None:
    """A typo must not become a persistent grant."""
    scope = _scope()
    rule = consent.make_rule(consent.PROGRAM, "npm")

    store.remember(rule, "forever", scope=scope, workspace="/w")
    assert store.lifetime_of(rule, scope=scope, workspace="/w") == consent.ONCE

    token = store.ask(kind="shell", summary="npm test", rule=rule, scope=scope)
    assert store.note_lifetime(token, "forever") is False
    store.approve(token, scope=scope)
    assert store.lifetime_of(rule, scope=scope, workspace="/w") == consent.ONCE


def test_rules_are_named_not_remembered_as_command_lines() -> None:
    """The point of a rule: it can answer a question the bytes cannot."""
    assert consent.make_rule(consent.PROGRAM, "npm") == "program:npm"
    assert consent.rule_kind("program:npm") == "program"
    assert consent.rule_value("program:npm") == "npm"
    # A colon in the value is the value's business, not the kind's.
    assert consent.rule_value("command:pytest -q 2>&1 | tail -20") == (
        "pytest -q 2>&1 | tail -20"
    )


def test_a_workspace_key_is_stable_and_path_shaped(tmp_path: Path) -> None:
    a = consent.workspace_key(tmp_path)
    b = consent.workspace_key(str(tmp_path))
    assert a == b
    assert a.isalnum() and len(a) == 16
    assert a != consent.workspace_key(tmp_path / "elsewhere")


# ── the install/activation adapter ──────────────────────────────────────────


@pytest.fixture()
def installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> consent.ConsentStore:
    """The process-wide store swapped for an isolated one.

    ``plugin_approval`` reads ``consent.SHELL`` at call time, so replacing the
    module attribute is enough -- and it keeps a test from writing into the
    user's real trust ledger.
    """
    fresh = consent.ConsentStore(ledger_root=tmp_path / "trust")
    monkeypatch.setattr(consent, "SHELL", fresh)
    return fresh


def test_an_install_can_be_answered_always_and_that_persists(
    installs: consent.ConsentStore, tmp_path: Path
) -> None:
    """The question the human could not answer before.

    An approval was five minutes in RAM keyed by source, so "yes, always" was
    not expressible and the same plugin asked again on the next retry after the
    window lapsed.  Now it is a rule in the ledger, and a restarted process
    still knows.
    """
    from agent.security import plugin_approval

    source = "https://example.invalid/plugin.git"
    scope = _scope("s-1")
    assert plugin_approval.plugin_install_was_approved(scope, source) is False

    plugin_approval.plugin_install_mark_approved(
        scope, source, lifetime=consent.ALWAYS
    )
    assert plugin_approval.plugin_install_was_approved(scope, source) is True

    restarted = consent.ConsentStore(ledger_root=tmp_path / "trust")
    monkeypatch_target = consent.SHELL
    assert restarted.approved(
        consent.make_rule(consent.PLUGIN, source), scope=scope, workspace=""
    )
    assert monkeypatch_target is installs


def test_an_install_approval_is_global_rather_than_per_workspace(
    installs: consent.ConsentStore,
) -> None:
    """A plugin lands in the agent's own directory, not in a project."""
    from agent.security import plugin_approval

    source = "https://example.invalid/plugin.git"
    plugin_approval.plugin_install_mark_approved(
        _scope("s-1"), source, lifetime=consent.ALWAYS
    )

    # Any scope, and it was recorded in the global bucket rather than under a
    # workspace the install does not have.
    assert plugin_approval.plugin_install_was_approved(_scope("s-9", "web", "u"), source)
    assert [row.workspace for row in installs.remembered()] == [""]


def test_an_install_record_is_a_question_that_can_be_answered_by_number(
    installs: consent.ConsentStore,
) -> None:
    """Several installs are answerable without copying a token each."""
    from agent.security import plugin_approval

    scope = _scope("s-1")
    first = plugin_approval.plugin_install_record_pending(scope, "source-a")
    second = plugin_approval.plugin_install_record_pending(scope, "source-b")
    assert first != second

    assert plugin_approval.plugin_install_approve_single(scope) is None, "ambiguous"
    assert [record.what for record in plugin_approval.plugin_install_pending_records(scope)] == [
        "source-a",
        "source-b",
    ]

    # By number: the second question, answered without a token.
    records = plugin_approval.plugin_install_pending_records(scope)
    assert plugin_approval.plugin_install_approve_record(
        records[1].token, scope
    ) == "source-b"
    assert plugin_approval.plugin_install_was_approved(scope, "source-b") is True
    assert plugin_approval.plugin_install_was_approved(scope, "source-a") is False


def test_answering_one_kind_does_not_answer_another(
    installs: consent.ConsentStore,
) -> None:
    """An install question and a shell question are both pending; one reply each.

    They share a store now, so the boundary between them has to be the record's
    *kind* rather than which module was called.  Without it, a bare "yes" meant
    for the install could redeem the shell command.
    """
    from agent.security import plugin_approval

    scope = _scope("s-1")
    plugin_approval.plugin_install_record_pending(scope, "source-a")
    shell_rule = consent.make_rule(consent.COMMAND, "rm -rf /tmp/x")
    installs.ask(
        kind="shell", summary="rm -rf /tmp/x", rule=shell_rule, scope=scope
    )

    assert [record.what for record in plugin_approval.plugin_install_pending_records(scope)] == [
        "source-a"
    ]
    assert [record.what for record in installs.pending(scope) if record.kind == "shell"] == [
        "rm -rf /tmp/x"
    ]

    # The ambiguous-reply refusal counts only its own kind.
    assert plugin_approval.plugin_install_approve_single(scope) == "source-a"
    assert [record.kind for record in installs.pending(scope)] == ["shell"]


def test_the_install_adapter_keeps_its_old_public_names(
    installs: consent.ConsentStore,
) -> None:
    """Five test modules and four call sites import these by name."""
    from agent.security import plugin_approval

    for name in (
        "plugin_install_approve_record",
        "plugin_install_approve_single",
        "plugin_install_clear_all",
        "plugin_install_mark_approved",
        "plugin_install_pending_for_scope",
        "plugin_install_pending_records",
        "plugin_install_record_pending",
        "plugin_install_was_approved",
    ):
        assert callable(getattr(plugin_approval, name)), name

    scope = _scope("s-1")
    plugin_approval.plugin_install_record_pending(scope, "source-a")
    assert plugin_approval.plugin_install_pending_for_scope(scope) == ["source-a"]
    plugin_approval.plugin_install_clear_all()
    assert plugin_approval.plugin_install_pending_for_scope(scope) == []


def test_the_composition_root_points_the_store_at_the_home_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger is per agent home, and the home is chosen per build.

    Configured in the composition root rather than at import, because the
    gateway passes its own global home while a named session uses
    ``~/.agent-<name>``: a path frozen at import would write every session's
    trust into whichever home happened to load first.

    The build needs a provider and a database past this point and this test is
    about neither, so a failure *after* the store was pointed somewhere is
    tolerated -- what is asserted is the pointer.  That also makes the test fail
    if ``configure`` ever moves behind something that can fail, which would
    leave the store unconfigured for exactly the runs that need it.
    """
    import asyncio

    from agent.bootstrap import _build_components_async

    monkeypatch.setattr(consent, "SHELL", consent.ConsentStore())
    home = tmp_path / "agent-home"

    try:
        asyncio.run(
            _build_components_async({}, announce=False, resource_home=home)
        )
    except Exception:
        pass

    assert consent.SHELL.ledger_root is not None
    assert consent.SHELL.ledger_root.resolve() == (home / "trust").resolve()


