"""Bundles: named sets of configuration overrides.

The important test in this file is not about the two bundles that exist -- it is
:func:`test_every_patch_names_a_declared_path`.  Writing the table, I named
``channels.web.enabled`` and ``orchestration.sub_agent_retries``; *neither exists*.
``channels`` defaults to ``None`` and the retry count is not a declared default at
all, so the lookup returned ``None`` and the mistake was completely invisible.
The check against ``DEFAULT_CONFIG`` is what makes a guessed path fail loudly.
"""

from __future__ import annotations

import pytest

from agent.bundles import (
    BUNDLES,
    DEFAULT_BUNDLE,
    DEFAULT_BUNDLE_NAME,
    Bundle,
    KnownBundleError,
    apply_bundle,
    bundle_names,
    resolve_bundle,
)
from agent.config import DEFAULT_CONFIG
from agent.config_patch import UnknownConfigPath, entry


# ── The table ────────────────────────────────────────────────────────────────


def test_there_is_a_default_bundle() -> None:
    assert DEFAULT_BUNDLE_NAME in BUNDLES
    assert DEFAULT_BUNDLE is BUNDLES[DEFAULT_BUNDLE_NAME]


def test_the_default_bundle_changes_nothing() -> None:
    """A bundle that acted by existing would be a configuration nobody asked for."""
    assert DEFAULT_BUNDLE.patches == ()


def test_every_bundle_is_keyed_by_its_own_name() -> None:
    """A row whose key disagrees with its name is a lookup that cannot be found."""
    for name, bundle in BUNDLES.items():
        assert bundle.key == name


def test_every_bundle_has_a_label_and_a_summary() -> None:
    """They are shown to a user, so an empty one is an unfinished row."""
    for bundle in BUNDLES.values():
        assert bundle.label.strip()
        assert bundle.summary.strip()


def test_bundle_names_are_in_declaration_order() -> None:
    assert bundle_names() == tuple(BUNDLES)


def test_a_bundle_serialises() -> None:
    payload = DEFAULT_BUNDLE.to_payload()
    assert set(payload) == {"key", "label", "summary", "patches"}
    assert isinstance(payload["patches"], list)


def test_a_single_patch_serialises_flat() -> None:
    bundle = Bundle("k", "L", "s", (entry("a.b", 1),))
    assert bundle.to_payload()["patches"] == [{"path": "a.b", "value": 1}]


def test_resolving_a_known_bundle_returns_it() -> None:
    assert resolve_bundle(DEFAULT_BUNDLE_NAME) is DEFAULT_BUNDLE


def test_resolving_an_unknown_bundle_raises() -> None:
    with pytest.raises(KnownBundleError) as caught:
        resolve_bundle("nope")
    assert "nope" in str(caught.value)
    assert DEFAULT_BUNDLE_NAME in str(caught.value), "it names the known ones"


# ── The paths are real ───────────────────────────────────────────────────────


def test_every_patch_names_a_declared_path() -> None:
    """The check that caught two invented paths in the first draft of the table.

    A guessed path is invisible without this: ``dict.get`` returns ``None`` for a
    key that was never there, so a bundle can "override" a setting that does not
    exist and report success.
    """
    for name, bundle in BUNDLES.items():
        try:
            bundle.validate(DEFAULT_CONFIG)
        except UnknownConfigPath as exc:
            pytest.fail(f"bundle {name!r}: {exc}")


def test_applying_every_bundle_succeeds() -> None:
    """Refusing an undeclared path is the mechanism; every row must satisfy it."""
    for name in bundle_names():
        assert apply_bundle(DEFAULT_CONFIG, resolve_bundle(name)) is not None


# ── Applying ─────────────────────────────────────────────────────────────────


def test_applying_no_bundle_returns_an_equivalent_configuration() -> None:
    assert apply_bundle(DEFAULT_CONFIG) == DEFAULT_CONFIG


def test_the_default_bundle_leaves_the_configuration_alone() -> None:
    assert apply_bundle(DEFAULT_CONFIG, DEFAULT_BUNDLE) == DEFAULT_CONFIG


