"""Real-data capability smoke of the continuous research loop over verified PIT Research Datasets.

ADR-0049 implementation note (dataset-backed loop, 2026-09-26). ``research.loop.dataset_compose``
composes the Phase 11 loop with ``DatasetIngestStage`` as its round data source: every round reads
the feature / price ``ManifestPair`` (and optionally the sealed window's manifest) its
``DatasetRound`` declares, each loaded through the ``DatasetBuilder``'s verifying ``ManifestStore``.
The goal is to prove that the loop's pipes carry real-format data with every binding intact —
**never a market conclusion**: Profile numbers are not frozen, nothing here is validated, and a
verdict is only checked to be one of the defined verdicts.

Data. There is no real market data offline (D-NET is pending), so — as in
``test_research_pipeline_real_data.py`` — the fixture is Binance-format 1m klines (the 12-slot
archive / REST element) of BTCUSDT and ETHUSDT taken through the real ingestion path (archive
store, REST collector over the mock venue, REST store, Canonical normalizer, D-33 reconciliation,
quality reports, exchangeInfo listings, F3 ``DatasetBuilder``) on the dedicated PostgreSQL **test**
catalog with a ``tmp_path`` warehouse. The klines span two UTC days, so the TEST ONLY Profile's
sealed OOS window (from midnight) holds data: 2023-11-14 18:00 – 24:00 (research window) and
2023-11-15 00:00 – 01:00 (sealed window), 420 bars per symbol; every table stays far below 20k rows.

Rounds (cadence 4 h; each round's cutoff is its scheduled ``as_of``):

- round 0, cutoff 2023-11-14 21:00 — pair: interval ``[2023-11-14, 21:00)`` + point view at 21:00;
- round 1, cutoff 2023-11-15 01:00 — pair: interval ``[2023-11-14, 01:00)`` + point view at 01:00,
  plus the sealed window's point manifest (view 01:00, window 00:00 – 01:00): withheld — declared
  only, never loaded or read (ADR-0049 implementation note, review fixes 4).

Checked: both rounds complete with a hash-chained audit; every validation report binds its data
(``G0.manifest_binding`` PASS); no bar, observation, signal or decision is after its round's
cutoff and no sealed-window bar enters research; the withheld sealed manifest is recorded as a
declaration and never requested, loaded or read as bars (every manifest request, verified load and
dataset bar read of the builder is recorded, with and without the cache); a rerun in a fresh
process (the persisted manifests read by their declared hashes, durable state reopened between
the rounds) gives identical record hashes; nothing reaches PAPER / ACTIVE; a round whose declared
view is after its cutoff is refused at ingest.

!!! TEST ONLY !!!  ``LOOP_REAL_DATA_TEST_ONLY_PROFILE``, ``LOOP_REAL_DATA_TEST_ONLY_PARAMS``, the
budget, cost units, decision grid and state-model parameters hold arbitrary, uncalibrated numbers
chosen only to let every stage run on a few hours of minutes. They are not a proposal, not a
calibration result and must never be used for research.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from apps.worker import AUTOMATABLE_TARGETS, LoopBudget, RoundStatus, StageStatus
from apps.worker.loop import FORBIDDEN_TARGETS, LoopAuditLog, LoopRecord
from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import ResearchDatasetManifest
from core.contracts.validation_profile import (
    BenchmarkParams,
    CostStressParams,
    DataSplitParams,
    ParameterStabilityParams,
    SampleSizeParams,
    SignificanceParams,
    WalkForwardParams,
)
from core.domain.base import Kind, Ref
from core.domain.research import EvidenceLevel, KnowledgeItem, Verdict
from core.domain.selection import ProfileSelection, ProfileSelectionKey
from core.domain.specs import OutcomeSpec
from infrastructure.bars import VerifiedManifestCache
from infrastructure.dataset.builder import DatasetBuilder, DatasetBuilt
from infrastructure.event_bus import InMemoryEventBus
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from plugins.backtest import BarBacktester
from plugins.features import BarLogReturnProvider
from plugins.outcomes import ForwardReturnOutcome
from plugins.states import TrendRangeProvider
from research.loop import (
    DatasetCatalog,
    DatasetLoopConfig,
    DatasetRound,
    DurableLoop,
    LoopStateInconsistent,
    LoopWiring,
    OosUnsealBudget,
    ResearchMemory,
    build_dataset_loop,
    check_round_bus,
    dataset_source,
    open_dataset_loop,
)
from research.loop.durable import AUDIT_FILE
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.validation import RobustnessParams
from research.validation.sealed_oos import UnsealingLedger
from tests import factories
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e.first_slice_support import BTC, DAY_END, DAY_START, ETH
from tests.infrastructure.e2e.first_slice_support import ingest_bars_for as ingest_klines
from tests.infrastructure.parser.rest_support import kline_item
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss

pytestmark = pytest.mark.postgres

MINUTE: Final = timedelta(minutes=1)
HOUR: Final = timedelta(hours=1)
DAY1: Final = ss.DAY  # 2023-11-14
DAY2: Final = DAY1 + timedelta(days=1)
FIRST_BAR: Final = datetime(2023, 11, 14, 18, tzinfo=UTC)
DAY1_BARS, DAY2_BARS = 360, 60  # 18:00 - 24:00, then 00:00 - 01:00 of the next day
BOUNDARY: Final = DAY_END  # the TEST ONLY Profile's sealed OOS boundary (2023-11-15 00:00)
CUTOFFS: Final = (datetime(2023, 11, 14, 21, tzinfo=UTC), datetime(2023, 11, 15, 1, tzinfo=UTC))
SYMBOL: Final = "BTC-USDT"  # Canonical
FAMILY: Final = "loop_real_data_tsmom"
CODE_COMMIT: Final = "0123456789abcdef0123456789abcdef01234567"  # well-formed, fake (TEST ONLY)

COST_MODEL: Final = CostModelSpec(
    name="cost_v1",
    version="1.0.0",
    created_at=datetime(2023, 11, 1, tzinfo=UTC),
    fee_rate_per_side=Decimal("0.0004"),
    slippage_rate_per_side=Decimal("0.0001"),
)
LABEL: Final = OutcomeLabelSpec.bind(
    OutcomeSpec(
        name="loop_smoke_forward_15m",
        version="1.0.0",
        created_at=datetime(2023, 11, 1, tzinfo=UTC),
        horizon=15 * MINUTE,
        label_definition="TEST ONLY: 15-minute forward return on dataset bars (loop smoke)",
    ),
    OutcomeMethod.FORWARD_RETURN,
)
LOG_RETURN: Final = BarLogReturnProvider.spec()
#: TEST ONLY state-model parameters (a model parameter, not a validation threshold).
TREND: Final = TrendRangeProvider.spec(LOG_RETURN.ref, window=30, threshold="0.3")

#: TEST ONLY — arbitrary, uncalibrated numbers (see module docstring).
LOOP_REAL_DATA_TEST_ONLY_PROFILE: Final = factories.validation_profile(
    name="test_only_loop_real_data_uncalibrated",
    data_split=DataSplitParams(
        research_window_start=DAY1,
        sealed_oos_boundary=DAY2,
        sealed_oos_length=timedelta(days=1),
        sealed_oos_max_extension=timedelta(0),
        embargo=15 * MINUTE,
        walk_forward=WalkForwardParams(
            train_window=2 * HOUR,
            test_window=HOUR,
            step=HOUR,
            min_positive_window_fraction=0.5,
            max_single_window_pnl_share=0.6,
        ),
    ),
    sample_size=SampleSizeParams(
        min_effective_trades_in_sample=10,
        min_effective_trades_out_of_sample=5,
        min_effective_trades_per_state=5,
        effective_sample_method="overlap-clusters",
        min_regime_coverage="test-only",
    ),
    significance=SignificanceParams(
        multiple_testing_method="bonferroni",
        multiple_testing_threshold=0.05,
        overfitting_metric="pbo_cscv",
        overfitting_threshold=0.3,
        trial_count_scope="family",
    ),
    benchmark=BenchmarkParams(
        null_model="random-entry",
        null_model_simulations=50,
        null_model_percentile=90.0,
        market_benchmark_rule="test-only",
        inverse_control_reported=False,
    ),
    parameter_stability=ParameterStabilityParams(
        neighborhood_definition="adjacent_grid",
        min_neighborhood_performance_ratio=0.3,
        min_positive_neighbor_fraction=0.5,
        time_alignment_offsets=(MINUTE,),
    ),
    cost_stress=CostStressParams(
        cost_model=COST_MODEL.ref,
        fill_assumption="next-bar-open",
        stress_multipliers=(2.0,),
        reported_only_multipliers=(3.0,),
        delay_stress_bars=1,
        min_breakeven_cost_multiple=1.5,
    ),
)
#: TEST ONLY — explicit parameters for rules without a Profile field.
LOOP_REAL_DATA_TEST_ONLY_PARAMS: Final = RobustnessParams(
    cscv_partitions=4,
    max_participation_rate=0.01,
    min_capacity=0.0,
    impact_coefficient=0.1,
    cross_asset_min_positive_fraction=None,
    max_undersampled_pnl_share=0.5,
)
#: TEST ONLY budget.
LOOP_REAL_DATA_TEST_ONLY_BUDGET: Final = LoopBudget(
    max_trials_per_round=3,
    max_trials_total=10,
    max_llm_cost_units=Decimal(1),
    max_compute_seconds=Decimal(1000),
)
SELECTION: Final = ProfileSelection(
    selection_rule=Ref(kind=Kind.PROFILE_SELECTION_RULE, name="test_only_rule", version="1.0.0"),
    selection_rule_hash="f" * 64,
    key=ProfileSelectionKey(
        venue="binance", symbol="BTC_USDT", timeframe="1m", research_class="intraday"
    ),
)


# =========================================================================================
# fixture data: two UTC days of Binance-format 1m klines through the real ingestion path
# =========================================================================================


def _klines(base: int, seed: int, day2_bars: int = DAY2_BARS) -> list[list[Any]]:
    """``DAY1_BARS + day2_bars`` contiguous Binance 12-slot klines from ``FIRST_BAR``.

    Deterministic integer LCG, exact ``Decimal`` prices (cents), strings as the venue sends them;
    trend flips and volatility clusters so the TEST ONLY state model sees more than one regime.
    """
    first_ms = int(FIRST_BAR.timestamp() * 1000)
    state = seed
    price = Decimal(base)
    cent = Decimal("0.01")
    out: list[list[Any]] = []
    for index in range(DAY1_BARS + day2_bars):
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        drift = Decimal(base) / 20_000 * (1 if (index // 45) % 2 == 0 else -1)
        scale = Decimal(1 + (index // 70) % 3)
        shock = Decimal(int((state >> 33) % 2001) - 1000) / 1000 * Decimal(base) / 2_000 * scale
        opened = price
        closed = (price + drift + shock).quantize(cent)
        wick = (Decimal(int((state >> 12) % 400)) / 100 * scale).quantize(cent)
        volume = Decimal(5 + int((state >> 24) % 50)) / 10
        out.append(
            kline_item(
                first_ms + index * ss.MINUTE_MS,
                open_=f"{opened:.8f}",
                high=f"{max(opened, closed) + wick:.8f}",
                low=f"{min(opened, closed) - wick:.8f}",
                close=f"{closed:.8f}",
                volume=f"{volume:.8f}",
                quote_volume=f"{(volume * closed).quantize(cent):.8f}",
                trades=50 + index % 17,
                taker_base=f"{volume / 2:.8f}",
                taker_quote=f"{(volume * closed / 2).quantize(cent):.8f}",
            )
        )
        price = closed
    return out


def _ingest(w: ds.World, day2_bars: int = DAY2_BARS) -> None:
    """E2 listings, then archive + REST klines of both symbols on both days, reports."""
    w.listed(ds.TRADING, ds.L1)
    for venue, tag, base, seed in ((BTC, "btc", 36_000, 20231114), (ETH, "eth", 2_000, 20231115)):
        items = _klines(base, seed, day2_bars)
        ingest_klines(w, venue, tag=f"{tag}-d1", base="0", items=items[:DAY1_BARS])
        ingest_klines(w, venue, tag=f"{tag}-d2", base="0", items=items[DAY1_BARS:], day=DAY2)
    w.report("klines_1m", symbols=(BTC, ETH), days=(DAY1, DAY2), listing=True)


def _assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    """Bind the ADR-0032 archive event-time assumption (a bar is known 5 s after its close)."""
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


@dataclass(frozen=True)
class Manifests:
    """The F3 builds behind the declared rounds."""

    pairs: tuple[tuple[DatasetBuilt, DatasetBuilt], ...]  # (interval, point) per round
    sealed: DatasetBuilt

    @property
    def rounds(self) -> tuple[DatasetRound, ...]:
        (i0, p0), (i1, p1) = self.pairs
        return (
            DatasetRound(i0.manifest.content_hash(), p0.manifest.content_hash()),
            DatasetRound(
                i1.manifest.content_hash(),
                p1.manifest.content_hash(),
                sealed_manifest_hash=self.sealed.manifest.content_hash(),
            ),
        )


def _build(w: ds.World) -> Manifests:
    day = (DAY_START, DAY_END)
    pairs = tuple(
        (
            rt.build(
                w,
                _assumed(w.spec(interval=(DAY_START, cutoff), skip=rt.OWN)),
                data_type="klines_1m",
                window=day,
            ),
            rt.build(
                w, _assumed(w.spec(at=cutoff, skip=rt.OWN)), data_type="klines_1m", window=day
            ),
        )
        for cutoff in CUTOFFS
    )
    sealed = rt.build(
        w,
        _assumed(w.spec(at=CUTOFFS[1], skip=rt.OWN)),
        data_type="klines_1m",
        window=(BOUNDARY, BOUNDARY + HOUR),
    )
    return Manifests(pairs, sealed)


def _catalog(w: ds.World, cache: VerifiedManifestCache | None = None) -> DatasetCatalog:
    return DatasetCatalog(
        adapter=w.h.adapter, storage=w.h.storage, builder=w.builder(), manifest_cache=cache
    )


def _knowledge(lookback: int) -> KnowledgeItem:
    return KnowledgeItem(
        name=f"k_real_data_tsmom_{lookback}",
        version="1.0.0",
        created_at=datetime(2023, 11, 1, tzinfo=UTC),
        source="test://real-format-fixture",
        license="test-only",
        claim=f"TEST ONLY: a {lookback}-bar time-series momentum on minute bars",
        conditions=("strategy = tsmom_bars@1.0.0", f"param lookback = {lookback}"),
        evidence_level=EvidenceLevel.E0_ANECDOTE,
    )


def _config(rounds: tuple[DatasetRound, ...]) -> DatasetLoopConfig:
    wiring = LoopWiring(
        feature_provider=BarLogReturnProvider((LOG_RETURN,)),
        feature_spec=LOG_RETURN,
        feature_chunk_bars=60,
        state_provider=TrendRangeProvider((TREND,)),
        state_spec=TREND,
        decision_step=5 * MINUTE,
        decision_warmup=61 * MINUTE,
        strategies=(library_entries()[0].candidate(),),
        backtester=BarBacktester(),
        cost_model=COST_MODEL,
        initial_equity=Decimal(1_000_000),
        outcome_provider=ForwardReturnOutcome((LABEL,)),
        label_spec=LABEL,
        robustness=LOOP_REAL_DATA_TEST_ONLY_PARAMS,
        profile_selection=SELECTION,
        declared_research_class="intraday",
        code_commit=CODE_COMMIT,
        environment_lock="test-only-lock",
        evolution=None,
    )
    return DatasetLoopConfig(
        loop_id="dataset_loop_smoke",
        seed=5,
        epoch=CUTOFFS[0],
        cadence=CUTOFFS[1] - CUTOFFS[0],
        budget=LOOP_REAL_DATA_TEST_ONLY_BUDGET,
        symbol=SYMBOL,
        rounds=rounds,
        ingest_compute_seconds=Decimal(1),
        wiring=wiring,
        family_id=FAMILY,
        knowledge=(_knowledge(60), _knowledge(240)),
        max_new_hypotheses_per_round=1,
        max_reevaluations_per_round=1,
        hypothesis_compute_seconds=Decimal("0.5"),
        compute_seconds_per_trial=Decimal(1),
        validation_compute_seconds=Decimal(5),
        state_compute_seconds=Decimal(1),
        profile=LOOP_REAL_DATA_TEST_ONLY_PROFILE,
        constitution_version="1.0.0",
    )


def _open(
    w: ds.World,
    config: DatasetLoopConfig,
    state_dir: Path,
    cache: VerifiedManifestCache | None = None,
    catalog: DatasetCatalog | None = None,
) -> DurableLoop:
    """Durable, on the composition's own ``state_dir / "bus"`` (cross-checked with the audit)."""
    return open_dataset_loop(config, state_dir=state_dir, catalog=catalog or _catalog(w, cache))


