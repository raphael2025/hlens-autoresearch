"""Phase 10 code completion: explicit stop, run-hash coverage (tamper), report binding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

import pytest

from core.contracts.state import StateInput, StateProviderDescriptor, StateRequest, StateResult
from core.contracts.strategy import StrategyResult
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.lifecycle.strategy import LifecycleState
from plugins.backtest import BarBacktester
from research.router import (
    RouterError,
    RouterPaperRun,
    RouterSpec,
    RouterStop,
    RouterStopped,
    StrategyRouter,
    paper_run,
    paper_run_or_stop,
)
from tests.research.router.test_paper import (
    BARS,
    LIFECYCLE,
    STATE,
    STATES,
    STRATEGIES,
    ZERO,
    A,
    B,
    _router,
    _run,
    _strategy,
)

#: ``_run()``'s run hash before this change: an omitted ``validation_reports`` keeps it.
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): 0744ad26…
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0
#: values still hold when the test builds every object at 2.1.0 (verified: the unmodified
#: test passes inside ``contract_schema_version_scope("2.1.0")``).
#: 2.1.0 values (evidence, git history): 7f30d3d4…
#: Re-pinned for contract 2.4.0 (ADR-0088, 2026-09-28): the default envelope is part of the
#: RouterPaperRun identity; historical versions remain covered by contract-version replay tests.
BASELINE_RUN_HASH = "3042183e2e74b2249ab7eebe751979bbd677a7bd6a687be4659e60c19d37f681"
REPORT_A = content_hash({"validation_report": "trend_a"})
REPORT_B = content_hash({"validation_report": "revert_b"})
STATE_SPEC_HASH = content_hash({"fixture": "state"})  # as test_paper._states


def _spec(table: Mapping[str, Mapping[str, Decimal]], fallback: dict[str, Decimal]) -> RouterSpec:
    return RouterSpec(
        name="vol_router",
        version="1.0.0",
        table=table,
        fallback=fallback,
        switching_cost_rate=Decimal("0.01"),
    )


SPEC = _router().spec
FLAT = _spec({"calm": {}, "wild": {str(A): Decimal(0)}}, {})


def _paper(
    router: StrategyRouter | None = None,
    states: StateResult = STATES,
    strategies: Mapping[Ref, StrategyResult] | None = None,
    validation_reports: Mapping[Ref, str] | Mapping[str, str] | None = None,
) -> RouterPaperRun:
    return paper_run(
        router or _router(),
        states,
        STRATEGIES if strategies is None else strategies,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
        validation_reports=validation_reports,
    )


def _or_stop(spec: RouterSpec, lifecycle: dict[Ref, LifecycleState]) -> RouterPaperRun | RouterStop:
    return paper_run_or_stop(
        spec,
        lifecycle,
        STATES,
        STRATEGIES,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
    )


# ------------------------------------------------------------------------------ (a) explicit stop


@pytest.mark.parametrize(
    "lifecycle",
    [{}, {A: LifecycleState.VALIDATION, B: LifecycleState.CANDIDATE}],
    ids=["empty", "none_validated"],
)
def test_no_validated_candidate_stops_the_router(lifecycle: dict[Ref, LifecycleState]) -> None:
    with pytest.raises(RouterStopped) as caught:
        StrategyRouter(SPEC, lifecycle)
    assert caught.value.reason == "no_validated_candidate"
    assert isinstance(caught.value, RouterError)
    assert caught.value.spec_hash == SPEC.spec_hash() and caught.value.router == "vol_router@1.0.0"


def test_every_state_and_the_fallback_flat_stops_the_router() -> None:
    with pytest.raises(RouterStopped) as caught:
        StrategyRouter(FLAT, LIFECYCLE)
    assert caught.value.reason == "all_routes_flat"
    with pytest.raises(RouterStopped, match="all_routes_flat"):
        StrategyRouter(_spec({}, {}), LIFECYCLE)


def test_per_state_flat_routing_is_kept_when_a_validated_candidate_is_routed() -> None:
    router = StrategyRouter(_spec({"calm": {str(A): Decimal(1)}, "wild": {}}, {}), LIFECYCLE)
    run = _paper(router, strategies={A: STRATEGIES[A]})
    assert [d.weights for d in run.decisions] == [
        {str(A): Decimal(1)},
        {str(A): Decimal(1)},
        {},
        {},
        {},
    ]
    run.verify()


def test_an_unvalidated_route_is_still_a_plain_error_not_a_stop() -> None:
    with pytest.raises(RouterError, match="unvalidated") as caught:
        StrategyRouter(SPEC, {A: LifecycleState.ACTIVE, B: LifecycleState.VALIDATION})
    assert not isinstance(caught.value, RouterStopped)
    with pytest.raises(RouterError, match="unvalidated"):
        _or_stop(SPEC, {A: LifecycleState.ACTIVE, B: LifecycleState.VALIDATION})


def test_the_paper_run_records_a_stop_bound_to_its_inputs() -> None:
    stop = _or_stop(FLAT, LIFECYCLE)
    assert isinstance(stop, RouterStop)
    assert stop.reason == "all_routes_flat" and stop.router_spec_hash == FLAT.spec_hash()
    assert stop.state_result_hash == STATES.result_hash
    assert stop.strategy_result_hashes == {str(k): v.result_hash for k, v in STRATEGIES.items()}
    assert stop.lifecycle == {str(A): "ACTIVE", str(B): "PRODUCTION_CANDIDATE"}
    assert stop.validation_reports is None
    assert stop == _or_stop(FLAT, LIFECYCLE)  # deterministic
    other = _or_stop(FLAT, {A: LifecycleState.VALIDATION})
    assert isinstance(other, RouterStop) and other.reason == "no_validated_candidate"
    assert other.stop_hash != stop.stop_hash
    assert other.lifecycle == {str(A): LifecycleState.VALIDATION.value}


def test_paper_run_or_stop_is_paper_run_when_the_router_can_route() -> None:
    run = _or_stop(SPEC, LIFECYCLE)
    assert isinstance(run, RouterPaperRun)
    assert run == _run() and run.run_hash == BASELINE_RUN_HASH


# ------------------------------------------------------------------ (b) run hash coverage, tamper


def test_an_omitted_report_mapping_keeps_the_existing_run_hash() -> None:
    run = _run()
    assert run.validation_reports is None
    assert run.run_hash == BASELINE_RUN_HASH == run.expected_run_hash()
    run.verify()


def test_every_recorded_identity_is_covered_by_the_run_hash() -> None:
    run = _run()
    other_hash = content_hash({"tampered": True})
    first, *rest = run.decisions
    tampered = [
        replace(run, router="other_router@1.0.0"),
        replace(run, router_spec_hash=other_hash),
        replace(run, state_result_hash=other_hash),
        replace(run, strategy_result_hashes={**run.strategy_result_hashes, str(A): other_hash}),
        replace(run, decisions=tuple(rest)),
        replace(run, decisions=(replace(first, state="wild"), *rest)),
        replace(run, charges=run.charges[1:]),
        replace(run, validation_reports={str(A): REPORT_A, str(B): REPORT_B}),
    ]
    for record in tampered:
        assert record.expected_run_hash() != run.run_hash
        with pytest.raises(RouterError, match="run_hash does not match"):
            record.verify()
    with pytest.raises(RouterError, match="do not answer"):
        replace(run, request=run.request.model_copy(update={"initial_equity": Decimal(1)})).verify()
    with pytest.raises(RouterError, match="run_hash does not match"):
        replace(run, result=run.gross).verify()  # same request, other result


def test_the_routing_table_is_bound_through_the_spec_hash() -> None:
    changed = _spec({"calm": {str(A): Decimal(1)}, "wild": {str(B): Decimal("0.4")}}, {})
    assert changed.spec_hash() != SPEC.spec_hash()
    run = _paper(StrategyRouter(changed, LIFECYCLE))
    assert run.router_spec_hash == changed.spec_hash() and run.run_hash != BASELINE_RUN_HASH
    fallback = _spec(dict(SPEC.table), {str(B): Decimal(0)})  # same exposure, other rule
    assert _paper(StrategyRouter(fallback, LIFECYCLE)).run_hash != BASELINE_RUN_HASH


def test_input_identities_are_bound_even_when_routing_and_targets_do_not_change() -> None:
    base = _run()
    # A decides at minute 6 too: no routing decision sees it, targets are unchanged ...
    strategies = {**STRATEGIES, A: _strategy(A, {1: "1", 3: "1", 6: "1"})}
    run = _paper(strategies=strategies)
    assert run.targets == base.targets and run.decisions == base.decisions
    # ... but the strategy result's identity differs, and so does the run hash
    assert run.strategy_result_hashes[str(A)] != base.strategy_result_hashes[str(A)]
    assert run.run_hash != base.run_hash
    # the same state labels from another provider: a different state result identity
    request = StateRequest(
        state=STATE,
        spec_hash=STATE_SPEC_HASH,
        evaluation_times=tuple(value.evaluation_time for value in STATES.values),
        inputs=tuple(
            StateInput(
                feature=Ref(kind=Kind.FEATURE, name="bar_realized_vol_5", version="1.0.0"),
                evaluation_time=value.evaluation_time,
                value=Decimal(1),
                source_result_hash=content_hash({"fixture": "vol"}),
            )
            for value in STATES.values
        ),
    )
    assert request.content_hash() == STATES.request_hash  # the very same question ...
    descriptor = StateProviderDescriptor(
        name="other_states",
        version="1.0.0",
        deterministic=True,
        supported_states=FrozenMapping({str(STATE): STATE_SPEC_HASH}),
    )
    states = StateResult.build(request, descriptor, STATES.values)  # ... another answerer
    assert states.values == STATES.values and states.result_hash != STATES.result_hash
    moved = _paper(states=states)
    assert moved.decisions == base.decisions and moved.targets == base.targets
    assert moved.state_result_hash == states.result_hash and moved.run_hash != base.run_hash
    moved.verify()


# ---------------------------------------------------------------- (c) validation report binding


def test_supplied_validation_reports_are_recorded_and_hashed() -> None:
    run = _paper(validation_reports={B: REPORT_B, A: REPORT_A})
    assert run.validation_reports == {str(B): REPORT_B, str(A): REPORT_A}
    assert list(run.validation_reports) == sorted([str(A), str(B)])
    assert run.run_hash != BASELINE_RUN_HASH
    run.verify()
    # ref keys and ref-string keys are the same evidence
    same = _paper(validation_reports={str(A): REPORT_A, str(B): REPORT_B})
    assert same.run_hash == run.run_hash
    other = _paper(validation_reports={A: REPORT_A, B: REPORT_A})
    assert other.run_hash != run.run_hash
    # everything but the reports is unchanged
    assert replace(run, validation_reports=None, run_hash=BASELINE_RUN_HASH) == _run()


def test_a_routed_strategy_without_a_report_is_refused_when_reports_are_supplied() -> None:
    with pytest.raises(RouterError, match=r"without a validation report: \['strategy:revert_b"):
        _paper(validation_reports={A: REPORT_A})
    with pytest.raises(RouterError, match="without a validation report"):
        _paper(validation_reports={})
    stray = Ref(kind=Kind.STRATEGY, name="stray", version="1.0.0")
    with pytest.raises(RouterError, match="never routes"):
        _paper(validation_reports={A: REPORT_A, B: REPORT_B, stray: REPORT_A})
    for bad in ("", "ABC", REPORT_A.upper(), 42):
        with pytest.raises(RouterError, match="sha256"):
            _paper(validation_reports={A: REPORT_A, B: bad})  # type: ignore[arg-type]
    with pytest.raises(RouterError, match="given twice"):
        _paper(validation_reports={A: REPORT_A, str(A): REPORT_A, B: REPORT_B})  # type: ignore[arg-type]
