"""ADR-0083 "PM 决定" §1 (2026-09-28): a retry round runs only the admitted trials — evolution
never runs in it, due or not, so it never registers offspring the retry's own budget check (G2)
never accounted for.

The scenario below proves the skip actually matters: the same ``ResearchMemory``, after one real
round, has an eligible (INCONCLUSIVE) parent an ``EvolutionStage`` with ``every_rounds=1`` would
otherwise mutate on the very next round. Run as a retry round (its hypothesis-stage artifacts carry
the ``retry_reevaluations`` key ``research.loop.stages.HypothesisStage._run_retry`` always sets), it
registers nothing; run as an ordinary round with the same memory, it does — showing the guard, not a
missing candidate, is what suppressed it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.worker import RoundStatus
from apps.worker.loop import LifecycleGuard, RoundContext, StageUsage
from core.domain.research import Verdict
from research.loop.evolution import EvolutionStage
from tests.research.loop import loop_fixtures as fx


def _ctx(guard: LifecycleGuard, artifacts: dict[str, Any]) -> RoundContext:
    return RoundContext(
        loop_id="synthetic_loop",
        round_index=1,
        seed=0,
        as_of=fx.T0,
        _guard=guard,
        artifacts=artifacts,
    )


def test_a_retry_round_skips_evolution_and_registers_no_offspring(tmp_path: Path) -> None:
    loop, memory, _ = fx.build(
        tmp_path, fx.config(lookbacks=(60,), loop_wiring=fx.wiring(evolution=False))
    )
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    [validated] = memory.validations
    assert validated.verdict is Verdict.INCONCLUSIVE and validated.outcome.candidate is not None
    assert memory.offspring == []  # nothing has evolved yet: a real eligible parent is available

    plan = fx.wiring(evolution=True).evolution
    assert plan is not None  # TEST ONLY: every_rounds=1, parents_per_round=1 (loop_fixtures)
    stage = EvolutionStage(memory, plan)
    guard = LifecycleGuard("research_loop:test_evolution_retry_skip")
    hypothesis = validated.outcome.hypothesis

    # A retry round: exactly what HypothesisStage._run_retry's artifacts look like.
    retry_ctx = _ctx(guard, {"hypothesis": {"retry_reevaluations": ((hypothesis, "retry-1"),)}})
    assert stage.estimate(retry_ctx) == StageUsage()
    result = stage.run(retry_ctx)
    assert result.summary == {"due": False, "offspring": []}
    assert result.usage == StageUsage()
    assert result.artifacts == {"registered": ()}
    assert memory.offspring == []  # nothing registered
    assert guard.histories == ()  # no lifecycle move either

    # The same round index, the same memory, but a normal (non-retry) hypothesis-stage artifact
    # shape: every_rounds=1 makes this due, and the parent above is still eligible.
    normal_ctx = _ctx(
        guard, {"hypothesis": {"registered": (), "reevaluations": (), "llm_calls": {}}}
    )
    assert stage.estimate(normal_ctx).trials == 1
    result = stage.run(normal_ctx)
    assert result.summary["due"] is True
    assert len(result.summary["offspring"]) == 1
    assert memory.offspring  # proves the retry round above really would have evolved otherwise
