"""Explicit baseline-set exporter for the P11 degradation operation (ADR-0105 §3, ADR-0098 §3).

Turns one caller-chosen ``ValidationReport`` (and the ``ExperimentRun`` it validates) plus an
explicit ``metric=gate_id`` mapping into the ``BaselineMetricSet`` JSON that
``research.operations.degradation_cli --baseline-set`` reads. Nothing is inferred:

- the mapping is the repeated ``--gate metric=gate_id`` flags (one per metric, no duplicates);
  there is no discovery of "the" baseline gate, no alias and no default metric list;
- the report must belong to the run (same ``run_id`` and ``experiment_hash``) and be ``PASS``
  (the degradation operation refuses anything else);
- each ``(metric, gate_id)`` must select exactly one closed-registry definition
  (``MONITORING_METRICS``; ``G5.*`` and the reported-only gates are refused, as the resolver does)
  and is verified with the resolver's own ``_check_baseline_gates``: one gate with that id, the
  definition's exact label, an exact value (a float-only value is never exported);
- the metric key must also be what the degradation operation compares (the gate's label without
  one comparator suffix), so an exported set cannot be refused later for a naming mismatch.

Values are the gates' exact values as canonical decimal text; ``validation_report_hash`` is the
report's content hash. Any missing, extra or mismatching input is refused and nothing is written.
The output file must not exist (never overwritten). No clock, no network, no registry access.

Run with ``python -m research.operations.baseline_export --help``. Exit codes: ``0`` written;
``1`` refused; ``3`` I/O failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from core.domain.base import Contract, canonical_json, exact_decimal_text
from core.domain.research import ExperimentRun, ValidationReport, Verdict
from research.operations.authority import (
    MONITORING_METRICS,
    AuthorityRefused,
    RuledMetric,
    _check_baseline_gates,
    _refused_reason,
)
from research.operations.degradation import (
    BaselineMetricSet,
    DegradationOperationRefused,
    _gate_metric_name,
)

__all__ = ["BaselineExportRefused", "export_baseline_set", "main", "parse_gate_mapping"]

EXIT_REFUSED: Final = 1
EXIT_FAILED: Final = 3


class BaselineExportRefused(ValueError):
    """An input or binding failed a rule; nothing was exported."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BaselineExportRefused("JSON object has a duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise BaselineExportRefused("JSON contains a non-finite number")


def _load_model[M: Contract](path: Path, model: type[M], label: str) -> M:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise BaselineExportRefused(f"{label} is not valid JSON") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise BaselineExportRefused(f"{label} does not match its contract") from exc


def parse_gate_mapping(entries: Sequence[str]) -> dict[str, str]:
    """``["metric=gate_id", ...]`` as a mapping; empty, malformed or repeated entries (a metric
    or a gate id twice) are refused."""
    if not entries:
        raise BaselineExportRefused("at least one --gate metric=gate_id is required")
    mapping: dict[str, str] = {}
    for entry in entries:
        metric, separator, gate_id = entry.partition("=")
        if not separator or not metric or not gate_id or metric != metric.strip():
            raise BaselineExportRefused(f"--gate {entry!r} is not metric=gate_id")
        if gate_id != gate_id.strip():
            raise BaselineExportRefused(f"--gate {entry!r} has a gate id with whitespace")
        if metric in mapping:
            raise BaselineExportRefused(f"metric {metric!r} is mapped twice")
        mapping[metric] = gate_id
    if len(set(mapping.values())) != len(mapping):
        raise BaselineExportRefused("two metrics are mapped to one gate")
    return mapping


def _ruled(mapping: dict[str, str]) -> list[RuledMetric]:
    """Each pair bound to its one closed-registry definition (never a guessed one)."""
    ruled: list[RuledMetric] = []
    for metric, gate_id in sorted(mapping.items()):
        refused = _refused_reason(gate_id)
        if refused is not None:
            raise BaselineExportRefused(f"{metric!r} of gate {gate_id!r}: {refused}")
        candidates = MONITORING_METRICS.get(metric, ())
        if not candidates:
            raise BaselineExportRefused(
                f"{metric!r} is not defined by the closed monitoring metric registry"
            )
        matching = [definition for definition in candidates if definition.matches(gate_id)]
        if len(matching) != 1:
            raise BaselineExportRefused(
                f"the registry defines {metric!r} for "
                f"{sorted(d.baseline_gate_id for d in candidates)}, not for gate {gate_id!r}"
            )
        ruled.append(RuledMetric(definition=matching[0], gate_id=gate_id))
    return ruled


def export_baseline_set(
    report: ValidationReport, run: ExperimentRun, mapping: dict[str, str]
) -> BaselineMetricSet:
    """The ``BaselineMetricSet`` of ``mapping`` over ``report`` (module docs); refuses anything
    that is not exactly bound."""
    if report.run_id != run.run_id:
        raise BaselineExportRefused("the report is not for this run (run_id differs)")
    if report.experiment_hash != run.experiment_hash:
        raise BaselineExportRefused("the report is not for this run (experiment_hash differs)")
    if report.verdict is not Verdict.PASS:
        raise BaselineExportRefused(
            f"the baseline report's verdict is {report.verdict.value}, not PASS"
        )
    ruled = _ruled(mapping)
    try:
        _check_baseline_gates(report, ruled)
    except AuthorityRefused as exc:
        raise BaselineExportRefused(str(exc)) from exc
    gates = {gate.gate_id: gate for gate in report.gates}
    metrics: dict[str, str] = {}
    for item in ruled:
        gate = gates[item.gate_id]  # unique and present: _check_baseline_gates
        if _gate_metric_name(gate.metric) != item.metric_name:
            raise BaselineExportRefused(
                f"gate {item.gate_id!r} reports {gate.metric!r}, which the degradation "
                f"operation does not compare as {item.metric_name!r}"
            )
        if gate.value_exact is None:  # unreachable after _check_baseline_gates
            raise BaselineExportRefused(f"gate {item.gate_id!r} has no exact value")
        metrics[item.metric_name] = exact_decimal_text(gate.value_exact)
    try:
        return BaselineMetricSet(
            validation_report_hash=report.content_hash(),
            metrics=metrics,
            gate_ids=dict(mapping),
        )
    except DegradationOperationRefused as exc:
        raise BaselineExportRefused(f"the baseline set is invalid: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the explicit baseline-set JSON of one PASS ValidationReport "
        "(ADR-0105 §3). Nothing is inferred; the gate mapping is the repeated --gate flag."
    )
    parser.add_argument("--report", required=True, type=Path, help="ValidationReport JSON")
    parser.add_argument("--run", required=True, type=Path, help="its ExperimentRun JSON")
    parser.add_argument(
        "--gate",
        action="append",
        default=[],
        metavar="METRIC=GATE_ID",
        help="one metric and the gate id it is read from (repeat for every metric)",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="baseline-set JSON to create (must not exist)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        mapping = parse_gate_mapping(args.gate)
        report = _load_model(args.report, ValidationReport, "validation report")
        run = _load_model(args.run, ExperimentRun, "experiment run")
        baseline = export_baseline_set(report, run, mapping)
        text = canonical_json(baseline.payload()) + "\n"
        try:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError as exc:
            raise BaselineExportRefused("the output file already exists") from exc
    except BaselineExportRefused as exc:
        print(f"baseline export refused ({exc})", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"baseline export failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAILED
    print(f"baseline_set={args.output}")
    print(f"baseline_set_hash={baseline.content_hash()}")
    print(f"validation_report_hash={baseline.validation_report_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
