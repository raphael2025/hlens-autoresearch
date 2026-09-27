"""Phase 4: the minimal Validation Pipeline (G0 – G3 + G5) on seeded synthetic markets.

Uses the clearly marked TEST ONLY profile (``fixtures.TEST_ONLY_PROFILE``). The acceptance items
proven here: leakage detection works (shuffle / shift negative controls catch a study that looks
at the outcome), Outcome refs as inputs are refused, the cost model is always applied, every
threshold comes from the Profile with its source, and the verdict is ``derive_verdict``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from core.contracts.synthetic import SyntheticMarket
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Kind, Ref
from core.domain.research import Verdict, derive_verdict
from core.errors import ReasonCode
from research.outcomes import OutcomeTable
from research.validation import (
    SealedOosInput,
    build_report,
    failure_record,
    run_in_sample,
    run_sealed_oos,
)
from research.validation.calibration import MomentumSignStudy
from research.validation.gates import profile_value
from research.validation.sealed_oos import InMemoryUnsealingLedger, SealedOosVault
from research.validation.stats import UnsupportedMethod
from tests import factories
from tests.research.validation.fixtures import (
    BOUNDARY,
    LABEL_SPEC,
    MINUTE,
    TEST_ONLY_PROFILE,
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
    sealed_events,
)


class LeakyStudy:
    """Trades in the direction of the (supposedly unknown) outcome: must be caught by G1."""

    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return (Ref(kind=Kind.FEATURE, name="innocent_looking", version="1.0.0"),)

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        return tuple((value > 0) - (value < 0) for value in label_values)


class OutcomeFedStudy(MomentumSignStudy):
    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return (Ref(kind=Kind.OUTCOME, name="fwd_2m", version="1.0.0"),)


@pytest.fixture(scope="module")
def planted() -> tuple[SyntheticMarket, OutcomeTable]:
    market = generate(seed=3, strength="0.6")
    return market, outcome_table(market, research_events(market))


@pytest.fixture(scope="module")
def noise() -> tuple[SyntheticMarket, OutcomeTable]:
    market = generate(seed=5)
    return market, outcome_table(market, research_events(market))


def _gates(market: SyntheticMarket, table: OutcomeTable, **kwargs: object) -> dict[str, object]:
    study = kwargs.pop("study", None) or MomentumSignStudy(market, _events(table))
    gates = run_in_sample(in_sample_input(table, study, **kwargs))  # type: ignore[arg-type]
    return {gate.gate_id: gate for gate in gates}


def _events(table: OutcomeTable):  # type: ignore[no-untyped-def]
    from core.contracts.outcome import OutcomeEvent

    return [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]


def test_a_planted_effect_passes_g0_to_g3(planted: tuple[SyntheticMarket, OutcomeTable]) -> None:
    market, table = planted
    study = MomentumSignStudy(market, _events(table))
    gates = run_in_sample(in_sample_input(table, study))
    ids = [gate.gate_id for gate in gates]
    assert ids[0].startswith("G0") and ids[-1] == "G3.adjusted_p_value"
    failing = [(g.gate_id, g.verdict) for g in gates if g.verdict is not Verdict.PASS]
    assert not failing, failing
    report = build_report(context(), gates)
    assert report.verdict is Verdict.PASS is derive_verdict(report.gates)
    assert failure_record(report, "family-1") is None


def test_pure_noise_does_not_pass(noise: tuple[SyntheticMarket, OutcomeTable]) -> None:
    market, table = noise
    gates = run_in_sample(in_sample_input(table, MomentumSignStudy(market, _events(table))))
    report = build_report(context(), gates)
    assert report.verdict is not Verdict.PASS
    record = failure_record(report, "family-1")
    if record is not None:
        assert record.reason_code in {
            ReasonCode.COST_KILLED,
            ReasonCode.BENCHMARK_NOT_BEATEN,
            ReasonCode.NOT_SIGNIFICANT_AFTER_MTC,
        }


def test_shuffle_and_shift_controls_detect_a_leaky_study(
    noise: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = noise
    gates = _gates(market, table, study=LeakyStudy())
    assert gates["G1.shuffle_control"].verdict is Verdict.FAIL  # type: ignore[attr-defined]
    assert gates["G1.shift_control"].verdict is Verdict.FAIL  # type: ignore[attr-defined]
    assert not any(key.startswith("G2") for key in gates), "a failed stage stops the pipeline"
    report = build_report(context(), tuple(gates.values()))  # type: ignore[arg-type]
    record = failure_record(report, "family-1")
    assert record is not None and record.reason_code is ReasonCode.LEAKAGE_DETECTED


def test_honest_study_passes_the_negative_controls(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    gates = _gates(market, table)
    for gate_id in ("G1.shuffle_control", "G1.shift_control"):
        gate = gates[gate_id]
        assert gate.verdict is Verdict.PASS  # type: ignore[attr-defined]
        assert gate.threshold_source == "significance.multiple_testing_threshold"  # type: ignore[attr-defined]


def test_outcome_refs_as_inputs_fail_g1(noise: tuple[SyntheticMarket, OutcomeTable]) -> None:
    market, table = noise
    gates = _gates(market, table, study=OutcomeFedStudy(market, _events(table)))
    assert gates["G1.outcome_not_input"].verdict is Verdict.FAIL  # type: ignore[attr-defined]
    report = build_report(context(), tuple(gates.values()))  # type: ignore[arg-type]
    record = failure_record(report, "family-1")
    assert record is not None and record.reason_code is ReasonCode.OUTCOME_USED_AS_INPUT


def test_a_non_reproducible_run_fails_g0_and_stops(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    gates = _gates(market, table, reproduced_hash="0" * 64)
    assert gates["G0.reproducibility"].verdict is Verdict.FAIL  # type: ignore[attr-defined]
    assert all(key.startswith("G0") for key in gates)
    report = build_report(context(), tuple(gates.values()))  # type: ignore[arg-type]
    record = failure_record(report, "family-1")
    assert record is not None
    assert (record.terminal_state, record.reason_code) == ("FAILED", ReasonCode.NOT_REPRODUCIBLE)


def test_mismatched_bindings_fail_g0(planted: tuple[SyntheticMarket, OutcomeTable]) -> None:
    market, table = planted
    other_cost = factories.cost_model_ref(name="cost_other")
    profile = TEST_ONLY_PROFILE.model_copy(
        update={
            "cost_stress": TEST_ONLY_PROFILE.cost_stress.model_copy(
                update={"cost_model": other_cost}
            )
        }
    )
    gates = _gates(market, table, ctx=context(profile=profile))
    gate = gates["G0.bindings"]
    assert gate.verdict is Verdict.FAIL and gate.value == 1.0  # type: ignore[attr-defined]


def test_labels_reaching_the_sealed_window_fail_g1() -> None:
    market = generate(seed=5)
    events = research_events(market) + sealed_events(market)[:5]
    table = outcome_table(market, events)
    gates = _gates(market, table)
    gate = gates["G1.sealed_oos_excluded"]
    assert gate.verdict is Verdict.FAIL and gate.value == 5.0  # type: ignore[attr-defined]


def _with_embargo(embargo: timedelta) -> ValidationProfile:
    return TEST_ONLY_PROFILE.model_copy(
        update={"data_split": TEST_ONLY_PROFILE.data_split.model_copy(update={"embargo": embargo})}
    )


def test_an_embargo_shorter_than_the_label_horizon_fails_g1_and_stops(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    # C-L5: the embargo must cover the label horizon. This covers the check against the bound
    # label spec only; the cross-object check point (D-30) is still an open decision.
    market, table = planted
    ctx = context(profile=_with_embargo(LABEL_SPEC.horizon - MINUTE))
    gates = _gates(market, table, ctx=ctx)
    gate = gates["G1.embargo_covers_horizon"]
    assert gate.verdict is Verdict.FAIL  # type: ignore[attr-defined]
    failing = [key for key, item in gates.items() if item.verdict is Verdict.FAIL]  # type: ignore[attr-defined]
    assert failing == ["G1.embargo_covers_horizon"]  # the embargo alone causes the FAIL
    assert gate.value == -MINUTE.total_seconds()  # type: ignore[attr-defined]
    # A Constitution rule, not a Profile number: no threshold is read.
    assert gate.threshold is None  # type: ignore[attr-defined]
    assert not [key for key in gates if key.startswith(("G2", "G3"))]
    report = build_report(ctx, tuple(gates.values()))  # type: ignore[arg-type]
    record = failure_record(report, "family-1")
    assert record is not None
    assert (record.gate_id, record.terminal_state, record.reason_code) == (
        "G1.embargo_covers_horizon",
        "REJECTED",
        ReasonCode.LEAKAGE_DETECTED,
    )


def test_an_embargo_equal_to_the_label_horizon_passes_the_check(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    gates = _gates(market, table, ctx=context(profile=_with_embargo(LABEL_SPEC.horizon)))
    gate = gates["G1.embargo_covers_horizon"]
    assert gate.verdict is Verdict.PASS and gate.value == 0.0  # type: ignore[attr-defined]


def test_every_threshold_is_read_from_the_profile(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    gates = run_in_sample(in_sample_input(table, MomentumSignStudy(market, _events(table))))
    with_threshold = [gate for gate in gates if gate.threshold is not None]
    assert len(with_threshold) >= 6
    for gate in with_threshold:
        assert gate.threshold_source is not None
        assert float(profile_value(TEST_ONLY_PROFILE, gate.threshold_source)) == gate.threshold  # type: ignore[arg-type]
        assert gate.metric.endswith(("[>=]", "[<=]"))


def test_a_stricter_profile_changes_the_verdict_without_code_changes(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    strict = TEST_ONLY_PROFILE.model_copy(
        update={
            "cost_stress": TEST_ONLY_PROFILE.cost_stress.model_copy(
                update={"min_breakeven_cost_multiple": 1000.0}
            )
        }
    )
    gates = _gates(market, table, ctx=context(profile=strict))
    gate = gates["G2.breakeven_cost_multiple"]
    assert gate.verdict is Verdict.FAIL and gate.threshold == 1000.0  # type: ignore[attr-defined]


def test_inconclusive_band_comes_from_the_profile(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    banded = TEST_ONLY_PROFILE.model_copy(
        update={"inconclusive_bands": {"G2.breakeven_cost_multiple": 1e6}}
    )
    gates = _gates(market, table, ctx=context(profile=banded))
    assert gates["G2.breakeven_cost_multiple"].verdict is Verdict.INCONCLUSIVE  # type: ignore[attr-defined]


def test_unknown_statistical_methods_are_refused(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    odd = TEST_ONLY_PROFILE.model_copy(
        update={
            "significance": TEST_ONLY_PROFILE.significance.model_copy(
                update={"multiple_testing_method": "made-up"}
            )
        }
    )
    with pytest.raises(UnsupportedMethod):
        _gates(market, table, ctx=context(profile=odd))


def test_too_few_trades_is_inconclusive_not_pass() -> None:
    market = generate(seed=5)
    table = outcome_table(market, research_events(market)[:30])
    gates = _gates(market, table)
    assert gates["G2.effective_sample_size"].verdict is Verdict.INCONCLUSIVE  # type: ignore[attr-defined]
    verdict = derive_verdict(tuple(gates.values()))  # type: ignore[arg-type]
    assert verdict is not Verdict.PASS


def test_sealed_oos_gate_requires_the_recorded_unsealing(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, _ = planted
    oos_table = outcome_table(market, sealed_events(market))
    study = MomentumSignStudy(market, _events(oos_table))
    vault = SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=1)
    ctx = context()
    locked = run_sealed_oos(SealedOosInput(ctx, vault, oos_table, study))
    assert [(g.gate_id, g.verdict) for g in locked] == [("G5.unsealing_recorded", Verdict.FAIL)]
    vault.unseal(ctx.metadata.hypothesis_family_id, "raphael", BOUNDARY + timedelta(days=2))
    gates = run_sealed_oos(SealedOosInput(ctx, vault, oos_table, study))
    by_id = {gate.gate_id: gate for gate in gates}
    assert by_id["G5.unsealing_recorded"].verdict is Verdict.PASS
    assert by_id["G5.oos_effective_sample_size"].verdict is Verdict.PASS
    assert by_id["G5.oos_breakeven_cost_multiple"].verdict is Verdict.PASS
    assert build_report(ctx, gates).verdict is Verdict.PASS


def test_outcome_table_hides_labels_until_they_are_known(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    _, table = planted
    first = table.computable()[0]
    assert first.available_time is not None
    assert first not in table.known_as_of(first.available_time - timedelta(microseconds=1))
    assert first in table.known_as_of(first.available_time)
    assert table.rows()[0]["label_only"] is True


def test_a_context_stamp_makes_the_report_hash_reproducible() -> None:
    """Regression (real-data smoke, backlog E3): ``created_at`` is part of the report's content
    hash; without a stamp it is the wall clock, so two identical validations hashed differently."""
    gates = (factories.gate_result(Verdict.PASS),)
    stamp = BOUNDARY - timedelta(hours=1)
    ctx = replace(context(), created_at=stamp)
    first, second = build_report(ctx, gates), build_report(ctx, gates)
    assert first.created_at == second.created_at == stamp
    assert first.content_hash() == second.content_hash()
    unstamped = build_report(context(), gates)  # the default is unchanged: the wall clock
    assert unstamped.created_at != stamp
    assert unstamped.model_copy(update={"created_at": stamp}).content_hash() == first.content_hash()
    with pytest.raises(ValueError):  # the stamp is validated like any other report field
        build_report(replace(ctx, created_at=stamp.replace(tzinfo=None)), gates)
