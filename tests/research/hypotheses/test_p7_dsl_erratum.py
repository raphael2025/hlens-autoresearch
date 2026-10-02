"""ADR-0103 §7 erratum of the hypothesis DSL: a negation is a candidate strategy (ADR-0088), not a
validation negative control, and the cross-sectional transforms are DSL transformations."""

from __future__ import annotations

import pytest

from core.domain.base import Kind, Ref
from research.hypotheses import negation, transformation

S = Ref(kind=Kind.STRATEGY, name="tsmom_bars", version="1.0.0")
F = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")


def test_negation_is_stated_as_a_candidate_not_a_control() -> None:
    statement = negation("h_neg", "fam", S, "0.1").statement
    assert "a candidate strategy, not a validation negative control" in statement
    assert "(a control, not a candidate)" not in statement


@pytest.mark.parametrize("transform", ["rank_cs", "quantile_cs"])
def test_cross_sectional_transforms_are_accepted(transform: str) -> None:
    hypothesis = transformation("h_cs", "fam", F, transform, "0.1")
    assert transform in hypothesis.statement and hypothesis.origin_refs == (F,)


def test_unknown_transforms_are_still_refused() -> None:
    with pytest.raises(ValueError, match="unknown transformation"):
        transformation("h_x", "fam", F, "rank_xs", "0.1")
