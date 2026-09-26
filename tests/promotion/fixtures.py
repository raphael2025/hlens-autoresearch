"""TEST ONLY fixtures for the promotion chain (ADR-0005): a toy strategy that "passes" synthetic
TEST ONLY reports. Nothing here is evidence about any real strategy; every report, experiment,
lifecycle transition and golden input is fabricated to exercise the fail-closed checks.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from core.contracts.strategy import (
    SignalObservation,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedStrategy,
)
from core.contracts.validation_profile import (
    BenchmarkParams,
    ProfileStatus,
    Provenance,
    ValidationProfile,
)
from core.domain.base import FrozenMapping, Ref, canonical_json, content_hash
from core.domain.research import ExperimentSpec, GateResult, ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import (
    HUMAN_APPROVAL_TRANSITIONS,
    LifecycleHistory,
    LifecycleState,
    LifecycleTransition,
)
from infrastructure.registry import ProfileFreezeRegistry
from research.promotion import PromotionEvidence
from research.validation.benchmark import INVERSE_CONTROL_GATE
from tests.factories import (
    HASH_E,
    cost_model_ref,
    dependency_hashes,
    experiment_spec,
    git_code_revision,
    hypothesis_ref,
    outcome_ref,
    repro_tuple,
    validation_profile,
    validation_report,
)
from tests.fake_strategy import SIGNAL_REF, FakeSignStrategy, fake_strategy_spec

S = LifecycleState

TEST_ONLY_SNAPSHOT = "TEST-ONLY-synthetic-golden-snapshot"


def toy_calibration_report(note: str = "TEST ONLY") -> tuple[bytes, str]:
    """A TEST ONLY ``gate_calibration`` report file (canonical JSON bytes) and its ``report_hash``.

    Only the parts the Profile freeze registry verifies are real — ``kind`` and the self-hash
    ``report_hash = content_hash(<report without report_hash>)`` (ADR-0062 decision 3); it carries
    no calibration runs and supports no Profile decision.
    """
    body: dict[str, object] = {
        "kind": "gate_calibration",
        "schema_version": "1.0.0",
        "status": "TEST ONLY",
        "disclaimer": "evidence only — not a Profile decision",
        "note": note,
        "inputs": {},
        "candidates": [],
    }
    report_hash = content_hash(body)
    return canonical_json({**body, "report_hash": report_hash}).encode("utf-8"), report_hash


#: The TEST ONLY calibration report file and the ``report_hash`` the toy Profile cites in
#: ``provenance.calibration_report``: no calibration was run; it only lets the toy Profile be marked
#: FROZEN and registered in a temporary Profile freeze registry (ADR-0062), so the happy path of the
#: promotion chain is exercised (C-A8 is checked, not met).
TOY_CALIBRATION_REPORT, TEST_ONLY_CALIBRATION = toy_calibration_report()
#: The TEST ONLY approver and time of the toy Profile's registered freeze (a declared name only).
TOY_FREEZE_APPROVER = "TEST-ONLY approver"
TOY_FREEZE_TIME = datetime(2026, 2, 15, tzinfo=UTC)
#: The (registered, ADR-0060) market benchmark rule of the toy Profile; a toy report that evaluates
#: G2 carries its reported-only item ``G2.market_benchmark.flat`` unless told not to.
TOY_BENCHMARK_RULE = "flat"
REPORT_TIME = datetime(2026, 2, 1, tzinfo=UTC)
LIFECYCLE_START = datetime(2026, 2, 2, tzinfo=UTC)
ARTIFACT_TIME = datetime(2026, 3, 1, tzinfo=UTC)
GOLDEN_T0 = datetime(2026, 1, 10, tzinfo=UTC)
INSTRUMENTS = ("BTCUSDT", "ETHUSDT")

PATH_TO_PAPER = (S.IDEA, S.CANDIDATE, S.VALIDATION, S.OOS, S.PAPER)
PATH_TO_PRODUCTION_CANDIDATE = (*PATH_TO_PAPER, S.PRODUCTION_CANDIDATE)


def toy_spec() -> StrategySpec:
    return fake_strategy_spec(name="toy_promotion")


def history(
    subject: Ref, path: Sequence[LifecycleState], *, start: datetime = LIFECYCLE_START
) -> LifecycleHistory:
    transitions = tuple(
        LifecycleTransition(
            subject=subject,
            from_state=a,
            to_state=b,
            reason="TEST ONLY transition",
            evidence=("TEST-ONLY-evidence",),
            triggered_by="test",
            approved_by="TEST-ONLY-human" if (a, b) in HUMAN_APPROVAL_TRANSITIONS else None,
            occurred_at=start + timedelta(hours=index),
        )
        for index, (a, b) in enumerate(zip(path, path[1:], strict=False))
    )
    return LifecycleHistory(subject=subject, transitions=transitions)


def toy_profile(
    status: ProfileStatus = ProfileStatus.FROZEN,
    calibration_report: str | None = TEST_ONLY_CALIBRATION,
    *,
    market_benchmark_rule: str = TOY_BENCHMARK_RULE,
    **overrides: object,
) -> ValidationProfile:
    """The TEST ONLY Profile the toy reports ran under; FROZEN with a TEST ONLY calibration
    reference by default (``tests.factories.validation_profile`` values — not calibrated)."""
    provenance = Provenance(calibration_report=calibration_report, approval_adr="TEST-ONLY")
    base = validation_profile().benchmark
    benchmark = BenchmarkParams.model_validate(
        {**base.model_dump(), "market_benchmark_rule": market_benchmark_rule}
    )
    payload: dict[str, object] = {
        "status": status,
        "provenance": provenance,
        "benchmark": benchmark,
    }
    payload.update(overrides)
    return validation_profile(**payload)


def toy_experiment(
    spec: StrategySpec, *, profile: ValidationProfile | None = None, **overrides: object
) -> ExperimentSpec:
    deps = dependency_hashes(hypothesis_ref(), spec.ref, outcome_ref(), cost_model_ref())
    deps[str(spec.ref)] = spec.content_hash()
    bound = toy_profile() if profile is None else profile
    payload: dict[str, object] = {
        "strategy_ref": spec.ref,
        "risk_policy_ref": None,
        "dependency_hashes": deps,
        "validation_profile": bound.ref,
        "validation_profile_hash": bound.content_hash(),
    }
    payload.update(overrides)
    return experiment_spec(repro=repro_tuple(**payload))


def gate(gate_id: str, verdict: Verdict = Verdict.PASS) -> GateResult:
    return GateResult(gate_id=gate_id, metric="TEST_ONLY", value=1.0, verdict=verdict)


def toy_report(
    spec: StrategySpec,
    experiment: ExperimentSpec,
    report_id: str,
    stages: Sequence[str],
    *,
    failing: str | None = None,
    verdict: Verdict = Verdict.FAIL,
    market_benchmark: str | None = f"G2.market_benchmark.{TOY_BENCHMARK_RULE}",
    inverse_control: str | None = INVERSE_CONTROL_GATE,
    **overrides: object,
) -> ValidationReport:
    ids = [(stage, f"{stage}.test_only") for stage in stages]
    if market_benchmark is not None and "G2" in stages:
        ids.append(("G2", market_benchmark))  # ADR-0060 reported-only item
    if inverse_control is not None and "G2" in stages:
        # ADR-0060 reported-only item: the toy Profile keeps the factory's
        # inverse_control_reported=True
        ids.append(("G2", inverse_control))
    gates = tuple(
        gate(gate_id, verdict if stage == failing else Verdict.PASS) for stage, gate_id in ids
    )
    derived = verdict if failing is not None else Verdict.PASS
    payload: dict[str, object] = {
        "validation_profile": experiment.repro.validation_profile,
        "validation_profile_hash": experiment.repro.validation_profile_hash,
        "report_id": report_id,
        "run_id": f"run-{report_id}",
        "subject": spec.ref,
        "experiment_hash": experiment.experiment_hash,
        "gates": gates,
        "created_at": REPORT_TIME,
    }
    payload.update(overrides)
    return validation_report(derived, **payload)


def signals(shift: int) -> tuple[SignalObservation, ...]:
    values = ("1.5", "-0.25", "0", "2", "-3", "0.75")
    out = []
    for step in range(4):
        for position, instrument in enumerate(INSTRUMENTS):
            event = GOLDEN_T0 + timedelta(hours=step)
            available = event + timedelta(minutes=1)
            out.append(
                SignalObservation(
                    signal=SIGNAL_REF,
                    instrument=instrument,
                    event_time=event,
                    available_time=available,
                    knowledge_time=available,
                    value=Decimal(values[(step + position + shift) % len(values)]),
                )
            )
    return tuple(out)


def golden_request(spec: StrategySpec, *, shift: int = 0, **overrides: object) -> StrategyRequest:
    payload: dict[str, object] = {
        "strategy": spec.ref,
        "spec_hash": spec.content_hash(),
        "instruments": INSTRUMENTS,
        "knowledge_cutoff": GOLDEN_T0 + timedelta(hours=10),
        "decision_times": tuple(GOLDEN_T0 + timedelta(hours=step, minutes=30) for step in range(4)),
        "signals": signals(shift),
    }
    payload.update(overrides)
    return StrategyRequest(**payload)  # type: ignore[arg-type]


def toy_evidence(**overrides: object) -> PromotionEvidence:
    """A complete TEST ONLY evidence bundle for ``toy_spec`` that passes every check."""
    spec = toy_spec()
    experiment = toy_experiment(spec)
    evidence = PromotionEvidence(
        spec=spec,
        reports=(
            toy_report(spec, experiment, "TEST-ONLY-in-sample", ("G0", "G1", "G2", "G3", "G4")),
            toy_report(spec, experiment, "TEST-ONLY-sealed-oos", ("G5",)),
        ),
        profiles=(toy_profile(),),
        experiments=(experiment,),
        lifecycle=history(spec.ref, PATH_TO_PRODUCTION_CANDIDATE),
        research_code=git_code_revision(),
        research_provider=FakeSignStrategy([spec]),
        golden_inputs=(golden_request(spec, shift=0), golden_request(spec, shift=3)),
        dataset_snapshot_id=TEST_ONLY_SNAPSHOT,
        created_at=ARTIFACT_TIME,
        signal_dependencies={str(SIGNAL_REF): HASH_E},
    )
    return replace(evidence, **overrides)  # type: ignore[arg-type]


@contextmanager
def toy_freezes(
    *profiles: ValidationProfile, root: Path | None = None
) -> Iterator[ProfileFreezeRegistry]:
    """An open TEST ONLY Profile freeze registry (a temporary directory and a sibling anchor file)
    in which ``profiles`` (default: ``toy_profile()``) are frozen on the TEST ONLY calibration
    report by ``TOY_FREEZE_APPROVER`` — the ADR-0062 record Promotion requires."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) if root is None else root
        registry = ProfileFreezeRegistry(base / "freezes", anchor=base / "freezes.anchor.jsonl")
        with registry:
            for profile in profiles or (toy_profile(),):
                registry.register_freeze(
                    profile,
                    TOY_CALIBRATION_REPORT,
                    approved_by=TOY_FREEZE_APPROVER,
                    approved_at=TOY_FREEZE_TIME,
                )
            yield registry


