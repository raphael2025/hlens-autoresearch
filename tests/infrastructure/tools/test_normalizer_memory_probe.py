"""The E1-CAP-1 probe's attribution: a stage is judged by its own samples, never by history.

Review 2026-09-27-e1: a process high-water mark minus a later current RSS is not a stage peak.
The probe's baseline is the median of the samples between ``ready`` and ``start``; its peak is
the largest sample between ``start`` and ``end``; ``VmHWM`` is only reported, labelled
process-level. These tests pin that arithmetic and the probe's refusals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.tools import normalizer_memory_probe as probe


def _events(**hwm: int) -> list[dict[str, object]]:
    return [
        {"event": "ready", "t": 10.0, "vmhwm_kb": hwm.get("ready", 900 * 1024)},
        {"event": "start", "t": 11.0},
        {"event": "end", "t": 13.0, "vmhwm_kb": hwm.get("end", 950 * 1024), "rows_read": 7},
    ]


def test_a_stage_is_attributed_from_its_own_window_of_samples() -> None:
    mib = 1024
    samples = [
        (9.0, 900 * mib),  # before ready (import / setup spike): never the stage's
        (10.1, 100 * mib),
        (10.5, 102 * mib),
        (10.9, 101 * mib),
        (11.5, 130 * mib),
        (12.0, 145 * mib),
        (12.5, 160 * mib),
        (13.5, 400 * mib),  # after end: never the stage's either
    ]
    record = probe._attribute(_events(), samples, 0.01)
    assert record["baseline_mib"] == 101.0  # median of the settle window
    assert record["peak_mib"] == 160.0  # largest sample inside [start, end]
    assert record["delta_mib"] == 59.0
    assert (record["settle_samples"], record["stage_samples"]) == (3, 3)
    # The process high-water marks are reported as such, not used for the delta.
    assert record["process_vmhwm_at_ready_mib"] == 900.0
    assert record["process_vmhwm_at_end_mib"] == 950.0
    assert record["rows_read"] == 7


def test_too_few_samples_are_refused() -> None:
    with pytest.raises(RuntimeError, match="too few samples"):
        probe._attribute(
            _events(),
            [(10.5, 1024), (11.5, 2 * 1024), (12.5, 3 * 1024)],
            0.01,
        )


def test_one_sample_per_window_is_not_enough() -> None:
    with pytest.raises(RuntimeError, match="too few samples"):
        probe._attribute(_events(), [(10.5, 1024), (11.5, 2 * 1024)], 0.01)


def test_the_slope_is_a_least_squares_fit() -> None:
    assert probe._slope([(10, 1.0)]) is None
    slope = probe._slope([(0, 0.0), (1_000, 1.0), (2_000, 2.0)])
    assert slope == pytest.approx(2**20 / 1_000)


def test_growth_gate_checks_every_scale_including_an_intermediate_peak() -> None:
    verdict = probe._stage_verdict(
        [(10_000, 1.0), (100_000, 40.0), (500_000, 2.0)],
        (10_000, 100_000, 500_000),
        microbatch=256,
    )
    assert verdict["growth_mib"] == 39.0
    assert verdict["pass"] is False


@pytest.mark.parametrize(
    ("points", "message"),
    [
        ([(10_000, 1.0), (100_000, 2.0)], "missing or duplicate"),
        (
            [(10_000, 1.0), (100_000, 2.0), (100_000, 3.0)],
            "missing or duplicate",
        ),
    ],
)
def test_growth_gate_refuses_missing_or_duplicate_measurements(
    points: list[tuple[int, float]], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        probe._stage_verdict(points, (10_000, 100_000, 500_000), microbatch=256)


@pytest.mark.parametrize("sizes", [(10_000,), (10_000, 10_000, 100_000)])
def test_probe_requires_three_distinct_sizes(sizes: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="three distinct"):
        probe._validate_sizes(sizes)


@pytest.mark.parametrize(("verdict", "expected"), [(False, 1), (True, 0)])
def test_cli_exit_code_matches_capacity_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: bool,
    expected: int,
) -> None:
    monkeypatch.setattr(probe, "_filesystem", lambda _path: "ext4")
    monkeypatch.setattr(probe, "run_probe", lambda *args, **kwargs: {"pass": verdict})
    result = probe.main(
        [
            "--rows",
            "10000",
            "100000",
            "500000",
            "--base",
            str(tmp_path / "probe"),
            "--i-know-memory",
        ]
    )
    assert result == expected


def test_probe_rejects_nonpositive_sampling_interval(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        probe.main(
            [
                "--rows",
                "10000",
                "100000",
                "500000",
                "--interval",
                "0",
                "--base",
                str(tmp_path),
                "--i-know-memory",
            ]
        )


def test_a_tmpfs_work_directory_is_refused(tmp_path: Path) -> None:
    assert probe._filesystem(Path("/proc")) == "proc"
    if probe._filesystem(Path("/dev/shm")) == "tmpfs":
        refused = Path("/dev/shm/hlens-probe-refused")
        with pytest.raises(SystemExit):
            probe.main(["--rows", "10", "--base", str(refused)])
        assert not refused.exists()  # refused before anything is created there
    with pytest.raises(SystemExit):
        probe.main(["--rows", "200000", "--base", str(tmp_path)])  # no --i-know-memory


def test_the_growth_limit_is_the_documented_one() -> None:
    assert probe.GROWTH_LIMIT_MIB == 32
    assert probe.DEFAULT_SIZES == (10_000, 100_000, 500_000)
    assert probe.DEFAULT_MICROBATCH == 256
