"""The isolated E1 probe includes the real v3 Dataset build as a diagnostic stage."""

from __future__ import annotations

from pathlib import Path

from infrastructure.tools import normalizer_memory_probe as probe


def test_v3_dataset_is_prepared_and_built_in_distinct_child_processes(tmp_path: Path) -> None:
    config = {"microbatch": 64, "d2_batch": 128}
    workdir = tmp_path / "dataset-v3"
    workdir.mkdir()

    setup = probe._run_child(
        probe._child_args("dataset_v3_setup", workdir, 200, config, None, staged_diagnostics=False),
        temp_dir=workdir,
        runtime="default",
        interval=0.01,
        timeout=120,
        rss_limit_kb=2 * 1024 * 1024,
    )
    assert setup.returncode == 0, setup.stderr_tail
    assert "dataset_setup" in setup.by_event
    assert setup.by_event["dataset_setup"]["canonical_rows"] == 200

    build = probe._run_child(
        probe._child_args(
            "dataset_v3_build",
            workdir,
            200,
            config,
            setup.by_event["dataset_setup"]["unit"],
            staged_diagnostics=False,
        ),
        temp_dir=workdir,
        runtime="default",
        interval=0.01,
        timeout=120,
        rss_limit_kb=2 * 1024 * 1024,
    )
    assert build.returncode == 0, build.stderr_tail
    end = build.by_event["end"]
    assert end["stage"] == "dataset_v3_build"
    assert end["hold_seconds"] == probe._HOLD_SECONDS
    assert end["facts"]["row_count"] > 0
    assert end["facts"]["row_count"] == 200
    assert end["facts"]["expected_row_count"] == 200
    assert end["facts"]["chunk_count"] > 0
    assert end["facts"]["diagnostic_only"] is True
    assert end["facts"]["dq9_parameters"] == probe._DATASET_V3_DIAGNOSTIC_RULE


def test_dq9_diagnostic_parameters_cannot_qualify_as_e1_evidence() -> None:
    assert "dataset_v3_build" in probe.STAGES
    assert probe._DATASET_V3_DQ9_ACCEPTED is False
    assert probe._DATASET_V3_DIAGNOSTIC_RULE == {
        "chunk_rows": probe.PROTOCOL_MICROBATCH,
        "leaf_max_records": 1_000,
        "leaf_max_bytes": 1 << 20,
        "fanout": 4,
    }
    assert probe._DATASET_V3_DIAGNOSTIC_PIT["row_batch_rows"] == probe.PROTOCOL_MICROBATCH
    assert probe._DATASET_V3_DIAGNOSTIC_PIT["edge_batch_rows"] == probe.PROTOCOL_MICROBATCH
    assert probe._DATASET_V3_DIAGNOSTIC_UNIVERSE["capacity"] == probe.PROTOCOL_MICROBATCH
    assert probe._DATASET_V3_DIAGNOSTIC_UNIVERSE["merge_fanout"] == 4
