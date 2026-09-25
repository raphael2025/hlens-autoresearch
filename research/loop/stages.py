"""Concrete research stages of the continuous loop (Phase 11; ADR-0049, W2 wiring).

Each class implements ``apps.worker.loop.LoopStage`` (``name`` / ``estimate`` / ``run``). Every
number a stage uses — minutes per round, decision grid, chunk sizes, cost units, compute
declarations — is a constructor parameter; validation thresholds are read from the bound
``ValidationProfile`` only (inside ``research/validation``).

- ``IngestStage``: generates this round's new market segment ``[as_of - minutes, as_of)`` and
  splits it by the Profile's fixed calendar: research bars (label known before the sealed OOS
  boundary) and sealed bars (withheld; ``segment.SealedBars``). Bars outside the Profile's research
  window and after the sealed window are not used (counted in the summary);
- ``StateStage``: Phase 1 F4 ``bar_log_return`` through ``run_feature`` over the research bars,
  then a Phase 2 ``StateProvider`` through ``infrastructure.state.run_state`` at every decision
  time and at the end of the research data; the same feature values become the strategy signals
  (``infrastructure.strategy.signals.signals_from_features``);
- ``HypothesisStage``: pre-registers knowledge hypotheses and human-reviewed LLM drafts
  (IDEA → CANDIDATE) and asks the LLM for one new draft, which only goes to the review queue;
- ``EvolutionStage`` (optional, ``research/loop/evolution.py``): offspring of the best earlier
  candidates, registered as new hypotheses and validated afresh this round;
- ``ExperimentStage`` / ``ValidationStage`` (``research/loop/trials.py``): the reproducible
  experiment records, the Phase 5 strategy → backtest run, the Phase 6 State × Strategy matrix and
  the Phase 4 / 8 pipeline (G0 – G4; G5 only with an explicit unseal budget);
- ``MemoryStage``: every outcome into memory and the lifecycle: errored trial → FAILED; FAIL →
  REJECTED (both with a FailureRecord); in-sample PASS → OOS (the furthest the loop can go; OOS
  means *under / eligible for* sealed OOS evaluation, not "passed OOS" — see ``MemoryStage``);
  a failed sealed OOS → REJECTED; INCONCLUSIVE stays in VALIDATION.

Positions use only features known before the bar they trade (C-L1); the synthetic market's planted
truth is never an input (it is only counted in the audit summary).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from apps.worker.loop import RoundContext, StageResult, StageUsage
from core.contracts.feature import FeatureProvider
from core.contracts.llm import LLMProvider
from core.contracts.state import StateProvider, StateResult
from core.contracts.strategy import SignalObservation
from core.contracts.synthetic import (
    SyntheticBar,
    SyntheticMarketProvider,
    SyntheticMarketSpec,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import FailureRecord, Hypothesis, KnowledgeItem, LlmCall, Verdict
from core.domain.specs import FeatureSpec, StateSpec
from core.errors import ReasonCode
from core.lifecycle.strategy import LifecycleState
from infrastructure.state import run_state, state_inputs, state_request
from infrastructure.strategy.signals import signals_from_features
from research.hypotheses import HypothesisDraft, from_knowledge, from_llm
from research.loop.evolution import EvolutionPlan, EvolutionStage
from research.loop.memory import ResearchMemory
from research.loop.segment import (
    SealedBars,
    Segment,
    decision_grid,
    feature_pairs,
    observations,
)
from research.loop.trials import (
    ExperimentStage,
    OosUnsealBudget,
    TrialComponents,
    TrialOutcome,
    ValidationOutcome,
    ValidationStage,
    failure_of,
)
from research.validation.splits import midnight_utc

__all__ = [
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
]

_MINUTE: Final = timedelta(minutes=1)


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int")
    return value


class IngestStage:
    name = "ingest"

    def __init__(
        self,
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
        payload = self._base.model_dump()
        payload.update(
            seed=ctx.seed, start=ctx.as_of - self._minutes * _MINUTE, minutes=self._minutes
        )
        spec = SyntheticMarketSpec.model_validate(payload)
        market = self._provider.generate(spec)
        split = self._profile.data_split
        start = midnight_utc(split.research_window_start)
        boundary = midnight_utc(split.sealed_oos_boundary)
        window = (boundary, boundary + split.sealed_oos_length)
        research: list[SyntheticBar] = []
        sealed: list[SyntheticBar] = []
        unused = 0
        for bar in market.bars:
            if bar.interval_start >= start and bar.interval_end <= boundary:
                research.append(bar)
            elif bar.interval_start >= window[0] and bar.interval_end <= window[1]:
                sealed.append(bar)
            else:
                unused += 1
        descriptor = self._provider.descriptor
        segment = Segment(
            market=market,
            symbol=spec.symbol,
            provider_key=f"{descriptor.name}@{descriptor.version}",
            provider_hash=descriptor.content_hash(),
            research=tuple(research),
            sealed=SealedBars(sealed, window),
            decision_times=decision_grid(
                research, step=self._step, warmup=self._warmup, horizon=self._horizon
            ),
        )
        summary = {
            "spec_hash": market.spec_hash,
            "market_hash": market.market_hash,
            "provider": market.provider,
            "bars": len(market.bars),
            "research_bars": len(research),
            "sealed_bars_withheld": len(sealed),
            "unused_bars": unused,
            "decision_times": len(segment.decision_times),
            "start": spec.start.isoformat(),
            "planted_effects": len(market.truth),
        }
        return StageResult(summary, self.estimate(ctx), {"segment": segment})


class StateStage:
    """F4 features → P2 states of the new segment (see module docs)."""

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

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._compute)

    def _signals(
        self, segment: Segment, bars: Sequence[SyntheticBar]
    ) -> tuple[tuple[Any, ...], tuple[SignalObservation, ...]]:
        pairs = feature_pairs(
            self._feature_provider,
            self._feature_spec,
            observations(segment.market, bars, segment.symbol),
            chunk=self._chunk,
            manifest=segment.market.market_hash,
        )
        signals = tuple(
            signal
            for _, result in pairs
            for signal in signals_from_features(
                result,
                feature=self._feature_spec.ref,
                instrument=segment.symbol,
                knowledge_time=result.values[-1].evaluation_time,
            )
        )
        return pairs, signals

    def run(self, ctx: RoundContext) -> StageResult:
        segment: Segment = ctx.artifact("ingest", "segment")
        plugins = {
            segment.provider_key: segment.provider_hash,
            **_plugin(self._feature_provider.descriptor),
            **_plugin(self._state_provider.descriptor),
        }
        artifacts: dict[str, Any] = {
            "spec_ref": self._state_spec.ref,
            "plugins": plugins,
            "signals_for": lambda bars: self._signals(segment, bars)[1],
        }
        if not segment.research or segment.research_end is None:
            summary: dict[str, Any] = {
                "state": str(self._state_spec.ref),
                "label": None,
                "evaluated": 0,
            }
            self._memory.states.append({"round": ctx.round_index, **summary})
            empty: Mapping[datetime, str | None] = {}
            artifacts.update(signals=(), result=None, labels=empty, label=None)
            return StageResult(summary, self.estimate(ctx), artifacts)
        pairs, signals = self._signals(segment, segment.research)
        times = tuple(sorted({*segment.decision_times, segment.research_end}))
        request = state_request(self._state_spec, times, state_inputs(pairs))
        result: StateResult = run_state(self._state_provider, self._state_spec, request)
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


class HypothesisStage:
    name = "hypothesis"

    def __init__(
        self,
        memory: ResearchMemory,
        *,
        family_id: str,
        knowledge: Sequence[KnowledgeItem],
        max_new_per_round: int,
        compute_seconds: Decimal,
        llm: LLMProvider | None = None,
        llm_prompt: str | None = None,
        llm_cost_units_per_call: Decimal = Decimal(0),
    ) -> None:
        if (llm is None) != (llm_prompt is None):
            raise ValueError("an LLM source needs both a provider and a prompt")
        self._memory = memory
        self._family = family_id
        self._knowledge = tuple(knowledge)
        self._max_new = _positive_int(max_new_per_round, "max_new_per_round")
        self._compute = compute_seconds
        self._llm = llm
        self._prompt = llm_prompt
        self._llm_cost = llm_cost_units_per_call

    def _plan(self) -> tuple[tuple[Hypothesis, ...], tuple[HypothesisDraft, ...]]:
        known = {(h.name, h.version) for h in self._memory.ledger.hypotheses}
        fresh = tuple(
            h
            for h in from_knowledge(self._knowledge, self._family)
            if (h.name, h.version) not in known
        )[: self._max_new]
        return fresh, self._memory.reviews.reviewed_untaken()

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return self._usage(*self._plan())

    def run(self, ctx: RoundContext) -> StageResult:
        fresh, drafts = self._plan()
        llm_summary: dict[str, Any] | None = None
        if self._llm is not None and self._prompt is not None:
            context = {
                "round": ctx.round_index,
                "state": ctx.artifact("state", "label"),
                "failures_so_far": len(self._memory.failures.records()),
            }
            try:
                draft = from_llm(self._llm, self._prompt, context, self._family)
            except ValueError as exc:  # schema-invalid output: recorded, never registered
                llm_summary = {"rejected": str(exc)[:500]}
            else:
                llm_summary = {
                    "draft": str(draft.hypothesis.ref),
                    "draft_hash": draft.hypothesis.content_hash(),
                    "call_hash": draft.call.content_hash(),
                    "enqueued_for_review": self._memory.reviews.enqueue(draft),
                }
        registered: list[Hypothesis] = []
        llm_calls: dict[str, LlmCall] = {}
        for hypothesis in fresh:
            self._memory.ledger.register(hypothesis)
            registered.append(hypothesis)
            self._admit(
                ctx, hypothesis, (f"hypothesis:{hypothesis.ref}#{hypothesis.content_hash()}",)
            )
        for reviewed in drafts:
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
        summary = {
            "registered": [str(h.ref) for h in registered],
            "hypothesis_hashes": [h.content_hash() for h in registered],
            "family_trials": self._memory.ledger.trials(self._family),
            "llm": llm_summary,
            "pending_reviews": list(self._memory.reviews.pending),
        }
        return StageResult(
            summary,
            self._usage(fresh, drafts),
            {"registered": tuple(registered), "llm_calls": llm_calls},
        )

    def _usage(
        self, fresh: tuple[Hypothesis, ...], drafts: tuple[HypothesisDraft, ...]
    ) -> StageUsage:
        calls = 0 if self._llm is None else 1
        return StageUsage(
            trials=len(fresh) + len(drafts),
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


#: A technical failure (``FAILED`` terminal state) after VALIDATION has no ADR-0006 edge
#: (``VALIDATION → FAILED`` does not exist): the record is filed, the lifecycle stays put.
_NO_FAILED_EDGE: Final = frozenset({LifecycleState.VALIDATION, LifecycleState.OOS})


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
            failures.append(
                self._record(
                    FailureRecord(
                        subject_ref=hypothesis.ref,
                        terminal_state="FAILED",
                        reason_code=outcome.reason or ReasonCode.RUN_ERRORED,
                        evidence=evidence,
                        hypothesis_family_id=hypothesis.family_id,
                        lessons=None if outcome.error is None else outcome.error[:2000],
                        recorded_at=ctx.as_of,
                    )
                )
            )
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
        if result.report is None:  # the validator itself errored: a technical failure
            failures.append(
                self._record(
                    FailureRecord(
                        subject_ref=hypothesis.ref,
                        terminal_state="FAILED",
                        reason_code=ReasonCode.RUN_ERRORED,
                        evidence=(f"run:{result.outcome.run.run_id}", round_ref),
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
        failures.append(
            self._record(
                FailureRecord(
                    subject_ref=hypothesis.ref,
                    terminal_state=state,
                    reason_code=reason,
                    gate_id=gate_id,
                    evidence=evidence,
                    hypothesis_family_id=hypothesis.family_id,
                    recorded_at=ctx.as_of,
                )
            )
        )
        current = ctx.state_of(hypothesis.ref)
        if state == "FAILED" and current in _NO_FAILED_EDGE:
            unchanged.append(subject)
            return
        ctx.advance(
            hypothesis.ref,
            LifecycleState.REJECTED,
            reason=f"validation gate {gate_id} failed",
            evidence=evidence,
        )
