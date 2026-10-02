"""Read-only export of durable loop state to files (ADR-0105 §3, second item).

A loop's durable state directory (``research.loop.durable``) records every run the loop made, but
the P11 tools read plain JSON artifacts: ``research.operations.baseline_export`` the baseline
``ExperimentRun`` and its ``ValidationReport``; the default ``AuthorityEnvironment``
(``research.operations.authority_environment``) the ``ExperimentRun``, ``StrategySpec``,
``CostModelSpec`` and ``OutcomeLabelSpec`` (``HLENS_AUTHORITY_BASELINE_RUN`` / ``STRATEGY_SPEC`` /
``COST_MODEL`` / ``LABEL_SPEC``). ``export_run_artifacts`` writes exactly those files for one
``run_id`` the caller names; ``read_durable_state`` is the read it is built on.

**Read only.** Nothing under ``state_dir`` is written, created, locked for writing or repaired: the
memory and audit journals are replayed read-only (``AppendOnlyJournal`` / ``LoopAuditLog`` take a
shared read lock and verify their hash chains), the state's writer lock (``state.lock``) is not
taken, no anchor is published and no admission is recovered. The durable write paths and formats
are unchanged. A state another process is writing is read as of the last complete journal line.

**What counts.** Only rounds the audit recorded: every ``round_memory`` checkpoint must pair with
the audit record of the same ``round_index`` and ``record_hash``, except one trailing checkpoint of
a round that was never recorded (the crash window between checkpoint and audit record; that round
is not exported). Anything else is refused (``StateExportRefused``). Every run, report and strategy
spec must reproduce the content hash its checkpoint row recorded. This is not ``open_state``'s full
cross-check (ledgers, positions, anchors need the writer side); open the state with the operator
or ``open_synthetic_loop`` / ``open_dataset_loop`` for that.

**What the state holds, and what it does not.** The state holds each run, its validation report
and the specs of strategies the loop *evolved* (checkpoint deltas). The configured strategies, the
cost model and the label spec are configuration: the state records only their content hashes (the
header fingerprint's ``strategies`` / ``cost_model`` / ``label_spec``). For those the caller passes
the object (e.g. from the operator configuration's hash-bound artifacts) and it is exported only
when its content hash is exactly the one the state and the run recorded — never inferred, never
looked up elsewhere:

- ``StrategySpec``: the run's ``repro.strategy_ref`` and the checkpoint row's ``strategy`` /
  ``strategy_hash`` (also the run's ``dependency_hashes`` entry); taken from the state when it
  holds that spec, else from ``strategy_spec``;
- ``CostModelSpec``: the run's ``repro.cost_model_ref`` and its ``dependency_hashes`` entry, and
  the header's ``cost_model``;
- ``OutcomeLabelSpec``: the header's ``label_spec``; its outcome ref and ``outcome_spec_hash`` must
  be the run's ``repro.outcome_ref`` and its ``dependency_hashes`` entry (the binding
  ``authority_environment`` checks).

A run without a strategy has no strategy, cost or label export. A run validated more than once is
refused rather than choosing a report; a run without a report exports none. Risk policies are not
exported (the state records only their hash).

**Files.** ``out_dir`` must exist and lie outside ``state_dir``. Each file is created exclusively
(an existing file is refused before anything is written; nothing is overwritten), holds the
model's JSON (``model_dump(mode="json")``, as the checkpoint stores it) and is read back and
checked against its content hash; on a failure the files this call created are removed. No clock,
no network, no registry access.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from pydantic import ValidationError

from apps.worker.loop import LoopAuditLog
from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec
from core.domain.base import Contract
from core.domain.research import ExperimentRun, ValidationReport
from core.domain.specs import StrategySpec
from research.loop.durable import (
    AUDIT_FILE,
    LOOP_STATE_OPENED,
    MEMORY_FILE,
    ROUND_MEMORY,
)
from research.persistence import AppendOnlyJournal

__all__ = [
    "COST_MODEL_FILE",
    "EXPERIMENT_RUN_FILE",
    "LABEL_SPEC_FILE",
    "STRATEGY_SPEC_FILE",
    "VALIDATION_REPORT_FILE",
    "DurableRun",
    "DurableStateView",
    "ExportedArtifacts",
    "StateExportRefused",
    "export_run_artifacts",
    "read_durable_state",
]

EXPERIMENT_RUN_FILE: Final = "experiment_run.json"
VALIDATION_REPORT_FILE: Final = "validation_report.json"
STRATEGY_SPEC_FILE: Final = "strategy_spec.json"
COST_MODEL_FILE: Final = "cost_model.json"
LABEL_SPEC_FILE: Final = "label_spec.json"


class StateExportRefused(ValueError):
    """The state cannot be read as recorded, or an input does not match it; nothing is written."""


@dataclass(frozen=True, slots=True)
class DurableRun:
    """One recorded run: its round, the run, the strategy its checkpoint row names and the
    validation reports recorded for it (in record order)."""

    round_index: int
    run: ExperimentRun
    strategy: str | None
    strategy_hash: str | None
    reports: tuple[ValidationReport, ...]


@dataclass(frozen=True, slots=True)
class DurableStateView:
    """What ``read_durable_state`` read: the header (version, fingerprint), the recorded rounds,
    every recorded run and the strategy specs the state itself holds (by ref)."""

    state_dir: Path
    state_version: int
    fingerprint: Mapping[str, Any]
    rounds: int
    runs: tuple[DurableRun, ...]
    strategies: Mapping[str, StrategySpec]

    def run(self, run_id: str) -> DurableRun:
        """The one recorded run with ``run_id`` (``StateExportRefused`` for none or several)."""
        matches = [item for item in self.runs if item.run.run_id == run_id]
        if not matches:
            raise StateExportRefused(f"no recorded run has run_id {run_id!r}")
        if len(matches) > 1:
            raise StateExportRefused(f"run_id {run_id!r} is recorded more than once")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ExportedArtifacts:
    """The files ``export_run_artifacts`` wrote (``None``: not exported)."""

    run: Path
    report: Path | None
    strategy_spec: Path | None
    cost_model: Path | None
    label_spec: Path | None


# ---- reading --------------------------------------------------------------------------------


def _checked[M: Contract](model: type[M], payload: Any, expected: Any, what: str) -> M:
    try:
        value = model.model_validate(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateExportRefused(f"{what} does not match its contract: {exc}") from exc
    if value.content_hash() != expected:
        raise StateExportRefused(f"{what} does not reproduce its recorded content hash")
    return value


def _rows(delta: Mapping[str, Any], key: str, round_index: int) -> Sequence[Any]:
    rows = delta.get(key)
    if not isinstance(rows, list):
        raise StateExportRefused(f"round {round_index}'s checkpoint has no {key} rows")
    return rows


def read_durable_state(state_dir: Path) -> DurableStateView:
    """Replay ``state_dir``'s memory and audit journals read-only (module docs, **Read only** /
    **What counts**) into every recorded run and every strategy spec the state holds."""
    root = Path(state_dir)
    memory_path, audit_path = root / MEMORY_FILE, root / AUDIT_FILE
    if not root.is_dir() or not memory_path.is_file():
        raise StateExportRefused(f"{root} is not a loop state directory (no {MEMORY_FILE})")
    try:
        entries = AppendOnlyJournal(memory_path).entries
        records = LoopAuditLog(audit_path).records if audit_path.is_file() else ()
    except Exception as exc:  # noqa: BLE001 - any read / verification failure refuses the export
        raise StateExportRefused(f"the state journals cannot be read or verified: {exc}") from exc
    if not entries or entries[0].type != LOOP_STATE_OPENED:
        raise StateExportRefused(f"{memory_path.name} does not start with a loop state header")
    header = entries[0].payload
    version, fingerprint = header.get("state_version"), header.get("fingerprint")
    if type(version) is not int or not isinstance(fingerprint, Mapping):
        raise StateExportRefused("the state header has no state version or fingerprint")

    checkpoints = [entry.payload for entry in entries if entry.type == ROUND_MEMORY]
    if len(checkpoints) > len(records) + 1:
        raise StateExportRefused("the memory journal holds more round checkpoints than rounds")
    for position, record in enumerate(records):
        checkpoint = checkpoints[position] if position < len(checkpoints) else None
        if checkpoint is None or (
            checkpoint.get("round_index"),
            checkpoint.get("record_hash"),
        ) != (record.round_index, record.record_hash):
            raise StateExportRefused(
                f"round {record.round_index}'s audit record has no matching memory checkpoint"
            )
    if len(checkpoints) > len(records) and checkpoints[-1].get("round_index") != len(records):
        raise StateExportRefused("the unrecorded trailing checkpoint is not the next round's")

    try:
        strategies, trials, reports = _replay(checkpoints[: len(records)])
    except (KeyError, TypeError) as exc:
        raise StateExportRefused(f"a round checkpoint row is malformed: {exc!r}") from exc
    runs = tuple(
        DurableRun(
            round_index=index,
            run=run,
            strategy=strategy,
            strategy_hash=strategy_hash,
            reports=tuple(reports.get(position, ())),
        )
        for position, (index, run, strategy, strategy_hash) in enumerate(trials)
    )
    return DurableStateView(
        state_dir=root,
        state_version=version,
        fingerprint=MappingProxyType(dict(fingerprint)),
        rounds=len(records),
        runs=runs,
        strategies=MappingProxyType(strategies),
    )


def _replay(
    checkpoints: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, StrategySpec],
    list[tuple[int, ExperimentRun, str | None, str | None]],
    dict[int, list[ValidationReport]],
]:
    """The recorded rounds' strategy specs, runs and reports (each hash-checked)."""
    strategies: dict[str, StrategySpec] = {}
    trials: list[tuple[int, ExperimentRun, str | None, str | None]] = []
    reports: dict[int, list[ValidationReport]] = {}
    for checkpoint in checkpoints:
        index = checkpoint["round_index"]
        delta = checkpoint.get("delta")
        if not isinstance(delta, Mapping):
            raise StateExportRefused(f"round {index}'s checkpoint has no delta")
        for row in _rows(delta, "strategies", index):
            spec = _checked(StrategySpec, row["spec"], row["spec_hash"], "a recorded strategy")
            strategies[str(spec.ref)] = spec
        for row in _rows(delta, "trials", index):
            run = _checked(ExperimentRun, row["run"], row["run_hash"], "a recorded run")
            trials.append((index, run, row["strategy"], row["strategy_hash"]))
        for row in _rows(delta, "validations", index):
            trial = row["trial"]
            if type(trial) is not int or not 0 <= trial < len(trials):
                raise StateExportRefused(f"round {index} validates an unknown trial")
            report = row["report"]
            if report is not None:
                reports.setdefault(trial, []).append(
                    _checked(ValidationReport, report["record"], report["hash"], "a report")
                )
    return strategies, trials, reports


