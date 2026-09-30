# PIT SQLite selector integration — 2026-09-30

## Integrated slice

Candidate `codex/e1-cap-pit-sqlite-selector@7a43fcc98f87945a16e9e1970df3e9572b3948b7` was reviewed by three independent roles and received three limit-scoped ACCEPT decisions. It was cherry-picked to `codex/project-consolidation` as `00594b8`.

The slice connects `SQLitePitGraph` to `_evaluate_bounded()` for one Canonical observation key. It builds and validates the known-at-cutoff graph once, stores each graph revision's effective availability once, and reuses SQLite indexed reachability for every simulation time in the interval. Candidate revisions are filtered by effective availability; traversal retains graph edges through unavailable intermediate revisions. Complete maximal heads are emitted in stable revision-id order to the existing ADR-0094 sink. The legacy `select()` path and the public `iter_bounded()` result shape remain unchanged.

The implementation keeps traversal state in SQLite tables (`candidates`, `frontier`, `visited`, and `eliminated`) instead of graph-sized Python maps. The three reviewers confirmed complete-head behavior, unavailable-intermediate reachability, stable output order, close/error propagation, and no correctness blocker. SQL plan checks covered availability, edge, candidate, and frontier access without temporary sorting; the algorithm still requires I/O proportional to the reachable graph for each simulation instant.

## Verification

The candidate author ran the focused direct and caller suites:

```text
uv run --offline pytest -q --tb=short tests/infrastructure/pit/test_sqlite_graph.py tests/infrastructure/pit/test_sqlite_selector.py
18 passed in 2.89s

uv run --offline pytest -q --tb=short tests/infrastructure/pit/test_selector_v3.py tests/infrastructure/dataset/test_dataset_v3_sources.py -k 'not test_canonical_key_closure_is_spilled_and_matches_v2_rows and not test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
63 passed, 4 skipped, 2 deselected in 115.87s

uv run --offline pytest -q --tb=short tests/infrastructure/pit/test_selector_bounded_proofs.py -k 'test_bounded_proofs_spill_and_close_readers_with_revision_order or test_bounded_proofs_validate_batches_while_reverse_snapshot_stream_is_open'
2 passed, 3 deselected in 9.04s

uv run --offline ruff check infrastructure/pit/sqlite_graph.py infrastructure/pit/selector.py tests/infrastructure/pit/test_sqlite_graph.py tests/infrastructure/pit/test_sqlite_selector.py
All checks passed!

uv run --offline ruff format --check infrastructure/pit/sqlite_graph.py infrastructure/pit/selector.py tests/infrastructure/pit/test_sqlite_graph.py tests/infrastructure/pit/test_sqlite_selector.py
4 files already formatted

uv run --offline mypy infrastructure/pit/sqlite_graph.py infrastructure/pit/selector.py
Success: no issues found in 2 source files

git diff --check
```

Independent reviewers reran the focused direct tests and reported `18 passed` (2.87–2.92s), with no blocking finding. An additional unfiltered `test_selector_v3.py` run completed with `39 passed, 1 skipped`; because it did not apply the project's deferred-node filter, it is not used as replacement acceptance evidence.

## Acceptance boundary and next work

This accepts only the SQLite graph-to-selector integration slice. It does not accept full ADR-0097, E1-CAP-1, or Phase 1. Repeated-cutoff I/O and total process RSS have not been measured; long-chain and wide-DAG capacity diagnostics, selector-level multi-key / assumption-bound regression, deep object bounds, full-process 32 MiB E1-CAP-1, real PostgreSQL append/replay, and full integration build remain open. The two previously deferred PIT nodes stay deferred and are not counted as passing.

The lower-level validator foundation review remains historical for commit `889b268`; its former statement that the validator was not connected to the selector is superseded by this follow-up. Keep both records to preserve the staged acceptance history.
