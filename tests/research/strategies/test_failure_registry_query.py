"""Failure Registry retrieval: ``query`` and the per-``reason_code`` tally (read only).

docs/research/failure-registry.md: the registry is searched before a new hypothesis is registered
and keeps a failure-mode tally. Both helpers read through ``records`` (corruption still fails
closed) and never write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.domain.base import Ref
from core.domain.research import FailureRecord
from core.errors import ReasonCode
from research.strategies.cross_sectional_momentum import xsmom_spec
from research.strategies.failure_registry import FailureRegistry, FailureRegistryCorrupted
from research.strategies.time_series_momentum import tsmom_spec, tsmom_vol_scaled_spec


def _record(
    spec_ref: Ref, state: str, reason: ReasonCode, family: str, gate: str | None
) -> FailureRecord:
    return FailureRecord(
        subject_ref=spec_ref,
        terminal_state=state,
        reason_code=reason,
        gate_id=gate,
        hypothesis_family_id=family,
    )


@pytest.fixture
def registry(tmp_path: Path) -> FailureRegistry:
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    for record in (
        _record(tsmom_spec().ref, "REJECTED", ReasonCode.COST_KILLED, "tsmom_bars", "G2.x"),
        _record(tsmom_spec().ref, "FAILED", ReasonCode.NOT_REPRODUCIBLE, "tsmom_bars", "G0.y"),
        _record(xsmom_spec().ref, "REJECTED", ReasonCode.COST_KILLED, "xsmom_bars", "G2.x"),
        _record(xsmom_spec().ref, "REJECTED", ReasonCode.OOS_DECAY, "xsmom_bars", None),
    ):
        registry.append(record)
    return registry


def test_no_filter_returns_every_record_in_append_order(registry: FailureRegistry) -> None:
    assert registry.query() == registry.records()
    assert len(registry.query()) == 4


def test_each_filter_matches_exactly(registry: FailureRegistry) -> None:
    by_subject = registry.query(subject=tsmom_spec().ref)
    assert [r.reason_code for r in by_subject] == [
        ReasonCode.COST_KILLED,
        ReasonCode.NOT_REPRODUCIBLE,
    ]
    assert registry.query(subject=tsmom_vol_scaled_spec().ref) == ()
    assert {r.subject_ref for r in registry.query(family="xsmom_bars")} == {xsmom_spec().ref}
    assert len(registry.query(reason=ReasonCode.COST_KILLED)) == 2
    assert len(registry.query(terminal_state="FAILED")) == 1
    assert len(registry.query(gate_id="G2.x")) == 2


def test_filters_combine(registry: FailureRegistry) -> None:
    (record,) = registry.query(family="xsmom_bars", reason=ReasonCode.COST_KILLED)
    assert record.subject_ref == xsmom_spec().ref and record.gate_id == "G2.x"
    assert registry.query(family="tsmom_bars", reason=ReasonCode.OOS_DECAY) == ()


def test_a_terminal_state_outside_the_registry_is_refused(registry: FailureRegistry) -> None:
    with pytest.raises(ValueError, match="REJECTED / FAILED"):
        registry.query(terminal_state="RETIRED")


def test_reason_counts_tally_every_record(registry: FailureRegistry) -> None:
    counts = registry.reason_counts()
    assert counts == {
        ReasonCode.COST_KILLED.value: 2,
        ReasonCode.NOT_REPRODUCIBLE.value: 1,
        ReasonCode.OOS_DECAY.value: 1,
    }
    assert list(counts) == sorted(counts)
    assert sum(counts.values()) == len(registry.records())


def test_an_empty_registry_has_no_records_and_no_counts(tmp_path: Path) -> None:
    empty = FailureRegistry(tmp_path / "none.jsonl")
    assert empty.query() == () and empty.reason_counts() == {}


def test_retrieval_fails_closed_on_rewritten_history(registry: FailureRegistry) -> None:
    path = registry.path
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    with pytest.raises(FailureRegistryCorrupted):
        registry.query()
    with pytest.raises(FailureRegistryCorrupted):
        registry.reason_counts()
