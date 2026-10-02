"""ADR-0108 §2: the streaming unit fingerprint equals the rule on the concatenated unit."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import BatchRejected
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.fingerprint_unit import UnitFingerprint
from tests.infrastructure.catalog.test_batch_fingerprint import SCHEMA, golden_inputs


def _slices(source: pa.Table, size: int) -> list[pa.Table]:
    return [source.slice(start, size) for start in range(0, source.num_rows, size)]


@pytest.mark.parametrize("name", sorted(golden_inputs()))
@pytest.mark.parametrize("size", [1, 2, 3, 1000])
def test_the_unit_fingerprint_is_the_rule_on_the_concatenation(
    name: str, size: int, tmp_path: Path
) -> None:
    source = golden_inputs()[name]
    with UnitFingerprint(source.schema, tmp_path) as unit:
        for part in _slices(source, size) or [source]:
            unit.add(part)
        assert unit.rows == source.num_rows
        assert unit.hexdigest() == PYARROW_BATCH_FINGERPRINT.fingerprint(source)
    assert list(tmp_path.iterdir()) == []


def test_repeated_golden_units_match_their_concatenation(tmp_path: Path) -> None:
    parts = [table for _, table in sorted(golden_inputs().items()) if table.schema == SCHEMA]
    whole = pa.concat_tables(parts)
    with UnitFingerprint(SCHEMA, tmp_path) as unit:
        for part in parts:
            unit.add(part)
        first = unit.hexdigest()
        assert first == PYARROW_BATCH_FINGERPRINT.fingerprint(whole)
        assert unit.hexdigest() == first  # reading the spools does not consume them


def test_an_empty_unit_is_the_rule_on_an_empty_table(tmp_path: Path) -> None:
    with UnitFingerprint(SCHEMA, tmp_path) as unit:
        assert unit.hexdigest() == PYARROW_BATCH_FINGERPRINT.fingerprint(SCHEMA.empty_table())


def test_a_microbatch_of_another_schema_or_type_is_rejected(tmp_path: Path) -> None:
    other = pa.table({"a": pa.array([1], pa.int64())})
    with UnitFingerprint(SCHEMA, tmp_path) as unit:
        with pytest.raises(BatchRejected, match="unit schema"):
            unit.add(other)
        with pytest.raises(BatchRejected, match="bounded pyarrow.Table"):
            unit.add(other.to_batches()[0])
    with pytest.raises(BatchRejected):
        UnitFingerprint(pa.schema([pa.field("f", pa.float64())]), tmp_path)
    assert list(tmp_path.iterdir()) == []
