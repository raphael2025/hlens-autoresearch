"""``research.validation.instruments``: pooled + per-instrument G0 – G3, then G4 (Phase 8
implementation note, 2026-09-26; CODE_COMPLETE / DEBUG_PENDING).

!!! TEST ONLY !!!  Uses the uncalibrated ``TEST_ONLY_PROFILE`` of ``fixtures``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.contracts.outcome import OutcomeEvent
from core.domain.base import Kind, Ref
from core.domain.research import Verdict, derive_verdict
from research.outcomes.table import OutcomeTable
from research.validation.calibration import MomentumSignStudy
from research.validation.controls import FixedSides
from research.validation.instruments import (
    INSTRUMENT_INFIX,
    instrument_of,
    pool_outcomes,
    run_multi_instrument_validation,
)
from research.validation.pipeline import InSampleInput, run_in_sample
from tests.research.validation import fixtures as fx

NAMES = ("A-USDT", "B-USDT")


#: Planted (``strength`` 0.6, the planted fixture of ``test_pipeline``) market seed per instrument.
SEEDS = {"A-USDT": 3, "B-USDT": 4}
SIDES: dict[str, int] = {}  # event key -> side of the causal toy signal (MomentumSignStudy)


def _table(name: str, seed: int) -> OutcomeTable:
    market = fx.generate(seed, strength="0.6")
    events = tuple(
        OutcomeEvent(event_key=f"{name}|{e.event_time.isoformat()}", event_time=e.event_time)
        for e in fx.research_events(market)
    )
    keys = tuple(event.event_key for event in events)
    SIDES.update(zip(keys, MomentumSignStudy(market, events).sides(keys, ()), strict=True))
    return fx.outcome_table(market, events)


@pytest.fixture(scope="module")
def tables() -> dict[str, OutcomeTable]:
    return {name: _table(name, seed) for name, seed in SEEDS.items()}


def _study(*tables: OutcomeTable) -> FixedSides:
    refs = (Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0"),)
    return FixedSides(
        refs=refs,
        by_event={label.event_key: SIDES[label.event_key] for table in tables for label in table},
    )


def _no_pooled_fail(pooled: InSampleInput) -> None:
    failing = [g.gate_id for g in run_in_sample(pooled) if g.verdict is Verdict.FAIL]
    assert not failing, failing  # precondition of the TEST ONLY data


def _inputs(
    tables: dict[str, OutcomeTable],
) -> tuple[InSampleInput, dict[str, InSampleInput]]:
    pooled_table = pool_outcomes(tables)
    ctx = fx.context()
    pooled = InSampleInput(
        context=ctx,
        outcomes=pooled_table,
        study=_study(*tables.values()),
        seed=11,
        reproduce=lambda: "h" * 64,
        recorded_result_hash="h" * 64,
    )
    per = {
        name: replace(pooled, outcomes=table, study=_study(table)) for name, table in tables.items()
    }
    return pooled, per


def test_instrument_of_reads_the_key() -> None:
    assert instrument_of("A-USDT|2024-01-01T00:00:00+00:00") == "A-USDT"
    for bad in ("e00001", "|2024", "A-USDT|"):
        with pytest.raises(ValueError, match="does not name its instrument"):
            instrument_of(bad)


def test_pooling_keeps_every_correctly_keyed_label(tables: dict[str, OutcomeTable]) -> None:
    pooled = pool_outcomes(tables)
    assert len(pooled) == sum(len(table) for table in tables.values())
    assert {label.event_key for label in pooled} == {
        label.event_key for table in tables.values() for label in table
    }
    order = [(label.event_time, label.event_key) for label in pooled]
    assert order == sorted(order)
    assert pooled.result_hash == pool_outcomes(dict(reversed(tables.items()))).result_hash
    assert pooled.result_hash not in {table.result_hash for table in tables.values()}
    assert (pooled.request_hash, pooled.provider_hash) == (None, None)  # never stored


def test_pooling_refuses_what_is_not_correctly_keyed(tables: dict[str, OutcomeTable]) -> None:
    with pytest.raises(ValueError, match="nothing to pool"):
        pool_outcomes({})
    swapped = {"A-USDT": tables["B-USDT"], "B-USDT": tables["A-USDT"]}
    with pytest.raises(ValueError, match="keyed to another instrument"):
        pool_outcomes(swapped)
    other = replace(tables["B-USDT"], label_spec_hash="0" * 64)
    with pytest.raises(ValueError, match="different outcomes"):
        pool_outcomes({"A-USDT": tables["A-USDT"], "B-USDT": other})


def test_the_inputs_must_partition_the_pooled_labels(tables: dict[str, OutcomeTable]) -> None:
    pooled, per = _inputs(tables)
    with pytest.raises(ValueError, match="at least two"):
        run_multi_instrument_validation(pooled, per, ("A-USDT",), None)
    with pytest.raises(ValueError, match="outside the scope"):
        run_multi_instrument_validation(pooled, per, ("A-USDT", "C-USDT"), None)
    with pytest.raises(ValueError, match="not exactly the pooled labels"):
        run_multi_instrument_validation(pooled, {"A-USDT": per["A-USDT"]}, NAMES, None)
    with pytest.raises(ValueError, match="share the pooled binding"):
        run_multi_instrument_validation(
            pooled, {**per, "B-USDT": replace(per["B-USDT"], seed=12)}, NAMES, None
        )
    wrong = {"A-USDT": per["B-USDT"], "B-USDT": per["A-USDT"]}
    with pytest.raises(ValueError, match="keyed to another instrument"):
        run_multi_instrument_validation(pooled, wrong, NAMES, None)


def test_per_instrument_gates_are_the_instruments_own_run(tables: dict[str, OutcomeTable]) -> None:
    pooled, per = _inputs(tables)
    _no_pooled_fail(pooled)
    run = run_multi_instrument_validation(pooled, per, NAMES, None)
    pooled_gates = run_in_sample(pooled)
    assert run.gates[: len(pooled_gates)] == pooled_gates
    offset = len(pooled_gates)
    for name in NAMES:
        own = run_in_sample(per[name])
        recorded = run.gates[offset : offset + len(own)]
        offset += len(own)
        assert [g.gate_id for g in recorded] == [
            f"{g.gate_id}{INSTRUMENT_INFIX}{name}" for g in own
        ]
        assert [g.model_dump(exclude={"gate_id"}) for g in recorded] == [
            g.model_dump(exclude={"gate_id"}) for g in own
        ]
        evidence = next(item for item in run.per_instrument if item.instrument == name)
        assert (evidence.labels, evidence.verdict) == (len(tables[name]), derive_verdict(own))
    # Both planted instruments pass on their own; G4 then runs (no input here → INCONCLUSIVE).
    assert [item.verdict for item in run.per_instrument] == [Verdict.PASS, Verdict.PASS]
    assert run.gates[-1].gate_id == "G4.robustness_input"
    assert derive_verdict(run.gates) is Verdict.INCONCLUSIVE


def test_one_failing_instrument_fails_and_stops_before_g4(tables: dict[str, OutcomeTable]) -> None:
    pooled, per = _inputs(tables)
    broken = replace(per["B-USDT"], reproduce=lambda: "x" * 64)  # B's re-run does not reproduce
    _no_pooled_fail(pooled)
    run = run_multi_instrument_validation(pooled, {**per, "B-USDT": broken}, NAMES, None)
    gate = next(g for g in run.gates if g.gate_id == f"G0.reproducibility{INSTRUMENT_INFIX}B-USDT")
    assert gate.verdict is Verdict.FAIL
    assert derive_verdict(run.gates) is Verdict.FAIL
    assert run.robustness is None and not any(g.gate_id.startswith("G4.") for g in run.gates)
    assert run.view()["B-USDT"] == {"labels": len(tables["B-USDT"]), "verdict": "FAIL"}


def test_an_instrument_without_labels_is_never_a_pass(tables: dict[str, OutcomeTable]) -> None:
    only_a = {"A-USDT": tables["A-USDT"]}
    pooled, per = _inputs(only_a)
    _no_pooled_fail(pooled)
    run = run_multi_instrument_validation(pooled, per, NAMES, None)
    gap = next(g for g in run.gates if g.gate_id == f"G0.data_available{INSTRUMENT_INFIX}B-USDT")
    assert (gap.verdict, gap.metric) == (Verdict.INCONCLUSIVE, "instrument_labels")
    assert derive_verdict(run.gates) is not Verdict.PASS
    assert run.view()["B-USDT"] == {"labels": 0, "verdict": "INCONCLUSIVE"}


def test_a_pooled_failure_stops_before_the_instruments(tables: dict[str, OutcomeTable]) -> None:
    pooled, per = _inputs(tables)
    failing = replace(pooled, reproduce=lambda: "x" * 64)
    run = run_multi_instrument_validation(failing, per, NAMES, None)
    assert derive_verdict(run.gates) is Verdict.FAIL
    assert not any(INSTRUMENT_INFIX in g.gate_id for g in run.gates)
    assert [item.verdict for item in run.per_instrument] == [None, None]