# ---- binding the configuration objects ------------------------------------------------------


def _strategy_spec(
    view: DurableStateView, recorded: DurableRun, given: StrategySpec | None
) -> StrategySpec:
    run = recorded.run
    ref = run.repro.strategy_ref
    if ref is None or recorded.strategy != str(ref) or recorded.strategy_hash is None:
        raise StateExportRefused("the run's strategy is not the one its checkpoint row names")
    if run.repro.dependency_hashes.get(str(ref)) != recorded.strategy_hash:
        raise StateExportRefused("the run's dependency hash of its strategy is not the recorded")
    held = view.strategies.get(str(ref))
    if held is not None and given is not None and given.content_hash() != held.content_hash():
        raise StateExportRefused(f"the StrategySpec is not the {ref} the state holds")
    spec = held if held is not None else given
    if spec is None:
        raise StateExportRefused(
            f"the state does not hold {ref} (a configured strategy): pass its StrategySpec"
        )
    if str(spec.ref) != str(ref) or spec.content_hash() != recorded.strategy_hash:
        raise StateExportRefused(f"the StrategySpec is not the {ref} the run recorded")
    return spec


def _cost_model(view: DurableStateView, run: ExperimentRun, given: CostModelSpec) -> CostModelSpec:
    digest = given.content_hash()
    if (
        given.ref != run.repro.cost_model_ref
        or run.repro.dependency_hashes.get(str(given.ref)) != digest
        or view.fingerprint.get("cost_model") != digest
    ):
        raise StateExportRefused("the CostModelSpec is not the cost model the state and run bound")
    return given


