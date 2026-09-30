"""Optional in-loop replacement proposal trigger (ADR-0100 item 7, 2026-09-30; P12-LOOP).

**Default off.** ``LoopWiring.replacement_trigger`` is ``None`` by default, and a
``ReplacementTrigger`` is composed only with the explicit flag ``enabled=True``; without it every
stage, record, fingerprint and state directory is byte-identical to a loop without this module.
There is no scheduler here: the trigger runs inside the loop's own rounds, at the loop's own
cadence, and only in the rounds its ``every_rounds`` names.

**Where it runs.** The worker's stage order is a frozen contract (``core.contracts.loop_audit``:
one optional stage, ``evolution``). The trigger is part of that P12 extension point:
``ReplacementTriggerStage`` wraps the loop's ``EvolutionStage`` under the same stage name, runs it
unchanged, and then — in a due round — runs the trigger. It therefore needs an ``EvolutionPlan``
(the candidates it proposes are evolution offspring) and runs *after* the rounds before it: it sees
everything earlier rounds recorded (lineage, trial ledger) plus this round's ingested research
span. A retry round (ADR-0083) never runs it, exactly like evolution. Its audit summary is the
``replacement_trigger`` key of the ``evolution`` stage summary (``REPLACEMENT_TRIGGER_KEY``); the
offspring rows the durable opener compares are untouched.

**What a trigger is.** The caller's ``source(round_index, as_of)`` returns ``ReplacementInputs``
from outside the loop: running incumbents (``Incumbent``: ``ACTIVE`` / ``DEGRADED``), candidates
with the lifecycle a **human** gave them on the Promotion path and the validation reports that back
them, a report resolver and the Profiles of those reports. A candidate is *eligible* when it
descends from a given incumbent in the loop's lineage, is ``PAPER`` / ``PRODUCTION_CANDIDATE``
(OOS → PAPER is a human approval: the loop never makes it, ``apps.worker.loop.FORBIDDEN_TARGETS``),
has a pair with such an incumbent that the ``ProposalLedger`` does not hold yet, and was never
triggered before. Non-eligible candidates are listed in the summary and cost nothing. Each eligible
candidate is **one trigger**, in this order:

1. **trial** — a ``Hypothesis`` for the candidate version (origin ``combination``, the loop's own
   family: no new family) is registered in the loop's ``TrialLedger`` **before** anything is opened
   or evaluated, so every trigger counts as a trial of the family (G3 sees it) and against the
   ``LoopBudget`` (the stage declares it in ``estimate``). A candidate version is triggered at most
   once, ever: its hypothesis in the ledger is the durable mark (``already_triggered``);
2. **guard** — the independent sealed window (see below); a refusal is recorded (``refused``) and
   nothing is opened;
3. **opening** — the window's single opening is journaled in the loop's unsealing ledger
   (``research.validation.sealed_oos``, format 2, ``replacement_window_opened``) **before** any
   evidence is used, so it is consumed even if what follows fails;
4. **job** — the existing ``research.evolution.replacement_job.propose_replacements`` for that
   candidate and its pending incumbents, with the proposer the loop's own automation identity
   (``apps.worker.loop.loop_actor``), ``proposed_at`` the round's scheduled time and provenance
   evidence appended (``loop:``, ``loop_round:``, ``trial:``, ``sealed_window:``,
   ``sealed_window_opening:``). Every claimed report is checked by the job
   (``research.router.evidence.check_report``: found, hash, subject, verdict PASS including G5, its
   Profile given, ADR-0060 items). Proposals go to the caller's durable ``ProposalLedger`` and are
   always ``PENDING_HUMAN_APPROVAL``; nothing here approves, promotes, swaps or moves any lifecycle
   state. An exception from the job is recorded as ``failed`` (type and message) and the window
   stays consumed; nothing is retried or deleted.

**The sealed window guard.** A trigger may only use an **independent, pre-registered** window the
loop has **never opened**:

- *pre-registered*: the trigger's ``windows`` (``RegisteredSealedWindow``: id, bounds, registrar,
  registration time not after the window's start) are part of the loop's configuration fingerprint,
  i.e. of the anchored ``loop_state_opened`` header written when the state directory was created;
  reopening with other windows is refused (a new window is a new directory), like a budget;
- *independent*: no registered window overlaps the Profile's sealed OOS window (refused at
  composition) or another registered window, and a window the loop's accumulated research data
  reaches into (``RoundData.research_start`` / ``research_end`` of this round) is refused — the loop
  has seen it;
- *evidence on it*: at least one claimed report's Profile has exactly that window as its sealed OOS
  window (``SealedWindow.from_profile``); evidence on the loop's own window or on an unregistered
  window is not independent (refused); evidence on several registered windows is refused (one
  trigger opens exactly one);
- *never opened*: a window the unsealing ledger records as opened — for anyone, in particular an
  ancestor of the candidate — is refused (single use: descendants never reuse a sealed window);
- the candidate's history must hold its ``OOS → PAPER`` transition approved by a non-automation
  identity.

The trigger requires a ``DurableUnsealingLedger`` (a state directory's ``sealed_oos.jsonl``, or
``ResearchMemory(oos_ledger=DurableUnsealingLedger(path))``): an in-memory ledger forgets openings
on a restart and a window could be opened twice.

**Durability** (ADR-0073 / ADR-0083 patterns): the trial registration and the window opening are
store writes of the round, inside the state's admission gate, positioned by the round checkpoint
and anchored with it; the audit record holds each trigger's full row (trial, window, opening,
the job's payload including every proposal); the durable opener cross-checks every row's trial
against the trial ledger and every opening against the rows (``research.loop.durable``, cross-check
6). A process that dies inside the round leaves an unrecorded round, which the opener refuses for
human review; a proposal the job had already recorded stays in the ``ProposalLedger`` (pending,
idempotent: the pair is never proposed twice). The ``ProposalLedger`` lives outside the state
directory (its own single-writer lock and optional external anchor).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from apps.worker.loop import RoundContext, StageResult, StageUsage, loop_actor
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import Hypothesis, HypothesisOrigin, ValidationReport
from core.lifecycle.strategy import LifecycleState
from research.evolution import LineageGraph
from research.evolution.proposals import (
    CANDIDATE_STATES,
    PENDING_HUMAN_APPROVAL,
    ProposalLedger,
)
from research.evolution.replacement_job import (
    Incumbent,
    ReplacementCandidate,
    propose_replacements,
)
from research.loop.evolution import EvolutionStage, _retry_round
from research.loop.memory import AUTOMATION_ACTOR_PREFIX, ResearchMemory
from research.router.evidence import ReportResolver, ReportUnreadable
from research.validation.sealed_oos import (
    DurableUnsealingLedger,
    RegisteredSealedWindow,
    SealedWindow,
    WindowOpening,
)

__all__ = [
    "REPLACEMENT_TRIGGER_KEY",
    "TRIGGER_FORMAT",
    "ReplacementInputs",
    "ReplacementTrigger",
    "ReplacementTriggerStage",
    "check_independent_of_profile",
    "trigger_rows",
]

#: The key of the trigger's summary inside the ``evolution`` stage summary.
REPLACEMENT_TRIGGER_KEY: Final = "replacement_trigger"
#: Format of the trigger's summary and fingerprint payload.
TRIGGER_FORMAT: Final = 1
_REASON_FIELDS: Final = ("incumbent", "incumbent_state", "candidate", "candidate_state")
_TOKEN: Final = re.compile(r"[^a-z0-9_]")


@dataclass(frozen=True)
class ReplacementInputs:
    """What lives outside the loop, handed in by the caller's source (module docs)."""

    incumbents: Sequence[Incumbent]
    candidates: Sequence[ReplacementCandidate]
    reports: ReportResolver
    profiles: Sequence[ValidationProfile]

    def __post_init__(self) -> None:
        if not all(isinstance(i, Incumbent) for i in self.incumbents):
            raise TypeError("every incumbent must be an Incumbent (spec + ACTIVE/DEGRADED history)")
        if not all(isinstance(c, ReplacementCandidate) for c in self.candidates):
            raise TypeError("every candidate must be a ReplacementCandidate")
        if not all(isinstance(p, ValidationProfile) for p in self.profiles):
            raise TypeError("every profile must be a ValidationProfile")
        if not callable(self.reports):
            raise TypeError("reports must be a resolver: report hash -> ValidationReport | None")


