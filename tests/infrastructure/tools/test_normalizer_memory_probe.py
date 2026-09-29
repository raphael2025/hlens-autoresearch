from datetime import UTC, timedelta

import pytest

from infrastructure.tools.normalizer_memory_probe import (
    DATASET_V3_DAY_END,
    DATASET_V3_DAY_START,
    DATASET_V3_RULE_KEYS,
    DATASET_V3_SOURCE_RUN_BYTES,
    DATASET_V3_SOURCE_RUN_FANOUT,
    DATASET_V3_SOURCE_RUN_RECORDS,
    _dataset_v3_source_params,
    _validate_dataset_v3_config,
)


def test_dataset_v3_window_is_one_full_utc_day() -> None:
    assert DATASET_V3_DAY_START.tzinfo is UTC
    assert DATASET_V3_DAY_END.tzinfo is UTC
    assert DATASET_V3_DAY_END - DATASET_V3_DAY_START == timedelta(days=1)
    assert DATASET_V3_DAY_START.hour == 0
    assert DATASET_V3_DAY_END.hour == 0


def test_dataset_v3_requires_all_dq9_values_and_does_not_default() -> None:
    assert len(DATASET_V3_RULE_KEYS) == 4
    with pytest.raises(ValueError, match="missing="):
        _validate_dataset_v3_config({})
    with pytest.raises(ValueError, match="extra="):
        _validate_dataset_v3_config(
            {
                "chunk_rows": 1,
                "leaf_max_records": 1,
                "leaf_max_bytes": 64,
                "fanout": 2,
                "unapproved": 1,
            }
        )
    with pytest.raises(ValueError, match="positive integers"):
        _validate_dataset_v3_config(
            {"chunk_rows": True, "leaf_max_records": 1, "leaf_max_bytes": 64, "fanout": 2}
        )


def test_dataset_v3_explicit_rule_values_are_returned_unchanged() -> None:
    supplied = {"chunk_rows": 1, "leaf_max_records": 1, "leaf_max_bytes": 64, "fanout": 2}
    assert _validate_dataset_v3_config(supplied) == supplied


def test_dataset_v3_upstream_run_bounds_are_explicit_and_separately_reportable() -> None:
    pit, universe = _dataset_v3_source_params(microbatch=256)
    assert pit.row_batch_rows == 256
    assert pit.edge_batch_rows == 256
    assert pit.key_history_buffer == 256
    assert pit.limits.leaf_max_records == DATASET_V3_SOURCE_RUN_RECORDS
    assert pit.limits.leaf_max_bytes == DATASET_V3_SOURCE_RUN_BYTES
    assert pit.limits.fanout == DATASET_V3_SOURCE_RUN_FANOUT
    assert universe.capacity == DATASET_V3_SOURCE_RUN_RECORDS
    assert universe.merge_fanout == DATASET_V3_SOURCE_RUN_FANOUT
