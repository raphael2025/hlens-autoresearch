"""The v3 Dataset build as a worker-job port (ADR-0101 D2): plain JSON in, plain JSON out.

``apps.worker.dataset_job`` depends only on ``core`` and the standard library, so it cannot build a
``DatasetEvidenceRequest`` itself. A deployment's trusted runtime factory opens the pipeline
(``infrastructure.dataset.factory.open_dataset_pipeline``) and hands the job a
``PipelineDatasetJobPort`` over it. The port speaks a strict request document:

- ``universe``: ``NAME@VERSION`` of a registered universe;
- ``pit``: the **pinned** ``PointInTimeSpec`` as its JSON document (pin it with
  ``pin_dataset_pit_spec`` when the job is submitted, so the job's ``selection_id`` is fixed and a
  re-delivery builds exactly the same selection however the catalog heads moved since);
- ``data_type``, ``start``, ``end``: the Canonical table and the UTC half-open window (ISO 8601
  with an offset).

Missing, unknown or malformed fields raise ``DatasetJobRequestError``; nothing is defaulted. Only
the bounded ``DatasetBuildPipeline`` is used (never the v2 builder, an unbounded PIT selection or
``UniverseBuilder.build``; fixed by ``tests/test_architecture_boundaries.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError

from core.contracts.revision import PointInTimeSpec
from core.domain.base import canonical_json
from infrastructure.canonical import rules
from infrastructure.dataset.builder import DatasetBuildSummary, DatasetEvidenceRequest
from infrastructure.dataset.pipeline import DatasetBuildPipeline
from infrastructure.dataset.profile import DatasetBuildProfile
from infrastructure.universe.builder import REGISTERED_UNIVERSES

__all__ = [
    "REQUEST_FIELDS",
    "DatasetJobRequestError",
    "PipelineDatasetJobPort",
    "profile_identity",
    "request_document",
    "request_from_document",
    "summary_document",
]

#: The exact keys of a job request document.
REQUEST_FIELDS: Final = frozenset({"universe", "pit", "data_type", "start", "end"})


class DatasetJobRequestError(ValueError):
    """A Dataset job request document is missing, malformed or names an unknown value."""


# ------------------------------------------------------------------------- documents


def profile_identity(profile: DatasetBuildProfile) -> dict[str, Any]:
    """The profile's hash and capacity evidence (``none``: not an accepted configuration)."""
    return {
        "profile_hash": profile.profile_hash(),
        "capacity_evidence": "none"
        if profile.capacity_evidence is None
        else profile.capacity_evidence,
    }


def summary_document(
    profile: DatasetBuildProfile, pit: PointInTimeSpec, summary: DatasetBuildSummary
) -> dict[str, Any]:
    """The fixed-size JSON summary of one build (shared by the CLI and the worker job)."""
    return {
        **profile_identity(profile),
        "selection_id": summary.selection_id,
        "manifest_hash": summary.manifest_hash,
        "pit_content_hash": pit.content_hash(),
        "dataset": {"table": summary.dataset.table, "snapshot_id": summary.dataset.snapshot_id},
        "row_count": summary.row_count,
        "chunk_count": summary.chunk_count,
        "replayed_chunk_count": summary.replayed_chunk_count,
        "manifest_replayed": summary.manifest_replayed,
        "replayed": summary.replayed,
        "evidence": [
            {"stream": ref.stream.value, "record_count": ref.record_count}
            for ref in summary.evidence
        ],
    }


def request_document(request: DatasetEvidenceRequest) -> dict[str, Any]:
    """The JSON request document of ``request`` (``request_from_document`` reads it back)."""
    universe = request.universe
    if REGISTERED_UNIVERSES.get((universe.name, universe.version)) != universe:
        raise DatasetJobRequestError(
            f"universe {universe.name}@{universe.version} is not a registered universe"
        )
    return {
        "universe": f"{universe.name}@{universe.version}",
        "pit": request.pit.model_dump(mode="json"),
        "data_type": request.data_type,
        "start": _utc(request.start, "start").isoformat(),
        "end": _utc(request.end, "end").isoformat(),
    }


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DatasetJobRequestError(f"{name} must carry a UTC offset")
    return value.astimezone(UTC)


def _instant(document: Mapping[str, Any], name: str) -> datetime:
    text = document[name]
    if not isinstance(text, str):
        raise DatasetJobRequestError(f"{name} must be an ISO 8601 string")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise DatasetJobRequestError(f"{name} is not an ISO 8601 timestamp") from None
    return _utc(value, name)


def _plain(value: Any) -> Any:
    """The plain JSON form of a (possibly frozen) bus payload value."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def request_from_document(document: object) -> DatasetEvidenceRequest:
    """Rebuild the request of a job document; every field is required, none is defaulted."""
    if not isinstance(document, Mapping):
        raise DatasetJobRequestError("the request must be a JSON object")
    keys = set(document)
    if keys != REQUEST_FIELDS:
        missing, unknown = sorted(REQUEST_FIELDS - keys), sorted(keys - REQUEST_FIELDS)
        raise DatasetJobRequestError(f"request fields: missing {missing}, unknown {unknown}")
    name, separator, version = str(document["universe"]).rpartition("@")
    universe = REGISTERED_UNIVERSES.get((name, version)) if separator else None
    if universe is None or not isinstance(document["universe"], str):
        raise DatasetJobRequestError("universe must be NAME@VERSION of a registered universe")
    data_type = document["data_type"]
    if not isinstance(data_type, str) or data_type not in rules.CANONICAL_TABLES:
        raise DatasetJobRequestError(f"data_type must be one of {sorted(rules.CANONICAL_TABLES)}")
    if not isinstance(document["pit"], Mapping):
        raise DatasetJobRequestError("pit must be a PointInTimeSpec JSON object")
    pit_document = _plain(document["pit"])
    try:
        pit = PointInTimeSpec.model_validate(pit_document)
    except (ValidationError, ValueError, TypeError):
        raise DatasetJobRequestError("pit is not a valid PointInTimeSpec") from None
    if canonical_json(pit.model_dump(mode="json")) != canonical_json(pit_document):
        raise DatasetJobRequestError("pit is not in its canonical JSON form")
    return DatasetEvidenceRequest(
        universe=universe,
        pit=pit,
        data_type=data_type,
        start=_instant(document, "start"),
        end=_instant(document, "end"),
    )


# ------------------------------------------------------------------------- the port


class PipelineDatasetJobPort:
    """``apps.worker.dataset_job.DatasetBuildPort`` over one open ``DatasetBuildPipeline``.

    The pipeline (and the resources behind it) belong to the caller, normally the deployment's
    runtime factory inside ``open_dataset_pipeline``.
    """

    def __init__(self, pipeline: DatasetBuildPipeline, profile: DatasetBuildProfile) -> None:
        self._pipeline = pipeline
        self._profile = profile

    def selection_id(self, request: Mapping[str, Any]) -> str:
        """The ``selection_id`` the request builds (the job's idempotency key); no write."""
        return self._pipeline.builder.selection_id(request_from_document(request))

    def build(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Build (or replay) the request's v3 Dataset; return the fixed-size JSON summary."""
        evidence_request = request_from_document(request)
        summary = self._pipeline.build(evidence_request)
        return summary_document(self._profile, evidence_request.pit, summary)
