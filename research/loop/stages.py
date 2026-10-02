"""Concrete research stages of the continuous loop (Phase 11; ADR-0049, W2 wiring).

Each class implements ``apps.worker.loop.LoopStage`` (``name`` / ``estimate`` / ``run``). Every
number a stage uses — minutes per round, decision grid, chunk sizes, cost units, compute
declarations — is a constructor parameter; validation thresholds are read from the bound
``ValidationProfile`` only (inside ``research/validation``).

- ``IngestStage``: generates this round's new market segment ``[as_of - minutes, as_of)``
  (continuing the previously ingested price path) and splits it by the Profile's fixed calendar:
  research bars (label known before the sealed OOS boundary) join the **accumulated research
  data** in ``ResearchMemory.research_data``; sealed bars are withheld (``segment.SealedBars``)
  and never join it. Bars outside the Profile's research window and after the sealed window are
  not used (counted in the summary). Generating the sealed bars is not reading real sealed data
  (synthetic market); the later stages never receive them except through ``sealed.release``
  after a claim (``research.loop.segment``, "Synthetic sealed bars");
- ``StateStage``: Phase 1 F4 ``bar_log_return`` through ``run_feature`` over the accumulated
  research bars, then a Phase 2 ``StateProvider`` through ``infrastructure.state.run_state`` at
  every decision time and at the end of the research data; the same feature values become the
  strategy signals (``infrastructure.strategy.signals.signals_from_features``);
- ``HypothesisStage``: pre-registers knowledge hypotheses and human-reviewed LLM drafts
  (IDEA → CANDIDATE), pre-registers re-evaluations of still-open (VALIDATION / INCONCLUSIVE)
  hypotheses as new trials when the research data has grown, and asks the LLM for one new draft,
  which only goes to the review queue (a schema-invalid output, or with ``ContentVerifiedLLM`` an
  unverifiable one — ``LlmContentUnverified`` — is recorded with its ``LlmCall``'s content hash and
  refs next to the rejection reason, never registered; any other provider error fails the stage);
  an optional declared ``HypothesisBatch`` (``research.hypotheses.batch``) is pre-registered as a
  whole the first round it runs; an optional ``KnowledgeSource`` is searched once per round and its
  items join the declared knowledge, with the query hash and ``result_hash`` recorded as their
  origin;
- ``EvolutionStage`` (optional, ``research/loop/evolution.py``): offspring of the best earlier
  candidates, registered as new hypotheses and validated afresh this round;
- ``ExperimentStage`` / ``ValidationStage`` (``research/loop/trials.py``): the reproducible
  experiment records, the Phase 5 strategy → backtest run, the Phase 6 State × Strategy matrix and
  the Phase 4 / 8 pipeline (G0 – G4; G5 only with an explicit unseal budget);
- ``MemoryStage``: every outcome into memory and the lifecycle: errored trial → FAILED; FAIL →
  REJECTED (both with a FailureRecord); in-sample PASS → OOS (the furthest the loop can go; OOS
  means *under / eligible for* sealed OOS evaluation, not "passed OOS" — see ``MemoryStage``);
  a failed sealed OOS → REJECTED; INCONCLUSIVE stays in VALIDATION; a technical failure in
  VALIDATION → FAILED only for the causes ADR-0053 §2 allows (``validation_failed_refusal``).

Accumulated research window (ADR-0049 accumulated-window note, 2026-09-25): every round's
experiment and validation stages evaluate on all research-window bars ingested up to the round's
``as_of`` — not only the new segment — because the Profile's walk-forward spans the whole research
window (a segment-only evaluation leaves its windows empty: G4 INCONCLUSIVE by construction). The
price of looking at the same data again is paid in trials: each (hypothesis, round) evaluation is
a separately pre-registered ``TrialLedger`` trial, so the family trial count G3 corrects for grows
with every look; REJECTED / FAILED hypotheses are never re-evaluated and OOS ones are never re-run
in sample. The sealed OOS window never enters the research data.

Positions use only features known before the bar they trade (C-L1); the synthetic market's planted
truth is never an input (it is only counted in the audit summary).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from apps.worker.loop import RoundContext, StageResult, StageUsage
from core.contracts.feature import FeatureProvider
from core.contracts.llm import LLMProvider
from core.contracts.state import StateProvider, StateResult
from core.contracts.synthetic import (
    SyntheticBar,
    SyntheticMarketProvider,
    SyntheticMarketSpec,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref
from core.domain.research import (
    FailureRecord,
    Hypothesis,
    KnowledgeItem,
    LlmCall,
    ValidationReport,
    Verdict,
)
from core.domain.specs import FeatureSpec, StateSpec
from core.errors import LifecycleViolation, ReasonCategory, ReasonCode
from core.lifecycle.strategy import LifecycleState
from infrastructure.content import ContentResolver
from infrastructure.state import run_state, state_inputs, state_request
from research.experiments.run_inputs import state_labeller_identity
from research.hypotheses import (
    HypothesisBatch,
    HypothesisDraft,
    KnowledgeSearch,
    KnowledgeSource,
    LlmDraftRejected,
    from_knowledge,
    from_llm,
    preregister_batch,
)
from research.loop.evolution import EvolutionPlan, EvolutionStage
from research.loop.llm_content import LlmContentUnverified, verify_call_content
from research.loop.memory import ResearchMemory
from research.loop.retry_admission import retry_summary_rows
from research.loop.segment import (
    ResearchPiece,
    RoundData,
    SealedBars,
    Segment,
    decision_grid,
    trial_point,
)
from research.loop.trials import (
    ExperimentStage,
    OosUnsealBudget,
    TrialComponents,
    TrialOutcome,
    ValidationOutcome,
    ValidationStage,
    _request_point,
    failure_of,
)
from research.validation.splits import midnight_utc

if TYPE_CHECKING:
    from research.loop.p7_admission import P7RoundAdmission

__all__ = [
    "NOT_REPRODUCIBLE_GATES",
    "EvolutionPlan",
    "EvolutionStage",
    "ExperimentStage",
    "HypothesisStage",
    "IngestStage",
    "MemoryStage",
    "OosUnsealBudget",
    "StateStage",
    "TrialComponents",
    "ValidationStage",
    "reevaluation_attempt",
    "reevaluation_candidates",
    "validation_failed_evidence",
    "validation_failed_refusal",
]

_MINUTE: Final = timedelta(minutes=1)


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int")
    return value


class IngestStage:
    """New market data → the accumulated research window of the round (see module docs).

    Each round generates its new segment ``[as_of - minutes, as_of)``; the market continues the
    path of the previously ingested one (its initial price is the last ingested close), so the
    accumulated research data is one continuous price path rather than segments that each restart
    at the base price. The new segment is split by the Profile's fixed calendar: research-window
    bars whose label is known before the sealed OOS boundary are appended to
    ``ResearchMemory.research_data``; sealed-window bars are withheld (``SealedBars``) and never
    join the research data; bars before the research window, after the sealed window, or already
    covered by earlier rounds are not used (counted).
    """

    name = "ingest"

    def __init__(
        self,
        memory: ResearchMemory,
        provider: SyntheticMarketProvider,
        base: SyntheticMarketSpec,
        profile: ValidationProfile,
        *,
        minutes_per_round: int,
        compute_seconds_per_bar: Decimal,
        decision_step: timedelta,
        decision_warmup: timedelta,
        label_horizon: timedelta,
    ) -> None:
        self._memory = memory
        self._provider = provider
        self._base = base
        self._profile = profile
        self._minutes = _positive_int(minutes_per_round, "minutes_per_round")
        self._per_bar = compute_seconds_per_bar
        self._step = decision_step
        self._warmup = decision_warmup
        self._horizon = label_horizon

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._per_bar * self._minutes)

    def run(self, ctx: RoundContext) -> StageResult:
        memory = self._memory
        payload = self._base.model_dump()
        payload.update(
            seed=ctx.seed, start=ctx.as_of - self._minutes * _MINUTE, minutes=self._minutes
        )
        if memory.markets and memory.markets[-1].bars:
            payload.update(initial_price=memory.markets[-1].bars[-1].close)
        spec = SyntheticMarketSpec.model_validate(payload)
        market = self._provider.generate(spec)
        split = self._profile.data_split
        start = midnight_utc(split.research_window_start)
        boundary = midnight_utc(split.sealed_oos_boundary)
        window = (boundary, boundary + split.sealed_oos_length)
        covered = memory.research_data[-1].bars[-1].interval_end if memory.research_data else None
        research: list[SyntheticBar] = []
        sealed: list[SyntheticBar] = []
        unused = already = 0
        for bar in market.bars:
            if bar.interval_start >= start and bar.interval_end <= boundary:
                if covered is not None and bar.interval_start < covered:
                    already += 1  # an earlier round already contributed this time
                else:
                    research.append(bar)
            elif bar.interval_start >= window[0] and bar.interval_end <= window[1]:
                sealed.append(bar)
            else:
                unused += 1
        memory.add_market(spec, market)
        if research:
            memory.research_data.append(ResearchPiece(ctx.round_index, market, tuple(research)))
        pieces = tuple(memory.research_data)
        accumulated = tuple(bar for piece in pieces for bar in piece.bars)
        descriptor = self._provider.descriptor
        segment = Segment(
            market=market,
            symbol=spec.symbol,
            provider_key=f"{descriptor.name}@{descriptor.version}",
            provider_hash=descriptor.content_hash(),
            pieces=pieces,
            sealed=SealedBars(sealed, window),
            decision_times=decision_grid(
                accumulated, step=self._step, warmup=self._warmup, horizon=self._horizon
            ),
        )
        summary = {
            "spec_hash": market.spec_hash,
            "market_hash": market.market_hash,
            "provider": market.provider,
            "initial_price": str(spec.initial_price),
            "bars": len(market.bars),
            "research_bars": len(research),
            "sealed_bars_withheld": len(sealed),
            "unused_bars": unused,
            "already_covered_bars": already,
            "accumulated_research_bars": len(segment.research),
            "research_pieces": len(pieces),
            "research_data_hash": segment.data_hash,
            "decision_times": len(segment.decision_times),
            "start": spec.start.isoformat(),
            "planted_effects": len(market.truth),
        }
        # the decision grid the segment's decision times were built with: a new run records it
        # (research.experiments.run_inputs, ADR-0100 修订 2)
        grid = (self._step, self._warmup)
        return StageResult(summary, self.estimate(ctx), {"segment": segment, "decision_grid": grid})


class StateStage:
    """F4 features → P2 states of the accumulated research data (see module docs)."""

    name = "state"

    def __init__(
        self,
        memory: ResearchMemory,
        *,
        feature_provider: FeatureProvider,
        feature_spec: FeatureSpec,
        state_provider: StateProvider,
        state_spec: StateSpec,
        feature_chunk_bars: int,
        compute_seconds: Decimal,
    ) -> None:
        if feature_spec.ref not in state_spec.features:
            raise ValueError(f"{state_spec.ref} does not read {feature_spec.ref}")
        self._memory = memory
        self._feature_provider = feature_provider
        self._feature_spec = feature_spec
        self._state_provider = state_provider
        self._state_spec = state_spec
        self._chunk = _positive_int(feature_chunk_bars, "feature_chunk_bars")
        self._compute = compute_seconds
        # Pure caches (same inputs, same outputs): the round data source's feature runs (per
        # research piece on the synthetic path), and the last state result keyed by the research
        # data hash.
        self._feature_cache: dict[str, Any] = {}
        self._last_state: tuple[str, StateResult] | None = None

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._compute)

    def run(self, ctx: RoundContext) -> StageResult:
        segment: RoundData = ctx.artifact("ingest", "segment")
        plugins = {
            **segment.plugins,
            **_plugin(self._feature_provider.descriptor),
            **_plugin(self._state_provider.descriptor),
        }
        pairs, signals, signals_with = segment.feature_runs(
            self._feature_provider, self._feature_spec, chunk=self._chunk, cache=self._feature_cache
        )
        artifacts: dict[str, Any] = {
            "spec_ref": self._state_spec.ref,
            "plugins": plugins,
            "signals_with": signals_with,
            # the manifest hash of every feature request behind the signals (G0.manifest_binding
            # compares them with the round's manifest pair on the dataset path)
            "feature_manifest_hashes": tuple(request.manifest_content_hash for request, _ in pairs),
            # the identity of the labeller behind ``labels`` (the validator's ``state_of``); a new
            # run records it (research.experiments.run_inputs, ADR-0100 修订 2)
            "labeller": state_labeller_identity(
                state_spec=self._state_spec,
                state_provider=self._state_provider,
                feature_spec=self._feature_spec,
                feature_provider=self._feature_provider,
            ),
        }
        if not segment.research_bars or segment.research_end is None:
            summary: dict[str, Any] = {
                "state": str(self._state_spec.ref),
                "label": None,
                "evaluated": 0,
            }
            self._memory.states.append({"round": ctx.round_index, **summary})
            empty: Mapping[datetime, str | None] = {}
            artifacts.update(signals=(), result=None, labels=empty, label=None)
            return StageResult(summary, self.estimate(ctx), artifacts)
        times = tuple(sorted({*segment.decision_times, segment.research_end}))
        if self._last_state is not None and self._last_state[0] == segment.data_hash:
            result = self._last_state[1]  # no new research data: the same request and result
        else:
            request = state_request(self._state_spec, times, state_inputs(pairs))
            result = run_state(self._state_provider, self._state_spec, request)
            self._last_state = (segment.data_hash, result)
        labels = {value.evaluation_time: value.state for value in result.values}
        current = labels[segment.research_end]
        counts: dict[str, int] = {}
        for label in labels.values():
            key = "(not computable)" if label is None else label
            counts[key] = counts.get(key, 0) + 1
        summary = {
            "state": str(self._state_spec.ref),
            "state_spec_hash": self._state_spec.content_hash(),
            "feature": str(self._feature_spec.ref),
            "feature_result_hashes": [result_.result_hash for _, result_ in pairs],
            "state_result_hash": result.result_hash,
            "evaluated": len(result.values),
            "label_counts": dict(sorted(counts.items())),
            "label": current,
            "as_of": segment.research_end.isoformat(),
        }
        self._memory.states.append({"round": ctx.round_index, **summary})
        artifacts.update(signals=signals, result=result, labels=labels, label=current)
        return StageResult(summary, self.estimate(ctx), artifacts)


def _plugin(descriptor: Any) -> dict[str, str]:
    return {f"{descriptor.name}@{descriptor.version}": descriptor.content_hash()}


def reevaluation_attempt(ctx: RoundContext) -> str:
    """The ``TrialLedger`` attempt key of a re-evaluation in this round."""
    return f"loop_round:{ctx.loop_id}:{ctx.round_index}"


def reevaluation_candidates(
    memory: ResearchMemory,
    state_of: Callable[[Ref], LifecycleState | None],
    data_end: datetime | None,
    limit: int,
) -> tuple[Hypothesis, ...]:
    """Registered hypotheses to evaluate again on the grown research data (ledger order).

    ADR-0049 accumulated-window note: a hypothesis is re-evaluated only while it is still open —
    lifecycle ``VALIDATION`` with an ``INCONCLUSIVE`` latest in-sample report on its latest trial —
    and only when the accumulated research data now ends later than the data that latest trial
    used (identical data would only add a trial). Never re-evaluated: a hypothesis with a REJECTED
    failure record or in ``REJECTED`` / ``FAILED`` (terminal), one in ``OOS`` (not re-run in
    sample), one whose latest trial errored or whose validator errored (a FailureRecord was
    filed), and one still in ``CANDIDATE``. At most ``limit`` hypotheses, oldest registration
    first.
    """
    if limit < 1 or data_end is None:
        return ()
    latest_trial: dict[str, TrialOutcome] = {}
    for outcome in memory.trials:
        latest_trial[str(outcome.hypothesis.ref)] = outcome
    latest_validation: dict[str, ValidationOutcome] = {}
    for validated in memory.validations:
        latest_validation[str(validated.outcome.hypothesis.ref)] = validated
    rejected = {
        str(record.subject_ref)
        for record in memory.failures.records()
        if record.terminal_state == "REJECTED"
    }
    chosen: list[Hypothesis] = []
    for hypothesis in memory.ledger.hypotheses:
        key = str(hypothesis.ref)
        trial, result = latest_trial.get(key), latest_validation.get(key)
        if (
            key in rejected
            or state_of(hypothesis.ref) is not LifecycleState.VALIDATION
            or trial is None
            or trial.knowledge_cutoff is None
            or result is None
            or result.outcome is not trial
            or result.verdict is not Verdict.INCONCLUSIVE
            or trial.knowledge_cutoff >= data_end
        ):
            continue
        chosen.append(hypothesis)
        if len(chosen) == limit:
            break
    return tuple(chosen)


class HypothesisStage:
    """Pre-registers this round's trials (see module docs).

    Fresh knowledge hypotheses and human-reviewed LLM drafts are registered (IDEA → CANDIDATE).
    Up to ``max_reevaluations_per_round`` still-open hypotheses of earlier rounds
    (``reevaluation_candidates``) are pre-registered again as **new trials**
    (``TrialLedger.register_reevaluation``, attempt ``loop_round:<loop>:<round>``): each
    (hypothesis, round) evaluation counts towards the family's trial count and the round's trial
    budget, so G3's multiple-testing correction sees every look at the accumulated data.

    Retry round (ADR-0083, v6 state only): while ``memory.retry_reevaluations`` holds a human
    admitted retry, this round runs only those already registered trials (see ``_run_retry``);
    otherwise the stage behaves exactly as before.

    ``batch`` (optional; ``None`` changes nothing): a declared ``HypothesisBatch`` of this stage's
    family. Every cell must be runnable here — ``trial_point`` reads it, its strategy is in the
    loop's catalog with the grid's exact spec, and the point is a requestable one — or the stage
    refuses to be constructed (never an ERRORED trial later). The **whole** batch is
    pre-registered in the ``TrialLedger`` before any cell runs; the round's trial budget is
    charged for every pending cell. A pending cell is one without a recorded ``TrialOutcome``. If
    a stage fails after registration but before the experiment stage records outcomes, a later
    round can retry those same registered cells idempotently. Completed cells are not run again.
    The summary's ``batch`` key names the grid and the reviewed allowlist. A P7 plan candidate
    (``StrategyCandidate.plan_record``) is refused as a batch strategy: P7 plans enter only
    through ``p7`` (ADR-0103 D3).

    ``p7`` (optional; ``None`` changes nothing): ``LoopWiring.p7_plans`` composed over the durable
    state (``research.loop.p7_admission.P7RoundAdmission``). Before the round's first journal
    write it admits or rejects the source's pending plan; admitted hypotheses are this round's
    trials (origin ``p7_plan``, listed first in ``registered``) and the summary gains ``p7_plan``
    (the COMMIT), ``p7_plan_rejection`` (``{plan_hash, code, where}``) and ``rejected_plans``
    (every plan rejected so far) — ADR-0103 D2.

    ``knowledge_source`` (optional; ``None`` changes nothing): a declared ``KnowledgeProvider`` +
    ``KnowledgeQuery`` searched once per round (``KnowledgeSource.search``). Its items become
    knowledge hypotheses after the declared ``knowledge`` (same ``max_new_per_round`` cap; an item
    the declared knowledge already holds counts as declared). The summary's ``knowledge_search``
    key records the provider, the query hash, the ``KnowledgeResult.result_hash``, the items and
    the hypotheses registered from them this round; each of those hypotheses' lifecycle evidence
    carries ``knowledge_query:<hash>`` / ``knowledge_result:<hash>`` — their origin.
    """

    name = "hypothesis"

    def __init__(
        self,
        memory: ResearchMemory,
        *,
        family_id: str,
        knowledge: Sequence[KnowledgeItem],
        max_new_per_round: int,
        max_reevaluations_per_round: int,
        compute_seconds: Decimal,
        llm: LLMProvider | None = None,
        llm_prompt: str | None = None,
        llm_cost_units_per_call: Decimal = Decimal(0),
        llm_content: ContentResolver | None = None,
        batch: HypothesisBatch | None = None,
        knowledge_source: KnowledgeSource | None = None,
        p7: P7RoundAdmission | None = None,
    ) -> None:
        if (llm is None) != (llm_prompt is None):
            raise ValueError("an LLM source needs both a provider and a prompt")
        if llm_prompt is not None and (not isinstance(llm_prompt, str) or not llm_prompt):
            # refused here: an empty prompt is a configuration error, never an LLM rejection (the
            # round's LlmRequest would refuse it; the stage records only typed rejections)
            raise ValueError("the LLM prompt must be a non-empty str")
        reevaluations = max_reevaluations_per_round
        if (
            isinstance(reevaluations, bool)
            or not isinstance(reevaluations, int)
            or reevaluations < 0
        ):
            raise ValueError("max_reevaluations_per_round must be a non-negative int")
        self._memory = memory
        self._family = family_id
        self._knowledge = tuple(knowledge)
        self._max_new = _positive_int(max_new_per_round, "max_new_per_round")
        self._max_reevaluations = reevaluations
        self._compute = compute_seconds
        self._llm = llm
        self._prompt = llm_prompt
        self._llm_cost = llm_cost_units_per_call
        self._llm_content = llm_content
        if batch is not None:
            self._check_batch(batch)
        self._batch = batch
        self._source = knowledge_source
        self._searched: tuple[tuple[str, int], KnowledgeSearch] | None = None
        self._p7 = p7

    def _check_batch(self, batch: HypothesisBatch) -> None:
        """Every cell runs here as declared, or the stage is refused (see class docs)."""
        if batch.grid.family_id != self._family:
            raise ValueError(
                f"the batch's family {batch.grid.family_id!r} is not this stage's {self._family!r}"
            )
        for spec in batch.grid.strategies:
            candidate = self._memory.strategies.get(str(spec.ref))
            if candidate is None or candidate.spec.content_hash() != spec.content_hash():
                raise ValueError(f"the batch strategy {spec.ref} is not in the loop's catalog")
            if candidate.plan_record is not None:  # ADR-0103 D3: P7 plans only through p7
                raise ValueError(f"the batch strategy {spec.ref} is a P7 plan candidate")
        for hypothesis in batch.hypotheses:
            point = trial_point(hypothesis)
            candidate = self._memory.strategies.get(f"strategy:{point.strategy}")
            if candidate is None:
                raise ValueError(f"{hypothesis.ref}: no strategy {point.strategy} in the catalog")
            _request_point(candidate, point.overrides)

    def _search(self, ctx: RoundContext) -> KnowledgeSearch | None:
        """This round's knowledge search (asked once per round: the plan and the run agree)."""
        if self._source is None:
            return None
        key = (ctx.loop_id, ctx.round_index)
        if self._searched is None or self._searched[0] != key:
            self._searched = (key, self._source.search(self._family))
        return self._searched[1]

    def _batch_pending(self) -> tuple[Hypothesis, ...]:
        if self._batch is None:
            return ()
        completed = {str(outcome.hypothesis.ref) for outcome in self._memory.trials}
        return tuple(h for h in self._batch.hypotheses if str(h.ref) not in completed)

    def _plan(
        self, ctx: RoundContext
    ) -> tuple[
        tuple[Hypothesis, ...],
        tuple[HypothesisDraft, ...],
        tuple[Hypothesis, ...],
        tuple[Hypothesis, ...],
    ]:
        known = {(h.name, h.version) for h in self._memory.ledger.hypotheses}
        declared = from_knowledge(self._knowledge, self._family)
        search = self._search(ctx)
        if search is not None:  # searched items after the declared ones, never twice
            listed = {(h.name, h.version) for h in declared}
            declared += tuple(h for h in search.hypotheses if (h.name, h.version) not in listed)
        fresh = tuple(h for h in declared if (h.name, h.version) not in known)[: self._max_new]
        segment: RoundData = ctx.artifact("ingest", "segment")
        again = reevaluation_candidates(
            self._memory, ctx.state_of, segment.research_end, self._max_reevaluations
        )
        return fresh, self._memory.reviews.reviewed_untaken(), again, self._batch_pending()

    def estimate(self, ctx: RoundContext) -> StageUsage:
        retry = self._retry_reevaluations()
        if retry:
            return self._retry_usage(retry)
        return self._usage(*self._plan(ctx), p7=0 if self._p7 is None else self._p7.trials())

    def run(self, ctx: RoundContext) -> StageResult:
        retry = self._retry_reevaluations()
        if retry:
            return self._run_retry(retry)
        fresh, drafts, again, batch = self._plan(ctx)
        # ADR-0103 D3: the P7 admission runs before any other journal write of the round
        p7 = None if self._p7 is None else self._p7.run(ctx.round_index)
        p7_registered = () if p7 is None else p7.hypotheses
        batch_summary: dict[str, Any] = {}
        batched = batch
        pre_registered: tuple[Hypothesis, ...] = ()
        if self._batch is not None:  # the whole batch, before anything else of the round
            pre_registered = preregister_batch(self._batch, self._memory.ledger)
            if any(not self._memory.ledger.is_registered(hypothesis) for hypothesis in batched):
                raise ValueError("a pending batch cell was not pre-registered")
            payload = self._batch.payload()
            batch_summary = {
                "batch": {
                    "grid": self._batch.grid.name,
                    "grid_hash": payload["grid"],
                    "allowlist": payload["allowlist"],
                    "allowlist_hash": payload["allowlist_hash"],
                    "reviewer": self._batch.allowlist.reviewer,
                    "declared_trials": len(self._batch.hypotheses),
                    "pre_registered": [str(h.ref) for h in pre_registered],
                }
            }
        llm_summary: dict[str, Any] | None = None
        if self._llm is not None and self._prompt is not None:
            context = {
                "round": ctx.round_index,
                "state": ctx.artifact("state", "label"),
                "failures_so_far": len(self._memory.failures.records()),
            }
            try:
                draft = from_llm(self._llm, self._prompt, context, self._family)
            # schema-invalid output, or (ContentVerifiedLLM) content that is not retrievable or not
            # what was exchanged: recorded with its call, never registered. Any other error of the
            # provider (a ValueError included) fails the stage, like a RuntimeError always did.
            except (LlmDraftRejected, LlmContentUnverified) as exc:
                llm_summary = {
                    "rejected": exc.reason[:500],
                    "call_hash": exc.call.content_hash(),
                    "call": exc.call.model_dump(mode="json"),
                }
            else:
                llm_summary = {
                    "draft": str(draft.hypothesis.ref),
                    "draft_hash": draft.hypothesis.content_hash(),
                    "call_hash": draft.call.content_hash(),
                    "enqueued_for_review": self._memory.reviews.enqueue(draft),
                }
        registered: list[Hypothesis] = []
        llm_calls: dict[str, LlmCall] = {}
        if p7 is not None and p7.admission is not None:
            for hypothesis in p7_registered:
                self._admit(
                    ctx,
                    hypothesis,
                    (
                        f"hypothesis:{hypothesis.ref}#{hypothesis.content_hash()}",
                        f"p7_plan:{p7.plan_hash}",
                        f"plan_admission:{p7.admission.prepare.transaction_id}",
                    ),
                )
        search = self._search(ctx)
        searched: set[tuple[str, str]] = set()
        if search is not None:
            listed = {(item.name, item.version) for item in self._knowledge}
            searched = {
                (h.name, h.version)
                for h, item in zip(search.hypotheses, search.items, strict=True)
                if (item.name, item.version) not in listed
            }
        if self._batch is not None and batched:
            evidence = (
                f"batch:{self._batch.grid.name}#{self._batch.grid.content_hash()}",
                f"reviewed_operators:{self._batch.allowlist.key}"
                f"#{self._batch.allowlist.content_hash()}",
            )
            for hypothesis in batched:
                registered.append(hypothesis)
                self._admit(
                    ctx,
                    hypothesis,
                    (f"hypothesis:{hypothesis.ref}#{hypothesis.content_hash()}", *evidence),
                )
        from_search: list[str] = []
        for hypothesis in fresh:
            self._memory.ledger.register(hypothesis)
            registered.append(hypothesis)
            origin: tuple[str, ...] = ()
            if search is not None and (hypothesis.name, hypothesis.version) in searched:
                origin = search.evidence()
                from_search.append(str(hypothesis.ref))
            self._admit(
                ctx,
                hypothesis,
                (f"hypothesis:{hypothesis.ref}#{hypothesis.content_hash()}", *origin),
            )
        for reviewed in drafts:
            if self._llm_content is not None:  # the reviewed content must still be the content
                verify_call_content(reviewed.call, self._llm_content)
            self._memory.ledger.register_draft(reviewed)
            self._memory.reviews.mark_taken(reviewed)
            registered.append(reviewed.hypothesis)
            llm_calls[str(reviewed.hypothesis.ref)] = reviewed.call
            self._admit(
                ctx,
                reviewed.hypothesis,
                (
                    f"hypothesis:{reviewed.hypothesis.ref}#{reviewed.hypothesis.content_hash()}",
                    f"llm_call:{reviewed.call.content_hash()}",
                    f"human_review:{self._memory.reviews.reviewer_of(reviewed)}",
                ),
            )
        attempt = reevaluation_attempt(ctx)
        for hypothesis in again:  # pre-registered as new trials before they run
            if not self._memory.ledger.register_reevaluation(hypothesis, attempt):
                raise ValueError(f"{hypothesis.ref} was already re-evaluated as {attempt}")
        this_round = (*p7_registered, *registered)
        summary = {
            "registered": [str(h.ref) for h in this_round],
            "hypothesis_hashes": [h.content_hash() for h in this_round],
            "reevaluations": [str(h.ref) for h in again],
            "reevaluation_attempt": attempt if again else None,
            "family_trials": self._memory.ledger.trials(self._family),
            "llm": llm_summary,
            "pending_reviews": list(self._memory.reviews.pending),
            **batch_summary,
        }
        if search is not None:
            summary["knowledge_search"] = {**search.summary(), "registered": from_search}
        if self._p7 is not None:  # the keys exist only with P7 plans (records unchanged)
            rejection = None if p7 is None else p7.rejection_summary()
            summary["p7_plan"] = None if p7 is None else p7.admitted_summary()
            summary["p7_plan_rejection"] = rejection
            summary["rejected_plans"] = [
                *self._p7.rejected(),
                *([] if rejection is None else [rejection["plan_hash"]]),
            ]
        return StageResult(
            summary,
            self._usage(fresh, drafts, again, batch, p7=len(p7_registered)),
            {
                "p7_registered": p7_registered,
                "registered": tuple(registered),
                "reevaluations": tuple((h, attempt) for h in again),
                "llm_calls": llm_calls,
            },
        )

    def _retry_reevaluations(self) -> tuple[tuple[Hypothesis, str], ...]:
        """ADR-0083: the admitted retry's trials waiting for this round (empty but in v6 state).

        Each was pre-registered in the TrialLedger by the durable retry admission; a missing
        registration fails the stage (it is never registered here)."""
        pending = tuple(self._memory.retry_reevaluations)
        for hypothesis, attempt in pending:
            if not self._memory.ledger.is_registered(hypothesis, attempt):
                raise ValueError(
                    f"retry trial {hypothesis.ref} ({attempt}) has no durable TrialLedger "
                    "registration"
                )
        return pending

    def _retry_usage(self, retry: tuple[tuple[Hypothesis, str], ...]) -> StageUsage:
        return StageUsage(trials=len(retry), compute_seconds=self._compute)

    def _run_retry(self, retry: tuple[tuple[Hypothesis, str], ...]) -> StageResult:
        """A retry round (ADR-0083) runs exactly the admitted trials: nothing new is registered,
        re-evaluated or drafted, so the round declares precisely what the admission's budget check
        (G2) accepted. The summary's ``retry_reevaluations`` rows mark the retry as consumed."""
        summary: dict[str, Any] = {
            "registered": [],
            "hypothesis_hashes": [],
            "reevaluations": [],
            "reevaluation_attempt": None,
            "retry_reevaluations": retry_summary_rows(retry),
            "family_trials": self._memory.ledger.trials(self._family),
            "llm": None,
            "pending_reviews": list(self._memory.reviews.pending),
        }
        self._memory.retry_reevaluations.clear()
        return StageResult(
            summary,
            self._retry_usage(retry),
            {
                "registered": (),
                "reevaluations": (),
                "retry_reevaluations": retry,
                "llm_calls": {},
            },
        )

    def _usage(
        self,
        fresh: tuple[Hypothesis, ...],
        drafts: tuple[HypothesisDraft, ...],
        again: tuple[Hypothesis, ...],
        batch: tuple[Hypothesis, ...],
        *,
        p7: int = 0,
    ) -> StageUsage:
        calls = 0 if self._llm is None else 1
        return StageUsage(
            trials=len(fresh) + len(drafts) + len(again) + len(batch) + p7,
            llm_cost_units=self._llm_cost * calls,
            compute_seconds=self._compute,
        )

    @staticmethod
    def _admit(ctx: RoundContext, hypothesis: Hypothesis, evidence: tuple[str, ...]) -> None:
        ctx.open_subject(hypothesis.ref)
        ctx.advance(
            hypothesis.ref,
            LifecycleState.CANDIDATE,
            reason="pre-registered by the research loop",
            evidence=evidence,
        )


