# W6 independent module validation — 2026-09-29

## Scope and evidence

These checks use synthetic inputs or temporary SQLite/Iceberg fixtures. They do not require E1-CAP-1, create a production catalog, or accept Phase 1 / W6 as complete.

- P2 State runner, storage and diagnostics: **44 passed**.
- P3 Event and P4 Outcome selected providers, runner, lineage, statistics and temporary Iceberg tests: **203 passed, 1 failed**.
- The one failure is `tests/infrastructure/event/test_event_iceberg.py::test_phase1_registry_is_untouched`. The Event table remains outside `PHASE1_TABLES`; the stale assertion expects 15 definitions, while the current catalog source explicitly defines 17, including the two ADR-0077 additions.
- The PostgreSQL-enabled full-run summary has seven failed node IDs missing, so this node's prior failure count cannot be reconciled. Do not repeat it in this batch; retain it as unpassed until the stored run evidence is classified. No real PostgreSQL catalog or production data was touched.

## W2 / W10 runtime checks

- `tests/infrastructure/observability/test_logging.py::test_configure_logging_writes_json_and_is_idempotent`: **1 passed**. This verifies the helper only; logging is not yet wired into the API entry point.
- The first real-Uvicorn test attempt skipped two cases because the optional API server dependency was not installed in the fresh worktree. After installing the declared `api-server` extra, `tests/apps/test_api_server.py::test_a_real_uvicorn_serves_on_loopback_and_stops_gracefully` completed with **2 passed, 1 warning**.
- This remains partial W2 evidence; PostgreSQL + Iceberg combined recovery and explicit State/Event adapter close/reopen checks remain open.

## Acceptance boundary

The State/Event/Outcome results are module-level evidence only. The registry-count failure is not counted as passed, full-suite failure IDs remain incomplete, and no Phase exit gate is closed by this report.
