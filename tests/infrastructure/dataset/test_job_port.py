"""ADR-0101 D2: the v3 Dataset job port and the worker job over the real bounded pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.worker.dataset_job import DATASET_BUILD_JOB, dataset_build_job, dataset_job_handlers
from apps.worker.jobs import JobRunner
from infrastructure.dataset.builder import DatasetBuilder, DatasetEvidenceRequest
from infrastructure.dataset.factory import open_dataset_pipeline
from infrastructure.dataset.job_port import (
    DatasetJobRequestError,
    PipelineDatasetJobPort,
    request_document,
    request_from_document,
)
from infrastructure.dataset.pinning import pin_dataset_pit_spec
from infrastructure.event_bus import InMemoryEventBus
from infrastructure.pit.selector import PitSelector
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseBuilder
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, START, World
from tests.infrastructure.dataset.entry_support import (
    make_profile,
    patch_catalog,
    settings_for,
    v3_ready,
)


def _request(w: World) -> DatasetEvidenceRequest:
    pit = pin_dataset_pit_spec(
        w.h.adapter,
        name="test.job",
        version="1.0.0",
        simulation_time=ds.SIM,
        knowledge_cutoff=ds.SIM,
        listing_assumption=False,
    )
    return DatasetEvidenceRequest(FIRST_SLICE_UNIVERSE, pit, "agg_trades", START, END)


# ------------------------------------------------------------------------- the document


def test_the_request_document_round_trips_through_json(w: World) -> None:
    w.listed()
    request = _request(w)
    document = json.loads(json.dumps(request_document(request)))
    assert request_from_document(document) == request
    assert document["universe"] == f"{FIRST_SLICE_UNIVERSE.name}@{FIRST_SLICE_UNIVERSE.version}"
    assert document["start"] == START.isoformat()


def _mutated(w: World, **changes: Any) -> dict[str, Any]:
    document = request_document(_request(w))
    for key, value in changes.items():
        if value is None:
            del document[key]
        else:
            document[key] = value
    return document


@pytest.mark.parametrize(
    "changes",
    [
        {"universe": None},
        {"extra": 1},
        {"universe": "hlens.universe.other@1.0.0"},
        {"universe": FIRST_SLICE_UNIVERSE.name},
        {"data_type": "trades_raw"},
        {"start": "2023-11-14T21:00:00"},
        {"end": "not-a-time"},
        {"end": 7},
        {"pit": "not an object"},
        {"pit": {"name": "x"}},
    ],
)
def test_a_malformed_request_document_is_refused(w: World, changes: dict[str, Any]) -> None:
    w.listed()
    with pytest.raises(DatasetJobRequestError):
        request_from_document(_mutated(w, **changes))


def test_a_pit_document_not_in_canonical_form_is_refused(w: World) -> None:
    """The validator would re-sort the bindings: the document is not what it hashes as."""
    w.listed()
    document = request_document(_request(w))
    bindings = document["pit"]["availability_bindings"]
    assert len(bindings) >= 2
    document["pit"]["availability_bindings"] = list(reversed(bindings))
    with pytest.raises(DatasetJobRequestError, match="canonical"):
        request_from_document(document)


def test_a_request_document_is_never_built_from_a_non_mapping() -> None:
    with pytest.raises(DatasetJobRequestError, match="JSON object"):
        request_from_document(["universe"])


# ------------------------------------------------------------------------- the real job


def _forbidden(name: str) -> Any:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{name} is not part of the v3 job")

    return fail


@pytest.fixture
def ready(w: World, tmp_path: Path) -> World:
    v3_ready(w, tmp_path)
    return w


def test_the_worker_job_builds_then_replays_one_v3_dataset(
    ready: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = ready
    _, profile = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)
    for owner, name in (
        (DatasetBuilder, "select"),
        (DatasetBuilder, "build"),
        (PitSelector, "select"),
        (UniverseBuilder, "build"),
    ):
        monkeypatch.setattr(owner, name, _forbidden(f"{owner.__name__}.{name}"))
    results = tmp_path / "results.jsonl"
    with open_dataset_pipeline(settings_for(w), profile) as opened:
        port = PipelineDatasetJobPort(opened.pipeline, profile)
        document = request_document(_request(w))
        job = dataset_build_job(port, document)
        assert job.params["selection_id"] == opened.pipeline.builder.selection_id(_request(w))
        runner = JobRunner(
            InMemoryEventBus(),
            consumer="dataset",
            topic="worker.dataset",
            handlers=dataset_job_handlers(port),
            max_attempts=1,
            results=results,
        )
        runner.submit(job)
        (outcome,) = runner.run_pending()
        assert outcome.succeeded, outcome.error
        summary = outcome.result
        assert summary["selection_id"] == job.params["selection_id"]
        assert summary["profile_hash"] == profile.profile_hash()
        assert summary["capacity_evidence"] == "none"
        assert summary["row_count"] > 0 and not summary["manifest_replayed"]
        assert opened.pipeline.load_manifest(summary["manifest_hash"]) is not None
        # The same selection built again outside the runner is the replay of that manifest.
        again = port.build(document)
        assert again["replayed"] and again["manifest_hash"] == summary["manifest_hash"]
    assert DATASET_BUILD_JOB in results.read_text(encoding="utf-8")