#: ``source(round_index, as_of)``: read-only, deterministic within a round (it is asked once, in
#: the stage's ``estimate``, and the answer is reused by ``run``).
ReplacementSource = Callable[[int, datetime], ReplacementInputs]


@dataclass(frozen=True)
class ReplacementTrigger:
    """The trigger's configuration (module docs); no defaults, ``enabled`` must be explicit.

    ``windows``, ``every_rounds``, ``reason``, ``minimum_meaningful_effect`` and
    ``compute_seconds`` are fingerprinted (``payload``); ``source`` and ``ledger`` are external
    (code / the caller's store), like an evolution plan's ``provider_for``.
    """

    enabled: bool
    every_rounds: int
    windows: tuple[RegisteredSealedWindow, ...]
    source: ReplacementSource
    ledger: ProposalLedger
    reason: str
    minimum_meaningful_effect: str
    compute_seconds: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be an explicit bool")
        if (
            isinstance(self.every_rounds, bool)
            or not isinstance(self.every_rounds, int)
            or self.every_rounds < 1
        ):
            raise ValueError("every_rounds must be a positive int")
        if not isinstance(self.windows, tuple) or not self.windows:
            raise ValueError("a replacement trigger needs a non-empty tuple of registered windows")
        if not all(isinstance(w, RegisteredSealedWindow) for w in self.windows):
            raise TypeError("every window must be a RegisteredSealedWindow")
        ids = [w.window_id for w in self.windows]
        if len(set(ids)) != len(ids):
            raise ValueError(f"registered window ids must be unique: {ids}")
        for index, window in enumerate(self.windows):
            for other in self.windows[index + 1 :]:
                if window.overlaps(other.start, other.end):
                    raise ValueError(
                        f"registered windows {window.window_id!r} and {other.window_id!r} "
                        "overlap: independent windows share no instant"
                    )
        if not callable(self.source):
            raise TypeError("source must be callable: (round_index, as_of) -> ReplacementInputs")
        if not isinstance(self.ledger, ProposalLedger):
            raise TypeError("ledger must be a durable ProposalLedger")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a replacement trigger needs a non-empty reason template")
        try:
            self.reason.format(**{name: name for name in _REASON_FIELDS})
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"the reason template may only use {{{'}, {'.join(_REASON_FIELDS)}}}: {exc!r}"
            ) from exc
        if (
            not isinstance(self.minimum_meaningful_effect, str)
            or not self.minimum_meaningful_effect.strip()
        ):
            raise ValueError("minimum_meaningful_effect must be declared (no default)")
        if not isinstance(self.compute_seconds, Decimal) or not self.compute_seconds.is_finite():
            raise TypeError("compute_seconds must be a finite Decimal")
        if self.compute_seconds < 0:
            raise ValueError("compute_seconds must not be negative")

    def payload(self) -> dict[str, Any]:
        """The fingerprinted part (the pre-registration of every window included)."""
        return {
            "format_version": TRIGGER_FORMAT,
            "enabled": self.enabled,
            "every_rounds": self.every_rounds,
            "windows": [
                {**w.payload(), "registration_hash": w.registration_hash()} for w in self.windows
            ],
            "reason": self.reason,
            "minimum_meaningful_effect": self.minimum_meaningful_effect,
            "compute_seconds": str(self.compute_seconds),
        }


