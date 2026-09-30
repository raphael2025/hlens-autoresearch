"""Phase 7: batch hypothesis generation over a declared grid (research.hypotheses.batch)."""

from __future__ import annotations

from dataclasses import MISSING, fields, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.research import Hypothesis, HypothesisOrigin
from core.domain.specs import StrategySpec
from research.hypotheses import (
    BatchGrid,
    BatchOperator,
    BatchRefused,
    HypothesisBatch,
    LedgerError,
    ReviewedOperators,
    TrialLedger,
    expand_batch,
    preregister_batch,
)
from research.hypotheses.batch import NOT_RUNNABLE_KINDS
from research.loop.segment import trial_point
from research.strategies.library import library_entries

NOW = datetime(2026, 9, 26, tzinfo=UTC)
FAMILY = "batch_fam"
TSMOM = library_entries()[0].candidate().spec  # lookback (60, 240, 1440), long_only (F, T)
POINT = BatchOperator(
    name="tsmom_point",
    version="1.0.0",
    kind="parameter_point",
    claim="time-series momentum at this point beats costs",
    expected_direction="higher",
)
REVIEWED = ReviewedOperators(
    name="loop_operators", version="1.0.0", reviewer="alice", operators=(POINT,)
)


def _grid(**changes: Any) -> BatchGrid:
    base: dict[str, Any] = {
        "name": "b_tsmom",
        "family_id": FAMILY,
        "created_at": NOW,
        "operators": (POINT,),
        "strategies": (TSMOM,),
        "points": (
            {"lookback": 60},
            {"lookback": 240, "long_only": True},
            {"lookback": 1440, "long_only": False},
        ),
        "minimum_meaningful_effect": "net mean return above costs",
    }
    return BatchGrid(**{**base, **changes})


def test_every_cell_is_a_runnable_trial_point_of_what_was_declared() -> None:
    batch = expand_batch(_grid(), REVIEWED)
    assert len(batch.hypotheses) == _grid().cells == 3
    expected = [
        {"lookback": 60},
        {"long_only": True, "lookback": 240},
        {"long_only": False, "lookback": 1440},
    ]
    for hypothesis, point in zip(batch.hypotheses, expected, strict=True):
        parsed = trial_point(hypothesis)  # exactly the loop's parser
        assert parsed.strategy == "tsmom_bars@1.0.0" and dict(parsed.overrides) == point
        assert all(type(v) is type(point[k]) for k, v in parsed.overrides.items())
        assert hypothesis.origin is HypothesisOrigin.COMBINATION
        assert hypothesis.origin_refs == (TSMOM.ref,) and hypothesis.family_id == FAMILY
        assert hypothesis.created_at == NOW and hypothesis.name.startswith("b_tsmom_")
    again = expand_batch(_grid(), REVIEWED)  # deterministic
    assert [h.content_hash() for h in again.hypotheses] == [
        h.content_hash() for h in batch.hypotheses
    ]
    assert batch.payload()["hypotheses"] == [h.content_hash() for h in batch.hypotheses]


def test_frozen_parameter_points_keep_canonical_batch_hashing() -> None:
    grid = _grid(points=({"lookback": 60}, {"lookback": 240, "long_only": True}))

    assert [dict(point) for point in grid.points] == [
        {"lookback": 60},
        {"long_only": True, "lookback": 240},
    ]
    assert (
        grid.content_hash()
        == _grid(points=({"lookback": 60}, {"lookback": 240, "long_only": True})).content_hash()
    )


def test_the_whole_batch_is_preregistered_before_anything_runs() -> None:
    ledger = TrialLedger()
    batch = expand_batch(_grid(), REVIEWED)
    assert preregister_batch(batch, ledger) == batch.hypotheses
    assert ledger.trials(FAMILY) == 3 and all(ledger.is_registered(h) for h in batch.hypotheses)
    assert preregister_batch(batch, ledger) == ()  # idempotent: no trial counted twice
    assert ledger.trials(FAMILY) == 3


def test_a_conflicting_cell_refuses_the_whole_batch_before_any_line() -> None:
    batch = expand_batch(_grid(), REVIEWED)
    ledger = TrialLedger()
    clash = batch.hypotheses[2].model_copy(update={"statement": "something else"})
    ledger.register(clash)
    with pytest.raises(LedgerError, match="other content"):
        preregister_batch(batch, ledger)
    assert ledger.trials(FAMILY) == 1  # nothing of the batch was written


