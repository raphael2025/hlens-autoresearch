"""Experiment and validation stages of the loop on the real Phase 4 / 5 / 6 / 8 components.

W2 wiring (ADR-0049 implementation note, 2026-09-25). Every number comes from ``TrialComponents``
(or a stage parameter); every validation threshold is read from the bound ``ValidationProfile`` or
from explicit ``RobustnessParams`` by ``research/validation`` itself.

``ExperimentStage`` — one trial per hypothesis registered this round (by the hypothesis stage and,
when present, the evolution stage) and per re-evaluation the hypothesis stage pre-registered, all
on the round's **accumulated research data** (every research-window bar ingested up to ``as_of``;
ADR-0049 accumulated-window note):

1. the hypothesis must already be in the ``TrialLedger`` with exactly this content, and a
   re-evaluation must be registered there as its own trial (``attempt``), before it runs
   (Constitution A1 / C-T1); otherwise the trial is ERRORED and not run;
2. ``segment.trial_point`` resolves the strategy (catalog of ``StrategyCandidate``) and the
   parameter point; an unresolvable hypothesis is an ERRORED trial (recorded, never dropped);
3. an ``ExperimentSpec`` + ``ExperimentRun`` carry the full reproducibility tuple of
   06-experiment.md §2 (hypothesis / strategy / risk / outcome refs, dataset snapshots = one per
   ingested market contributing research bars, with its time range, code commit, plugin versions,
   dependency hashes, params and search space, seeds, environment lock, Constitution version,
   Profile ref + hash + selection, split spec, cost model, LLM calls — for a re-evaluation, the
   LLM call its first trial cited);
4. ``CandidateTrialRunner`` runs strategy → risk → backtest on the accumulated research bars with
   the F4 ``bar_log_return`` signals of the state stage;
5. P6: the backtest's per-bar returns are compounded per decision period ``[d_i, d_i+1)`` and
   attributed to the P2 state evaluated at ``d_i`` (``state_strategy_matrix`` over the state
   result; the matrix binds the backtest and state result hashes);
6. a completed first trial moves CANDIDATE → VALIDATION (a re-evaluation is already there); an
   errored one is left for the memory stage (CANDIDATE → FAILED + FailureRecord; an errored
   re-evaluation moves VALIDATION → FAILED only when the subject's own code caused the error,
   ADR-0053 §2 — see ``TrialOutcome.subject_fault``; otherwise it only files its FailureRecord).

Fault attribution (ADR-0053 §2 ``RUN_ERRORED``): the experiment run calls the candidate's own
providers (strategy, risk) through ``_OwnStrategy`` / ``_OwnRisk``, which run the provider **and**
its ``check_answers`` inside ``_subject_code``: an error there is the subject's
(``SubjectRunError``, ``TrialOutcome.subject_fault = True``). Every other error of the trial —
building the request from the round's data, the shared backtester, the State × Strategy matrix, a
segment without a decision time — and any ``MemoryError`` / ``OSError`` even inside the provider is
infrastructure, never attributed to the subject. The proxies keep the providers' descriptors, so
every recorded hash is unchanged; the validator re-runs the unwrapped candidate.

Conditional hypotheses (P6 in the loop; opt-in, 2026-09-26, CODE_COMPLETE / DEBUG_PENDING —
decided by Claude under Raphael's 2026-09-26 autonomous-decision instruction). Only with an
explicit ``ConditionalPlan`` (``LoopWiring.conditional``; ``None`` by default: nothing below
happens and every record is byte-identical to a loop without the field). Then, right after a
trial's matrix is computed and before any per-cell number is read, every cell of the matrix — the
declared ``StateSpec.state_space`` plus the unknown-state cell, never a subset chosen by results —
is registered in the ``TrialLedger`` as a conditioning hypothesis of the trial's hypothesis
(``research.experiments.register_trial_conditionals``): the trial's family, one counted trial per
cell and look — the hypothesis's first trial registers the cells, a re-evaluation (attempt ``a``)
registers one re-evaluation of every cell under ``a``. They therefore enter the family trial
count that ``ValidationStage`` hands G3 (``family_trial_count``), from this round on. The trial's
experiment row carries ``conditional``: the registrations (hypothesis, trial index), the plan's
``minimum_effect`` / ``min_support`` and each cell's sample support against ``min_support``
(``meets_min_support`` / ``below_min_support`` / ``no_support_threshold``) — a count, not a
verdict. An errored trial has no matrix, so nothing is registered for it (``conditional: null``).
The stage declares ``trials = cells × trials this round`` (an upper bound: the runner charges
``max(estimate, usage)``), so the conditional trials are charged to the ``LoopBudget`` like every
other registration. With ``ConditionalPlan.validate_cells=False`` **per-cell validation is not
run** (no gate sees a cell's returns; ``validation`` = ``PER_CELL_VALIDATION``): the registrations
are the pre-commitment and the honest trial count.

Per-cell validation (P6, opt-in, 2026-09-26, CODE_COMPLETE / DEBUG_PENDING — decided by Claude
under Raphael's 2026-09-26 autonomous-decision instruction; no core / contract / Schema change).
With ``validate_cells=True`` the experiment row's ``validation`` is ``PER_CELL_VALIDATION_RUN``
and ``ValidationStage`` validates the cells of every completed trial it validates, right after
the trial's own report, in the same stage (so every cell of every trial of the round is already
in the family count):

- the cells are exactly the experiment row's registrations (declared state space + unknown cell,
  in order); a cell whose recorded support is not ``meets_min_support`` (below ``min_support``,
  or ``min_support=None``) is recorded ``unsupported`` and **never validated** (no gate, no
  verdict — never a PASS);
- a supported cell's evidence is the trial's non-flat targets (one re-run of the chosen point per
  trial, shared by its cells) whose decision time the state stage attributed to that cell — the
  same causal ``evaluation_time -> state`` attribution the matrix used (the ``state`` stage's
  ``labels``; nothing is recomputed); a decision time with no attributed state belongs to no cell
  (counted as ``unattributed_traded``);
- the gates are the trial report's adapter gates (``G0.backtest_cost_model``, the instrument gate,
  ``G0.execution_model`` / ``G0.manifest_binding`` when present — computed on the same re-run
  bars), then ``research.validation.run_in_sample`` (G0 → G3) on the cell's labels, with
  ``trial_index`` = the cell's registered trial index and ``family_trial_count`` = **the same**
  count the trial's own report used (every cell already counted: the G3 correction covers them);
  a cell without a non-flat target is ``G0.data_available`` = ``INCONCLUSIVE``; a non-PASS
  adapter gate stops there, as in ``PipelineBacktestValidator``;
- G4 and G5 are **not run** for a cell (recorded ``not_run`` with the reason): its verdict is the
  G0 – G3 in-sample verdict only. A cell whose trial's validation errored is ``not_run``;
- the per-cell rows are recorded in the validation report row (``conditional_cells``), hence in
  the audit and the durable checkpoint. **No cell verdict moves any lifecycle state** and no cell
  FAIL files a ``FailureRecord``: a cell hypothesis is not a lifecycle subject (it is registered,
  never advanced), and a PASS on in-sample G0 – G3 alone would need its own G4 robustness and G5
  sealed-OOS path before it could mean anything — which does not exist for cell hypotheses. The
  trial's own lifecycle move depends only on the trial's own report, as without the plan;
- compute: each validated cell is charged one ``compute_seconds_per_validation`` (the estimate
  counts every supported cell of the round's completed trials; an upper bound).

``ValidationStage`` — ``PipelineBacktestValidator`` (G0 → G3, then G4 robustness) for every
completed trial, with a ``ValidationContext`` bound to the trial's run and an ``ExperimentMetadata``
whose ``trial_index`` / ``family_trial_count`` come from the ledger's trial log (failures and every
re-evaluation included, so G3 corrects for each look at the accumulated data). G5 (sealed
OOS) runs only when an explicit ``OosUnsealBudget`` is configured **and lists the family** with its
approving human, the in-sample verdict is PASS, the round has sealed data and the family has not
used its one unsealing; otherwise the sealed window stays sealed and G5 is simply not run. Once
the family is unsealed, its single evaluation is claimed (``SealedOosVault.claim_evaluation``)
before any sealed bar is released, so it is consumed atomically: if the sealed run then ends
without a statistic (no sealed decision time, no non-flat target, an error) the G5 report is
``INCONCLUSIVE`` with ``G5.oos_evaluation`` = ``consumed_without_result:<reason>`` and the window
stays closed for good (ADR-0049 review fixes 2). Whether a round could evaluate the window at all
is asked of its ``RoundData.sealed`` source (``evaluable``) without reading any sealed data; a
dataset source reads its sealed manifest pair only on ``release``, after the claim, and a refused
pair ends as ``consumed_without_result:sealed_data_refused``; its G5 report also carries
``G0.manifest_binding`` for the sealed pair (ADR-0049 implementation note, dataset G5). The stage
makes no lifecycle move; the memory stage does (PASS → OOS at most; FAIL → REJECTED).

The unsealing ledger must be durable (ADR-0049 implementation note, review fixes 4): an unseal
budget over an in-memory ledger is refused at construction and again before every unsealing
(``require_durable_unsealing``), because a restarted process would forget the unsealing and could
run G5 for the same family again. The TEST-ONLY ``OosUnsealBudget.ephemeral_unseal_for_tests``
flag is the only way around it and is written into the stage summary (``unseal_ledger``) and into
every unsealed G5 status (``EPHEMERAL_UNSEAL_MARK``).

Record hashes never depend on the wall clock: reports and metadata are stamped with the round's
scheduled time, and floats are written as quantized Decimal text (``segment.decimal_text``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Final

from apps.worker.loop import RoundContext, StageFailed, StageResult, StageUsage
from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabelSpec,
    OutcomePriceBar,
    OutcomeProvider,
    OutcomeRequest,
)
from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.state import StateResult
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    RiskProvider,
    RiskProviderDescriptor,
    RiskRequest,
    RiskResult,
    SignalObservation,
    StrategyProvider,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import FrozenMapping, Ref, content_hash
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    GateResult,
    Hypothesis,
    LlmCall,
    ReproducibilityTuple,
    RunState,
    ValidationReport,
    Verdict,
)
from core.domain.selection import ProfileSelection
from core.domain.specs import StateSpec
from core.errors import ReasonCode
from core.lifecycle.strategy import LifecycleState
from research.experiments import (
    RETURN_QUANTUM,
    ConditionalRegistration,
    StateStrategyMatrix,
    backtest_returns,
    register_trial_conditionals,
    state_strategy_matrix,
)
from research.loop.memory import ResearchMemory
from research.loop.segment import (
    Param,
    RoundData,
    SealedDataRefused,
    decimal_text,
    decision_grid,
    trial_point,
)
from research.outcomes.table import materialize
from research.strategies.pipeline import CandidateTrialRunner, EvaluationInputs, StrategyCandidate
from research.strategies.validation import (
    BacktestValidation,
    PipelineBacktestValidator,
    TrialRun,
    ValidatorSetup,
    binding_mismatches,
)
from research.validation import (
    InSampleInput,
    RobustnessParams,
    SealedOosInput,
    ValidationContext,
    build_report,
    reason_for_gate,
    run_in_sample,
    run_sealed_oos,
)
from research.validation.controls import FixedSides
from research.validation.gates import flag_gate, inconclusive_gate
from research.validation.pipeline import CONSUMED_WITHOUT_RESULT, sealed_oos_without_result
from research.validation.sealed_oos import (
    DurableUnsealingLedger,
    OosBudgetExhausted,
    SealedEvaluation,
    SealedOosVault,
    UnsealingLedger,
)

__all__ = [
    "EPHEMERAL_UNSEAL_MARK",
    "CELL_G4_NOT_RUN",
    "CELL_G5_NOT_RUN",
    "CELL_LIFECYCLE",
    "PER_CELL_VALIDATION",
    "PER_CELL_VALIDATION_RUN",
    "ConditionalPlan",
    "ExperimentStage",
    "OosUnsealBudget",
    "SubjectRunError",
    "TrialComponents",
    "TrialOutcome",
    "ValidationOutcome",
    "ValidationStage",
    "failure_of",
    "require_durable_unsealing",
]

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_AUTOMATION_PREFIX: Final = "research_loop:"


@dataclass(frozen=True)
class TrialComponents:
    """What experiments and validations bind. No field has a default: absence must be explicit."""

    backtester: BacktestProvider
    cost_model: CostModelSpec
    initial_equity: Decimal
    outcome_provider: OutcomeProvider
    label_spec: OutcomeLabelSpec
    robustness: RobustnessParams
    profile: ValidationProfile
    profile_selection: ProfileSelection
    constitution_version: str
    declared_research_class: str
    code_commit: str
    environment_lock: str

    @property
    def backtest_costs(self) -> BacktestCostModel:
        """The backtester's cost model: exactly the rates of the bound ``CostModelSpec``."""
        return BacktestCostModel(
            name=self.cost_model.name,
            version=self.cost_model.version,
            fee_rate=self.cost_model.fee_rate_per_side,
            slippage_rate=self.cost_model.slippage_rate_per_side,
        )