@dataclass(frozen=True)
class _Eligible:
    candidate: ReplacementCandidate
    hypothesis: Hypothesis
    incumbents: tuple[Incumbent, ...]


@dataclass(frozen=True)
class _Plan:
    inputs: ReplacementInputs
    eligible: tuple[_Eligible, ...]
    skipped: tuple[dict[str, Any], ...]
    already: tuple[dict[str, Any], ...]


def _token(text: str) -> str:
    return _TOKEN.sub("_", text.lower())


def _human_paper_approval(candidate: ReplacementCandidate) -> str | None:
    """The refusal when the candidate's ``OOS → PAPER`` was not approved by a human."""
    approvals = [
        t
        for t in candidate.history.transitions
        if t.from_state is LifecycleState.OOS and t.to_state is LifecycleState.PAPER
    ]
    if not approvals:
        return "its history holds no OOS → PAPER transition (a human approval, ADR-0006)"
    for transition in approvals:
        approver = transition.approved_by
        if approver is None or not approver.strip() or approver.startswith(AUTOMATION_ACTOR_PREFIX):
            return (
                f"its OOS → PAPER transition was approved by {approver!r}, not a human "
                "(OOS → PAPER requires human approval)"
            )
    return None


def check_independent_of_profile(trigger: ReplacementTrigger, profile: ValidationProfile) -> None:
    """No registered window overlaps the Profile's own sealed OOS window (``ValueError``)."""
    own = SealedWindow.from_profile(profile)
    for window in trigger.windows:
        if window.overlaps(own.start, own.end):
            raise ValueError(
                f"registered window {window.window_id!r} overlaps the Profile's sealed OOS "
                f"window [{own.start.isoformat()}, {own.end.isoformat()}): the loop opens "
                "that window itself, so it is not independent"
            )