#: A technical failure (``FAILED`` terminal state) in OOS has no lifecycle edge (``OOS → FAILED``
#: does not exist; ADR-0053 alternative C, not decided): the record is filed, the lifecycle stays
#: put. ``VALIDATION → FAILED`` exists since ADR-0053, for the causes ``validation_failed_refusal``
#: allows.
_NO_FAILED_EDGE: Final = frozenset({LifecycleState.OOS})

#: ADR-0053 §2: the G0 gates whose FAIL is a validation-time ``NOT_REPRODUCIBLE`` failure.
NOT_REPRODUCIBLE_GATES: Final = frozenset({"G0.reproducibility", "G0.signal_determinism"})


def validation_failed_refusal(
    record: FailureRecord,
    *,
    report: ValidationReport | None = None,
    subject_fault: bool = False,
) -> str | None:
    """Why ``record`` may **not** move its subject ``VALIDATION → FAILED``; ``None`` if it may.

    ADR-0053 §2 (exhaustive): a ``FAILED`` record of the ``REPRODUCIBILITY`` class and either

    - ``NOT_REPRODUCIBLE`` at a FAIL ``G0.reproducibility`` / ``G0.signal_determinism`` gate of
      ``report`` (the validation report that failed it), or
    - ``RUN_ERRORED`` of a run that errored in the subject's own code (``subject_fault``; module
      docs of ``research.loop.trials``), with no report.

    Everything else is refused: a statistical / robustness / sealed OOS FAIL (→ REJECTED), a
    technical failure of another class (e.g. ``CONTRACT_VIOLATION``), a ``RUN_ERRORED`` found by a
    validation gate (``G0.run_state``) and every infrastructure error — among them the
    validator's own error (no report) and a run error outside the subject's code.
    """
    if record.terminal_state != "FAILED":
        return f"a {record.terminal_state} record is not a technical failure"
    code = record.reason_code
    if code.category is not ReasonCategory.REPRODUCIBILITY:
        return f"{code.value} is not a reproducibility failure (C-P3)"
    if code is ReasonCode.NOT_REPRODUCIBLE:
        if report is None:
            return "NOT_REPRODUCIBLE needs the validation report that failed a G0 gate"
        failed = {g.gate_id for g in report.gates if g.verdict is Verdict.FAIL}
        if record.gate_id not in NOT_REPRODUCIBLE_GATES or record.gate_id not in failed:
            return f"gate {record.gate_id} is not a failed reproducibility gate of the report"
        return None
    if report is not None:
        return "RUN_ERRORED is a run error of the subject, not a validation gate"
    if not subject_fault:
        return "the run error is not attributable to the subject (infrastructure)"
    return None


