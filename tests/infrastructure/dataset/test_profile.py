"""``DatasetBuildProfile`` / ``load_dataset_profile`` (ADR-0101 D1): strict, default-free JSON."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from core.domain.base import content_hash
from infrastructure.dataset.builder import dataset_evidence_rule
from infrastructure.dataset.profile import (
    PROFILE_SCHEMA_MAJOR,
    DatasetBuildProfile,
    DatasetProfileError,
    QualityLimits,
    QualityReporterLimits,
    load_dataset_profile,
)
from infrastructure.pit.selector import PitRunParams
from infrastructure.quality.report_streams import QualityReportStreamLimits
from infrastructure.streaming.runs import RunLimits
from infrastructure.universe.run_params import UniverseRunParams
from tests.infrastructure.dataset.entry_support import (
    PROFILE_DOCUMENT,
    listing_profile_document,
    profile_document,
    write_profile,
)


def _paths(node: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every path of the profile document, objects and leaves alike."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield (*prefix, key)
            yield from _paths(value, (*prefix, key))


ALL_PATHS = sorted(_paths(PROFILE_DOCUMENT))


def _get(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = document
    for key in path:
        node = node[key]
    return node


NUMERIC_PATHS = [
    p
    for p in ALL_PATHS
    if not isinstance(_get(PROFILE_DOCUMENT, p), dict) and p != ("schema_version",)
]
OBJECT_PATHS = [(), *[p for p in ALL_PATHS if isinstance(_get(PROFILE_DOCUMENT, p), dict)]]


def _loaded(tmp_path: Path, document: Any) -> DatasetBuildProfile:
    return load_dataset_profile(write_profile(tmp_path / "p.json", document))


def _rejects(tmp_path: Path, document: Any, match: str | None = None) -> None:
    with pytest.raises(DatasetProfileError, match=match):
        _loaded(tmp_path, document)


# ------------------------------------------------------------------------- the success path


def test_valid_profile_loads_every_typed_value(tmp_path: Path) -> None:
    profile = _loaded(tmp_path, profile_document())
    assert profile.schema_version == "1.0.0"
    assert profile.rule == dataset_evidence_rule(
        chunk_rows=2, leaf_max_records=2, leaf_max_bytes=16384, fanout=2
    )
    assert profile.pit == PitRunParams(
        row_batch_rows=1,
        edge_batch_rows=1,
        merge_fanout=2,
        key_history_buffer=1,
        limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
    )
    assert profile.universe == UniverseRunParams(
        capacity=2,
        merge_fanout=3,
        limits=RunLimits(leaf_max_records=8, leaf_max_bytes=4096, fanout=3),
    )
    quality = profile.quality
    assert quality.stream_limits == QualityReportStreamLimits(2, 8192, 2)
    assert quality.run_limits == RunLimits(2, 4096, 2)
    assert (quality.run_capacity, quality.merge_fanout) == (2, 2)
    assert (quality.max_run_object_bytes, quality.max_identity_bytes) == (8192, 8192)
    assert quality.reporter == QualityReporterLimits(4096, 1024, 4096, 8192, 32768, 2)
    assert profile.capacity_evidence is None


def test_profile_is_frozen(tmp_path: Path) -> None:
    profile = _loaded(tmp_path, profile_document())
    with pytest.raises(FrozenInstanceError):
        profile.capacity_evidence = "x"  # type: ignore[misc]


def test_document_round_trips_and_hash_is_the_canonical_content_hash(tmp_path: Path) -> None:
    profile = _loaded(tmp_path, profile_document())
    assert profile.to_document() == PROFILE_DOCUMENT
    assert profile.profile_hash() == content_hash(PROFILE_DOCUMENT)
    again = _loaded(tmp_path, profile.to_document())
    assert again == profile and again.profile_hash() == profile.profile_hash()


def test_hash_ignores_key_order_and_spacing_but_not_values(tmp_path: Path) -> None:
    reference = _loaded(tmp_path, profile_document()).profile_hash()
    shuffled = dict(reversed(list(profile_document().items())))
    path = tmp_path / "spaced.json"
    path.write_text(json.dumps(shuffled, indent=7), encoding="utf-8")
    assert load_dataset_profile(path).profile_hash() == reference
    path.write_text(json.dumps(profile_document(), sort_keys=True, separators=(",", ":")))
    assert load_dataset_profile(str(path)).profile_hash() == reference
    changed = profile_document()
    changed["rule"]["chunk_rows"] = 3
    assert _loaded(tmp_path, changed).profile_hash() != reference
    changed = profile_document()
    changed["quality"]["reporter"]["retries"] = 3
    assert _loaded(tmp_path, changed).profile_hash() != reference


def test_capacity_evidence_is_optional_and_part_of_the_hash(tmp_path: Path) -> None:
    without = _loaded(tmp_path, profile_document())
    document = profile_document()
    document["capacity_evidence"] = "docs/evidence/capacity-2026-10.md"
    with_evidence = _loaded(tmp_path, document)
    assert with_evidence.capacity_evidence == "docs/evidence/capacity-2026-10.md"
    assert with_evidence.profile_hash() != without.profile_hash()
    assert with_evidence.to_document() == document


@pytest.mark.parametrize("value", [None, "", "   ", 7, True, ["x"]])
def test_capacity_evidence_must_be_a_non_blank_string_when_present(
    tmp_path: Path, value: Any
) -> None:
    document = profile_document()
    document["capacity_evidence"] = value
    _rejects(tmp_path, document, "capacity_evidence")


def test_programmatic_profile_rejects_blank_evidence(tmp_path: Path) -> None:
    profile = _loaded(tmp_path, profile_document())
    with pytest.raises(DatasetProfileError, match="capacity_evidence"):
        replace(profile, capacity_evidence=" ")


# ------------------------------------------------------------------------- field-level rejection


@pytest.mark.parametrize("path", ALL_PATHS, ids=[".".join(p) for p in ALL_PATHS])
def test_a_missing_field_is_rejected(tmp_path: Path, path: tuple[str, ...]) -> None:
    document = profile_document()
    del _get(document, path[:-1])[path[-1]]
    _rejects(tmp_path, document, "missing")


@pytest.mark.parametrize("path", OBJECT_PATHS, ids=[".".join(p) or "top" for p in OBJECT_PATHS])
def test_an_unknown_field_is_rejected(tmp_path: Path, path: tuple[str, ...]) -> None:
    document = profile_document()
    _get(document, path)["surprise"] = 1
    _rejects(tmp_path, document, "unknown fields")


@pytest.mark.parametrize("path", NUMERIC_PATHS, ids=[".".join(p) for p in NUMERIC_PATHS])
@pytest.mark.parametrize("value", [True, 2.0, 1.5, "2", None, [2]], ids=repr)
def test_a_non_integer_number_is_rejected(
    tmp_path: Path, path: tuple[str, ...], value: Any
) -> None:
    document = profile_document()
    _get(document, path[:-1])[path[-1]] = value
    _rejects(tmp_path, document)


@pytest.mark.parametrize("path", NUMERIC_PATHS, ids=[".".join(p) for p in NUMERIC_PATHS])
def test_a_negative_number_is_rejected(tmp_path: Path, path: tuple[str, ...]) -> None:
    document = profile_document()
    _get(document, path[:-1])[path[-1]] = -1
    _rejects(tmp_path, document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("rule", "chunk_rows"), 0),
        (("rule", "leaf_max_records"), 0),
        (("rule", "leaf_max_bytes"), 0),
        (("rule", "fanout"), 1),
        (("pit", "row_batch_rows"), 0),
        (("pit", "edge_batch_rows"), 0),
        (("pit", "merge_fanout"), 1),
        (("pit", "key_history_buffer"), 0),
        (("pit", "run_limits", "leaf_max_records"), 0),
        (("pit", "run_limits", "leaf_max_bytes"), 1),
        (("pit", "run_limits", "fanout"), 1),
        (("universe", "capacity"), 0),
        (("universe", "merge_fanout"), 1),
        (("universe", "run_limits", "fanout"), 1),
        (("universe", "run_limits", "leaf_max_bytes"), 1),
        (("quality", "stream_limits", "leaf_max_records"), 0),
        (("quality", "stream_limits", "leaf_max_bytes"), 0),
        (("quality", "stream_limits", "fanout"), 1),
        (("quality", "run_limits", "leaf_max_records"), 0),
        (("quality", "run_limits", "leaf_max_bytes"), 1),
        (("quality", "run_limits", "fanout"), 1),
        (("quality", "run_capacity"), 0),
        (("quality", "merge_fanout"), 1),
        (("quality", "max_run_object_bytes"), 0),
        (("quality", "max_identity_bytes"), 0),
        (("quality", "reporter", "max_event_record_bytes"), 0),
        (("quality", "reporter", "max_revision_record_bytes"), 0),
        (("quality", "reporter", "max_gap_record_bytes"), 0),
        (("quality", "reporter", "max_input_record_bytes"), 0),
        (("quality", "reporter", "max_manifest_record_bytes"), 0),
        (("quality", "reporter", "retries"), 0),
    ],
    ids=lambda value: ".".join(value) if isinstance(value, tuple) else str(value),
)
def test_a_value_below_the_downstream_minimum_is_rejected(
    tmp_path: Path, path: tuple[str, ...], value: int
) -> None:
    document = profile_document()
    _get(document, path[:-1])[path[-1]] = value
    _rejects(tmp_path, document)


def test_the_boundary_values_are_accepted(tmp_path: Path) -> None:
    document = profile_document()
    document["rule"].update(chunk_rows=1, leaf_max_records=1, leaf_max_bytes=1, fanout=2)
    document["pit"].update(
        row_batch_rows=1, edge_batch_rows=1, merge_fanout=2, key_history_buffer=1
    )
    document["quality"].update(run_capacity=1, merge_fanout=2, max_identity_bytes=1)
    document["quality"]["reporter"]["retries"] = 1
    profile = _loaded(tmp_path, document)
    assert profile.rule.chunk_rows == 1 and profile.quality.reporter.retries == 1


# ------------------------------------------------------------------------- schema_version


@pytest.mark.parametrize("version", ["2.0.0", "0.9.0", "10.0.0"])
def test_another_schema_major_is_rejected(tmp_path: Path, version: str) -> None:
    document = profile_document()
    document["schema_version"] = version
    _rejects(tmp_path, document, "not supported")


@pytest.mark.parametrize("version", ["1", "1.0", "v1.0.0", "", "1.0.0.0", 1, None, 1.0])
def test_a_non_semver_schema_version_is_rejected(tmp_path: Path, version: Any) -> None:
    document = profile_document()
    document["schema_version"] = version
    _rejects(tmp_path, document, "SemVer")


def test_a_minor_or_patch_of_the_supported_major_loads(tmp_path: Path) -> None:
    document = profile_document()
    document["schema_version"] = f"{PROFILE_SCHEMA_MAJOR}.4.2"
    assert _loaded(tmp_path, document).schema_version == "1.4.2"


# ------------------------------------------------------------------------- file and JSON level


def test_a_missing_file_or_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetProfileError, match="cannot be read"):
        load_dataset_profile(tmp_path / "absent.json")
    with pytest.raises(DatasetProfileError, match="cannot be read"):
        load_dataset_profile(tmp_path)


def test_oversized_file_is_rejected_unparsed(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * ((1 << 20) + 1))
    with pytest.raises(DatasetProfileError, match="larger than"):
        load_dataset_profile(path)


@pytest.mark.parametrize(
    "text",
    ["", "{", "not json", "[]", "null", "7", '"s"', "{'a': 1}", '{"a": 1,}'],
)
def test_malformed_or_non_object_json_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(DatasetProfileError):
        load_dataset_profile(path)


def test_non_utf8_bytes_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(b'{"schema_version": "\xff"}')
    with pytest.raises(DatasetProfileError, match="UTF-8 JSON"):
        load_dataset_profile(path)


def test_a_duplicate_key_is_rejected_at_any_depth(tmp_path: Path) -> None:
    text = json.dumps(profile_document())
    top = text.replace(
        '"schema_version": "1.0.0"', '"schema_version": "1.0.0", "schema_version": "1.0.0"'
    )
    nested = text.replace('"chunk_rows": 2', '"chunk_rows": 2, "chunk_rows": 5')
    for broken in (top, nested):
        assert broken != text
        path = tmp_path / "dup.json"
        path.write_text(broken, encoding="utf-8")
        with pytest.raises(DatasetProfileError, match="duplicate JSON key"):
            load_dataset_profile(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_constants_are_rejected(tmp_path: Path, constant: str) -> None:
    path = tmp_path / "nan.json"
    path.write_text(
        json.dumps(profile_document()).replace('"chunk_rows": 2', f'"chunk_rows": {constant}')
    )
    with pytest.raises(DatasetProfileError, match="not valid JSON"):
        load_dataset_profile(path)


def test_errors_never_embed_file_content_beyond_the_offending_field(tmp_path: Path) -> None:
    document = profile_document()
    document["capacity_evidence"] = 12345
    path = write_profile(tmp_path / "p.json", document)
    with pytest.raises(DatasetProfileError) as caught:
        load_dataset_profile(path)
    assert "leaf_max_bytes" not in str(caught.value)


# ------------------------------------------------------------------------- direct construction


def test_quality_types_validate_their_own_arguments() -> None:
    reporter = QualityReporterLimits(1, 1, 1, 1, 1, 1)
    with pytest.raises(DatasetProfileError, match="retries"):
        QualityReporterLimits(1, 1, 1, 1, 1, 0)
    with pytest.raises(DatasetProfileError, match="stream_limits"):
        QualityLimits(
            stream_limits=RunLimits(1, 4096, 2),  # type: ignore[arg-type]
            run_limits=RunLimits(1, 4096, 2),
            run_capacity=1,
            merge_fanout=2,
            max_run_object_bytes=1,
            max_identity_bytes=1,
            reporter=reporter,
        )
    with pytest.raises(DatasetProfileError, match="run_limits"):
        QualityLimits(
            stream_limits=QualityReportStreamLimits(1, 1, 2),
            run_limits=None,  # type: ignore[arg-type]
            run_capacity=1,
            merge_fanout=2,
            max_run_object_bytes=1,
            max_identity_bytes=1,
            reporter=reporter,
        )
    with pytest.raises(DatasetProfileError, match="reporter"):
        QualityLimits(
            stream_limits=QualityReportStreamLimits(1, 1, 2),
            run_limits=RunLimits(1, 4096, 2),
            run_capacity=1,
            merge_fanout=2,
            max_run_object_bytes=1,
            max_identity_bytes=1,
            reporter=None,  # type: ignore[arg-type]
        )


# ----------------------------------------------------------- listing_quality (修订 1 §2)


def test_listing_quality_section_loads_and_round_trips(tmp_path: Path) -> None:
    document = listing_profile_document()
    profile = _loaded(tmp_path, document)
    listing = profile.listing_quality
    assert listing is not None
    assert listing.metadata_limits.max_snapshots == 5000
    assert listing.metadata_limits.key_tree_params.page_max_bytes == 1024 * 1024
    assert listing.metadata_limits.run_limits.leaf_max_records == 32
    assert listing.prefix_fanout == 2 and listing.max_hash_chunk_bytes == 64 * 1024
    assert profile.to_document() == document
    assert profile.profile_hash() == content_hash(document)


def test_a_profile_without_listing_quality_hashes_as_before(tmp_path: Path) -> None:
    document = profile_document()
    profile = _loaded(tmp_path, document)
    assert profile.listing_quality is None
    assert "listing_quality" not in profile.to_document()
    assert profile.profile_hash() == content_hash(document)


@pytest.mark.parametrize("version", ["1.0.0", "1.0.7"])
def test_listing_quality_needs_schema_1_1(tmp_path: Path, version: str) -> None:
    document = listing_profile_document()
    document["schema_version"] = version
    _rejects(tmp_path, document, "listing_quality needs profile schema_version >= 1.1.0")


@pytest.mark.parametrize(
    "path",
    [
        ("listing_quality", "metadata"),
        ("listing_quality", "prefix_fanout"),
        ("listing_quality", "metadata", "key_tree"),
        ("listing_quality", "metadata", "run_limits", "fanout"),
        ("listing_quality", "metadata", "max_snapshots"),
    ],
)
def test_a_missing_listing_field_is_rejected(tmp_path: Path, path: tuple[str, ...]) -> None:
    document = listing_profile_document()
    *parents, leaf = path
    node = document
    for key in parents:
        node = node[key]
    del node[leaf]
    _rejects(tmp_path, document, "missing")


@pytest.mark.parametrize(
    "path", [("listing_quality",), ("listing_quality", "metadata", "key_tree")]
)
def test_an_unknown_listing_field_is_rejected(tmp_path: Path, path: tuple[str, ...]) -> None:
    document = listing_profile_document()
    node = document
    for key in path:
        node = node[key]
    node["surprise"] = 1
    _rejects(tmp_path, document, "unknown fields")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("listing_quality", "prefix_fanout"), 1),
        (("listing_quality", "max_hash_chunk_bytes"), 15),
        (("listing_quality", "row_chunk_capacity"), 0),
        (("listing_quality", "metadata", "run_merge_fanout"), 1),
        (("listing_quality", "metadata", "key_tree", "fanout"), 1),
        (("listing_quality", "max_record_bytes"), True),
        (("listing_quality", "max_record_bytes"), 1.5),
    ],
)
def test_a_listing_value_below_its_minimum_is_rejected(
    tmp_path: Path, path: tuple[str, ...], value: Any
) -> None:
    document = listing_profile_document()
    *parents, leaf = path
    node = document
    for key in parents:
        node = node[key]
    node[leaf] = value
    with pytest.raises(DatasetProfileError):
        load_dataset_profile(write_profile(tmp_path / "profile.json", document))
