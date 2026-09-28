"""ADR-0085 library registration: explicit-point entries, sources, pipeline wiring."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from plugins.backtest import BarBacktester
from plugins.knowledge import LocalKnowledgeProvider
from research.strategies.donchian_breakout import DONCHIAN_SIGNALS
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import (
    LibraryEntry,
    donchian_entry,
    dual_momentum_entry,
    library_entries,
    resolve_knowledge,
)
from research.strategies.pipeline import EvaluationInputs, EvaluationStatus, evaluate_strategy
from research.strategies.price_signals import bar_price_signals
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes

PAIR = ("BTCUSDT", "ETHUSDT")
BARS = make_bars("BTCUSDT", wave_closes(160)) + make_bars("ETHUSDT", wave_closes(160, phase=13))
DECISIONS = tuple(T0 + minute * MINUTE for minute in range(62, 160, 6))
ENTRIES = (
    donchian_entry(entry_window=20, exit_window=10),
    dual_momentum_entry(lookback=60),
)


def test_default_library_is_unchanged() -> None:
    names = [entry.spec.name for entry in library_entries()]
    assert names == ["tsmom_bars", "tsmom_bars_vol_scaled", "xsmom_bars"]


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda entry: entry.spec.name)
def test_entries_have_sources_and_an_explicit_declared_point(entry: LibraryEntry) -> None:
    assert entry.sources
    items = resolve_knowledge(entry.sources, LocalKnowledgeProvider())
    assert [item.ref.target_identity() for item in items] == [
        ref.target_identity() for ref in entry.sources
    ]
    space = entry.spec.param_search_space
    assert set(entry.spec.params) == set(space)
    assert all(entry.spec.params[key] in values for key, values in space.items())
    assert entry.risk_policy is None


def test_entries_need_every_parameter() -> None:
    with pytest.raises(TypeError):
        donchian_entry(entry_window=20)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        dual_momentum_entry()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        dual_momentum_entry(lookback=7)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda entry: entry.spec.name)
def test_pipeline_is_deterministic_and_not_validated(tmp_path: Path, entry: LibraryEntry) -> None:
    if entry.spec.signals == DONCHIAN_SIGNALS:
        signals = bar_price_signals(BARS, DONCHIAN_SIGNALS)
    else:
        signals = tuple(item for item in bar_signals(BARS) if item.signal == LOG_RETURN_SIGNAL)
    inputs = EvaluationInputs(
        instruments=PAIR,
        bars=BARS,
        decision_times=DECISIONS,
        knowledge_cutoff=T0 + timedelta(days=1),
        cost_model=COSTS,
        initial_equity=Decimal(10000),
        signals=signals,
    )
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    first, second = (
        evaluate_strategy(entry.candidate(), inputs, backtester=BarBacktester(), registry=registry)
        for _ in range(2)
    )
    assert first.status is EvaluationStatus.NOT_VALIDATED, first.failure
    assert first.backtest is not None and second.backtest is not None
    assert first.backtest.result_hash == second.backtest.result_hash
    assert first.backtest.fills, "the fixture must trade"
    assert registry.records() == ()
