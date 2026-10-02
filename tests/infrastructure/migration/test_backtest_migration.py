"""Phase 14 migration drill (ADR-0106): ``BarBacktester`` -> ``ReferenceBacktester``.

The source is the production ``BarBacktester``; the target is the independent reference engine
(``plugins/backtest/reference.py``). The drill reruns the committed golden experiment
(``tests/golden/experiments/tsmom_g0_g4.py``: market -> TSMOM -> backtest -> G0 - G4) on the target,
compares it at **tolerance 0** with exactly one declared exclusion (``backtest.result_hash``: it
embeds the provider identity), checks conformance (``BACKTEST_CHECKS``) on both engines, proves the
comparison is falsifiable, produces rollback evidence by switching back to ``BarBacktester`` and
writes / reads back a ``MigrationReport``.

Baselines. The drill uses two golden records: the **committed** one (``GOLDEN_HASH``) and a
**fresh baseline** recorded from ``BarBacktester`` in this process (the method of
10-migration.md §4: baseline, migrate, roll back). Both are bit-identical in every output except
``backtest.result_hash``: the committed record's ``result_hash`` no longer reproduces under the
current contract envelope (2.5.0; it was regenerated at 2.2.0 and is red in
``test_golden_experiments.py`` independently of this drill — see the ADR-0106 implementation
note). That hash is the declared exclusion here, so the committed-record comparison is unaffected;
the rollback proof (bit-exact, nothing excluded) needs the fresh baseline.

Finding (recorded, not hidden): the full output set is **not** reproduced at tolerance 0 with that
single exclusion. Every engine-derived output is bit-identical (all 37 baseline gates, the
equity / fees / slippage / fill and unexecuted-target counts), but declaring the target to the
validator (``ValidatorSetup.backtester``, required: undeclared, the ADR-0060 benchmark re-run uses a
plain ``BarBacktester`` and cannot reproduce the target's ``result_hash``, so its items turn
INCONCLUSIVE) makes G0 add its structural ``G0.execution_model`` gate: three outputs that exist
only on the migrated side (``gate.G0.execution_model.value`` / ``.verdict``, ``report.gates`` 37 ->
38). Nothing here widens the tolerance or the exclusion list; the PM decides how to reconcile it
(see ``test_the_declared_exclusion_alone_does_not_reach_tolerance_zero``).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.strategy import BacktestRequest, BacktestResult, EquityPoint
from infrastructure.migration import (
    ConformanceReport,
    GoldenRecord,
    MigrationReport,
    MigrationTarget,
    RollbackVerdict,
    compare_golden,
    load_golden,
    load_migration_report,
    record_golden,
    rollback_evidence,
    run_conformance,
    save_migration_report,
)
from plugins.backtest import MONEY_QUANTUM, BarBacktester, ReferenceBacktester
from tests.contract_suites import backtest as backtest_suite
from tests.contract_suites.backtest import BACKTEST_CHECKS, BacktestSubject
from tests.golden.experiments import tsmom_g0_g4 as experiment
from tests.infrastructure.migration.test_golden_experiments import GOLDEN_HASH
from tests.strategy_fixtures import COSTS, make_bars, wave_closes

ZERO = Decimal(0)
SOURCE = "hlens_bar_backtest@1.0.0"
TARGET = "hlens_reference_backtest@1.0.0"
#: Outputs that exist only because the migrated side declares its backtester to the validator.
STRUCTURAL = {
    "gate.G0.execution_model.value",
    "gate.G0.execution_model.verdict",
    "report.gates",
}

MIGRATION = MigrationTarget(
    migration_id="p14-backtest-bar-to-reference",
    source=SOURCE,
    target=TARGET,
    tolerance=ZERO,
    excluded_outputs=("backtest.result_hash",),
    scope=(
        "BacktestProvider with the next_bar_open execution model",
        "committed golden experiment synthetic_tsmom60_g0_g4_lax_test_only_seed100",
        "BACKTEST_CHECKS conformance on source and target",
    ),
    out_of_scope=(
        "next_bar_open_participation / carry-over",
        "ExecutionModel (participation cap, impact, funding)",
        "run_with_risk / run_with_report",
        "replacing the production default BarBacktester (ADR-0106 decision 5)",
    ),
    limitations=(
        "both engines were written by the same team: the equivalence evidence is bounded by "
        "shared reading of the next_bar_open specification",
        "the golden experiment is synthetic and TEST ONLY: no claim about real markets",
        "backtest.result_hash is excluded because it embeds the provider identity",
    ),
)


type Engine = type[BarBacktester] | type[ReferenceBacktester]


def _subject(engine: Engine) -> BacktestSubject:
    return BacktestSubject(
        open=engine,
        bars=make_bars("BTCUSDT", wave_closes(12)),
        costs=COSTS,
        initial_equity=Decimal("10000"),
        tolerance=MONEY_QUANTUM * 1000,
    )


def _conformance(candidate: str, engine: Engine) -> ConformanceReport:
    return run_conformance(candidate, lambda: _subject(engine), BACKTEST_CHECKS)


@pytest.fixture(scope="module")
def committed() -> GoldenRecord:
    return load_golden(experiment.DIRECTORY, GOLDEN_HASH)


@pytest.fixture(scope="module")
def baseline() -> GoldenRecord:
    """The source engine's golden recorded now, before the migrated run (10-migration.md §4)."""
    return experiment.record()


