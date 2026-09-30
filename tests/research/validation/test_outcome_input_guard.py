"""C-L2 at run time: the validation pipeline refuses an Outcome payload as a study input.

MOD-VALID O-1 (2026-09-28): ``run_in_sample`` / ``run_sealed_oos`` pass the study's declared inputs
(and every fitted fold study's inputs) through the contract's own guard
``core.contracts.outcome.refuse_outcome_input`` before computing any gate — fail closed with
``OutcomeUsedAsInput``; G5 refuses before the sealed window is touched. A study whose inputs are
refs is unaffected. Uses the TEST ONLY Profile of ``fixtures``.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast

import pytest

from core.contracts.outcome import OutcomeEvent, OutcomeUsedAsInput
from core.contracts.synthetic import SyntheticMarket
from core.domain.base import Ref
from research.outcomes import OutcomeTable
from research.validation import SealedOosInput, run_in_sample, run_sealed_oos
from research.validation.calibration import MomentumSignStudy
from research.validation.controls import SignalStudy
from tests.research.validation.fixtures import (
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
)


@pytest.fixture(scope="module")
def planted() -> tuple[SyntheticMarket, OutcomeTable]:
    market = generate(seed=3, strength="0.6")
    return market, outcome_table(market, research_events(market))


def _events(table: OutcomeTable) -> list[OutcomeEvent]:
    return [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]


class _OutcomeInputStudy:
    """A study whose declared inputs carry an Outcome payload (object or its JSON dump)."""

    def __init__(self, inputs: tuple[object, ...]) -> None:
        self._inputs = inputs
        self.calls = 0

    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return self._inputs  # type: ignore[return-value]

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        self.calls += 1
        return tuple(0 for _ in event_keys)


class _UntouchableVault:
    """Any use of the vault fails the test: the refusal must come before the sealed window."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the sealed vault was touched ({name}) for a leaking study")


class _LeakyFitStudy(MomentumSignStudy):
    """Honest refs, but a fitted fold study that declares an Outcome payload as input."""

    leak: object

    def fit(self, train_keys: Sequence[str], train_values: Sequence[Decimal]) -> SignalStudy:
        return _OutcomeInputStudy((self.leak,))


def _payloads(table: OutcomeTable) -> tuple[object, object]:
    label = next(iter(table.labels))
    return label, label.model_dump(mode="json")


@pytest.mark.parametrize("dumped", [False, True])
def test_run_in_sample_refuses_an_outcome_payload_input_before_any_gate(
    planted: tuple[SyntheticMarket, OutcomeTable], dumped: bool
) -> None:
    market, table = planted
    payload = _payloads(table)[1 if dumped else 0]
    honest = MomentumSignStudy(market, _events(table)).signal_refs
    study = _OutcomeInputStudy((*honest, payload))
    with pytest.raises(OutcomeUsedAsInput, match="C-L2"):
        run_in_sample(in_sample_input(table, study))
    assert study.calls == 0  # refused before G0 asked for a single side


def test_run_sealed_oos_refuses_an_outcome_payload_input_before_the_vault(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    _, table = planted
    study = _OutcomeInputStudy((_payloads(table)[0],))
    sealed = SealedOosInput(
        context=context(),
        vault=cast(Any, _UntouchableVault()),
        outcomes=table,
        study=study,
    )
    with pytest.raises(OutcomeUsedAsInput, match="C-L2"):
        run_sealed_oos(sealed)
    assert study.calls == 0


def test_a_fitted_fold_study_with_an_outcome_payload_input_is_refused(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    study = _LeakyFitStudy(market, _events(table))
    study.leak = _payloads(table)[0]
    with pytest.raises(OutcomeUsedAsInput, match="C-L2"):
        run_in_sample(in_sample_input(table, study))


def test_a_study_with_ref_inputs_is_unaffected(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    study = MomentumSignStudy(market, _events(table))
    gates = run_in_sample(in_sample_input(table, study))
    assert gates[-1].gate_id == "G3.adjusted_p_value"  # the full G0 - G3 run, as before