type PositionTransform = Callable[[TargetPosition], TargetPosition]


class ToyProductionSign:
    """TEST ONLY "production re-implementation" of the toy strategy: its own descriptor, the same
    rule; an optional ``transform`` makes it a buggy one."""

    def __init__(
        self,
        spec: StrategySpec,
        *,
        transform: PositionTransform | None = None,
        name: str = "toy_production_sign",
    ) -> None:
        self._inner = FakeSignStrategy([spec])
        self._transform = transform
        self._descriptor = StrategyProviderDescriptor(
            name=name,
            version="1.0.0",
            deterministic=True,
            supported_strategies=FrozenMapping({str(spec.ref): spec.content_hash()}),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        if not self._descriptor.supports(request.strategy, request.spec_hash):
            raise UnsupportedStrategy(f"{request.strategy} is not supported")
        positions = self._inner.target_positions(request).positions
        if self._transform is not None:
            positions = tuple(self._transform(item) for item in positions)
        return StrategyResult.build(request, self._descriptor, positions)


def flip_sign(item: TargetPosition) -> TargetPosition:
    return TargetPosition.model_validate(
        {**item.model_dump(), "target_weight": -item.target_weight}
    )


def widen_exponent(item: TargetPosition) -> TargetPosition:
    """Same value, different exact encoding (``0.25`` → ``0.2500``)."""
    weight = item.target_weight.quantize(Decimal("0.0001"))
    return TargetPosition.model_validate({**item.model_dump(), "target_weight": weight})


class FlakyStrategy:
    """TEST ONLY non-deterministic provider: asked the same request again, it flips its answer."""

    def __init__(self, spec: StrategySpec) -> None:
        self._inner = FakeSignStrategy([spec])
        self._calls: dict[str, int] = {}

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._inner.descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        key = request.content_hash()
        self._calls[key] = self._calls.get(key, 0) + 1
        positions = self._inner.target_positions(request).positions
        if self._calls[key] % 2 == 0:
            positions = tuple(flip_sign(item) for item in positions)
        return StrategyResult.build(request, self.descriptor, positions)


class RaisingStrategy(ToyProductionSign):
    """TEST ONLY candidate that declares the spec but fails on every request."""

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        raise RuntimeError("TEST ONLY failure")
