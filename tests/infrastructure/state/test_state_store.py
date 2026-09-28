"""Local StateResult content-addressed artifact storage (ADR-0089)."""

from __future__ import annotations

from pathlib import Path

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