class LoadRecorder:
    """What the loop reads of manifests through one builder (shared with the G5 e2e).

    - ``requests``: every manifest hash the builder's ``ManifestStore`` is asked to load (before
      any row is read; ``load_manifest`` is the only way the loop loads a manifest, the verified
      cache included);
    - ``loads``: every verified load (``ManifestStore.load`` proves each one through
      ``verify_manifest``; every bar / feature read of a manifest loads it first);
    - ``bars``: every dataset bar read of the loop (``backtest_bars_from_dataset`` as the dataset
      ingest and the sealed pair call it) through this builder.

    Each entry records whether the family's sealed evaluation had already been claimed then."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, builder: DatasetBuilder, family: str = FAMILY
    ) -> None:
        self.requests: list[tuple[str, bool]] = []
        self.loads: list[tuple[str, bool]] = []
        self.bars: list[tuple[str, bool]] = []
        self.ledger: UnsealingLedger | None = None
        self._family = family
        verify, stores = builder.verify_manifest, builder.manifests

        def recording(manifest: ResearchDatasetManifest) -> None:
            self.loads.append((manifest.content_hash(), self._claimed()))
            verify(manifest)

        def recorded_store() -> Any:
            store = stores()
            load = store.load

            def recording_load(key: str) -> Any:
                self.requests.append((key, self._claimed()))
                return load(key)

            store.load = recording_load  # type: ignore[method-assign, assignment]
            return store

        read = vars(dataset_source)["backtest_bars_from_dataset"]  # the name the loop calls

        def recording_bars(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("builder") is builder:
                self.bars.append((kwargs["manifest_content_hash"], self._claimed()))
            return read(*args, **kwargs)

        monkeypatch.setattr(builder, "verify_manifest", recording)
        monkeypatch.setattr(builder, "manifests", recorded_store)
        monkeypatch.setattr(dataset_source, "backtest_bars_from_dataset", recording_bars)

    def _claimed(self) -> bool:
        return self.ledger is not None and self.ledger.is_evaluated(self._family)

    @staticmethod
    def _of(entries: list[tuple[str, bool]], builds: Iterable[DatasetBuilt]) -> list[Any]:
        hashes = {built.manifest.content_hash() for built in builds}
        return [entry for entry in entries if entry[0] in hashes]

    def of(self, builds: Iterable[DatasetBuilt]) -> list[tuple[str, bool]]:
        """The verified loads of ``builds``' manifests."""
        return self._of(self.loads, builds)

    def requested(self, builds: Iterable[DatasetBuilt]) -> list[tuple[str, bool]]:
        return self._of(self.requests, builds)

    def bars_of(self, builds: Iterable[DatasetBuilt]) -> list[tuple[str, bool]]:
        return self._of(self.bars, builds)

    def touched(self, builds: Iterable[DatasetBuilt]) -> list[tuple[str, bool]]:
        """Every request, verified load or bar read of ``builds``' manifests."""
        items = tuple(builds)
        return [*self.requested(items), *self.of(items), *self.bars_of(items)]