@pytest.fixture(scope="module", params=["committed", "fresh_baseline"])
def golden(
    request: pytest.FixtureRequest, committed: GoldenRecord, baseline: GoldenRecord
) -> GoldenRecord:
    return committed if request.param == "committed" else baseline


@pytest.fixture(scope="module")
def declared_baseline() -> GoldenRecord:
    """The source engine declared to the validator, like the migrated run (ADR-0106 §2 as
    amended 2026-10-02): the like-for-like baseline of the migration comparison."""
    return record_golden(
        experiment.NAME,
        lambda: experiment.outputs(
            experiment.evaluate(backtester=BarBacktester(), declare_backtester=True)
        ),
    )


@pytest.fixture(scope="module")
def migrated() -> dict[str, Decimal]:
    """The golden experiment on the target, declared to the validator."""
    return experiment.outputs(
        experiment.evaluate(backtester=ReferenceBacktester(), declare_backtester=True)
    )


@pytest.fixture(scope="module")
def rolled_back() -> dict[str, Decimal]:
    """The golden experiment after switching back to the source (the default pipeline)."""
    return experiment.run()


# --- conformance -------------------------------------------------------------------------------


def test_the_target_passes_the_backtest_contract_suite_like_the_source() -> None:
    for candidate, engine in ((SOURCE, BarBacktester), (TARGET, ReferenceBacktester)):
        report = _conformance(candidate, engine)
        assert report.passed, report.failures
        assert report.checks == tuple(check.__name__ for check in BACKTEST_CHECKS)


class _IgnoresCosts(ReferenceBacktester):
    """Faulty target: simulates without the request's cost model (optimistic execution)."""

    def run(self, request: BacktestRequest) -> BacktestResult:
        cheap = super().run(request.model_copy(update={"cost_model": backtest_suite.ZERO_COST}))
        return BacktestResult.build(
            request,
            self.descriptor,
            fills=cheap.fills,
            equity_curve=cheap.equity_curve,
            unexecuted_targets=cheap.unexecuted_targets,
        )


def test_a_faulty_target_fails_conformance() -> None:
    report = run_conformance("faulty@1.0.0", lambda: _subject(_IgnoresCosts), BACKTEST_CHECKS)
    assert not report.passed
    assert [name for name, _ in report.failures] == [
        "check_buy_and_hold_with_costs",
        "check_round_trip_on_flat_prices_costs_only",
    ]


# --- golden comparison -------------------------------------------------------------------------


def test_the_fresh_baseline_agrees_with_the_committed_golden_beyond_the_stale_hash(
    committed: GoldenRecord, baseline: GoldenRecord
) -> None:
    """The source engine today differs from the committed record at most in ``result_hash``
    (the contract envelope moved on): every economic and every gate output is the same."""
    diff = compare_golden(committed, lambda: dict(baseline.outputs), ZERO)
    assert set(diff.differences) <= {"backtest.result_hash"}, diff.report()


