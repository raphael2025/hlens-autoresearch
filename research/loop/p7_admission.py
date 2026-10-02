"""Admit a declared P7 plan into the Research Loop (ADR-0103 D2 / D3), default OFF.

``LoopWiring.p7_plans`` is ``None`` by default: nothing here runs and every record, fingerprint and
hash is byte-identical to a loop without the field. A ``P7PlanSource`` declares the plans
(``P7PlanRequest``: the typed plan, its verified direct-reference resolution, the hypotheses and
the declared ``ExperimentSpec`` of each, and the explicit Provider wiring), the
``P7ExecutionSwitch`` and the Provider allowlist. It needs a durable state directory whose format
has the ADR-0073 plan admission journal (v4 / v5 / v6); the composition refuses anything else.

Once per round, before the round's first journal write, the hypothesis stage asks
``P7RoundAdmission.run`` to admit the first **pending** plan of the source (in declared order: not
rejected in an earlier round, its hypotheses not all registered yet). The steps, in order:

1. **pure checks** (nothing is written): a cross-sectional node is refused
   (``cross_sectional_loop_unsupported``: the loop is single-instrument, ADR-0103 D4 / 修订 1);
   compile (``compile_lowered_plan`` with the source's switch and allowlist: a disabled switch is
   ``execution_disabled``); the root must be a strategy with a Provider serving it; build the
   Providers; **produce the binding** (``P7PlanRecord.from_compiled``, ``bind_experiment``: the
   record and every node output into each declared ``ExperimentSpec``); **integrity**
   (``plan_bindings.validate_complete_experiment_bindings``); **cross-check** of the generated
   PREPARE evidence (``p7_evidence.cross_check_admission_evidence``); **non-LLM** (an LLM-origin
   hypothesis is refused), the loop's family, each hypothesis runnable here (``trial_point``:
   ``strategy = <root>``, ``p7_plan = <plan_hash>``, requestable parameters) and new to the
   TrialLedger;
2. **PREPARE → complete** under ``DurableState.plan_admission()`` (TrialLedger batch event,
   COMMIT, admission checkpoint, anchor);
3. the candidate (``p7_plan.p7_strategy_candidates`` with the COMMIT as proof) joins the loop's
   catalog; the hypotheses become this round's trials with origin ``p7_plan``.

A refusal in step 1 is the plan's **rejection** (ADR-0103 D2): no byte is written and no trial is
registered; the hypothesis stage records ``p7_plan_rejection: {plan_hash, code, where}`` in its
summary (the ``LoopRecord`` format is unchanged) and ``rejected_plans`` — every plan rejected so
far, read back from the audit, so a rejected plan is never offered again. Anything that fails in
step 2 or 3 fails the stage (the durable state's own recovery applies, ADR-0073 §4).

**Restore** (ADR-0110). The candidate's memory-checkpoint row carries
``provenance: {origin: "p7", plan_hash, round_index}``; reopening the directory rebuilds it only
through ``p7_rebuild`` (``LoopWiring.p7_plans``): the durable state first finds the plan's
checkpointed COMMIT of that round in the plan admission journal, then ``P7CandidateRebuild``
recompiles the declared plan on the admission's own path (cross-sectional refusal, compile with the
source's switch and allowlist, strategy root and its Provider, the binding), requires every piece
of the PREPARE evidence — compiler, operators, Providers, inputs, outputs, bound ExperimentSpecs
and hypotheses — to be exactly what that recompilation produces, and builds the candidate with
``p7_strategy_candidates`` and the COMMIT as this round's proof. Any difference refuses the
directory (``P7RestoreRefused``, a ``ValueError``: the durable state reports it as an inconsistent
checkpoint).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from core.contracts.feature import FeatureProvider
from core.contracts.strategy import RiskProvider, StrategyProvider
from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import content_hash
from core.domain.research import ExperimentSpec, Hypothesis, HypothesisOrigin
from core.domain.specs import InstrumentType, RiskPolicy, StrategySpec
from research.hypotheses.p7_binding import P7PlanRecord, P7PlanRecordError, bind_experiment
from research.hypotheses.p7_evidence import (
    cross_check_admission_evidence,
    experiment_evidence,
    inputs_evidence,
    outputs_evidence,
)
from research.hypotheses.plan_bindings import (
    produce_lowered_output_bindings,
    validate_complete_experiment_bindings,
)
from research.hypotheses.typed_plan import PlanRefused, PlanRejected, TypedPlan
from research.hypotheses.typed_plan_audit import CommittedAdmission, RoundStartedIdentity
from research.hypotheses.typed_plan_compiler import (
    CompiledPlan,
    OperatorImplementation,
    P7ExecutionSwitch,
    compile_lowered_plan,
)
from research.hypotheses.typed_plan_resolver import DirectReferenceResolution
from research.loop.durable import (
    OPERATOR_STATE_VERSION,
    RETRY_STATE_VERSION,
    STATE_VERSION,
    DurableState,
    LoopStateInconsistent,
)
from research.loop.p7_plan import (
    CROSS_SECTIONAL_LOOP_UNSUPPORTED,
    has_cross_sectional_node,
    p7_strategy_candidates,
)
from research.loop.segment import trial_point
from research.loop.trials import _request_point
from research.strategies.pipeline import StrategyCandidate

__all__ = [
    "P7AdmissionRefused",
    "P7CandidateRebuild",
    "P7PlanRequest",
    "P7PlanSource",
    "P7RestoreRefused",
    "P7RoundAdmission",
    "P7RoundOutcome",
    "p7_rebuild",
    "rejected_plans",
]

#: The admission-journal state versions (ADR-0073 v4, ADR-0074 v5, ADR-0083 v6).
_ADMISSION_STATE_VERSIONS: Final = frozenset(
    {STATE_VERSION, OPERATOR_STATE_VERSION, RETRY_STATE_VERSION}
)
_WHERE_LIMIT: Final = 200


class P7AdmissionRefused(PlanRefused):
    """A declared plan cannot be admitted into this loop; ``code`` names the precise reason."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code}: {where}: {detail}")