def _recorded(
    w: ds.World, monkeypatch: pytest.MonkeyPatch, cache: VerifiedManifestCache | None = None
) -> tuple[DatasetCatalog, LoadRecorder]:
    catalog = _catalog(w, cache)
    return catalog, LoadRecorder(monkeypatch, catalog.builder)


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[ds.World]:
    with ds.postgres_world(tmp_path / "catalog", postgres_test_catalog_uri()) as opened:
        yield opened


# =========================================================================================
# checks
# =========================================================================================


def _stage(record: LoopRecord, name: str) -> Any:
    return next(stage for stage in record.stages if stage.name == name)


def _check_audit(records: tuple[LoopRecord, ...], state_dir: Path) -> None:
    assert [r.round_index for r in records] == [0, 1]
    assert [r.as_of for r in records] == list(CUTOFFS)
    assert all(r.status is RoundStatus.COMPLETED for r in records), [
        (s.name, s.status.value, s.error) for r in records for s in r.stages
    ]
    assert all(s.status is StageStatus.COMPLETED for r in records for s in r.stages)
    # hash-chained, and the durable audit file replays (and re-verifies) to the same records
    assert records[0].previous_hash is None
    assert records[1].previous_hash == records[0].record_hash
    assert [r.record_hash for r in LoopAuditLog(state_dir / AUDIT_FILE).records] == [
        r.record_hash for r in records
    ]
    budget = LOOP_REAL_DATA_TEST_ONLY_BUDGET
    assert records[-1].total_usage.trials <= budget.max_trials_total
    assert all(r.round_usage.trials <= budget.max_trials_per_round for r in records)