#: What the loop records about the validation of a registered cell hypothesis (module docs).
PER_CELL_VALIDATION: Final = "not_run: registration only (per-cell validation is a follow-up)"
#: ... with ``ConditionalPlan.validate_cells=True`` (module docs, **Per-cell validation**).
PER_CELL_VALIDATION_RUN: Final = (
    "in_sample_g0_g3: supported cells validated by the validation stage (conditional_cells); "
    "G4 / G5 not run"
)
#: Why a cell's G4 / G5 are not run, and why its verdict moves nothing (module docs).
CELL_G4_NOT_RUN: Final = "not_run: per-cell validation is in-sample G0 - G3 only"
CELL_G5_NOT_RUN: Final = "not_run: a cell hypothesis has no sealed OOS path"
CELL_LIFECYCLE: Final = (
    "unchanged: a cell hypothesis is not a lifecycle subject; a G0 - G3 verdict alone would need "
    "its own G4 / G5 path"
)
#: The trial report's adapter gates a cell report repeats (``PipelineBacktestValidator``).
_ADAPTER_GATES: Final = frozenset(
    {
        "G0.backtest_cost_model",
        "G0.single_instrument_adapter",
        "G0.instrument_scope",
        "G0.execution_model",
        "G0.manifest_binding",
    }
)
_LABEL: Final = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class ConditionalPlan:
    """Opt-in: register every cell of each trial's State × Strategy matrix (module docs).

    Every field is required (no defaults): ``minimum_effect`` is the conditioning hypotheses'
    declared minimum meaningful effect (non-blank text); ``min_support`` is the per-cell sample
    support threshold the stage summary reports against (a positive int, or an explicit ``None``:
    no threshold stated, every cell reported ``no_support_threshold``). ``min_support`` is not a
    Validation Profile number and loosens no gate: it only decides which cells are *eligible* for
    per-cell validation (a cell below it — every cell when it is ``None`` — is never validated).
    ``validate_cells`` (a ``bool``): ``False`` registers the cells only (as before per-cell
    validation existed); ``True`` also runs the in-sample G0 – G3 gates on every supported cell
    (module docs, **Per-cell validation**).
    """

    minimum_effect: str
    min_support: int | None
    validate_cells: bool

    def __post_init__(self) -> None:
        if not isinstance(self.minimum_effect, str) or not self.minimum_effect.strip():
            raise ValueError("a ConditionalPlan needs a non-blank minimum_effect")
        support = self.min_support
        if support is not None and (
            isinstance(support, bool) or not isinstance(support, int) or support < 1
        ):
            raise ValueError("min_support must be a positive int, or None when none is stated")
        if not isinstance(self.validate_cells, bool):
            raise ValueError("validate_cells must be a bool")

    def payload(self) -> dict[str, Any]:
        """The plan as fingerprinted and recorded.

        ``validate_cells`` appears only when ``True``: a registration-only plan's payload (hence
        its fingerprint and every record) is exactly what it was before per-cell validation, and
        the two settings still fingerprint differently (a state directory is bound to one)."""
        payload: dict[str, Any] = {
            "minimum_effect": self.minimum_effect,
            "min_support": self.min_support,
        }
        if self.validate_cells:
            payload["validate_cells"] = True
        return payload


