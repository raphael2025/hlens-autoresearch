"""Promotion service: evidence → immutable ``StrategyArtifact`` → Strategy Registry (ADR-0005).

CODE_COMPLETE / DEBUG_PENDING (2026-09-26). No contract, Schema or lifecycle change.

**Placement.** Packaging a research conclusion needs research code: the report helpers
(``research.validation.report.promotion_blocked_reason``) and the research ``StrategyProvider`` that
computes the golden outputs. ``apps/`` must not import ``research/`` (01-system.md §3), so this is
the Research Plane side of the ``VAL -- promotion`` arrow: it submits the finished artifact to the
Control Plane registry (``infrastructure.registry.StrategyRegistry``) and nothing else. The
production side (Equivalence Gate, deployment record) is ``apps.promotion``; it never sees this
module.

**What an artifact is built from — and nothing else** (every check is fail closed; the first
failing check raises ``PromotionRefused`` naming a ``PromotionRefusal``; nothing is written and no
partial artifact exists):

1. the ``StrategySpec`` (re-validated from its JSON, so a ``model_construct`` object cannot pass);
2. its ``ValidationReport`` s: at least one; every report re-validated, about this spec
   (``subject``), unique ``report_id``; **every** report ``PASS``
   (``promotion_blocked_reason`` → ``verdict_not_pass``); stages ``G0``–``G4`` each evaluated by
   some report (``stage_not_evaluated``); and at least one report whose
   ``promotion_blocked_reason`` is ``None`` — PASS **including** a G5 sealed-OOS gate
   (``sealed_oos_not_evaluated``). Because a report's verdict is ``derive_verdict(gates)``
   (ADR-0013), every gate of an accepted report is PASS;
3. the ``ExperimentSpec`` of every report's ``experiment_hash`` (no more, no fewer): each binds the
   spec (``strategy_ref`` and its dependency hash = the spec's content hash) and the report's
   Constitution / Profile binding; all were run on ``research_code.commit_oid``;
4. the ``ValidationProfile`` object of every report (``profiles``; no more, no fewer): the object's
   ``content_hash()`` equals the report's ``validation_profile_hash`` and its ref is the report's
   ``validation_profile`` (``profile_missing`` / ``profile_hash_mismatch``); its ``status`` is
   ``FROZEN`` (``profile_not_frozen``) and ``provenance.calibration_report`` is present
   (``profile_not_calibrated``) — Constitution C-A8: promotion needs a frozen, calibrated Profile.
   ``status`` is excluded from the Profile content hash (ADR-0008), so neither a report nor the
   Profile object's own ``status`` proves the freeze: the **authoritative** source is the
   anchored ``ProfileFreezeRegistry`` (ADR-0062). Each Profile must have a verified
   ``profile.frozen`` record for exactly its ref **and** content hash whose calibration report is
   the one ``provenance.calibration_report`` cites (``frozen_record``); none, or a registry that is
   closed / poisoned → ``profile_not_frozen``, whatever the object says. The registry records a
   named human approval; it does not authenticate identity and is not a production Control Plane
   (ADR-0062 decision 6). No Profile is frozen today, so **every** promotion is refused here
   (correct: Step 2 calibration has not happened). ADR-0060 C-T4: a report that
   evaluates stage G2 under a Profile whose ``benchmark.market_benchmark_rule`` is not ``none``
   must carry the item that rule calls for — ``G2.market_benchmark.<rule>`` for a registered rule,
   the bare ``G2.market_benchmark`` (INCONCLUSIVE) for an unregistered one
   (``market_benchmark_missing``); and when that Profile's ``benchmark.inverse_control_reported``
   is true, the exact ADR-0060 inverse-control item ``G2.inverse_control``
   (``inverse_control_missing``; checked after the market benchmark item; presence only — a
   reported-only item, no threshold on its value). The validator keeps
   ``ValidatorSetup.market_benchmark=False`` as its default; promotion simply does not accept a
   report produced without them. Finally every thresholded gate of the report must agree with that
   Profile instance (``research.validation.verification.verify_report``, ADR-0013: the source is
   a field of the bound Profile carrying exactly the recorded threshold, a ``param:`` source does
   not override a field the Profile carries, and the verdict agrees with the metric's comparison)
   — any discrepancy is ``report_threshold_mismatch`` (MOD-VALID, 2026-09-28);
5. the dependency closure: the union of the experiments' ``dependency_hashes`` and the caller's
   ``signal_dependencies`` without conflicting hashes, covering every signal and the risk policy
   of the spec;
6. the lifecycle history (ADR-0006): re-validated (so every transition is a legal edge with the
   required human approvals, in time order), about this spec, having passed ``OOS → PAPER``
   (human-approved) and now in ``PAPER`` / ``PRODUCTION_CANDIDATE`` / ``ACTIVE``;
7. golden outputs computed here, deterministically, by the research provider on the declared golden
   input set: every golden request asks for this spec and hash with the spec's **fixed** params
   (``params`` empty), no duplicates; the provider declares the spec; every answer passes
   ``StrategyResult.check_answers``; a second run gives byte-identical results
   (``golden_output_not_deterministic`` otherwise). The payload encoding is
   ``infrastructure.registry.golden``.

``created_at`` is explicit (the artifact id is a content hash) and may not precede its evidence
(reports, lifecycle transitions and the Profiles' freeze approvals).
The artifact's ``name`` / ``version`` are the spec's.

**Status.** No library strategy (``research.strategies.library``) can be promoted today: even
complete TEST ONLY evidence is refused because no Profile is frozen — no record exists in any
Profile freeze registry (``profile_not_frozen``; tested). The happy path is exercised only with a
TEST ONLY freeze in a temporary registry. Nothing here places code in ``strategies/`` or
``plugins/`` and nothing touches execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from core.contracts.strategy import StrategyProvider, StrategyRequest, StrategyResult
from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.artifact import GoldenOutputs, StrategyArtifact
from core.domain.base import FrozenMapping, GitCodeRevision, Ref
from core.domain.research import ExperimentSpec, ValidationReport
from core.domain.specs import StrategySpec
from core.errors import HlensError
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from infrastructure.registry import (
    ProfileFreeze,
    ProfileFreezeRegistry,
    RegistryError,
    StrategyRegistry,
    golden_positions_payload,
    golden_signals_payload,
    payload_hash,
)
from infrastructure.registry.blobs import blob_uri
from research.validation.benchmark import (
    INVERSE_CONTROL_GATE,
    MARKET_BENCHMARK_GATE,
    resolve_market_benchmark,
)
from research.validation.report import (
    SEALED_OOS_NOT_EVALUATED,
    STAGES,
    VERDICT_NOT_PASS,
    promotion_blocked_reason,
)
from research.validation.verification import verify_report

__all__ = [
    "PROMOTABLE_STATES",
    "PromotionEvidence",
    "PromotionPackage",
    "PromotionRefusal",
    "PromotionRefused",
    "build_artifact",
    "promote",
]

#: Lifecycle states from which an artifact may be packaged: reached only through the
#: human-approved ``OOS → PAPER`` edge, not terminal, not DEGRADED / under REVALIDATION.
PROMOTABLE_STATES: Final = frozenset(
    {LifecycleState.PAPER, LifecycleState.PRODUCTION_CANDIDATE, LifecycleState.ACTIVE}
)
#: In-sample stages that must each be evaluated (G5 is checked by ``promotion_blocked_reason``).
_IN_SAMPLE_STAGES: Final = tuple(stage for stage in STAGES if stage != "G5")


class PromotionRefusal(StrEnum):
    """Why an artifact was not built (the reason code of ``PromotionRefused``)."""

    EVIDENCE_INVALID = "evidence_invalid"
    NO_VALIDATION_REPORT = "no_validation_report"
    REPORT_SUBJECT_MISMATCH = "report_subject_mismatch"
    VERDICT_NOT_PASS = VERDICT_NOT_PASS
    STAGE_NOT_EVALUATED = "stage_not_evaluated"
    SEALED_OOS_NOT_EVALUATED = SEALED_OOS_NOT_EVALUATED
    EXPERIMENT_MISSING = "experiment_missing"
    EXPERIMENT_NOT_EVIDENCED = "experiment_not_evidenced"
    EXPERIMENT_MISMATCH = "experiment_mismatch"
    RESEARCH_CODE_MISMATCH = "research_code_mismatch"
    PROFILE_MISSING = "profile_missing"
    PROFILE_HASH_MISMATCH = "profile_hash_mismatch"
    PROFILE_NOT_EVIDENCED = "profile_not_evidenced"
    PROFILE_NOT_FROZEN = "profile_not_frozen"
    PROFILE_NOT_CALIBRATED = "profile_not_calibrated"
    MARKET_BENCHMARK_MISSING = "market_benchmark_missing"
    INVERSE_CONTROL_MISSING = "inverse_control_missing"
    REPORT_THRESHOLD_MISMATCH = "report_threshold_mismatch"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    DEPENDENCY_UNBOUND = "dependency_unbound"
    LIFECYCLE_SUBJECT_MISMATCH = "lifecycle_subject_mismatch"
    LIFECYCLE_NOT_ELIGIBLE = "lifecycle_not_eligible"
    GOLDEN_INPUTS_MISSING = "golden_inputs_missing"
    GOLDEN_INPUT_MISMATCH = "golden_input_mismatch"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    GOLDEN_OUTPUT_INVALID = "golden_output_invalid"
    GOLDEN_OUTPUT_NOT_DETERMINISTIC = "golden_output_not_deterministic"
    ALREADY_REGISTERED = "already_registered"


class PromotionRefused(Exception):
    """The evidence does not support an artifact; ``reason`` names the first failing check."""

    def __init__(self, reason: PromotionRefusal, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PromotionEvidence:
    """Everything an artifact may be built from (see module docs)."""

    spec: StrategySpec
    reports: Sequence[ValidationReport]
    #: The Validation Profile(s) the reports were produced under (C-A8: frozen and calibrated).
    profiles: Sequence[ValidationProfile]
    experiments: Sequence[ExperimentSpec]
    lifecycle: LifecycleHistory
    research_code: GitCodeRevision
    research_provider: StrategyProvider
    golden_inputs: Sequence[StrategyRequest]
    #: The fixed data snapshot the golden inputs were taken from (ADR-0005 §2).
    dataset_snapshot_id: str
    created_at: datetime
    #: Content hashes of the spec's signals / risk policy when no experiment binds them.
    signal_dependencies: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionPackage:
    """A built artifact and the two golden payloads its ``golden_outputs`` bind."""

    artifact: StrategyArtifact
    signals_payload: Mapping[str, Any]
    positions_payload: Mapping[str, Any]


def _refuse(reason: PromotionRefusal, detail: str) -> PromotionRefused:
    return PromotionRefused(reason, detail)


def _revalidated[M: BaseModel](model: M, label: str) -> M:
    try:
        return type(model).model_validate_json(model.model_dump_json())
    except (ValidationError, HlensError) as exc:  # LifecycleViolation is not a ValueError
        raise _refuse(PromotionRefusal.EVIDENCE_INVALID, f"{label}: {exc}") from exc


def _same_target(left: Ref, right: Ref) -> bool:
    return left.target_identity() == right.target_identity()


def _check_reports(spec: StrategySpec, reports: Sequence[ValidationReport]) -> None:
    if not reports:
        raise _refuse(PromotionRefusal.NO_VALIDATION_REPORT, f"{spec.ref} has no ValidationReport")
    ids = [report.report_id for report in reports]
    if len(set(ids)) != len(ids):
        raise _refuse(PromotionRefusal.EVIDENCE_INVALID, "a report_id is given twice")
    stages: set[str] = set()
    sealed_oos_passed = False
    for report in reports:
        if not _same_target(report.subject, spec.ref):
            raise _refuse(
                PromotionRefusal.REPORT_SUBJECT_MISMATCH,
                f"report {report.report_id} is about {report.subject}, not {spec.ref}",
            )
        blocked = promotion_blocked_reason(report)
        if blocked == VERDICT_NOT_PASS:
            raise _refuse(
                PromotionRefusal.VERDICT_NOT_PASS,
                f"report {report.report_id} is {report.verdict.value}",
            )
        sealed_oos_passed = sealed_oos_passed or blocked is None
        stages.update(gate.gate_id.split(".")[0] for gate in report.gates)
    missing = [stage for stage in _IN_SAMPLE_STAGES if stage not in stages]
    if missing:
        raise _refuse(
            PromotionRefusal.STAGE_NOT_EVALUATED, f"no PASS report evaluates stage(s) {missing}"
        )
    if not sealed_oos_passed:
        raise _refuse(
            PromotionRefusal.SEALED_OOS_NOT_EVALUATED,
            "no PASS report carries a G5 (sealed OOS) gate",
        )


def _check_experiments(
    spec: StrategySpec,
    reports: Sequence[ValidationReport],
    experiments: Sequence[ExperimentSpec],
    research_code: GitCodeRevision,
) -> tuple[ExperimentSpec, ...]:
    by_hash = {experiment.experiment_hash: experiment for experiment in experiments}
    cited = {report.experiment_hash for report in reports}
    for report in reports:
        experiment = by_hash.get(report.experiment_hash)
        if experiment is None:
            raise _refuse(
                PromotionRefusal.EXPERIMENT_MISSING,
                f"report {report.report_id} cites experiment {report.experiment_hash}, "
                "which is not in the evidence",
            )
        repro = experiment.repro
        if (
            repro.constitution_version != report.constitution_version
            or not _same_target(repro.validation_profile, report.validation_profile)
            or repro.validation_profile_hash != report.validation_profile_hash
        ):
            raise _refuse(
                PromotionRefusal.EXPERIMENT_MISMATCH,
                f"report {report.report_id} and experiment {report.experiment_hash} bind "
                "different Constitution / Profile versions",
            )
    unused = sorted(set(by_hash) - cited)
    if unused:
        raise _refuse(
            PromotionRefusal.EXPERIMENT_NOT_EVIDENCED,
            f"experiment(s) {unused} are cited by no report",
        )
    spec_hash = spec.content_hash()
    for experiment_hash in sorted(cited):
        repro = by_hash[experiment_hash].repro
        if repro.strategy_ref is None or not _same_target(repro.strategy_ref, spec.ref):
            raise _refuse(
                PromotionRefusal.EXPERIMENT_MISMATCH,
                f"experiment {experiment_hash} tests {repro.strategy_ref}, not {spec.ref}",
            )
        if repro.dependency_hashes.get(str(spec.ref)) != spec_hash:
            raise _refuse(
                PromotionRefusal.EXPERIMENT_MISMATCH,
                f"experiment {experiment_hash} binds another content hash of {spec.ref}",
            )
        if repro.code_commit != research_code.commit_oid:
            raise _refuse(
                PromotionRefusal.RESEARCH_CODE_MISMATCH,
                f"experiment {experiment_hash} ran on commit {repro.code_commit}, "
                f"not {research_code.commit_oid}",
            )
    return tuple(by_hash[h] for h in sorted(cited))


def _market_benchmark_item(profile: ValidationProfile) -> str | None:
    """The ADR-0060 gate id ``profile``'s benchmark rule calls for (``None``: rule ``none``)."""
    name = profile.benchmark.market_benchmark_rule
    if name == "none":
        return None
    registered = resolve_market_benchmark(name) is not None
    return f"{MARKET_BENCHMARK_GATE}.{name}" if registered else MARKET_BENCHMARK_GATE