def _check_ingest(records: tuple[LoopRecord, ...], manifests: Manifests) -> None:
    for record, declared, (interval, point) in zip(
        records, manifests.rounds, manifests.pairs, strict=True
    ):
        summary = _stage(record, "ingest").summary
        assert summary["source"] == "research_dataset" and summary["symbol"] == SYMBOL
        assert (summary["feature_manifest_hash"], summary["price_manifest_hash"]) == (
            interval.manifest.content_hash(),
            point.manifest.content_hash(),
        )
        assert summary["sealed_manifest_hash"] == declared.sealed_manifest_hash
        # the round's cutoff: the price view and every research bar are known by as_of
        assert datetime.fromisoformat(summary["price_view"]) == record.as_of
        assert datetime.fromisoformat(summary["latest_available_time"]) <= record.as_of
        assert summary["research_bars"] > 0 and summary["decision_times"] > 0
    first, second = (_stage(r, "ingest").summary for r in records)
    # the research data grows with the rounds; only round 1 declares a (withheld) sealed manifest
    assert first["research_bars"] < second["research_bars"]
    # the sealed window is recorded as a declaration only (review fixes 4): its hash and the
    # Profile's window; nothing of it is read, so nothing is counted
    window = [BOUNDARY.isoformat(), (BOUNDARY + timedelta(days=1)).isoformat()]
    assert first["sealed_window"] == second["sealed_window"] == window
    assert first["sealed_manifest_hash"] is None
    assert second["sealed_manifest_hash"] == manifests.sealed.manifest.content_hash()
    assert first["sealed_bars_withheld"] is None and second["sealed_bars_withheld"] is None
    assert "unused_bars" not in first and "unused_bars" not in second