def _label_spec(
    view: DurableStateView, run: ExperimentRun, given: OutcomeLabelSpec
) -> OutcomeLabelSpec:
    outcome = run.repro.outcome_ref
    if (
        view.fingerprint.get("label_spec") != given.content_hash()
        or outcome is None
        or given.outcome.target_identity() != outcome.target_identity()
        or given.outcome_spec_hash != run.repro.dependency_hashes.get(str(outcome))
    ):
        raise StateExportRefused(
            "the OutcomeLabelSpec is not the label spec the state and run bound"
        )
    return given


# ---- writing --------------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _write_exclusive[M: Contract](path: Path, value: M, created: list[Path]) -> None:
    text = json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
    with path.open("x", encoding="utf-8") as handle:
        created.append(path)  # ours from here on: removed again if anything below fails
        handle.write(text + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    read_back = type(value).model_validate(json.loads(path.read_text(encoding="utf-8")))
    if read_back.content_hash() != value.content_hash():
        raise StateExportRefused(f"{path.name} does not reproduce its content hash when read back")


def export_run_artifacts(
    state_dir: Path,
    run_id: str,
    out_dir: Path,
    *,
    strategy_spec: StrategySpec | None = None,
    cost_model: CostModelSpec | None = None,
    label_spec: OutcomeLabelSpec | None = None,
) -> ExportedArtifacts:
    """Write the recorded run ``run_id`` of ``state_dir`` (and its one validation report, if any)
    to ``out_dir``, with its strategy spec and — when given and bound — the cost model and label
    spec (module docs). ``StateExportRefused`` before anything is written when an input does not
    match the state; ``OSError`` for an I/O failure (created files removed)."""
    view = read_durable_state(state_dir)
    recorded = view.run(run_id)
    run = recorded.run
    if len(recorded.reports) > 1:
        raise StateExportRefused(f"run {run_id!r} has several validation reports: none is chosen")
    report = recorded.reports[0] if recorded.reports else None
    if run.repro.strategy_ref is None:
        if strategy_spec is not None or cost_model is not None or label_spec is not None:
            raise StateExportRefused(f"run {run_id!r} has no strategy, cost or label to export")
        spec = None
    else:
        spec = _strategy_spec(view, recorded, strategy_spec)
    cost = None if cost_model is None else _cost_model(view, run, cost_model)
    label = None if label_spec is None else _label_spec(view, run, label_spec)

    target = Path(out_dir)
    if not target.is_dir():
        raise StateExportRefused(f"out_dir {target} is not an existing directory")
    if _inside(target.resolve(), view.state_dir.resolve()):
        raise StateExportRefused("out_dir must lie outside the state directory")
    planned: list[tuple[Path, Contract]] = [(target / EXPERIMENT_RUN_FILE, run)]
    optional: tuple[tuple[str, Contract | None], ...] = (
        (VALIDATION_REPORT_FILE, report),
        (STRATEGY_SPEC_FILE, spec),
        (COST_MODEL_FILE, cost),
        (LABEL_SPEC_FILE, label),
    )
    planned.extend((target / name, value) for name, value in optional if value is not None)
    existing = [path.name for path, _ in planned if path.exists() or path.is_symlink()]
    if existing:
        raise StateExportRefused(f"out_dir already holds {existing}: nothing is overwritten")
    created: list[Path] = []
    try:
        for path, artifact in planned:
            _write_exclusive(path, artifact, created)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    written = {path.name: path for path in created}
    return ExportedArtifacts(
        run=written[EXPERIMENT_RUN_FILE],
        report=written.get(VALIDATION_REPORT_FILE),
        strategy_spec=written.get(STRATEGY_SPEC_FILE),
        cost_model=written.get(COST_MODEL_FILE),
        label_spec=written.get(LABEL_SPEC_FILE),
    )
