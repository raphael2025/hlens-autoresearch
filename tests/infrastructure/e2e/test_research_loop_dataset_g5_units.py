"""Units of the dataset-backed loop's sealed-OOS declaration (no catalog; ADR-0049 dataset G5 note).

The end-to-end behaviour on the PostgreSQL test catalog is in
``tests/infrastructure/e2e/test_research_loop_real_data_g5.py``; here: the ``DatasetRound``
declaration, the unseal budget / fingerprint of ``DatasetLoopConfig``, and that a
``SealedDatasetPair`` decides ``evaluable`` and refuses every read without touching storage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from research.loop import DatasetRound, OosUnsealBudget
from research.loop.dataset_compose import dataset_loop_fingerprint
from research.loop.dataset_source import SealedDatasetPair, WithheldSealedBars
from research.validation.sealed_oos import SealedEvaluation, SealedOosLocked, SealedWindow
from tests.infrastructure.e2e import test_research_loop_real_data as base

A, B, C, D, E = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)
START = datetime(2023, 11, 15, tzinfo=UTC)
END = START + timedelta(hours=1)
UNSEAL = OosUnsealBudget(max_unsealings=1, approved_families={base.FAMILY: "test-human"})


def _with_unseal(rounds: tuple[DatasetRound, ...]) -> Any:
    config = base._config((DatasetRound(A, B),))
    wiring = replace(config.wiring, oos_unseal=UNSEAL, sealed_decision_step=timedelta(minutes=5))
    return replace(config, rounds=rounds, wiring=wiring)


def test_a_sealed_pair_is_declared_whole_and_never_with_a_withheld_manifest() -> None:
    with pytest.raises(ValueError, match="both its feature and price"):
        DatasetRound(A, B, sealed_feature_manifest_hash=C)
    with pytest.raises(ValueError, match="both its feature and price"):
        DatasetRound(A, B, sealed_price_manifest_hash=D)
    with pytest.raises(ValueError, match="not both"):
        DatasetRound(A, B, E, sealed_feature_manifest_hash=C, sealed_price_manifest_hash=D)
    with pytest.raises(ValueError, match="content hash"):
        DatasetRound(A, B, sealed_feature_manifest_hash=" ", sealed_price_manifest_hash=D)
    paired = DatasetRound(A, B, sealed_feature_manifest_hash=C, sealed_price_manifest_hash=D)
    assert paired.has_sealed_pair and not DatasetRound(A, B, E).has_sealed_pair
    # a round without a pair keeps its earlier payload (and fingerprint); a pair is bound
    assert DatasetRound(A, B, E).payload() == {
        "feature_manifest_hash": A,
        "price_manifest_hash": B,
        "sealed_manifest_hash": E,
    }
    assert paired.payload()["sealed_feature_manifest_hash"] == C
    assert paired.payload()["sealed_price_manifest_hash"] == D


def test_an_unseal_budget_needs_a_declared_sealed_pair_and_is_fingerprinted() -> None:
    with pytest.raises(ValueError, match="G5 over dataset rounds is not wired"):
        _with_unseal((DatasetRound(A, B, E),))  # withheld only: nothing G5 could run on
    paired = DatasetRound(A, B, sealed_feature_manifest_hash=C, sealed_price_manifest_hash=D)
    config = _with_unseal((DatasetRound(A, B), paired))
    assert config.wiring.oos_unseal == UNSEAL
    printed = dataset_loop_fingerprint(config)
    assert printed["oos_unseal"] == {
        "max_unsealings": 1,
        "approved_families": {base.FAMILY: "test-human"},
    }
    assert printed["rounds"][1]["sealed_price_manifest_hash"] == D
    # another sealed pair, another approver or no budget: another fingerprint
    other_pair = replace(
        config, rounds=(DatasetRound(A, B), replace(paired, sealed_price_manifest_hash=E))
    )
    other_human = replace(
        config,
        wiring=replace(
            config.wiring,
            oos_unseal=OosUnsealBudget(max_unsealings=1, approved_families={base.FAMILY: "x"}),
        ),
    )
    no_budget = replace(
        config, wiring=replace(config.wiring, oos_unseal=None, sealed_decision_step=None)
    )
    prints = [dataset_loop_fingerprint(c) for c in (config, other_pair, other_human, no_budget)]
    assert len({str(sorted(p.items())) for p in prints}) == 4


def _pair() -> SealedDatasetPair:
    # no catalog, no research manifest: evaluable and the refusals below never touch them
    return SealedDatasetPair(
        cast(Any, None),
        symbol="BTC-USDT",
        feature_manifest_hash=C,
        price_manifest_hash=D,
        window=(START, END),
        as_of=END,
        research_price=cast(Any, None),
    )


def test_a_sealed_pair_decides_evaluable_and_refuses_reads_without_storage() -> None:
    pair = _pair()
    assert pair.window == (START, END)
    assert "after this round's cutoff" in (pair.evaluable(END - timedelta(minutes=1)) or "")
    assert pair.evaluable(END) is None
    assert pair.binding() == {}
    with pytest.raises(SealedOosLocked, match="not been released"):
        pair.signals(cast(Any, None), cast(Any, None), ())
    other = SealedEvaluation(base.FAMILY, SealedWindow(START, END + timedelta(hours=1)))
    with pytest.raises(SealedOosLocked, match="another sealed window"):
        pair.release(other)
    assert not other.taken("bars")


def test_a_withheld_only_sealed_window_is_never_evaluable_or_released() -> None:
    withheld = WithheldSealedBars((), (START, END))
    assert "no sealed manifest pair" in (withheld.evaluable(END) or "")
    with pytest.raises(SealedOosLocked):
        withheld.release(SealedEvaluation(base.FAMILY, SealedWindow(START, END)))
