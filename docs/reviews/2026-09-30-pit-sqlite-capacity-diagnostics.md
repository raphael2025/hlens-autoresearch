# PIT SQLite bounded diagnostics — 2026-09-30

## Integrated slice

Candidate `codex/pit-sqlite-capacity-diagnostics@01054b68c3f625e505e30b636e01cf89e7d0b955` received three independent, limit-scoped ACCEPT reviews. It was cherry-picked to `codex/project-consolidation` as `5d143c4`.

The new `python -m infrastructure.pit.diagnostics` command creates bounded synthetic long-chain, wide-DAG, and repeated-cutoff graphs using lazy inputs and an explicit scratch root. Hard limits are V ≤ 512, fanout ≤ 8, and K ≤ 16. It reports actual SQLite V/E/K, statement counts by class, candidate/visited/edge-row counts with their semantics, elapsed times, database and scratch bytes, SQLite settings, scratch filesystem, RSS samples, cgroup samples, and invocation-directory cleanup. Results label themselves `diagnostic_only_not_E1_CAP_1`.

## Verification and measured smoke

Three independent reviewers accepted the harness. On the integrated line, the PIT direct suite was rerun:

```text
uv run --offline pytest -q --tb=short tests/infrastructure/pit/test_sqlite_graph_diagnostics.py tests/infrastructure/pit/test_sqlite_graph.py tests/infrastructure/pit/test_sqlite_selector.py
24 passed in 2.94s
```

The coordinator command was:

```text
uv run python -m infrastructure.pit.diagnostics --scenario all --vertices 64 --fanout 4 --cutoffs 3 --scratch /home/raphael/.local/share/hlens-probes/pit-sqlite-diag-01054b6
```

The coordinator run used fanout 4 / three cutoffs: chain V/E/K = 64/63/1 with 63 visited predecessors; wide DAG V/E/K = 64/240/1 with 60 heads; repeated cutoffs V/E/K = 64/63/3 with candidate counts 21/42/63 and visited counts 20/41/62. An independent reviewer also ran fanout 8 / eight cutoffs under a 3 GiB systemd scope on the same ext4 mount: chain V/E/K = 64/63/1; wide DAG V/E/K = 64/448/1 with 8 roots and 56 maximal heads; repeated cutoffs V/E/K = 64/63/8 with candidate counts 8, 16, …, 64 and visited counts one lower. Both runs reported `scratch_directory_cleaned=true` and the diagnostic-only acceptance label.

## Limits

This is a bounded harness smoke, not a capacity stress test or E1-CAP-1 evidence. The run does not measure the full selector/caller/PyIceberg working set. RSS is sampled every 50 ms with phase checkpoints and can miss short peaks; cgroup `memory.current` covers the scope and is not process RSS. There is no repeated-run or cache-state comparison, and logical file sizes are not allocated filesystem blocks. No capacity threshold or Phase 1 status changed.