def validation_failed_evidence(
    record: FailureRecord,
    round_ref: str,
    *,
    report: ValidationReport | None = None,
    run_id: str | None = None,
    subject_fault: bool = False,
) -> tuple[str, ...]:
    """The ADR-0053 §3 evidence of a ``VALIDATION → FAILED`` move.

    ``validation_report:<id>`` (``NOT_REPRODUCIBLE``) or ``run:<run_id>`` (``RUN_ERRORED``), the
    FailureRecord's ``failure_record:<content hash>`` and the round's ``loop_round:<loop>:<index>``.
    A cause ``validation_failed_refusal`` refuses raises ``LifecycleViolation``.
    """
    refusal = validation_failed_refusal(record, report=report, subject_fault=subject_fault)
    if refusal is not None:
        raise LifecycleViolation(f"VALIDATION → FAILED refused (ADR-0053 §2): {refusal}")
    if not round_ref.startswith("loop_round:"):
        raise LifecycleViolation("VALIDATION → FAILED needs the round reference loop_round:<...>")
    if report is not None:
        cited = f"validation_report:{report.report_id}"
    elif run_id:
        cited = f"run:{run_id}"
    else:
        raise LifecycleViolation("a RUN_ERRORED VALIDATION → FAILED needs the errored run")
    return (cited, f"failure_record:{record.content_hash()}", round_ref)


