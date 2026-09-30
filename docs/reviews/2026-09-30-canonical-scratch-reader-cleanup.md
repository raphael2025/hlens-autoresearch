# Canonical scratch reader cleanup review — 2026-09-30

## Scope and result

Commit `2f984615b8b49402e7e4caaf42b4f22d2016038d` ports the narrow reader-cleanup fix from isolated candidate `codex/canonical-scratch-integration-port@4397fd22692c78cd31bb1cc1df43585747f5f107` onto `codex/project-consolidation`. Only `infrastructure/canonical/normalizer.py` and `tests/infrastructure/canonical/test_normalizer.py` changed.

`CanonicalNormalizer._positions()` now creates `_PositionIndex` inside the reader's cleanup boundary. If index initialization fails, the opened reader is closed without being consumed. If later scanning fails, both reader and an already-created index are closed. The new regression test checks the initialization-failure path.

Three independent reviewers approved this exact candidate. Each independently ran the new regression:

- `uv run --offline pytest -q --tb=short tests/infrastructure/canonical/test_normalizer.py::test_positions_closes_reader_when_scratch_index_initialization_fails` — `1 passed in 0.20s` (implementation agent).
- `uv run pytest -q --tb=short tests/infrastructure/canonical/test_normalizer.py::test_positions_closes_reader_when_scratch_index_initialization_fails` — `1 passed in 0.05s` (reviewer 1).
- `uv run pytest -q tests/infrastructure/canonical/test_normalizer.py::test_positions_closes_reader_when_scratch_index_initialization_fails` — `1 passed in 0.02s` (reviewer 2).
- `uv run pytest -q tests/infrastructure/canonical/test_normalizer.py::test_positions_closes_reader_when_scratch_index_initialization_fails` — `1 passed` (reviewer 3; pytest progress output only).

The candidate also passed Ruff, Ruff format check, `mypy infrastructure/canonical/normalizer.py`, and `git diff --check` before integration. The final consolidated integration tree has not rerun this test yet.

## Acceptance boundary

This closes only the reader leak when local position-index initialization fails. It does not accept the Canonical module, E1-CAP-1, or Phase 1. The caller-owned scratch path is already threaded through the current consolidation line; the older `codex/canonical-position-bounds@991b126` branch remains provenance and was not merged wholesale. No `StorageAdapter`, public contract, or SQLite PIT graph code changed.

Next: implement the accepted [ADR-0097](../adr/0097-pit-bounded-graph-scratch-index.md) graph index, then run scoped PIT/Dataset acceptance. Full-process 32 MiB measurement remains a separate gate.
