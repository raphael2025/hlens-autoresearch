"""Phase 12 framework: lineage-preserving evolution operators (ADR-0045).

``combine`` fail-closed coverage (ADR-0069) lives here too: conflicting search spaces, risk
policies and applicable instrument sets must all be refused before a child is ever built, and a
matching pair on all three must still combine cleanly.
"""

from __future__ import annotations

import pytest

from core.domain.base import Kind, Ref
from core.domain.specs import Instrument, InstrumentType, StrategySpec
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
RISK_POLICY = Ref(kind=Kind.RISK, name="cap", version="1.0.0")
OTHER_RISK_POLICY = Ref(kind=Kind.RISK, name="cap_wide", version="1.0.0")
BTC = Instrument(
    venue="sim", symbol="BTCUSDT", instrument_type=InstrumentType.SPOT, base="BTC", quote="USDT"
)
ETH = Instrument(
    venue="sim", symbol="ETHUSDT", instrument_type=InstrumentType.SPOT, base="ETH", quote="USDT"
)


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


def test_combine_needs_two_different_strategies() -> None:
    """ADR-0069: identical parents (same ``ref``) can never combine, even with themselves."""
    a = _spec("tsmom")
    with pytest.raises(EvolutionError, match="two different strategies"):
        combine(a, a, "tsmom_dup")


def test_combine_rejects_disagreeing_search_spaces_for_a_shared_param() -> None:
    """ADR-0069 §2: a shared ``param_search_space`` key must have exactly equal value lists."""
    a = _spec("tsmom")
    b = _spec("tsmom_b", param_search_space={"lookback": (10, 20, 50)})
    with pytest.raises(EvolutionError, match="search spaces"):
        combine(a, b, "tsmom_combo")


def test_combine_rejects_params_outside_the_combined_search_space() -> None:
    """ADR-0069 §2: a param carried over from one parent must still fit the *combined* space."""
    a = _spec("tsmom", params={"target": 5}, param_search_space={})
    b = _spec("volscaled", signals=(OTHER,), params={}, param_search_space={"target": (1, 2, 3)})
    with pytest.raises(EvolutionError, match="outside the combined search spaces"):
        combine(a, b, "tsmom_volscaled")


def test_combine_rejects_mismatched_risk_policy() -> None:
    """ADR-0069 §3: risk policies must match exactly; combine never infers or picks one."""
    a = _spec("tsmom", risk_policy=RISK_POLICY)
    b = _spec("volscaled", signals=(OTHER,), params={"target": 1}, param_search_space={})
    with pytest.raises(EvolutionError, match="risk_policy"):
        combine(a, b, "tsmom_volscaled")


def test_combine_rejects_two_different_risk_policies() -> None:
    """ADR-0069 §3: even two non-``None`` risk policies must be the exact same ref."""
    a = _spec("tsmom", risk_policy=RISK_POLICY)
    b = _spec(
        "volscaled",
        signals=(OTHER,),
        params={"target": 1},
        param_search_space={},
        risk_policy=OTHER_RISK_POLICY,
    )
    with pytest.raises(EvolutionError, match="risk_policy"):
        combine(a, b, "tsmom_volscaled")


def test_combine_rejects_mismatched_applicable_instruments() -> None:
    """ADR-0069 §4: applicable instrument sets must match exactly, no union/intersection/pick."""
    a = _spec("tsmom", applicable_instruments=(BTC,))
    b = _spec(
        "volscaled",
        signals=(OTHER,),
        params={"target": 1},
        param_search_space={},
        applicable_instruments=(ETH,),
    )
    with pytest.raises(EvolutionError, match="applicable_instruments"):
        combine(a, b, "tsmom_volscaled")


def test_combine_rejects_a_child_ref_colliding_with_a_parent() -> None:
    """A combined ``name@1.0.0`` that reuses a parent's own identity is refused, not silently
    treated as replacing the parent."""
    a = _spec("tsmom")
    b = _spec("volscaled", signals=(OTHER,), params={"target": 1}, param_search_space={})
    with pytest.raises(EvolutionError, match="collides"):
        combine(a, b, "tsmom")


def test_combine_accepts_matching_risk_policy_and_instruments() -> None:
    """ADR-0069: a pair that agrees on risk policy and instruments still combines cleanly."""
    a = _spec("tsmom", risk_policy=RISK_POLICY, applicable_instruments=(BTC,))
    b = _spec(
        "volscaled",
        signals=(OTHER,),
        params={"target": 1},
        param_search_space={},
        risk_policy=RISK_POLICY,
        applicable_instruments=(BTC,),
    )
    child = combine(a, b, "tsmom_volscaled")
    assert child.spec.risk_policy == RISK_POLICY
    assert child.spec.applicable_instruments == (BTC,)


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