def _check_sealed_never_read(recorder: LoadRecorder, manifests: Manifests) -> None:
    """The withheld sealed manifest is never requested, loaded or read as bars, while the
    research pairs are (the recorder does see the loop's reads)."""
    assert recorder.touched((manifests.sealed,)) == []
    pairs = [built for pair in manifests.pairs for built in pair]
    assert recorder.requested(pairs) and recorder.of(pairs) and recorder.bars_of(pairs)
    assert {key for key, _ in recorder.bars} == {
        point.manifest.content_hash() for _, point in manifests.pairs
    }


def _check_no_future_and_no_sealed(records: tuple[LoopRecord, ...], memory: ResearchMemory) -> None:
    as_of = {r.round_index: r.as_of for r in records}
    trials = [o for o in memory.trials if o.inputs is not None]
    assert {o.round_index for o in trials} == {0, 1}
    for outcome in trials:
        inputs, cutoff = outcome.inputs, as_of[outcome.round_index]
        assert inputs is not None and inputs.knowledge_cutoff <= cutoff
        assert inputs.bars and all(bar.available_time <= cutoff for bar in inputs.bars)
        assert all(bar.interval_end <= BOUNDARY for bar in inputs.bars)  # no sealed bar
        assert all(bar.interval_start >= DAY_START for bar in inputs.bars)
        assert inputs.signals and all(
            s.available_time <= cutoff and s.knowledge_time <= cutoff for s in inputs.signals
        )
        assert inputs.decision_times and max(inputs.decision_times) <= cutoff
        assert max(inputs.decision_times) + LABEL.horizon <= BOUNDARY
        # the reproducibility tuple names the round's two manifests' Research Dataset snapshots
        snapshots = outcome.run.repro.dataset_snapshots
        assert [s.zone.value for s in snapshots] == ["research_dataset"] * 2
        assert all(s.time_range_end <= BOUNDARY for s in snapshots)
    # the sealed window stays sealed: no unsealing, no G5 anywhere
    assert memory.oos_ledger.count() == 0
    for result in memory.validations:
        assert result.sealed_report is None
        assert result.sealed_status["status"] in {"sealed", "not_run"}


