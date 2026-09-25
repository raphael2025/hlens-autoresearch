"""Concrete research stages of the continuous loop (Phase 11; ADR-0049).

Each class implements ``apps.worker.loop.LoopStage`` (``name`` / ``estimate`` / ``run``). Every
number a stage uses — minutes per round, state window, cost units, compute declarations — is a
constructor parameter; validation thresholds are read from the bound ``ValidationProfile`` only.

- ``IngestStage``: generates this round's new market segment ``[as_of - minutes, as_of)``;
- ``StateStage``: loop-local trend / range summary of the new segment (efficiency ratio);
- ``HypothesisStage``: pre-registers knowledge hypotheses and human-reviewed LLM drafts
  (IDEA → CANDIDATE) and asks the LLM for one new draft, which only goes to the review queue;
- ``ExperimentStage``: lag-``k`` sign-following study on the new segment (``lag_minutes = k``),
  CANDIDATE → VALIDATION;
- ``ValidationStage``: screening gates G2 effective sample and G3 multiple-testing adjusted p
  (trials = the family's registrations, failures included);
- ``MemoryStage``: FAIL → REJECTED + FailureRecord, errored → FAILED + FailureRecord; PASS and
  INCONCLUSIVE stay in VALIDATION awaiting the full P4 / P8 pipeline.

The screening is **not** the full validation pipeline; a screening PASS never advances a subject
(to OOS or beyond). Positions use only returns known before the bar they trade (C-L1); the synthetic
market's planted truth is never an input (it is only counted in the audit summary).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

from apps.worker.loop import RoundContext, StageResult, StageUsage
from core.contracts.llm import LLMProvider
from core.contracts.synthetic import SyntheticMarket, SyntheticMarketProvider, SyntheticMarketSpec
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import content_hash
from core.domain.research import (
    FailureRecord,
    GateResult,
    Hypothesis,
    KnowledgeItem,
    ValidationReport,
    Verdict,
    derive_verdict,
)
from core.errors import ReasonCode
from core.lifecycle.strategy import LifecycleState
from research.hypotheses import HypothesisDraft, from_knowledge, from_llm
from research.loop.memory import ResearchMemory
from research.validation.gates import Direction, compare_gate, inconclusive_gate, threshold
from research.validation.stats import adjust_p_value, effective_sample_size, hac_t_test

__all__ = [
    "ExperimentStage",
    "HypothesisStage",
    "IngestStage",
    "MemoryStage",
    "StateStage",
    "ValidationStage",
]

_LAG: Final = re.compile(r"^lag_minutes\s*=\s*(\d+)$")
_MINUTE: Final = timedelta(minutes=1)
_GATE_REASON: Final = {
    "G2.effective_sample_size": ReasonCode.INSUFFICIENT_EFFECTIVE_SAMPLE,
    "G3.adjusted_p_value": ReasonCode.NOT_SIGNIFICANT_AFTER_MTC,
}


def _returns(market: SyntheticMarket) -> list[Decimal]:
    closes = [bar.close for bar in market.bars]
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


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
        *,
        minutes_per_round: int,
        compute_seconds_per_bar: Decimal,
    ) -> None:
        self._provider = provider
        self._base = base
        self._minutes = _positive_int(minutes_per_round, "minutes_per_round")
        self._per_bar = compute_seconds_per_bar

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._per_bar * self._minutes)

    def run(self, ctx: RoundContext) -> StageResult:
        payload = self._base.model_dump()
        payload.update(
            seed=ctx.seed, start=ctx.as_of - self._minutes * _MINUTE, minutes=self._minutes
        )
        spec = SyntheticMarketSpec.model_validate(payload)
        market = self._provider.generate(spec)
        summary = {
            "spec_hash": market.spec_hash,
            "market_hash": market.market_hash,
            "provider": market.provider,
            "bars": len(market.bars),
            "start": spec.start.isoformat(),
            "planted_effects": len(market.truth),
        }
        return StageResult(summary, self.estimate(ctx), {"market": market})


class StateStage:
    """Efficiency ratio ``|sum r| / sum |r|`` over the last ``window`` returns of the segment.

    ``>= trend_threshold`` is a trend (up / down by the sign), otherwise range. Both numbers are
    model parameters of the caller (not validation thresholds). Wiring the Phase 2
    ``StateProvider`` runner in place of this summary is a follow-up.
    """

    name = "state"

    def __init__(
        self,
        memory: ResearchMemory,
        *,
        window: int,
        trend_threshold: Decimal,
        compute_seconds: Decimal,
    ) -> None:
        self._memory = memory
        self._window = _positive_int(window, "window")
        if not Decimal(0) <= trend_threshold <= Decimal(1):
            raise ValueError("trend_threshold must be within [0, 1]")
        self._threshold = trend_threshold
        self._compute = compute_seconds

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._compute)

    def run(self, ctx: RoundContext) -> StageResult:
        market: SyntheticMarket = ctx.artifact("ingest", "market")
        returns = _returns(market)[-self._window :]
        label: str | None
        ratio: Decimal | None
        if len(returns) < self._window:
            label, ratio = None, None
        else:
            total = sum(returns, Decimal(0))
            path = sum((abs(r) for r in returns), Decimal(0))
            ratio = Decimal(0) if path == 0 else abs(total) / path
            if ratio >= self._threshold and total != 0:
                label = "trend_up" if total > 0 else "trend_down"
            else:
                label = "range"
        summary = {
            "label": label,
            "efficiency_ratio": None if ratio is None else str(ratio),
            "window": self._window,
        }
        self._memory.states.append({"round": ctx.round_index, **summary})
        return StageResult(summary, self.estimate(ctx), {"label": label})


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
        return StageResult(summary, self._usage(fresh, drafts), {"registered": tuple(registered)})

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


def _lag_of(hypothesis: Hypothesis) -> int:
    lags = [int(m.group(1)) for c in hypothesis.conditions if (m := _LAG.fullmatch(c.strip()))]
    if len(lags) != 1 or lags[0] < 1:
        raise ValueError(
            f"{hypothesis.ref} does not declare exactly one 'lag_minutes = k' (k >= 1)"
        )
    return lags[0]


class ExperimentStage:
    """Sign-following study: position ``sign(r_{t-k})`` earns ``r_t`` (only past returns used)."""

    name = "experiment"

    def __init__(self, memory: ResearchMemory, *, compute_seconds_per_trial: Decimal) -> None:
        self._memory = memory
        self._per_trial = compute_seconds_per_trial

    def estimate(self, ctx: RoundContext) -> StageUsage:
        registered: tuple[Hypothesis, ...] = ctx.artifact("hypothesis", "registered")
        return StageUsage(compute_seconds=self._per_trial * len(registered))

    def run(self, ctx: RoundContext) -> StageResult:
        market: SyntheticMarket = ctx.artifact("ingest", "market")
        registered: tuple[Hypothesis, ...] = ctx.artifact("hypothesis", "registered")
        returns = _returns(market)
        bars = market.bars
        results: list[dict[str, Any]] = []
        for hypothesis in registered:
            base = {"hypothesis": str(hypothesis.ref), "market_hash": market.market_hash}
            try:
                lag = _lag_of(hypothesis)
            except ValueError as exc:
                results.append({**base, "status": "errored", "error": str(exc)})
                continue
            values: list[float] = []
            intervals = []
            for i in range(lag, len(returns)):
                past = returns[i - lag]
                sign = (past > 0) - (past < 0)
                values.append(float(sign * returns[i]))
                bar = bars[i + 1]  # returns[i] is realized over bar i + 1
                intervals.append((bar.interval_start, bar.interval_end))
            row: dict[str, Any] = {
                **base,
                "status": "completed",
                "lag": lag,
                "n": len(values),
                "effective_n": effective_sample_size(intervals),
            }
            if len(values) >= 2:
                test = hac_t_test(values, lag=0)
                row.update(mean=test.mean, t_stat=test.t_stat, p_greater=test.p_greater)
            row["experiment_hash"] = content_hash(row)
            results.append(row)
            ctx.advance(
                hypothesis.ref,
                LifecycleState.VALIDATION,
                reason="experiment completed; awaiting validation",
                evidence=(f"experiment:{row['experiment_hash']}",),
            )
        self._memory.experiments.extend({"round": ctx.round_index, **r} for r in results)
        return StageResult(
            {"experiments": results},
            self.estimate(ctx),
            {"results": tuple(results), "by_ref": {str(h.ref): h for h in registered}},
        )


class ValidationStage:
    """Screening gates only (G2 effective sample, G3 adjusted p); thresholds from the Profile."""

    name = "validation"

    def __init__(
        self,
        memory: ResearchMemory,
        profile: ValidationProfile,
        *,
        constitution_version: str,
        compute_seconds: Decimal,
    ) -> None:
        self._memory = memory
        self._profile = profile
        self._constitution = constitution_version
        self._compute = compute_seconds

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._compute)

    def _gates(self, row: dict[str, Any], family_trials: int) -> tuple[GateResult, ...]:
        profile = self._profile
        sample = compare_gate(
            profile,
            "G2.effective_sample_size",
            "effective_trades",
            float(row["effective_n"]),
            threshold(profile, "sample_size.min_effective_trades_in_sample"),
            Direction.AT_LEAST,
        )
        if "p_greater" not in row:
            return sample, inconclusive_gate("G3.adjusted_p_value", "adjusted_p_value", 1.0)
        adjusted = adjust_p_value(
            float(row["p_greater"]),
            profile.significance.multiple_testing_method,
            family_trials,
        )
        significance = compare_gate(
            profile,
            "G3.adjusted_p_value",
            "adjusted_p_value",
            adjusted,
            threshold(profile, "significance.multiple_testing_threshold"),
            Direction.AT_MOST,
        )
        return sample, significance

    def run(self, ctx: RoundContext) -> StageResult:
        results: tuple[dict[str, Any], ...] = ctx.artifact("experiment", "results")
        by_ref: dict[str, Hypothesis] = ctx.artifact("experiment", "by_ref")
        reports: list[ValidationReport] = []
        rows: list[dict[str, Any]] = []
        for row in results:
            if row["status"] != "completed":
                continue
            hypothesis = by_ref[row["hypothesis"]]
            gates = self._gates(row, self._memory.ledger.trials(hypothesis.family_id))
            run_id = f"{ctx.loop_id}:{ctx.round_index}:{hypothesis.name}"
            report = ValidationReport(
                report_id=content_hash({"run_id": run_id, "experiment": row["experiment_hash"]}),
                run_id=run_id,
                subject=hypothesis.ref,
                experiment_hash=row["experiment_hash"],
                constitution_version=self._constitution,
                validation_profile=self._profile.ref,
                validation_profile_hash=self._profile.content_hash(),
                gates=gates,
                verdict=derive_verdict(gates),
                created_at=ctx.as_of,
            )
            reports.append(report)
            rows.append(
                {
                    "hypothesis": row["hypothesis"],
                    "report_id": report.report_id,
                    "report_hash": report.content_hash(),
                    "verdict": report.verdict.value,
                    "gates": [
                        {"gate_id": g.gate_id, "value": g.value, "verdict": g.verdict.value}
                        for g in gates
                    ],
                }
            )
        summary = {
            "scope": "loop screening (G2 effective sample, G3 adjusted p); not the full pipeline",
            "profile": str(self._profile.ref),
            "reports": rows,
        }
        return StageResult(summary, self.estimate(ctx), {"reports": tuple(reports)})


class MemoryStage:
    name = "memory"

    def __init__(self, memory: ResearchMemory) -> None:
        self._memory = memory

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage()

    def _record(self, record: FailureRecord) -> str:
        self._memory.failures.append(record)
        return record.content_hash()

    def run(self, ctx: RoundContext) -> StageResult:
        results: tuple[dict[str, Any], ...] = ctx.artifact("experiment", "results")
        by_ref: dict[str, Hypothesis] = ctx.artifact("experiment", "by_ref")
        reports: tuple[ValidationReport, ...] = ctx.artifact("validation", "reports")
        round_ref = f"loop_round:{ctx.loop_id}:{ctx.round_index}"
        failures: list[str] = []
        screen_passed: list[str] = []
        inconclusive: list[str] = []
        for row in results:
            if row["status"] == "completed":
                continue
            hypothesis = by_ref[row["hypothesis"]]
            failures.append(
                self._record(
                    FailureRecord(
                        subject_ref=hypothesis.ref,
                        terminal_state="FAILED",
                        reason_code=ReasonCode.RUN_ERRORED,
                        evidence=(round_ref,),
                        hypothesis_family_id=hypothesis.family_id,
                        lessons=str(row["error"])[:2000],
                        recorded_at=ctx.as_of,
                    )
                )
            )
            ctx.advance(
                hypothesis.ref,
                LifecycleState.FAILED,
                reason="experiment errored",
                evidence=(round_ref,),
            )
        for report in reports:
            subject = str(report.subject)
            hypothesis = by_ref[subject]
            if report.verdict is Verdict.PASS:
                screen_passed.append(subject)
                continue
            if report.verdict is Verdict.INCONCLUSIVE:
                inconclusive.append(subject)
                continue
            failed = next(g for g in report.gates if g.verdict is Verdict.FAIL)
            evidence = (f"validation_report:{report.report_id}", round_ref)
            failures.append(
                self._record(
                    FailureRecord(
                        subject_ref=report.subject,
                        terminal_state="REJECTED",
                        reason_code=_GATE_REASON.get(failed.gate_id, ReasonCode.CONTRACT_VIOLATION),
                        gate_id=failed.gate_id,
                        evidence=evidence,
                        hypothesis_family_id=hypothesis.family_id,
                        recorded_at=ctx.as_of,
                    )
                )
            )
            ctx.advance(
                report.subject,
                LifecycleState.REJECTED,
                reason=f"screening gate {failed.gate_id} failed",
                evidence=evidence,
            )
        summary = {
            "failure_records": failures,
            "screen_passed_awaiting_full_validation": screen_passed,
            "inconclusive": inconclusive,
            "ledger_size": len(self._memory.ledger.hypotheses),
            "pending_reviews": list(self._memory.reviews.pending),
        }
        return StageResult(summary)