def _freeze_record(freezes: ProfileFreezeRegistry, profile: ValidationProfile) -> ProfileFreeze:
    """The registry's verified freeze of exactly ``profile`` (ADR-0062), or refuse."""
    try:
        record = freezes.frozen_record(profile)
    except RegistryError as exc:  # closed or poisoned: it cannot answer, so nothing is frozen
        raise _refuse(
            PromotionRefusal.PROFILE_NOT_FROZEN,
            f"the Profile freeze registry cannot answer for {profile.ref}: {exc}",
        ) from exc
    if record is None:
        raise _refuse(
            PromotionRefusal.PROFILE_NOT_FROZEN,
            f"{profile.ref} ({profile.content_hash()}) has no profile.frozen record citing its "
            "calibration report in the Profile freeze registry (ADR-0062); its own status is not "
            "authoritative",
        )
    return record


def _check_profiles(
    reports: Sequence[ValidationReport],
    profiles: Sequence[ValidationProfile],
    freezes: ProfileFreezeRegistry,
) -> tuple[ProfileFreeze, ...]:
    """Every report's Profile is given, is the one it hashes to, is FROZEN + calibrated, and its
    freeze is registered (ADR-0062). Returns the freeze records (for the time check)."""
    if not isinstance(freezes, ProfileFreezeRegistry):
        raise _refuse(
            PromotionRefusal.PROFILE_NOT_FROZEN,
            "no Profile freeze registry was given (ADR-0062: the authoritative freeze source)",
        )
    records: dict[str, ProfileFreeze] = {}
    by_hash: dict[str, ValidationProfile] = {}
    for index, given in enumerate(profiles):
        if not isinstance(given, ValidationProfile):
            raise _refuse(PromotionRefusal.EVIDENCE_INVALID, f"profile {index} is not a Profile")
        by_hash.setdefault(given.content_hash(), given)
    cited: set[str] = set()
    for report in reports:
        wanted = report.validation_profile_hash
        profile = by_hash.get(wanted)
        if profile is None:
            same_ref = any(_same_target(p.ref, report.validation_profile) for p in profiles)
            raise _refuse(
                PromotionRefusal.PROFILE_HASH_MISMATCH
                if same_ref
                else PromotionRefusal.PROFILE_MISSING,
                f"report {report.report_id} ran under {report.validation_profile} / {wanted}; "
                + (
                    "the given Profile of that ref hashes differently"
                    if same_ref
                    else "no such Profile is in the evidence"
                ),
            )
        if not _same_target(profile.ref, report.validation_profile):
            raise _refuse(
                PromotionRefusal.PROFILE_HASH_MISMATCH,
                f"report {report.report_id} names {report.validation_profile}, the Profile of "
                f"hash {wanted} is {profile.ref}",
            )
        # status / provenance before re-validation: the typed reason, not evidence_invalid
        if profile.status is not ProfileStatus.FROZEN:
            raise _refuse(
                PromotionRefusal.PROFILE_NOT_FROZEN,
                f"report {report.report_id} ran under {profile.ref}, which is "
                f"{profile.status!s}; C-A8 requires a frozen Profile",
            )
        calibration = profile.provenance.calibration_report
        if calibration is None or not calibration.strip():
            raise _refuse(
                PromotionRefusal.PROFILE_NOT_CALIBRATED,
                f"{profile.ref} has no provenance.calibration_report (C-A8)",
            )
        checked = _revalidated(profile, f"profile {profile.ref}")
        if checked.content_hash() != wanted:
            raise _refuse(
                PromotionRefusal.PROFILE_HASH_MISMATCH,
                f"{profile.ref} does not re-validate to hash {wanted}",
            )
        records[wanted] = _freeze_record(freezes, checked)
        item = _market_benchmark_item(checked)
        evaluates_g2 = any(gate.gate_id.split(".")[0] == "G2" for gate in report.gates)
        if (
            item is not None
            and evaluates_g2
            and not any(gate.gate_id == item for gate in report.gates)
        ):
            raise _refuse(
                PromotionRefusal.MARKET_BENCHMARK_MISSING,
                f"report {report.report_id} evaluates G2 under {profile.ref} "
                f"(benchmark.market_benchmark_rule={checked.benchmark.market_benchmark_rule}) "
                f"without the ADR-0060 item {item}",
            )
        if (
            checked.benchmark.inverse_control_reported
            and evaluates_g2
            and not any(gate.gate_id == INVERSE_CONTROL_GATE for gate in report.gates)
        ):
            raise _refuse(
                PromotionRefusal.INVERSE_CONTROL_MISSING,
                f"report {report.report_id} evaluates G2 under {profile.ref} "
                "(benchmark.inverse_control_reported=true) without the ADR-0060 item "
                f"{INVERSE_CONTROL_GATE}",
            )
        verification = verify_report(report, checked)
        if not verification.ok:
            first = verification.discrepancies[0]
            raise _refuse(
                PromotionRefusal.REPORT_THRESHOLD_MISMATCH,
                f"report {report.report_id} disagrees with {profile.ref} "
                f"({len(verification.discrepancies)} discrepancies; first: "
                f"{first.gate_id} {first.problem.value}: {first.detail})",
            )
        cited.add(wanted)
    unused = sorted(set(by_hash) - cited)
    if unused or len(by_hash) != len(profiles):
        raise _refuse(
            PromotionRefusal.PROFILE_NOT_EVIDENCED,
            f"profile(s) {unused} are cited by no report, or a Profile is given twice",
        )
    return tuple(records[key] for key in sorted(records))


