"""Writer for Phase 6 state x strategy matrices (``research/experiments/state_strategy.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.experiments.state_strategy import StateStrategyMatrix
from research.reports.envelope import WrittenReport, write_report_file

__all__ = ["KIND", "write_state_strategy_matrix"]

#: Directory name under the report root; matches ``apps.api.store.ReportKind.STATE_STRATEGY_MATRIX``
KIND = "state_strategy_matrix"


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _payload(matrix: StateStrategyMatrix) -> dict[str, Any]:
    return {
        "strategy": str(matrix.strategy),
        "state": str(matrix.state),
        "cells": [
            {
                "state": cell.state,
                "count": cell.count,
                "total": str(cell.total),
                "mean": _text(cell.mean),
                "hit_rate": _text(cell.hit_rate),
                "top_returns": [str(value) for value in cell.top_returns],
            }
            for cell in matrix.cells
        ],
        "best_state_share": _text(matrix.best_state_share),
        "top_k_share_in_best_state": _text(matrix.top_k_share_in_best_state),
        "top_k": matrix.top_k,
        "backtest_result_hash": matrix.backtest_result_hash,
        "state_result_hash": matrix.state_result_hash,
        "matrix_hash": matrix.matrix_hash,
    }


def write_state_strategy_matrix(root: Path, matrix: StateStrategyMatrix) -> WrittenReport:
    """Write at ``<root>/state_strategy_matrix/<matrix_hash>.json``.

    ``matrix.matrix_hash`` is already the object's own content hash (every cell, share and the
    bound backtest / state result hashes). This module mirrors it field-for-field rather than
    importing a private helper from ``research/experiments/state_strategy.py`` (that module is
    owned by another phase's work in this batch and is not touched here).
    """
    return write_report_file(root, KIND, matrix.matrix_hash, _payload(matrix))
