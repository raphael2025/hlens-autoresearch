"""Local StateResult content-addressed artifact storage (ADR-0089)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from infrastructure.state.store import StateResultStore, StateStoreCorrupted
from infrastructure.state.runner import run_state, state_request
from plugins.states import VolatilityRegimeProvider
from tests.fake_states import TEST_CUTS, TEST_MIN_HISTORY, TEST_SEED, TEST_WINDOW, VOL_FEATURE, at, level_inputs


def _result():
    spec = VolatilityRegimeProvider.spec(
        VOL_FEATURE,
        cuts=TEST_CUTS,
        min_history=TEST_MIN_HISTORY,
        training_window=TEST_WINDOW,
        seed=TEST_SEED,
    )
    provider = VolatilityRegimeProvider((spec,))
    request = state_request(spec, (at(4), at(5), at(6)), level_inputs(VOL_FEATURE, 7))
    return run_state(provider, spec, request)


def test_store_round_trips_canonical_run_and_does_not_overwrite(tmp_path: Path) -> None:
    result = _result()
    store = StateResultStore(tmp_path / "states")
    path = store.put(result)
    assert path.name == f"{result.result_hash}.json"
    assert store.put(result) == path
    assert store.get(result.result_hash) == result
    assert store.hashes() == (result.result_hash,)


def test_store_refuses_tampered_and_non_object_json(tmp_path: Path) -> None:
    store = StateResultStore(tmp_path / "states")
    result = _result()
    path = store.put(result)
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(StateStoreCorrupted):
        store.get(result.result_hash)
    with pytest.raises(StateStoreCorrupted):
        store.put(result)


def test_concurrent_puts_use_distinct_temporary_paths_and_publish_one_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    result = _result()
    store = StateResultStore(tmp_path / "states")
    barrier = Barrier(2)
    temporary_paths: list[Path] = []
    real_link = os.link

    def synchronized_link(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        temporary_paths.append(Path(source))
        barrier.wait(timeout=10)
        real_link(source, target)

    monkeypatch.setattr("infrastructure.state.store.os.link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: store.put(result), range(2)))

    assert results == (store.root / f"{result.result_hash}.json",) * 2
    assert len(temporary_paths) == 2 and len(set(temporary_paths)) == 2
    assert all(
        path.parent == store.root and path.name.endswith(".tmp") for path in temporary_paths
    )
    assert tuple(path.name for path in store.root.iterdir()) == (f"{result.result_hash}.json",)
    assert store.get(result.result_hash) == result