def _dependencies(
    spec: StrategySpec,
    experiments: Sequence[ExperimentSpec],
    extra: Mapping[str, str],
) -> dict[str, str]:
    merged: dict[str, str] = {}
    sources: list[tuple[str, Mapping[str, str]]] = [
        (f"experiment {e.experiment_hash}", e.repro.dependency_hashes) for e in experiments
    ]
    sources.append(("signal_dependencies", extra))
    for label, mapping in sources:
        for key, value in mapping.items():
            if merged.setdefault(key, value) != value:
                raise _refuse(
                    PromotionRefusal.DEPENDENCY_CONFLICT,
                    f"{label} binds {key} to {value}, another source to {merged[key]}",
                )
    required = [*spec.signals, *([spec.risk_policy] if spec.risk_policy is not None else [])]
    unbound = sorted(str(ref) for ref in required if str(ref) not in merged)
    if unbound:
        raise _refuse(
            PromotionRefusal.DEPENDENCY_UNBOUND, f"no content hash is bound for {unbound}"
        )
    return dict(sorted(merged.items()))


def _check_lifecycle(spec: StrategySpec, lifecycle: LifecycleHistory) -> None:
    if not _same_target(lifecycle.subject, spec.ref):
        raise _refuse(
            PromotionRefusal.LIFECYCLE_SUBJECT_MISMATCH,
            f"the lifecycle history is about {lifecycle.subject}, not {spec.ref}",
        )
    state = lifecycle.current_state
    paper = [
        t
        for t in lifecycle.transitions
        if (t.from_state, t.to_state) == (LifecycleState.OOS, LifecycleState.PAPER)
    ]
    if state not in PROMOTABLE_STATES or not paper or not paper[0].approved_by:
        raise _refuse(
            PromotionRefusal.LIFECYCLE_NOT_ELIGIBLE,
            f"{spec.ref} is {state.value}; an artifact needs a human-approved OOS → PAPER and a "
            f"current state in {sorted(s.value for s in PROMOTABLE_STATES)}",
        )


