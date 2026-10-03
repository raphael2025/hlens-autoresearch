"""Reproduce bounded audit observations against the e8e3a426 implementation.

Run with the repository virtualenv and PYTHONPATH=.; --skip-timing runs only
the inexpensive semantic probes. No market data, services or credentials used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import timedelta
from decimal import Decimal

from core.contracts.feature import FeatureRequest
from core.contracts.strategy import BacktestRequest, TargetPosition
from infrastructure.feature.runner import run_feature
from plugins.backtest import BarBacktester
from plugins.features.bars import BarVolumeSumProvider
from research.validation.stats import effective_sample_size
from tests.plugins.features.test_bar_features import MANIFEST, T0, bar
from tests.strategy_fixtures import COSTS, make_bars


def feature_scaling() -> list[dict[str, int | float]]:
    rows: list[dict[str, int | float]] = []
    for n in (250, 500, 1000, 2000):
        spec = BarVolumeSumProvider.spec(10)
        request = FeatureRequest(
            feature=spec.ref,
            spec_hash=spec.content_hash(),
            manifest_content_hash=MANIFEST,
            knowledge_cutoff=T0 + timedelta(days=10),
            evaluation_times=tuple(T0 + timedelta(minutes=i + 1) for i in range(n)),
            observations=tuple(bar(i, "100", volume="10") for i in range(n)),
        )
        provider = BarVolumeSumProvider((spec,))
        start = time.perf_counter()
        result = run_feature(provider, spec, request)
        elapsed = time.perf_counter() - start
        assert len(result.values) == n
        assert all(value.value is None for value in result.values[:9])
        assert all(value.value == 100 for value in result.values[9:])
        rows.append(
            {
                "n": n,
                "seconds": round(elapsed, 6),
                "returned_values": len(result.values),
                "prefix_observations": n * (n + 1) // 2,
            }
        )
    return rows


def cash_semantics() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    bars = make_bars("BTCUSDT", [Decimal("100")] * 3, start=T0)
    for weight in ("1", "-1"):
        target = TargetPosition(
            decision_time=T0,
            instrument="BTCUSDT",
            target_weight=Decimal(weight),
            inputs_used=1,
            latest_input_available_time=T0,
        )
        result = BarBacktester().run(
            BacktestRequest(
                cost_model=COSTS,
                initial_equity=Decimal("1000"),
                bars=bars,
                targets=(target,),
            )
        )
        first_fill = result.fills[0]
        first_book = next(p for p in result.equity_curve if p.time >= first_fill.fill_time)
        rows.append(
            {
                "weight": weight,
                "quantity": str(first_fill.quantity),
                "cash_after_fill": str(first_book.cash),
                "final_equity": str(result.final_equity),
            }
        )
    return rows


def overlap_semantics() -> dict[str, int]:
    return {
        str(horizon): effective_sample_size(
            [(T0 + timedelta(minutes=i), T0 + timedelta(minutes=i + horizon)) for i in range(1000)]
        )
        for horizon in (1, 2, 15)
    }


def archive_import() -> dict[str, str | int]:
    # Fresh interpreter: already-imported package members would hide this failure.
    code = """
import importlib.abc
import sys

class MissingReference(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'plugins.backtest.reference':
            raise ModuleNotFoundError('simulated archive: ' + fullname)
        return None

sys.meta_path.insert(0, MissingReference())
from plugins.backtest import BarBacktester
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return {"returncode": result.returncode, "last_stderr_line": result.stderr.splitlines()[-1]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-timing", action="store_true")
    args = parser.parse_args()
    results = {
        "cash": cash_semantics(),
        "overlap_cluster_count": overlap_semantics(),
        "archive_import": archive_import(),
    }
    if not args.skip_timing:
        results["feature_scaling"] = feature_scaling()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
