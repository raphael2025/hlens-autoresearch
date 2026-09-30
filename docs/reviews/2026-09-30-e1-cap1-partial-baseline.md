# E1-CAP-1 partial baseline diagnostic (2026-09-30)

## Scope and identity

This is an intentionally partial diagnostic of the clean baseline candidate
`codex/pit-conflict-v3@5d9dd718cf13d2aede32d9ed3f78621a178ef78a`. It is **not** E1-CAP-1 evidence or Phase 1 acceptance: the candidate's production paths differ from `main` (`e5841879cfb2aaf6700a49b68ca97f90f3ea171c`), and the protocol did not complete all sizes and three repeats.

The run used Python 3.13.15, M=256, D2 batch 4096, 10k/100k/500k rows, controlled runtime, 0.01-second RSS sampling, and a 6 GiB cgroup memory cap with swap disabled. Work files were on ext4. The probe source matched HEAD and was clean. No staged diagnostics were enabled.

Command (the probe itself requested `--i-know-memory` for sizes above 50k):

```text
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 \
  uv run python -m infrastructure.tools.normalizer_memory_probe \
  --i-know-memory \
  --base /home/raphael/.local/share/hlens-probes/e1-cap1-5d9dd71 \
  --json-out /tmp/e1-cap1-5d9dd71.json \
  --samples-out /tmp/e1-cap1-5d9dd71-samples.jsonl
```

## Observed measurements

The complete first repeat at 10k and 100k produced:

| Rows | verify_archive | write_crash | resume | replay | read_batch | metadata |
|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 44.3 MiB | 56.9 MiB | 53.7 MiB | 45.5 MiB | 34.1 MiB | 2.2 MiB |
| 100,000 | 54.5 MiB | 64.3 MiB | 62.7 MiB | 55.1 MiB | 41.4 MiB | 5.8 MiB |

At 500k, fixture setup completed in 27.734 seconds and sampled a setup maximum of 296.6 MiB. The first `verify_archive` stage proved all 500,000 rows; it ran for 991.084 seconds and measured a 93.0 MiB delta (baseline 134.0 MiB, peak 227.1 MiB).

The observed `verify_archive` delta range between 10k and 500k is already at least 48.7 MiB (`93.0 - 44.3`), above the fixed 32 MiB limit. Because that alone is conclusive for the observed points, the remaining stages/repeats were stopped to avoid spending hours on a candidate that cannot satisfy the measured range.

## Protocol completion and interpretation

The probe exited with status 4 / `capacity_verdict = ERROR` because the next 500k `write_crash` child was deliberately sent SIGTERM after the observed range exceeded the limit. Preserved artifacts are [the output JSON](artifacts/e1-cap1-5d9dd71-partial.json) and [the raw RSS samples](artifacts/e1-cap1-5d9dd71-partial-samples.jsonl); the initial local copies were `/tmp/e1-cap1-5d9dd71.json` and `/tmp/e1-cap1-5d9dd71-samples.jsonl`.

This establishes only a **partial diagnostic failure on 5d9dd71**. It is not a release verdict for `main`, and it says nothing about the later PIT lazy-key, archive-spool, row/edge RunSet commits on `codex/e1-phase1-progress`. A new complete protocol run is required on a clean candidate whose compared production paths match `main`; first address the measured archive verification growth and the remaining unbounded per-key PIT structures.