def _check_golden_inputs(
    spec: StrategySpec, requests: Sequence[StrategyRequest], snapshot_id: str
) -> tuple[StrategyRequest, ...]:
    if not snapshot_id.strip():
        raise _refuse(PromotionRefusal.GOLDEN_INPUTS_MISSING, "no dataset_snapshot_id is declared")
    if not requests:
        raise _refuse(PromotionRefusal.GOLDEN_INPUTS_MISSING, "the golden input set is empty")
    checked = tuple(_revalidated(r, f"golden request {i}") for i, r in enumerate(requests))
    spec_hash = spec.content_hash()
    seen: set[str] = set()
    for index, request in enumerate(checked):
        if not _same_target(request.strategy, spec.ref) or request.spec_hash != spec_hash:
            raise _refuse(
                PromotionRefusal.GOLDEN_INPUT_MISMATCH,
                f"golden request {index} asks for {request.strategy} / {request.spec_hash}",
            )
        if request.params:
            raise _refuse(
                PromotionRefusal.GOLDEN_INPUT_MISMATCH,
                f"golden request {index} overrides params {sorted(request.params)}; the artifact "
                "fixes the spec's params",
            )
        if request.content_hash() in seen:
            raise _refuse(
                PromotionRefusal.GOLDEN_INPUT_MISMATCH, f"golden request {index} is a duplicate"
            )
        seen.add(request.content_hash())
    return checked


