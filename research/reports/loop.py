"""Writer for Phase 11 research-loop round audit records.

Imports ``apps.worker.loop.LoopRecord`` — research importing ``apps/worker`` is the one sanctioned
exception to "apps/ and research/ never see each other" (ADR-0049; ``research/loop/compose.py``
already does the same import). ``apps/`` still never imports ``research/``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from apps.worker.loop import LoopRecord
from research.reports.envelope import WrittenReport, write_report_file

__all__ = ["KIND", "write_research_loop_round", "write_research_loop_rounds"]

#: Directory name under the report root; matches ``apps.api.store.ReportKind.RESEARCH_LOOP_ROUND``.
KIND = "research_loop_round"


def write_research_loop_round(root: Path, record: LoopRecord) -> WrittenReport:
    """Write one round at ``<root>/research_loop_round/<record_hash>.json``.

    ``record.record_hash`` is already the round's own content hash (the loop's hash-chained audit
    log, ``apps/worker/loop.py``), so it is the natural report id: the same seed and inputs
    reproduce the same id (idempotent re-write), and any divergent replay of "the same" round gets
    its own id instead of silently overwriting the earlier one.
    """
    return write_report_file(root, KIND, record.record_hash, record.payload())


def write_research_loop_rounds(
    root: Path, records: Sequence[LoopRecord]
) -> tuple[WrittenReport, ...]:
    """``write_research_loop_round`` over every record, in order."""
    return tuple(write_research_loop_round(root, record) for record in records)
