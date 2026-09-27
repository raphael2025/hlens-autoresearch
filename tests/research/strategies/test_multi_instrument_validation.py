"""Multi-instrument validation through ``PipelineBacktestValidator`` (Phase 8 implementation note,
2026-09-26; CODE_COMPLETE / DEBUG_PENDING).

!!! TEST ONLY !!!  The Profile, cost model and robustness parameters are the deliberately lax,
**uncalibrated** TEST ONLY values of ``tests/research/synthetic_lab/gate_fixtures.py`` (plus an
explicit TEST ONLY ``cross_asset_min_positive_fraction``); they exercise the adapter and are never
research values.

The markets are ``RandomWalkMarket``\\ s, one per instrument: ``p`` has the planted 60-minute
return autocorrelation a 60-bar TSMOM captures, ``n`` is pure noise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.strategy import PriceBar
from core.contracts.synthetic import PlantedEffect, SyntheticMarket, SyntheticMarketSpec
from core.domain.research import Verdict, derive_verdict
from core.errors import ReasonCode
from infrastructure.bars import DatasetPriceBars
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome
from plugins.synthetic import RandomWalkMarket
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.strategies.pipeline import (
    CandidateTrialRunner,
    EvaluationInputs,
    EvaluationStatus,
    StrategyCandidate,
    StrategyEvaluation,
    evaluate_strategy,
)
from research.strategies.signals import bar_signals
from research.strategies.validation import (
    PipelineBacktestValidator,
    TrialRun,
    TrialRunner,
    ValidatorSetup,
    binding_mismatches,
)
from research.validation.instruments import INSTRUMENT_INFIX
from research.validation.returns import from_backtest
from tests.research.strategies import test_backtest_validation as single
from tests.research.synthetic_lab import gate_fixtures as lax

T0, HOUR, MINUTE = lax.T0, lax.HOUR, lax.MINUTE
BOUNDARY = lax.BOUNDARY
CHOSEN = lax.CHOSEN
#: TEST ONLY — the lax parameters with an explicit cross-asset fraction (otherwise the C-R3
#: consistency gate is ``profile_field_missing`` and nothing can PASS).
PARAMS = replace(lax.TEST_ONLY_PARAMS, cross_asset_min_positive_fraction=0.5)
MANIFEST = "7" * 64
PLANTED_SEED = 11  # S<i>-USDT planted with seed 11 + 10 * i; noise with seed 3 + i


def _market(seed: int, planted: bool) -> SyntheticMarket:
    effects = (PlantedEffect(lag_minutes=60, strength=Decimal("0.5")),) if planted else ()
    return RandomWalkMarket().generate(
        SyntheticMarketSpec(
            name="multi_instrument_market",
            version="1.0.0",
            symbol="SYN-USDT",
            start=T0,
            minutes=2 * 1440,
            seed=seed,
            initial_price=Decimal(100),
            volatility=Decimal("0.001"),
            effects=effects,
        )
    )


@dataclass(frozen=True)
class Book:
    """Markets by instrument (``"p,n"`` → ``S0-USDT`` planted, ``S1-USDT`` noise)."""

    markets: dict[str, SyntheticMarket]

    @classmethod
    def of(cls, kinds: str) -> Book:
        markets = {
            f"S{i}-USDT": _market(PLANTED_SEED + 10 * i if kind == "p" else 3 + i, kind == "p")
            for i, kind in enumerate(kinds.split(","))
        }
        return cls(markets)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.markets))

    def bars(self, *, with_volume: bool = False) -> tuple[PriceBar, ...]:
        """``with_volume``: as ``backtest_bars_from_dataset`` produces them since B61 (dataset path
        only; the synthetic path keeps its volume-less bars and hashes)."""
        return tuple(
            PriceBar(
                instrument=name,
                interval_start=bar.interval_start,
                interval_end=bar.interval_end,
                available_time=bar.interval_end,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume if with_volume else None,
            )
            for name in self.names
            for bar in self.markets[name].bars
            if bar.interval_end <= BOUNDARY
        )

    def inputs(self, *, with_volume: bool = False) -> EvaluationInputs:
        bars = self.bars(with_volume=with_volume)
        decisions: list[datetime] = []
        t = T0 + 61 * MINUTE
        while t + HOUR < BOUNDARY:
            decisions.append(t)
            t += HOUR
        return EvaluationInputs(
            instruments=self.names,
            bars=bars,
            decision_times=tuple(decisions),
            knowledge_cutoff=BOUNDARY,
            cost_model=lax.BACKTEST_COSTS,
            initial_equity=Decimal(1_000_000),
            signals=bar_signals(bars),
            params=CHOSEN,
        )

    def proven(self, manifest: str = MANIFEST) -> DatasetPriceBars:
        """Stands in for ``backtest_bars_from_dataset`` over every instrument's bars (each with
        its volume, as that producer gives them since B61)."""
        bars = self.bars(with_volume=True)
        return DatasetPriceBars(manifest, max(bar.available_time for bar in bars), bars)


def _candidate() -> StrategyCandidate:
    return library_entries()[0].candidate()


def _setup(book: Book, runner: TrialRunner | None = None, **fields: object) -> ValidatorSetup:
    candidate = _candidate()
    values: dict[str, object] = {
        "context": lax.context(candidate, lax.LAX_TEST_ONLY_PROFILE),
        "outcome_provider": ForwardReturnOutcome((lax.LABEL_SPEC,)),
        "manifest_content_hash": MANIFEST,
        "instrument": book.names[0],
        # the dataset path trades on the proven bars themselves (which carry volume since B61)
        "trials": runner
        or CandidateTrialRunner(
            candidate,
            book.inputs(with_volume=fields.get("dataset_bars") is not None),
            BarBacktester(),
        ),
        "chosen_params": CHOSEN,
        "seed": 11,
        "robustness": PARAMS,
        "state_of": lambda t: "am" if t.hour < 12 else "pm",
        "bar_volume": {
            (name, bar.interval_start): bar.volume
            for name, market in book.markets.items()
            for bar in market.bars
        },
        "declared_instruments": book.names,
        "instruments": book.names,
    }
    values.update(fields)
    return ValidatorSetup(**values)  # type: ignore[arg-type]


def _evaluate(
    book: Book, tmp_path: Path, runner: TrialRunner | None = None, **fields: object
) -> tuple[StrategyEvaluation, FailureRegistry]:
    candidate = _candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        candidate,
        book.inputs(with_volume=fields.get("dataset_bars") is not None),
        backtester=BarBacktester(),
        registry=registry,
        validator=PipelineBacktestValidator(_setup(book, runner, **fields)),
    )
    return result, registry


def _per_instrument(result: StrategyEvaluation) -> dict[str, object]:
    assert result.validation is not None and result.validation.view is not None
    extra = result.validation.view["extra"]
    assert isinstance(extra, dict)
    per = extra["per_instrument"]
    assert isinstance(per, dict)
    return per


def _instrument_gates(result: StrategyEvaluation, name: str) -> dict[str, Verdict]:
    assert result.validation is not None
    suffix = f"{INSTRUMENT_INFIX}{name}"
    return {
        g.gate_id.removesuffix(suffix): g.verdict
        for g in result.validation.report.gates
        if g.gate_id.endswith(suffix)
    }


# =========================================================================================
# single-instrument path: byte-identical (hashes pinned before the multi-instrument change)
# =========================================================================================

#: ``test_backtest_validation`` fixtures with ``created_at=T0``, computed on 1fb7918 before this
#: change: the single-instrument path must reproduce them exactly. Re-pinned for ADR-0060
#: enforcement (2026-09-26): the fixture Profile's ``benchmark`` block now names
#: ``buy_and_hold_equal_weight`` + inverse control instead of the placeholder ``"test-only"``
#: (the report binds the Profile); the validator path is unchanged (no opt-in here).
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): 6dd0103a…, fcfb36eb…
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0
#: values still hold when the test builds every object at 2.1.0 (verified: the unmodified
#: test passes inside ``contract_schema_version_scope("2.1.0")``).
#: 2.1.0 values (evidence, git history): d5ca922f…, de88c4e2…
PINNED_SINGLE_REPORT_HASHES = {
    "planted_synthetic": "f46de6b1b9c047e5743ff9d5676f3e640aea7f2f47b05f00092d7e43acdbbd76",
    "manifest_mismatch": "f04c916426661ffaae886914f00e49cb93f1c6fffdb9645dc5d0d489bdf4e751",
}


def test_the_single_instrument_path_is_byte_identical(tmp_path: Path) -> None:
    market = single._market(seed=7, planted=True)
    ctx = replace(single._context(_candidate()), created_at=single.T0)
    planted, _ = single._evaluate(market, tmp_path / "a", context=ctx)
    mismatch, _ = single._evaluate(
        market, tmp_path / "b", context=ctx, dataset_bars=single._proven(market, "8" * 64)
    )
    assert planted.validation is not None and mismatch.validation is not None
    hashes = {
        "planted_synthetic": planted.validation.report.content_hash(),
        "manifest_mismatch": mismatch.validation.report.content_hash(),
    }
    assert hashes == PINNED_SINGLE_REPORT_HASHES
    gates = {g.gate_id for g in planted.validation.report.gates}
    assert "G0.single_instrument_adapter" in gates
    assert (
        not any(INSTRUMENT_INFIX in gate for gate in gates) and "G0.instrument_scope" not in gates
    )
    extra = single._extra(planted)
    assert "instruments" not in extra and "per_instrument" not in extra


# =========================================================================================
# setup
# =========================================================================================


def test_a_multi_instrument_setup_is_checked() -> None:
    book = Book.of("p,p")
    with pytest.raises(ValueError, match="at least two"):
        _setup(book, instruments=("S0-USDT",))
    with pytest.raises(ValueError, match="at least two"):
        _setup(book, instruments=("S0-USDT", "S0-USDT"))
    with pytest.raises(ValueError, match="is not one of"):
        _setup(book, instrument="OTHER-USDT")
    with pytest.raises(ValueError, match="without"):
        _setup(book, instrument="A|B", instruments=("A|B", "S1-USDT"))
    assert _setup(book, instruments=("S1-USDT", "S0-USDT")).validated_instruments == book.names
    assert _setup(book, instruments=None).validated_instruments == ("S0-USDT",)


# =========================================================================================
# positive / negative end to end
# =========================================================================================


@pytest.fixture(scope="module")
def good_pair(tmp_path_factory: pytest.TempPathFactory) -> StrategyEvaluation:
    result, _ = _evaluate(Book.of("p,p"), tmp_path_factory.mktemp("good_pair"))
    return result


def test_multi_instrument_passes_only_when_every_instrument_supports_it(
    good_pair: StrategyEvaluation,
) -> None:
    assert good_pair.validation is not None
    report = good_pair.validation.report
    assert report.verdict is Verdict.PASS, [
        (g.gate_id, g.verdict) for g in report.gates if g.verdict is not Verdict.PASS
    ]
    assert good_pair.status is EvaluationStatus.PASSED
    by_id = {g.gate_id: g for g in report.gates}
    assert (by_id["G0.instrument_scope"].verdict, by_id["G0.instrument_scope"].value) == (
        Verdict.PASS,
        2.0,
    )
    assert "G0.single_instrument_adapter" not in by_id
    # Pooled statistics under the standard ids, and each instrument's own G0 – G3.
    for name in ("S0-USDT", "S1-USDT"):
        mine = _instrument_gates(good_pair, name)
        assert {"G0.data_available", "G2.effective_sample_size", "G3.adjusted_p_value"} <= set(mine)
        assert set(mine.values()) == {Verdict.PASS}
    assert by_id["G3.adjusted_p_value"].verdict is Verdict.PASS
    assert _per_instrument(good_pair) == {
        "S0-USDT": {"labels": 46, "verdict": "PASS"},
        "S1-USDT": {"labels": 46, "verdict": "PASS"},
    }
    # The pooled table holds both instruments' labels, keyed by instrument.
    assert by_id["G0.data_available"].value == 92.0
    assert any(g.startswith("G4.cross_asset") for g in by_id)
    assert good_pair.promotion_blocked_reason == "sealed_oos_not_evaluated"


def test_one_bad_instrument_is_never_a_pass(tmp_path: Path) -> None:
    """Three planted instruments carry the pooled statistics; the noise one fails on its own."""
    result, registry = _evaluate(Book.of("p,p,p,n"), tmp_path)
    assert result.validation is not None
    report = result.validation.report
    assert report.verdict is Verdict.FAIL
    assert report.verdict is derive_verdict(report.gates)
    pooled = {g.gate_id: g.verdict for g in report.gates if INSTRUMENT_INFIX not in g.gate_id}
    assert Verdict.FAIL not in pooled.values()  # the pooled evidence alone would not refute it
    assert Verdict.FAIL in _instrument_gates(result, "S3-USDT").values()
    for name in ("S0-USDT", "S1-USDT", "S2-USDT"):
        assert set(_instrument_gates(result, name).values()) == {Verdict.PASS}
    per = _per_instrument(result)
    assert [per[name]["verdict"] for name in sorted(per)] == ["PASS", "PASS", "PASS", "FAIL"]  # type: ignore[index]
    # G4 does not run after a FAIL; the failure is filed under the instrument's gate.
    assert not any(g.gate_id.startswith("G4.") for g in report.gates)
    (record,) = registry.records()
    failed = next(g for g in report.gates if g.verdict is Verdict.FAIL)
    assert record.gate_id == failed.gate_id and failed.gate_id.endswith(".instrument.S3-USDT")
    assert record.reason_code is ReasonCode.COST_KILLED


class _FlatOn:
    """Stands in for a runner whose candidate never trades one instrument (flat targets)."""

    def __init__(self, inner: TrialRunner, flat: str) -> None:
        self._inner, self._flat = inner, flat

    def run(self, params, **kwargs) -> TrialRun:  # type: ignore[no-untyped-def]
        run = self._inner.run(params, **kwargs)
        targets = tuple(
            t.model_copy(update={"target_weight": Decimal(0)}) if t.instrument == self._flat else t
            for t in run.targets
        )
        return replace(run, targets=targets)


def test_an_instrument_without_labels_is_inconclusive(tmp_path: Path) -> None:
    book = Book.of("p,p")
    inner = CandidateTrialRunner(_candidate(), book.inputs(), BarBacktester())
    result, _ = _evaluate(book, tmp_path, runner=_FlatOn(inner, "S1-USDT"))
    assert result.validation is not None
    report = result.validation.report
    assert report.verdict is not Verdict.PASS
    gap = next(g for g in report.gates if g.gate_id == "G0.data_available.instrument.S1-USDT")
    assert (gap.verdict, gap.metric) == (Verdict.INCONCLUSIVE, "instrument_labels")
    assert _per_instrument(result)["S1-USDT"] == {"labels": 0, "verdict": "INCONCLUSIVE"}


def test_a_trade_outside_the_validated_scope_is_inconclusive(tmp_path: Path) -> None:
    book = Book.of("p,p,p")
    scope = ("S0-USDT", "S1-USDT")  # the runner trades S2-USDT as well
    result, _ = _evaluate(book, tmp_path, instruments=scope, declared_instruments=scope)
    assert result.validation is not None
    report = result.validation.report
    scope_gate = next(g for g in report.gates if g.gate_id == "G0.instrument_scope")
    assert (scope_gate.verdict, scope_gate.metric, scope_gate.value) == (
        Verdict.INCONCLUSIVE,
        "instruments_outside_scope",
        1.0,
    )
    assert report.verdict is Verdict.INCONCLUSIVE
    assert {g.gate_id.split(".")[0] for g in report.gates} == {"G0"}  # no label was computed


# =========================================================================================
# price-bar binding per instrument
# =========================================================================================


def test_a_matching_dataset_binds_every_instrument(
    good_pair: StrategyEvaluation, tmp_path: Path
) -> None:
    book = Book.of("p,p")
    result, _ = _evaluate(book, tmp_path, dataset_bars=book.proven())
    assert result.validation is not None and good_pair.validation is not None
    gates = result.validation.report.gates
    binding = next(g for g in gates if g.gate_id == "G0.manifest_binding")
    assert (binding.verdict, binding.value) == (Verdict.PASS, 0.0)
    assert tuple(g for g in gates if g is not binding) == good_pair.validation.report.gates


def test_a_binding_mismatch_on_one_instrument_fails_g0(tmp_path: Path) -> None:
    book = Book.of("p,p")
    proven = book.proven()
    # One S1-USDT bar of the backtest is not the manifest's (the manifest holds another price).
    index = next(i for i, bar in enumerate(proven.bars) if bar.instrument == "S1-USDT")
    bar = proven.bars[index]
    moved = bar.model_copy(
        update={k: getattr(bar, k) + 1 for k in ("open", "high", "low", "close")}
    )
    tampered = DatasetPriceBars(
        MANIFEST, proven.price_cutoff, (*proven.bars[:index], moved, *proven.bars[index + 1 :])
    )
    result, registry = _evaluate(book, tmp_path, dataset_bars=tampered)
    assert result.status is EvaluationStatus.REJECTED
    assert result.validation is not None
    report = result.validation.report
    binding = next(g for g in report.gates if g.gate_id == "G0.manifest_binding")
    assert (binding.verdict, binding.value) == (Verdict.FAIL, 2.0)
    assert {g.gate_id.split(".")[0] for g in report.gates} == {"G0"}
    (record,) = registry.records()
    assert (record.gate_id, record.reason_code) == (
        "G0.manifest_binding",
        ReasonCode.CONTRACT_VIOLATION,
    )
    assert binding_mismatches(_setup(book, dataset_bars=tampered), book.bars(with_volume=True)) == [
        "bars_in_manifest",
        "bars_in_manifest[S1-USDT]",
    ]


def test_every_instrument_needs_manifest_bars() -> None:
    book = Book.of("p,p")
    only_first = DatasetPriceBars(
        MANIFEST,
        book.proven().price_cutoff,
        tuple(bar for bar in book.bars() if bar.instrument == "S0-USDT"),
    )
    setup = _setup(book, dataset_bars=only_first)
    first_bars = tuple(bar for bar in book.bars() if bar.instrument == "S0-USDT")
    assert binding_mismatches(setup, first_bars) == ["instrument_bars[S1-USDT]"]
    assert binding_mismatches(setup, book.bars()) == [
        "bars_in_manifest",
        "bars_in_manifest[S1-USDT]",
    ]
    early = DatasetPriceBars(MANIFEST, BOUNDARY - timedelta(hours=1), book.bars())
    assert binding_mismatches(_setup(book, dataset_bars=early), book.bars()) == [
        "price_cutoff",
        "price_cutoff[S0-USDT]",
        "price_cutoff[S1-USDT]",
    ]


# =========================================================================================
# trial count and G4 cross-asset
# =========================================================================================


class _Counting:
    def __init__(self, inner: TrialRunner) -> None:
        self.inner = inner
        self.calls: list[tuple[str, ...] | None] = []

    def run(self, params, **kwargs) -> TrialRun:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs.get("instruments"))
        return self.inner.run(params, **kwargs)


def _grid_size(candidate: StrategyCandidate) -> int:
    size = 1
    for values in candidate.spec.param_search_space.values():
        size *= len(values)
    return size


def test_instruments_add_no_trials(good_pair: StrategyEvaluation) -> None:
    """Per-instrument evidence re-uses the one re-run: no extra trial, same family count."""
    book = Book.of("p,p")
    candidate = _candidate()
    counting = _Counting(CandidateTrialRunner(candidate, book.inputs(), BarBacktester()))
    setup = _setup(book, counting)
    backtest = CandidateTrialRunner(candidate, book.inputs(), BarBacktester()).run(CHOSEN)
    validation = PipelineBacktestValidator(setup).validate(
        candidate.spec.ref, candidate.spec, backtest.backtest
    )
    assert good_pair.validation is not None
    assert validation.report.gates == good_pair.validation.report.gates
    grid = _grid_size(candidate)
    # re-run + the other grid points + delay + one time offset + one run per declared asset
    assert len(counting.calls) == 1 + (grid - 1) + 1 + 1 + len(book.names)
    assert [c for c in counting.calls if c is not None] == [("S0-USDT",), ("S1-USDT",)]
    g4 = PipelineBacktestValidator(setup).robustness_input(candidate.spec, backtest.backtest)
    assert g4.family_trial_count == setup.context.metadata.family_trial_count == grid


def test_g4_cross_asset_uses_the_real_per_instrument_runs() -> None:
    book = Book.of("p,p")
    candidate = _candidate()
    runner = CandidateTrialRunner(candidate, book.inputs(), BarBacktester())
    backtest = runner.run(CHOSEN).backtest
    # Even when the declared scope names only one asset, a multi-instrument base run is never
    # used as that asset's returns.
    for declared in (book.names, ("S0-USDT",)):
        setup = _setup(book, runner, declared_instruments=declared)
        g4 = PipelineBacktestValidator(setup).robustness_input(candidate.spec, backtest)
        assert set(g4.per_asset) == set(declared)
        for name in declared:
            alone = from_backtest(runner.run(CHOSEN, instruments=(name,)).backtest)
            assert g4.per_asset[name] == alone
            assert g4.per_asset[name] != from_backtest(backtest)