def _check_reports(records: tuple[LoopRecord, ...], memory: ResearchMemory) -> None:
    assert memory.validations, "at least one trial must reach validation"
    for result in memory.validations:
        report = result.report
        assert report is not None, result.error
        binding = next(g for g in report.gates if g.gate_id == "G0.manifest_binding")
        assert binding.verdict is Verdict.PASS and binding.value == 0.0
        assert report.verdict in set(Verdict)  # a defined verdict; never asserted to PASS
        assert report.validation_profile_hash == LOOP_REAL_DATA_TEST_ONLY_PROFILE.content_hash()
    rows = [row for r in records for row in _stage(r, "validation").summary["reports"]]
    assert len(rows) == len(memory.validations)
    for row in rows:
        gates = {gate["gate_id"]: gate["verdict"] for gate in row["gates"]}
        assert gates["G0.manifest_binding"] == "PASS"
        assert row["verdict"] in {v.value for v in Verdict}
    # the experiment rows name the manifests the trial's data came from
    for record, (interval, point) in zip(records, _pairs_of(records), strict=True):
        for row in _stage(record, "experiment").summary["experiments"]:
            assert (row["feature_manifest_hash"], row["price_manifest_hash"]) == (interval, point)


def _pairs_of(records: tuple[LoopRecord, ...]) -> list[tuple[str, str]]:
    return [
        (
            _stage(r, "ingest").summary["feature_manifest_hash"],
            _stage(r, "ingest").summary["price_manifest_hash"],
        )
        for r in records
    ]


