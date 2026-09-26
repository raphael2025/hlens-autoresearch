"""Optional multi-seed G1 negative controls (Phase 4 debugging pass, 2026-09-26; ``pipeline``
module docs, **Multi-seed negative controls**; backlog C "P4").

``InSampleInput.control_seeds = None`` (default) is byte-identical to the single-seed controls
(gate hashes pinned below). With seeds, each control runs once per seed; per-seed gates
``G1.<control>.seed.<s>`` are judged under the base gate id (its Profile band applies) and the
base gate aggregates them by the standard rule, reporting the minimum p-value. Profiles below are
the clearly marked TEST ONLY fixtures; the thresholds derived here exist only inside these tests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from core.contracts.outcome import OutcomeEvent
from core.contracts.synthetic import SyntheticMarket
from core.domain.research import GateResult, Verdict
from core.errors import ReasonCode
from research.outcomes import OutcomeTable
from research.validation import build_report, failure_record, run_in_sample
from research.validation.calibration import MomentumSignStudy
from research.validation.pipeline import CONTROL_SEED_INFIX
from tests.contract_version_support import envelopes_at, envelopes_at_pre_bump
from tests.research.validation.fixtures import (
    TEST_ONLY_PROFILE,
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
)
from tests.research.validation.test_pipeline import LeakyStudy

#: sha256 of the canonical JSON of ``run_in_sample`` gates (default fixture seed 11) computed with
#: the pre-change ``pipeline.py`` (commit 9714b7f) on the development platform. Float-derived
#: (ADR-0037 §5, D-FLOAT): same-platform reproducible.
PINNED = {
    "planted": "41fb0a6a0b2602e61f8d56b3437a9633870bad4af4834fba081c0e68c67a5112",
    "noise": "20d73f2ca84e5d6569fa5c52644169d24cd889301a013f2fd541563fde9198c8",
    "leaky": "cc4811fea065b46d099d13385db9254f53b44d925794164c48801e4ce3ac90ae",
}
CONTROLS = ("G1.shuffle_control", "G1.shift_control")
SEEDS = (11, 12, 101, 202, 303, 404, 505, 606)


#: The same objects built now carry the 2.1.0 envelope (ADR-0052 M2: new objects are 2.1.0
#: and the envelope is part of every content hash); pinned next to the 2.0.0 evidence above.
PINNED_2_1_0 = {
    "planted": "af480090c8e7c5bc67aee572227b034602c84d0ea92a6009c4cc83a01b055f51",
    "noise": "b099dce524d093411ee44a67f708860ae24487537f489e2b71c8b8b2270670b6",
    "leaky": "9a263b397e9a2578ffeb318bafa8bcab78038aee02faa436feb6df6c36300777",
}


def _case(name: str) -> tuple[SyntheticMarket, OutcomeTable, object]:
    seed, strength = (3, "0.6") if name == "planted" else (5, None)
    market = generate(seed=seed, strength=strength)
    table = outcome_table(market, research_events(market))
    events = [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]
    study = LeakyStudy() if name == "leaky" else MomentumSignStudy(market, events)
    return market, table, study


@pytest.fixture(scope="module")
def cases() -> dict[str, tuple[SyntheticMarket, OutcomeTable, object]]:
    return {name: _case(name) for name in PINNED}


def _run(case: tuple[SyntheticMarket, OutcomeTable, object], **kwargs: object):  # type: ignore[no-untyped-def]
    _, table, study = case
    seeds = kwargs.pop("control_seeds", None)
    inp = in_sample_input(table, study, **kwargs)  # type: ignore[arg-type]
    return run_in_sample(replace(inp, control_seeds=seeds))  # type: ignore[arg-type]


def _digest(gates: tuple[GateResult, ...]) -> str:
    # The pins predate contract 2.1.0 (ADR-0052 §4): envelopes compared as 2.0.0, all else exact.
    text = json.dumps(
        envelopes_at_pre_bump([g.model_dump(mode="json") for g in gates]), sort_keys=True
    )
    return hashlib.sha256(text.encode()).hexdigest()


def _by_id(gates: tuple[GateResult, ...]) -> dict[str, GateResult]:
    return {gate.gate_id: gate for gate in gates}


# --------------------------------------------------------------------------------------
# unset: byte-identical
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PINNED))
def test_unset_control_seeds_is_byte_identical(
    name: str, cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]]
) -> None:
    gates = _run(cases[name])
    assert _digest(gates) == PINNED[name]
    # the 2.1.0 pins (ADR-0055): envelopes compared as 2.1.0, all else exact
    raw = json.dumps(
        envelopes_at([g.model_dump(mode="json") for g in gates], "2.1.0"), sort_keys=True
    )
    assert hashlib.sha256(raw.encode()).hexdigest() == PINNED_2_1_0[name]
    assert not any(CONTROL_SEED_INFIX in gate.gate_id for gate in gates)


# --------------------------------------------------------------------------------------
# with seeds: per-seed gates + aggregate
# --------------------------------------------------------------------------------------


def test_per_seed_gates_are_the_single_seed_controls_under_recorded_ids(
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    legacy = _by_id(_run(cases["planted"]))  # shuffle with seed 11, shift with seed 12
    multi = _by_id(_run(cases["planted"], control_seeds=(11, 12)))
    for gate_id, seed in (("G1.shuffle_control", 11), ("G1.shift_control", 12)):
        per_seed = multi[f"{gate_id}.seed.{seed}"]
        assert per_seed.model_dump() == {
            **legacy[gate_id].model_dump(),
            "gate_id": per_seed.gate_id,
        }


def test_an_honest_study_passes_every_seed_and_the_aggregate_reports_the_minimum(
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    gates = _run(cases["planted"], control_seeds=SEEDS)
    ids = [gate.gate_id for gate in gates]
    by_id = _by_id(gates)
    for gate_id in CONTROLS:
        start = ids.index(gate_id)
        assert ids[start + 1 : start + 1 + len(SEEDS)] == [f"{gate_id}.seed.{s}" for s in SEEDS]
        per_seed = [by_id[f"{gate_id}.seed.{s}"] for s in SEEDS]
        aggregate = by_id[gate_id]
        assert all(gate.verdict is Verdict.PASS for gate in per_seed)
        assert aggregate.verdict is Verdict.PASS
        assert aggregate.value == min(gate.value for gate in per_seed)
        assert aggregate.metric.endswith("_timing_p_value_min_over_seeds[>=]")
        assert aggregate.threshold_source == "significance.multiple_testing_threshold"
        assert all(g.threshold_source == aggregate.threshold_source for g in per_seed)
    assert ids[-1] == "G3.adjusted_p_value"


def test_the_null_model_stays_single_seed(
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    legacy = _by_id(_run(cases["planted"]))
    multi = _by_id(_run(cases["planted"], control_seeds=SEEDS))
    later = [gate_id for gate_id in legacy if gate_id.split(".")[0] in {"G2", "G3"}]
    assert "G2.null_model_percentile" in later
    for gate_id in later:
        assert multi[gate_id] == legacy[gate_id]


def test_a_leaky_study_fails_every_seed_and_is_recorded_as_leakage(
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    gates = _run(cases["leaky"], control_seeds=(1, 2, 3))
    by_id = _by_id(gates)
    for gate_id in CONTROLS:
        assert by_id[gate_id].verdict is Verdict.FAIL
        assert all(by_id[f"{gate_id}.seed.{s}"].verdict is Verdict.FAIL for s in (1, 2, 3))
    assert not any(gate.gate_id.startswith("G2") for gate in gates)
    record = failure_record(build_report(context(), gates), "family-1")
    assert record is not None and record.reason_code is ReasonCode.LEAKAGE_DETECTED
    assert (record.gate_id or "").startswith("G1.")  # the first G1 FAIL (label-blind sides)


def test_one_failing_seed_fails_a_control_the_single_seed_would_pass(
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    probe = _by_id(_run(cases["planted"], control_seeds=SEEDS))
    shuffles = {s: probe[f"G1.shuffle_control.seed.{s}"].value for s in SEEDS}
    lowest = min(shuffles.values())
    # TEST ONLY threshold strictly between the lowest per-seed p-value and every legacy p-value
    legacy = _by_id(_run(cases["planted"]))
    ceiling = min(legacy[gate_id].value for gate_id in CONTROLS)
    assert lowest < ceiling, "the probe seeds must include a lower shuffle p-value"
    alpha = (lowest + ceiling) / 2
    strict = TEST_ONLY_PROFILE.model_copy(
        update={
            "name": "test_only_multi_seed_alpha",
            "significance": TEST_ONLY_PROFILE.significance.model_copy(
                update={"multiple_testing_threshold": alpha}
            ),
        }
    )
    ctx = context(profile=strict)
    single = _by_id(_run(cases["planted"], ctx=ctx))
    assert single["G1.shuffle_control"].verdict is Verdict.PASS
    multi = _by_id(_run(cases["planted"], ctx=ctx, control_seeds=SEEDS))
    assert multi["G1.shuffle_control"].verdict is Verdict.FAIL
    assert multi["G1.shuffle_control"].value == lowest
    verdicts = {multi[f"G1.shuffle_control.seed.{s}"].verdict for s in SEEDS}
    assert verdicts == {Verdict.PASS, Verdict.FAIL}


def test_the_base_gate_band_applies_to_every_seed(
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    banded = TEST_ONLY_PROFILE.model_copy(
        update={
            "name": "test_only_multi_seed_band",
            "inconclusive_bands": {"G1.shuffle_control": 1.0},  # TEST ONLY: covers every p
        }
    )
    multi = _by_id(_run(cases["planted"], ctx=context(profile=banded), control_seeds=(11, 12)))
    assert multi["G1.shuffle_control.seed.11"].verdict is Verdict.INCONCLUSIVE
    assert multi["G1.shuffle_control.seed.12"].verdict is Verdict.INCONCLUSIVE
    assert multi["G1.shuffle_control"].verdict is Verdict.INCONCLUSIVE
    assert multi["G1.shift_control"].verdict is Verdict.PASS  # its own id carries no band
    # a band keyed by a per-seed id is not a Profile gate id: it changes nothing
    stray = TEST_ONLY_PROFILE.model_copy(
        update={
            "name": "test_only_multi_seed_stray_band",
            "inconclusive_bands": {"G1.shuffle_control.seed.11": 1.0},
        }
    )
    plain = _by_id(_run(cases["planted"], ctx=context(profile=stray), control_seeds=(11,)))
    assert plain["G1.shuffle_control.seed.11"].verdict is Verdict.PASS


# --------------------------------------------------------------------------------------
# refused options
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seeds", "error"),
    [
        ((), ValueError),
        ((1, 1), ValueError),
        ([1, 2], ValueError),
        ((1, True), TypeError),
        ((1, 2.0), TypeError),
    ],
)
def test_invalid_control_seeds_are_refused(
    seeds: object,
    error: type[Exception],
    cases: dict[str, tuple[SyntheticMarket, OutcomeTable, object]],
) -> None:
    _, table, study = cases["planted"]
    with pytest.raises(error):
        replace(in_sample_input(table, study), control_seeds=seeds)  # type: ignore[arg-type]
