"""ADR-0105 §3: the explicit baseline-set exporter (`research.operations.baseline_export`).

Reports, runs and numbers are TEST ONLY fabrications (`tests/research/operations/fixtures.py`).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.domain.base import canonical_json
from core.domain.research import GateResult, Verdict
from research.operations.baseline_export import (
    BaselineExportRefused,
    export_baseline_set,
    main,
    parse_gate_mapping,
)
from research.operations.degradation import BaselineMetricSet
from tests.factories import experiment_run, strategy_ref, validation_profile, validation_report
from tests.research.operations.fixtures import (
    GATE_ID,
    GATE_LABEL,
    METRIC,
    read_json,
    toy_gate,
    toy_report_and_run,
    write_json,
)

PROFILE = validation_profile()
SUBJECT = strategy_ref()
WALK_FORWARD = ("positive_window_fraction", "G4.walk_forward.positive_fraction")


def test_the_exported_set_is_exactly_the_named_gates_exact_values() -> None:
    walk = toy_gate("0.75", gate_id=WALK_FORWARD[1], label="positive_window_fraction[>=]")
    report, run = toy_report_and_run(PROFILE, SUBJECT, gates=(toy_gate("3.5"), walk))
    baseline = export_baseline_set(report, run, {METRIC: GATE_ID, WALK_FORWARD[0]: WALK_FORWARD[1]})
    assert baseline.payload() == {
        "validation_report_hash": report.content_hash(),
        "metrics": {METRIC: "3.5", "positive_window_fraction": "0.75"},
        "gate_ids": {METRIC: GATE_ID, WALK_FORWARD[0]: WALK_FORWARD[1]},
    }
    # nothing outside the mapping is exported, even though the report has more gates
    only = export_baseline_set(report, run, {METRIC: GATE_ID})
    assert set(dict(only.metrics)) == {METRIC}


def test_the_cli_writes_the_canonical_baseline_set_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report, run = toy_report_and_run(PROFILE, SUBJECT)
    report_path = write_json(tmp_path / "report.json", report.model_dump(mode="json"))
    run_path = write_json(tmp_path / "run.json", run.model_dump(mode="json"))
    output = tmp_path / "baseline_set.json"
    argv = [
        "--report", str(report_path),
        "--run", str(run_path),
        "--gate", f"{METRIC}={GATE_ID}",
        "--output", str(output),
    ]  # fmt: skip
    assert main(argv) == 0
    expected = export_baseline_set(report, run, {METRIC: GATE_ID})
    assert output.read_text(encoding="utf-8") == canonical_json(expected.payload()) + "\n"
    assert f"baseline_set_hash={expected.content_hash()}" in capsys.readouterr().out
    written = read_json(output)
    assert (
        BaselineMetricSet(
            validation_report_hash=str(written["validation_report_hash"]),
            metrics={METRIC: Decimal("3.5")},
            gate_ids={METRIC: GATE_ID},
        ).payload()
        == written
    )  # the shape the degradation CLI reads back
    before = output.read_text(encoding="utf-8")
    assert main(argv) == 1  # the output already exists
    assert output.read_text(encoding="utf-8") == before
    assert "already exists" in capsys.readouterr().err


def test_a_report_that_does_not_belong_to_the_run_is_refused() -> None:
    report, run = toy_report_and_run(PROFILE, SUBJECT)
    other_run = experiment_run(run_id="another-run")
    with pytest.raises(BaselineExportRefused, match="run_id"):
        export_baseline_set(report, other_run, {METRIC: GATE_ID})
    foreign = validation_report(
        Verdict.PASS,
        report_id="rep",
        run_id=run.run_id,
        experiment_hash="9" * 64,
        gates=(toy_gate(),),
    )
    with pytest.raises(BaselineExportRefused, match="experiment_hash"):
        export_baseline_set(foreign, run, {METRIC: GATE_ID})


def test_a_report_that_is_not_pass_is_refused() -> None:
    failing = GateResult(
        gate_id=GATE_ID,
        metric=GATE_LABEL,
        value=1.0,
        value_exact=Decimal("1.0"),
        verdict=Verdict.FAIL,
    )
    _, run = toy_report_and_run(PROFILE, SUBJECT)
    report = validation_report(
        Verdict.FAIL,
        report_id="rep-ops-fail",
        run_id=run.run_id,
        subject=SUBJECT,
        experiment_hash=run.experiment_hash,
        validation_profile=PROFILE.ref,
        validation_profile_hash=PROFILE.content_hash(),
        gates=(failing,),
    )
    assert report.verdict is Verdict.FAIL
    with pytest.raises(BaselineExportRefused, match="not PASS"):
        export_baseline_set(report, run, {METRIC: GATE_ID})


@pytest.mark.parametrize(
    ("mapping", "reason"),
    [
        ({"sharpe": GATE_ID}, "not defined"),  # a metric the closed registry does not define
        ({METRIC: "G4.cost_stress.other"}, "not for gate"),  # a defined metric, another gate
        ({METRIC: "G5.cost_stress.breakeven"}, "sealed OOS"),  # always refused
        ({WALK_FORWARD[0]: GATE_ID}, "not for gate"),  # a mapping is never guessed
    ],
)
def test_an_undefined_or_refused_metric_gate_pair_is_refused(
    mapping: dict[str, str], reason: str
) -> None:
    report, run = toy_report_and_run(PROFILE, SUBJECT)
    with pytest.raises(BaselineExportRefused, match=reason):
        export_baseline_set(report, run, mapping)


def test_a_gate_the_report_does_not_have_or_labels_differently_is_refused() -> None:
    other = toy_gate("0.75", gate_id="G4.walk_forward.positive_fraction", label="x[>=]")
    report, run = toy_report_and_run(PROFILE, SUBJECT, gates=(other,))
    with pytest.raises(BaselineExportRefused, match="no single"):
        export_baseline_set(report, run, {METRIC: GATE_ID})  # the gate is absent
    mislabelled = toy_gate("3.5", label="some_other_metric[>=]")
    report, run = toy_report_and_run(PROFILE, SUBJECT, gates=(mislabelled,))
    with pytest.raises(BaselineExportRefused, match="no single"):
        export_baseline_set(report, run, {METRIC: GATE_ID})
    wrong = toy_gate("0.75", gate_id=WALK_FORWARD[1], label="not_the_metric[>=]")
    report, run = toy_report_and_run(PROFILE, SUBJECT, gates=(wrong,))
    with pytest.raises(BaselineExportRefused, match="no single"):
        export_baseline_set(report, run, {WALK_FORWARD[0]: WALK_FORWARD[1]})


def test_a_float_only_gate_is_never_exported() -> None:
    float_only = GateResult(gate_id=GATE_ID, metric=GATE_LABEL, value=3.5, verdict=Verdict.PASS)
    report, run = toy_report_and_run(PROFILE, SUBJECT, gates=(float_only,))
    with pytest.raises(BaselineExportRefused, match="no exact value"):
        export_baseline_set(report, run, {METRIC: GATE_ID})


@pytest.mark.parametrize(
    "entries",
    [
        [],
        ["no-separator"],
        ["=gate"],
        ["metric="],
        [" metric=gate"],
        ["metric=gate "],
        ["a=g1", "a=g2"],  # one metric twice
        ["a=g1", "b=g1"],  # one gate twice
    ],
)
def test_a_missing_malformed_or_repeated_gate_mapping_is_refused(entries: list[str]) -> None:
    with pytest.raises(BaselineExportRefused):
        parse_gate_mapping(entries)


def test_the_cli_refuses_bad_inputs_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report, run = toy_report_and_run(PROFILE, SUBJECT)
    report_path = write_json(tmp_path / "report.json", report.model_dump(mode="json"))
    run_path = write_json(tmp_path / "run.json", run.model_dump(mode="json"))
    output = tmp_path / "out.json"
    duplicate = tmp_path / "dup.json"
    duplicate.write_text(
        json.dumps(report.model_dump(mode="json"))[:-1] + ', "run_id": "again"}', encoding="utf-8"
    )
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    contract_miss = write_json(tmp_path / "not_a_report.json", {"hello": "world"})

    def run_cli(report_file: Path, run_file: Path, *gates: str) -> int:
        flags = [item for gate in gates for item in ("--gate", gate)]
        return main(
            ["--report", str(report_file), "--run", str(run_file), *flags, "--output", str(output)]
        )

    good_gate = f"{METRIC}={GATE_ID}"
    assert run_cli(report_path, run_path) == 1  # no --gate at all
    assert run_cli(duplicate, run_path, good_gate) == 1
    assert run_cli(broken, run_path, good_gate) == 1
    assert run_cli(contract_miss, run_path, good_gate) == 1
    assert run_cli(report_path, contract_miss, good_gate) == 1
    assert run_cli(report_path, run_path, f"sharpe={GATE_ID}") == 1
    assert run_cli(tmp_path / "missing.json", run_path, good_gate) == 3  # I/O failure
    assert not output.exists()
    assert "refused" in capsys.readouterr().err