@dataclass(frozen=True)
class OosUnsealBudget:
    """Explicit, per-family permission to unseal the sealed OOS window (C-S2).

    Without this object the loop never unseals anything. ``approved_families`` maps every
    hypothesis family a human has approved for its one unsealing to **that** human's identity
    (recorded as ``approved_by`` in the family's ``OosUnsealing``); the loop may only unseal a
    family on this list, so a budget signed once at configuration time cannot be spent on
    families nobody looked at (ADR-0049 review fixes 2). ``max_unsealings`` still bounds the
    global count (``SealedOosVault``). An empty list, a blank family id or an automation
    identity is refused.

    Durable ledger (ADR-0049 implementation note, review fixes 4, 2026-09-26): an unseal budget
    is spent against an unsealing ledger that must survive a restart — ``DurableUnsealingLedger``
    (a ``state_dir``, or ``ResearchMemory(oos_ledger=DurableUnsealingLedger(path))``). An
    in-memory ledger forgets its unsealings when the process ends, so a restarted loop could
    unseal the same family again: ``ValidationStage`` refuses the combination. The only exception
    is ``ephemeral_unseal_for_tests=True``, a **TEST-ONLY** flag for a non-durable ledger; it is
    bound into the loop fingerprint and written into every G5 status and validation summary it
    touches (``EPHEMERAL_UNSEAL_MARK``), so such a run can never be mistaken for real use. It is
    refused with a durable ledger and with a ``state_dir``.
    """

    max_unsealings: int
    approved_families: Mapping[str, str]
    #: TEST ONLY: allow a non-durable unsealing ledger (see the class docs). Never for research.
    ephemeral_unseal_for_tests: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_unsealings, bool) or self.max_unsealings < 1:
            raise ValueError("max_unsealings must be a positive int")
        if not isinstance(self.ephemeral_unseal_for_tests, bool):
            raise ValueError("ephemeral_unseal_for_tests must be a bool")
        if not isinstance(self.approved_families, Mapping) or not self.approved_families:
            raise ValueError("an unseal budget needs at least one explicitly approved family")
        approved: dict[str, str] = {}
        for family, approver in self.approved_families.items():
            key = family.strip() if isinstance(family, str) else ""
            who = approver.strip() if isinstance(approver, str) else ""
            if not key:
                raise ValueError("an approved family id must not be blank")
            if key in approved:
                raise ValueError(f"family {key!r} is listed twice")
            if not who or who.startswith(_AUTOMATION_PREFIX):
                raise ValueError(f"family {key!r} needs the approving human's identity")
            approved[key] = who
        object.__setattr__(self, "approved_families", FrozenMapping(approved))

    def approver_of(self, family_id: str) -> str | None:
        """The human who approved ``family_id``'s unsealing (``None``: not approved)."""
        return self.approved_families.get(family_id)


class SubjectRunError(Exception):
    """ADR-0053 §2 ``RUN_ERRORED``: the candidate's own provider raised, or its answer failed
    ``check_answers``. The original error is ``__cause__`` (module docs, "Fault attribution")."""


#: Never attributed to the subject, even when raised inside its provider: memory, storage and
#: network faults are infrastructure (ADR-0053 §2).
_INFRASTRUCTURE_ERRORS: Final = (MemoryError, OSError)


@contextmanager
def _subject_code() -> Iterator[None]:
    try:
        yield
    except _INFRASTRUCTURE_ERRORS:
        raise
    except Exception as exc:
        raise SubjectRunError(f"{type(exc).__name__}: {exc}") from exc


class _OwnStrategy:
    """The candidate's strategy provider with fault attribution (same descriptor)."""

    def __init__(self, inner: StrategyProvider) -> None:
        self._inner = inner

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._inner.descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        with _subject_code():
            answer = self._inner.target_positions(request)
            answer.check_answers(request, self._inner.descriptor)
        return answer


class _OwnRisk:
    """The candidate's risk provider with fault attribution (same descriptor)."""

    def __init__(self, inner: RiskProvider) -> None:
        self._inner = inner

    @property
    def descriptor(self) -> RiskProviderDescriptor:
        return self._inner.descriptor

    def constrain(self, request: RiskRequest) -> RiskResult:
        with _subject_code():
            answer = self._inner.constrain(request)
            answer.check_answers(request, self._inner.descriptor)
        return answer


def _attributed(candidate: StrategyCandidate) -> StrategyCandidate:
    """``candidate`` whose own providers raise ``SubjectRunError`` (module docs)."""
    risk = None if candidate.risk is None else _OwnRisk(candidate.risk)
    return replace(candidate, strategy=_OwnStrategy(candidate.strategy), risk=risk)


@dataclass(frozen=True)
class TrialOutcome:
    """One trial of one round: its reproducibility records and what the run produced."""

    round_index: int
    hypothesis: Hypothesis
    origin: str
    experiment: ExperimentSpec
    run: ExperimentRun
    candidate: StrategyCandidate | None
    request_params: Mapping[str, Param]
    inputs: EvaluationInputs | None
    trial: TrialRun | None
    validation_seed: int
    summary: Mapping[str, Any]
    error: str | None = None
    reason: ReasonCode | None = None
    #: ``None``: the hypothesis's first trial (its registration); otherwise the re-evaluation's
    #: ``TrialLedger`` attempt key.
    attempt: str | None = None
    #: The research data's end the trial evaluated up to (``inputs.knowledge_cutoff``; ``None``
    #: when no inputs were built). Kept apart from ``inputs`` because a trial restored from a
    #: durable state directory (``research.loop.durable``) carries no in-memory ``inputs`` /
    #: ``trial`` artifacts — only what later rounds read.
    knowledge_cutoff: datetime | None = None
    #: ADR-0053 §2: the run errored in the subject's own code (``SubjectRunError``), not in the
    #: infrastructure. Only an errored run of this process can set it; a trial restored from a
    #: durable state directory keeps ``False`` (its lifecycle move is already in the audit).
    subject_fault: bool = False

    @property
    def completed(self) -> bool:
        """The run completed (``trial`` is set for every completed run of this process)."""
        return self.run.state is RunState.COMPLETED


@dataclass(frozen=True)
class ValidationOutcome:
    """The validation of one completed trial (in-sample report and, when run, the G5 report)."""

    round_index: int
    outcome: TrialOutcome
    report: ValidationReport | None
    failure_reason: ReasonCode | None
    sealed_report: ValidationReport | None
    sealed_status: Mapping[str, Any]
    summary: Mapping[str, Any]
    error: str | None = None

    @property
    def verdict(self) -> Verdict | None:
        return None if self.report is None else self.report.verdict

    def gate_value(self, gate_id: str) -> float | None:
        if self.report is None:
            return None
        return next((g.value for g in self.report.gates if g.gate_id == gate_id), None)


#: What the audit carries when an unseal budget runs on a non-durable ledger (TEST ONLY).
EPHEMERAL_UNSEAL_MARK: Final = "EPHEMERAL_TEST_ONLY: in-memory unsealing ledger, lost on restart"


