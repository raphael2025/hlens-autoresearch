# PIT SQLite graph validator foundation — 2026-09-30

## Integrated slice

Candidate `codex/adr0097-pit-sqlite-graph@cef7b116baf99ae81d83cd7d3624d93904cab5e0` was independently reviewed by three roles and received three APPROVE decisions after two lifecycle defects were corrected. It was squashed onto `codex/project-consolidation` as `889b268018f14f3e51eb962650f3f5449c816677`.

The slice adds `infrastructure/pit/sqlite_graph.py` and `tests/infrastructure/pit/test_sqlite_graph.py`. The validator:

- requires an explicit absolute caller-owned scratch root and creates a unique mode-0700 invocation directory with a UUID ownership marker;
- disables SQLite mmap and automatic indexes, checks a configured 4096 KiB page-cache setting, and uses file-backed temporary storage;
- streams cutoff-known revisions/evidence into indexed SQLite tables and checks unique revision IDs, arrival sequences and payloads; cross-key claims; timely evidence for each declared supersedes edge; dangling predecessors; and graph cycles;
- stores DFS state in SQLite, avoiding graph-sized Python maps; key cycle/claim/evidence queries were checked to use primary-key scans/ranges without a temporary B-tree;
- closes the connection before deleting its owned directory on normal completion and handled failures. Marker open/write failure cleanup preserves the original setup exception. Reentering the same context manager instance is rejected before a second directory or connection is created.

Three independent reviewers each ran the direct suite on the final candidate. The consolidation line reran it after integration:

```text
uv run --offline pytest -q --tb=short tests/infrastructure/pit/test_sqlite_graph.py
...........                                                              [100%]
11 passed in 0.05s

uv run --offline ruff check infrastructure/pit/sqlite_graph.py tests/infrastructure/pit/test_sqlite_graph.py
All checks passed!

uv run --offline ruff format --check infrastructure/pit/sqlite_graph.py tests/infrastructure/pit/test_sqlite_graph.py
2 files already formatted

uv run --offline mypy infrastructure/pit/sqlite_graph.py
Success: no issues found in 1 source file

git diff --check
```

The integration commit also passed `git diff --cached --check` before commit. The independent reviewers inspected the exact final candidate and each reported 11 passing tests; no selector integration was included in that candidate.

## Acceptance boundary and next work

This is a lower-level validator foundation, not full ADR-0097 acceptance. It is not connected to `PitSelector.iter_bounded()` or `_evaluate_bounded()`. It does not yet provide reachability/maximal-head traversal, reuse one index across multiple availability instants, or emit the complete ADR-0094 conflict-head stream. Long-chain, wide-DAG and repeated-cutoff diagnostics, deep-object bounds, caller PIT/Dataset regressions and full-process E1-CAP-1 remain open. E1-CAP-1 and Phase 1 are not accepted.

Next: build the graph once for each key invocation, reuse it across cutoff evaluations, implement indexed reachability through unavailable intermediate revisions, and stream the complete canonically ordered heads. Preserve v2 replay and ADR-0094 behavior. Measure query counts, runtime, scratch bytes and RSS/cgroup before making capacity claims.
