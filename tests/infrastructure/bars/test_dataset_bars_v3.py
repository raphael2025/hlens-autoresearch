"""Dataset bars from v3 evidence manifests (ADR-0077; C1-CONSUMERS).

``outcome_request_from_dataset`` / ``backtest_bars_from_dataset`` with an ``evidence_verifier``
load either manifest form through ``ManifestStore.load_any``; a v3 manifest's bars come from a
chunk walk (no re-selection) and must equal the v2 path's bars of the same world bit for bit (only
the manifest hash differs). A v2 hash behaves exactly as without a verifier; a v3 hash without one
is refused by the store. Same SQLite ``World`` as ``test_dataset_bars.py``; v3 builds use the real
upstreams, chunk writer and streaming verifier (``v3_support``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec
from infrastructure.bars.dataset import (
    DatasetBarsError,
    backtest_bars_from_dataset,
    feature_observations_from_dataset,
    outcome_request_from_dataset,
)
from infrastructure.dataset.builder import DatasetBuildSummary, DatasetBuilt
from infrastructure.dataset.manifests import ManifestFormError
from infrastructure.feature.dataset import DatasetBindingError
from infrastructure.feature.observations import bar_observations
from infrastructure.pit.selector import PitSelector
from plugins.outcomes import ForwardReturnOutcome
from research.outcomes import materialize
from tests.infrastructure.bars import v3_support as v
from tests.infrastructure.bars.test_dataset_bars import BTC, EVENTS, FIRST, FORWARD, MINUTE
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision.rest_store_support import SYMBOL


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _both(
    w: World, *, with_assumption: bool = True, **spec_fields: Any
) -> tuple[v.V3, PointInTimeSpec, DatasetBuilt, DatasetBuildSummary]:
    """One spec, a v2 and a v3 bar dataset of it (spec taken before both builds)."""
    v.ingest(w)
    spec = v.spec_of(w, with_assumption=with_assumption, **spec_fields)
    machinery = v.v3(w)
    built = rt.build(w, spec, data_type="klines_1m", window=v.DAY_WINDOW)
    summary = machinery.build(spec)
    return machinery, spec, built, summary


def _bars(w: World, manifest_hash: str, verifier: Any = None, **kwargs: Any) -> Any:
    return backtest_bars_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=manifest_hash,
        evidence_verifier=verifier,
        **kwargs,
    )


def _outcome(w: World, manifest_hash: str, verifier: Any = None, **kwargs: Any) -> Any:
    return outcome_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=manifest_hash,
        symbol=kwargs.pop("symbol", BTC),
        label_spec=FORWARD,
        events=EVENTS,
        evidence_verifier=verifier,
        **kwargs,
    )


# ---------------------------------------------------------------------------------------------
# equivalence with the v2 path


@pytest.mark.parametrize("with_assumption", [True, False], ids=["adr-0032", "stored"])
def test_v3_backtest_bars_are_the_v2_bars_of_the_same_world(
    w: World, with_assumption: bool
) -> None:
    machinery, _, built, summary = _both(w, with_assumption=with_assumption)
    v2_hash, v3_hash = built.manifest.content_hash(), summary.manifest_hash
    assert v2_hash != v3_hash
    assert summary.row_count == v.BARS and summary.chunk_count == 3  # chunk_rows 2: 2 + 2 + 1
    v2 = _bars(w, v2_hash)
    got = _bars(w, v3_hash, machinery.verifier)
    assert got.bars == v2.bars and len(got.bars) == v.BARS
    assert got.price_cutoff == v2.price_cutoff
    assert got.manifest_content_hash == v3_hash
    # a sub-window, every symbol explicitly
    window = {"start": FIRST + MINUTE, "end": FIRST + 3 * MINUTE, "symbols": (BTC,)}
    assert _bars(w, v3_hash, machinery.verifier, **window).bars == _bars(w, v2_hash, **window).bars


def test_v3_outcome_requests_are_the_v2_requests_but_for_the_manifest_hash(w: World) -> None:
    machinery, _, built, summary = _both(w)
    v2 = _outcome(w, built.manifest.content_hash())
    got = _outcome(w, summary.manifest_hash, machinery.verifier)
    assert got.manifest_content_hash == summary.manifest_hash
    assert got.model_copy(update={"manifest_content_hash": v2.manifest_content_hash}) == v2
    # the labels computed on either request are the same
    provider = ForwardReturnOutcome((FORWARD,))
    ours, theirs = materialize(provider, got), materialize(provider, v2)
    for event in EVENTS:
        mine, reference = ours.get(event.event_key), theirs.get(event.event_key)
        assert (mine is None) == (reference is None)
        if mine is None or reference is None:
            continue
        assert (mine.value, mine.entry_time, mine.exit_time, mine.available_time) == (
            reference.value,
            reference.entry_time,
            reference.exit_time,
            reference.available_time,
        )


def test_v3_feature_observations_are_the_v2_selections_observations(w: World) -> None:
    machinery, spec, _, summary = _both(w)
    selection = PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    ).select(spec, "klines_1m", SYMBOL, *v.DAY_WINDOW)
    expected = bar_observations(selection, spec)
    got = feature_observations_from_dataset(
        w.h.adapter,
        builder=w.builder(),
        manifest_content_hash=summary.manifest_hash,
        symbol=BTC,
        evidence_verifier=machinery.verifier,
    )
    assert got == expected and len(got) == v.BARS


# ---------------------------------------------------------------------------------------------
# the v2 path is unchanged; v3 needs its verifier


def test_a_v2_hash_with_an_evidence_verifier_is_exactly_the_v2_path(w: World) -> None:
    machinery, _, built, _ = _both(w)
    v2_hash = built.manifest.content_hash()
    assert _bars(w, v2_hash, machinery.verifier) == _bars(w, v2_hash)
    assert _outcome(w, v2_hash, machinery.verifier) == _outcome(w, v2_hash)


def test_a_v3_hash_without_an_evidence_verifier_is_refused(w: World) -> None:
    _, _, _, summary = _both(w)
    with pytest.raises(ManifestFormError):
        _bars(w, summary.manifest_hash)
    with pytest.raises(ManifestFormError):
        _outcome(w, summary.manifest_hash)


def test_v3_observations_of_a_v2_hash_are_refused(w: World) -> None:
    machinery, _, built, _ = _both(w)
    with pytest.raises(DatasetBindingError, match="not a v3"):
        feature_observations_from_dataset(
            w.h.adapter,
            builder=w.builder(),
            manifest_content_hash=built.manifest.content_hash(),
            symbol=BTC,
            evidence_verifier=machinery.verifier,
        )


def test_an_unknown_hash_is_refused_with_the_verifier(w: World) -> None:
    machinery, _, _, _ = _both(w)
    with pytest.raises(DatasetBindingError, match="no Research Dataset manifest"):
        _bars(w, "0" * 64, machinery.verifier)


def test_a_verifier_of_another_catalog_is_refused(w: World, tmp_path: Path) -> None:
    _, _, _, summary = _both(w)
    with ds.sqlite_world(tmp_path / "other") as other:
        foreign = v.v3(other)
        with pytest.raises(DatasetBindingError, match="another catalog"):
            _bars(w, summary.manifest_hash, foreign.verifier)


# ---------------------------------------------------------------------------------------------
# the same refusals as v2


def test_a_v3_interval_dataset_is_refused_for_bars(w: World) -> None:
    v.ingest(w)
    spec = v.spec_of(w, interval=v.INTERVAL)
    machinery = v.v3(w)
    summary = machinery.build(spec)
    with pytest.raises(DatasetBarsError, match="point-simulation"):
        _bars(w, summary.manifest_hash, machinery.verifier)


def test_v3_refuses_what_v2_refuses(w: World) -> None:
    machinery, _, built, summary = _both(w)
    v2_hash, v3_hash = built.manifest.content_hash(), summary.manifest_hash
    cases: list[tuple[dict[str, Any], str]] = [
        ({"price_cutoff": FIRST}, "after price_cutoff"),  # bars available after the cutoff
        ({"price_cutoff": ds.SIM + timedelta(days=1)}, "after the dataset's PIT view"),
        ({"symbols": ("ETH-USDT",)}, "has no klines_1m rows"),
        ({"symbols": ()}, "no symbol requested"),
        ({"start": FIRST, "end": FIRST}, "non-empty part"),
    ]
    for kwargs, match in cases:
        with pytest.raises(DatasetBarsError, match=match):
            _bars(w, v2_hash, **kwargs)
        with pytest.raises(DatasetBarsError, match=match):
            _bars(w, v3_hash, machinery.verifier, **kwargs)
