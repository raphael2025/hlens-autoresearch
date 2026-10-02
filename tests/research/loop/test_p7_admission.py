"""ADR-0103 D2 / D3: a declared P7 plan is admitted into the durable Research Loop through the
ADR-0073 PREPARE / COMMIT journal, or rejected (audited) before anything is written.

Every number is TEST ONLY (``loop_fixtures``). The plan is ``negation(tsmom_bars@1.0.0)``: a
strategy root the single-instrument loop can run.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.domain.research import HypothesisOrigin
from core.domain.specs import InstrumentType
from infrastructure.event_bus import InMemoryEventBus
from plugins.synthetic import RandomWalkMarket
from research.hypotheses.p7_binding import (
    P7_PLAN_KEY,
    P7PlanRecord,
    bind_experiment,
    p7_plan_condition,
    plan_dependency_hashes,
    recorded_p7_plan,
)
from research.hypotheses.p7_evidence import experiment_evidence, inputs_evidence, outputs_evidence
from research.hypotheses.typed_plan import TypedPlan, parse_plan_json
from research.hypotheses.typed_plan_audit import (
    CommittedAdmission,
    PlanAdmissionJournal,
    RoundStartedIdentity,
)
from research.hypotheses.typed_plan_compiler import (
    P7_OPERATOR_ALLOWLIST,
    CompiledPlan,
    P7ExecutionSwitch,
    PlanCompileRefused,
)
from research.hypotheses.typed_plan_resolver import resolve_direct_references
from research.loop import LoopStateInconsistent, build_synthetic_loop, open_synthetic_loop
from research.loop.compose import DurableLoop, loop_fingerprint
from research.loop.durable import PLAN_ADMISSION_FILE
from research.loop.memory import ResearchMemory
from research.loop.p7_admission import (
    P7PlanRequest,
    P7PlanSource,
    P7RoundAdmission,
    rejected_plans,
)
from research.loop.p7_plan import p7_strategy_candidates
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import StrategyCandidate
from tests.research.hypotheses.p7_fixtures import (
    SOURCE_A,
    cross_sectional_nodes,
    experiment_for,
    spec_input,
)
from tests.research.hypotheses.p7_fixtures import (
    compile_nodes as compile_fixture_nodes,
)
from tests.research.hypotheses.p7_fixtures import (
    parse as parse_fixture_nodes,
)
from tests.research.loop import loop_fixtures as fx
from tests.research.loop.p7_loop_fixtures import (
    CREATED,
    ENABLED,
    LIMITS,
    TSMOM,
    Resolver,
    compiled_plan,
    negation_plan,
    plan_hypothesis,
)
from tests.test_universe_contracts import manifest


def _request(**hypothesis_changes: Any) -> P7PlanRequest:
    plan = negation_plan()
    compiled = compiled_plan(plan)
    hypothesis = plan_hypothesis(compiled, **hypothesis_changes)
    experiment = experiment_for(compiled, hypothesis, with_record=False, bind_outputs=False)
    return P7PlanRequest(
        plan=plan,
        resolution=resolve_direct_references(plan, resolver=Resolver()),
        created_at=CREATED,
        hypotheses=(hypothesis,),
        experiment_specs=(experiment,),
        strategy_providers={str(TSMOM.spec.ref): TSMOM.strategy},
        instrument_type=InstrumentType.PERPETUAL,
    )


def _source(*plans: P7PlanRequest, switch: P7ExecutionSwitch = ENABLED) -> P7PlanSource:
    return P7PlanSource(switch=switch, allowlist=dict(P7_OPERATOR_ALLOWLIST), plans=plans)


def _config(source: P7PlanSource | None) -> Any:
    return fx.config(
        lookbacks=(60,),
        loop_wiring=replace(fx.wiring(evolution=False), p7_plans=source),
    )


def _open(state_dir: Path, source: P7PlanSource | None) -> DurableLoop:
    return open_synthetic_loop(
        _config(source), state_dir=state_dir, provider=RandomWalkMarket(), bus=InMemoryEventBus()
    )


def _hypothesis_summary(durable: DurableLoop) -> Any:
    record = durable.loop.audit.records[-1]
    return next(stage for stage in record.stages if stage.name == "hypothesis").summary


def _journal_events(state_dir: Path) -> list[str]:
    lines = (state_dir / PLAN_ADMISSION_FILE).read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["type"] for line in lines]


# ------------------------------------------------------------------------------- admission


def test_a_declared_plan_is_admitted_committed_and_run_with_its_binding(tmp_path: Path) -> None:
    request = _request()
    compiled = compiled_plan(request.plan)
    record = P7PlanRecord.from_compiled(compiled)
    (hypothesis,) = request.hypotheses
    state_dir = tmp_path / "state"
    durable = _open(state_dir, _source(request))
    try:
        assert "p7_plans" in loop_fingerprint(_config(_source(request)))
        durable.loop.run_unattended(1)
        summary = _hypothesis_summary(durable)

        admitted = summary["p7_plan"]
        assert admitted["plan_hash"] == request.plan_hash
        assert admitted["registered"] == [str(hypothesis.ref)]
        assert admitted["candidate"] == str(compiled.root.spec.ref)
        assert summary["p7_plan_rejection"] is None
        assert summary["rejected_plans"] == []
        # the admitted hypothesis is this round's first registered trial
        assert summary["registered"][0] == str(hypothesis.ref)
        assert summary["hypothesis_hashes"][0] == hypothesis.content_hash()
        assert durable.memory.ledger.is_registered(hypothesis)
        assert _journal_events(state_dir) == [
            "plan_admission_header",
            "plan_admission_prepare",
            "plan_admission_commit",
        ]

        candidate = durable.memory.strategies[str(compiled.root.spec.ref)]
        assert candidate.plan_record == record
        (trial,) = [o for o in durable.memory.trials if o.hypothesis.ref == hypothesis.ref]
        assert trial.origin == "p7_plan" and trial.candidate is candidate
        assert trial.error is None and trial.trial is not None  # the plan's root really ran
        repro = trial.run.repro
        assert recorded_p7_plan(repro.params) == record
        for ref, digest in plan_dependency_hashes(record).items():
            assert repro.dependency_hashes[ref] == digest
        assert P7_PLAN_KEY not in trial.summary["params"]
        assert trial.summary["origin"] == "p7_plan"
        # a fully registered plan is not pending any more
        admission = P7RoundAdmission(
            _source(request), durable.durable_state, loop_id="synthetic_loop", family_id=fx.FAMILY
        )
        assert admission.pending() is None and admission.trials() == 0
    finally:
        durable.close()
    # Known limit (ADR-0103 实现记录): restoring a P7 candidate after a restart is not
    # specified, so the directory is refused on reopening (fail closed, nothing restored).
    with pytest.raises(LoopStateInconsistent, match="cannot be rebuilt"):
        _open(state_dir, _source(request))


def test_a_rejected_plan_writes_nothing_and_is_recorded(tmp_path: Path) -> None:
    request = _request(origin=HypothesisOrigin.LLM)
    state_dir = tmp_path / "state"
    durable = _open(state_dir, _source(request))
    try:
        durable.loop.run_unattended(1)
        summary = _hypothesis_summary(durable)
        assert summary["p7_plan"] is None
        assert summary["p7_plan_rejection"] == {
            "plan_hash": request.plan_hash,
            "code": "llm_hypothesis_not_admitted",
            "where": "hypotheses[0]",
        }
        assert summary["rejected_plans"] == [request.plan_hash]
        assert not durable.memory.ledger.is_registered(request.hypotheses[0])
        assert str(request.hypotheses[0].ref) not in summary["registered"]
        assert _journal_events(state_dir) == ["plan_admission_header"]
        assert rejected_plans(durable.loop.audit.records) == [request.plan_hash]
        admission = P7RoundAdmission(
            _source(request), durable.durable_state, loop_id="synthetic_loop", family_id=fx.FAMILY
        )
        assert admission.pending() is None  # never offered again
    finally:
        durable.close()


# ------------------------------------------------------------------------------- refusals


def _rejection(tmp_path: Path, request: P7PlanRequest, **source: Any) -> tuple[str, str]:
    durable = _open(tmp_path / "state", None)
    try:
        assert durable.durable_state is not None
        admission = P7RoundAdmission(
            _source(request, **source),
            durable.durable_state,
            loop_id="synthetic_loop",
            family_id=fx.FAMILY,
        )
        outcome = admission.run(0)
        assert outcome is not None and outcome.admission is None and outcome.candidate is None
        assert outcome.hypotheses == ()
        assert outcome.code is not None and outcome.where is not None
        assert _journal_events(tmp_path / "state") == ["plan_admission_header"]
        assert not durable.memory.ledger.hypotheses
        return outcome.code, outcome.where
    finally:
        durable.close()


def test_the_default_off_switch_rejects_every_plan(tmp_path: Path) -> None:
    code, _ = _rejection(tmp_path, _request(), switch=P7ExecutionSwitch())
    assert code == "execution_disabled"


def test_a_cross_sectional_plan_is_refused_entering_the_loop(tmp_path: Path) -> None:
    universe = manifest()
    compiled, resolution = compile_fixture_nodes(
        cross_sectional_nodes(universe), "xs", universes=(universe,)
    )
    hypothesis = plan_hypothesis(compiled, conditions=(p7_plan_condition(compiled.plan_hash),))
    request = P7PlanRequest(
        plan=compiled.plan,
        resolution=resolution,
        created_at=CREATED,
        hypotheses=(hypothesis,),
        experiment_specs=(experiment_for(compiled, hypothesis, with_record=False),),
        universes=(universe,),
    )
    assert _rejection(tmp_path, request) == ("cross_sectional_loop_unsupported", "xs")


def _difference_nodes() -> list[dict[str, Any]]:
    return [
        {
            "id": "diff",
            "operator": "transformation",
            "inputs": [spec_input(SOURCE_A)],
            "parameters": {"transform": "difference", "window": 5},
        }
    ]


def test_a_feature_root_is_refused(tmp_path: Path) -> None:
    compiled, resolution = compile_fixture_nodes(_difference_nodes(), "diff")
    hypothesis = plan_hypothesis(compiled)
    request = P7PlanRequest(
        plan=compiled.plan,
        resolution=resolution,
        created_at=CREATED,
        hypotheses=(hypothesis,),
        experiment_specs=(experiment_for(compiled, hypothesis, with_record=False),),
    )
    assert _rejection(tmp_path, request) == ("root_not_strategy", "diff")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"origin": HypothesisOrigin.LLM}, "llm_hypothesis_not_admitted"),
        ({"family": "another_family"}, "foreign_family"),
        (
            {"conditions": ("strategy = tsmom_bars@1.0.0", p7_plan_condition("a" * 64))},
            "hypothesis_plan_condition_mismatch",
        ),
    ],
)
def test_hypotheses_that_cannot_run_the_plan_are_refused(
    tmp_path: Path, changes: dict[str, Any], code: str
) -> None:
    assert _rejection(tmp_path, _request(**changes))[0] == code


def test_a_hypothesis_naming_another_strategy_is_refused(tmp_path: Path) -> None:
    compiled = compiled_plan(negation_plan())
    conditions = ("strategy = tsmom_bars@1.0.0", p7_plan_condition(compiled.plan_hash))
    assert _rejection(tmp_path, _request(conditions=conditions)) == (
        "hypothesis_strategy_mismatch",
        "hypotheses[0]",
    )


def test_an_experiment_that_binds_another_hypothesis_is_refused(tmp_path: Path) -> None:
    request = _request()
    compiled = compiled_plan(request.plan)
    other = plan_hypothesis(compiled, name="h_other")
    foreign = replace(
        request,
        experiment_specs=(experiment_for(compiled, other, with_record=False, bind_outputs=False),),
    )
    code, _ = _rejection(tmp_path, foreign)
    assert code == "unknown_hypothesis"


def test_a_missing_input_provider_is_refused(tmp_path: Path) -> None:
    code, _ = _rejection(tmp_path, replace(_request(), strategy_providers={}))
    assert code == "missing_input_provider"


# ------------------------------------------------------------------------------- composition


def test_p7_plans_need_a_durable_state(tmp_path: Path) -> None:
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    with pytest.raises(ValueError, match="durable state directory"):
        build_synthetic_loop(
            _config(_source(_request())),
            provider=RandomWalkMarket(),
            bus=InMemoryEventBus(),
            memory=memory,
        )


def test_a_plan_candidate_in_the_static_catalog_is_refused(tmp_path: Path) -> None:
    compiled = compiled_plan(negation_plan())
    candidate = StrategyCandidate(
        spec=compiled.root.spec,
        strategy=TSMOM.strategy,
        hypothesis_family_id=fx.FAMILY,
        plan_record=P7PlanRecord.from_compiled(compiled),
    )
    wiring = replace(fx.wiring(evolution=False), strategies=(TSMOM, candidate))
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    with pytest.raises(ValueError, match="p7_plans"):
        build_synthetic_loop(
            fx.config(loop_wiring=wiring),
            provider=RandomWalkMarket(),
            bus=InMemoryEventBus(),
            memory=memory,
        )


def test_without_p7_plans_nothing_is_fingerprinted() -> None:
    assert "p7_plans" not in loop_fingerprint(fx.config())
    assert fx.wiring().p7_plans is None


def test_the_source_and_request_validate_their_declaration() -> None:
    request = _request()
    with pytest.raises(ValueError, match="twice"):
        _source(request, request)
    with pytest.raises(ValueError, match="P7ExecutionSwitch"):
        P7PlanSource(switch=True, allowlist={}, plans=(request,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="another plan"):
        replace(request, resolution=parse_fixture_nodes(_difference_nodes(), "diff")[1])
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(request, created_at=datetime(2024, 1, 1))
    with pytest.raises(ValueError, match="non-empty tuple"):
        replace(request, hypotheses=())
    payload = _source(request).payload()
    assert payload["execution_enabled"] is True
    assert payload["plans"][0]["plan_hash"] == request.plan_hash
    assert _source(request).payload() == payload  # deterministic


# ------------------------------------------------------------------- the candidate (D1 / D3)


def _committed(tmp_path: Path, compiled: CompiledPlan, round_index: int) -> CommittedAdmission:
    """A COMMIT of ``compiled`` in ``round_index`` (journal PREPARE, test-built COMMIT record)."""
    request = _request()
    hypothesis = request.hypotheses[0]
    bound = bind_experiment(request.experiment_specs[0], P7PlanRecord.from_compiled(compiled))
    journal = PlanAdmissionJournal(tmp_path / "admission.jsonl", loop_id="loop", create=True)
    prepared = journal.prepare(
        round=RoundStartedIdentity("loop", round_index, 1, "a" * 64),
        plan=compiled.plan,
        compiler=compiled.compiler_evidence(),
        operators=compiled.operator_evidence(),
        providers=compiled.provider_evidence(),
        inputs=inputs_evidence(request.resolution),
        outputs=outputs_evidence(compiled),
        experiment_specs=experiment_evidence([bound]),
        hypotheses=[hypothesis],
        ledger_baseline_seq=0,
        ledger_baseline_hash="0" * 64,
    )
    return CommittedAdmission(
        prepare=prepared,
        seq=3,
        entry_hash="b" * 64,
        ledger_event_seq=1,
        ledger_event_prev_hash="0" * 64,
        ledger_event_hash="c" * 64,
    )


def _providers(compiled: CompiledPlan) -> dict[str, Any]:
    return compiled.build_providers(
        strategy_providers={str(TSMOM.spec.ref): TSMOM.strategy},
        instrument_type=InstrumentType.PERPETUAL,
    )


def test_the_candidate_requires_this_rounds_commit_of_this_plan(tmp_path: Path) -> None:
    compiled = compiled_plan(negation_plan())
    providers = _providers(compiled)
    committed = _committed(tmp_path, compiled, round_index=2)
    common: dict[str, Any] = {"switch": ENABLED, "hypothesis_family_id": fx.FAMILY}

    (candidate,) = p7_strategy_candidates(
        compiled, providers, admission=committed, round_index=2, **common
    )
    assert candidate.plan_record == P7PlanRecord.from_compiled(compiled)
    assert candidate.spec == compiled.root.spec

    def refused(**changes: Any) -> str:
        kwargs = {"admission": committed, "round_index": 2, **common, **changes}
        with pytest.raises(PlanCompileRefused) as caught:
            p7_strategy_candidates(compiled, providers, **kwargs)
        return caught.value.code

    assert refused(admission=None) == "admission_missing"
    assert refused(round_index=3) == "admission_not_this_round"
    assert refused(switch=P7ExecutionSwitch()) == "execution_disabled"
    other = _committed(tmp_path / "other", compiled_plan(_other_negation_plan()), round_index=2)
    assert refused(admission=other) == "admission_plan_mismatch"


def _other_negation_plan() -> TypedPlan:
    """The same negation with another node id: another plan hash, another lowered spec."""
    payload = {
        "schema_version": "1.3.0",
        "root": "flip",
        "nodes": [
            {
                "id": "flip",
                "operator": "negation",
                "inputs": [spec_input(TSMOM.spec)],
                "parameters": {},
            }
        ],
    }
    return parse_plan_json(json.dumps(payload, separators=(",", ":")), limits=LIMITS)


def test_a_cross_sectional_plan_never_becomes_a_candidate() -> None:
    universe = manifest()
    nodes = cross_sectional_nodes(universe)
    compiled, _ = compile_fixture_nodes(nodes, "xs", universes=(universe,))
    with pytest.raises(PlanCompileRefused) as caught:
        p7_strategy_candidates(compiled, {}, switch=ENABLED, hypothesis_family_id=fx.FAMILY)
    assert caught.value.code == "cross_sectional_loop_unsupported"