def test_the_input_configuration_is_never_mutated() -> None:
    before = apply_bundle(DEFAULT_CONFIG, DEFAULT_BUNDLE)
    apply_bundle(DEFAULT_CONFIG, resolve_bundle("unattended"))
    assert apply_bundle(DEFAULT_CONFIG, DEFAULT_BUNDLE) == before


def test_a_bundle_override_reaches_the_result() -> None:
    out = apply_bundle(DEFAULT_CONFIG, resolve_bundle("unattended"))
    assert out["scheduler"]["poll_seconds"] == 10


def test_a_bundle_keeps_the_sibling_settings_of_a_section() -> None:
    out = apply_bundle(DEFAULT_CONFIG, resolve_bundle("unattended"))
    assert out["scheduler"]["signal_max_depth"] == DEFAULT_CONFIG["scheduler"][
        "signal_max_depth"
    ], "a shallow patch would have dropped it"


def test_the_poll_interval_stays_below_the_lease() -> None:
    """A lease shorter than the poll would make every run look abandoned.

    Asserted as a property of the *table* rather than of one bundle, because this
    is the kind of relationship a future row can break silently: both numbers are
    individually plausible.
    """
    out = apply_bundle(DEFAULT_CONFIG, resolve_bundle("unattended"))
    assert out["scheduler"]["poll_seconds"] < out["scheduler"]["lease_seconds"]


def test_bundles_compose_by_including_entries() -> None:
    """The documented composition style, exercised so it stays true."""
    fragment = (entry("evolution.enabled", True),)
    composed = Bundle("k", "L", "s", fragment + (entry("memory.tidy_file_threshold", 5),))
    out = apply_bundle(DEFAULT_CONFIG, composed)
    assert out["evolution"]["enabled"] is True
    assert out["memory"]["tidy_file_threshold"] == 5


def test_validate_refuses_a_patch_that_names_nothing_real() -> None:
    """The check a bundle author runs before adding a row to the table."""
    bundle = Bundle("bad", "L", "s", (entry("orchestration.not_a_real_key", 1),))
    with pytest.raises(UnknownConfigPath) as caught:
        bundle.validate(DEFAULT_CONFIG)
    assert "not_a_real_key" in str(caught.value)


def test_validate_accepts_the_bundles_that_exist() -> None:
    for bundle in BUNDLES.values():
        bundle.validate(DEFAULT_CONFIG)  # must not raise


def test_a_bundle_with_no_patches_validates_against_anything() -> None:
    """The default bundle has nothing to check, so it passes even on {}."""
    DEFAULT_BUNDLE.validate({})


def test_apply_is_lenient_where_validate_is_strict() -> None:
    """The two differ on purpose -- see apply_bundle's docstring.

    Application meets partial configs (a run's fallback), so re-checking there
    would drop a requested composition exactly when the file was unreadable.
    """
    bundle = Bundle("partial", "L", "s", (entry("memory.tidy_interval_seconds", 60),))
    assert apply_bundle({"scheduler": {}}, bundle)["memory"][
        "tidy_interval_seconds"
    ] == 60
    with pytest.raises(UnknownConfigPath):
        bundle.validate({"scheduler": {}})


def test_a_bundle_is_frozen() -> None:
    with pytest.raises(Exception):
        DEFAULT_BUNDLE.key = "other"  # type: ignore[misc]


# ── The per-run seam ─────────────────────────────────────────────────────────


def test_a_scheduled_run_can_ask_for_a_bundle() -> None:
    """Bundles are per-run, so the per-run loader is where they belong."""
    from agent.config import load_config_for_run

    out = load_config_for_run(
        {"scheduler": {"poll_seconds": 30, "lease_seconds": 300}},
        bundle=resolve_bundle("unattended"),
    )
    assert out["scheduler"]["poll_seconds"] == 10


def test_a_run_without_a_bundle_is_unchanged() -> None:
    """The default must stay the default, or every existing run changes."""
    from agent.config import load_config_for_run

    fallback = {"scheduler": {"poll_seconds": 30}}
    assert load_config_for_run(fallback) == fallback