class MemoryStage:
    """Files every outcome and makes the loop's lifecycle moves.

    What ``OOS`` means (ADR-0049 review fixes 2): the lifecycle state ``OOS`` is
    "正在经过封存样本外检验" — a subject that is *undergoing / eligible for* the sealed OOS
    evaluation — not a subject that passed it (ADR-0006 §1 state table, 07-validation.md §3;
    the edge ``VALIDATION → OOS`` is labelled "in-sample gates passed" and the next edge
    ``OOS → PAPER`` "sealed OOS passed"). The stage therefore moves an in-sample G0 – G4 ``PASS``
    to ``OOS`` whether or not G5 ran, with the **in-sample** report as the transition's evidence.
    G5 decides only what happens inside OOS:

    - G5 not run (no budget / family not approved / no sealed data) or ``INCONCLUSIVE`` (including
      ``consumed_without_result``): the subject stays in ``OOS``;
    - G5 ``FAIL``: ``OOS → REJECTED`` with a FailureRecord citing the sealed report;
    - G5 ``PASS``: still ``OOS``. ``OOS → PAPER`` needs a human approval (ADR-0006 §3) and the
      ``LifecycleGuard`` refuses it, so nothing beyond ``OOS`` ever happens in the loop.

    A re-evaluation (ADR-0049 accumulated-window note) is settled the same way from ``VALIDATION``:
    PASS → OOS, FAIL → REJECTED, INCONCLUSIVE stays.

    Technical failures in ``VALIDATION`` (ADR-0053): the FailureRecord is always filed; the subject
    moves ``VALIDATION → FAILED`` (evidence: ``validation_failed_evidence``) only for a cause
    ``validation_failed_refusal`` allows — a failed ``G0.reproducibility`` /
    ``G0.signal_determinism`` gate, or an errored re-evaluation whose error is the subject's own.
    An infrastructure error (the validator's own error, a run error outside the subject's code)
    and a technical failure in ``OOS`` leave the lifecycle unchanged
    (``technical_failures_lifecycle_unchanged``).

    Outcome used as input (ADR-0086 decision 3): the C-L2 guard raises ``OutcomeUsedAsInput``
    before any gate runs, so the trial's ``ValidationOutcome`` carries no report — but unlike a
    validator's own technical error, ``ValidationStage._validate`` tags it with
    ``failure_reason=ReasonCode.OUTCOME_USED_AS_INPUT`` (research.loop.trials), and this stage
    reads that tag to move the subject ``VALIDATION → REJECTED`` with a FailureRecord citing the
    same reason — leakage, never a technical failure, and never retried as one.
    """

    name = "memory"

    def __init__(self, memory: ResearchMemory) -> None:
        self._memory = memory

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage()

    def _record(self, record: FailureRecord) -> str:
        self._memory.failures.append(record)
        return record.content_hash()

    def run(self, ctx: RoundContext) -> StageResult:
        outcomes: tuple[TrialOutcome, ...] = ctx.artifact("experiment", "outcomes")
        results: tuple[ValidationOutcome, ...] = ctx.artifact("validation", "validations")
        round_ref = f"loop_round:{ctx.loop_id}:{ctx.round_index}"
        failures: list[str] = []
        oos: list[str] = []
        inconclusive: list[str] = []
        lifecycle_unchanged: list[str] = []
        for outcome in outcomes:
            if outcome.completed:
                continue
            hypothesis = outcome.hypothesis
            evidence = (f"run:{outcome.run.run_id}", round_ref)
            record = FailureRecord(
                subject_ref=hypothesis.ref,
                terminal_state="FAILED",
                reason_code=outcome.reason or ReasonCode.RUN_ERRORED,
                evidence=evidence,
                hypothesis_family_id=hypothesis.family_id,
                lessons=None if outcome.error is None else outcome.error[:2000],
                recorded_at=ctx.as_of,
            )
            failures.append(self._record(record))
            current = ctx.state_of(hypothesis.ref)
            if current in _NO_FAILED_EDGE:
                lifecycle_unchanged.append(str(hypothesis.ref))
                continue
            if current is LifecycleState.VALIDATION:  # an errored re-evaluation (ADR-0053)
                self._fail_validation(
                    ctx,
                    record,
                    round_ref,
                    lifecycle_unchanged,
                    run_id=outcome.run.run_id,
                    subject_fault=outcome.subject_fault,
                )
                continue
            ctx.advance(
                hypothesis.ref, LifecycleState.FAILED, reason="trial errored", evidence=evidence
            )
        for result in results:
            self._settle(ctx, result, round_ref, failures, oos, inconclusive, lifecycle_unchanged)
        summary = {
            "failure_records": failures,
            "moved_to_oos": oos,
            "inconclusive_in_validation": inconclusive,
            "technical_failures_lifecycle_unchanged": lifecycle_unchanged,
            "ledger_size": len(self._memory.ledger.hypotheses),
            "pending_reviews": list(self._memory.reviews.pending),
        }
        return StageResult(summary)

    def _settle(
        self,
        ctx: RoundContext,
        result: ValidationOutcome,
        round_ref: str,
        failures: list[str],
        oos: list[str],
        inconclusive: list[str],
        unchanged: list[str],
    ) -> None:
        hypothesis = result.outcome.hypothesis
        subject = str(hypothesis.ref)
        if result.report is None:
            evidence = (f"run:{result.outcome.run.run_id}", round_ref)
            if result.failure_reason is ReasonCode.OUTCOME_USED_AS_INPUT:
                # ADR-0086 decision 3: C-L2 leakage, not a technical validator failure — REJECTED,
                # never retried as though the validator itself had errored (class docs).
                failures.append(
                    self._record(
                        FailureRecord(
                            subject_ref=hypothesis.ref,
                            terminal_state="REJECTED",
                            reason_code=ReasonCode.OUTCOME_USED_AS_INPUT,
                            evidence=evidence,
                            hypothesis_family_id=hypothesis.family_id,
                            lessons=None if result.error is None else result.error[:2000],
                            recorded_at=ctx.as_of,
                        )
                    )
                )
                ctx.advance(
                    hypothesis.ref,
                    LifecycleState.REJECTED,
                    reason="Outcome used as input (C-L2): rejected as leakage, not retried",
                    evidence=evidence,
                )
                return
            # the validator itself errored: a technical failure
            failures.append(
                self._record(
                    FailureRecord(
                        subject_ref=hypothesis.ref,
                        terminal_state="FAILED",
                        reason_code=ReasonCode.RUN_ERRORED,
                        evidence=evidence,
                        hypothesis_family_id=hypothesis.family_id,
                        lessons=None if result.error is None else result.error[:2000],
                        recorded_at=ctx.as_of,
                    )
                )
            )
            unchanged.append(subject)
            return
        if result.report.verdict is Verdict.INCONCLUSIVE:
            inconclusive.append(subject)
            return
        if result.report.verdict is Verdict.PASS:
            ctx.advance(
                hypothesis.ref,
                LifecycleState.OOS,
                reason=(
                    "in-sample G0-G4 passed: eligible for the sealed OOS evaluation (G5); "
                    "OOS is the last state the loop may reach"
                ),
                evidence=(f"validation_report:{result.report.report_id}", round_ref),
            )
            oos.append(subject)
        failure = failure_of(result)
        if failure is None:
            return
        state, reason, gate_id = failure
        report = result.sealed_report if result.sealed_report is not None else result.report
        evidence = (f"validation_report:{report.report_id}", round_ref)
        record = FailureRecord(
            subject_ref=hypothesis.ref,
            terminal_state=state,
            reason_code=reason,
            gate_id=gate_id,
            evidence=evidence,
            hypothesis_family_id=hypothesis.family_id,
            recorded_at=ctx.as_of,
        )
        failures.append(self._record(record))
        if state == "FAILED":  # technical: never REJECTED (ADR-0053 alternative D)
            if ctx.state_of(hypothesis.ref) in _NO_FAILED_EDGE:
                unchanged.append(subject)
            else:
                self._fail_validation(ctx, record, round_ref, unchanged, report=report)
            return
        ctx.advance(
            hypothesis.ref,
            LifecycleState.REJECTED,
            reason=f"validation gate {gate_id} failed",
            evidence=evidence,
        )

    @staticmethod
    def _fail_validation(
        ctx: RoundContext,
        record: FailureRecord,
        round_ref: str,
        unchanged: list[str],
        *,
        report: ValidationReport | None = None,
        run_id: str | None = None,
        subject_fault: bool = False,
    ) -> None:
        """``VALIDATION → FAILED`` for a cause ADR-0053 §2 allows; otherwise the lifecycle stays."""
        if validation_failed_refusal(record, report=report, subject_fault=subject_fault):
            unchanged.append(str(record.subject_ref))
            return
        ctx.advance(
            record.subject_ref,
            LifecycleState.FAILED,
            reason=f"technical failure during validation: {record.reason_code.value} (C-P3)",
            evidence=validation_failed_evidence(
                record, round_ref, report=report, run_id=run_id, subject_fault=subject_fault
            ),
        )