class P7RestoreRefused(ValueError):
    """A P7-admitted catalog entry does not rebuild exactly on reopening (ADR-0110 §5)."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code}: {where}: {detail}")


def _descriptor_hash(provider: object, what: str) -> str:
    descriptor = getattr(provider, "descriptor", None)
    digest = getattr(descriptor, "content_hash", None)
    if not callable(digest):
        raise ValueError(f"{what} has no descriptor content hash")
    return str(digest())


@dataclass(frozen=True)
class P7PlanRequest:
    """One declared plan and everything its admission and execution need (module docs).

    ``experiment_specs`` are the declared ExperimentSpecs, one per hypothesis, **not yet** bound
    to the plan: the admission binds the plan record and node outputs into each. Provider maps
    are keyed by ``str(ref)`` of the plan's external inputs (``CompiledPlan.build_providers``).
    """

    plan: TypedPlan
    resolution: DirectReferenceResolution
    created_at: datetime
    hypotheses: tuple[Hypothesis, ...]
    experiment_specs: tuple[ExperimentSpec, ...]
    strategy_providers: Mapping[str, StrategyProvider] = field(default_factory=dict)
    feature_providers: Mapping[str, FeatureProvider] = field(default_factory=dict)
    bar_durations: Mapping[str, timedelta] = field(default_factory=dict)
    instrument_type: InstrumentType | None = None
    universes: tuple[ResearchDatasetManifest, ...] = ()
    risk_policy: RiskPolicy | None = None
    risk: RiskProvider | None = None

    def __post_init__(self) -> None:
        if type(self.plan) is not TypedPlan:
            raise ValueError("plan must be an exact TypedPlan")
        if not isinstance(self.resolution, DirectReferenceResolution):
            raise ValueError("resolution must be the plan's DirectReferenceResolution")
        if self.resolution.plan_hash != self.plan.content_hash():
            raise ValueError("resolution belongs to another plan")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be an explicit, timezone-aware datetime")
        for name, kind in (("hypotheses", Hypothesis), ("experiment_specs", ExperimentSpec)):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{name} must be a non-empty tuple")
            if any(type(item) is not kind for item in values):
                raise ValueError(f"{name} must contain exact {kind.__name__} values")
        if not isinstance(self.universes, tuple) or any(
            type(item) is not ResearchDatasetManifest for item in self.universes
        ):
            raise ValueError("universes must be a tuple of ResearchDatasetManifest")
        for name in ("strategy_providers", "feature_providers", "bar_durations"):
            if not isinstance(getattr(self, name), Mapping):
                raise ValueError(f"{name} must be a mapping")

    @property
    def plan_hash(self) -> str:
        return self.plan.content_hash()

    def payload(self) -> dict[str, Any]:
        """The fingerprinted declaration (Provider code is bound by ``code_commit``)."""
        return {
            "plan_hash": self.plan_hash,
            "created_at": self.created_at.isoformat(),
            "hypotheses": [item.content_hash() for item in self.hypotheses],
            "experiment_specs": [item.content_hash() for item in self.experiment_specs],
            "universes": [item.content_hash() for item in self.universes],
            "instrument_type": None if self.instrument_type is None else self.instrument_type.value,
            "strategy_providers": {
                key: _descriptor_hash(value, f"strategy provider {key}")
                for key, value in sorted(self.strategy_providers.items())
            },
            "feature_providers": {
                key: _descriptor_hash(value, f"feature provider {key}")
                for key, value in sorted(self.feature_providers.items())
            },
            "bar_durations_microseconds": {
                key: value // timedelta(microseconds=1)
                for key, value in sorted(self.bar_durations.items())
            },
            "risk_policy": None if self.risk_policy is None else self.risk_policy.content_hash(),
            "risk": None if self.risk is None else _descriptor_hash(self.risk, "risk provider"),
        }


@dataclass(frozen=True)
class P7PlanSource:
    """``LoopWiring.p7_plans``: the declared plans, the switch and the allowlist (module docs).

    The switch stays the caller's explicit choice: with it off every plan is rejected
    (``execution_disabled``) and recorded, never executed.
    """

    switch: P7ExecutionSwitch
    allowlist: Mapping[str, OperatorImplementation]
    plans: tuple[P7PlanRequest, ...]

    def __post_init__(self) -> None:
        if type(self.switch) is not P7ExecutionSwitch:
            raise ValueError("switch must be a P7ExecutionSwitch")
        if not isinstance(self.allowlist, Mapping) or any(
            type(item) is not OperatorImplementation or key != item.definition
            for key, item in self.allowlist.items()
        ):
            raise ValueError("allowlist must map definitions to their OperatorImplementation")
        if not isinstance(self.plans, tuple) or not self.plans:
            raise ValueError("plans must be a non-empty tuple")
        if any(type(item) is not P7PlanRequest for item in self.plans):
            raise ValueError("plans must contain only P7PlanRequest values")
        hashes = [item.plan_hash for item in self.plans]
        if len(set(hashes)) != len(hashes):
            raise ValueError("a plan is declared twice")

    def payload(self) -> dict[str, Any]:
        return {
            "execution_enabled": self.switch.enabled,
            "allowlist_hash": content_hash(
                {"allowlist": [self.allowlist[key].payload() for key in sorted(self.allowlist)]}
            ),
            "plans": [item.payload() for item in self.plans],
        }


@dataclass(frozen=True, slots=True)
class P7RoundOutcome:
    """What one round did with its pending plan: admitted (``admission`` + ``candidate``) or
    rejected (``code`` / ``where``)."""

    plan_hash: str
    admission: CommittedAdmission | None = None
    candidate: StrategyCandidate | None = None
    hypotheses: tuple[Hypothesis, ...] = ()
    code: str | None = None
    where: str | None = None

    def admitted_summary(self) -> dict[str, Any] | None:
        admission, candidate = self.admission, self.candidate
        if admission is None or candidate is None:
            return None
        return {
            "plan_hash": self.plan_hash,
            "transaction_id": admission.prepare.transaction_id,
            "manifest_hash": admission.prepare.manifest_hash,
            "commit_seq": admission.seq,
            "ledger_event_hash": admission.ledger_event_hash,
            "candidate": str(candidate.spec.ref),
            "registered": [str(item.ref) for item in self.hypotheses],
        }

    def rejection_summary(self) -> dict[str, str] | None:
        if self.code is None or self.where is None:
            return None
        return {"plan_hash": self.plan_hash, "code": self.code, "where": self.where}


def rejected_plans(records: Sequence[Any]) -> list[str]:
    """Every plan hash a recorded hypothesis stage rejected, in audit order (ADR-0103 D2)."""
    found: list[str] = []
    for record in records:
        for stage in record.stages:
            if stage.name != "hypothesis" or not isinstance(stage.summary, Mapping):
                continue
            rejection = stage.summary.get("p7_plan_rejection")
            if isinstance(rejection, Mapping) and isinstance(rejection.get("plan_hash"), str):
                found.append(str(rejection["plan_hash"]))
    return found


@dataclass(frozen=True)
class _Checked:
    """Everything step 1 produced for one plan (nothing written yet)."""

    compiled: CompiledPlan
    providers: dict[str, Any]
    experiments: tuple[ExperimentSpec, ...]


class P7RoundAdmission:
    """The per-round admission of ``LoopWiring.p7_plans`` over a durable state (module docs)."""

    def __init__(
        self, source: P7PlanSource, state: DurableState, *, loop_id: str, family_id: str
    ) -> None:
        if type(source) is not P7PlanSource:
            raise ValueError("source must be a P7PlanSource")
        if not isinstance(state, DurableState) or state.state_version not in (
            _ADMISSION_STATE_VERSIONS
        ):
            raise ValueError(
                "P7 plans are admitted only into a durable state directory with the plan "
                "admission journal (state v4 / v5 / v6)"
            )
        self._source = source
        self._state = state
        self._loop_id = loop_id
        self._family = family_id

    def rejected(self) -> list[str]:
        """Plans rejected in the recorded rounds (audit order)."""
        return rejected_plans(self._state.audit.records)

    def pending(self) -> P7PlanRequest | None:
        """The first declared plan neither rejected before nor fully registered already."""
        rejected = set(self.rejected())
        ledger = self._state.memory.ledger
        for request in self._source.plans:
            if request.plan_hash in rejected:
                continue
            if all(ledger.is_registered(item) for item in request.hypotheses):
                continue
            return request
        return None

    def trials(self) -> int:
        """The trials this round's admission would register (the stage's estimate)."""
        request = self.pending()
        return 0 if request is None else len(request.hypotheses)

    def run(self, round_index: int) -> P7RoundOutcome | None:
        """Admit or reject this round's pending plan (``None``: nothing pending)."""
        request = self.pending()
        if request is None:
            return None
        try:
            checked = self._check(request)
        except (PlanRefused, PlanRejected, ValueError, TypeError) as exc:
            # Step 1 wrote nothing: any refusal there is the plan's rejection (ADR-0103 D2).
            code = getattr(exc, "code", None)
            where = getattr(exc, "where", None) or getattr(exc, "node_id", None)
            return P7RoundOutcome(
                plan_hash=request.plan_hash,
                code=code if isinstance(code, str) and code else "invalid_plan_request",
                where=(str(where) if where else request.plan.root)[:_WHERE_LIMIT],
            )
        committed = self._admit(request, checked, round_index)
        (candidate,) = p7_strategy_candidates(
            checked.compiled,
            checked.providers,
            switch=self._source.switch,
            admission=committed,
            round_index=round_index,
            hypothesis_family_id=self._family,
            risk_policy=request.risk_policy,
            risk=request.risk,
        )
        self._state.memory.add_strategy(candidate)
        return P7RoundOutcome(
            plan_hash=request.plan_hash,
            admission=committed,
            candidate=candidate,
            hypotheses=committed.prepare.hypotheses,
        )

    # ------------------------------------------------------------------ step 1: pure checks

    def _check(self, request: P7PlanRequest) -> _Checked:
        plan = request.plan
        compiled, providers, root = _compile(self._source, request)
        provider = providers[plan.root]
        experiments = _bind(request, compiled)
        outputs = tuple(
            binding
            for experiment in experiments
            for binding in produce_lowered_output_bindings(
                experiment_hash=experiment.experiment_hash,
                plan=plan,
                specs_by_node=compiled.specs_by_node(),
            )
        )
        validate_complete_experiment_bindings(  # integrity
            experiment_specs=experiments,
            hypotheses=request.hypotheses,
            plans_by_experiment={item.experiment_hash: plan for item in experiments},
            lowered_outputs=outputs,
        )
        cross_check_admission_evidence(  # the PREPARE evidence is this plan's
            compiled=compiled,
            inputs=inputs_evidence(request.resolution),
            outputs=outputs_evidence(compiled),
            operators=compiled.operator_evidence(),
            experiment_specs=experiments,
            experiment_evidence_items=experiment_evidence(experiments),
            hypotheses=request.hypotheses,
        )
        candidate = StrategyCandidate(  # the risk wiring must hold before anything is written
            spec=root,
            strategy=provider,
            hypothesis_family_id=self._family,
            risk_policy=request.risk_policy,
            risk=request.risk,
        )
        self._check_hypotheses(request, compiled, candidate)
        return _Checked(compiled=compiled, providers=providers, experiments=experiments)

    def _check_hypotheses(
        self, request: P7PlanRequest, compiled: CompiledPlan, candidate: StrategyCandidate
    ) -> None:
        root = candidate.spec
        known = {(item.name, item.version) for item in self._state.memory.ledger.hypotheses}
        for index, hypothesis in enumerate(request.hypotheses):
            where = f"hypotheses[{index}]"
            if hypothesis.origin is HypothesisOrigin.LLM:
                raise P7AdmissionRefused(
                    "llm_hypothesis_not_admitted", where, "an unreviewed LLM hypothesis"
                )
            if hypothesis.family_id != self._family:
                raise P7AdmissionRefused(
                    "foreign_family", where, f"{hypothesis.family_id!r} is not the loop's family"
                )
            if (hypothesis.name, hypothesis.version) in known:
                raise P7AdmissionRefused(
                    "hypothesis_already_registered", where, f"{hypothesis.ref} is in the ledger"
                )
            point = trial_point(hypothesis)  # ValueError: not runnable as declared
            if point.strategy != f"{root.name}@{root.version}":
                raise P7AdmissionRefused(
                    "hypothesis_strategy_mismatch",
                    where,
                    f"names strategy {point.strategy}, the plan root is {root.ref}",
                )
            if point.p7_plan != compiled.plan_hash:
                raise P7AdmissionRefused(
                    "hypothesis_plan_mismatch", where, "does not name this plan's hash"
                )
            _request_point(candidate, point.overrides)

    # ------------------------------------------------------------------ step 2: PREPARE → COMMIT

    def _admit(
        self, request: P7PlanRequest, checked: _Checked, round_index: int
    ) -> CommittedAdmission:
        state = self._state
        identity = state.audit.started_entry_identity(self._loop_id, round_index)
        if identity is None:
            raise LoopStateInconsistent(
                f"round {round_index} has no persisted start: a P7 plan cannot be admitted"
            )
        compiled = checked.compiled
        with state.plan_admission() as lease:
            lease.prepare(
                round=RoundStartedIdentity(self._loop_id, round_index, identity[0], identity[1]),
                plan=request.plan,
                compiler=compiled.compiler_evidence(),
                operators=compiled.operator_evidence(),
                providers=compiled.provider_evidence(),
                inputs=inputs_evidence(request.resolution),
                outputs=outputs_evidence(compiled),
                experiment_specs=experiment_evidence(checked.experiments),
                hypotheses=request.hypotheses,
            )
            return lease.complete()


