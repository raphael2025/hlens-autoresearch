# ADR-0094 Contract 2.5.0 follow-up review

**Scope:** current-version checks, report DTO support, and console fixtures following ADR-0094's
2.4.0 → 2.5.0 contract bump. Work was isolated on `codex/pit-conflict-current-version-tests`,
based on implementation follow-up `023c6c2`. This review does not accept E1-CAP-1 or Phase 1.

## Changes

- Updated tests which assert the current contract version or current registry size. The previous
  contract versions, model order, and historical schema hashes remain covered.
- Added `validation_report` 2.5.0 support to the API and Web DTO registries. API OpenAPI JSON and
  generated Web types reflect the supported baseline; 2.4.0 remains supported.
- Registered the six existing 2.4.0 report files as immutable legacy fixtures. Regeneration in a
  fresh interpreter under `contract_schema_version_scope("2.4.0")` reproduced all six existing
  IDs byte for byte. Added the six 2.5.0 current reports without replacing old files.
- Updated Python and Web fixture expectations and descriptions for both current and retained
  versions. Corrected the pre-existing retro-audit fixture count from one to two.

## Verification

The following checks ran on the follow-up worktree:

```text
uv run --offline pytest -q --tb=short tests/test_revision_contracts.py::test_contract_version_and_kind_are_unchanged tests/test_adr_0055_versions.py tests/test_adr_0088_contract_240.py tests/test_adr_0054_0057_versions.py tests/test_universe_contracts.py tests/test_construction_and_versioning.py tests/test_information_flow.py tests/test_adapter_contracts.py tests/test_experiment_identity.py --deselect='tests/test_adr_0088_contract_240.py::test_an_old_effect_without_a_kind_still_reads_as_return_autocorrelation' --deselect='tests/test_adapter_contracts.py::test_pre_b3_current_schemas_are_byte_identical[EventSpec]' --deselect='tests/test_adapter_contracts.py::test_pre_b3_current_schemas_are_byte_identical[ResearchDatasetManifest]' --deselect='tests/test_adapter_contracts.py::test_pre_b3_current_schemas_are_byte_identical[StrategySpec]' --deselect='tests/test_adapter_contracts.py::test_pre_b3_current_schemas_are_byte_identical[UniverseMember]'
1308 passed, 5 deselected in 8.15s
```

```text
uv run --offline pytest -q --tb=short tests/infrastructure/catalog/test_adr_0077_tables.py tests/apps/test_report_dto.py tests/apps/test_console_fixtures.py tests/research/reports/test_console_fixture_writers.py
172 passed, 1 warning in 29.10s
```

The warning is the existing Starlette `httpx` TestClient deprecation notice.

## Three independent acceptance roles

- **Contract QA:** accepted the narrow 2.5.0 contract/version registry slice. It independently
  reran the current-version assertion and the 324-test contract/schema subset after the one-test
  version assertion correction.
- **PIT QA:** accepted only the bounded PIT summary/output slice at frozen implementation SHA
  `15655f5f24960328efece5028e9f01ebc5cadb8e`; its clean detached run was `2 passed in 2.32s`.
- **Dataset QA:** accepted only the Dataset integration slice at the same frozen implementation
  SHA; its clean detached run was `226 passed, 5 deselected in 114.25s`. The separate catalog
  collection issue was the stale 2.4.0 current-version assertion corrected in this follow-up;
  catalog/API/report follow-up coverage is included in the `172 passed` result above.

These are separate narrow acceptances; none certifies the E1 capacity gate or Phase 1.

```text
npm test
120 passed, 0 failed
```

```text
npm run build
tsc --noEmit && tsc --noEmit -p tsconfig.test.json && vite build
exit code 0
```

```text
uv run --offline ruff check <changed Python files>
All checks passed!

git diff --check
exit code 0
```

```text
uv run --offline mypy --follow-imports=silent apps/api/report_dto.py
Success: no issues found in 1 source file
```

## Deferred checks and remaining scope

The five deselected nodes are previously recorded issues, not changes made to the contract by this
follow-up:

1. `tests/test_adapter_contracts.py::test_pre_b3_current_schemas_are_byte_identical` for `EventSpec`,
   `ResearchDatasetManifest`, `StrategySpec`, and `UniverseMember`. These four historical schema
   hash mismatches predate ADR-0094 and are listed in the 2026-09-29 W1 deferral record. A clean
   baseline also reproduces the `ResearchDatasetManifest` and `UniverseMember` mismatches. Pins
   and frozen schemas were not edited.
2. `tests/test_adr_0088_contract_240.py::test_an_old_effect_without_a_kind_still_reads_as_return_autocorrelation` selects a
   `UniverseMember` golden fixture without an `effects` field. The same test defect was deferred
   before ADR-0094; neither its assertion nor a historical pin was changed.

The ADR-0077 v2 schema pin failures for `ResearchDatasetManifest` / `UniverseMember` and the
bounded-PIT lineage-history fixture failure remain deferred after their baseline reproductions as
recorded in the implementation ledger. No capacity benchmark was run here. PIT per-key history /
graph memory and the complete process 32 MiB E1-CAP-1 gate remain open, so Phase 1 remains blocked.

**Phase acceptance:** none. This follow-up accepts only the current contract-version and report
fixture compatibility slice; it does not accept E1-CAP-1, the full ADR-0094 delivery, or Phase 1.
