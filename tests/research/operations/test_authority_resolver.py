"""ADR-0098 §2 / §4 end to end: ``resolve_degradation_inputs`` over a real v3 dataset (ADR-0077),
a real Lifecycle Registry snapshot and a real ``BarBacktester``; and the CLI authority mode on the
same inputs.

The dataset is a real v3 build of the dataset-test ``World`` (SQLite catalog, real evidence
verifier; ``tests.infrastructure.bars.v3_support``) with twelve one-minute bars of BTC-USDT and
ETH-USDT (22:05 – 22:17 of 2023-11-14, ``first_slice_support``). The source authority must be
exactly the pinned v3 manifest of the pinned dataset (missing or mismatched pins fail closed); the
window must be tiled by its bars as of ``as_of``. The recent metric is compared with a direct call
of the validation function on an independent read of the same bars (no formula re-implemented).

Every verified manifest load re-derives the dataset (the v3 store's rule), so this module keeps
the number of resolutions small; the refusals decided before the source is read are in
``test_authority_refusals.py``. Every Profile number, price, threshold and target is TEST ONLY.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.contracts.strategy import BacktestCostModel, BacktestRequest
from core.contracts.universe import ResearchDatasetEvidenceManifest
from core.contracts.validation_profile import ValidationProfile
from infrastructure.bars.dataset import backtest_bars_from_dataset
from infrastructure.feature.dataset import load_any_manifest
from infrastructure.registry import ProfileFreezeRegistry
from infrastructure.registry.lifecycle import LifecycleRegistry
from plugins.backtest import BarBacktester
from research.loop.dataset_source import DatasetCatalog
from research.operations import authority, degradation_cli
from research.operations.authority import (
    ANCHOR_ABSENT,
    ANCHOR_PRESENT,
    AUTHORITY_FORMAT,
    METRIC_REGISTRY_ID,
    SOURCE_CONFLICT,
    SOURCE_IDENTITY_MISMATCH,
    SOURCE_INCOMPLETE,
    SOURCE_SCOPE_MISMATCH,
    SOURCE_UNAVAILABLE,
    AuthorityResolution,
    universe_symbols,
)
from research.operations.degradation import ObservationWindow, run_degradation_check
from research.validation.returns import from_backtest
from research.validation.robustness import cost_stress_check
from tests.infrastructure.bars import v3_support as v
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e import first_slice_support as fs
from tests.research.operations import authority_fixtures as af
from tests.research.operations.authority_fixtures import (
    AS_OF,
    BAR_COUNT,
    INSTRUMENTS,
    START,
    WINDOW,
    ResolverCase,
    Source,
)
from tests.research.operations.fixtures import read_json


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Source]:
    with ds.sqlite_world(tmp_path_factory.mktemp("authority-world")) as w:
        w.listed()
        fs.ingest_bars_for(w, fs.BTC, tag="btc", base="100")
        fs.ingest_bars_for(w, fs.ETH, tag="eth", base="50")
        v.report(w)
        machinery = v.v3(w)
        built = machinery.build(v.spec_of(w))
        catalog = DatasetCatalog(
            w.h.adapter, w.h.storage, w.builder(), evidence_verifier=machinery.verifier
        )
        manifest = load_any_manifest(w.builder(), built.manifest_hash, machinery.verifier)
        assert isinstance(manifest, ResearchDatasetEvidenceManifest)
        yield Source(
            catalog=catalog,
            dataset_id=manifest.selection_id,
            manifest_hash=built.manifest_hash,
            dataset=manifest.dataset,
        )


@pytest.fixture(scope="module")
def case(source: Source, tmp_path_factory: pytest.TempPathFactory) -> ResolverCase:
    return af.resolver_case(source, tmp_path_factory.mktemp("authority-case"))


@pytest.fixture(scope="module")
def resolution(case: ResolverCase) -> tuple[AuthorityResolution, list[ObservationWindow]]:
    case.calls.clear()
    resolved = case.resolve()
    return resolved, list(case.calls)


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    return root


def _expected_breakeven(source: Source, bound: ValidationProfile) -> Any:
    """The validation function itself on an independent read of the window's bars."""
    prices = backtest_bars_from_dataset(
        source.catalog.adapter,
        source.catalog.storage,
        builder=source.catalog.builder,
        manifest_content_hash=source.manifest_hash,
        symbols=INSTRUMENTS,
        price_cutoff=AS_OF,
        start=WINDOW.start,
        end=WINDOW.end,
        evidence_verifier=source.catalog.evidence_verifier,
    )
    bars = tuple(bar for bar in prices.bars if bar.interval_end <= WINDOW.end)
    request = BacktestRequest(
        cost_model=BacktestCostModel(
            name=af.COST_MODEL.name,
            version=af.COST_MODEL.version,
            fee_rate=af.COST_MODEL.fee_rate_per_side,
            slippage_rate=af.COST_MODEL.slippage_rate_per_side,
        ),
        initial_equity=af.INITIAL_EQUITY,
        bars=bars,
        targets=af.long_targets(af.bars(INSTRUMENTS, START, BAR_COUNT)),
    )
    returns = from_backtest(BarBacktester().run(request))
    [gate] = [g for g in cost_stress_check(bound, returns).gates if g.gate_id == af.GATE_ID]
    return gate.value_exact