# ---------------------------------------------------------------------- shared with the restore


def _compile(
    source: P7PlanSource, request: P7PlanRequest
) -> tuple[CompiledPlan, dict[str, Any], StrategySpec]:
    """Step 1's compile part: cross-sectional refusal, compile, strategy root, its Provider."""
    plan = request.plan
    cross_sectional = has_cross_sectional_node(plan)
    if cross_sectional is not None:
        raise P7AdmissionRefused(
            CROSS_SECTIONAL_LOOP_UNSUPPORTED,
            cross_sectional,
            "the research loop is single-instrument; a cross-sectional plan needs its own ADR",
        )
    compiled = compile_lowered_plan(
        plan,
        resolution=request.resolution,
        created_at=request.created_at,
        allowlist=source.allowlist,
        switch=source.switch,
        universes=request.universes,
    )
    root = compiled.root.spec
    if type(root) is not StrategySpec:
        raise P7AdmissionRefused(
            "root_not_strategy", plan.root, "only a strategy root can enter the loop"
        )
    providers = compiled.build_providers(
        feature_providers=request.feature_providers,
        strategy_providers=request.strategy_providers,
        bar_durations=request.bar_durations,
        instrument_type=request.instrument_type,
        universes=request.universes,
    )
    provider = providers.get(plan.root)
    if provider is None or not provider.descriptor.supports(root.ref, root.content_hash()):
        raise P7AdmissionRefused(
            "root_provider_mismatch", plan.root, "the root Provider does not serve the root"
        )
    return compiled, providers, root