def test_a_bundle_applies_to_the_fallback_when_the_file_is_unreadable(
    monkeypatch,
) -> None:
    """Silently running without the requested composition is the failure to avoid."""
    import agent.config as config_module
    from agent.config import load_config_for_run

    monkeypatch.setattr(config_module, "_read_config_file", lambda: None)
    out = load_config_for_run(
        {"scheduler": {"poll_seconds": 30, "lease_seconds": 300}},
        bundle=resolve_bundle("unattended"),
    )
    assert out["scheduler"]["poll_seconds"] == 10, "the bundle still applied"


def test_the_fallback_is_not_mutated_by_a_bundle(monkeypatch) -> None:
    import agent.config as config_module
    from agent.config import load_config_for_run

    monkeypatch.setattr(config_module, "_read_config_file", lambda: None)
    fallback = {"scheduler": {"poll_seconds": 30, "lease_seconds": 300}}
    load_config_for_run(fallback, bundle=resolve_bundle("unattended"))
    assert fallback["scheduler"]["poll_seconds"] == 30


# ── The CLI entry ────────────────────────────────────────────────────────────


def test_a_loader_applies_its_bundle(monkeypatch) -> None:
    """The loader is what the scheduler calls once per run."""
    from agent.bundles import config_loader_for

    loader = config_loader_for(resolve_bundle("unattended"))
    out = loader()
    assert out["scheduler"]["poll_seconds"] == 10


def test_a_loader_with_no_bundle_composes_nothing(monkeypatch) -> None:
    """It still reads the file; it just adds nothing to what it read."""
    import agent.config as config_module
    from agent.bundles import config_loader_for

    monkeypatch.setattr(
        config_module, "_read_config_file", lambda: {"scheduler": {"poll_seconds": 30}}
    )
    out = config_loader_for(None)()
    assert out == {"scheduler": {"poll_seconds": 30}}


def test_an_unnamed_bundle_resolves_to_an_uncomposed_loader() -> None:
    from agent.bundles import loader_for_name

    for blank in (None, "", "   "):
        assert callable(loader_for_name(blank))


def test_a_named_bundle_resolves_to_a_composing_loader() -> None:
    from agent.bundles import loader_for_name

    assert loader_for_name("unattended")()["scheduler"]["poll_seconds"] == 10


def test_an_unknown_name_fails_at_wiring_time() -> None:
    """Not per run: a typo must stop startup, not quietly uncompose every run."""
    from agent.bundles import loader_for_name

    with pytest.raises(KnownBundleError):
        loader_for_name("nonexistent")


def test_the_name_is_trimmed_before_resolving() -> None:
    from agent.bundles import loader_for_name

    assert loader_for_name("  unattended  ")()["scheduler"]["poll_seconds"] == 10


def test_the_gateway_accepts_a_bundle_flag() -> None:
    """The flag exists and is documented, so the mechanism is reachable."""
    from typer.testing import CliRunner

    from agent.cli import app

    result = CliRunner().invoke(app, ["gateway", "--help"])
    assert "--bundle" in result.stdout
    assert "unattended" in result.stdout


def test_the_gateway_passes_the_loader_to_the_scheduler() -> None:
    """Source-level: the flag has to reach the service, not just be parsed.

    A parsed-and-ignored option is worse than no option: the user believes the
    composition is in effect.
    """
    import inspect

    import agent.cli as cli_module

    source = inspect.getsource(cli_module.gateway)
    # The loader now travels through the composition, because the scheduler is mounted as
    # a plugin and a plugin has only a context: the gateway resolves the name, puts the
    # loader in, and the plugin reads it.  Both halves are asserted -- resolving it and
    # handing it over -- because the failure this guard exists for is an option that is
    # parsed and never used, and either half missing produces that.
    assert "loader_for_name(bundle)" in source, (
        "the gateway parses --bundle but never resolves it"
    )
    assert 'components["scheduler_config_loader"] = loader_for_name(bundle)' in source, (
        "the resolved loader must reach the composition the scheduler reads"
    )
