"""``run_unattended_and_report`` (research/loop/compose.py): the P11 loop's optional report sink.

Reuses the P11 loop fixtures (``tests/research/loop/loop_fixtures.py``, W2 real components) so this
exercises the real composed loop, not a hand-built stand-in for ``LoopRecord``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportStore
from core.domain.base import canonical_json
from research.loop import run_unattended_and_report
from tests.research.loop import loop_fixtures as fx


def _build(tmp: Path) -> Any:
    """The W2 real-component loop (TEST ONLY config) with one knowledge hypothesis and no
    evolution: every round still yields a real ``LoopRecord`` (round 1 re-evaluates round 0's
    INCONCLUSIVE hypothesis on the accumulated research data), at a fraction of the default
    scenario's validation cost — this module tests the report sink, not the research."""
    return fx.build(tmp, fx.config(lookbacks=(60,), loop_wiring=fx.wiring(evolution=False)))


def test_reports_root_none_writes_nothing(tmp_path: Path) -> None:
    loop, _, _ = _build(tmp_path)
    records = run_unattended_and_report(loop, 2, reports_root=None)
    assert len(records) == 2
    assert ReportStore(tmp_path).list(ReportKind.RESEARCH_LOOP_ROUND) == []


def test_reports_root_writes_every_round_and_the_store_reads_them_back(tmp_path: Path) -> None:
    loop_dir, reports_dir = tmp_path / "loop", tmp_path / "reports"
    loop, _, _ = _build(loop_dir)
    records = run_unattended_and_report(loop, 3, reports_root=reports_dir)
    assert len(records) == 3

    store = ReportStore(reports_dir)
    listed = store.list(ReportKind.RESEARCH_LOOP_ROUND)
    assert {env.id for env in listed} == {record.record_hash for record in records}
    for record in records:
        envelope = store.get(ReportKind.RESEARCH_LOOP_ROUND, record.record_hash)
        assert envelope.payload == json.loads(canonical_json(record.payload()))

    client = TestClient(create_app(reports_root=reports_dir))
    detail = client.get(f"/reports/research_loop_round/{records[0].record_hash}").json()
    assert detail["payload"]["round_index"] == 0
    assert detail["payload"]["loop_id"] == "synthetic_loop"


def test_running_the_same_rounds_again_is_idempotent_on_disk(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    first_loop, _, _ = _build(tmp_path / "a")
    run_unattended_and_report(first_loop, 2, reports_root=reports_dir)
    before = {p: p.read_text() for p in (reports_dir / "research_loop_round").glob("*.json")}

    second_loop, _, _ = _build(tmp_path / "b")  # same seed/config -> identical record hashes
    run_unattended_and_report(second_loop, 2, reports_root=reports_dir)
    after = {p: p.read_text() for p in (reports_dir / "research_loop_round").glob("*.json")}
    assert before == after