def _bind(request: P7PlanRequest, compiled: CompiledPlan) -> tuple[ExperimentSpec, ...]:
    """Step 1's binding: the plan record into every declared ExperimentSpec."""
    try:
        record = P7PlanRecord.from_compiled(compiled)
        return tuple(bind_experiment(item, record) for item in request.experiment_specs)
    except P7PlanRecordError as exc:
        raise P7AdmissionRefused("plan_binding_refused", "experiment_specs", str(exc)) from exc


def _hashes(items: Sequence[Any]) -> list[str]:
    return [item.content_hash for item in items]


@dataclass(frozen=True)
class P7CandidateRebuild:
    """``open_state(p7_rebuild=...)``: a P7-admitted catalog entry rebuilt on reopening (ADR-0110
    §2 / §5; module docs, **Restore**). Called with the row's ``plan_hash``, the plan's COMMIT of
    the row's round (found and checked by the durable state) and that round's index."""

    source: P7PlanSource
    family_id: str

    def __call__(
        self, plan_hash: str, admission: CommittedAdmission, round_index: int
    ) -> StrategyCandidate:
        try:
            return self._rebuild(plan_hash, admission, round_index)
        except (PlanRefused, PlanRejected, TypeError) as exc:
            code = getattr(exc, "code", None)
            raise P7RestoreRefused(
                code if isinstance(code, str) and code else "plan_not_rebuilt",
                plan_hash,
                str(exc),
            ) from exc

    def _rebuild(
        self, plan_hash: str, admission: CommittedAdmission, round_index: int
    ) -> StrategyCandidate:
        request = next((item for item in self.source.plans if item.plan_hash == plan_hash), None)
        if request is None:
            raise P7RestoreRefused(
                "plan_not_declared", plan_hash, "LoopWiring.p7_plans does not declare this plan"
            )
        compiled, providers, _ = _compile(self.source, request)
        experiments = _bind(request, compiled)
        prepared = admission.prepare
        recompiled = {
            "compiler": [compiled.compiler_evidence().content_hash],
            "operators": _hashes(compiled.operator_evidence()),
            "providers": _hashes(compiled.provider_evidence()),
            "inputs": _hashes(inputs_evidence(request.resolution)),
            "outputs": _hashes(outputs_evidence(compiled)),
            "experiment_specs": _hashes(experiment_evidence(experiments)),
            "hypotheses": sorted(item.content_hash() for item in request.hypotheses),
        }
        recorded = {
            "compiler": [prepared.compiler.content_hash],
            "operators": _hashes(prepared.operators),
            "providers": _hashes(prepared.providers),
            "inputs": _hashes(prepared.inputs),
            "outputs": _hashes(prepared.outputs),
            "experiment_specs": _hashes(prepared.experiment_specs),
            "hypotheses": sorted(item.content_hash() for item in prepared.hypotheses),
        }
        differ = sorted(key for key in recompiled if recompiled[key] != recorded[key])
        if differ:
            raise P7RestoreRefused(
                "admission_evidence_mismatch",
                plan_hash,
                f"the declared plan no longer recompiles to its PREPARE evidence ({differ})",
            )
        (candidate,) = p7_strategy_candidates(
            compiled,
            providers,
            switch=self.source.switch,
            admission=admission,
            round_index=round_index,
            hypothesis_family_id=self.family_id,
            risk_policy=request.risk_policy,
            risk=request.risk,
        )
        return candidate


def p7_rebuild(source: P7PlanSource | None, *, family_id: str) -> P7CandidateRebuild | None:
    """The composition's ``open_state(p7_rebuild=...)`` for ``LoopWiring.p7_plans`` (``None``
    without a source: a P7-admitted row is then refused on reopening)."""
    return None if source is None else P7CandidateRebuild(source, family_id)
