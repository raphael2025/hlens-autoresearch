# E1 bounded Quality v3 reporter acceptance

**Result: ACCEPT for the canonical-partition v3 reporter slice only.** This does not accept Dataset v3 integration, E1-CAP-1, or Phase 1.

## Scope

The implementation was developed on `codex/e1-quality-v3-reporter` and integrated into the isolated Phase 1 candidate `codex/e1-phase1-progress` as commits `4f7ff0a` and `d11097d`, corresponding to final developer SHA `5b97c064177d3dac371429cf5393ecda5a1579e0`.

The additive `QualityReporterV3` in `infrastructure/quality/report_v3.py` preserves the v1/v2 reporter and persisted legacy shape. It checks Raw-to-Canonical coverage with external RunSet joins; fully exhausts bounded PIT selection and checks pinned key coverage; checks REST responses one page at a time with `PAGE_LIMIT` enforced before index collection and shared persisted-body verification on complete and incomplete pages; projects complete events, event revisions, and gaps to bounded streams; publishes streams before the manifest commit point; and re-derives and compares stored streams for `existing_only`.

The `existing_only` rule is clarified in ADR-0093: no clock read, manifest/catalog mutation, or write to report evidence storage. Temporary sorted and expected-stream objects may be published only through a separately configured scratch adapter. The implementation rejects reuse of the same adapter object, but callers remain responsible for ensuring different adapters do not target the same physical namespace.

## Independent acceptance

The coordinator independently ran the final candidate suite:

```text
uv run pytest -q tests/infrastructure/quality/test_report_v3.py tests/infrastructure/quality/test_reporter.py
43 passed in 52.14s

uv run ruff check infrastructure/quality/report_v3.py tests/infrastructure/quality/test_report_v3.py
All checks passed!

uv run ruff format --check infrastructure/quality/report_v3.py tests/infrastructure/quality/test_report_v3.py
2 files already formatted

git diff --check HEAD~1 HEAD
```

The final status was clean. Two independent review roles accepted the data semantics, manifest/replay behavior, and REST page hardening. On the preceding reporter commit, additional independent checks were:

```text
uv run pytest tests/infrastructure/quality/test_report_streams.py -q
33 passed in 0.25s

uv run pytest tests/infrastructure/streaming/test_run_record_seek.py -q
3 passed in 0.12s
```

The final REST guard was independently reviewed with focused nodes:

```text
uv run pytest tests/infrastructure/quality/test_report_v3.py::test_rest_page_count_above_rule_limit_fails_before_collecting_indexes tests/infrastructure/quality/test_report_v3.py::test_complete_rest_page_still_runs_persisted_verifier tests/infrastructure/quality/test_report_v3.py::test_incomplete_rest_response_page_fails_reporter_gate tests/infrastructure/quality/test_report_v3.py::test_rest_gate_accepts_multiple_responses_with_elements_held_by_first_page -q
4 passed in 3.68s
```

Coverage includes the v3 and legacy reporter paths, empty and missing Canonical snapshots, full-day bar gaps, multiple REST responses, incomplete-page rejection, conflict-head revision streaming, aggregate-trade discontinuities, clock floors, missing normalized Canonical rows, manifest-commit failure, bounded row-byte checks, and `existing_only` replay with no clock/evidence writes.

## Remaining boundaries

- Dataset v3 does not yet bind and consume the new manifest/evidence streams; its end-to-end quality reference proof remains open.
- E1-CAP-1 has not been measured on a main-matching clean SHA. This reporter slice makes no fixed total-RSS claim; PIT retains a per-key working-set boundary.
- Scratch/evidence physical namespace separation and complete rule-owned identity-hash/snapshot-table configuration are caller-owned prerequisites.
- The developer's `mypy` run reported existing dependency diagnostics; a reviewer found the production reporter itself clean when checked alone, while checking the new test module also reports test typing errors. Mypy is not recorded as passing for this slice.
- Real PostgreSQL append/replay was not run.

E1-CAP-1 remains open and Phase 1 remains unaccepted.
