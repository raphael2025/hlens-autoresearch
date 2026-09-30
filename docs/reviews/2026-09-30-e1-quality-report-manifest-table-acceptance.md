# E1 Quality Report Manifest Catalog Table Acceptance

**Result: ACCEPT for the Catalog schema/registry slice only.** This does not accept the ADR-0093 Quality report implementation, E1-CAP-1, or Phase 1.

## Scope

Developer branch `codex/e1-quality-report-manifest-table`, commit `f6abf7b45849264f2a1ef1b712edb9b3224b385d`, was based on `aa8ec31ae73a5ecb7052bf3347ed6eddfbf66d2e`. The change adds the unpartitioned `quality.data_quality_report_manifests` table as table 18 in the Phase 1 registry. It defines fixed top-level IDs 1–14 and nested IDs 15–38 for snapshot bindings and references to the three evidence streams. Existing legacy report fields retain their types and requiredness; existing tables, order, and pinned layouts remain unchanged.

The slice changes only:

- `infrastructure/catalog/phase1_tables.py`
- `tests/infrastructure/catalog/phase1_support.py`
- `tests/infrastructure/catalog/test_phase1_tables.py`
- `tests/infrastructure/catalog/test_adr_0077_tables.py`

No report writer/reader, Dataset integration, core contract, or ADR was changed. ADR-0093 remains accepted as a design decision, with implementation still outstanding.

## Independent acceptance

Three independent QA roles accepted this bounded slice:

- Full Catalog suite: `189 passed, 59 skipped in 3.46s` (QA run).
- Targeted schema, golden layout, field ID, registry-order, and B2 compatibility nodes: `10 passed in 0.08s`.
- Arrow row-builder probe: `rows=1 columns=14`.
- Ruff: `All checks passed!`; formatting: `4 files already formatted`; changed-file mypy: `Success: no issues found in 4 source files`; `git diff --check` clean.
- PostgreSQL append/replay node: `1 skipped in 0.07s` because no PostgreSQL catalog URI was configured. This is not a pass and leaves real catalog write/replay behavior unverified.

One non-blocking implementation obligation was recorded: Iceberg `ListType` does not enforce a maximum `snapshot_bindings` length. A future report writer/validator must enforce that the bindings equal the finite rule-input set and are sorted by table. The table schema alone cannot guarantee this.

## Integrated candidate

The accepted commit was cherry-picked to `codex/e1-phase1-progress` as `c230b23`. The integrated command and result were:

```text
uv run pytest -q tests/infrastructure/catalog
189 passed, 59 skipped in 3.83s
```

Skipped cases remain unverified and are not counted as passing. The catalog suite validates the schema/registry surface; it does not complete ADR-0093's stream serialization, report read/write/replay, Dataset v3 quality binding, or the finite-set runtime check.

## Remaining Phase 1 work

- Implement and independently validate the ADR-0093 report writer/reader and three evidence streams.
- Wire report manifest evidence into Dataset v3 and enforce finite, sorted snapshot bindings.
- Close the remaining bounded-memory review items, including the `revision_ids` interface boundary and full finalization/workset costs.
- Run the complete main-matching E1-CAP-1 process measurement against the unchanged 32 MiB threshold.

E1-CAP-1 remains unproven and Phase 1 remains open. This acceptance is limited to the registered Catalog schema slice.
