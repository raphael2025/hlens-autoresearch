"""Phase 12 framework: lineage-preserving evolution operators (ADR-0045)."""

from __future__ import annotations

import pytest

from core.domain.base import Kind, Ref
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleState
from research.evolution import (
    EvolutionError,
    LineageGraph,
    combine,
    mutate,
    require_new_version,
    retire,
)

SIGNAL = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
OTHER = Ref(kind=Kind.FEATURE, name="bar_realized_vol_30", version="1.0.0")


def _spec(name: str = "tsmom", **fields: object) -> StrategySpec:
    base: dict[str, object] = {
        "name": name,
        "version": "1.0.0",
        "signals": (SIGNAL,),
        "params": {"lookback": 20},
        "param_search_space": {"lookback": (10, 20, 40)},
    }
    base.update(fields)
    return StrategySpec(**base)  # type: ignore[arg-type]


def test_a_mutation_is_a_new_version_descending_from_its_parent() -> None:
    parent = _spec()
    child = mutate(parent, "lookback", 40)
    assert child.spec.version == "1.1.0" and child.spec.params["lookback"] == 40
    assert child.spec.lineage == (parent.ref,) and child.lifecycle_entry is LifecycleState.IDEA
    assert parent.params["lookback"] == 20  # the parent is untouched


@pytest.mark.parametrize(("param", "value"), [("lookback", 30), ("threshold", 1), ("lookback", 20)])
def test_mutations_outside_the_declared_space_are_refused(param: str, value: int) -> None:
    with pytest.raises(EvolutionError):
        mutate(_spec(), param, value)


def test_a_combination_traces_both_parents() -> None:
    a = _spec("tsmom")
    b = _spec("volscaled", signals=(OTHER,), params={"target": 1}, param_search_space={})
    child = combine(a, b, "tsmom_volscaled")
    assert set(child.spec.lineage) == {a.ref, b.ref}
    graph = LineageGraph([a, b, child.spec])
    assert graph.ancestors(child.spec.ref) == tuple(sorted((a.ref, b.ref), key=str))
    assert child.spec.ref in graph.descendants(a.ref) and graph.missing() == ()


def test_an_active_strategy_cannot_be_changed_in_place() -> None:
    active = _spec()
    edited = active.model_copy(update={"params": {"lookback": 40}})
    with pytest.raises(EvolutionError, match="in place"):
        require_new_version(active, StrategySpec.model_validate(edited.model_dump()))
    require_new_version(active, mutate(active, "lookback", 40).spec)
    stranger = _spec(version="2.0.0", params={"lookback": 40})
    with pytest.raises(EvolutionError, match="does not descend"):
        require_new_version(active, stranger)


def test_retirement_is_an_append_only_record() -> None:
    record = retire(_spec(), "degraded beyond the declared bound", evidence=("report-1",))
    assert record.subject_ref == _spec().ref and record.evidence == ("report-1",)
