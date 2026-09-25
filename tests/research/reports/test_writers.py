"""Report writers (ADR-0048 report writer): round-trip through apps.api.store.ReportStore.

Covers the generic append-only file writer directly and each of the four kind-specific writers,
reading every written file back through both ``ReportStore`` and the ``/reports/...`` HTTP
endpoints (``apps.api.create_app``), so the round trip proves the writer and the store agree on
the file layout, not just that the writer produces *some* JSON.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportStore
from apps.worker.loop import LoopRecord, RoundStatus, StageUsage
from core.domain.base import Kind, Ref, canonical_json, content_hash
from core.domain.research import GateResult, ValidationReport, Verdict
from research.experiments.state_strategy import (
    StateStrategyMatrix,
    matrix_from_backtest,
    state_strategy_matrix,
)
from research.reports import (
    ReportConflict,
    write_report_file,
    write_research_loop_round,
    write_research_loop_rounds,
    write_router_paper_run,
    write_state_strategy_matrix,
    write_validation_report,
)
from research.reports.validation import KIND as VALIDATION_KIND
from tests.research.experiments.test_state_strategy import ST as MATRIX_STATE
from tests.research.experiments.test_state_strategy import S as MATRIX_STRATEGY
from tests.research.experiments.test_state_strategy import _buy_and_hold
from tests.research.experiments.test_state_strategy import _state_result as _matrix_state_result
from tests.research.experiments.test_state_strategy import _t as _matrix_t
from tests.research.router.test_paper import _run as build_router_paper_run

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _gate(verdict: Verdict = Verdict.PASS) -> GateResult:
    return GateResult(
        gate_id="G2.effective_sample_size",
        metric="effective_trades",
        value=10.0,
        threshold=5.0,
        threshold_source="sample_size.min_effective_trades_in_sample",
        verdict=verdict,
    )


def _validation_report(report_id: str = "run-report-1") -> ValidationReport:
    return ValidationReport(
        report_id=report_id,
        run_id="run-1",
        subject=Ref(kind=Kind.HYPOTHESIS, name="h_test", version="1.0.0"),
        experiment_hash=content_hash({"experiment": report_id}),
        constitution_version="1.0.0",
        validation_profile=Ref(kind=Kind.PROFILE, name="test_profile", version="1.0.0"),
        validation_profile_hash=content_hash({"profile": "test"}),
        gates=(_gate(),),
        verdict=Verdict.PASS,
        created_at=T0,
    )


def _loop_record(round_index: int = 0, seed: int = 1) -> LoopRecord:
    return LoopRecord(
        loop_id="loop-x",
        round_index=round_index,
        seed=seed,
        as_of=T0,
        budget_hash=content_hash({"budget": "test"}),
        status=RoundStatus.COMPLETED,
        stages=(),
        transitions=(),
        round_usage=StageUsage(),
        total_usage=StageUsage(),
        previous_hash=None,
    )


# --------------------------------------------------------------------------- write_report_file


def test_write_report_file_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    payload = {"b": 2, "a": 1, "nested": {"z": "1", "y": None}}
    first = write_report_file(tmp_path, "some_kind", "id1", payload)
    assert first.written is True
    on_disk = first.path.read_text(encoding="utf-8")
    assert on_disk == canonical_json(payload)  # sorted keys, compact
    second = write_report_file(tmp_path, "some_kind", "id1", payload)
    assert second.written is False
    assert second.path == first.path
    assert first.path.read_text(encoding="utf-8") == on_disk  # untouched


def test_write_report_file_refuses_conflicting_content(tmp_path: Path) -> None:
    write_report_file(tmp_path, "some_kind", "id1", {"v": 1})
    with pytest.raises(ReportConflict):
        write_report_file(tmp_path, "some_kind", "id1", {"v": 2})
    # the original file is untouched by the refused write
    assert json.loads((tmp_path / "some_kind" / "id1.json").read_text()) == {"v": 1}


@pytest.mark.parametrize("bad_id", ["../secret", "..", "a/b", ".hidden", ""])
def test_write_report_file_rejects_unsafe_ids(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(ValueError):
        write_report_file(tmp_path, "some_kind", bad_id, {"v": 1})


def test_write_report_file_rejects_non_mapping_payload(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_report_file(tmp_path, "some_kind", "id1", ["not", "a", "mapping"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- validation_report


def test_validation_report_round_trips_through_the_store(tmp_path: Path) -> None:
    report = _validation_report()
    written = write_validation_report(tmp_path, report)
    assert written.id == report.content_hash()

    store = ReportStore(tmp_path)
    envelope = store.get(ReportKind.VALIDATION_REPORT, written.id)
    assert envelope.payload == report.model_dump(mode="json")

    client = TestClient(create_app(reports_root=tmp_path))
    detail = client.get(f"/reports/validation_report/{written.id}").json()
    assert detail["payload"]["verdict"] == "PASS"
    assert detail["payload"]["report_id"] == "run-report-1"
    listed = client.get("/reports/validation_report").json()
    assert {item["id"] for item in listed} == {written.id}


def test_rewriting_the_identical_validation_report_is_a_no_op(tmp_path: Path) -> None:
    report = _validation_report()
    first = write_validation_report(tmp_path, report)
    second = write_validation_report(tmp_path, report)
    assert first.written is True
    assert second.written is False
    assert first.id == second.id


def test_two_different_validation_reports_never_collide_on_id(tmp_path: Path) -> None:
    a = write_validation_report(tmp_path, _validation_report("report-a"))
    b = write_validation_report(tmp_path, _validation_report("report-b"))
    assert a.id != b.id  # different content -> different content-hash id, no conflict possible


def test_forcing_the_same_id_with_different_validation_payloads_is_refused(tmp_path: Path) -> None:
    # exercises the append-only rule at the same (root, kind) a kind-specific writer uses.
    write_report_file(tmp_path, VALIDATION_KIND, "shared-id", {"verdict": "PASS"})
    with pytest.raises(ReportConflict):
        write_report_file(tmp_path, VALIDATION_KIND, "shared-id", {"verdict": "FAIL"})


# --------------------------------------------------------------------------- research_loop_round


def test_research_loop_round_round_trips_through_the_store(tmp_path: Path) -> None:
    record = _loop_record()
    written = write_research_loop_round(tmp_path, record)
    assert written.id == record.record_hash

    store = ReportStore(tmp_path)
    envelope = store.get(ReportKind.RESEARCH_LOOP_ROUND, written.id)
    assert envelope.payload == json.loads(canonical_json(record.payload()))

    client = TestClient(create_app(reports_root=tmp_path))
    detail = client.get(f"/reports/research_loop_round/{written.id}").json()
    assert detail["payload"]["loop_id"] == "loop-x"
    assert detail["payload"]["status"] == "COMPLETED"


def test_write_research_loop_rounds_writes_every_record_in_order(tmp_path: Path) -> None:
    records = [_loop_record(round_index=i, seed=i + 1) for i in range(3)]
    written = write_research_loop_rounds(tmp_path, records)
    assert [w.id for w in written] == [r.record_hash for r in records]
    assert len(ReportStore(tmp_path).list(ReportKind.RESEARCH_LOOP_ROUND)) == 3


def test_replaying_the_same_round_content_is_idempotent(tmp_path: Path) -> None:
    record = _loop_record()
    first = write_research_loop_round(tmp_path, record)
    second = write_research_loop_round(tmp_path, record)
    assert first.written is True
    assert second.written is False


# --------------------------------------------------------------------------- state_strategy_matrix


def _matrix_with_backtest() -> StateStrategyMatrix:
    backtest = _buy_and_hold()
    states = _matrix_state_result(
        {_matrix_t(1): "high", _matrix_t(2): "low", _matrix_t(3): "low", _matrix_t(4): None}
    )
    return matrix_from_backtest(MATRIX_STRATEGY, MATRIX_STATE, backtest, states, top_k=1)


def test_state_strategy_matrix_round_trips_through_the_store(tmp_path: Path) -> None:
    matrix = _matrix_with_backtest()
    written = write_state_strategy_matrix(tmp_path, matrix)
    assert written.id == matrix.matrix_hash

    store = ReportStore(tmp_path)
    envelope = store.get(ReportKind.STATE_STRATEGY_MATRIX, written.id)
    assert envelope.payload["strategy"] == str(matrix.strategy)
    assert envelope.payload["backtest_result_hash"] == matrix.backtest_result_hash
    assert envelope.payload["state_result_hash"] == matrix.state_result_hash

    client = TestClient(create_app(reports_root=tmp_path))
    detail = client.get(f"/reports/state_strategy_matrix/{written.id}").json()
    assert detail["payload"]["matrix_hash"] == matrix.matrix_hash


def test_a_plain_state_strategy_matrix_without_p5_p2_hashes_also_writes(tmp_path: Path) -> None:
    matrix = state_strategy_matrix(
        MATRIX_STRATEGY,
        MATRIX_STATE,
        {T0: Decimal("0.01")},
        {T0: "high"},
    )
    written = write_state_strategy_matrix(tmp_path, matrix)
    envelope = ReportStore(tmp_path).get(ReportKind.STATE_STRATEGY_MATRIX, written.id)
    assert envelope.payload["backtest_result_hash"] is None
    assert envelope.payload["state_result_hash"] is None


# --------------------------------------------------------------------------- router_paper_run


def test_router_paper_run_round_trips_through_the_store(tmp_path: Path) -> None:
    run = build_router_paper_run()
    written = write_router_paper_run(tmp_path, run)
    assert written.id == run.run_hash

    store = ReportStore(tmp_path)
    envelope = store.get(ReportKind.ROUTER_PAPER_RUN, written.id)
    assert envelope.payload["router_spec_hash"] == run.router_spec_hash
    assert envelope.payload["run_hash"] == run.run_hash
    assert envelope.payload["result_hash"] == run.result.result_hash
    assert Decimal(envelope.payload["total_switching_cost"]) == run.total_switching_cost

    client = TestClient(create_app(reports_root=tmp_path))
    listed = client.get("/reports/router_paper_run").json()
    assert {item["id"] for item in listed} == {written.id}


def test_router_paper_run_writer_is_deterministic(tmp_path: Path) -> None:
    run = build_router_paper_run()
    again = build_router_paper_run()
    assert run.run_hash == again.run_hash  # paper_run itself is deterministic (test_paper.py)
    first = write_router_paper_run(tmp_path, run)
    second = write_router_paper_run(tmp_path, again)
    assert first.id == second.id
    assert second.written is False
