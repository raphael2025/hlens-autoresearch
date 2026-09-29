"""Durable PREPARE / COMMIT evidence for Phase 7 typed-plan admission (ADR-0073).

This module is a persistence primitive only. It does not compile, lower, execute, or authorize a
typed plan, and it does not open or modify a Research Loop state directory. The loop composition
root is responsible for holding its single-writer lock and coordinating this journal with the
TrialLedger, memory checkpoint, and external anchor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from core.domain.base import SHA256_PATTERN, FrozenMapping, canonical_json, content_hash
from core.domain.research import Hypothesis, HypothesisOrigin
from research.hypotheses.typed_plan import PlanLimits, PlanRejected, TypedPlan, parse_plan_json
from research.persistence import AppendOnlyJournal, JournalEntry

__all__ = [
    "PLAN_ADMISSION_FORMAT_VERSION",
    "PlanAdmissionCorrupted",
    "PlanAdmissionError",
    "PlanAdmissionEvidence",
    "PlanAdmissionJournal",
    "PreparedAdmission",
    "CommittedAdmission",
    "RoundStartedIdentity",
]

PLAN_ADMISSION_FORMAT_VERSION: Final = "1.0.0"
_PREPARE_EVENT: Final = "plan_admission_prepare"
_COMMIT_EVENT: Final = "plan_admission_commit"
_HEADER_EVENT: Final = "plan_admission_header"
_BATCH_EVENT: Final = "register_batch"
_HASH = re.compile(SHA256_PATTERN)
_PREPARE_KEYS: Final = frozenset(
    {
        "schema_version",
        "transaction_id",
        "manifest_hash",
        "round",
        "plan",
        "compiler",
        "operators",
        "providers",
        "inputs",
        "outputs",
        "experiment_specs",
        "hypotheses",
        "ledger_baseline",
        "ledger_batch_payload_hash",
    }
)
_COMMIT_KEYS: Final = frozenset(
    {"schema_version", "transaction_id", "prepare_seq", "prepare_hash", "ledger_event"}
)
_ROUND_KEYS: Final = frozenset(
    {"loop_id", "round_index", "started_entry_seq", "started_entry_hash"}
)
_POSITION_KEYS: Final = frozenset({"seq", "hash"})
_EVIDENCE_KEYS: Final = frozenset({"data", "content_hash"})
_LEDGER_EVENT_KEYS: Final = frozenset(
    {"type", "seq", "prev_hash", "hash", "payload_hash"}
)
_HEADER_KEYS: Final = frozenset({"schema_version", "loop_id", "state_version"})


class PlanAdmissionError(ValueError):
    """A plan admission transition is invalid or cannot be matched to its prepared evidence."""


class PlanAdmissionCorrupted(RuntimeError):
    """The plan admission journal does not replay as one strict PREPARE / COMMIT history."""


def _is_json_value(value: object) -> bool:
    """Accept canonical JSON data and explicitly exclude floats / non-JSON Python values."""
    if value is None or type(value) in (str, int, bool):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False


def _json_object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    copied = dict(value)
    if not _is_json_value(copied):
        raise ValueError(f"{what} must contain only canonical JSON values without floats")
    try:
        return cast(dict[str, Any], json.loads(canonical_json(copied)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} is not canonical JSON data: {exc}") from exc


def _object(value: object, keys: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        actual = sorted(value) if isinstance(value, Mapping) else type(value).__name__
        raise ValueError(f"{what} fields must be exactly {sorted(keys)}, got {actual}")
    return dict(value)


def _hash(value: object, what: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(f"{what} must be a lowercase SHA-256 hash")
    return value


def _hypothesis_sort_key(hypothesis: Hypothesis) -> tuple[str, str, str, str]:
    return (
        hypothesis.family_id,
        hypothesis.name,
        hypothesis.version,
        hypothesis.content_hash(),
    )


def _positive_int(value: object, what: str, *, zero_ok: bool = False) -> int:
    lower = 0 if zero_ok else 1
    if type(value) is not int or value < lower:
        qualifier = "non-negative" if zero_ok else "positive"
        raise ValueError(f"{what} must be a {qualifier} integer")
    return value


def _plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _entry_hash(seq: int, event_type: str, payload: Mapping[str, Any], previous_hash: str) -> str:
    body = canonical_json(
        {"seq": seq, "type": event_type, "payload": payload, "prev_hash": previous_hash}
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanAdmissionEvidence:
    """A canonical JSON object bound to its project content hash.

    Plan artifacts use a fixed ``{"identity": ..., "value": ...}`` data envelope. The module
    validates that envelope and hash but deliberately does not infer domain-specific identity
    fields or lowering semantics.
    """

    data: FrozenMapping[str, Any]
    content_hash: str

    def __post_init__(self) -> None:
        normalized = _json_object(self.data, "evidence data")
        digest = _hash(self.content_hash, "evidence content_hash")
        if content_hash(normalized) != digest:
            raise ValueError("evidence content_hash does not match its data")
        object.__setattr__(self, "data", FrozenMapping(normalized))

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PlanAdmissionEvidence:
        normalized = _json_object(data, "evidence data")
        return cls(FrozenMapping(normalized), content_hash(normalized))

    def payload(self) -> dict[str, Any]:
        return {"data": _plain(self.data), "content_hash": self.content_hash}


def _parse_evidence(value: object, what: str) -> PlanAdmissionEvidence:
    raw = _object(value, _EVIDENCE_KEYS, what)
    data = _json_object(raw["data"], f"{what}.data")
    digest = _hash(raw["content_hash"], f"{what}.content_hash")
    try:
        return PlanAdmissionEvidence(FrozenMapping(data), digest)
    except ValueError as exc:
        raise PlanAdmissionCorrupted(f"{what} failed content verification: {exc}") from exc


def _validate_artifact_evidence(evidence: PlanAdmissionEvidence, what: str) -> None:
    data = _object(_plain(evidence.data), frozenset({"identity", "value"}), f"{what}.data")
    identity = data["identity"]
    if isinstance(identity, str):
        if not identity or identity != identity.strip():
            raise ValueError(f"{what}.identity must be non-empty canonical text")
    elif isinstance(identity, Mapping):
        identity_data = _json_object(identity, f"{what}.identity")
        if not identity_data:
            raise ValueError(f"{what}.identity must not be empty")
    else:
        raise ValueError(f"{what}.identity must be a non-empty string or object")
    if not isinstance(data["value"], Mapping):
        raise ValueError(f"{what}.value must be a JSON object")
    _json_object(data["value"], f"{what}.value")


def _validate_plan_evidence(evidence: PlanAdmissionEvidence) -> None:
    """Re-parse a stored non-runnable AST instead of trusting its hash alone."""
    try:
        plain_data = _plain(evidence.data)
        payload = _object(
            plain_data,
            frozenset({"schema_version", "root", "limits", "nodes", "runnable"}),
            "plan data",
        )
        if payload["runnable"] is not False:
            raise ValueError("stored typed plans must be non-runnable")
        limits_raw = _object(
            payload["limits"],
            frozenset(
                {"max_depth", "max_nodes", "max_json_bytes", "max_parameters_per_node"}
            ),
            "plan limits",
        )
        limits = PlanLimits(
            max_depth=limits_raw["max_depth"],
            max_nodes=limits_raw["max_nodes"],
            max_json_bytes=limits_raw["max_json_bytes"],
            max_parameters_per_node=limits_raw["max_parameters_per_node"],
        )
        if not isinstance(payload["nodes"], list):
            raise ValueError("plan nodes must be an array")
        nodes: list[dict[str, Any]] = []
        for index, raw_node in enumerate(payload["nodes"]):
            node = _object(
                raw_node,
                frozenset({"id", "operator", "inputs", "parameters", "output_type", "runnable"}),
                f"plan.nodes[{index}]",
            )
            if node["runnable"] is not False:
                raise ValueError(f"plan.nodes[{index}] must be non-runnable")
            nodes.append(
                {
                    "id": node["id"],
                    "operator": node["operator"],
                    "inputs": node["inputs"],
                    "parameters": node["parameters"],
                }
            )
        source = {
            "schema_version": payload["schema_version"],
            "root": payload["root"],
            "nodes": nodes,
        }
        parsed = parse_plan_json(canonical_json(source), limits=limits)
        if canonical_json(parsed.payload()) != canonical_json(plain_data):
            raise ValueError("typed plan did not round-trip to its canonical payload")
    except (KeyError, TypeError, ValueError, PlanRejected) as exc:
        raise PlanAdmissionCorrupted(f"stored typed plan is invalid: {exc}") from exc


@dataclass(frozen=True, slots=True)
class RoundStartedIdentity:
    """Minimal hash-bound reference to the persisted worker round-start entry."""

    loop_id: str
    round_index: int
    started_entry_seq: int
    started_entry_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.loop_id, str) or not self.loop_id or self.loop_id != self.loop_id.strip():
            raise ValueError("loop_id must be non-empty text without surrounding whitespace")
        _positive_int(self.round_index, "round_index", zero_ok=True)
        _positive_int(self.started_entry_seq, "started_entry_seq")
        _hash(self.started_entry_hash, "started_entry_hash")

    def payload(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "round_index": self.round_index,
            "started_entry_seq": self.started_entry_seq,
            "started_entry_hash": self.started_entry_hash,
        }


def _parse_round(value: object) -> RoundStartedIdentity:
    raw = _object(value, _ROUND_KEYS, "round identity")
    try:
        return RoundStartedIdentity(
            raw["loop_id"],
            raw["round_index"],
            raw["started_entry_seq"],
            raw["started_entry_hash"],
        )
    except (TypeError, ValueError) as exc:
        raise PlanAdmissionCorrupted(f"invalid round identity: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PreparedAdmission:
    seq: int
    entry_hash: str
    transaction_id: str
    round: RoundStartedIdentity
    plan: PlanAdmissionEvidence
    compiler: PlanAdmissionEvidence
    operators: tuple[PlanAdmissionEvidence, ...]
    providers: tuple[PlanAdmissionEvidence, ...]
    inputs: tuple[PlanAdmissionEvidence, ...]
    outputs: tuple[PlanAdmissionEvidence, ...]
    experiment_specs: tuple[PlanAdmissionEvidence, ...]
    hypotheses: tuple[Hypothesis, ...]
    ledger_baseline_seq: int
    ledger_baseline_hash: str
    ledger_batch_payload_hash: str
    manifest_hash: str

    def batch_payload(self) -> dict[str, Any]:
        return {"hypotheses": [item.model_dump(mode="json") for item in self.hypotheses]}


@dataclass(frozen=True, slots=True)
class CommittedAdmission:
    prepare: PreparedAdmission
    seq: int
    entry_hash: str
    ledger_event_seq: int
    ledger_event_prev_hash: str
    ledger_event_hash: str


class PlanAdmissionJournal:
    """Strict append-only PREPARE / COMMIT reducer for ADR-0073.

    One uncommitted PREPARE may exist at a time. This class validates only its own journal; the
    composition root must compare ``CommittedAdmission`` against the TrialLedger event and perform
    the durable loop checkpoint / anchor transaction under the state lock.
    """

    def __init__(
        self, path: Path, *, loop_id: str, create: bool = False, state_version: int = 4
    ) -> None:
        if not isinstance(loop_id, str) or not loop_id or loop_id != loop_id.strip():
            raise PlanAdmissionError("loop_id must be non-empty text without surrounding whitespace")
        if type(state_version) is not int or state_version not in {4, 5}:
            raise PlanAdmissionError("plan admission state_version must be 4 or 5")
        self._loop_id = loop_id
        self._journal = AppendOnlyJournal(Path(path))
        self._prepared: list[PreparedAdmission] = []
        self._committed: list[CommittedAdmission] = []
        self._pending: PreparedAdmission | None = None
        self._transaction_ids: set[str] = set()
        entries = self._journal.entries
        if not entries:
            if not create:
                raise PlanAdmissionCorrupted(
                    f"the v{state_version} plan admission journal is missing its header"
                )
            self._journal.append(
                _HEADER_EVENT,
                {
                    "schema_version": PLAN_ADMISSION_FORMAT_VERSION,
                    "loop_id": loop_id,
                    "state_version": state_version,
                },
            )
        else:
            header = entries[0]
            if (
                header.seq != 1
                or header.type != _HEADER_EVENT
                or set(header.payload) != _HEADER_KEYS
                # exact JSON integer: ``4.0`` compares equal to ``4`` but is another format
                or type(header.payload["state_version"]) is not int
                or header.payload
                != {
                    "schema_version": PLAN_ADMISSION_FORMAT_VERSION,
                    "loop_id": loop_id,
                    "state_version": state_version,
                }
            ):
                raise PlanAdmissionCorrupted(
                    f"the plan admission journal header differs from v{state_version} state"
                )
        for entry in self._journal.entries[1:]:
            try:
                self._replay(entry)
            except (KeyError, TypeError, ValueError) as exc:
                raise PlanAdmissionCorrupted(
                    f"{self._journal.path}:{entry.seq} is an invalid plan admission event: {exc}"
                ) from exc

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        """Verified reducer journal entries, exposed without its append interface."""
        return self._journal.entries

    @property
    def head_hash(self) -> str:
        """Verified journal tip for composition-root checkpoint and anchor cross-checks."""
        return self._journal.head_hash

    @property
    def prepared(self) -> tuple[PreparedAdmission, ...]:
        return tuple(self._prepared)

    @property
    def committed(self) -> tuple[CommittedAdmission, ...]:
        return tuple(self._committed)

    @property
    def pending(self) -> PreparedAdmission | None:
        return self._pending

    def prepare(
        self,
        *,
        round: RoundStartedIdentity,
        plan: TypedPlan,
        compiler: PlanAdmissionEvidence,
        operators: Sequence[PlanAdmissionEvidence],
        providers: Sequence[PlanAdmissionEvidence],
        inputs: Sequence[PlanAdmissionEvidence],
        outputs: Sequence[PlanAdmissionEvidence],
        experiment_specs: Sequence[PlanAdmissionEvidence],
        hypotheses: Sequence[Hypothesis],
        ledger_baseline_seq: int,
        ledger_baseline_hash: str,
    ) -> PreparedAdmission:
        """Append a fully hash-bound prepare; execution is never performed here."""
        if self._pending is not None:
            raise PlanAdmissionError("a PREPARE is already pending; commit it before another")
        if not isinstance(round, RoundStartedIdentity):
            raise PlanAdmissionError("round must be a validated RoundStartedIdentity")
        if round.loop_id != self._loop_id:
            raise PlanAdmissionError("round loop_id differs from the plan admission journal header")
        if not isinstance(plan, TypedPlan) or plan.runnable:
            raise PlanAdmissionError("plan must be a non-runnable TypedPlan value")
        if not isinstance(compiler, PlanAdmissionEvidence):
            raise PlanAdmissionError("compiler evidence is required")
        _validate_artifact_evidence(compiler, "compiler")
        if not isinstance(operators, Sequence) or not operators:
            raise PlanAdmissionError("at least one operator implementation identity is required")
        if not isinstance(providers, Sequence) or not providers:
            raise PlanAdmissionError("at least one provider identity is required")
        if not isinstance(inputs, Sequence) or not isinstance(outputs, Sequence):
            raise PlanAdmissionError("inputs and outputs must be ordered evidence sequences")
        if not outputs:
            raise PlanAdmissionError("at least one lowered output is required")
        if not isinstance(experiment_specs, Sequence) or not experiment_specs:
            raise PlanAdmissionError("at least one ExperimentSpec evidence item is required")
        if not isinstance(hypotheses, Sequence) or not hypotheses:
            raise PlanAdmissionError("at least one Hypothesis is required")
        if any(not isinstance(item, PlanAdmissionEvidence) for item in (*operators, *providers, *inputs, *outputs, *experiment_specs)):
            raise PlanAdmissionError("all artifact evidence items must be PlanAdmissionEvidence")
        for category, values in (
            ("operators", operators),
            ("providers", providers),
            ("inputs", inputs),
            ("outputs", outputs),
            ("experiment_specs", experiment_specs),
        ):
            for index, evidence in enumerate(values):
                _validate_artifact_evidence(evidence, f"{category}[{index}]")
        _positive_int(ledger_baseline_seq, "ledger_baseline_seq", zero_ok=True)
        _hash(ledger_baseline_hash, "ledger_baseline_hash")

        hypothesis_values = tuple(hypotheses)
        if any(not isinstance(item, Hypothesis) for item in hypothesis_values):
            raise PlanAdmissionError("hypotheses must contain only Hypothesis values")
        if any(item.origin is HypothesisOrigin.LLM for item in hypothesis_values):
            raise PlanAdmissionError("unreviewed LLM hypotheses are not admitted")
        identities = [(item.name, item.version) for item in hypothesis_values]
        if len(set(identities)) != len(identities):
            raise PlanAdmissionError("the prepared batch repeats a Hypothesis identity")
        hypothesis_values = tuple(sorted(hypothesis_values, key=_hypothesis_sort_key))

        plan_evidence = PlanAdmissionEvidence.from_data(plan.payload())
        hypothesis_payloads = [item.model_dump(mode="json") for item in hypothesis_values]
        batch_payload = {"hypotheses": hypothesis_payloads}
        baseline = {"seq": ledger_baseline_seq, "hash": ledger_baseline_hash}
        base: dict[str, Any] = {
            "schema_version": PLAN_ADMISSION_FORMAT_VERSION,
            "round": round.payload(),
            "plan": plan_evidence.payload(),
            "compiler": compiler.payload(),
            "operators": [item.payload() for item in operators],
            "providers": [item.payload() for item in providers],
            "inputs": [item.payload() for item in inputs],
            "outputs": [item.payload() for item in outputs],
            "experiment_specs": [item.payload() for item in experiment_specs],
            "hypotheses": [
                {"hypothesis": payload, "content_hash": hypothesis.content_hash()}
                for hypothesis, payload in zip(hypothesis_values, hypothesis_payloads, strict=True)
            ],
            "ledger_baseline": baseline,
            "ledger_batch_payload_hash": content_hash(batch_payload),
        }
        manifest_hash = content_hash(base)
        transaction_id = content_hash(
            {"schema_version": PLAN_ADMISSION_FORMAT_VERSION, "manifest_hash": manifest_hash}
        )
        if transaction_id in self._transaction_ids:
            raise PlanAdmissionError("this exact admission transaction was already prepared")
        payload = {
            **base,
            "transaction_id": transaction_id,
            "manifest_hash": manifest_hash,
        }
        # Validate the exact on-disk schema before mutating the journal.
        parsed = _parse_prepare(payload, seq=len(self._journal.entries) + 1, entry_hash="0" * 64)
        entry = self._journal.append(_PREPARE_EVENT, payload)
        prepared = PreparedAdmission(
            seq=entry.seq,
            entry_hash=entry.hash,
            transaction_id=parsed.transaction_id,
            round=parsed.round,
            plan=parsed.plan,
            compiler=parsed.compiler,
            operators=parsed.operators,
            providers=parsed.providers,
            inputs=parsed.inputs,
            outputs=parsed.outputs,
            experiment_specs=parsed.experiment_specs,
            hypotheses=parsed.hypotheses,
            ledger_baseline_seq=parsed.ledger_baseline_seq,
            ledger_baseline_hash=parsed.ledger_baseline_hash,
            ledger_batch_payload_hash=parsed.ledger_batch_payload_hash,
            manifest_hash=parsed.manifest_hash,
        )
        self._prepared.append(prepared)
        self._pending = prepared
        self._transaction_ids.add(transaction_id)
        return prepared

    def commit(self, transaction_id: str, ledger_event: JournalEntry) -> CommittedAdmission:
        """Append COMMIT only for the exact single batch event named by pending PREPARE."""
        pending = self._pending
        if pending is None:
            raise PlanAdmissionError("there is no pending PREPARE to commit")
        if transaction_id != pending.transaction_id:
            raise PlanAdmissionError("COMMIT transaction_id differs from the pending PREPARE")
        if not isinstance(ledger_event, JournalEntry):
            raise PlanAdmissionError("ledger_event must be a verified JournalEntry")
        event = _ledger_event_payload(pending, ledger_event)
        payload = {
            "schema_version": PLAN_ADMISSION_FORMAT_VERSION,
            "transaction_id": pending.transaction_id,
            "prepare_seq": pending.seq,
            "prepare_hash": pending.entry_hash,
            "ledger_event": event,
        }
        entry = self._journal.append(_COMMIT_EVENT, payload)
        committed = CommittedAdmission(
            prepare=pending,
            seq=entry.seq,
            entry_hash=entry.hash,
            ledger_event_seq=ledger_event.seq,
            ledger_event_prev_hash=ledger_event.prev_hash,
            ledger_event_hash=ledger_event.hash,
        )
        self._committed.append(committed)
        self._pending = None
        return committed

    def _replay(self, entry: JournalEntry) -> None:
        if entry.type == _PREPARE_EVENT:
            if self._pending is not None:
                raise PlanAdmissionCorrupted("a second PREPARE appeared before COMMIT")
            prepared = _parse_prepare(entry.payload, seq=entry.seq, entry_hash=entry.hash)
            if prepared.round.loop_id != self._loop_id:
                raise PlanAdmissionCorrupted("PREPARE loop_id differs from the journal header")
            if prepared.transaction_id in self._transaction_ids:
                raise PlanAdmissionCorrupted("transaction_id is repeated")
            self._prepared.append(prepared)
            self._pending = prepared
            self._transaction_ids.add(prepared.transaction_id)
            return
        if entry.type == _COMMIT_EVENT:
            if self._pending is None:
                raise PlanAdmissionCorrupted("COMMIT has no pending PREPARE")
            committed = _parse_commit(entry.payload, entry, self._pending)
            self._committed.append(committed)
            self._pending = None
            return
        raise PlanAdmissionCorrupted(f"unknown plan admission event type {entry.type!r}")


def _parse_prepare(payload: object, *, seq: int, entry_hash: str) -> PreparedAdmission:
    raw = _object(payload, _PREPARE_KEYS, "PREPARE payload")
    if raw["schema_version"] != PLAN_ADMISSION_FORMAT_VERSION:
        raise PlanAdmissionCorrupted(
            f"unsupported plan admission schema_version {raw['schema_version']!r}"
        )
    transaction_id = _hash(raw["transaction_id"], "transaction_id")
    manifest_hash = _hash(raw["manifest_hash"], "manifest_hash")
    round_identity = _parse_round(raw["round"])
    plan = _parse_evidence(raw["plan"], "plan")
    _validate_plan_evidence(plan)
    compiler = _parse_evidence(raw["compiler"], "compiler")
    _validate_artifact_evidence(compiler, "compiler")
    operators = _parse_evidence_list(raw["operators"], "operators")
    providers = _parse_evidence_list(raw["providers"], "providers")
    inputs = _parse_evidence_list(raw["inputs"], "inputs")
    outputs = _parse_evidence_list(raw["outputs"], "outputs")
    experiment_specs = _parse_evidence_list(raw["experiment_specs"], "experiment_specs")
    for category, values in (
        ("operators", operators),
        ("providers", providers),
        ("inputs", inputs),
        ("outputs", outputs),
        ("experiment_specs", experiment_specs),
    ):
        for index, evidence in enumerate(values):
            _validate_artifact_evidence(evidence, f"{category}[{index}]")
    if not operators or not providers or not outputs or not experiment_specs:
        raise PlanAdmissionCorrupted("PREPARE requires compiler, operators, providers, outputs, and ExperimentSpecs")
    raw_hypotheses = raw["hypotheses"]
    if not isinstance(raw_hypotheses, list) or not raw_hypotheses:
        raise PlanAdmissionCorrupted("PREPARE hypotheses must be a non-empty list")
    hypotheses: list[Hypothesis] = []
    for index, item in enumerate(raw_hypotheses):
        pair = _object(item, frozenset({"hypothesis", "content_hash"}), f"hypotheses[{index}]")
        hypothesis_raw = pair["hypothesis"]
        if not isinstance(hypothesis_raw, dict):
            raise PlanAdmissionCorrupted(f"hypotheses[{index}].hypothesis must be an object")
        hypothesis = Hypothesis.model_validate(hypothesis_raw)
        if hypothesis.model_dump(mode="json") != hypothesis_raw:
            raise PlanAdmissionCorrupted(f"hypotheses[{index}] is not a canonical Hypothesis payload")
        if hypothesis.origin is HypothesisOrigin.LLM:
            raise PlanAdmissionCorrupted("PREPARE contains an LLM-originated Hypothesis")
        if _hash(pair["content_hash"], f"hypotheses[{index}].content_hash") != hypothesis.content_hash():
            raise PlanAdmissionCorrupted(f"hypotheses[{index}] content_hash does not match")
        hypotheses.append(hypothesis)
    identities = [(item.name, item.version) for item in hypotheses]
    if len(set(identities)) != len(identities):
        raise PlanAdmissionCorrupted("PREPARE repeats a Hypothesis identity")
    if hypotheses != sorted(hypotheses, key=_hypothesis_sort_key):
        raise PlanAdmissionCorrupted("PREPARE hypotheses do not use the canonical batch order")
    baseline = _object(raw["ledger_baseline"], _POSITION_KEYS, "ledger_baseline")
    baseline_seq = _positive_int(baseline["seq"], "ledger_baseline.seq", zero_ok=True)
    baseline_hash = _hash(baseline["hash"], "ledger_baseline.hash")
    batch_payload = {"hypotheses": [item.model_dump(mode="json") for item in hypotheses]}
    batch_hash = _hash(raw["ledger_batch_payload_hash"], "ledger_batch_payload_hash")
    if content_hash(batch_payload) != batch_hash:
        raise PlanAdmissionCorrupted("ledger_batch_payload_hash does not match the Hypothesis batch")

    base = {
        "schema_version": PLAN_ADMISSION_FORMAT_VERSION,
        "round": round_identity.payload(),
        "plan": plan.payload(),
        "compiler": compiler.payload(),
        "operators": [item.payload() for item in operators],
        "providers": [item.payload() for item in providers],
        "inputs": [item.payload() for item in inputs],
        "outputs": [item.payload() for item in outputs],
        "experiment_specs": [item.payload() for item in experiment_specs],
        "hypotheses": [
            {"hypothesis": item.model_dump(mode="json"), "content_hash": item.content_hash()}
            for item in hypotheses
        ],
        "ledger_baseline": {"seq": baseline_seq, "hash": baseline_hash},
        "ledger_batch_payload_hash": batch_hash,
    }
    if content_hash(base) != manifest_hash:
        raise PlanAdmissionCorrupted("manifest_hash does not match PREPARE contents")
    expected_transaction = content_hash(
        {"schema_version": PLAN_ADMISSION_FORMAT_VERSION, "manifest_hash": manifest_hash}
    )
    if transaction_id != expected_transaction:
        raise PlanAdmissionCorrupted("transaction_id does not match manifest_hash")
    if type(seq) is not int or seq < 1:
        raise PlanAdmissionCorrupted("PREPARE journal sequence must be positive")
    return PreparedAdmission(
        seq=seq,
        entry_hash=entry_hash,
        transaction_id=transaction_id,
        round=round_identity,
        plan=plan,
        compiler=compiler,
        operators=operators,
        providers=providers,
        inputs=inputs,
        outputs=outputs,
        experiment_specs=experiment_specs,
        hypotheses=tuple(hypotheses),
        ledger_baseline_seq=baseline_seq,
        ledger_baseline_hash=baseline_hash,
        ledger_batch_payload_hash=batch_hash,
        manifest_hash=manifest_hash,
    )


def _parse_evidence_list(value: object, what: str) -> tuple[PlanAdmissionEvidence, ...]:
    if type(value) is not list:
        raise PlanAdmissionCorrupted(f"{what} must be an ordered JSON array")
    return tuple(_parse_evidence(item, f"{what}[{index}]") for index, item in enumerate(value))


def _ledger_event_payload(
    prepared: PreparedAdmission, event: JournalEntry
) -> dict[str, Any]:
    _positive_int(event.seq, "TrialLedger event seq")
    if not isinstance(event.type, str) or not event.type:
        raise PlanAdmissionError("TrialLedger event type must be non-empty text")
    _hash(event.prev_hash, "TrialLedger event prev_hash")
    _hash(event.hash, "TrialLedger event hash")
    if not isinstance(event.payload, Mapping):
        raise PlanAdmissionError("TrialLedger batch event payload must be an object")
    expected_payload = prepared.batch_payload()
    if (
        event.type != _BATCH_EVENT
        or event.seq != prepared.ledger_baseline_seq + 1
        or event.prev_hash != prepared.ledger_baseline_hash
        or dict(event.payload) != expected_payload
        or content_hash(dict(event.payload)) != prepared.ledger_batch_payload_hash
        or event.hash
        != _entry_hash(event.seq, event.type, dict(event.payload), event.prev_hash)
    ):
        raise PlanAdmissionError("TrialLedger event does not exactly match the pending PREPARE")
    return {
        "type": event.type,
        "seq": event.seq,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
        "payload_hash": prepared.ledger_batch_payload_hash,
    }


def _parse_commit(
    payload: object, entry: JournalEntry, prepared: PreparedAdmission
) -> CommittedAdmission:
    raw = _object(payload, _COMMIT_KEYS, "COMMIT payload")
    if raw["schema_version"] != PLAN_ADMISSION_FORMAT_VERSION:
        raise PlanAdmissionCorrupted(
            f"unsupported plan admission schema_version {raw['schema_version']!r}"
        )
    if raw["transaction_id"] != prepared.transaction_id:
        raise PlanAdmissionCorrupted("COMMIT transaction_id differs from PREPARE")
    prepare_seq = _positive_int(raw["prepare_seq"], "COMMIT.prepare_seq")
    prepare_hash = _hash(raw["prepare_hash"], "COMMIT.prepare_hash")
    if prepare_seq != prepared.seq or prepare_hash != prepared.entry_hash:
        raise PlanAdmissionCorrupted("COMMIT does not reference the exact PREPARE journal entry")
    if entry.seq != prepared.seq + 1:
        raise PlanAdmissionCorrupted("COMMIT does not immediately follow its PREPARE")
    ledger = _object(raw["ledger_event"], _LEDGER_EVENT_KEYS, "COMMIT ledger_event")
    ledger_seq = _positive_int(ledger["seq"], "COMMIT.ledger_event.seq")
    ledger_previous_hash = _hash(ledger["prev_hash"], "COMMIT.ledger_event.prev_hash")
    ledger_hash = _hash(ledger["hash"], "COMMIT.ledger_event.hash")
    ledger_payload_hash = _hash(ledger["payload_hash"], "COMMIT.ledger_event.payload_hash")
    expected_seq = prepared.ledger_baseline_seq + 1
    expected_payload = prepared.batch_payload()
    if (
        ledger["type"] != _BATCH_EVENT
        or ledger_seq != expected_seq
        or ledger_previous_hash != prepared.ledger_baseline_hash
        or ledger_payload_hash != prepared.ledger_batch_payload_hash
        or ledger_hash
        != _entry_hash(expected_seq, _BATCH_EVENT, expected_payload, prepared.ledger_baseline_hash)
    ):
        raise PlanAdmissionCorrupted("COMMIT ledger event identity differs from PREPARE")
    return CommittedAdmission(
        prepare=prepared,
        seq=entry.seq,
        entry_hash=entry.hash,
        ledger_event_seq=expected_seq,
        ledger_event_prev_hash=prepared.ledger_baseline_hash,
        ledger_event_hash=ledger_hash,
    )
