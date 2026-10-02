"""ADR-0100 修订 2: the ``hlens.p11.inputs@1.0.0`` record a new run carries in its hashed
``repro.params`` (``research.experiments.run_inputs``) — what the P11 authority compares, so a
record must be exactly one canonical payload, never a partial or tolerant read. TEST ONLY values.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.domain.base import canonical_json
from research.experiments.run_inputs import (
    RUN_INPUTS_FORMAT,
    RUN_INPUTS_KEY,
    STATE_LABELLER_FORMAT,
    RunInputs,
    RunInputsError,
    execution_payload,
    recorded_run_inputs,
    state_labeller_identity,
    strategy_params,
    with_run_inputs,
)
from tests.research.operations import authority_fixtures as af


def test_the_record_round_trips_exactly() -> None:
    inputs = af.run_inputs(
        control_seeds=(1, 2), cscv_partitions=8, impact_coefficient=0.5, state_labeller={"a": 1}
    )
    params = with_run_inputs({"lookback": 2}, inputs)
    assert params[RUN_INPUTS_KEY] == inputs.text() == canonical_json(inputs.payload())
    assert recorded_run_inputs(params) == inputs
    assert strategy_params(params) == {"lookback": 2}
    assert inputs.payload()["format"] == RUN_INPUTS_FORMAT == RUN_INPUTS_KEY
    assert inputs.execution_payload() == {
        "decision_step_microseconds": 60_000_000,
        "decision_warmup_microseconds": 0,
        "initial_equity": "10000",
    }


def test_a_run_without_the_key_records_nothing() -> None:
    assert recorded_run_inputs({"lookback": 2}) is None  # an old run: never inferred


def test_the_key_is_reserved() -> None:
    with pytest.raises(RunInputsError):
        with_run_inputs({RUN_INPUTS_KEY: "x"}, af.run_inputs())
    with pytest.raises(RunInputsError):
        with_run_inputs({}, af.run_inputs().payload())  # type: ignore[arg-type]


def _text(payload: Any) -> str:
    return canonical_json(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {**p, "format": "hlens.p11.inputs@2.0.0"},
        lambda p: {**p, "extra": 1},
        lambda p: {k: v for k, v in p.items() if k != "validation"},
        lambda p: {**p, "execution": {**p["execution"], "initial_equity": "1e4"}},
        lambda p: {**p, "execution": {**p["execution"], "initial_equity": 10000}},
        lambda p: {**p, "execution": {**p["execution"], "decision_step_microseconds": 60.0}},
        lambda p: {**p, "execution": {**p["execution"], "decision_step_microseconds": 0}},
        lambda p: {**p, "validation": {**p["validation"], "family_trial_count": 0}},
        lambda p: {**p, "validation": {**p["validation"], "family_trial_count": True}},
        lambda p: {**p, "validation": {**p["validation"], "validation_seed": 7.0}},
        lambda p: {**p, "validation": {**p["validation"], "control_seeds": []}},
        lambda p: {**p, "validation": {**p["validation"], "control_seeds": [1.5]}},
        lambda p: {**p, "validation": {**p["validation"], "state_labeller": {}}},
        lambda p: {**p, "validation": {**p["validation"], "impact_coefficient": "0.1"}},
    ],
)
def test_an_invalid_payload_is_refused(mutate: Any) -> None:
    payload = mutate(af.run_inputs().payload())
    with pytest.raises(RunInputsError):
        recorded_run_inputs({RUN_INPUTS_KEY: _text(payload)})


def test_a_non_canonical_or_non_text_record_is_refused() -> None:
    payload = af.run_inputs().payload()
    for value in (json.dumps(payload), "not json", 7):
        with pytest.raises(RunInputsError):
            recorded_run_inputs({RUN_INPUTS_KEY: value})


@pytest.mark.parametrize(
    ("step", "warmup", "equity"),
    [
        (timedelta(0), timedelta(0), Decimal(1)),
        (timedelta(minutes=1), timedelta(seconds=-1), Decimal(1)),
        (timedelta(microseconds=1) / 2, timedelta(0), Decimal(1)),
        (timedelta(minutes=1), timedelta(0), Decimal(0)),
        (timedelta(minutes=1), timedelta(0), Decimal("Infinity")),
    ],
)
def test_the_execution_block_needs_a_valid_grid_and_equity(
    step: timedelta, warmup: timedelta, equity: Decimal
) -> None:
    with pytest.raises(RunInputsError):
        execution_payload(step, warmup, equity)


def test_equal_equities_with_other_text_are_other_records() -> None:
    plain = execution_payload(af.MINUTE, timedelta(0), Decimal("1000"))
    padded = execution_payload(af.MINUTE, timedelta(0), Decimal("1000.00"))
    assert canonical_json(plain) != canonical_json(padded)


def test_the_state_labeller_identity_binds_its_specs_and_providers() -> None:
    class _Descriptor:
        def __init__(self, name: str) -> None:
            self.name, self.version = name, "1.0.0"

        def content_hash(self) -> str:
            return self.name[0] * 64

    class _Provider:
        def __init__(self, name: str) -> None:
            self.descriptor = _Descriptor(name)

    class _Spec:
        def __init__(self, ref: str, digest: str) -> None:
            self.ref, self._digest = ref, digest

        def content_hash(self) -> str:
            return self._digest

    identity = state_labeller_identity(
        state_spec=_Spec("state:s@1.0.0", "1" * 64),
        state_provider=_Provider("trend_range"),
        feature_spec=_Spec("feature:f@1.0.0", "2" * 64),
        feature_provider=_Provider("bar_log_return"),
    )
    assert identity["format"] == STATE_LABELLER_FORMAT
    assert identity["state_provider"] == "trend_range@1.0.0"
    assert identity["feature_spec_hash"] == "2" * 64
    assert identity["missing_label"] == "unknown"
    record = af.run_inputs(state_labeller=identity)
    assert recorded_run_inputs(with_run_inputs({}, record)) == record
    assert isinstance(RunInputs.from_payload(record.payload()), RunInputs)