def require_durable_unsealing(budget: OosUnsealBudget | None, ledger: UnsealingLedger) -> None:
    """Refuse an unseal budget whose ledger would not survive a restart (``OosUnsealBudget`` docs).

    ``DurableUnsealingLedger`` is the only ledger accepted as durable (another durable
    ``UnsealingLedger`` must be added here explicitly: fail closed)."""
    if budget is None:
        return
    durable = isinstance(ledger, DurableUnsealingLedger)
    if budget.ephemeral_unseal_for_tests:
        if durable:
            raise ValueError(
                "ephemeral_unseal_for_tests is a TEST-ONLY flag for a non-durable unsealing "
                "ledger; this loop's ledger is durable: drop the flag"
            )
        return
    if not durable:
        raise ValueError(
            "an OosUnsealBudget needs a durable unsealing ledger (a state_dir, or "
            "ResearchMemory(oos_ledger=DurableUnsealingLedger(path))): an in-memory ledger "
            "forgets its unsealings on a restart and would let a family be unsealed again "
            f"(got {type(ledger).__name__}; ephemeral_unseal_for_tests=True is for tests only)"
        )


def _registered_this_round(ctx: RoundContext) -> tuple[tuple[Hypothesis, str, str | None], ...]:
    """This round's trials in order: new hypotheses, re-evaluations, then evolution offspring."""
    stage = ctx.artifacts.get("hypothesis", {})
    found: list[tuple[Hypothesis, str, str | None]] = [
        (h, "hypothesis", None) for h in stage.get("registered", ())
    ]
    found += [(h, "reevaluation", attempt) for h, attempt in stage.get("reevaluations", ())]
    found += [
        (h, "evolution", None) for h in ctx.artifacts.get("evolution", {}).get("registered", ())
    ]
    return tuple(found)


def _plugin(descriptor: Any) -> tuple[str, str]:
    return f"{descriptor.name}@{descriptor.version}", descriptor.content_hash()


def _seed(round_seed: int, hypothesis: Hypothesis) -> int:
    return int(content_hash({"round_seed": round_seed, "trial": str(hypothesis.ref)})[:8], 16)


def _llm_calls(
    ctx: RoundContext, hypothesis: Hypothesis, memory: ResearchMemory
) -> tuple[LlmCall, ...]:
    """The LLM call behind ``hypothesis``: this round's, else the one its first trial cited."""
    calls: Mapping[str, LlmCall] = ctx.artifacts.get("hypothesis", {}).get("llm_calls", {})
    call = calls.get(str(hypothesis.ref))
    if call is not None:
        return (call,)
    first = next((o for o in memory.trials if o.hypothesis.ref == hypothesis.ref), None)
    return () if first is None else tuple(first.run.repro.llm_calls)


def _period_returns(
    backtest_curve: Mapping[datetime, Decimal], decisions: Sequence[datetime]
) -> dict[datetime, Decimal]:
    """Per-bar returns compounded per decision period ``[d_i, d_i+1)`` (last: to end of data)."""
    out: dict[datetime, Decimal] = {}
    keys = sorted(backtest_curve)
    with localcontext(_CONTEXT):
        for index, start in enumerate(decisions):
            end = decisions[index + 1] if index + 1 < len(decisions) else None
            growth = Decimal(1)
            seen = False
            for key in keys:
                if key >= start and (end is None or key < end):
                    growth *= 1 + backtest_curve[key]
                    seen = True
            if seen:
                out[start] = (growth - 1).quantize(RETURN_QUANTUM)
    return out


def _request_point(
    candidate: StrategyCandidate, overrides: Mapping[str, Param]
) -> dict[str, Param]:
    """The declared parameters of the trial point: spec defaults overridden by the hypothesis."""
    spec = candidate.spec
    stray = sorted(set(overrides) - set(spec.param_search_space))
    if stray:
        raise ValueError(f"{spec.ref} declares no search space for {stray}")
    point: dict[str, Param] = {}
    for key, value in {**dict(spec.params), **dict(overrides)}.items():
        if key not in spec.param_search_space:
            continue
        if isinstance(value, float):
            raise ValueError(f"{spec.ref}: float parameter {key}={value!r} cannot be requested")
        point[key] = value
    return point


