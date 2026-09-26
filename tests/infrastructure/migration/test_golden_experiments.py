"""Phase 14 golden experiment replay: a committed, frozen G0 – G4 validation + backtest run is
rerun and compared bit-exactly (10-migration.md §2; roadmap P14 "golden experiments reproduce").

The record lives in ``tests/golden/experiments/`` (content-addressed, write-once) and is produced
by ``tests.golden.experiments.tsmom_g0_g4`` (TEST ONLY synthetic run; see its docstring). A
migration ADR that changes an engine must either reproduce it at its declared tolerance or
deliberately regenerate it (new hash here, with the ADR's reason). No production code is changed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from infrastructure.migration import compare_golden, load_golden
from infrastructure.migration.golden import GoldenRecord
from infrastructure.migration.rollback import RollbackVerdict, rollback_evidence
from tests.golden.experiments import tsmom_g0_g4 as experiment

REPO = Path(__file__).resolve().parents[3]
#: The committed record (``<hash>.json`` in ``tests/golden/experiments/``). Changing it is a
#: deliberate regeneration, never a way to make this test pass (CLAUDE.md H3 / H4).
GOLDEN_HASH = "be2430e633d1d3eb2b1273209681f32f673b5d749eb320e6c1a172ad4602ec23"
ZERO = Decimal(0)


@pytest.fixture(scope="module")
def golden() -> GoldenRecord:
    return load_golden(experiment.DIRECTORY, GOLDEN_HASH)


@pytest.fixture(scope="module")
def rerun() -> dict[str, Decimal]:
    return experiment.run()


def test_the_committed_record_is_the_one_golden_experiment_of_its_directory(
    golden: GoldenRecord,
) -> None:
    assert golden.name == experiment.NAME
    assert sorted(p.name for p in experiment.DIRECTORY.glob("*.json")) == [f"{GOLDEN_HASH}.json"]
    # it really covers a backtest and every stage of G0 - G4
    stages = {key.split(".")[1] for key in golden.outputs if key.startswith("gate.")}
    assert stages == {"G0", "G1", "G2", "G3", "G4"}
    assert {"backtest.final_equity", "backtest.result_hash", "report.verdict"} <= set(
        golden.outputs
    )


def test_a_rerun_reproduces_the_golden_experiment_bit_exactly(
    golden: GoldenRecord, rerun: dict[str, Decimal]
) -> None:
    diff = compare_golden(golden, lambda: rerun, ZERO)
    assert diff.passed and diff.bit_identical and diff.differences == {}
    assert diff.rerun_hash == diff.golden_hash == golden.outputs_hash
    assert "verdict: PASS" in diff.report() and "differences: 0" in diff.report()
    # a rollback rerun that reproduces it is RESTORED evidence (rollback.py)
    evidence = rollback_evidence("golden-self-check", golden, diff, diff)
    assert evidence.verdict is RollbackVerdict.RESTORED


def test_a_perturbed_pipeline_rerun_is_reported_as_a_diff(golden: GoldenRecord) -> None:
    """Another market seed through the same pipeline: the diff names the changed outputs with
    their golden and rerun values, and the rollback evidence says NOT_RESTORED."""
    perturbed = experiment.outputs(experiment.evaluate(seed=experiment.SEED + 1))
    diff = compare_golden(golden, lambda: perturbed, ZERO)
    assert not diff.passed and not diff.bit_identical
    assert diff.golden_hash == golden.outputs_hash and diff.rerun_hash != golden.outputs_hash
    for key in ("backtest.final_equity", "backtest.result_hash"):
        assert diff.differences[key] == (golden.outputs[key], perturbed[key])
        assert golden.outputs[key] != perturbed[key]
    assert all(
        diff.differences[key] == (golden.outputs.get(key), perturbed.get(key))
        for key in diff.differences
    )
    report = diff.report()
    assert "verdict: FAIL" in report and "bit_identical: no" in report
    old, new = diff.differences["backtest.final_equity"]
    assert old is not None and new is not None
    assert f"  backtest.final_equity: golden={old} rerun={new} delta={new - old}" in report
    evidence = rollback_evidence("golden-self-check", golden, diff, diff)
    assert evidence.verdict is RollbackVerdict.NOT_RESTORED
    assert "backtest.final_equity" in evidence.residual_differences


def test_the_smallest_perturbations_are_reported_at_zero_tolerance(
    golden: GoldenRecord, rerun: dict[str, Decimal]
) -> None:
    """One output off by 1e-18, one missing and one extra: exactly those three, with the right
    (golden, rerun) pairs; the first one passes only under a tolerance that covers it."""
    nudged = dict(rerun)
    nudged["backtest.total_fees"] += Decimal("1e-18")
    del nudged["report.gates"]
    nudged["report.extra"] = Decimal(1)
    diff = compare_golden(golden, lambda: nudged, ZERO)
    assert diff.differences == {
        "backtest.total_fees": (
            golden.outputs["backtest.total_fees"],
            nudged["backtest.total_fees"],
        ),
        "report.gates": (golden.outputs["report.gates"], None),
        "report.extra": (None, Decimal(1)),
    }
    assert "  report.gates: golden=" in diff.report() and "rerun=<missing>" in diff.report()
    only_nudged = {**rerun, "backtest.total_fees": nudged["backtest.total_fees"]}
    tolerant = compare_golden(golden, lambda: only_nudged, Decimal("1e-18"))
    assert tolerant.passed and not tolerant.bit_identical


def test_the_committed_file_is_what_the_regeneration_helper_produces(tmp_path: Path) -> None:
    """The helper, run as ``python -m`` in a fresh process with another hash seed, writes a file
    byte-identical to the committed one (and names its hash)."""
    env = {**os.environ, "PYTHONHASHSEED": "4242"}
    done = subprocess.run(
        [sys.executable, "-m", "tests.golden.experiments.tsmom_g0_g4", "--out", str(tmp_path)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[0] == GOLDEN_HASH
    produced = tmp_path / f"{GOLDEN_HASH}.json"
    assert produced.read_bytes() == (experiment.DIRECTORY / f"{GOLDEN_HASH}.json").read_bytes()
    assert experiment.main(["--bogus"]) == 2
