"""Phase 7: an optional declared hypothesis batch as a source of the loop's hypothesis stage.

The whole batch is pre-registered before any of its trials runs, every cell runs as the trial
point it declares, and a batch the loop cannot run is refused when the loop is composed. Without a
batch every record and fingerprint is unchanged (pinned:
``test_loop_e2e.test_records_without_a_conditional_plan_are_pinned``).

TEST ONLY numbers: see ``loop_fixtures``.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.worker import LoopBudget, StageStatus
from core.domain.base import content_hash
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.hypotheses import BatchGrid, BatchOperator, ReviewedOperators, expand_batch
from research.hypotheses.batch import HypothesisBatch
from research.loop import (
    ResearchMemory,
    build_synthetic_loop,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from tests.research.loop import loop_fixtures as fx

TSMOM = library_entries()[0].candidate().spec
POINT = BatchOperator(
    name="tsmom_point",
    version="1.0.0",
    kind="parameter_point",
    claim="time-series momentum at this point beats costs (test only)",
    expected_direction="higher",
)
REVIEWED = ReviewedOperators(
    name="test_only_operators", version="1.0.0", reviewer="alice", operators=(POINT,)
)
POINTS: tuple[dict[str, Any], ...] = (
    {"lookback": 60, "long_only": True},
    {"lookback": 240},
    {"lookback": 1440, "long_only": False},
)
#: TEST ONLY: room for the fresh knowledge hypothesis and the three cells in one round.
ROOMY = LoopBudget(
    max_trials_per_round=6,
    max_trials_total=20,
    max_llm_cost_units=Decimal(10),
    max_compute_seconds=Decimal(1000),
)


def _batch(**changes: Any) -> HypothesisBatch:
    grid: dict[str, Any] = {
        "name": "b_tsmom",
        "family_id": fx.FAMILY,
        "created_at": fx.T0,
        "operators": (POINT,),
        "strategies": (TSMOM,),
        "points": POINTS,
        "minimum_meaningful_effect": "net mean return above costs (test only)",
    }
    return expand_batch(BatchGrid(**{**grid, **changes}), REVIEWED)


def _config(batch: HypothesisBatch | None, budget: LoopBudget = ROOMY) -> Any:
    wiring = replace(fx.wiring(evolution=False), hypothesis_batch=batch)
    return fx.config(budget=budget, loop_wiring=wiring)


def _llm(consumed: int = 0) -> ScriptedLLMProvider:
    """The scripted LLM, resumed after ``consumed`` calls (its position is not loop state)."""
    outputs = [fx.llm_output(i, None) for i in range(3)]
    return ScriptedLLMProvider(outputs[consumed:], clock=lambda: fx.T0)


def _loop(tmp: Path, config: Any) -> tuple[Any, ResearchMemory]:
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    loop = build_synthetic_loop(
        config, provider=RandomWalkMarket(), bus=InMemoryEventBus(), memory=memory, llm=_llm()
    )
    return loop, memory


def _stage(record: Any, name: str) -> Any:
    return next(stage for stage in record.stages if stage.name == name)


def test_the_whole_batch_is_preregistered_and_every_cell_runs_as_declared(
    tmp_path: Path,
) -> None:
    batch = _batch()
    loop, memory = _loop(tmp_path, _config(batch))
    [record] = loop.run_unattended(1)
    hypothesis = _stage(record, "hypothesis")
    cells = [str(h.ref) for h in batch.hypotheses]
    assert hypothesis.summary["registered"][:3] == cells  # the batch first, then the knowledge
    assert hypothesis.summary["family_trials"] == 4  # counted before any trial ran
    assert hypothesis.usage.trials == 4 == hypothesis.estimate.trials
    assert hypothesis.summary["batch"] == {
        "grid": "b_tsmom",
        "grid_hash": batch.grid.content_hash(),
        "allowlist": "test_only_operators@1.0.0",
        "allowlist_hash": REVIEWED.content_hash(),
        "reviewer": "alice",
        "declared_trials": 3,
        "pre_registered": cells,
    }
    ran = {str(o.hypothesis.ref): o for o in memory.trials}
    for cell, point in zip(batch.hypotheses, POINTS, strict=True):
        outcome = ran[str(cell.ref)]
        assert outcome.error is None  # never an ERRORED trial
        assert {k: outcome.request_params[k] for k in point} == point
    transitions = [t for h in loop.guard.histories for t in h.transitions]
    evidence = {e for t in transitions for e in t.evidence}
    assert f"reviewed_operators:{REVIEWED.key}#{REVIEWED.content_hash()}" in evidence


def test_a_batch_beyond_the_round_budget_registers_nothing(tmp_path: Path) -> None:
    loop, memory = _loop(tmp_path, _config(_batch(), budget=fx.TEST_ONLY_BUDGET))  # 3 per round
    [record] = loop.run_unattended(1)
    refused = _stage(record, "hypothesis")
    assert refused.status is StageStatus.REFUSED_BUDGET
    assert memory.ledger.trials(fx.FAMILY) == 0  # all or nothing: no cell was registered


def test_a_batch_the_loop_cannot_run_is_refused_when_composed(tmp_path: Path) -> None:
    other_version = TSMOM.model_copy(update={"version": "9.0.0"})  # not in the catalog
    edited = TSMOM.model_copy(update={"params": {"lookback": 60, "long_only": False}})
    for batch, match in (
        (_batch(strategies=(other_version,)), "not in the loop's catalog"),
        (_batch(strategies=(edited,)), "not in the loop's catalog"),  # same ref, other spec
        (_batch(family_id="another_family"), "family"),
    ):
        with pytest.raises(ValueError, match=match):
            _loop(tmp_path, _config(batch))


def test_the_fingerprint_binds_the_batch_only_when_set() -> None:
    plain = loop_fingerprint(_config(None))
    assert "hypothesis_batch" not in plain
    batch = _batch()
    bound = loop_fingerprint(_config(batch))
    assert bound.pop("hypothesis_batch") == batch.payload()
    assert content_hash(bound) == content_hash(plain)


def test_a_restarted_durable_loop_does_not_register_the_batch_again(tmp_path: Path) -> None:
    config = _config(_batch())

    def _open(consumed: int) -> Any:
        return open_synthetic_loop(
            config,
            state_dir=tmp_path / "state",
            provider=RandomWalkMarket(),
            bus=InMemoryEventBus(),
            llm=_llm(consumed),
        )

    first = _open(0)
    first.loop.run_unattended(1)
    trials = first.memory.ledger.trials(fx.FAMILY)
    assert trials == 4
    first.close()
    second = _open(1)
    [record] = second.loop.run_unattended(1)
    summary = _stage(record, "hypothesis").summary
    assert summary["batch"]["pre_registered"] == []
    assert not set(summary["registered"]) & {str(h.ref) for h in _batch().hypotheses}
    second.close()
