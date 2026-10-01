"""Shared fixtures for the State run / report CLI tests (ADR-0102).

Builds a real feature run (synthetic bars -> ``bar_realized_vol_5``), a trained StateSpec and the
JSON files the CLI reads. Test-only numbers: fixture parameters, not validation thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.contracts.feature import FeatureRequest, FeatureResult
from core.domain.base import Contract, canonical_json, content_hash
from core.domain.specs import FeatureSpec, StateSpec
from infrastructure.feature.runner import run_feature
from plugins.features import BarRealizedVolatilityProvider
from plugins.states import VolatilityRegimeProvider
from tests.fake_states import (
    TEST_CUTS,
    TEST_MIN_HISTORY,
    TEST_SEED,
    TEST_WINDOW,
    at,
    bars,
)

MANIFEST = content_hash({"manifest": "state-run-cli"})
PROVIDER = "volatility_regime@1.0.0"
BAR_COUNT = 40
TIMES = tuple(at(i) for i in range(1, BAR_COUNT + 1))


def write_json(path: Path, model: Contract) -> Path:
    path.write_text(canonical_json(model.model_dump(mode="json")) + "\n", encoding="utf-8")
    return path


def feature_run(
    spec: FeatureSpec, times: tuple[datetime, ...] = TIMES
) -> tuple[FeatureRequest, FeatureResult]:
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=at(10_000),
        evaluation_times=times,
        observations=bars(BAR_COUNT),
    )
    provider = BarRealizedVolatilityProvider((spec,))
    return request, run_feature(provider, spec, request)


@dataclass(frozen=True)
class Files:
    """The three JSON inputs of ``compute`` plus the objects they were written from."""

    spec: StateSpec
    spec_path: Path
    request_path: Path
    result_path: Path

    def compute_args(self, *extra: str, provider: str = PROVIDER) -> list[str]:
        return [
            "compute",
            "--spec",
            str(self.spec_path),
            "--provider",
            provider,
            "--feature",
            str(self.request_path),
            str(self.result_path),
            *extra,
        ]


def make_files(root: Path, *, seed: int | None = TEST_SEED) -> Files:
    vol = BarRealizedVolatilityProvider.spec(5)
    spec = VolatilityRegimeProvider.spec(
        vol.ref,
        cuts=TEST_CUTS,
        min_history=TEST_MIN_HISTORY,
        training_window=TEST_WINDOW,
        seed=TEST_SEED,
    )
    if seed is None:  # a trained spec that does not fix its seed (not constructible via spec())
        spec = spec.model_copy(update={"seed": None})
    request, result = feature_run(vol)
    return Files(
        spec=spec,
        spec_path=write_json(root / "spec.json", spec),
        request_path=write_json(root / "request.json", request),
        result_path=write_json(root / "result.json", result),
    )