def _answer(
    provider: StrategyProvider, requests: Sequence[StrategyRequest]
) -> tuple[StrategyResult, ...]:
    descriptor = provider.descriptor
    results: list[StrategyResult] = []
    for index, request in enumerate(requests):
        try:
            result = provider.target_positions(request)
            result.check_answers(request, descriptor)
        except Exception as exc:  # any failure of the research answer is a refusal, not a partial
            raise _refuse(
                PromotionRefusal.GOLDEN_OUTPUT_INVALID,
                f"golden request {index}: {type(exc).__name__}: {exc}",
            ) from exc
        results.append(result)
    return tuple(results)


def _golden_outputs(
    spec: StrategySpec,
    provider: StrategyProvider,
    requests: Sequence[StrategyRequest],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not provider.descriptor.supports(spec.ref, spec.content_hash()):
        raise _refuse(
            PromotionRefusal.PROVIDER_UNSUPPORTED,
            f"{provider.descriptor.plugin_key} does not declare {spec.ref} with this spec hash",
        )
    first = _answer(provider, requests)
    second = _answer(provider, requests)
    if [r.content_hash() for r in first] != [r.content_hash() for r in second]:
        raise _refuse(
            PromotionRefusal.GOLDEN_OUTPUT_NOT_DETERMINISTIC,
            "two runs of the research provider on the golden inputs differ",
        )
    return golden_signals_payload(requests), golden_positions_payload(first)


def _check_time(
    created_at: datetime,
    reports: Sequence[ValidationReport],
    lifecycle: LifecycleHistory,
    freezes: Sequence[ProfileFreeze],
) -> None:
    times = [report.created_at for report in reports]
    times.extend(t.occurred_at for t in lifecycle.transitions)
    times.extend(record.approved_at for record in freezes)
    if created_at.tzinfo is None or any(created_at < t for t in times):
        raise _refuse(
            PromotionRefusal.EVIDENCE_INVALID,
            "created_at must be UTC and not precede any report, lifecycle transition or Profile "
            "freeze approval",
        )


def build_artifact(
    evidence: PromotionEvidence, *, freezes: ProfileFreezeRegistry
) -> PromotionPackage:
    """Build the artifact and its golden payloads, or raise ``PromotionRefused``. No I/O: the
    open, anchored ``freezes`` registry answers from its replayed, verified records."""
    spec = _revalidated(evidence.spec, "strategy spec")
    reports = tuple(_revalidated(r, f"report {i}") for i, r in enumerate(evidence.reports))
    _check_reports(spec, reports)
    experiments = tuple(
        _revalidated(e, f"experiment {i}") for i, e in enumerate(evidence.experiments)
    )
    research_code = _revalidated(evidence.research_code, "research code revision")
    cited = _check_experiments(spec, reports, experiments, research_code)
    freeze_records = _check_profiles(reports, evidence.profiles, freezes)
    dependencies = _dependencies(spec, cited, evidence.signal_dependencies)
    lifecycle = _revalidated(evidence.lifecycle, "lifecycle history")
    _check_lifecycle(spec, lifecycle)
    _check_time(evidence.created_at, reports, lifecycle, freeze_records)
    requests = _check_golden_inputs(spec, evidence.golden_inputs, evidence.dataset_snapshot_id)
    signals, positions = _golden_outputs(spec, evidence.research_provider, requests)
    signals_hash, positions_hash = payload_hash(signals), payload_hash(positions)
    try:
        artifact = StrategyArtifact(
            name=spec.name,
            version=spec.version,
            created_at=evidence.created_at,
            strategy_spec=spec.ref,
            dependencies=FrozenMapping(dependencies),
            research_code_commit=research_code.commit_oid,
            research_code_tree_hash=research_code.tree_oid,
            experiment_hashes=tuple(e.experiment_hash for e in cited),
            validation_reports=tuple(sorted(r.report_id for r in reports)),
            golden_outputs=GoldenOutputs(
                dataset_snapshot_id=evidence.dataset_snapshot_id,
                signals_uri=blob_uri(signals_hash),
                signals_hash=signals_hash,
                positions_uri=blob_uri(positions_hash),
                positions_hash=positions_hash,
            ),
            applicable_instruments=spec.applicable_instruments,
        )
    except ValidationError as exc:
        raise _refuse(PromotionRefusal.EVIDENCE_INVALID, f"artifact: {exc}") from exc
    return PromotionPackage(artifact, signals, positions)


def promote(
    evidence: PromotionEvidence,
    registry: StrategyRegistry,
    *,
    freezes: ProfileFreezeRegistry,
) -> StrategyArtifact:
    """Build the artifact (``build_artifact``, checked against the ``freezes`` registry), store
    its golden blobs, register it.

    Every evidence check runs before anything is written. Re-promoting the same evidence is
    ``already_registered`` (the registry is append-only; nothing is overwritten).
    """
    package = build_artifact(evidence, freezes=freezes)
    artifact = package.artifact
    if registry.has_artifact(artifact.artifact_id):
        raise _refuse(
            PromotionRefusal.ALREADY_REGISTERED, f"artifact {artifact.artifact_id} is registered"
        )
    golden = artifact.golden_outputs
    stored = (
        registry.put_blob(package.signals_payload),
        registry.put_blob(package.positions_payload),
    )
    if stored != (
        (golden.signals_uri, golden.signals_hash),
        (golden.positions_uri, golden.positions_hash),
    ):
        raise RuntimeError("the blob store returned other identities than the artifact binds")
    registry.register_artifact(artifact)
    return artifact