def _check_lifecycle(loop: Any) -> None:
    transitions = [t for history in loop.guard.histories for t in history.transitions]
    assert transitions
    states = {t.to_state for t in transitions}
    assert states <= AUTOMATABLE_TARGETS and states.isdisjoint(FORBIDDEN_TARGETS)
    assert all(t.approved_by is None for t in transitions)


# =========================================================================================
# the test
# =========================================================================================


def test_two_unattended_rounds_run_on_verified_pit_datasets(
    pg: ds.World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(pg)
    manifests = _build(pg)
    assert not manifests.pairs[0][0].replayed
    for interval, point in manifests.pairs:
        assert ASSUMPTION_BINDING in interval.manifest.point_in_time.availability_bindings
        assert point.manifest.point_in_time.simulation_time is not None
    config = _config(manifests.rounds)

    # ---- first run: two unattended rounds, durable, with the opt-in verified-manifest cache ----
    state_dir = tmp_path / "state-1"
    cache = VerifiedManifestCache()
    catalog, recorder = _recorded(pg, monkeypatch, cache)
    durable = _open(pg, config, state_dir, catalog=catalog)
    records = durable.loop.run_unattended(2)
    # each round loads its price manifest, the pair (feature + price), the feature manifest and
    # the price manifest's bars; the withheld sealed manifest of round 1 is never loaded: 4
    # distinct manifests are proven once (nothing is written to the catalog in between, so every
    # miss is stored), 6 loads reuse them
    stats = cache.stats
    assert (stats.hits, stats.misses, stats.stored, stats.uncacheable) == (6, 4, 4, 0)
    _check_audit(records, state_dir)
    _check_ingest(records, manifests)
    _check_sealed_never_read(recorder, manifests)
    _check_no_future_and_no_sealed(records, durable.memory)
    _check_reports(records, durable.memory)
    _check_lifecycle(durable.loop)
    # the automatic durable bus holds exactly the audit's rounds
    assert check_round_bus(durable.bus, config.loop_id, records) == 0
    durable.close()

    # the state directory is bound to its declared rounds (another manifest list is refused)
    other = replace(config, rounds=(manifests.rounds[1], manifests.rounds[0]))
    with pytest.raises(LoopStateInconsistent):
        _open(pg, other, state_dir)

    # ---- rerun in a fresh process on the same catalog, which reads the persisted manifests by
    # their declared hashes only (nothing is rebuilt), with the durable state reopened between
    # the rounds, and without the cache (every load re-verifies): every record hash is identical
    # ----
    pg.h.reopen()
    rerun_dir = tmp_path / "state-2"
    with _open(pg, config, rerun_dir) as opened:
        [first] = opened.loop.run_unattended(1)
    rerun_catalog, rerun_recorder = _recorded(pg, monkeypatch)  # every load verified (no cache)
    with _open(pg, config, rerun_dir, catalog=rerun_catalog) as reopened:
        [second] = reopened.loop.run_unattended(1)
        _check_lifecycle(reopened.loop)
    assert [first.record_hash, second.record_hash] == [r.record_hash for r in records]
    # round 1 (the withheld declaration) again: the sealed manifest is never touched
    assert rerun_recorder.touched((manifests.sealed,)) == []
    assert rerun_recorder.requested(manifests.pairs[1]) and rerun_recorder.bars_of(
        manifests.pairs[1]
    )

    # ---- a declared pair whose view is after the round's as_of is refused at ingest ----
    early = _config((manifests.rounds[1],))  # round 0 (as of 21:00) declares the 01:00 view
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    loop = build_dataset_loop(early, catalog=_catalog(pg), bus=InMemoryEventBus(), memory=memory)
    [record] = loop.run_unattended(1)
    ingest = _stage(record, "ingest")
    assert ingest.status is StageStatus.FAILED
    assert "after the round's cutoff" in (ingest.error or "")
    assert record.status is RoundStatus.FAILED
    assert not memory.trials and not memory.validations


def test_a_dataset_loop_refuses_an_unseal_budget() -> None:
    config = _config((DatasetRound("a" * 64, "b" * 64),))
    unseal = OosUnsealBudget(max_unsealings=1, approved_families={FAMILY: "test-human"})
    wiring = replace(config.wiring, oos_unseal=unseal, sealed_decision_step=HOUR)
    with pytest.raises(ValueError, match="G5 over dataset rounds is not wired"):
        replace(config, wiring=wiring)
    with pytest.raises(ValueError, match="DatasetRound"):
        replace(config, rounds=())
