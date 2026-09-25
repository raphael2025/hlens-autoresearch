"""One state directory for the whole research loop (ADR-0049 implementation note, durable
composition, 2026-09-26).

``open_synthetic_loop(config, state_dir=...)`` (``research/loop/compose.py``) keeps every stateful
part of the composed loop in one directory, so a process can die between rounds and a new one
continues exactly where it stopped:

=====================  ==========================================================================
``audit.jsonl``        ``apps.worker.LoopAuditLog`` — the hash-chained round records
``memory.jsonl``       this module — a header (``loop_state_opened``: the configuration fingerprint)
                       and one ``round_memory`` checkpoint per recorded round
``trial_ledger.jsonl`` ``TrialLedger(path)`` — registrations and pre-registered re-evaluations
``sealed_oos.jsonl``   ``DurableUnsealingLedger`` — the sealed-OOS unsealings and evaluations
``lineage.jsonl``      ``LineageGraph(path=...)`` — every strategy spec the loop evolved from / into
``reviews.jsonl``      ``ReviewQueue(path)`` — LLM drafts, human approvals, drafts taken
``failures.jsonl``     ``FailureRegistry`` — FailureRecords (append-only, fsync'd, not chained)
=====================  ==========================================================================

Every file but ``failures.jsonl`` is an ``AppendOnlyJournal`` (``research.persistence`` /
``apps.worker.journal``): each verifies its own hash chain on open.

**Checkpoint.** ``ResearchLoop(checkpoint=...)`` calls ``MemoryCheckpoint`` with every finished
round's record **before** the audit records it. The checkpoint line holds

- ``round_index`` and ``record_hash``: the audit record it belongs to;
- ``heads``: the position of every other file at the end of the round — ``{"seq", "hash"}`` of each
  journal (entry count and chain head) and ``{"count", "digest"}`` of the failure registry
  (``content_hash`` of the record hashes);
- ``delta``: what the round added to the in-memory ``ResearchMemory`` that the stages read in
  later rounds — market specs (the market is regenerated on restore and must reproduce its
  ``market_hash``), research pieces, strategies added to the catalog (offspring), trial and
  validation outcomes (their pydantic records; the in-memory ``inputs`` / ``trial`` run artifacts
  are not kept — a restored ``TrialOutcome`` keeps ``knowledge_cutoff``), the experiment / state /
  offspring summaries.

**Cross-checks on reopening** (``LoopStateInconsistent`` on any failure; nothing is repaired):

1. configuration: the header's fingerprint equals this configuration's (loop id, seed, family,
   Profile, market spec, strategy catalog, knowledge, feature / state / label / cost specs,
   Constitution version, code commit, environment lock, evolution on / off); a missing header
   while any other file holds state is refused;
2. no interrupted round: an audit round started but never recorded is refused (what it spent is
   unknown; a human reviews it);
3. audit ↔ checkpoint: exactly one checkpoint per recorded round, same index and record hash;
4. positions: every checkpoint's position of every journal exists in that journal (same sequence
   number and chain hash), positions never go back, and the last checkpoint's position is the
   journal's end — except ``reviews.jsonl``, which may hold human approvals made after the last
   round (nothing else); the failure registry's count / digest likewise;
5. content: each round's restored delta equals what the audit record's hashed stage summaries say
   (ingest market and spec hash, the state summary, every experiment row = its trial's summary,
   every validation report row, every offspring row), trial / validation / hypothesis / report
   records reproduce their content hashes, and every catalog strategy the delta names rebuilds
   from its parent;
6. audit ↔ ledgers: every hypothesis the audit registered or re-evaluated is in the trial ledger
   (the re-evaluation under its attempt); every offspring's child and parent spec is in the lineage
   with the recorded spec hash; every ``human_review:<who>`` evidence has that human's approval of
   that draft in the review queue, taken; every unsealed / consumed sealed-OOS report has the
   family's unsealing by the same approver, marked evaluated; every failure record hash the audit
   lists is in the failure registry;
7. after the loop replayed the audit into its guard: every lifecycle subject is a registered
   hypothesis.

**Tail truncation.** Deleting whole trailing lines of one journal leaves a valid shorter chain
(the journal alone cannot tell). Across files it is detected: the checkpoint names the position of
every other file (a shorter ledger / lineage / vault / failure registry is *behind* its recorded
position) and the audit names the checkpoints (a shorter audit or memory file no longer pairs one
checkpoint per record). **Remaining limit:** truncating *every* file consistently back to an
earlier round boundary yields a valid, shorter history — detecting that needs an anchor outside the
directory (e.g. a ``record_hash`` published on the bus / kept elsewhere). Likewise, human approvals
appended after the last round are not named by any checkpoint until a round takes them, so
dropping only those is indistinguishable from "not approved yet".

The LLM provider is external: its own state (e.g. a scripted provider's position) is not loop
state and is the caller's to resume.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from apps.worker.loop import LifecycleGuard, LoopAuditLog, LoopRecord
from core.contracts.synthetic import SyntheticMarketProvider, SyntheticMarketSpec
from core.domain.base import FrozenMapping, canonical_json, content_hash
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    FailureRecord,
    Hypothesis,
    ValidationReport,
)
from core.domain.specs import StrategySpec
from core.errors import ReasonCode
from research.evolution import LineageGraph
from research.hypotheses import TrialLedger
from research.loop.memory import REVIEW_APPROVED, ResearchMemory, ReviewQueue
from research.loop.segment import ResearchPiece
from research.loop.trials import TrialOutcome, ValidationOutcome
from research.persistence import GENESIS_HASH, AppendOnlyJournal
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import StrategyCandidate
from research.validation.pipeline import CONSUMED_WITHOUT_RESULT
from research.validation.sealed_oos import DurableUnsealingLedger

__all__ = [
    "AUDIT_FILE",
    "FAILURES_FILE",
    "LEDGER_FILE",
    "LINEAGE_FILE",
    "LOOP_STATE_OPENED",
    "MEMORY_FILE",
    "REVIEWS_FILE",
    "ROUND_MEMORY",
    "SEALED_OOS_FILE",
    "STATE_VERSION",
    "DurableState",
    "LoopStateInconsistent",
    "MemoryCheckpoint",
    "open_state",
]

AUDIT_FILE: Final = "audit.jsonl"
MEMORY_FILE: Final = "memory.jsonl"
LEDGER_FILE: Final = "trial_ledger.jsonl"
SEALED_OOS_FILE: Final = "sealed_oos.jsonl"
LINEAGE_FILE: Final = "lineage.jsonl"
REVIEWS_FILE: Final = "reviews.jsonl"
FAILURES_FILE: Final = "failures.jsonl"

#: Memory journal line types.
LOOP_STATE_OPENED: Final = "loop_state_opened"
ROUND_MEMORY: Final = "round_memory"
#: Version of the memory journal's payload layout.
STATE_VERSION: Final = 1

#: The journals a checkpoint positions (name -> file), in a fixed order.
_JOURNALS: Final = (
    ("trial_ledger", LEDGER_FILE),
    ("sealed_oos", SEALED_OOS_FILE),
    ("lineage", LINEAGE_FILE),
    ("reviews", REVIEWS_FILE),
)
_ROUND_KEYS: Final = frozenset({"round_index", "record_hash", "heads", "delta"})
_DELTA_KEYS: Final = frozenset(
    {
        "markets",
        "research_data",
        "strategies",
        "trials",
        "validations",
        "experiments",
        "states",
        "offspring",
    }
)
_UNSEALED: Final = frozenset({"unsealed", CONSUMED_WITHOUT_RESULT})


class LoopStateInconsistent(RuntimeError):
    """The files of a loop state directory disagree with each other or with the configuration."""


# ------------------------------------------------------------------------------------ positions


def _journals(memory: ResearchMemory) -> dict[str, AppendOnlyJournal]:
    ledger, reviews = memory.ledger.journal, memory.reviews.journal
    graph, oos = memory.lineage_graph, memory.oos_ledger
    if (
        ledger is None
        or reviews is None
        or graph is None
        or graph.journal is None
        or not isinstance(oos, DurableUnsealingLedger)
    ):
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    return {
        "trial_ledger": ledger,
        "sealed_oos": oos.journal,
        "lineage": graph.journal,
        "reviews": reviews,
    }


def _failure_hashes(records: Sequence[FailureRecord]) -> list[str]:
    return [record.content_hash() for record in records]


def heads(memory: ResearchMemory) -> dict[str, Any]:
    """The position of every file of the state directory but the audit and the memory journal."""
    out: dict[str, Any] = {
        name: {"seq": len(journal.entries), "hash": journal.head_hash}
        for name, journal in _journals(memory).items()
    }
    hashes = _failure_hashes(memory.failures.records())
    out["failures"] = {"count": len(hashes), "digest": content_hash(hashes)}
    return out


# ----------------------------------------------------------------------------------- the delta


@dataclass(frozen=True, slots=True)
class _Marks:
    markets: int
    research_data: int
    strategies: int
    trials: int
    validations: int
    experiments: int
    states: int
    offspring: int

    @classmethod
    def of(cls, memory: ResearchMemory) -> _Marks:
        return cls(
            markets=len(memory.markets),
            research_data=len(memory.research_data),
            strategies=len(memory.strategies),
            trials=len(memory.trials),
            validations=len(memory.validations),
            experiments=len(memory.experiments),
            states=len(memory.states),
            offspring=len(memory.offspring),
        )


def _json(value: Any) -> Any:
    """``value`` as plain JSON data (canonical round trip; refuses NaN / Infinity)."""
    return json.loads(canonical_json(value))


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _trial_payload(outcome: TrialOutcome) -> dict[str, Any]:
    candidate = outcome.candidate
    return {
        "round_index": outcome.round_index,
        "hypothesis": outcome.hypothesis.model_dump(mode="json"),
        "hypothesis_hash": outcome.hypothesis.content_hash(),
        "origin": outcome.origin,
        "experiment": outcome.experiment.model_dump(mode="json"),
        "experiment_hash": outcome.experiment.content_hash(),
        "run": outcome.run.model_dump(mode="json"),
        "run_hash": outcome.run.content_hash(),
        "strategy": None if candidate is None else str(candidate.spec.ref),
        "strategy_hash": None if candidate is None else candidate.spec.content_hash(),
        "request_params": dict(outcome.request_params),
        "validation_seed": outcome.validation_seed,
        "summary": dict(outcome.summary),
        "error": outcome.error,
        "reason": None if outcome.reason is None else outcome.reason.value,
        "attempt": outcome.attempt,
        "knowledge_cutoff": _iso(outcome.knowledge_cutoff),
    }


def _report_payload(report: ValidationReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {"record": report.model_dump(mode="json"), "hash": report.content_hash()}


def _validation_payload(result: ValidationOutcome, trial_index: int) -> dict[str, Any]:
    return {
        "round_index": result.round_index,
        "trial": trial_index,
        "report": _report_payload(result.report),
        "failure_reason": None if result.failure_reason is None else result.failure_reason.value,
        "sealed_report": _report_payload(result.sealed_report),
        "sealed_status": dict(result.sealed_status),
        "summary": dict(result.summary),
        "error": result.error,
    }


def _first_bar(piece: ResearchPiece) -> int:
    start = piece.bars[0].interval_start
    return next(i for i, bar in enumerate(piece.market.bars) if bar.interval_start == start)


def _strategy_payload(candidate: StrategyCandidate) -> dict[str, Any]:
    spec = candidate.spec
    return {
        "spec": spec.model_dump(mode="json"),
        "spec_hash": spec.content_hash(),
        "parent": str(spec.lineage[-1]) if spec.lineage else None,
        "hypothesis_family_id": candidate.hypothesis_family_id,
        "risk_policy_hash": None
        if candidate.risk_policy is None
        else candidate.risk_policy.content_hash(),
    }


def _delta(memory: ResearchMemory, marks: _Marks) -> Any:
    now = _Marks.of(memory)
    if any(getattr(now, f.name) < getattr(marks, f.name) for f in fields(_Marks)):
        raise ValueError("research memory shrank: it is append-only")
    if len(memory.market_specs) != len(memory.markets):
        raise ValueError("every ingested market needs the spec it was generated from")
    market_index = {id(market): index for index, market in enumerate(memory.markets)}
    trial_index = {id(outcome): index for index, outcome in enumerate(memory.trials)}
    return _json(
        {
            "markets": [
                {"spec": spec.model_dump(mode="json"), "market_hash": market.market_hash}
                for spec, market in zip(
                    memory.market_specs[marks.markets :],
                    memory.markets[marks.markets :],
                    strict=True,
                )
            ],
            "research_data": [
                {
                    "market": market_index[id(piece.market)],
                    "first": _first_bar(piece),
                    "identity": piece.identity(),
                }
                for piece in memory.research_data[marks.research_data :]
            ],
            "strategies": [
                _strategy_payload(candidate)
                for candidate in list(memory.strategies.values())[marks.strategies :]
            ],
            "trials": [_trial_payload(o) for o in memory.trials[marks.trials :]],
            "validations": [
                _validation_payload(v, trial_index[id(v.outcome)])
                for v in memory.validations[marks.validations :]
            ],
            "experiments": memory.experiments[marks.experiments :],
            "states": memory.states[marks.states :],
            "offspring": memory.offspring[marks.offspring :],
        }
    )


class MemoryCheckpoint:
    """``ResearchLoop(checkpoint=...)``: one ``round_memory`` line per finished round."""

    def __init__(self, journal: AppendOnlyJournal, memory: ResearchMemory) -> None:
        self._journal = journal
        self._memory = memory
        self._marks = _Marks.of(memory)

    def __call__(self, record: LoopRecord) -> None:
        memory = self._memory
        self._journal.append(
            ROUND_MEMORY,
            {
                "round_index": record.round_index,
                "record_hash": record.record_hash,
                "heads": heads(memory),
                "delta": _delta(memory, self._marks),
            },
        )
        self._marks = _Marks.of(memory)


# ------------------------------------------------------------------------------------- restore


@dataclass(frozen=True)
class DurableState:
    """What ``open_state`` hands the composition root."""

    root: Path
    memory: ResearchMemory
    audit: LoopAuditLog
    checkpoint: MemoryCheckpoint

    def verify_guard(self, guard: LifecycleGuard) -> None:
        """Cross-check 7: every lifecycle subject the audit replayed is a registered hypothesis."""
        registered = {str(h.ref) for h in self.memory.ledger.hypotheses}
        stray = sorted(str(h.subject) for h in guard.histories if str(h.subject) not in registered)
        if stray:
            raise LoopStateInconsistent(
                f"the audit moved {stray} through the lifecycle, but the trial ledger never "
                "registered them"
            )


def _refuse(message: str) -> LoopStateInconsistent:
    return LoopStateInconsistent(message)


def open_state(
    state_dir: Path,
    *,
    fingerprint: Mapping[str, Any],
    strategies: Sequence[StrategyCandidate],
    provider: SyntheticMarketProvider,
    provider_for: Callable[[StrategySpec], Any] | None,
) -> DurableState:
    """Open (or create) a loop state directory and restore the research memory from it.

    ``strategies``: the configured library catalog (added before the restored offspring);
    ``provider``: regenerates the ingested markets; ``provider_for``: serves an offspring spec
    (the evolution plan's; ``None`` without evolution). Raises ``LoopStateInconsistent`` when the
    files disagree (see module docs), ``JournalCorrupted`` when one file is itself corrupt.
    """
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    audit = LoopAuditLog(root / AUDIT_FILE)
    journal = AppendOnlyJournal(root / MEMORY_FILE)
    graph = LineageGraph((), path=root / LINEAGE_FILE)
    memory = ResearchMemory(
        failures=FailureRegistry(root / FAILURES_FILE),
        ledger=TrialLedger(root / LEDGER_FILE),
        reviews=ReviewQueue(root / REVIEWS_FILE),
        oos_ledger=DurableUnsealingLedger(root / SEALED_OOS_FILE),
        lineage=list(graph.specs),
        lineage_graph=graph,
    )
    for candidate in strategies:
        memory.add_strategy(candidate)
    expected = _json(fingerprint)
    entries = journal.entries
    if not entries:
        _require_empty(audit, memory)
        journal.append(LOOP_STATE_OPENED, {"state_version": STATE_VERSION, "fingerprint": expected})
        return DurableState(root, memory, audit, MemoryCheckpoint(journal, memory))
    header = entries[0]
    if header.type != LOOP_STATE_OPENED or header.payload.get("state_version") != STATE_VERSION:
        raise _refuse(f"{journal.path} does not start with a loop state header")
    if header.payload.get("fingerprint") != expected:
        raise _refuse(
            f"{root} belongs to another loop configuration (fingerprint mismatch); a changed "
            "configuration is a new state directory"
        )
    checkpoints = []
    for entry in entries[1:]:
        if entry.type != ROUND_MEMORY or set(entry.payload) != _ROUND_KEYS:
            raise _refuse(f"{journal.path}:{entry.seq} is not a round memory checkpoint")
        checkpoints.append(entry.payload)
    _check_rounds(audit, checkpoints)
    _check_positions(memory, checkpoints)
    for record, checkpoint in zip(audit.records, checkpoints, strict=True):
        _restore_round(memory, record, checkpoint["delta"], provider, provider_for)
    _check_ledgers(memory, audit.records)
    return DurableState(root, memory, audit, MemoryCheckpoint(journal, memory))


def _require_empty(audit: LoopAuditLog, memory: ResearchMemory) -> None:
    """A state directory without a memory header must hold no state at all."""
    held = [name for name, journal in _journals(memory).items() if journal.entries]
    if audit.records or audit.open_round is not None:
        held.append("audit")
    if memory.failures.records():
        held.append("failures")
    if held:
        raise _refuse(
            f"the memory checkpoint file is missing or empty, but {sorted(held)} hold state: "
            "the directory was tampered with or mixed with another run"
        )


def _check_rounds(audit: LoopAuditLog, checkpoints: Sequence[Mapping[str, Any]]) -> None:
    """Cross-checks 2 and 3."""
    if audit.open_round is not None:
        raise _refuse(
            f"round {audit.open_round} was started but never recorded (interrupted): what it "
            "spent and wrote is unknown, so the loop does not continue until a human reviews it"
        )
    records = audit.records
    if len(checkpoints) != len(records):
        raise _refuse(
            f"the audit records {len(records)} round(s) but the memory checkpoint holds "
            f"{len(checkpoints)}: one of them was truncated or deleted, or a round was interrupted"
        )
    for index, (record, checkpoint) in enumerate(zip(records, checkpoints, strict=True)):
        if checkpoint["round_index"] != index or checkpoint["record_hash"] != record.record_hash:
            raise _refuse(f"the memory checkpoint of round {index} names another audit record")


def _check_positions(memory: ResearchMemory, checkpoints: Sequence[Mapping[str, Any]]) -> None:
    """Cross-check 4: every file is exactly where the checkpoints say it was."""
    for name, journal in _journals(memory).items():
        entries = journal.entries
        previous = 0
        for index, checkpoint in enumerate(checkpoints):
            position = checkpoint["heads"][name]
            seq = position["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < previous:
                raise _refuse(f"the checkpoint of round {index} moves {name} backwards")
            if seq > len(entries):
                raise _refuse(
                    f"{name} holds {len(entries)} line(s) but the checkpoint of round {index} "
                    f"recorded {seq}: {name} was truncated or deleted (the audit is ahead of it)"
                )
            head = entries[seq - 1].hash if seq else GENESIS_HASH
            if head != position["hash"]:
                raise _refuse(
                    f"{name} does not match the checkpoint of round {index}: its history was "
                    "rewritten"
                )
            previous = seq
        extra = entries[previous:]
        if name == "reviews":
            if any(entry.type != REVIEW_APPROVED for entry in extra):
                raise _refuse(
                    "reviews holds lines after the last recorded round other than human "
                    "approvals: the audit is behind it (truncated) or a round was interrupted"
                )
        elif extra:
            raise _refuse(
                f"{name} holds {len(extra)} line(s) no recorded round accounts for: the audit "
                "or the memory checkpoint is behind it (truncated), or a round was interrupted"
            )
    hashes = _failure_hashes(memory.failures.records())
    previous = 0
    for index, checkpoint in enumerate(checkpoints):
        position = checkpoint["heads"]["failures"]
        count = position["count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < previous:
            raise _refuse(f"the checkpoint of round {index} moves failures backwards")
        if count > len(hashes):
            raise _refuse(
                f"failures holds {len(hashes)} record(s) but the checkpoint of round {index} "
                f"recorded {count}: failures was truncated or deleted (the audit is ahead of it)"
            )
        if content_hash(hashes[:count]) != position["digest"]:
            raise _refuse(f"failures does not match the checkpoint of round {index}")
        previous = count
    if len(hashes) != previous:
        raise _refuse(
            f"failures holds {len(hashes) - previous} record(s) no recorded round accounts for"
        )


def _summary(record: LoopRecord, stage: str) -> Mapping[str, Any] | None:
    found = next((s for s in record.stages if s.name == stage), None)
    return None if found is None else found.summary


def _restore_round(
    memory: ResearchMemory,
    record: LoopRecord,
    delta: Any,
    provider: SyntheticMarketProvider,
    provider_for: Callable[[StrategySpec], Any] | None,
) -> None:
    """Re-apply one round's delta, checking it against the audit record (cross-check 5)."""
    index = record.round_index
    try:
        if set(delta) != _DELTA_KEYS:
            raise ValueError("the delta has other fields")
        _restore_markets(memory, record, delta, provider)
        _restore_strategies(memory, delta["strategies"], provider_for)
        _restore_trials(memory, record, delta)
        experiments = _summary(record, "experiment")
        if experiments is not None and delta["experiments"] != [
            {"round": index, **row} for row in experiments["experiments"]
        ]:
            raise ValueError("the experiment summaries differ from the audit")
        state = _summary(record, "state")
        if state is not None and delta["states"] != [{"round": index, **state}]:
            raise ValueError("the state summary differs from the audit")
        evolution = _summary(record, "evolution")
        if evolution is not None and delta["offspring"] != [
            {"round": index, **row} for row in evolution["offspring"]
        ]:
            raise ValueError("the offspring rows differ from the audit")
    except (ArithmeticError, KeyError, LookupError, TypeError, ValueError) as exc:
        raise _refuse(f"the memory checkpoint of round {index} is inconsistent: {exc}") from exc
    memory.experiments.extend(delta["experiments"])
    memory.states.extend(delta["states"])
    memory.offspring.extend(delta["offspring"])


def _restore_markets(
    memory: ResearchMemory, record: LoopRecord, delta: Any, provider: SyntheticMarketProvider
) -> None:
    for row in delta["markets"]:
        spec = SyntheticMarketSpec.model_validate(row["spec"])
        market = provider.generate(spec)
        if market.market_hash != row["market_hash"]:
            raise ValueError(f"market {len(memory.markets)} does not regenerate from its spec")
        memory.add_market(spec, market)
    ingest = _summary(record, "ingest")
    if ingest is not None:
        [row] = delta["markets"]
        if (ingest["market_hash"], ingest["spec_hash"]) != (
            row["market_hash"],
            memory.markets[-1].spec_hash,
        ):
            raise ValueError("the ingested market differs from the audit")
    for row in delta["research_data"]:
        market = memory.markets[row["market"]]
        count = row["identity"]["bars"]
        bars = tuple(market.bars[row["first"] : row["first"] + count])
        piece = ResearchPiece(row["identity"]["round"], market, bars)
        if len(bars) != count or _json(piece.identity()) != row["identity"]:
            raise ValueError("a research piece does not rebuild from its market")
        memory.research_data.append(piece)
    if ingest is not None and ingest["research_pieces"] != len(memory.research_data):
        raise ValueError("the accumulated research pieces differ from the audit")


def _restore_strategies(
    memory: ResearchMemory,
    rows: Sequence[Mapping[str, Any]],
    provider_for: Callable[[StrategySpec], Any] | None,
) -> None:
    for row in rows:
        spec = StrategySpec.model_validate(row["spec"])
        if spec.content_hash() != row["spec_hash"]:
            raise ValueError(f"{spec.ref} does not reproduce its spec hash")
        parent = memory.strategies.get(str(row["parent"]))
        if provider_for is None or parent is None:
            raise ValueError(f"{spec.ref} cannot be rebuilt: no evolution plan or no parent")
        risk_hash = None if parent.risk_policy is None else parent.risk_policy.content_hash()
        if (parent.hypothesis_family_id, risk_hash) != (
            row["hypothesis_family_id"],
            row["risk_policy_hash"],
        ):
            raise ValueError(f"{spec.ref} does not inherit its parent's family and risk policy")
        memory.add_strategy(
            StrategyCandidate(
                spec=spec,
                strategy=provider_for(spec),
                hypothesis_family_id=parent.hypothesis_family_id,
                risk_policy=parent.risk_policy,
                risk=parent.risk,
            )
        )


def _checked(model: Any, payload: Any, expected: Any, what: str) -> Any:
    value = model.model_validate(payload)
    if value.content_hash() != expected:
        raise ValueError(f"{what} does not reproduce its content hash")
    return value


def _report(payload: Any) -> ValidationReport | None:
    if payload is None:
        return None
    report: ValidationReport = _checked(
        ValidationReport, payload["record"], payload["hash"], "a validation report"
    )
    return report


def _restore_trials(memory: ResearchMemory, record: LoopRecord, delta: Any) -> None:
    index = record.round_index
    restored: list[TrialOutcome] = []
    for row in delta["trials"]:
        strategy = row["strategy"]
        candidate = None if strategy is None else memory.strategies[strategy]
        if candidate is not None and candidate.spec.content_hash() != row["strategy_hash"]:
            raise ValueError(f"trial strategy {strategy} is another spec in the catalog")
        cutoff = row["knowledge_cutoff"]
        restored.append(
            TrialOutcome(
                round_index=row["round_index"],
                hypothesis=_checked(
                    Hypothesis, row["hypothesis"], row["hypothesis_hash"], "a hypothesis"
                ),
                origin=row["origin"],
                experiment=_checked(
                    ExperimentSpec, row["experiment"], row["experiment_hash"], "an experiment"
                ),
                run=_checked(ExperimentRun, row["run"], row["run_hash"], "a run"),
                candidate=candidate,
                request_params=FrozenMapping(row["request_params"]),
                inputs=None,
                trial=None,
                validation_seed=row["validation_seed"],
                summary=row["summary"],
                error=row["error"],
                reason=None if row["reason"] is None else ReasonCode(row["reason"]),
                attempt=row["attempt"],
                knowledge_cutoff=None if cutoff is None else datetime.fromisoformat(cutoff),
            )
        )
    experiment = _summary(record, "experiment")
    if experiment is not None and [dict(o.summary) for o in restored] != experiment["experiments"]:
        raise ValueError("the restored trials differ from the audit's experiment rows")
    memory.trials.extend(restored)
    results: list[ValidationOutcome] = []
    for row in delta["validations"]:
        failure = row["failure_reason"]
        results.append(
            ValidationOutcome(
                round_index=row["round_index"],
                outcome=memory.trials[row["trial"]],
                report=_report(row["report"]),
                failure_reason=None if failure is None else ReasonCode(failure),
                sealed_report=_report(row["sealed_report"]),
                sealed_status=row["sealed_status"],
                summary=row["summary"],
                error=row["error"],
            )
        )
    validation = _summary(record, "validation")
    if validation is not None and [dict(v.summary) for v in results] != validation["reports"]:
        raise ValueError(f"the restored validations of round {index} differ from the audit")
    memory.validations.extend(results)


def _check_ledgers(memory: ResearchMemory, records: Sequence[LoopRecord]) -> None:
    """Cross-check 6: what the audit says happened is in the ledgers."""
    hypotheses = {str(h.ref): h for h in memory.ledger.hypotheses}
    trials = {(e.name, e.version, e.attempt) for e in memory.ledger.trial_log}
    lineage = {str(spec.ref): spec for spec in memory.lineage}
    approvals = {a.key: a for a in memory.reviews.approvals}
    failures = set(_failure_hashes(memory.failures.records()))

    def registered(ref: str, attempt: str | None, index: int) -> Hypothesis:
        hypothesis = hypotheses.get(ref)
        if hypothesis is None or (hypothesis.name, hypothesis.version, attempt) not in trials:
            what = ref if attempt is None else f"{ref} ({attempt})"
            raise _refuse(f"audit round {index} registered {what}, but the trial ledger has not")
        return hypothesis

    for record in records:
        index = record.round_index
        stage = _summary(record, "hypothesis")
        if stage is not None:
            for ref in stage["registered"]:
                registered(ref, None, index)
            for ref in stage["reevaluations"]:
                registered(ref, stage["reevaluation_attempt"], index)
        stage = _summary(record, "evolution")
        for row in [] if stage is None else stage["offspring"]:
            registered(row["hypothesis"], None, index)
            child = lineage.get(row["child"])
            if (
                child is None
                or child.content_hash() != row["child_spec_hash"]
                or row["parent"] not in lineage
            ):
                raise _refuse(f"audit round {index} evolved {row['child']}, not in the lineage")
        stage = _summary(record, "validation")
        for row in [] if stage is None else stage["reports"]:
            sealed = row.get("sealed_oos") or {}
            if sealed.get("status") not in _UNSEALED:
                continue
            family = registered(row["hypothesis"], row["attempt"], index).family_id
            unsealing = memory.oos_ledger.get(family)
            if (
                unsealing is None
                or unsealing.approved_by != sealed.get("approved_by")
                or not memory.oos_ledger.is_evaluated(family)
            ):
                raise _refuse(
                    f"audit round {index} unsealed the sealed OOS window of {family}, but the "
                    "unsealing ledger does not hold that evaluated unsealing"
                )
        stage = _summary(record, "memory")
        missing = [
            h for h in ([] if stage is None else stage["failure_records"]) if h not in failures
        ]
        if missing:
            raise _refuse(f"audit round {index} filed failure records {missing} not in failures")
        for transition in record.transitions:
            subject = str(transition.subject)
            for evidence in transition.evidence:
                if not evidence.startswith("human_review:"):
                    continue
                key = subject.split(":", 1)[1]
                approval = approvals.get(key)
                if (
                    approval is None
                    or approval.reviewer != evidence.removeprefix("human_review:")
                    or key not in memory.reviews.taken
                ):
                    raise _refuse(
                        f"audit round {index} registered {subject} on {evidence!r}, but the "
                        "review queue holds no such approval"
                    )
