"""Cross-sectional momentum (``xsmom_bars``): contract, ranking, causality, sources, validation.

Phase 5 (ADR-0038), research level; CODE_COMPLETE / DEBUG_PENDING. The strategy is ranked over the
request's instruments, so it is validated on the multi-instrument path (``ValidatorSetup
.instruments``, ``research/validation/instruments.py``) with the synthetic book of
``test_multi_instrument_validation``. The Profile / cost / robustness values there are the TEST ONLY
lax fixtures: the end-to-end test asserts the report and its bookkeeping, never a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.feature import ObservationScalar
from core.contracts.strategy import (
    SignalObservation,
    StrategyRequest,
    StrategyResult,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.research import Verdict, derive_verdict
from core.domain.specs import StrategySpec
from plugins.backtest import BarBacktester
from plugins.knowledge import LocalKnowledgeProvider
from plugins.outcomes import ForwardReturnOutcome
from research.strategies.cross_sectional_momentum import (
    XSMOM_KNOWLEDGE,
    CrossSectionalMomentumProvider,
    xsmom_spec,
)
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import (
    LibraryEntry,
    MissingKnowledgeSource,
    library_entries,
    resolve_knowledge,
)
from research.strategies.pipeline import (
    CandidateTrialRunner,
    EvaluationStatus,
    StrategyCandidate,
    evaluate_strategy,
)
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals
from research.strategies.validation import PipelineBacktestValidator, ValidatorSetup
from tests.contract_suites.strategy import StrategyProviderContract, StrategySubject
from tests.research.strategies import test_multi_instrument_validation as multi
from tests.research.synthetic_lab import gate_fixtures as lax
from tests.strategy_fixtures import MINUTE, T0, make_bars, wave_closes

INSTRUMENTS = ("ADAUSDT", "BTCUSDT", "ETHUSDT")
BARS = (
    make_bars("ADAUSDT", wave_closes(160, phase=27))
    + make_bars("BTCUSDT", wave_closes(160))
    + make_bars("ETHUSDT", wave_closes(160, phase=13))
)
SIGNALS = bar_signals(BARS)
DECISIONS = tuple(T0 + minute * MINUTE for minute in range(62, 160, 6))
CUTOFF = T0 + timedelta(days=1)
SMALL: Mapping[str, ObservationScalar] = {"lookback": 60}


def _request(
    params: Mapping[str, ObservationScalar] | None = None,
    *,
    spec: StrategySpec | None = None,
    signals: tuple[SignalObservation, ...] = SIGNALS,
    instruments: tuple[str, ...] = INSTRUMENTS,
    decisions: tuple[datetime, ...] = DECISIONS,
) -> StrategyRequest:
    spec = spec or xsmom_spec()
    return StrategyRequest(
        strategy=spec.ref,
        spec_hash=spec.content_hash(),
        params=FrozenMapping(params if params is not None else SMALL),
        instruments=instruments,
        knowledge_cutoff=CUTOFF,
        decision_times=decisions,
        signals=signals,
    )


def _perturb(item: SignalObservation) -> SignalObservation:
    value = item.value if isinstance(item.value, Decimal) else Decimal(0)
    return item.model_copy(update={"value": -value - Decimal("0.01")})


def _run(request: StrategyRequest) -> StrategyResult:
    provider = CrossSectionalMomentumProvider()
    result = provider.target_positions(request)
    result.check_answers(request, provider.descriptor)
    return result


class TestCrossSectionalMomentum(StrategyProviderContract):
    """check_descriptor / check_answers / determinism / causality / refusal."""

    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        return StrategySubject(
            open=CrossSectionalMomentumProvider,
            request=_request(),
            unsupported=_request({"lookback": 61}),
            perturb=_perturb,
        )


# --------------------------------------------------------------------------------------
# Ranking rule on hand-built signals
# --------------------------------------------------------------------------------------

HAND_T = T0 + 10 * MINUTE


def _obs(instrument: str, minute: int, value: Decimal | None) -> SignalObservation:
    at = T0 + minute * MINUTE
    return SignalObservation(
        signal=LOG_RETURN_SIGNAL,
        instrument=instrument,
        event_time=at,
        available_time=at,
        knowledge_time=at,
        value=value,
    )


def _hand(trailing: Mapping[str, tuple[str | None, str | None]]) -> tuple[SignalObservation, ...]:
    """Two log returns per instrument at minutes 1 and 2 (``None`` = not computable value)."""
    return tuple(
        _obs(name, minute, None if value is None else Decimal(value))
        for name, values in trailing.items()
        for minute, value in zip((1, 2), values, strict=True)
    )


def _hand_spec() -> StrategySpec:
    """``xsmom_bars`` with ``lookback`` 2 (every declared point outruns a hand fixture)."""
    spec = xsmom_spec()
    return StrategySpec.model_validate(
        {
            **spec.model_dump(),
            "name": "xsmom_bars_hand",
            "param_search_space": {**dict(spec.param_search_space), "lookback": [2]},
            "params": {**dict(spec.params), "lookback": 2},
        }
    )


def _weights(
    signals: tuple[SignalObservation, ...], **params: ObservationScalar
) -> dict[str, Decimal]:
    spec = _hand_spec()
    names = tuple(sorted({item.instrument for item in signals}))
    request = _request(params, spec=spec, signals=signals, instruments=names, decisions=(HAND_T,))
    provider = CrossSectionalMomentumProvider((spec,))
    result = provider.target_positions(request)
    result.check_answers(request, provider.descriptor)
    return {item.instrument: item.target_weight for item in result.positions}


FOUR = {
    "A": ("0.02", "0.01"),  # 0.03
    "B": ("-0.01", "-0.01"),  # -0.02
    "C": ("0.01", "0.00"),  # 0.01
    "D": ("-0.03", "0.00"),  # -0.03
}


def test_long_the_top_short_the_bottom_at_the_declared_gross() -> None:
    weights = _weights(_hand(FOUR))
    assert weights == {
        "A": Decimal("0.5"),
        "B": Decimal(0),
        "C": Decimal(0),
        "D": Decimal("-0.5"),
    }
    assert sum(abs(w) for w in weights.values()) == 1


def test_top_n_and_long_only() -> None:
    both = _weights(_hand(FOUR), top_n=2)
    assert both == {
        "A": Decimal("0.25"),
        "B": Decimal("-0.25"),
        "C": Decimal("0.25"),
        "D": Decimal("-0.25"),
    }
    assert _weights(_hand(FOUR), long_only=True) == {
        "A": Decimal(1),
        "B": Decimal(0),
        "C": Decimal(0),
        "D": Decimal(0),
    }
    longs = _weights(_hand(FOUR), long_only=True, top_n=2)
    assert longs == {"A": Decimal("0.5"), "B": Decimal(0), "C": Decimal("0.5"), "D": Decimal(0)}


def test_top_n_is_capped_by_half_the_cross_section() -> None:
    three = {k: v for k, v in FOUR.items() if k != "D"}
    # m = 3 → k = min(2, 1) = 1: the middle instrument stays flat.
    assert _weights(_hand(three), top_n=2) == {
        "A": Decimal("0.5"),
        "B": Decimal("-0.5"),
        "C": Decimal(0),
    }


def test_no_cross_section_or_no_dispersion_is_flat() -> None:
    lone = {"A": FOUR["A"], "B": (None, "0.01")}  # only A is computable
    assert set(_weights(_hand(lone)).values()) == {Decimal(0)}
    same = {"A": ("0.01", "0.01"), "B": ("0.02", "0.00")}
    assert set(_weights(_hand(same)).values()) == {Decimal(0)}


def test_ties_are_broken_by_instrument_name() -> None:
    tied = {"A": ("0.01", "0.00"), "B": ("0.01", "0.00"), "C": ("-0.01", "0.00")}
    assert _weights(_hand(tied)) == {"A": Decimal("0.5"), "B": Decimal(0), "C": Decimal("-0.5")}


def test_a_non_computable_instrument_uses_no_inputs() -> None:
    signals = _hand({**FOUR, "E": ("0.05", None)})
    small_request = _request(SMALL, signals=signals, instruments=("A", "B", "C", "D", "E"))
    result = _run(small_request)  # lookback 60: nothing is computable on two values
    assert all(item.inputs_used == 0 and item.target_weight == 0 for item in result.positions)
    weights = _weights(signals)
    assert weights["E"] == 0 and weights["A"] == Decimal("0.5")


def test_gross_and_inputs_on_the_fixture() -> None:
    result = _run(_request())
    traded = 0
    for t in DECISIONS:
        at = result.at(t)
        gross = sum(abs(item.target_weight) for item in at)
        assert gross in (Decimal(0), Decimal(1))
        assert sum(item.target_weight for item in at) == 0  # long-short is dollar neutral
        traded += gross > 0
        for item in at:
            if item.inputs_used:
                assert item.inputs_used == 60 * len(INSTRUMENTS)
    assert traded, "the fixture must rank something"
    thirds = _run(_request({"lookback": 60, "top_n": 1, "long_only": True}))
    assert {sum(abs(i.target_weight) for i in thirds.at(t)) for t in DECISIONS} <= {0, 1}


# --------------------------------------------------------------------------------------
# Causality and determinism
# --------------------------------------------------------------------------------------


def test_every_target_depends_only_on_what_was_visible() -> None:
    """Dropping every signal after ``t`` leaves the targets at ``t`` exactly as they were."""
    full = _run(_request())
    for t in DECISIONS:
        past = tuple(item for item in SIGNALS if item.available_time <= t)
        alone = _run(_request(signals=past, decisions=(t,)))
        assert [p.model_dump() for p in alone.positions] == [p.model_dump() for p in full.at(t)]


def test_future_perturbation_does_not_move_past_targets() -> None:
    cut = DECISIONS[len(DECISIONS) // 2]
    perturbed = tuple(_perturb(i) if i.available_time > cut else i for i in SIGNALS)
    base, moved = _run(_request()), _run(_request(signals=perturbed))
    assert [p for p in base.positions if p.decision_time <= cut] == [
        p for p in moved.positions if p.decision_time <= cut
    ]
    assert base.positions != moved.positions, "the perturbation must matter after the cut"


def test_deterministic_across_instances_and_points() -> None:
    for params in ({"lookback": 60}, {"lookback": 60, "top_n": 2, "long_only": True}):
        first = CrossSectionalMomentumProvider().target_positions(_request(params))
        second = CrossSectionalMomentumProvider().target_positions(_request(params))
        assert first.result_hash == second.result_hash


# --------------------------------------------------------------------------------------
# Parameter space and knowledge source
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"window": 60},
        {"lookback": 61},
        {"lookback": 60, "top_n": 3},
        {"lookback": 60, "top_n": True},
        {"lookback": 60, "long_only": 1},
        {"lookback": 60, "gross_exposure": 2},
        {"lookback": 60, "gross_exposure": Decimal(1)},
    ],
)
def test_undeclared_parameter_points_are_refused(params: dict[str, ObservationScalar]) -> None:
    with pytest.raises(UnsupportedStrategy):
        CrossSectionalMomentumProvider().target_positions(_request(params))


def test_another_spec_is_refused() -> None:
    other = StrategySpec.model_validate({**xsmom_spec().model_dump(), "version": "1.0.1"})
    with pytest.raises(UnsupportedStrategy):
        CrossSectionalMomentumProvider().target_positions(_request(spec=other))


def test_the_spec_declares_its_space_and_source() -> None:
    spec = xsmom_spec()
    assert spec.lineage == XSMOM_KNOWLEDGE
    assert set(spec.param_search_space) == {"lookback", "long_only", "top_n", "gross_exposure"}
    assert all(spec.params[k] in v for k, v in spec.param_search_space.items())
    (item,) = resolve_knowledge(spec.lineage, LocalKnowledgeProvider())
    assert item.ref.name == "factor_crypto_market_size_momentum"


def test_a_missing_source_is_refused() -> None:
    missing = Ref(kind=Kind.KNOWLEDGE, name="factor_crypto_not_in_the_base", version="1.0.0")
    with pytest.raises(MissingKnowledgeSource):
        resolve_knowledge((*XSMOM_KNOWLEDGE, missing), LocalKnowledgeProvider())


def _entry() -> LibraryEntry:
    (entry,) = [e for e in library_entries() if e.spec.ref == xsmom_spec().ref]
    return entry


def test_the_library_entry_has_its_own_family_and_provider() -> None:
    entry = _entry()
    assert entry.hypothesis_family_id == "xsmom_bars"
    others = {e.hypothesis_family_id for e in library_entries() if e.spec.ref != entry.spec.ref}
    assert entry.hypothesis_family_id not in others
    assert entry.sources == XSMOM_KNOWLEDGE and entry.risk_policy is None
    candidate = entry.candidate()
    assert isinstance(candidate.strategy, CrossSectionalMomentumProvider)
    assert candidate.strategy.descriptor.supports(entry.spec.ref, entry.spec.content_hash())
    # The existing entries keep their TSMOM provider and their positions in the library.
    assert [e.spec.name for e in library_entries()[:2]] == ["tsmom_bars", "tsmom_bars_vol_scaled"]


# --------------------------------------------------------------------------------------
# End to end: G0 – G4 on the multi-instrument path
# --------------------------------------------------------------------------------------

CHOSEN = {"lookback": 60}


def _setup(book: multi.Book, candidate: StrategyCandidate) -> ValidatorSetup:
    return ValidatorSetup(
        context=lax.context(candidate, lax.LAX_TEST_ONLY_PROFILE),
        outcome_provider=ForwardReturnOutcome((lax.LABEL_SPEC,)),
        manifest_content_hash=multi.MANIFEST,
        instrument=book.names[0],
        trials=CandidateTrialRunner(candidate, book.inputs(), BarBacktester()),
        chosen_params=CHOSEN,
        seed=11,
        robustness=multi.PARAMS,
        state_of=lambda t: "am" if t.hour < 12 else "pm",
        bar_volume={
            (name, bar.interval_start): bar.volume
            for name, market in book.markets.items()
            for bar in market.bars
        },
        declared_instruments=book.names,
        instruments=book.names,
        dataset_bars=book.proven(),
    )


@pytest.mark.parametrize("kinds", ["p,n", "p,p,n"])
def test_validated_end_to_end_on_the_multi_instrument_path(kinds: str, tmp_path: Path) -> None:
    book = multi.Book.of(kinds)
    entry = _entry()
    candidate = entry.candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        candidate,
        book.inputs(),
        backtester=BarBacktester(),
        registry=registry,
        validator=PipelineBacktestValidator(_setup(book, candidate)),
    )
    assert result.backtest is not None and result.backtest.fills, "the strategy must trade"
    assert result.validation is not None, (result.status, registry.records())
    report = result.validation.report
    # Bound to the spec, on the multi-instrument path, with the standard verdict rule.
    assert report.subject == entry.spec.ref
    assert report.verdict is derive_verdict(report.gates)
    gates = {g.gate_id: g for g in report.gates}
    assert gates["G0.instrument_scope"].verdict is Verdict.PASS
    assert gates["G0.manifest_binding"].verdict is Verdict.PASS
    assert "G0.single_instrument_adapter" not in gates
    records = registry.records()
    if report.verdict is Verdict.FAIL:
        assert result.status in (EvaluationStatus.REJECTED, EvaluationStatus.FAILED)
        (record,) = records
        failed = next(g for g in report.gates if g.verdict is Verdict.FAIL)
        assert record.subject_ref == entry.spec.ref and record.gate_id == failed.gate_id
        assert record.hypothesis_family_id == "xsmom_bars"
        assert f"validation_report:{report.report_id}" in record.evidence
        assert result.failure == record
    else:
        assert records == ()
        assert result.status is (
            EvaluationStatus.PASSED
            if report.verdict is Verdict.PASS
            else EvaluationStatus.INCONCLUSIVE
        )
    # Whatever the verdict, nothing here is promotable (no sealed-OOS evidence).
    assert result.promotion_blocked_reason is not None


def test_a_single_instrument_run_has_no_cross_section() -> None:
    """G4 cross-asset re-runs one instrument alone: a cross-sectional rule is flat there.

    The G4 input still builds (one per-asset series per declared instrument); those series are
    flat by construction and recorded as zero exposure. ``xsmom_bars`` is declared cross-sectional
    (ADR-0059), so C-R3 is judged over sub-universes instead; two instruments allow none
    (``test_cross_sectional_g4`` covers the verdicts).
    """
    book = multi.Book.of("p,n")
    candidate = _entry().candidate()
    runner = CandidateTrialRunner(candidate, book.inputs(), BarBacktester())
    for name in book.names:
        alone = runner.run(CHOSEN, instruments=(name,))
        assert {t.target_weight for t in alone.targets} == {Decimal(0)}
        assert not alone.backtest.fills
    backtest = runner.run(CHOSEN).backtest
    g4 = PipelineBacktestValidator(_setup(book, candidate)).robustness_input(
        candidate.spec, backtest
    )
    assert set(g4.per_asset) == set(book.names)
    assert g4.per_asset_exposed == dict.fromkeys(book.names, False)
    assert g4.sub_universes == ()