# ---- the whole resolution -----------------------------------------------------------------


def test_the_authorities_resolve_the_lifecycle_and_recent_metrics(
    case: ResolverCase, resolution: tuple[AuthorityResolution, list[ObservationWindow]]
) -> None:
    resolved, calls = resolution
    assert resolved.lifecycle.current_state.value == "ACTIVE"
    recent = resolved.recent.manifest
    expected = _expected_breakeven(case.source, case.profile)
    assert expected is not None
    assert dict(recent.metrics) == {af.METRIC: expected}
    assert recent.method_id == METRIC_REGISTRY_ID
    assert recent.as_of == AS_OF and recent.window == WINDOW
    [observed] = recent.sources
    assert observed.source_hash == case.source.manifest_hash
    assert observed.observed_time == WINDOW.end + af.LATENCY
    assert calls == [WINDOW]  # the declared pipeline ran once, on the window

    payload = resolved.provenance.payload()
    assert payload["format"] == AUTHORITY_FORMAT
    assert payload["subject"] == str(af.SUBJECT)
    assert payload["lifecycle"]["anchor"] == ANCHOR_ABSENT
    assert payload["lifecycle"]["history_hash"] == resolved.lifecycle.content_hash()
    assert len(payload["lifecycle"]["record_hashes"]) == len(af.PATH_TO_ACTIVE) - 1
    assert payload["source"]["dataset_id"] == case.source.dataset_id
    assert payload["source"]["manifest_hash"] == case.source.manifest_hash
    assert payload["source"]["data_type"] == "klines_1m"
    assert payload["baseline"]["run_id"] == case.run.run_id
    assert payload["baseline"]["baseline_set_hash"] == case.baseline.content_hash()
    assert payload["baseline"]["validation_report_hash"] == case.report.content_hash()
    [definition] = payload["metrics"]["definitions"]
    assert definition["gate_id"] == af.GATE_ID
    assert definition["definition"] == "hlens.p11.metric.breakeven_cost_multiple@1.0.0"
    execution = payload["execution"]
    assert execution["baseline_run_inputs"] == af.run_inputs().payload()
    assert execution["window_validation"] is None
    assert execution["target_source"]["instruments"] == list(INSTRUMENTS)
    assert execution["backtest_provider"] == BarBacktester().descriptor.plugin_key
    assert payload["as_of"] == "2023-11-15T00:00:00Z"
    assert payload["recent_manifest_hash"] == resolved.recent.manifest_hash
    # the instruments rule is the public one
    assert universe_symbols(case.source.catalog, case.source.manifest_hash) == INSTRUMENTS