def test_every_engine_derived_output_is_bit_identical_on_the_target(
    golden: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    """Tolerance 0: all outputs agree except the declared exclusion and the structural gate."""
    diff = compare_golden(golden, lambda: migrated, ZERO)
    assert set(diff.differences) == {"backtest.result_hash", *STRUCTURAL}
    assert (
        diff.differences["backtest.result_hash"][0] != diff.differences["backtest.result_hash"][1]
    )
    assert diff.differences["gate.G0.execution_model.value"] == (None, Decimal(1))
    assert diff.differences["gate.G0.execution_model.verdict"] == (None, Decimal(1))
    assert diff.differences["report.gates"] == (Decimal(37), Decimal(38))
    for key in ("backtest.final_equity", "backtest.total_fees", "backtest.total_slippage"):
        assert migrated[key] == golden.outputs[key]
    shared = set(golden.outputs) - {"backtest.result_hash", "report.gates"}
    assert all(migrated[key] == golden.outputs[key] for key in shared)
    # the 37 baseline gates keep their verdicts and values; only the new gate is added
    assert sorted(set(migrated) - set(golden.outputs)) == [
        "gate.G0.execution_model.value",
        "gate.G0.execution_model.verdict",
    ]


def test_the_declared_exclusion_alone_does_not_reach_tolerance_zero_against_undeclared(
    golden: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    """Against an *undeclared* source baseline, declaring the target adds the structural gate
    ``G0.execution_model`` (3 outputs, ``report.gates`` 37 -> 38): not an engine difference, but
    not like for like either, so the migration is compared against ``declared_baseline``."""
    diff = MIGRATION.compare(golden, lambda: migrated)
    assert diff.tolerance == ZERO
    assert not diff.passed and set(diff.differences) == STRUCTURAL


def test_the_declared_source_differs_from_the_committed_golden_only_by_the_declaration(
    committed: GoldenRecord, declared_baseline: GoldenRecord
) -> None:
    """Declaring the source engine to the validator only adds the structural G0 gate: every
    economic and every one of the 37 baseline gate outputs equals the committed golden."""
    diff = compare_golden(committed, lambda: dict(declared_baseline.outputs), ZERO)
    assert STRUCTURAL <= set(diff.differences) <= {"backtest.result_hash", *STRUCTURAL}


def test_the_migration_reaches_tolerance_zero_against_the_declared_source(
    declared_baseline: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    """ADR-0106 §2 (amended 2026-10-02): source and target both declared to the validator, only
    ``backtest.result_hash`` excluded, tolerance 0."""
    diff = MIGRATION.compare(declared_baseline, lambda: migrated)
    assert diff.tolerance == ZERO
    assert diff.passed and diff.differences == {}, diff.report()


def test_the_comparison_is_falsifiable_at_the_smallest_money_step(
    golden: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    """A target whose final equity is off by 1e-18 is reported at tolerance 0 (and tolerated
    only by a tolerance that covers it)."""

    class _Nudged(ReferenceBacktester):
        def run(self, request: BacktestRequest) -> BacktestResult:
            base = super().run(request)
            *head, last = base.equity_curve
            nudged = EquityPoint(
                time=last.time,
                cash=last.cash,
                equity=last.equity + Decimal("1e-18"),
                gross_exposure=last.gross_exposure,
            )
            return BacktestResult.build(
                request,
                self.descriptor,
                fills=base.fills,
                equity_curve=(*head, nudged),
                unexecuted_targets=base.unexecuted_targets,
            )

    perturbed = experiment.outputs(
        experiment.evaluate(backtester=_Nudged(), declare_backtester=True)
    )
    assert perturbed["backtest.final_equity"] - migrated["backtest.final_equity"] == Decimal(
        "1e-18"
    )
    diff = MIGRATION.compare(golden, lambda: perturbed)
    assert not diff.passed and "backtest.final_equity" in diff.differences
    assert diff.differences["backtest.final_equity"] == (
        golden.outputs["backtest.final_equity"],
        perturbed["backtest.final_equity"],
    )
    assert "backtest.result_hash" not in diff.differences  # excluded on both sides
    assert "backtest.final_equity" not in MIGRATION.compare(golden, lambda: migrated).differences


def test_a_target_that_never_sees_the_declaration_is_reported_too(golden: GoldenRecord) -> None:
    """Undeclared, the benchmark re-run cannot reproduce the target's hash: INCONCLUSIVE items,
    a different verdict and gate count — a much larger, and visible, difference."""
    undeclared = experiment.outputs(experiment.evaluate(backtester=ReferenceBacktester()))
    diff = MIGRATION.compare(golden, lambda: undeclared)
    assert not diff.passed
    assert {"report.verdict", "report.gates", "report.status"} <= set(diff.differences)


# --- exclusions are declared, never implicit ---------------------------------------------------


def test_the_exclusion_is_declared_on_the_target_and_nowhere_else(golden: GoldenRecord) -> None:
    assert MIGRATION.excluded_outputs == ("backtest.result_hash",)
    assert set(MIGRATION.project(golden.outputs)) == set(golden.outputs) - {"backtest.result_hash"}


# --- rollback ----------------------------------------------------------------------------------


def test_switching_back_to_the_source_restores_the_baseline_bit_exactly(
    baseline: GoldenRecord, migrated: dict[str, Decimal], rolled_back: dict[str, Decimal]
) -> None:
    golden = baseline
    migrated_diff = compare_golden(golden, lambda: migrated, MIGRATION.tolerance)
    back = compare_golden(golden, lambda: rolled_back, ZERO)
    assert back.passed and back.bit_identical and back.differences == {}
    evidence = rollback_evidence(MIGRATION.migration_id, golden, migrated_diff, back)
    assert evidence.verdict is RollbackVerdict.RESTORED
    assert evidence.residual_differences == ()
    assert not evidence.migrated_passed and evidence.rolled_back_hash == golden.outputs_hash


def test_a_rollback_that_does_not_restore_is_not_restored(
    baseline: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    """Staying on the target is not a rollback: its result_hash differs from the golden's."""
    golden = baseline
    diff = compare_golden(golden, lambda: migrated, ZERO)
    evidence = rollback_evidence(MIGRATION.migration_id, golden, diff, diff)
    assert evidence.verdict is RollbackVerdict.NOT_RESTORED
    assert "backtest.result_hash" in evidence.residual_differences


# --- the migration report ----------------------------------------------------------------------


def test_the_migration_report_is_written_once_and_read_back_verified(
    tmp_path: Path,
    baseline: GoldenRecord,
    migrated: dict[str, Decimal],
    rolled_back: Mapping[str, Decimal],
) -> None:
    golden = baseline
    conformance = (_conformance(SOURCE, BarBacktester), _conformance(TARGET, ReferenceBacktester))
    migrated_diff = compare_golden(golden, lambda: migrated, MIGRATION.tolerance)
    evidence = rollback_evidence(
        MIGRATION.migration_id,
        golden,
        migrated_diff,
        compare_golden(golden, lambda: rolled_back, ZERO),
    )
    report = MigrationReport(
        target=MIGRATION,
        golden_name=golden.name,
        golden_record_hash=golden.record_hash,
        conformance=conformance,
        golden_diff=MIGRATION.compare(golden, lambda: migrated),
        rollback=evidence,
    )
    # honest verdict: conformance passes and the rollback restores, the golden diff does not
    assert all(item.passed for item in report.conformance)
    assert report.rollback.verdict is RollbackVerdict.RESTORED
    assert not report.passed and report.verdict == "failed"
    assert report.failures == tuple(f"golden:{key}" for key in sorted(STRUCTURAL))
    path = save_migration_report(report, tmp_path)
    assert path.name == f"{report.report_hash}.json"
    assert save_migration_report(report, tmp_path) == path  # write-once: an identical re-save
    loaded = load_migration_report(tmp_path, report.report_hash)
    assert loaded == report and loaded.report_hash == report.report_hash
    assert loaded.render() == report.render() and "verdict: FAILED" in loaded.render()


def test_the_migration_report_passes_against_the_declared_source(
    tmp_path: Path, declared_baseline: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    golden = declared_baseline
    conformance = (_conformance(SOURCE, BarBacktester), _conformance(TARGET, ReferenceBacktester))
    migrated_diff = compare_golden(golden, lambda: migrated, MIGRATION.tolerance)
    rolled_back = experiment.outputs(
        experiment.evaluate(backtester=BarBacktester(), declare_backtester=True)
    )
    evidence = rollback_evidence(
        MIGRATION.migration_id,
        golden,
        migrated_diff,
        compare_golden(golden, lambda: rolled_back, ZERO),
    )
    report = MigrationReport(
        target=MIGRATION,
        golden_name=golden.name,
        golden_record_hash=golden.record_hash,
        conformance=conformance,
        golden_diff=MIGRATION.compare(golden, lambda: migrated),
        rollback=evidence,
    )
    assert report.rollback.verdict is RollbackVerdict.RESTORED
    assert report.passed and report.failures == ()
    path = save_migration_report(report, tmp_path)
    assert load_migration_report(tmp_path, report.report_hash) == report
    assert path.name == f"{report.report_hash}.json"
