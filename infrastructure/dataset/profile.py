"""Explicit run configuration of the v3 Dataset production entry (ADR-0101 D1).

``DatasetBuildProfile`` carries every number the v3 Dataset pipeline needs and that ADR-0077
leaves to the caller (DQ-9 stays OPEN): the Dataset rule, the PIT and Universe sorted-run bounds
and the Quality join / report bounds. It has no default value of its own; ``load_dataset_profile``
reads it from one strict JSON file and rejects a missing field, an unknown field, a duplicate key,
a non-integer number and anything the downstream limit types reject. ``profile_hash`` is the
content hash of the profile's canonical document, so it does not depend on key order or spacing.

Schema 1.1.0 (ADR-0101 修订 1 §2, additive) adds the optional ``listing_quality`` section: the
``ListingHistoryQualityReporterV2`` bounds the partition ``quality`` section does not carry. A
1.0.x document must not have it; a profile without it serves every command except the listing
report (which refuses and says why). A profile without the section hashes exactly as before.

Nothing here selects a value, opens a catalog or touches the network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from core.domain.base import SEMVER_PATTERN, content_hash
from infrastructure.catalog.bounded_metadata import BoundedMetadataError, BoundedMetadataLimits
from infrastructure.dataset.builder import DatasetEvidenceRule, dataset_evidence_rule
from infrastructure.pit.selector import PitRunParams
from infrastructure.quality.report_streams import (
    QualityReportStreamError,
    QualityReportStreamLimits,
)
from infrastructure.streaming.content_key_tree import ContentKeyTreeError, KeyTreeParams
from infrastructure.streaming.runs import RunLimits
from infrastructure.universe.run_params import UniverseRunParams

__all__ = [
    "PROFILE_SCHEMA_MAJOR",
    "DatasetBuildProfile",
    "DatasetProfileError",
    "LISTING_QUALITY_SINCE_MINOR",
    "ListingQualityLimits",
    "QualityLimits",
    "QualityReporterLimits",
    "load_dataset_profile",
]

#: The profile document major this code reads; any other major is rejected (ADR-0101 D1).
PROFILE_SCHEMA_MAJOR: Final = 1
#: The first profile minor that may carry ``listing_quality`` (ADR-0101 修订 1 §2).
LISTING_QUALITY_SINCE_MINOR: Final = 1
#: A profile is configuration, never data: refuse to read a file larger than this.
_MAX_PROFILE_BYTES: Final = 1 << 20

_SEMVER: Final = re.compile(SEMVER_PATTERN)


class DatasetProfileError(ValueError):
    """A Dataset build profile is missing, malformed, or carries a rejected value."""


# ------------------------------------------------------------------------- typed profile


def _integer(name: str, value: object, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DatasetProfileError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class QualityReporterLimits:
    """The ``QualityReporterV3`` record-byte and retry bounds not shared with the Quality join."""

    max_event_record_bytes: int
    max_revision_record_bytes: int
    max_gap_record_bytes: int
    max_input_record_bytes: int
    max_manifest_record_bytes: int
    retries: int

    def __post_init__(self) -> None:
        for name in (
            "max_event_record_bytes",
            "max_revision_record_bytes",
            "max_gap_record_bytes",
            "max_input_record_bytes",
            "max_manifest_record_bytes",
            "retries",
        ):
            _integer(f"quality.reporter.{name}", getattr(self, name), 1)


@dataclass(frozen=True, slots=True)
class QualityLimits:
    """Quality bounds: ``BoundedQualitySourceParams`` (the Dataset join) and ``QualityReporterV3``.

    The stream / run limits, capacity, fanout and identity byte bound are shared by both users, as
    in the capacity probe; ``reporter`` holds what only the report writer takes.
    """

    stream_limits: QualityReportStreamLimits
    run_limits: RunLimits
    run_capacity: int
    merge_fanout: int
    max_run_object_bytes: int
    max_identity_bytes: int
    reporter: QualityReporterLimits

    def __post_init__(self) -> None:
        if not isinstance(self.stream_limits, QualityReportStreamLimits):
            raise DatasetProfileError("quality.stream_limits must be QualityReportStreamLimits")
        if not isinstance(self.run_limits, RunLimits):
            raise DatasetProfileError("quality.run_limits must be RunLimits")
        if not isinstance(self.reporter, QualityReporterLimits):
            raise DatasetProfileError("quality.reporter must be QualityReporterLimits")
        _integer("quality.run_capacity", self.run_capacity, 1)
        _integer("quality.merge_fanout", self.merge_fanout, 2)
        _integer("quality.max_run_object_bytes", self.max_run_object_bytes, 1)
        _integer("quality.max_identity_bytes", self.max_identity_bytes, 1)


@dataclass(frozen=True, slots=True)
class ListingQualityLimits:
    """``ListingHistoryQualityReporterV2`` bounds not in the ``quality`` section (修订 1 §2).

    The run / stream limits, capacity, fanout, run-object and identity bytes, record-byte bounds
    and retries are shared with ``quality`` (the capacity probe's mapping); these are the rest.
    """

    metadata_limits: BoundedMetadataLimits
    max_record_bytes: int
    prefix_leaf_max_records: int
    prefix_fanout: int
    prefix_max_node_bytes: int
    prefix_max_record_bytes: int
    row_chunk_capacity: int
    max_hash_chunk_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.metadata_limits, BoundedMetadataLimits):
            raise DatasetProfileError(
                "listing_quality.metadata_limits must be BoundedMetadataLimits"
            )
        for name in _LISTING_FIELDS:
            _integer(f"listing_quality.{name}", getattr(self, name), _LISTING_MINIMUMS.get(name, 1))


@dataclass(frozen=True, slots=True)
class DatasetBuildProfile:
    """Everything the v3 Dataset pipeline needs from its operator; no value has a default.

    ``capacity_evidence`` is an optional operator reference to the capacity measurement that
    justifies these numbers; without it the entry reports ``capacity_evidence=none`` (the profile
    is then explicitly not an accepted configuration).
    """

    schema_version: str
    rule: DatasetEvidenceRule
    pit: PitRunParams
    universe: UniverseRunParams
    quality: QualityLimits
    capacity_evidence: str | None = None
    listing_quality: ListingQualityLimits | None = None

    def __post_init__(self) -> None:
        _check_schema_version(self.schema_version)
        if self.listing_quality is not None:
            if not isinstance(self.listing_quality, ListingQualityLimits):
                raise DatasetProfileError("listing_quality must be ListingQualityLimits")
            if int(self.schema_version.split(".")[1]) < LISTING_QUALITY_SINCE_MINOR:
                raise DatasetProfileError(
                    f"listing_quality needs profile schema_version >= "
                    f"{PROFILE_SCHEMA_MAJOR}.{LISTING_QUALITY_SINCE_MINOR}.0"
                )
        if self.capacity_evidence is not None and (
            not isinstance(self.capacity_evidence, str) or not self.capacity_evidence.strip()
        ):
            raise DatasetProfileError("capacity_evidence must be a non-blank string when given")

    def to_document(self) -> dict[str, Any]:
        """The profile as the plain JSON document ``load_dataset_profile`` reads back."""
        limits = self.rule.limits
        document: dict[str, Any] = {
            "schema_version": self.schema_version,
            "rule": {
                "chunk_rows": self.rule.chunk_rows,
                "leaf_max_records": limits.leaf_max_records,
                "leaf_max_bytes": limits.leaf_max_bytes,
                "fanout": limits.fanout,
            },
            "pit": {
                "row_batch_rows": self.pit.row_batch_rows,
                "edge_batch_rows": self.pit.edge_batch_rows,
                "merge_fanout": self.pit.merge_fanout,
                "key_history_buffer": self.pit.key_history_buffer,
                "run_limits": _limits_document(self.pit.limits),
            },
            "universe": {
                "capacity": self.universe.capacity,
                "merge_fanout": self.universe.merge_fanout,
                "run_limits": _limits_document(self.universe.limits),
            },
            "quality": {
                "stream_limits": _limits_document(self.quality.stream_limits),
                "run_limits": _limits_document(self.quality.run_limits),
                "run_capacity": self.quality.run_capacity,
                "merge_fanout": self.quality.merge_fanout,
                "max_run_object_bytes": self.quality.max_run_object_bytes,
                "max_identity_bytes": self.quality.max_identity_bytes,
                "reporter": {
                    name: getattr(self.quality.reporter, name) for name in _REPORTER_FIELDS
                },
            },
        }
        if self.capacity_evidence is not None:
            document["capacity_evidence"] = self.capacity_evidence
        if self.listing_quality is not None:
            listing = self.listing_quality
            metadata = listing.metadata_limits
            document["listing_quality"] = {
                "metadata": {
                    **{name: getattr(metadata, name) for name in _METADATA_FIELDS},
                    "run_limits": _limits_document(metadata.run_limits),
                    "key_tree": {
                        name: getattr(metadata.key_tree_params, name) for name in _KEY_TREE_FIELDS
                    },
                },
                **{name: getattr(listing, name) for name in _LISTING_FIELDS},
            }
        return document

    def profile_hash(self) -> str:
        """The content hash of the canonical profile document (key order never matters)."""
        return content_hash(self.to_document())


def _limits_document(limits: RunLimits | QualityReportStreamLimits) -> dict[str, int]:
    return {
        "leaf_max_records": limits.leaf_max_records,
        "leaf_max_bytes": limits.leaf_max_bytes,
        "fanout": limits.fanout,
    }


def _check_schema_version(value: object) -> None:
    if not isinstance(value, str) or _SEMVER.match(value) is None:
        raise DatasetProfileError(f"schema_version must be a SemVer string, got {value!r}")
    if int(value.split(".", 1)[0]) != PROFILE_SCHEMA_MAJOR:
        raise DatasetProfileError(
            f"profile schema_version {value} is not supported (major {PROFILE_SCHEMA_MAJOR} only)"
        )


# ------------------------------------------------------------------------- strict JSON loading

_LIMIT_FIELDS: Final = ("leaf_max_records", "leaf_max_bytes", "fanout")
_RULE_FIELDS: Final = ("chunk_rows", *_LIMIT_FIELDS)
_PIT_FIELDS: Final = ("row_batch_rows", "edge_batch_rows", "merge_fanout", "key_history_buffer")
_REPORTER_FIELDS: Final = (
    "max_event_record_bytes",
    "max_revision_record_bytes",
    "max_gap_record_bytes",
    "max_input_record_bytes",
    "max_manifest_record_bytes",
    "retries",
)
_TOP_REQUIRED: Final = frozenset({"schema_version", "rule", "pit", "universe", "quality"})
_TOP_OPTIONAL: Final = frozenset({"capacity_evidence", "listing_quality"})
_METADATA_FIELDS: Final = (
    "max_metadata_bytes",
    "max_item_bytes",
    "max_retained_json_bytes",
    "read_chunk_bytes",
    "max_small_array_items",
    "max_map_items",
    "max_snapshots",
    "run_capacity",
    "run_merge_fanout",
)
_KEY_TREE_FIELDS: Final = ("page_max_bytes", "leaf_max_records", "fanout")
_LISTING_FIELDS: Final = (
    "max_record_bytes",
    "prefix_leaf_max_records",
    "prefix_fanout",
    "prefix_max_node_bytes",
    "prefix_max_record_bytes",
    "row_chunk_capacity",
    "max_hash_chunk_bytes",
)
#: The reporter's own minimums (``ListingHistoryQualityReporterV2.__init__``); others are 1.
_LISTING_MINIMUMS: Final = {"prefix_fanout": 2, "max_hash_chunk_bytes": 16}


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetProfileError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise DatasetProfileError(f"{token} is not valid JSON")


def _exact(
    where: str,
    value: object,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DatasetProfileError(f"{where} must be a JSON object")
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise DatasetProfileError(f"{where} is missing {missing}")
    if unknown:
        raise DatasetProfileError(f"{where} has unknown fields {unknown}")
    return value


def _ints(where: str, value: object, names: tuple[str, ...]) -> dict[str, int]:
    document = _exact(where, value, frozenset(names))
    return {name: _integer(f"{where}.{name}", document[name], 0) for name in names}


def _run_limits(where: str, value: object) -> RunLimits:
    try:
        return RunLimits(**_ints(where, value, _LIMIT_FIELDS))
    except ValueError as exc:  # RunWriteError
        raise DatasetProfileError(f"{where}: {exc}") from exc


def _stream_limits(where: str, value: object) -> QualityReportStreamLimits:
    try:
        return QualityReportStreamLimits(**_ints(where, value, _LIMIT_FIELDS))
    except (ValueError, QualityReportStreamError) as exc:
        raise DatasetProfileError(f"{where}: {exc}") from exc


def _rule(value: object) -> DatasetEvidenceRule:
    try:
        return dataset_evidence_rule(**_ints("rule", value, _RULE_FIELDS))
    except ValueError as exc:  # DatasetSpecError
        raise DatasetProfileError(f"rule: {exc}") from exc


def _pit(value: object) -> PitRunParams:
    document = _exact("pit", value, frozenset({*_PIT_FIELDS, "run_limits"}))
    numbers = _ints("pit", {name: document[name] for name in _PIT_FIELDS}, _PIT_FIELDS)
    limits = _run_limits("pit.run_limits", document["run_limits"])
    try:
        return PitRunParams(**numbers, limits=limits)
    except ValueError as exc:
        raise DatasetProfileError(f"pit: {exc}") from exc


def _universe(value: object) -> UniverseRunParams:
    document = _exact("universe", value, frozenset({"capacity", "merge_fanout", "run_limits"}))
    numbers = _ints(
        "universe",
        {name: document[name] for name in ("capacity", "merge_fanout")},
        ("capacity", "merge_fanout"),
    )
    limits = _run_limits("universe.run_limits", document["run_limits"])
    try:
        return UniverseRunParams(**numbers, limits=limits)
    except ValueError as exc:
        raise DatasetProfileError(f"universe: {exc}") from exc


def _quality(value: object) -> QualityLimits:
    scalars = ("run_capacity", "merge_fanout", "max_run_object_bytes", "max_identity_bytes")
    document = _exact(
        "quality",
        value,
        frozenset({"stream_limits", "run_limits", "reporter", *scalars}),
    )
    numbers = _ints("quality", {name: document[name] for name in scalars}, scalars)
    reporter = QualityReporterLimits(
        **_ints("quality.reporter", document["reporter"], _REPORTER_FIELDS)
    )
    return QualityLimits(
        stream_limits=_stream_limits("quality.stream_limits", document["stream_limits"]),
        run_limits=_run_limits("quality.run_limits", document["run_limits"]),
        reporter=reporter,
        **numbers,
    )


def _listing_quality(value: object) -> ListingQualityLimits:
    document = _exact("listing_quality", value, frozenset({"metadata", *_LISTING_FIELDS}))
    metadata = _exact(
        "listing_quality.metadata",
        document["metadata"],
        frozenset({*_METADATA_FIELDS, "run_limits", "key_tree"}),
    )
    numbers = _ints(
        "listing_quality.metadata",
        {name: metadata[name] for name in _METADATA_FIELDS},
        _METADATA_FIELDS,
    )
    tree = _ints("listing_quality.metadata.key_tree", metadata["key_tree"], _KEY_TREE_FIELDS)
    try:
        limits = BoundedMetadataLimits(
            **numbers,
            run_limits=_run_limits("listing_quality.metadata.run_limits", metadata["run_limits"]),
            key_tree_params=KeyTreeParams(**tree),
        )
    except (BoundedMetadataError, ContentKeyTreeError, ValueError) as exc:
        raise DatasetProfileError(f"listing_quality.metadata: {exc}") from exc
    return ListingQualityLimits(
        metadata_limits=limits,
        **_ints(
            "listing_quality",
            {name: document[name] for name in _LISTING_FIELDS},
            _LISTING_FIELDS,
        ),
    )


def _read_document(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        raise DatasetProfileError(f"profile {path} cannot be read ({type(exc).__name__})") from exc
    if len(raw) > _MAX_PROFILE_BYTES:
        raise DatasetProfileError(f"profile {path} is larger than {_MAX_PROFILE_BYTES} bytes")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetProfileError(f"profile {path} is not valid UTF-8 JSON ({exc})") from exc


def load_dataset_profile(path: str | Path) -> DatasetBuildProfile:
    """Read one strict JSON profile; every field is required, nothing has a default.

    Raises ``DatasetProfileError`` for an unreadable or oversized file, invalid / duplicate-key
    JSON, a missing or unknown field, a non-integer number (``true`` and floats included), an
    unsupported ``schema_version`` major, or any value the downstream limit types reject.
    """
    document = _exact("profile", _read_document(Path(path)), _TOP_REQUIRED, _TOP_OPTIONAL)
    _check_schema_version(document["schema_version"])
    evidence = document.get("capacity_evidence")
    if "capacity_evidence" in document and not isinstance(evidence, str):
        raise DatasetProfileError("capacity_evidence must be a string when present")
    return DatasetBuildProfile(
        schema_version=document["schema_version"],
        rule=_rule(document["rule"]),
        pit=_pit(document["pit"]),
        universe=_universe(document["universe"]),
        quality=_quality(document["quality"]),
        capacity_evidence=evidence,
        listing_quality=(
            _listing_quality(document["listing_quality"]) if "listing_quality" in document else None
        ),
    )