class ReplacementTriggerStage:
    """``EvolutionStage`` followed by the replacement trigger, as the ``evolution`` stage."""

    name = "evolution"

    def __init__(
        self,
        evolution: EvolutionStage,
        memory: ResearchMemory,
        trigger: ReplacementTrigger,
        *,
        loop_id: str,
        family_id: str,
        profile: ValidationProfile,
    ) -> None:
        if not isinstance(trigger, ReplacementTrigger) or trigger.enabled is not True:
            raise ValueError("the replacement trigger is composed only with enabled=True")
        ledger = memory.oos_ledger
        if not isinstance(ledger, DurableUnsealingLedger):
            raise ValueError(
                "the replacement trigger needs a durable unsealing ledger (a state_dir, or "
                "ResearchMemory(oos_ledger=DurableUnsealingLedger(path))): an in-memory ledger "
                "forgets window openings on a restart"
            )
        check_independent_of_profile(trigger, profile)
        if not family_id.strip():
            raise ValueError("family_id must not be blank")
        self._evolution = evolution
        self._memory = memory
        self._windows_ledger: DurableUnsealingLedger = ledger
        self._trigger = trigger
        self._loop_id = loop_id
        self._family_id = family_id
        self._actor = loop_actor(loop_id)
        self._cached: tuple[RoundContext, _Plan] | None = None

    # ------------------------------------------------------------------ planning

    def _due(self, ctx: RoundContext) -> bool:
        if _retry_round(ctx):  # ADR-0083 "PM 决定" §1: a retry round never evolves or triggers
            return False
        every = self._trigger.every_rounds
        return ctx.round_index > 0 and ctx.round_index % every == 0

    def _hypothesis(self, candidate: ReplacementCandidate) -> Hypothesis:
        spec = candidate.spec
        return Hypothesis(
            name=f"rt_{_token(spec.name)}_v{_token(spec.version)}",
            version="1.0.0",
            family_id=self._family_id,
            statement=(
                f"{spec.ref}, moved to {sorted(s.value for s in CANDIDATE_STATES)} by a human on "
                "the Promotion path, is proposed to replace a running ancestor on evidence "
                "evaluated on an independent pre-registered sealed window; a proposal is never "
                "a promotion"
            ),
            conditions=(
                f"strategy = {spec.name}@{spec.version}",
                f"strategy_hash = {spec.content_hash()}",
            ),
            expected_direction="higher",
            minimum_meaningful_effect=self._trigger.minimum_meaningful_effect,
            origin=HypothesisOrigin.COMBINATION,
            origin_refs=(spec.ref,),
        )

    def _plan(self, ctx: RoundContext) -> _Plan:
        cached = self._cached
        if cached is not None and cached[0] is ctx:
            return cached[1]
        inputs = self._trigger.source(ctx.round_index, ctx.as_of)
        if not isinstance(inputs, ReplacementInputs):
            raise TypeError("the replacement source must return ReplacementInputs")
        lineage = LineageGraph(self._memory.lineage)
        registered = {(h.name, h.version): h for h in self._memory.ledger.hypotheses}
        eligible: list[_Eligible] = []
        skipped: list[dict[str, Any]] = []
        already: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in inputs.candidates:
            ref = str(candidate.spec.ref)
            if ref in seen:
                skipped.append({"candidate": ref, "reason": "listed twice by the source"})
                continue
            seen.add(ref)
            hypothesis = self._hypothesis(candidate)
            known = registered.get((hypothesis.name, hypothesis.version))
            if known is not None:
                if known.content_hash() == hypothesis.content_hash():
                    already.append({"candidate": ref, "hypothesis": str(known.ref)})
                else:
                    skipped.append(
                        {
                            "candidate": ref,
                            "reason": f"{known.ref} is registered with other content",
                        }
                    )
                continue
            ancestors = {str(a) for a in lineage.ancestors(candidate.spec.ref)}
            targets = [i for i in inputs.incumbents if str(i.spec.ref) in ancestors]
            if not targets:
                skipped.append({"candidate": ref, "reason": "descends from no given incumbent"})
                continue
            proposals = self._trigger.ledger
            pending = tuple(
                i for i in targets if proposals.proposal_for(str(i.spec.ref), ref) is None
            )
            if not pending:
                skipped.append(
                    {"candidate": ref, "reason": "already proposed for every given incumbent"}
                )
                continue
            state = candidate.history.current_state
            if state not in CANDIDATE_STATES:
                skipped.append(
                    {
                        "candidate": ref,
                        "reason": f"{state.value}: awaiting the human OOS → PAPER approval",
                    }
                )
                continue
            eligible.append(_Eligible(candidate, hypothesis, pending))
        plan = _Plan(inputs, tuple(eligible), tuple(skipped), tuple(already))
        self._cached = (ctx, plan)
        return plan

    # ------------------------------------------------------------------ guard

    def _research_span(self, ctx: RoundContext) -> tuple[datetime, datetime] | None:
        segment = ctx.artifacts.get("ingest", {}).get("segment")
        start = getattr(segment, "research_start", None)
        end = getattr(segment, "research_end", None)
        if isinstance(start, datetime) and isinstance(end, datetime):
            return start, end
        return None

    def _guard(
        self, ctx: RoundContext, item: _Eligible, inputs: ReplacementInputs
    ) -> tuple[RegisteredSealedWindow | None, tuple[str, ...], str | None]:
        """``(window, reports on it, None)`` or ``(None, (), refusal)`` (module docs)."""
        candidate = item.candidate
        refusal = _human_paper_approval(candidate)
        if refusal is not None:
            return None, (), refusal
        matched: dict[str, tuple[RegisteredSealedWindow, list[str]]] = {}
        for report_hash in candidate.report_hashes:
            try:
                report = inputs.reports(report_hash)
            except ReportUnreadable:
                continue  # the job's check_report refuses it
            if not isinstance(report, ValidationReport):
                continue
            profile = next(
                (
                    p
                    for p in inputs.profiles
                    if p.content_hash() == report.validation_profile_hash
                    and p.ref.target_identity() == report.validation_profile.target_identity()
                ),
                None,
            )
            if profile is None:
                continue
            sealed = SealedWindow.from_profile(profile)
            for window in self._trigger.windows:
                if window.same_bounds(sealed):
                    matched.setdefault(window.window_id, (window, []))[1].append(report_hash)
        lineage = LineageGraph(self._memory.lineage)
        ancestors = {str(a) for a in lineage.ancestors(candidate.spec.ref)}
        for window, _ in matched.values():
            opening = self._windows_ledger.window_opening(window.window_id)
            if opening is not None:
                whose = (
                    f"{opening.subject}, an ancestor of the candidate: descendants never reuse a "
                    "sealed window"
                    if opening.subject in ancestors
                    else f"{opening.subject}: a sealed window opens once"
                )
                return (
                    None,
                    (),
                    f"sealed window {window.window_id!r} was already opened in round "
                    f"{opening.round_index} for {whose}",
                )
        if not matched:
            return (
                None,
                (),
                "no claimed report was evaluated on a pre-registered replacement window (its "
                "Profile's sealed OOS window); the loop's own window or an unregistered one is "
                "not independent evidence",
            )
        if len(matched) > 1:
            return (
                None,
                (),
                f"the claimed reports use several pre-registered windows {sorted(matched)}: one "
                "trigger opens exactly one",
            )
        window, hashes = next(iter(matched.values()))
        span = self._research_span(ctx)
        if span is None:
            return None, (), "the round's research data span is unknown (no ingest segment)"
        if window.overlaps(span[0], span[1]):
            return (
                None,
                (),
                f"the loop's accumulated research data [{span[0].isoformat()}, "
                f"{span[1].isoformat()}] reaches into window {window.window_id!r}: the loop "
                "has seen it",
            )
        return window, tuple(hashes), None

    # ------------------------------------------------------------------ stage protocol

    def estimate(self, ctx: RoundContext) -> StageUsage:
        usage = self._evolution.estimate(ctx)
        if not self._due(ctx):
            return usage
        plan = self._plan(ctx)
        return usage + StageUsage(
            trials=len(plan.eligible), compute_seconds=self._trigger.compute_seconds
        )

    def run(self, ctx: RoundContext) -> StageResult:
        result = self._evolution.run(ctx)
        if not self._due(ctx):
            summary = {**result.summary, REPLACEMENT_TRIGGER_KEY: {"due": False}}
            return StageResult(summary, result.usage, result.artifacts)
        plan = self._plan(ctx)
        rows = [self._trigger_one(ctx, item, plan.inputs) for item in plan.eligible]
        trigger_summary = {
            "due": True,
            "format_version": TRIGGER_FORMAT,
            "status": PENDING_HUMAN_APPROVAL,
            "proposed_by": self._actor,
            "triggers": rows,
            "not_eligible": list(plan.skipped),
            "already_triggered": list(plan.already),
            "family_trials": self._memory.ledger.trials(self._family_id),
        }
        usage = result.usage + StageUsage(
            trials=len(rows), compute_seconds=self._trigger.compute_seconds
        )
        return StageResult(
            {**result.summary, REPLACEMENT_TRIGGER_KEY: trigger_summary}, usage, result.artifacts
        )

    def _trigger_one(
        self, ctx: RoundContext, item: _Eligible, inputs: ReplacementInputs
    ) -> dict[str, Any]:
        candidate, hypothesis = item.candidate, item.hypothesis
        spec = candidate.spec
        row: dict[str, Any] = {
            "candidate": str(spec.ref),
            "candidate_hash": spec.content_hash(),
            "candidate_state": candidate.history.current_state.value,
            "incumbents": [str(i.spec.ref) for i in item.incumbents],
            "report_hashes": list(candidate.report_hashes),
        }
        self._memory.ledger.register(hypothesis)  # 1. the trial, before anything is opened
        row["trial"] = {
            "hypothesis": str(hypothesis.ref),
            "hypothesis_hash": hypothesis.content_hash(),
            "family_id": hypothesis.family_id,
        }
        window, on_window, refusal = self._guard(ctx, item, inputs)  # 2. the guard
        if window is None:
            row.update(
                status="refused",
                refusal=refusal,
                window=None,
                window_opening=None,
                window_opening_hash=None,
                job=None,
                error=None,
            )
            return row
        opening = WindowOpening(
            window_id=window.window_id,
            registration_hash=window.registration_hash(),
            subject=str(spec.ref),
            subject_hash=spec.content_hash(),
            trial=str(hypothesis.ref),
            trial_hash=hypothesis.content_hash(),
            loop_id=ctx.loop_id,
            round_index=ctx.round_index,
        )
        self._windows_ledger.open_window(opening)  # 3. consumed before any evidence is used
        row.update(
            window={
                **window.payload(),
                "registration_hash": window.registration_hash(),
                "report_hashes": list(on_window),
            },
            window_opening=opening.payload(),
            window_opening_hash=opening.opening_hash(),
            refusal=None,
        )
        provenance = (
            f"loop:{ctx.loop_id}",
            f"loop_round:{ctx.round_index}",
            f"trial:{hypothesis.ref}#{hypothesis.content_hash()}",
            f"sealed_window:{window.window_id}#{window.registration_hash()}",
            f"sealed_window_opening:{opening.opening_hash()}",
        )
        try:  # 4. the existing job; any failure is recorded, the window stays consumed
            job = propose_replacements(
                incumbents=item.incumbents,
                candidates=(candidate,),
                reports=inputs.reports,
                profiles=inputs.profiles,
                lineage=LineageGraph(self._memory.lineage),
                ledger=self._trigger.ledger,
                reason=self._trigger.reason,
                proposed_by=self._actor,
                proposed_at=ctx.as_of,
                extra_evidence=provenance,
            )
        except Exception as exc:  # noqa: BLE001 - a failed trigger is research data, kept
            row.update(
                status="failed",
                job=None,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            return row
        row.update(
            status="proposed" if job.recorded else "not_proposed",
            job=job.payload(),
            error=None,
        )
        return row


def trigger_rows(summary: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """The trigger rows of one ``evolution`` stage summary (empty when absent or not due)."""
    if summary is None:
        return ()
    trigger = summary.get(REPLACEMENT_TRIGGER_KEY)
    if not isinstance(trigger, Mapping) or trigger.get("due") is not True:
        return ()
    rows = trigger.get("triggers", ())
    return tuple(rows)