class ExperimentStage:
    """Runs every trial registered this round (see module docs)."""

    name = "experiment"

    def __init__(
        self,
        memory: ResearchMemory,
        components: TrialComponents,
        *,
        compute_seconds_per_trial: Decimal,
        conditional: ConditionalPlan | None = None,
        state_spec: StateSpec | None = None,
    ) -> None:
        if conditional is not None:
            if not isinstance(conditional, ConditionalPlan):
                raise ValueError("conditional must be a ConditionalPlan (or None)")
            if not isinstance(state_spec, StateSpec):
                raise ValueError("a ConditionalPlan needs the StateSpec that declares the cells")
            unnameable = [x for x in state_spec.state_space if not _LABEL.fullmatch(x)]
            if unnameable:
                raise ValueError(
                    f"state labels {unnameable} cannot name a cell hypothesis ([a-z0-9_]+)"
                )
        self._memory = memory
        self._c = components
        self._per_trial = compute_seconds_per_trial
        self._conditional = conditional
        self._state_spec = state_spec
        self._conditional_trials = 0

    def _cells(self) -> int:
        """Conditional trials one trial can register (0 without a plan)."""
        if self._conditional is None or self._state_spec is None:
            return 0
        return len(self._state_spec.state_space) + 1

    def estimate(self, ctx: RoundContext) -> StageUsage:
        trials = len(_registered_this_round(ctx))
        return StageUsage(trials=self._cells() * trials, compute_seconds=self._per_trial * trials)

    def run(self, ctx: RoundContext) -> StageResult:
        outcomes: list[TrialOutcome] = []
        self._conditional_trials = 0
        try:
            for hypothesis, origin, attempt in _registered_this_round(ctx):
                outcomes.append(self._trial(ctx, hypothesis, origin, attempt))
        except Exception as exc:
            raise StageFailed(
                f"{type(exc).__name__}: {exc}",
                usage=StageUsage(
                    trials=self._conditional_trials,
                    compute_seconds=self._per_trial * (len(outcomes) + 1),
                ),
            ) from exc
        self._memory.trials.extend(outcomes)
        self._memory.experiments.extend(
            {"round": ctx.round_index, **dict(o.summary)} for o in outcomes
        )
        return StageResult(
            {"experiments": [dict(o.summary) for o in outcomes]},
            StageUsage(
                trials=self._conditional_trials, compute_seconds=self._per_trial * len(outcomes)
            ),
            {"outcomes": tuple(outcomes)},
        )

    def _register_conditionals(
        self, hypothesis: Hypothesis, attempt: str | None, matrix: StateStrategyMatrix
    ) -> dict[str, Any]:
        """Every cell of ``matrix`` registered as a trial before any cell number is read."""
        plan, spec = self._conditional, self._state_spec
        assert plan is not None and spec is not None
        registration: ConditionalRegistration = register_trial_conditionals(
            self._memory.ledger,
            matrix,
            parent=hypothesis,
            attempt=attempt,
            state_spec=spec,
            minimum_effect=plan.minimum_effect,
            min_support=plan.min_support,
        )
        self._conditional_trials += registration.newly_registered
        return {
            "parent": registration.parent,
            "attempt": registration.attempt,
            "family_id": registration.family_id,
            "state": str(spec.ref),
            **plan.payload(),
            "matrix_hash": registration.matrix_hash,
            "newly_registered": registration.newly_registered,
            "family_trials": registration.family_trials,
            "cells": [
                {
                    "state": cell.state,
                    "hypothesis": cell.hypothesis,
                    "trial_index": cell.trial_index,
                    "count": cell.count,
                    "supported": cell.supported,
                    "support": cell.reason,
                }
                for cell in registration.cells
            ],
            "validation": PER_CELL_VALIDATION_RUN if plan.validate_cells else PER_CELL_VALIDATION,
        }

    # ------------------------------------------------------------------------------------------

    def _pre_registered(self, hypothesis: Hypothesis, attempt: str | None) -> bool:
        return self._memory.ledger.is_registered(hypothesis, attempt)

    def _repro(
        self,
        ctx: RoundContext,
        hypothesis: Hypothesis,
        candidate: StrategyCandidate | None,
        params: Mapping[str, Param],
        segment: RoundData,
        plugins: Mapping[str, str],
        seed: int,
    ) -> ReproducibilityTuple:
        c, profile = self._c, self._c.profile
        spec = None if candidate is None else candidate.spec
        dependencies = {
            str(hypothesis.ref): hypothesis.content_hash(),
            str(c.label_spec.outcome): c.label_spec.outcome_spec_hash,
            str(c.cost_model.ref): c.cost_model.content_hash(),
        }
        all_plugins = dict(plugins)
        all_plugins.update(dict([_plugin(c.backtester.descriptor)]))
        all_plugins.update(dict([_plugin(c.outcome_provider.descriptor)]))
        if candidate is not None and spec is not None:
            dependencies[str(spec.ref)] = spec.content_hash()
            all_plugins.update(dict([_plugin(candidate.strategy.descriptor)]))
            if candidate.risk_policy is not None and candidate.risk is not None:
                dependencies[str(candidate.risk_policy.ref)] = candidate.risk_policy.content_hash()
                all_plugins.update(dict([_plugin(candidate.risk.descriptor)]))
        first = segment.research_start or ctx.as_of
        last = segment.research_end or ctx.as_of
        boundary = profile.data_split.sealed_oos_boundary.isoformat()
        snapshots = segment.dataset_snapshots(ctx.as_of)
        return ReproducibilityTuple(
            hypothesis_ref=hypothesis.ref,
            strategy_ref=None if spec is None else spec.ref,
            risk_policy_ref=None if spec is None else spec.risk_policy,
            outcome_ref=c.label_spec.outcome,
            dataset_snapshots=snapshots,
            code_commit=c.code_commit,
            plugin_versions=FrozenMapping(all_plugins),
            dependency_hashes=FrozenMapping(dependencies),
            params=FrozenMapping({} if spec is None else {**dict(spec.params), **dict(params)}),
            param_search_space=FrozenMapping({} if spec is None else dict(spec.param_search_space)),
            seeds=(ctx.seed, seed),
            environment_lock=c.environment_lock,
            constitution_version=c.constitution_version,
            validation_profile=profile.ref,
            validation_profile_hash=profile.content_hash(),
            profile_selection=c.profile_selection,
            split_spec=(
                f"walk_forward({profile.ref}); research [{first.isoformat()}, "
                f"{last.isoformat()}]; sealed_oos from {boundary} "
                f"for {profile.data_split.sealed_oos_length}"
            ),
            cost_model_ref=c.cost_model.ref,
            llm_calls=_llm_calls(ctx, hypothesis, self._memory),
        )

    def _trial(
        self, ctx: RoundContext, hypothesis: Hypothesis, origin: str, attempt: str | None
    ) -> TrialOutcome:
        segment: RoundData = ctx.artifact("ingest", "segment")
        signals: tuple[SignalObservation, ...] = ctx.artifact("state", "signals")
        states: StateResult = ctx.artifact("state", "result")
        state_ref: Ref = ctx.artifact("state", "spec_ref")
        plugins: Mapping[str, str] = ctx.artifact("state", "plugins")
        seed = _seed(ctx.seed, hypothesis)
        candidate: StrategyCandidate | None = None
        params: dict[str, Param] = {}
        error: str | None = None
        reason: ReasonCode | None = None
        subject_fault = False
        if not self._pre_registered(hypothesis, attempt):
            error, reason = (
                f"{hypothesis.ref} was not pre-registered"
                + ("" if attempt is None else f" for the re-evaluation {attempt}"),
                ReasonCode.CONTRACT_VIOLATION,
            )
        else:
            try:
                point = trial_point(hypothesis)
                candidate = self._memory.strategies.get(f"strategy:{point.strategy}")
                if candidate is None:
                    raise ValueError(f"no strategy {point.strategy} in the loop's catalog")
                params = _request_point(candidate, point.overrides)
            except ValueError as exc:
                error, reason = str(exc), ReasonCode.CONTRACT_VIOLATION
                candidate = None
        repro = self._repro(ctx, hypothesis, candidate, params, segment, plugins, seed)
        experiment = ExperimentSpec(
            name=f"e_{hypothesis.name}",
            version=hypothesis.version,
            created_at=ctx.as_of,
            repro=repro,
        )
        run_id = f"{ctx.loop_id}:{ctx.round_index}:{hypothesis.name}@{hypothesis.version}"
        inputs: EvaluationInputs | None = None
        trial: TrialRun | None = None
        matrix: StateStrategyMatrix | None = None
        if candidate is not None and error is None:
            if not segment.decision_times or segment.research_end is None:
                error, reason = "the segment has no decision time", ReasonCode.RUN_ERRORED
            else:
                inputs = EvaluationInputs(
                    instruments=(segment.symbol,),
                    bars=segment.research_bars,
                    decision_times=segment.decision_times,
                    knowledge_cutoff=segment.research_end,
                    cost_model=self._c.backtest_costs,
                    initial_equity=self._c.initial_equity,
                    signals=signals,
                    params=params,
                )
                try:
                    trial = CandidateTrialRunner(
                        _attributed(candidate), inputs, self._c.backtester
                    ).run(params)
                    matrix = state_strategy_matrix(
                        candidate.spec.ref,
                        state_ref,
                        _period_returns(backtest_returns(trial.backtest), segment.decision_times),
                        states,
                    )
                    matrix = replace(matrix, backtest_result_hash=trial.backtest.result_hash)
                except SubjectRunError as exc:  # the subject's own code (ADR-0053 §2)
                    cause = exc.__cause__ if exc.__cause__ is not None else exc
                    error, reason, trial, matrix = (
                        f"{type(cause).__name__}: {cause}",
                        ReasonCode.RUN_ERRORED,
                        None,
                        None,
                    )
                    subject_fault = True
                except Exception as exc:  # noqa: BLE001 - an errored trial is recorded, not dropped
                    error, reason, trial, matrix = (
                        f"{type(exc).__name__}: {exc}",
                        ReasonCode.RUN_ERRORED,
                        None,
                        None,
                    )
        # opt-in P6 (module docs), outside the errored-trial handler: a registration that fails
        # (a ledger conflict) fails the stage; it never becomes a quietly errored trial
        conditional = (
            None
            if self._conditional is None or matrix is None
            else self._register_conditionals(hypothesis, attempt, matrix)
        )
        state = RunState.COMPLETED if trial is not None else RunState.ERRORED
        run = ExperimentRun(
            run_id=run_id,
            experiment=experiment.ref,
            repro=repro,
            state=state,
            started_at=ctx.as_of,
            finished_at=ctx.as_of,
        )
        summary: dict[str, Any] = {
            "hypothesis": str(hypothesis.ref),
            "origin": origin,
            "strategy": None if candidate is None else str(candidate.spec.ref),
            "params": {
                k: decimal_text(v) if isinstance(v, float) else v
                for k, v in sorted(repro.params.items())
            },
            "experiment": str(experiment.ref),
            "experiment_hash": repro.experiment_hash,
            "run_id": run_id,
            "run_state": state.value,
            "attempt": attempt,
            **segment.source_fields(),
            "research_data_hash": segment.data_hash,
            "research_start": _iso(segment.research_start),
            "research_end": _iso(segment.research_end),
            "research_bars": len(segment.research_bars),
            "error": None if error is None else error[:500],
        }
        if trial is not None:
            traded = sum(1 for t in trial.targets if t.target_weight != 0)
            final = trial.backtest.equity_curve[-1].equity if trial.backtest.equity_curve else None
            summary.update(
                backtest_result_hash=trial.backtest.result_hash,
                decisions=len(segment.decision_times),
                traded_decisions=traded,
                net_return=None
                if final is None
                else decimal_text(final / trial.backtest.initial_equity - 1),
                state_strategy_matrix_hash=None if matrix is None else matrix.matrix_hash,
            )
            if ctx.state_of(hypothesis.ref) is LifecycleState.CANDIDATE:  # a re-evaluation is
                ctx.advance(  # already in VALIDATION
                    hypothesis.ref,
                    LifecycleState.VALIDATION,
                    reason="experiment completed; awaiting validation",
                    evidence=(f"experiment:{repro.experiment_hash}", f"run:{run_id}"),
                )
        if self._conditional is not None:  # the key exists only with a plan (records unchanged)
            summary["conditional"] = conditional
        return TrialOutcome(
            round_index=ctx.round_index,
            hypothesis=hypothesis,
            origin=origin,
            experiment=experiment,
            run=run,
            candidate=candidate,
            request_params=FrozenMapping(params),
            inputs=inputs,
            trial=trial,
            validation_seed=seed,
            summary=summary,
            error=error,
            reason=reason,
            attempt=attempt,
            knowledge_cutoff=None if inputs is None else inputs.knowledge_cutoff,
            subject_fault=subject_fault,
        )


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _side(weight: Decimal) -> int:
    return (weight > 0) - (weight < 0)