def test_the_operation_accepts_the_resolution_as_authority_evidence(
    case: ResolverCase, resolution: tuple[AuthorityResolution, list[ObservationWindow]]
) -> None:
    resolved, _ = resolution
    with ProfileFreezeRegistry(case.freezes, anchor=case.freeze_anchor) as freezes:
        result = run_degradation_check(
            subject=af.SUBJECT,
            lifecycle=resolved.lifecycle,
            profile=case.profile,
            baseline_report=case.report,
            freezes=freezes,
            baseline=case.baseline,
            recent=resolved.recent,
            window=WINDOW,
            authority=resolved.provenance,
        )
    assert result.evidence.as_mapping()["authority"] == resolved.provenance.payload()
    assert result.check.status in ("degraded", "not_degraded")


def test_the_cli_authority_mode_writes_the_same_resolution_with_a_verified_anchor(
    case: ResolverCase,
    resolution: tuple[AuthorityResolution, list[ObservationWindow]],
    reports: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolved, _ = resolution
    with LifecycleRegistry.open_snapshot(case.registry) as snapshot:
        head = snapshot.head.last_record_hash
    args = case.cli_args(
        reports, **{"--authority-head": head, "--authority-anchor": str(case.anchor)}
    )
    code = degradation_cli.main(args, authority_environment=case.environment())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "evidence=authority" in out and f"anchor={ANCHOR_PRESENT}" in out
    [written] = sorted(reports.glob("degradation_check/*.json"))
    evidence = read_json(written)["evidence"]
    assert isinstance(evidence, dict)
    written_authority = evidence["authority"]
    assert written_authority["lifecycle"]["anchor"] == ANCHOR_PRESENT
    # only the anchor state differs from the in-process resolution
    expected = resolved.provenance.payload()
    expected["lifecycle"]["anchor"] = ANCHOR_PRESENT
    assert written_authority == expected


# ---- pinned v3 source: missing / mismatched pins fail closed ------------------------------


def test_a_dataset_without_a_v3_manifest_is_unavailable(case: ResolverCase) -> None:
    dataset_id = case.source.dataset_id
    other = dataset_id[:-1] + ("0" if dataset_id[-1] != "0" else "1")
    assert "no v3 manifest" in case.refused(SOURCE_UNAVAILABLE, dataset_id=other)
    assert case.calls == []


def test_a_manifest_that_is_not_the_datasets_is_an_identity_mismatch(case: ResolverCase) -> None:
    case.refused(SOURCE_IDENTITY_MISMATCH, manifest_hash="f" * 64)
    assert case.calls == []


def test_two_persisted_manifests_of_one_dataset_are_a_conflict_never_a_choice(
    case: ResolverCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v3 store keys one manifest per selection (a rebuild is the same manifest), so two
    content hashes under one ``selection_id`` can only come from a corrupted catalog; the
    catalog read is replaced to present that state to the resolver's rule."""
    real = case.source.manifest_hash
    monkeypatch.setattr(
        authority,
        "evidence_manifest_hashes_for_selection",
        lambda adapter, dataset_id: {real, "e" * 64},
    )
    assert "none is chosen" in case.refused(SOURCE_CONFLICT)


def test_instruments_other_than_the_universe_are_a_scope_mismatch(case: ResolverCase) -> None:
    only_btc = case.execution(instruments=("BTC-USDT",))
    assert "universe members" in case.refused(SOURCE_SCOPE_MISMATCH, execution=only_btc)
    assert case.calls == []  # refused before the pipeline ran


def test_a_window_the_source_does_not_tile_is_incomplete(case: ResolverCase) -> None:
    past_the_last_bar = ObservationWindow(start=START, end=WINDOW.end + af.MINUTE, label="w")
    assert "do not span exactly the window" in case.refused(
        SOURCE_INCOMPLETE, window=past_the_last_bar
    )
    assert case.calls == []
