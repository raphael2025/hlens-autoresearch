"""B21 / B27 conditional plans through the dataset composition root, end to end (offline).

``research.loop.dataset_compose.open_dataset_loop`` with ``LoopWiring.conditional`` set to a
``ConditionalPlan(validate_cells=True)``: the round reads its declared, verified manifest pair
(``DatasetIngestStage``), the experiment stage pre-registers every cell of the trial's State ×
Strategy matrix as a trial of the family, and the validation stage runs the in-sample G0 – G3 gates
on every supported cell (``research.loop.trials``, **Conditional hypotheses** / **Per-cell
validation**). Checked: the per-cell registration and outcome are recorded in the round's audit
record, every cell is in the trial ledger, a supported cell's report binds the round's manifests
(``G0.manifest_binding``), and reopening the state directory restores the same ledger.

Data: the Binance-format fixture of ``test_research_loop_real_data`` (real ingestion path, F3
builds) on the SQLite test catalog of ``tmp_path`` (the ``w`` fixture of this directory): no
network, no PostgreSQL. Only round 0 of its declared rounds runs.

!!! TEST ONLY !!!  The plan's ``min_support`` = 1 and the budget below are arbitrary, uncalibrated
numbers chosen only to let every stage run on a few hours of minutes (``min_support`` is an
eligibility threshold, not a Profile number); the Profile and parameters are the TEST ONLY ones
of ``test_research_loop_real_data``. Nothing here is a research result.
"""

from __future__ import annotations

import gc
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.worker import LoopBudget, RoundStatus, StageStatus
from research.loop import ConditionalPlan
from research.loop.dataset_compose import DatasetLoopConfig, open_dataset_loop
from research.loop.trials import (
    CELL_G4_NOT_RUN,
    CELL_G5_NOT_RUN,
    CELL_LIFECYCLE,
    PER_CELL_VALIDATION_RUN,
)
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e import test_research_loop_real_data as base

#: TEST ONLY plan (arbitrary eligibility threshold; see module docs).
PLAN = ConditionalPlan(
    minimum_effect="net mean return above costs (test only)", min_support=1, validate_cells=True
)
#: TEST ONLY budget: room for the cell trials and one validation per supported cell.
BUDGET = LoopBudget(
    max_trials_per_round=30,
    max_trials_total=60,
    max_llm_cost_units=Decimal(1),
    max_compute_seconds=Decimal(5000),
)
CELLS = [*base.TREND.state_space, None]
IN_SAMPLE_STAGES = ("G0.", "G1.", "G2.", "G3.")


def _config(manifests: base.Manifests) -> DatasetLoopConfig:
    config = base._config(manifests.rounds)
    return replace(config, budget=BUDGET, wiring=replace(config.wiring, conditional=PLAN))


def _stage(record: Any, name: str) -> Any:
    return next(stage for stage in record.stages if stage.name == name)


def test_a_conditional_plan_runs_through_the_dataset_root(w: ds.World, tmp_path: Path) -> None:
    base._ingest(w)
    manifests = base._build(w)
    config = _config(manifests)
    state_dir = tmp_path / "state"
    with open_dataset_loop(config, state_dir=state_dir, catalog=base._catalog(w)) as durable:
        [record] = durable.loop.run_unattended(1)
        ledger = durable.memory.ledger
        trial_log = list(ledger.trial_log)
        hypotheses = {str(h.ref): h for h in ledger.hypotheses}
    assert record.status is RoundStatus.COMPLETED, [
        (s.name, s.status.value, s.error) for s in record.stages
    ]
    assert all(s.status is StageStatus.COMPLETED for s in record.stages)
    ingest = _stage(record, "ingest").summary
    interval, point = manifests.pairs[0]
    assert ingest["source"] == "research_dataset"
    assert (ingest["feature_manifest_hash"], ingest["price_manifest_hash"]) == (
        interval.manifest.content_hash(),
        point.manifest.content_hash(),
    )

    # ---- the experiment stage registered every cell of every completed trial ----
    experiments = {
        (row["hypothesis"], row["attempt"]): row
        for row in _stage(record, "experiment").summary["experiments"]
    }
    completed = [row for row in experiments.values() if row["conditional"] is not None]
    assert completed, "at least one trial must complete with a matrix"
    for row in completed:
        conditional = row["conditional"]
        assert conditional["parent"] == row["hypothesis"]
        assert conditional["validation"] == PER_CELL_VALIDATION_RUN
        assert conditional["matrix_hash"] == row["state_strategy_matrix_hash"]
        assert [c["state"] for c in conditional["cells"]] == CELLS
        for cell in conditional["cells"]:
            hypothesis = hypotheses[cell["hypothesis"]]
            assert hypothesis.family_id == base.FAMILY
            assert ledger.trial_index(hypothesis, row["attempt"]) == cell["trial_index"]

    # ---- the validation stage recorded the per-cell outcome ----
    rows = [
        r for r in _stage(record, "validation").summary["reports"] if r.get("conditional_cells")
    ]
    assert len(rows) == len(completed)
    validated = 0
    for row in rows:
        registration = experiments[(row["hypothesis"], row["attempt"])]["conditional"]["cells"]
        block = row["conditional_cells"]
        assert block["family_trial_count"] == row["family_trial_count"]
        assert (block["g4"], block["g5"], block["lifecycle"]) == (
            CELL_G4_NOT_RUN,
            CELL_G5_NOT_RUN,
            CELL_LIFECYCLE,
        )
        assert [c["state"] for c in block["cells"]] == CELLS
        for cell, registered in zip(block["cells"], registration, strict=True):
            assert cell["hypothesis"] == registered["hypothesis"]
            assert cell["trial_index"] == registered["trial_index"]
            if not registered["supported"]:
                assert cell["status"] == "unsupported" and cell["verdict"] is None
                continue
            validated += 1
            assert cell["status"] == "validated", cell
            gates = {g["gate_id"]: g["verdict"] for g in cell["gates"]}
            assert gates and all(gate.startswith(IN_SAMPLE_STAGES) for gate in gates)
            assert cell["verdict"] in {"PASS", "FAIL", "INCONCLUSIVE"}  # never asserted to PASS
            if cell["traded_decisions"]:  # the cell's evidence binds the round's manifests
                assert gates["G0.manifest_binding"] == "PASS"
    assert validated >= 1

    # ---- the durable state restores the same registrations ----
    gc.collect()
    with open_dataset_loop(config, state_dir=state_dir, catalog=base._catalog(w)) as reopened:
        assert list(reopened.memory.ledger.trial_log) == trial_log
        assert [r.record_hash for r in reopened.loop.audit.records] == [record.record_hash]