def _event_key(target: TargetPosition) -> str:
    return f"{target.instrument}|{target.decision_time.isoformat()}"


def _gates(report: ValidationReport) -> list[dict[str, Any]]:
    return [
        {
            "gate_id": gate.gate_id,
            "value": decimal_text(gate.value),
            "threshold": decimal_text(gate.threshold),
            "threshold_source": gate.threshold_source,
            "verdict": gate.verdict.value,
        }
        for gate in report.gates
    ]


class ValidationStage:
    """G0 → G4 for every completed trial; G5 only with an explicit unseal budget (module docs)."""

    name = "validation"

    def __init__(
        self,
        memory: ResearchMemory,
        components: TrialComponents,
        *,
        compute_seconds_per_validation: Decimal,
        oos_unseal: OosUnsealBudget | None = None,
        sealed_decision_step: timedelta | None = None,
        conditional: ConditionalPlan | None = None,
    ) -> None:
        if (oos_unseal is None) != (sealed_decision_step is None):
            raise ValueError("an unseal budget needs a sealed decision step and vice versa")
        if conditional is not None and not isinstance(conditional, ConditionalPlan):
            raise ValueError("conditional must be a ConditionalPlan (or None)")
        require_durable_unsealing(oos_unseal, memory.oos_ledger)
        self._memory = memory
        self._c = components
        self._per = compute_seconds_per_validation
        self._unseal = oos_unseal
        self._sealed_step = sealed_decision_step
        #: per-cell validation only with an explicit ``validate_cells=True`` plan (module docs)
        self._cells = conditional is not None and conditional.validate_cells
        self._cells_validated = 0

    @staticmethod
    def _completed(ctx: RoundContext) -> tuple[TrialOutcome, ...]:
        outcomes: tuple[TrialOutcome, ...] = ctx.artifact("experiment", "outcomes")
        return tuple(o for o in outcomes if o.completed)

    def _supported_cells(self, ctx: RoundContext) -> int:
        """Cells this round can validate: every supported cell of a completed trial."""
        if not self._cells:
            return 0
        return sum(
            1
            for o in self._completed(ctx)
            for cell in (o.summary.get("conditional") or {}).get("cells", ())
            if cell["supported"]
        )

    def estimate(self, ctx: RoundContext) -> StageUsage:
        validations = len(self._completed(ctx)) + self._supported_cells(ctx)
        return StageUsage(compute_seconds=self._per * validations)

    def run(self, ctx: RoundContext) -> StageResult:
        results: list[ValidationOutcome] = []
        self._cells_validated = 0
        try:
            for outcome in self._completed(ctx):
                results.append(self._validate(ctx, outcome))
        except Exception as exc:
            spent = len(results) + 1 + (self._supported_cells(ctx) if self._cells else 0)
            raise StageFailed(
                f"{type(exc).__name__}: {exc}",
                usage=StageUsage(compute_seconds=self._per * spent),
            ) from exc
        self._memory.validations.extend(results)
        summary = {
            "scope": "PipelineBacktestValidator G0-G4; G5 only with an explicit unseal budget",
            "profile": str(self._c.profile.ref),
            "profile_hash": self._c.profile.content_hash(),
            "reports": [dict(r.summary) for r in results],
        }
        if self._unseal is not None and self._unseal.ephemeral_unseal_for_tests:
            summary["unseal_ledger"] = EPHEMERAL_UNSEAL_MARK
        return StageResult(
            summary,
            StageUsage(compute_seconds=self._per * (len(results) + self._cells_validated)),
            {"validations": tuple(results)},
        )

    # ------------------------------------------------------------------------------------------

    def _context(self, ctx: RoundContext, outcome: TrialOutcome) -> ValidationContext:
        hypothesis, c = outcome.hypothesis, self._c
        ledger = self._memory.ledger
        metadata = ExperimentMetadata(
            experiment_hash=outcome.run.experiment_hash,
            constitution_version=c.constitution_version,
            validation_profile=c.profile.ref,
            validation_profile_hash=c.profile.content_hash(),
            profile_selection=c.profile_selection,
            hypothesis_family_id=hypothesis.family_id,
            trial_index=ledger.trial_index(hypothesis, outcome.attempt),
            family_trial_count=ledger.trials(hypothesis.family_id),
            declared_research_class=c.declared_research_class,
            llm_calls=outcome.run.repro.llm_calls,
            trace_id=outcome.run.run_id,
            recorded_at=ctx.as_of,
        )
        assert outcome.candidate is not None
        return ValidationContext(
            report_id=content_hash({"run": outcome.run.run_id, "stage": "in_sample"}),
            subject=outcome.candidate.spec.ref,
            run=outcome.run,
            metadata=metadata,
            profile=c.profile,
            cost_model=c.cost_model,
            label_spec=c.label_spec,
        )

    def _validate(self, ctx: RoundContext, outcome: TrialOutcome) -> ValidationOutcome:
        segment: RoundData = ctx.artifact("ingest", "segment")
        labels: Mapping[datetime, str | None] = ctx.artifact("state", "labels")
        feature_manifests: Sequence[str] = ctx.artifact("state", "feature_manifest_hashes")
        candidate, inputs, trial = outcome.candidate, outcome.inputs, outcome.trial
        assert candidate is not None and inputs is not None and trial is not None
        context = self._context(ctx, outcome)
        setup = ValidatorSetup(
            context=context,
            outcome_provider=self._c.outcome_provider,
            manifest_content_hash=segment.manifest_content_hash,
            instrument=segment.symbol,
            trials=CandidateTrialRunner(candidate, inputs, self._c.backtester),
            chosen_params=dict(outcome.request_params),
            seed=outcome.validation_seed,
            robustness=self._c.robustness,
            state_of=lambda t: labels.get(t) or "unknown",
            bar_volume=segment.bar_volume(),
            declared_instruments=(segment.symbol,),
            # the dataset path's proven bars, manifest pair and feature manifests
            # (G0.manifest_binding); nothing on the synthetic path
            **segment.validator_binding(feature_manifests),
        )
        base: dict[str, Any] = {
            "hypothesis": str(outcome.hypothesis.ref),
            "origin": outcome.origin,
            "attempt": outcome.attempt,
            "subject": str(candidate.spec.ref),
            "experiment_hash": outcome.run.experiment_hash,
            "family_trial_count": context.metadata.family_trial_count,
            "trial_index": context.metadata.trial_index,
        }
        try:
            answer: BacktestValidation = PipelineBacktestValidator(setup).validate(
                candidate.spec.ref, candidate.spec, trial.backtest
            )
            answer.check_subject(candidate.spec.ref)
        except Exception as exc:  # noqa: BLE001 - a validator error is a FAILED trial, recorded
            error = f"{type(exc).__name__}: {exc}"
            summary = {**base, "verdict": None, "error": error[:500]}
            if self._validates_cells(outcome):
                summary["conditional_cells"] = self._cells_block(
                    ctx, outcome, context, setup, labels, None
                )
            return ValidationOutcome(
                ctx.round_index, outcome, None, None, None, {"status": "not_run"}, summary, error
            )
        report = answer.report.model_copy(update={"created_at": ctx.as_of})
        sealed_report: ValidationReport | None = None
        sealed_status: dict[str, Any] = {"status": "not_run", "reason": "in-sample verdict"}
        if report.verdict is Verdict.PASS:
            sealed_report, sealed_status = self._sealed_oos(ctx, outcome, context, segment, setup)
        summary = {
            **base,
            "report_id": report.report_id,
            "report_hash": report.content_hash(),
            "verdict": report.verdict.value,
            "failure_reason": None
            if answer.failure_reason is None
            else answer.failure_reason.value,
            "stopped_at": next(
                (g.gate_id for g in report.gates if g.verdict is Verdict.FAIL), None
            ),
            "gates": _gates(report),
            "sealed_oos": {
                **sealed_status,
                "report_hash": None if sealed_report is None else sealed_report.content_hash(),
                "verdict": None if sealed_report is None else sealed_report.verdict.value,
                "gates": [] if sealed_report is None else _gates(sealed_report),
            },
        }
        if self._validates_cells(outcome):  # the key exists only then (records unchanged)
            summary["conditional_cells"] = self._cells_block(
                ctx, outcome, context, setup, labels, report
            )
        return ValidationOutcome(
            ctx.round_index,
            outcome,
            report,
            answer.failure_reason,
            sealed_report,
            sealed_status,
            summary,
        )

    # ------------------------------------------------------------------ per-cell validation

    def _validates_cells(self, outcome: TrialOutcome) -> bool:
        """Only with a ``validate_cells=True`` plan and a trial that registered cells."""
        return self._cells and bool(outcome.summary.get("conditional"))

    def _cells_block(
        self,
        ctx: RoundContext,
        outcome: TrialOutcome,
        context: ValidationContext,
        setup: ValidatorSetup,
        labels: Mapping[datetime, str | None],
        report: ValidationReport | None,
    ) -> dict[str, Any]:
        """The ``conditional_cells`` row of one trial (module docs, **Per-cell validation**)."""
        conditional: Mapping[str, Any] = outcome.summary["conditional"]
        attempt = conditional["attempt"]
        ledger = self._memory.ledger
        hypotheses = {str(h.ref): h for h in ledger.hypotheses}
        cells: Sequence[Mapping[str, Any]] = conditional["cells"]
        registered: list[Hypothesis] = []
        for cell in cells:  # exactly the recorded registrations, or the stage fails
            hypothesis = hypotheses.get(cell["hypothesis"])
            if (
                attempt != outcome.attempt
                or hypothesis is None
                or not ledger.is_registered(hypothesis, attempt)
                or ledger.trial_index(hypothesis, attempt) != cell["trial_index"]
            ):
                raise ValueError(
                    f"cell {cell['hypothesis']} ({attempt}) is not in the trial ledger as recorded"
                )
            registered.append(hypothesis)
        block: dict[str, Any] = {
            "scope": "in-sample G0 - G3 per supported cell; G4 / G5 not run",
            "family_trial_count": context.metadata.family_trial_count,
            "g4": CELL_G4_NOT_RUN,
            "g5": CELL_G5_NOT_RUN,
            "lifecycle": CELL_LIFECYCLE,
        }
        rows = [
            {
                "state": cell["state"],
                "hypothesis": cell["hypothesis"],
                "trial_index": cell["trial_index"],
                "count": cell["count"],
                "support": cell["support"],
            }
            for cell in cells
        ]
        supported = [i for i, cell in enumerate(cells) if cell["supported"]]
        for index, cell in enumerate(cells):
            if not cell["supported"]:
                rows[index].update(status="unsupported", reason=cell["support"], verdict=None)
        if report is None:
            for index in supported:
                rows[index].update(
                    status="not_run", reason="the trial's validation errored", verdict=None
                )
            return {**block, "rerun_result_hash": None, "cells": rows}
        adapter = tuple(g for g in report.gates if g.gate_id in _ADAPTER_GATES)
        rerun: TrialRun | None = None
        if supported:
            try:
                rerun = setup.trials.run(dict(outcome.request_params))
            except Exception as exc:  # noqa: BLE001 - recorded per cell, never a PASS
                error = f"{type(exc).__name__}: {exc}"[:500]
                for index in supported:
                    rows[index].update(status="error", error=error, verdict=None)
                return {**block, "rerun_result_hash": None, "cells": rows}
        traded = () if rerun is None else tuple(t for t in rerun.targets if _side(t.target_weight))
        block["rerun_result_hash"] = None if rerun is None else rerun.backtest.result_hash
        block["unattributed_traded"] = (
            None if rerun is None else sum(1 for t in traded if t.decision_time not in labels)
        )
        for index in supported:
            assert rerun is not None
            state = cells[index]["state"]
            mine = tuple(
                t for t in traded if t.decision_time in labels and labels[t.decision_time] == state
            )
            rows[index]["traded_decisions"] = len(mine)
            try:
                cell_report = self._cell_report(
                    ctx, outcome, context, setup, registered[index], adapter, rerun, mine
                )
            except Exception as exc:  # noqa: BLE001 - recorded per cell, never a PASS
                rows[index].update(
                    status="error", error=f"{type(exc).__name__}: {exc}"[:500], verdict=None
                )
                continue
            self._cells_validated += 1
            rows[index].update(
                status="validated",
                report_id=cell_report.report_id,
                report_hash=cell_report.content_hash(),
                verdict=cell_report.verdict.value,
                stopped_at=next(
                    (g.gate_id for g in cell_report.gates if g.verdict is Verdict.FAIL), None
                ),
                gates=_gates(cell_report),
            )
        return {**block, "cells": rows}

    def _cell_report(
        self,
        ctx: RoundContext,
        outcome: TrialOutcome,
        context: ValidationContext,
        setup: ValidatorSetup,
        cell: Hypothesis,
        adapter: tuple[GateResult, ...],
        rerun: TrialRun,
        traded: tuple[TargetPosition, ...],
    ) -> ValidationReport:
        """G0 – G3 of one supported cell under the trial's family count (module docs)."""
        trial = outcome.trial
        assert trial is not None and outcome.candidate is not None
        metadata = ExperimentMetadata.model_validate(
            {
                **context.metadata.model_dump(),
                "trial_index": self._memory.ledger.trial_index(cell, outcome.attempt),
            }
        )
        cell_context = replace(
            context,
            report_id=content_hash(
                {"run": outcome.run.run_id, "stage": "in_sample", "cell": str(cell.ref)}
            ),
            metadata=metadata,
            created_at=ctx.as_of,
        )
        if not adapter:  # the trial's report always opens with them: fail closed
            raise ValueError("the trial's report carries no adapter gate")
        if any(gate.verdict is not Verdict.PASS for gate in adapter):
            return build_report(cell_context, adapter)
        if not traded:
            gate = inconclusive_gate("G0.data_available", "non_flat_targets", 0.0)
            return build_report(cell_context, (*adapter, gate))
        bars = tuple(
            OutcomePriceBar(
                interval_start=bar.interval_start,
                interval_end=bar.interval_end,
                available_time=bar.available_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in sorted(rerun.bars, key=lambda item: item.interval_start)
            if bar.instrument == setup.instrument
        )
        table = materialize(
            self._c.outcome_provider,
            OutcomeRequest(
                label_spec=context.label_spec,
                manifest_content_hash=setup.manifest_content_hash,
                price_cutoff=max(bar.available_time for bar in bars),
                events=tuple(
                    OutcomeEvent(event_key=_event_key(t), event_time=t.decision_time)
                    for t in traded
                ),
                bars=bars,
            ),
        )
        seed = int(
            content_hash({"validation_seed": outcome.validation_seed, "cell": str(cell.ref)})[:8],
            16,
        )
        gates = run_in_sample(
            InSampleInput(
                context=cell_context,
                outcomes=table,
                study=FixedSides(
                    refs=tuple(outcome.candidate.spec.signals),
                    by_event={_event_key(t): _side(t.target_weight) for t in traded},
                ),
                seed=seed,
                reproduce=lambda: rerun.backtest.result_hash,
                recorded_result_hash=trial.backtest.result_hash,
                control_seeds=setup.control_seeds,
            )
        )
        return build_report(cell_context, (*adapter, *gates))

    def _sealed_oos(
        self,
        ctx: RoundContext,
        outcome: TrialOutcome,
        context: ValidationContext,
        segment: RoundData,
        setup: ValidatorSetup,
    ) -> tuple[ValidationReport | None, dict[str, Any]]:
        unseal, step = self._unseal, self._sealed_step
        if unseal is None or step is None:
            return None, {"status": "sealed", "reason": "no unseal budget configured"}
        # decided without reading any sealed data (a dataset source reads it only on release)
        closed = segment.sealed.evaluable(ctx.as_of)
        if closed is not None:
            return None, {"status": "sealed", "reason": closed}
        family = outcome.hypothesis.family_id
        approver = unseal.approver_of(family)
        if approver is None:
            return None, {
                "status": "sealed",
                "reason": "the family is not on the unseal budget's approved list",
            }
        # re-checked here: the memory's ledger is a mutable field (fail closed before unsealing)
        require_durable_unsealing(unseal, self._memory.oos_ledger)
        vault = SealedOosVault(
            self._c.profile, self._memory.oos_ledger, max_unsealings=unseal.max_unsealings
        )
        if vault.is_unsealed(family):
            return None, {"status": "sealed", "reason": "the family already used its unsealing"}
        try:
            unsealing = vault.unseal(family, approver, ctx.as_of)
        except OosBudgetExhausted as exc:
            return None, {"status": "sealed", "reason": str(exc)}
        # From here on the family's single evaluation is consumed, whatever happens next: the
        # claim marks it evaluated before any sealed bar leaves the vault — or, on the dataset
        # path, before any sealed manifest, bar or feature is read from storage (review fixes 2;
        # dataset G5 note).
        evaluation = vault.claim_evaluation(family)
        g5_context = replace(
            context,
            report_id=content_hash({"run": outcome.run.run_id, "stage": "sealed_oos"}),
            metadata=context.metadata.model_copy(update={"oos_unsealing": unsealing}),
        )
        status: dict[str, Any] = {"status": "unsealed", "approved_by": unsealing.approved_by}
        if unseal.ephemeral_unseal_for_tests:
            status["unseal_ledger"] = EPHEMERAL_UNSEAL_MARK
        result: tuple[GateResult, ...] | str
        try:
            result = self._sealed_gates(
                ctx, outcome, segment, vault, evaluation, g5_context, status, setup
            )
        except SealedDataRefused as exc:  # the claimed window's data did not prove (dataset)
            status["error"] = f"{type(exc).__name__}: {exc}"[:500]
            result = "sealed data refused"
        except Exception as exc:  # noqa: BLE001 - the consumed evaluation is recorded, not lost
            status["error"] = f"{type(exc).__name__}: {exc}"[:500]
            result = "sealed run errored"
        if isinstance(result, str):
            status.update(status=CONSUMED_WITHOUT_RESULT, reason=result)
            result = sealed_oos_without_result(
                g5_context, vault, evaluation, result.replace(" ", "_")
            )
        report = build_report(g5_context, result).model_copy(update={"created_at": ctx.as_of})
        return report, status

    def _sealed_gates(
        self,
        ctx: RoundContext,
        outcome: TrialOutcome,
        segment: RoundData,
        vault: SealedOosVault,
        evaluation: SealedEvaluation,
        g5_context: ValidationContext,
        status: dict[str, Any],
        setup: ValidatorSetup,
    ) -> tuple[GateResult, ...] | str:
        """G5 gates of the claimed evaluation, or why it ended without a result.

        On the dataset path the report also carries ``G0.manifest_binding`` over the data G5 ran
        on: the research bars against the round's pair and the released sealed bars and sealed
        feature requests against the sealed pair (``RoundData.sealed_binding``)."""
        step = self._sealed_step
        assert step is not None
        sealed_bars = segment.sealed.release(evaluation)
        signals_with: Callable[..., tuple[SignalObservation, ...]] = ctx.artifact(
            "state", "signals_with"
        )
        candidate, inputs = outcome.candidate, outcome.inputs
        assert candidate is not None and inputs is not None
        sealed_price_bars = segment.as_price_bars(sealed_bars)
        bars = (*segment.research_bars, *sealed_price_bars)
        decisions = decision_grid(
            sealed_bars, step=step, warmup=step, horizon=self._c.label_spec.horizon
        )
        status.update(sealed_bars=len(sealed_bars), decisions=len(decisions))
        if not decisions:
            return "no sealed decision time"
        sealed_inputs = replace(
            inputs,
            bars=bars,
            decision_times=decisions,
            knowledge_cutoff=sealed_bars[-1].interval_end,
            signals=signals_with(sealed_bars),
        )
        run = CandidateTrialRunner(candidate, sealed_inputs, self._c.backtester).run(
            dict(outcome.request_params)
        )
        traded = [t for t in run.targets if _side(t.target_weight) != 0]
        outcome_bars = tuple(
            OutcomePriceBar(
                interval_start=bar.interval_start,
                interval_end=bar.interval_end,
                available_time=bar.available_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in bars
        )
        if not traded:
            return "no non-flat target in the sealed window"
        table = materialize(
            self._c.outcome_provider,
            OutcomeRequest(
                label_spec=self._c.label_spec,
                manifest_content_hash=segment.sealed_manifest_label(),
                price_cutoff=max(bar.available_time for bar in outcome_bars),
                events=tuple(
                    OutcomeEvent(event_key=_event_key(t), event_time=t.decision_time)
                    for t in traded
                ),
                bars=outcome_bars,
            ),
        )
        study = FixedSides(
            refs=tuple(candidate.spec.signals),
            by_event={_event_key(t): _side(t.target_weight) for t in traded},
        )
        gates = run_sealed_oos(SealedOosInput(g5_context, vault, table, study, evaluation))
        binding = segment.sealed_binding()
        if not binding:  # synthetic path: nothing to bind (unchanged report)
            return gates
        mismatches = [
            *(f"research:{name}" for name in binding_mismatches(setup, segment.research_bars)),
            *(
                f"sealed:{name}"
                for name in binding_mismatches(replace(setup, **binding), sealed_price_bars)
            ),
        ]
        status["manifest_binding_mismatches"] = mismatches
        gate = flag_gate(
            "G0.manifest_binding",
            "manifest_binding_mismatch_count",
            not mismatches,
            float(len(mismatches)),
        )
        return (gate, *gates)


def failure_of(result: ValidationOutcome) -> tuple[str, ReasonCode, str] | None:
    """``(terminal state, reason, gate id)`` of a FAIL verdict, ``None`` otherwise."""
    report = result.sealed_report if _sealed_failed(result) else result.report
    if report is None or report.verdict is not Verdict.FAIL:
        return None
    gate = next(g for g in report.gates if g.verdict is Verdict.FAIL)
    state, reason = reason_for_gate(gate.gate_id)
    if report is result.report and result.failure_reason is not None:
        reason = result.failure_reason
    return state, reason, gate.gate_id


def _sealed_failed(result: ValidationOutcome) -> bool:
    return result.sealed_report is not None and result.sealed_report.verdict is Verdict.FAIL