def test_an_operator_off_the_reviewed_allowlist_is_refused() -> None:
    stranger = replace(POINT, name="unreviewed_point")
    with pytest.raises(BatchRefused, match="not on the reviewed allowlist"):
        expand_batch(_grid(operators=(stranger,)), REVIEWED)
    edited = replace(POINT, claim="a different claim under the reviewed name")
    with pytest.raises(BatchRefused, match="not on the reviewed allowlist"):
        expand_batch(_grid(operators=(edited,)), REVIEWED)
    newer = replace(POINT, version="1.1.0")
    with pytest.raises(BatchRefused, match="not on the reviewed allowlist"):
        expand_batch(_grid(operators=(newer,)), REVIEWED)


def test_a_dsl_operator_trial_point_cannot_run_is_refused_even_when_reviewed() -> None:
    for kind in sorted(NOT_RUNNABLE_KINDS):
        operator = replace(POINT, name=f"op_{kind}", kind=kind)
        reviewed = ReviewedOperators(
            name="loop_operators", version="1.0.1", reviewer="alice", operators=(operator,)
        )
        with pytest.raises(BatchRefused, match="trial_point cannot run"):
            expand_batch(_grid(operators=(operator,)), reviewed)


def test_an_unknown_operator_kind_is_refused_when_declared() -> None:
    with pytest.raises(BatchRefused, match="unknown kind"):
        replace(POINT, kind="exec_python")


@pytest.mark.parametrize(
    ("point", "match"),
    [
        ({"horizon": 5}, "no search space"),  # undeclared parameter
        ({"lookback": 61}, "not a declared point"),  # outside the declared space
        ({"lookback": 60.0}, "not a declared point"),  # a float is never requested
        ({"long_only": 1}, "not a declared point"),  # an int is not a bool
        ({"lookback": True}, "not a declared point"),  # nor a bool an int
        ({"Lookback": 60}, "parameter name"),
    ],
)
def test_a_point_the_strategy_cannot_request_is_refused(point: dict[str, Any], match: str) -> None:
    with pytest.raises(BatchRefused, match=match):
        expand_batch(_grid(points=(point,)), REVIEWED)


def test_a_text_value_that_would_not_read_back_is_refused() -> None:
    spec = StrategySpec(
        name="text_strategy",
        version="1.0.0",
        created_at=NOW,
        signals=(Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0"),),
        params=FrozenMapping({"mode": "fast"}),
        param_search_space=FrozenMapping({"mode": ("fast", "60", "a b", "true")}),
    )
    batch = expand_batch(_grid(strategies=(spec,), points=({"mode": "fast"},)), REVIEWED)
    assert dict(trial_point(batch.hypotheses[0]).overrides) == {"mode": "fast"}
    for value in ("60", "a b", "true"):  # would read back as an int, split, or a bool
        with pytest.raises(BatchRefused, match="read back"):
            expand_batch(_grid(strategies=(spec,), points=({"mode": value},)), REVIEWED)


@pytest.mark.parametrize(
    "changes",
    [
        {"operators": ()},
        {"strategies": ()},
        {"points": ()},
        {"operators": (POINT, POINT)},
        {"strategies": (TSMOM, TSMOM)},
        {"points": ({"lookback": 60}, {"lookback": 60})},
        {"name": "Bad Name"},
        {"family_id": " "},
        {"minimum_meaningful_effect": ""},
        {"created_at": datetime(2026, 9, 26)},  # naive
    ],
)
def test_an_empty_duplicated_or_malformed_grid_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(BatchRefused):
        _grid(**changes)


def test_the_allowlist_is_declared_versioned_and_reviewed() -> None:
    for kwargs in (
        {"version": "v1"},
        {"reviewer": " "},
        {"operators": ()},
        {"operators": (POINT, POINT)},
    ):
        with pytest.raises(BatchRefused):
            replace(REVIEWED, **kwargs)
    assert REVIEWED.admits(POINT) and REVIEWED.key == "loop_operators@1.0.0"


def test_grid_allowlist_and_operators_have_no_defaults() -> None:
    for cls in (BatchGrid, BatchOperator, ReviewedOperators):
        assert all(f.default is MISSING and f.default_factory is MISSING for f in fields(cls)), cls
    with pytest.raises(TypeError):
        HypothesisBatch(_grid(), REVIEWED, ())  # type: ignore[call-arg]  # computed, never passed


def test_generated_hypotheses_are_specifications_only() -> None:
    batch = expand_batch(_grid(), REVIEWED)
    assert all(isinstance(h, Hypothesis) for h in batch.hypotheses)
    for hypothesis in batch.hypotheses:
        assert all(line.startswith(("strategy = ", "param ")) for line in hypothesis.conditions)
