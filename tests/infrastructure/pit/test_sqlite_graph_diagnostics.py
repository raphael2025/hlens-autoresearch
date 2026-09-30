from __future__ import annotations

import json
from pathlib import Path

import pytest

from infrastructure.pit.diagnostics import run_diagnostic


def test_bounded_repeated_cutoff_diagnostic_reports_graph_sql_and_resources(tmp_path: Path) -> None:
    result = run_diagnostic(
        "repeated_cutoff", vertices=16, fanout=2, cutoffs=3, scratch_root=tmp_path / "scratch"
    )

    assert result["acceptance_scope"] == "diagnostic_only_not_E1_CAP_1"
    assert result["graph"] == {"V": 16, "E": 15, "K": 3}
    assert result["sqlite_settings"] == {
        "cache_size": -4096,
        "mmap_size": 0,
        "temp_store": 1,
        "journal_mode": "off",
        "automatic_index": 0,
    }
    assert result["database_bytes"] > 0
    assert result["scratch_directory_bytes"] >= result["database_bytes"]
    assert result["scratch_filesystem"]["total_bytes"] > 0
    assert result["elapsed"]["total_seconds"] >= result["elapsed"]["build_seconds"]
    assert len(result["memory_samples"]) >= 2
    assert [cutoff["candidate_nodes"] for cutoff in result["cutoffs"]] == [5, 10, 15]
    assert [cutoff["head_count"] for cutoff in result["cutoffs"]] == [1, 1, 1]
    # The production frontier seeds each candidate's predecessors. Its `visited` table
    # therefore excludes the seed candidates themselves (V-1 for this chain shape).
    assert [cutoff["visited_predecessor_nodes"] for cutoff in result["cutoffs"]] == [4, 9, 14]
    assert all(sum(cutoff["sql_statements"].values()) > 0 for cutoff in result["cutoffs"])
    assert result["scratch_directory_cleaned"] is True
    json.dumps(result)


def test_bounded_wide_dag_diagnostic_reports_expected_fanout(tmp_path: Path) -> None:
    result = run_diagnostic("wide_dag", vertices=16, fanout=4, cutoffs=2, scratch_root=tmp_path)
    assert result["graph"] == {"V": 16, "E": 48, "K": 1}
    assert result["cutoffs"][0]["head_count"] == 12
    assert result["cutoffs"][0]["candidate_seed_edge_rows"] == 48


@pytest.mark.parametrize(
    ("scenario", "kwargs"),
    [
        ("long_chain", {"vertices": 15}),
        ("long_chain", {"vertices": 513}),
        ("wide_dag", {"vertices": 16, "fanout": 9}),
        ("repeated_cutoff", {"vertices": 16, "cutoffs": 17}),
    ],
)
def test_diagnostic_rejects_sizes_outside_explicit_caps(
    tmp_path: Path, scenario: str, kwargs: dict[str, int]
) -> None:
    options = {"vertices": 16, "fanout": 8, "cutoffs": 8, **kwargs}
    with pytest.raises(ValueError):
        run_diagnostic(scenario, **options, scratch_root=tmp_path)
