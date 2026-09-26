"""Outcome table persistence (Phase 4 debugging pass, 2026-09-26; ``research.outcomes.store``).

Proven here: ``materialize`` records the result's request / provider hashes while ``rows()`` is
unchanged; a stored table loads back equal and fully re-verified (table hash recomputed, the
``OutcomeResult`` rebuilt so its ``result_hash`` check re-runs); tampering, truncation,
non-canonical text, a forged hash and a symlink are refused; an existing file is never
overwritten (identical rewrite = no-op, different bytes = conflict); outcomes stay labels.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.feature import FeatureObservation
from core.contracts.outcome import OutcomeEvent, OutcomeRequest, is_outcome_payload
from core.domain.base import canonical_json, content_hash
from research.outcomes import (
    OutcomeTable,
    OutcomeTableConflict,
    OutcomeTableCorrupted,
    OutcomeTableStore,
    bars_from_synthetic,
    materialize,
    table_hash,
)
from tests.research.validation.fixtures import (
    LABEL_SPEC,
    MANIFEST,
    PROVIDER,
    generate,
    research_events,
)


def _request() -> OutcomeRequest:
    market = generate(seed=3, strength="0.6")
    bars = bars_from_synthetic(market)
    events = research_events(market)[:40]
    gap = OutcomeEvent(event_key="z-gap", event_time=bars[-1].interval_end)  # no bar after it
    return OutcomeRequest(
        label_spec=LABEL_SPEC,
        manifest_content_hash=MANIFEST,
        price_cutoff=bars[-1].available_time,
        events=(*events, gap),
        bars=bars,
    )


@pytest.fixture(scope="module")
def request_() -> OutcomeRequest:
    return _request()


@pytest.fixture(scope="module")
def table(request_: OutcomeRequest) -> OutcomeTable:
    return materialize(PROVIDER, request_)


def _file(store: OutcomeTableStore, digest: str) -> Path:
    return store.path_of(digest)


def _rewrite(path: Path, raw: dict[str, object], *, rehash: bool) -> Path:
    """Write ``raw`` canonically; with ``rehash`` under its recomputed (forged) table hash."""
    if rehash:
        payload = {k: v for k, v in raw.items() if k != "table_hash"}
        raw = {**payload, "table_hash": content_hash(payload)}
        path = path.with_name(f"{raw['table_hash']}.json")
    path.write_bytes(canonical_json(raw).encode("utf-8"))
    return path


# --------------------------------------------------------------------------------------
# materialize / rows
# --------------------------------------------------------------------------------------


def test_materialize_records_the_result_hashes(
    request_: OutcomeRequest, table: OutcomeTable
) -> None:
    result = PROVIDER.compute(request_)
    assert (table.request_hash, table.provider_hash) == (
        result.request_hash,
        result.provider_hash,
    )
    assert table.request_hash == request_.content_hash()
    assert table.get("z-gap") is not None and table.get("z-gap").value is None  # type: ignore[union-attr]


def test_rows_are_unchanged(table: OutcomeTable) -> None:
    rows = table.rows()
    assert len(rows) == len(table)
    head = {
        "outcome": str(table.outcome),
        "label_spec_hash": table.label_spec_hash,
        "provider": table.provider,
        "result_hash": table.result_hash,
    }
    for row, label in zip(rows, table.labels, strict=True):
        assert row == {**head, **label.model_dump(mode="json")}
        assert "request_hash" not in row and "provider_hash" not in row


# --------------------------------------------------------------------------------------
# put / get
# --------------------------------------------------------------------------------------


def test_a_stored_table_loads_back_equal(tmp_path: Path, table: OutcomeTable) -> None:
    store = OutcomeTableStore(tmp_path / "outcomes")
    digest = store.put(table)
    assert digest == table_hash(table)
    path = _file(store, digest)
    assert path == tmp_path / "outcomes" / f"{digest}.json"
    raw = json.loads(path.read_text("utf-8"))
    assert canonical_json(raw).encode("utf-8") == path.read_bytes()
    assert raw["table_hash"] == digest and raw["kind"] == "research.outcome_table"
    loaded = store.get(digest)
    assert loaded == table
    cut = table.labels[10].available_time
    assert cut is not None
    assert loaded.known_as_of(cut) == table.known_as_of(cut)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no temp file left


def test_an_identical_rewrite_is_a_no_op(tmp_path: Path, table: OutcomeTable) -> None:
    store = OutcomeTableStore(tmp_path)
    digest = store.put(table)
    before = os.stat(_file(store, digest))
    assert store.put(table) == digest
    assert OutcomeTableStore(tmp_path).put(table) == digest
    after = os.stat(_file(store, digest))
    assert (after.st_ino, after.st_mtime_ns, after.st_nlink) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_nlink,
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{digest}.json"]


def test_a_different_existing_file_is_a_conflict_and_never_overwritten(
    tmp_path: Path, table: OutcomeTable
) -> None:
    store = OutcomeTableStore(tmp_path)
    digest = store.put(table)
    path = _file(store, digest)
    tampered = path.read_bytes().replace(b'"provider"', b'"provider" ', 1)
    path.write_bytes(tampered)
    with pytest.raises(OutcomeTableConflict):
        store.put(table)
    assert path.read_bytes() == tampered
    with pytest.raises(OutcomeTableCorrupted):
        store.get(digest)


# --------------------------------------------------------------------------------------
# refused on load
# --------------------------------------------------------------------------------------


def _stored(tmp_path: Path, table: OutcomeTable) -> tuple[OutcomeTableStore, str, Path]:
    store = OutcomeTableStore(tmp_path)
    digest = store.put(table)
    return store, digest, _file(store, digest)


def test_a_tampered_label_value_is_refused(tmp_path: Path, table: OutcomeTable) -> None:
    store, digest, path = _stored(tmp_path, table)
    raw = json.loads(path.read_text("utf-8"))
    label = next(item for item in raw["labels"] if item["value"] is not None)
    label["value"] = "0.5"
    _rewrite(path, raw, rehash=False)
    with pytest.raises(OutcomeTableCorrupted, match="does not match its hash"):
        store.get(digest)


def test_a_forged_table_hash_still_fails_the_result_hash_check(
    tmp_path: Path, table: OutcomeTable
) -> None:
    store, _, path = _stored(tmp_path, table)
    raw = json.loads(path.read_text("utf-8"))
    label = next(item for item in raw["labels"] if item["value"] is not None)
    label["value"] = "0.5"
    forged = _rewrite(path, raw, rehash=True)
    with pytest.raises(OutcomeTableCorrupted, match="does not verify"):
        store.get(forged.stem)


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "research.feature_table"), ("format_version", "9.0.0")],
)
def test_an_unknown_format_is_refused_even_with_a_matching_hash(
    tmp_path: Path, table: OutcomeTable, field: str, value: str
) -> None:
    store, _, path = _stored(tmp_path, table)
    raw = json.loads(path.read_text("utf-8"))
    raw[field] = value
    forged = _rewrite(path, raw, rehash=True)
    with pytest.raises(OutcomeTableCorrupted, match="unknown format"):
        store.get(forged.stem)


def test_an_extra_or_missing_field_is_refused(tmp_path: Path, table: OutcomeTable) -> None:
    store, _, path = _stored(tmp_path, table)
    raw = json.loads(path.read_text("utf-8"))
    extra = _rewrite(path, {**raw, "features": []}, rehash=True)
    with pytest.raises(OutcomeTableCorrupted, match="wrong fields"):
        store.get(extra.stem)
    missing = _rewrite(path, {k: v for k, v in raw.items() if k != "request_hash"}, rehash=True)
    with pytest.raises(OutcomeTableCorrupted, match="wrong fields"):
        store.get(missing.stem)


@pytest.mark.parametrize("keep", [0, 1, 100, -1])
def test_a_truncated_file_is_refused(tmp_path: Path, table: OutcomeTable, keep: int) -> None:
    store, digest, path = _stored(tmp_path, table)
    data = path.read_bytes()
    path.write_bytes(data[:keep])
    with pytest.raises(OutcomeTableCorrupted, match="not valid JSON"):
        store.get(digest)


def test_non_canonical_text_is_refused(tmp_path: Path, table: OutcomeTable) -> None:
    store, digest, path = _stored(tmp_path, table)
    raw = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps(raw, sort_keys=True, indent=1), "utf-8")
    with pytest.raises(OutcomeTableCorrupted, match="not canonical"):
        store.get(digest)


@pytest.mark.parametrize(
    "text",
    ['{"kind": 1, "kind": 2}', '{"value": NaN}', '{"value": Infinity}', "[]", "\xff"],
    ids=["duplicate-key", "nan", "infinity", "not-an-object", "not-utf8"],
)
def test_malformed_json_is_refused(tmp_path: Path, table: OutcomeTable, text: str) -> None:
    store, digest, path = _stored(tmp_path, table)
    path.write_bytes(text.encode("latin-1"))
    with pytest.raises(OutcomeTableCorrupted):
        store.get(digest)


def test_a_file_under_another_name_is_refused(tmp_path: Path, table: OutcomeTable) -> None:
    store, digest, path = _stored(tmp_path, table)
    other = "f" * 64
    path.rename(path.with_name(f"{other}.json"))
    with pytest.raises(OutcomeTableCorrupted, match="does not match its hash"):
        store.get(other)


def test_a_symlinked_table_is_refused(tmp_path: Path, table: OutcomeTable) -> None:
    real = OutcomeTableStore(tmp_path / "real")
    digest = real.put(table)
    linked = OutcomeTableStore(tmp_path / "linked")
    linked.root.mkdir()
    linked.path_of(digest).symlink_to(real.path_of(digest))
    with pytest.raises(OutcomeTableCorrupted, match="cannot be read safely"):
        linked.get(digest)
    with pytest.raises(OutcomeTableConflict):
        linked.put(table)


def test_a_missing_table_and_a_bad_name_are_refused(tmp_path: Path) -> None:
    store = OutcomeTableStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.get("a" * 64)
    for bad in ("../" + "a" * 61, "A" * 64, "a" * 63, ""):
        with pytest.raises(ValueError, match="not a table hash"):
            store.get(bad)


# --------------------------------------------------------------------------------------
# refused on write
# --------------------------------------------------------------------------------------


def test_a_hand_built_table_without_result_hashes_is_not_storable(
    tmp_path: Path, table: OutcomeTable
) -> None:
    bare = OutcomeTable(
        outcome=table.outcome,
        label_spec_hash=table.label_spec_hash,
        provider=table.provider,
        result_hash=table.result_hash,
        labels=table.labels,
    )
    with pytest.raises(ValueError, match="only a materialized OutcomeTable"):
        OutcomeTableStore(tmp_path).put(bare)
    assert not any(tmp_path.iterdir())


def test_an_inconsistent_table_is_not_storable(tmp_path: Path, table: OutcomeTable) -> None:
    dropped = OutcomeTable(
        outcome=table.outcome,
        label_spec_hash=table.label_spec_hash,
        provider=table.provider,
        result_hash=table.result_hash,
        labels=table.labels[1:],
        request_hash=table.request_hash,
        provider_hash=table.provider_hash,
    )
    with pytest.raises(ValidationError, match="result_hash"):
        OutcomeTableStore(tmp_path).put(dropped)
    assert not any(tmp_path.iterdir())


# --------------------------------------------------------------------------------------
# outcomes never become inputs (C-L2)
# --------------------------------------------------------------------------------------


def test_a_loaded_table_still_only_hands_out_labels(tmp_path: Path, table: OutcomeTable) -> None:
    store = OutcomeTableStore(tmp_path)
    loaded = store.get(store.put(table))
    public = {name for name in dir(loaded) if not name.startswith("_")}
    assert public == {
        "computable",
        "get",
        "known_as_of",
        "labels",
        "label_spec_hash",
        "outcome",
        "provider",
        "provider_hash",
        "request_hash",
        "result_hash",
        "rows",
    }
    for label in loaded.labels:
        assert label.label_only is True and is_outcome_payload(label)
    with pytest.raises(ValidationError):
        FeatureObservation.model_validate(loaded.computable()[0].model_dump(mode="json"))
    public_store = {name for name in dir(store) if not name.startswith("_")}
    assert public_store == {"get", "path_of", "put", "root"}
