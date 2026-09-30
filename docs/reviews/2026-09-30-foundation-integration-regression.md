# Foundational integration regression review — 2026-09-30

**Branch:** `codex/project-consolidation`  
**Phase:** Phase 1 — Market Representation  
**Scope:** Normalizer, shared RunSet merge, PIT v3, Dataset v3 source test seams.  
**Disposition:** Foundational module slice accepted on the consolidation line; no E1-CAP-1 or Phase acceptance.

## First integrated baseline

Command:

```text
uv run pytest -q --tb=short tests/infrastructure/canonical/test_normalizer.py tests/infrastructure/pit/test_bounded_runs.py tests/infrastructure/pit/test_selector.py tests/infrastructure/pit/test_selector_v3.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/dataset/test_dataset_v3_builder.py
```

Result: `7 failed, 236 passed, 1 skipped in 235.96s`.

The failures were classified rather than counted as passes:

| Area | Finding | Follow-up |
|---|---|---|
| Normalizer | Test constructor spy still used the former `_PositionIndex` signature. | Updated it to pass the current explicit scratch directory; exact node passed. |
| PIT selector | A boundary parity assertion compared different v2/v3 result models directly. | Changed it to compare their shared semantic signature; half-open interval assertions remain. |
| PIT orphan tests | Test monkeypatch targeted a removed helper name from the other branch implementation. | Rewired the fault injection to the current `_mapped_edge_run_stream`; both early/trailing orphan rejection nodes passed. |
| Dataset source | Test subclass omitted the now-required canonical scratch path. | Supplied an isolated `tmp_path` scratch directory; exact node passed. |
| Shared streaming RunSet | Lazy refs were materialized before merge; context exit left the `heapq.merge` iterator open. | A candidate fix was independently reviewed and blocked because it also persisted an intermediate run when the input count was exactly `merge_fanout`. It is not integrated. |

The five repaired nodes were rerun together:

```text
uv run pytest -q --tb=short \
  tests/infrastructure/canonical/test_normalizer.py::test_iter_revision_ids_early_close_releases_disk_backed_position_index \
  tests/infrastructure/pit/test_selector_v3.py::test_run_backed_evaluation_instants_are_deterministic_at_half_open_boundaries \
  tests/infrastructure/pit/test_selector_v3.py::test_iter_bounded_rejects_a_trailing_orphan_edge \
  tests/infrastructure/pit/test_selector_v3.py::test_iter_bounded_rejects_all_orphan_edges_before_the_first_key \
  tests/infrastructure/dataset/test_dataset_v3_sources.py::test_dataset_conflict_seals_only_first_interval_evaluation_and_closes_selector
```

Result: `5 passed in 5.32s`.

## Static checks

- Ruff check on the three changed test files: `All checks passed!`
- Ruff format check: `3 files already formatted`
- `uv run mypy infrastructure/canonical/normalizer.py infrastructure/dataset/builder.py infrastructure/pit/selector.py`: `Success: no issues found in 3 source files`
- `git diff --check`: passed.

Full mypy over the three affected legacy test files was also attempted and returned 36 existing test typing/import issues across those files. This is not counted as a passing type check; production-source MyPy above is the scoped result.

## Remaining acceptance work

The shared RunSet defects were fixed in consolidation commit `2fa58c6` and independently approved. The implementation preserves direct merge with no intermediate persistence for exactly `merge_fanout` refs, folds larger lazy iterables online, and closes the prefetched item, merge iterator, and reader stack on early exit.

The focused regression suite was rerun on the consolidation line:

```text
uv run pytest -q --tb=short tests/infrastructure/canonical/test_normalizer.py tests/infrastructure/pit/test_bounded_runs.py tests/infrastructure/pit/test_selector.py tests/infrastructure/pit/test_selector_v3.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/dataset/test_dataset_v3_builder.py tests/infrastructure/streaming/test_runs.py
```

Result: `247 passed, 1 skipped in 237.92s`.

Ruff reported all changed production and test files clean; format check reported `9 files already formatted`; MyPy reported `Success: no issues found in 5 source files`; `git diff --check` passed. The one skip is preserved and is not counted as a pass.

The broader Quality v3 → Dataset v3 consumer seam and full-process E1-CAP-1 ≤32 MiB measurement remain open. These results do not accept E1-CAP-1 or Phase 1.
