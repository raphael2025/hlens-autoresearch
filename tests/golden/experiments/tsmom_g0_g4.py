"""Golden experiment: synthetic 60-bar TSMOM through backtest and the full G0 – G4 validation
(Phase 14 golden rerun; ``infrastructure.migration.golden``).

!!! TEST ONLY !!!  The run is ``tests/research/synthetic_lab/gate_fixtures.py``'s detector setup:
a two-day ``RandomWalkMarket`` research window (seed ``SEED``, the planted 60-minute effect
``STRONG``), the first library entry (TSMOM, ``lookback = 60``), ``BarBacktester``, and
``PipelineBacktestValidator`` under the deliberately lax, **uncalibrated**
``LAX_TEST_ONLY_PROFILE`` with ``TEST_ONLY_PARAMS``. It freezes what the current engines compute;
it is not a research result, a calibration or a Profile proposal (Profile numbers remain TBD).
Since ADR-0060 enforcement (2026-09-26) the run sets ``market_benchmark=True`` like the lab
detector, and the Profile names ``buy_and_hold_equal_weight`` with the inverse control reported, so
the record includes the reported-only ``G2.market_benchmark.*`` / ``G2.inverse_control`` items
(regenerated).

Outputs (all finite ``Decimal``; floats as ``Decimal(repr(value))``, exact round-trip):

- ``backtest.*``: initial / final equity, total fees and slippage, fill and unexecuted-target
  counts, and ``result_hash`` (the content hash of the whole ``BacktestResult``, as an integer);
- ``report.*``: the overall verdict (``PASS`` 1 / ``INCONCLUSIVE`` 0 / ``FAIL`` -1), the gate count
  and the evaluation status code;
- ``gate.<gate_id>.value`` / ``.threshold`` (when the gate has one) / ``.verdict`` for every gate.

Regenerate (after a deliberate, ADR-backed engine change only)::

    uv run python -m tests.golden.experiments.tsmom_g0_g4            # writes into this directory
    uv run python -m tests.golden.experiments.tsmom_g0_g4 --out DIR  # writes into DIR

``save_golden`` is write-once and content-addressed (``<record_hash>.json``); a changed run gets
a new file, and ``tests/infrastructure/migration/test_golden_experiments.py`` pins the hash.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

from core.contracts.strategy import BacktestProvider
from core.contracts.synthetic import PlantedEffect
from core.domain.research import Verdict
from infrastructure.migration import GoldenRecord, record_golden, save_golden
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome
from plugins.synthetic import RandomWalkMarket
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import (
    CandidateTrialRunner,
    EvaluationStatus,
    StrategyEvaluation,
    evaluate_strategy,
)
from research.strategies.validation import PipelineBacktestValidator, ValidatorSetup
from tests.research.synthetic_lab import gate_fixtures as gf

NAME: Final = "synthetic_tsmom60_g0_g4_lax_test_only_seed100"
DIRECTORY: Final = Path(__file__).resolve().parent
SEED: Final = 100
EFFECTS: Final[tuple[PlantedEffect, ...]] = (gf.STRONG,)
VERDICT_CODE: Final = {Verdict.PASS: 1, Verdict.INCONCLUSIVE: 0, Verdict.FAIL: -1}
STATUS_CODE: Final = {
    EvaluationStatus.NOT_VALIDATED: 0,
    EvaluationStatus.PASSED: 1,
    EvaluationStatus.INCONCLUSIVE: 2,
    EvaluationStatus.REJECTED: 3,
    EvaluationStatus.FAILED: 4,
}


def _decimal(value: float) -> Decimal:
    exact = Decimal(repr(value))
    if not exact.is_finite():
        raise ValueError(f"a golden output must be finite, got {value!r}")
    return exact


def evaluate(
    seed: int = SEED,
    effects: tuple[PlantedEffect, ...] = EFFECTS,
    backtester: BacktestProvider | None = None,
    *,
    declare_backtester: bool = False,
) -> StrategyEvaluation:
    """The frozen pipeline: market → TSMOM → backtester → G0 – G4 validation.

    ``backtester=None`` (the default) is ``BarBacktester()`` and builds exactly the frozen setup.
    A migration drill injects another ``BacktestProvider`` (ADR-0106), used for the trials and the
    evaluation; ``declare_backtester=True`` also declares it to the validator
    (``ValidatorSetup.backtester``), which makes G0 add its structural ``G0.execution_model`` gate
    and lets the ADR-0060 benchmark re-run under it (undeclared, the validator re-runs a plain
    ``BarBacktester()`` and reports the benchmark items as not reproduced).
    """
    engine: BacktestProvider = BarBacktester() if backtester is None else backtester
    market = RandomWalkMarket().generate(
        gf.BASE_SPEC.model_copy(update={"seed": seed, "effects": effects})
    )
    candidate = gf.candidate()
    inputs = gf.inputs(market)
    setup = ValidatorSetup(
        context=gf.context(candidate, gf.LAX_TEST_ONLY_PROFILE),
        outcome_provider=ForwardReturnOutcome((gf.LABEL_SPEC,)),
        manifest_content_hash="7" * 64,
        instrument=gf.SYMBOL,
        trials=CandidateTrialRunner(candidate, inputs, engine),
        chosen_params=gf.CHOSEN,
        seed=11,
        robustness=gf.TEST_ONLY_PARAMS,
        state_of=lambda t: "am" if t.hour < 12 else "pm",
        bar_volume={(gf.SYMBOL, bar.interval_start): bar.volume for bar in market.bars},
        declared_instruments=(gf.SYMBOL,),
        market_benchmark=True,  # ADR-0060 enforced, as in the lab detector this run mirrors
        backtester=engine if declare_backtester else None,
    )
    with tempfile.TemporaryDirectory() as scratch:  # the failure registry is not an output
        return evaluate_strategy(
            candidate,
            inputs,
            backtester=engine,
            registry=FailureRegistry(Path(scratch) / "failures.jsonl"),
            validator=PipelineBacktestValidator(setup),
        )


def outputs(evaluation: StrategyEvaluation) -> dict[str, Decimal]:
    backtest, validation = evaluation.backtest, evaluation.validation
    if backtest is None or validation is None:
        raise ValueError("the golden run must backtest and validate")
    report = validation.report
    values: dict[str, Decimal] = {
        "backtest.initial_equity": backtest.initial_equity,
        "backtest.final_equity": backtest.final_equity,
        "backtest.total_fees": backtest.total_fees,
        "backtest.total_slippage": backtest.total_slippage,
        "backtest.fills": Decimal(len(backtest.fills)),
        "backtest.unexecuted_targets": Decimal(backtest.unexecuted_targets),
        "backtest.result_hash": Decimal(int(backtest.result_hash, 16)),
        "report.verdict": Decimal(VERDICT_CODE[report.verdict]),
        "report.gates": Decimal(len(report.gates)),
        "report.status": Decimal(STATUS_CODE[evaluation.status]),
    }
    for gate in report.gates:
        key = f"gate.{gate.gate_id}"
        if f"{key}.value" in values:
            raise ValueError(f"duplicate gate id {gate.gate_id!r}")
        values[f"{key}.value"] = _decimal(gate.value)
        if gate.threshold is not None:
            values[f"{key}.threshold"] = _decimal(gate.threshold)
        values[f"{key}.verdict"] = Decimal(VERDICT_CODE[gate.verdict])
    return values


def run() -> dict[str, Decimal]:
    """The golden run's outputs (what ``record_golden`` / ``compare_golden`` call)."""
    return outputs(evaluate())


def record() -> GoldenRecord:
    return record_golden(NAME, run)


def main(argv: Sequence[str]) -> int:
    directory = DIRECTORY
    if list(argv[:1]) == ["--out"] and len(argv) == 2:
        directory = Path(argv[1])
    elif argv:
        print("usage: python -m tests.golden.experiments.tsmom_g0_g4 [--out DIR]", file=sys.stderr)
        return 2
    golden = record()
    path = save_golden(golden, directory)
    print(f"{golden.record_hash} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
