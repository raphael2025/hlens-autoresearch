"""Sealed OOS (G5) of the dataset-backed research loop over verified PIT Research Datasets.

ADR-0049 implementation note (dataset G5, 2026-09-26). A ``DatasetRound`` may declare a sealed
manifest **pair** (interval feature + point price manifest over exactly the Profile's sealed
window); with an ``OosUnsealBudget`` the loop then runs G5 on it with the same guarantees as the
synthetic path. The fixture data is the one of ``test_research_loop_real_data.py`` (Binance-format
1m klines through the real ingestion path into the PostgreSQL **test** catalog), with five hours on
the second day instead of one: research window 2023-11-14 18:00 - 24:00, sealed window 2023-11-15
00:00 - 05:00. The sealed features are computed from the sealed manifest alone (no feature bridges
the boundary), so their first evaluations are not computable and the TEST ONLY 240-bar momentum
needs a four-hour warm-up inside the sealed window before it can take a position. Built once per
module:

- the research pair at 05:00 (interval ``[2023-11-14, 05:00)`` + point view at 05:00, data window
  of the research day);
- the sealed pair (interval ``[00:00, 05:00)`` + point view at 05:00, data window = the sealed
  window) on the same upstream snapshots;
- a "foreign" sealed pair over the same window, built after one more (unchanged) exchangeInfo
  observation moved the listing heads: the same market data on other upstream snapshots.

Checked: G5 runs once for the approved family and its report carries G5 gates plus
``G0.manifest_binding`` for the sealed pair; no sealed manifest is loaded before the family's
evaluation is claimed (every verified manifest load is recorded); an unapproved family is never
unsealed and its sealed pair is never read; a sealed pair on other snapshots is refused after the
claim (``consumed_without_result``, never evaluated); a restart does not unseal again; the validated
instrument must be a member of both pairs (review fixes 4). In-memory loops here carry the TEST-ONLY
``ephemeral_unseal_for_tests`` flag (an unseal budget otherwise needs a durable ledger).

!!! TEST ONLY !!!  ``G5_TEST_ONLY_PROFILE``, ``G5_TEST_ONLY_COST_MODEL`` and the robustness numbers
are deliberately permissive, uncalibrated numbers whose only purpose is to let a trial on a few
hours of fixture minutes reach the in-sample PASS that G5 requires, so the sealed path can be
exercised at all. The in-sample PASS here is a precondition of the pipe under test, **not** a
market conclusion; these numbers must never be used for research.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from apps.worker import RoundStatus, StageStatus
from core.contracts.cost_model import CostModelSpec
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
from core.domain.research import EvidenceLevel, KnowledgeItem, ValidationReport, Verdict
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.event_bus import InMemoryEventBus
from research.loop import (
    DatasetCatalog,
    DatasetLoopConfig,
    DatasetRound,
    OosUnsealBudget,
    ResearchMemory,
    ValidationOutcome,
    build_dataset_loop,
    dataset_source,
    open_dataset_loop,
)
from research.strategies.failure_registry import FailureRegistry
from research.validation.pipeline import CONSUMED_WITHOUT_RESULT
from tests import factories
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e import test_research_loop_real_data as base
from tests.infrastructure.redteam import redteam_support as rt

pytestmark = pytest.mark.postgres

MINUTE: Final = timedelta(minutes=1)
HOUR: Final = timedelta(hours=1)
DAY_START: Final = datetime(2023, 11, 14, tzinfo=UTC)
BOUNDARY: Final = base.BOUNDARY  # 2023-11-15 00:00, the sealed OOS boundary
SEALED_LENGTH: Final = 5 * HOUR  # the TEST ONLY Profile's sealed window
G5_DAY2_BARS: Final = 300  # 2023-11-15 00:00 - 05:00: exactly the sealed window
WINDOW_END: Final = BOUNDARY + SEALED_LENGTH
CUTOFF: Final = WINDOW_END  # 2023-11-15 05:00: the first round cutoff after the sealed window
FAMILY: Final = base.FAMILY
HUMAN: Final = "test-human-approver"

#: TEST ONLY — a near-zero cost model (see module docstring).
G5_TEST_ONLY_COST_MODEL: Final = CostModelSpec(
    name="cost_test_only_g5",
    version="1.0.0",
    created_at=datetime(2023, 11, 1, tzinfo=UTC),
    fee_rate_per_side=Decimal("0.000001"),
    slippage_rate_per_side=Decimal("0.000001"),
)
#: TEST ONLY — permissive, uncalibrated numbers (see module docstring).
G5_TEST_ONLY_PROFILE: Final = factories.validation_profile(
    name="test_only_loop_real_data_g5_permissive",
    data_split=DataSplitParams(
        research_window_start=base.DAY1,
        sealed_oos_boundary=base.DAY2,
        sealed_oos_length=SEALED_LENGTH,
        sealed_oos_max_extension=timedelta(0),
        embargo=15 * MINUTE,
        walk_forward=WalkForwardParams(
            train_window=18 * HOUR,
            test_window=3 * HOUR,
            step=3 * HOUR,
            min_positive_window_fraction=0.0,
            max_single_window_pnl_share=1.0,
        ),
    ),
    sample_size=SampleSizeParams(
        min_effective_trades_in_sample=1,
        min_effective_trades_out_of_sample=1,
        min_effective_trades_per_state=1,
        effective_sample_method="overlap-clusters",
        min_regime_coverage="test-only",
    ),
    significance=SignificanceParams(
        multiple_testing_method="bonferroni",
        multiple_testing_threshold=0.5,
        overfitting_metric="pbo_cscv",
        overfitting_threshold=1.0,
        trial_count_scope="family",
    ),
    benchmark=BenchmarkParams(
        null_model="random-entry",
        null_model_simulations=50,
        null_model_percentile=0.0,
        # ADR-0060 enforcement: a registered rule (the "test-only" placeholder is INCONCLUSIVE)
        market_benchmark_rule="buy_and_hold_equal_weight",
        inverse_control_reported=True,
    ),
    parameter_stability=ParameterStabilityParams(
        neighborhood_definition="adjacent_grid",
        min_neighborhood_performance_ratio=-1000.0,
        min_positive_neighbor_fraction=0.0,
        time_alignment_offsets=(MINUTE,),
    ),
    cost_stress=CostStressParams(
        cost_model=G5_TEST_ONLY_COST_MODEL.ref,
        fill_assumption="next-bar-open",
        stress_multipliers=(1.0,),
        delay_stress_bars=1,
        min_breakeven_cost_multiple=0.001,
    ),
)
#: TEST ONLY — the sealed window's decision step.
SEALED_STEP: Final = 5 * MINUTE
#: The hypothesis under test, and a second hypothesis of the same family with the same trial
#: point (registered in a later round: a later in-sample PASS of the family).
MOMENTUM: Final = base._knowledge(240)
REPLICA: Final = KnowledgeItem(
    name="k_real_data_tsmom_240_replica",
    version="1.0.0",
    created_at=datetime(2023, 11, 1, tzinfo=UTC),
    source="test://real-format-fixture/replica",
    license="test-only",
    claim="TEST ONLY: the 240-bar momentum again, proposed by a second source",
    conditions=MOMENTUM.conditions,
    evidence_level=EvidenceLevel.E0_ANECDOTE,
)


# =========================================================================================
# fixture: one catalog per module (ingest + six builds)
# =========================================================================================


@dataclass(frozen=True)
class G5World:
    world: ds.World
    research: tuple[DatasetBuilt, DatasetBuilt]  # (interval, point) at CUTOFF
    sealed: tuple[DatasetBuilt, DatasetBuilt]  # over exactly the sealed window
    foreign: tuple[DatasetBuilt, DatasetBuilt]  # the same window, other upstream snapshots

    def round(self, sealed: tuple[DatasetBuilt, DatasetBuilt] | None = None) -> DatasetRound:
        interval, point = self.research
        pair = self.sealed if sealed is None else sealed
        return DatasetRound(
            interval.manifest.content_hash(),
            point.manifest.content_hash(),
            sealed_feature_manifest_hash=pair[0].manifest.content_hash(),
            sealed_price_manifest_hash=pair[1].manifest.content_hash(),
        )


def _pair(
    w: ds.World, start: datetime, window: tuple[datetime, datetime], skip: tuple[str, ...]
) -> tuple[DatasetBuilt, DatasetBuilt]:
    interval = base._assumed(w.spec(interval=(start, CUTOFF), skip=skip))
    point = base._assumed(w.spec(at=CUTOFF, skip=skip))
    return (
        rt.build(w, interval, data_type="klines_1m", window=window),
        rt.build(w, point, data_type="klines_1m", window=window),
    )


@pytest.fixture(scope="module")
def g5(tmp_path_factory: pytest.TempPathFactory) -> Iterator[G5World]:
    root = tmp_path_factory.mktemp("g5")
    with ds.postgres_world(root / "catalog", postgres_test_catalog_uri()) as w:
        base._ingest(w, G5_DAY2_BARS)
        research = _pair(w, DAY_START, (DAY_START, BOUNDARY), rt.OWN)
        sealed = _pair(w, BOUNDARY, (BOUNDARY, WINDOW_END), rt.OWN)
        w.listed(ds.TRADING, ds.L2)  # the same statuses observed again: listing heads move
        w.report("klines_1m", symbols=(), days=(), listing=True)  # its listing report
        foreign = _pair(w, BOUNDARY, (BOUNDARY, WINDOW_END), rt.OWN)
        yield G5World(w, research, sealed, foreign)


# =========================================================================================
# helpers
# =========================================================================================


#: Every manifest request, verified load and dataset bar read of the loop's builder, with whether
#: the family's sealed evaluation had already been claimed then (shared with the withheld e2e).
LoadRecorder = base.LoadRecorder


def _catalog(g5: G5World, monkeypatch: pytest.MonkeyPatch) -> tuple[DatasetCatalog, LoadRecorder]:
    w = g5.world
    builder = w.builder()
    return (
        DatasetCatalog(adapter=w.h.adapter, storage=w.h.storage, builder=builder),
        LoadRecorder(monkeypatch, builder),
    )


def _config(
    rounds: tuple[DatasetRound, ...], *, replica: bool = False, approved: str = FAMILY
) -> DatasetLoopConfig:
    config = base._config(rounds)
    wiring = replace(
        config.wiring,
        cost_model=G5_TEST_ONLY_COST_MODEL,
        robustness=replace(config.wiring.robustness, max_undersampled_pnl_share=1.0),
        oos_unseal=OosUnsealBudget(max_unsealings=1, approved_families={approved: HUMAN}),
        sealed_decision_step=SEALED_STEP,
    )
    return replace(
        config,
        loop_id="dataset_loop_g5",
        epoch=CUTOFF,
        wiring=wiring,
        knowledge=(MOMENTUM, REPLICA) if replica else (MOMENTUM,),
        profile=G5_TEST_ONLY_PROFILE,
    )


def _in_memory(
    g5: G5World, monkeypatch: pytest.MonkeyPatch, config: DatasetLoopConfig, tmp_path: Path
) -> tuple[ResearchMemory, LoadRecorder, Any]:
    catalog, recorder = _catalog(g5, monkeypatch)
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    recorder.ledger = memory.oos_ledger
    # in memory: the unseal budget carries the TEST-ONLY ephemeral-ledger flag (review fixes 4)
    budget = config.wiring.oos_unseal
    assert budget is not None
    config = replace(
        config,
        wiring=replace(config.wiring, oos_unseal=replace(budget, ephemeral_unseal_for_tests=True)),
    )
    loop = build_dataset_loop(config, catalog=catalog, bus=InMemoryEventBus(), memory=memory)
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED, [
        (s.name, s.status.value, s.error) for s in record.stages
    ]
    return memory, recorder, loop


def _only(memory: ResearchMemory, round_index: int) -> ValidationOutcome:
    [result] = [r for r in memory.validations if r.round_index == round_index]
    report = result.report
    assert report is not None, result.error
    # the precondition of G5 (a TEST ONLY Profile makes it reachable; not a market conclusion)
    assert report.verdict is Verdict.PASS, [(g.gate_id, g.verdict.value) for g in report.gates]
    return result


def _gates(report: ValidationReport) -> dict[str, Any]:
    return {gate.gate_id: gate for gate in report.gates}


# =========================================================================================
# tests
# =========================================================================================


def test_g5_runs_once_for_the_approved_family_and_a_restart_does_not_unseal_again(
    g5: G5World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config((g5.round(), g5.round()), replica=True)
    state_dir = tmp_path / "state"

    # ---- round 0 (as of 05:00, after the sealed window): the family is unsealed once ----
    catalog, recorder = _catalog(g5, monkeypatch)
    with open_dataset_loop(config, state_dir=state_dir, catalog=catalog) as durable:
        recorder.ledger = durable.memory.oos_ledger
        [record] = durable.loop.run_unattended(1)
        assert record.status is RoundStatus.COMPLETED
        assert all(s.status is StageStatus.COMPLETED for s in record.stages)
        ingest = base._stage(record, "ingest").summary
        assert ingest["sealed_bars_withheld"] is None  # the ingest reads nothing of the pair
        assert ingest["sealed_price_manifest_hash"] == g5.sealed[1].manifest.content_hash()
        result = _only(durable.memory, 0)
        status = result.sealed_status
        assert status["status"] == "unsealed" and status["approved_by"] == HUMAN, str(status)
        assert status["sealed_bars"] == G5_DAY2_BARS - 1  # the last bar is known after 05:00
        sealed_report = result.sealed_report
        assert sealed_report is not None and sealed_report.verdict in set(Verdict)
        gates = _gates(sealed_report)
        assert {"G5.unsealing_recorded", "G5.oos_effective_sample_size"} <= set(gates)
        assert gates["G5.unsealing_recorded"].verdict is Verdict.PASS
        binding = gates["G0.manifest_binding"]  # the sealed pair (and the research bars) bound
        assert binding.verdict is Verdict.PASS and binding.value == 0.0
        assert status["manifest_binding_mismatches"] == []
        assert sealed_report.gates[0].gate_id == "G0.manifest_binding"
        # the audit carries the same G5 gates
        [row] = base._stage(record, "validation").summary["reports"]
        audited = {gate["gate_id"]: gate["verdict"] for gate in row["sealed_oos"]["gates"]}
        assert audited["G0.manifest_binding"] == "PASS" and "G5.unsealing_recorded" in audited
        # one unsealing, evaluated, by the approving human
        ledger = durable.memory.oos_ledger
        unsealing = ledger.get(FAMILY)
        assert ledger.count() == 1 and ledger.is_evaluated(FAMILY)
        assert unsealing is not None and unsealing.approved_by == HUMAN
        # no sealed manifest was loaded before the claim; the sealed pair was read after it
        sealed_loads = recorder.of(g5.sealed)
        assert sealed_loads and all(claimed for _, claimed in sealed_loads)
        assert {key for key, _ in sealed_loads} == {b.manifest.content_hash() for b in g5.sealed}
        # nothing of it was even requested before the claim; its bars were read once, after it
        touched = recorder.touched(g5.sealed)
        assert touched and all(claimed for _, claimed in touched)
        assert recorder.bars_of(g5.sealed) == [(g5.sealed[1].manifest.content_hash(), True)]
        base._check_lifecycle(durable.loop)
        first = record

    # ---- restart: a new process state over the same directory; round 1 does not unseal ----
    catalog, recorder = _catalog(g5, monkeypatch)
    with open_dataset_loop(config, state_dir=state_dir, catalog=catalog) as reopened:
        ledger = reopened.memory.oos_ledger
        recorder.ledger = ledger
        assert ledger.count() == 1 and ledger.is_evaluated(FAMILY)
        [restored] = [r for r in reopened.memory.validations if r.round_index == 0]
        assert restored.sealed_report is not None
        assert restored.sealed_report.content_hash() == sealed_report.content_hash()
        [second] = reopened.loop.run_unattended(1)
        assert second.previous_hash == first.record_hash
        assert second.status is RoundStatus.COMPLETED
        again = _only(reopened.memory, 1)  # a new hypothesis of the same family passes
        assert again.sealed_report is None
        assert again.sealed_status == {
            "status": "sealed",
            "reason": "the family already used its unsealing",
        }
        assert ledger.count() == 1
        assert recorder.touched(g5.sealed) == []  # nothing of the sealed pair read at all
        base._check_lifecycle(reopened.loop)


def test_an_unapproved_family_is_never_unsealed_and_its_sealed_pair_never_read(
    g5: G5World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config((g5.round(),), approved="another_family")
    memory, recorder, loop = _in_memory(g5, monkeypatch, config, tmp_path)
    result = _only(memory, 0)
    assert result.sealed_report is None
    assert result.sealed_status == {
        "status": "sealed",
        "reason": "the family is not on the unseal budget's approved list",
    }
    assert memory.oos_ledger.count() == 0 and not memory.oos_ledger.is_evaluated(FAMILY)
    assert recorder.touched(g5.sealed) == []
    # the research pair was read (the recorder does see loads)
    assert recorder.of(g5.research)
    base._check_lifecycle(loop)


def test_a_sealed_pair_on_other_snapshots_is_refused_after_the_claim(
    g5: G5World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    research = g5.research[1].manifest.point_in_time.snapshot_bindings
    foreign = g5.foreign[1].manifest.point_in_time.snapshot_bindings
    moved = sorted(table for table in research if research[table] != foreign.get(table))
    assert moved  # the same window and data, other upstream snapshots
    config = _config((g5.round(g5.foreign),))
    memory, recorder, loop = _in_memory(g5, monkeypatch, config, tmp_path)
    result = _only(memory, 0)
    status = result.sealed_status
    assert status["status"] == CONSUMED_WITHOUT_RESULT
    assert status["reason"] == "sealed data refused"
    assert "upstream snapshots differ" in status["error"] and moved[0] in status["error"]
    assert "sealed_bars" not in status  # refused before any sealed bar was released
    report = result.sealed_report
    assert report is not None and report.verdict is Verdict.INCONCLUSIVE
    evaluation = _gates(report)["G5.oos_evaluation"]
    assert evaluation.metric == f"{CONSUMED_WITHOUT_RESULT}:sealed_data_refused"
    assert "G0.manifest_binding" not in _gates(report)
    # the one evaluation is consumed: the window stays closed for the family
    assert memory.oos_ledger.count() == 1 and memory.oos_ledger.is_evaluated(FAMILY)
    # only the two verified manifest loads happened, both after the claim; no bar path read
    loads = recorder.of(g5.foreign)
    assert len(loads) == 2 and all(claimed for _, claimed in loads)
    assert recorder.bars_of(g5.foreign) == []
    base._check_lifecycle(loop)


def _without_symbol(manifest: ResearchDatasetManifest) -> ResearchDatasetManifest:
    """``manifest`` whose universe no longer lists the validated instrument (the other symbol
    stays a member): a stand-in for a sealed window of another universe composition."""
    members = tuple(m for m in manifest.members if ds.symbol_of(m) != base.SYMBOL)
    assert members and len(members) < len(manifest.members)
    return manifest.model_copy(update={"members": members})


@pytest.mark.parametrize("side", ["research pair", "sealed pair"])
def test_the_validated_instrument_must_be_a_member_of_both_pairs(
    g5: G5World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, side: str
) -> None:
    """Review fixes 4: G5 validates the same single instrument on both sides; a pair whose
    universe does not list it is refused after the claim, before any sealed bar is read."""
    target = (g5.research if side == "research pair" else g5.sealed)[1].manifest.content_hash()
    # the research pair's price manifest reaches the sealed pair through the ingest's first
    # verified load; the sealed price manifest through the sealed pair's own verified load
    name = "load_verified_manifest" if side == "research pair" else "load_manifest"
    load = getattr(dataset_source, name)

    def stripped(*args: Any) -> ResearchDatasetManifest:
        manifest = load(*args)
        return _without_symbol(manifest) if manifest.content_hash() == target else manifest

    monkeypatch.setattr(dataset_source, name, stripped)
    memory, recorder, loop = _in_memory(g5, monkeypatch, _config((g5.round(),)), tmp_path)
    result = _only(memory, 0)
    status = result.sealed_status
    assert status["status"] == CONSUMED_WITHOUT_RESULT
    assert status["reason"] == "sealed data refused"
    assert f"{base.SYMBOL} is not a member of the {side}" in status["error"]
    assert "sealed_bars" not in status  # refused before any sealed bar was released
    report = result.sealed_report
    assert report is not None and report.verdict is Verdict.INCONCLUSIVE
    evaluation = _gates(report)["G5.oos_evaluation"]
    assert evaluation.metric == f"{CONSUMED_WITHOUT_RESULT}:sealed_data_refused"
    # the one evaluation is consumed; the sealed manifests were loaded only after the claim and
    # no sealed bar was read
    assert memory.oos_ledger.count() == 1 and memory.oos_ledger.is_evaluated(FAMILY)
    touched = recorder.touched(g5.sealed)
    assert touched and all(claimed for _, claimed in touched)
    assert recorder.bars_of(g5.sealed) == []
    base._check_lifecycle(loop)
