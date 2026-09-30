# E1 Quality v3 manifest store and report identity acceptance

**Result: ACCEPT for the bounded manifest-store primitive and report-identity derivation only.** This does not accept the v3 reporter, end-to-end evidence completeness, Dataset integration, E1-CAP-1, or Phase 1.

## Scope

Developer branch `codex/e1-quality-manifest-store` ended at `d5be275b563de74be68401be298b0012a65cd722`. Its three commits were cherry-picked to the isolated candidate `codex/e1-phase1-progress`, producing code commit `39bf84f` before this documentation update.

Changed files:

- `infrastructure/quality/manifest_store.py`
- `infrastructure/quality/report_projection.py`
- `tests/infrastructure/quality/test_manifest_store.py`
- `tests/infrastructure/quality/test_report_projection.py`

The new `QualityReportManifestStore` validates a single fixed-size manifest row against registered Arrow schema; applies explicit row and identity byte caps before Arrow materialization; restricts snapshot bindings to a finite rule-owned allowlist with explicit required bindings; requires subject snapshot and matching table binding to appear together; and validates UTC intervals and all three stream references. `lookup` is read-only, checks duplicate IDs, and normalizes the stored row. `commit` is append-only, exact-replay idempotent, rejects different content for the same report ID, propagates parent-snapshot conflicts, and reads back the committed row.

Report IDs use a fixed domain-separated, chunked canonical identity hash. The identity contains the rule ID/version/hash, a bounded and key-sorted rule-hash map, full subject fields, and every normalized snapshot binding. The map must contain `quality`, equal to the manifest's `quality_rule_hash`; changing PIT or policy hashes changes the ID. The v3 report rule specification now records the domain, canonical encoding, identity fields, limits, and output format, so the rule hash changes with this contract. Legacy v1/v2 report paths and table schemas were not changed.

## Three independent QA roles

All three roles accepted final developer SHA `d5be275b563de74be68401be298b0012a65cd722`:

- Full Quality regression: `137 passed in 38.30s`; Ruff passed; format reported `4 files already formatted`; mypy succeeded for 4 source files; diff-check clean.
- Boundary QA: `36 passed in 3.41s` for the manifest store and report-ID digest node. Additional probes passed for mapping-order invariance, PIT/policy hash sensitivity, map-count and quality-hash guards, bounded long UTF-8 hashing, and the 256-character catalog ID limit.
- Static ADR review: ACCEPT. It confirmed that identity includes the complete caller-supplied rule-hash map, subject metadata, and exact pinned bindings, and that the identity protocol is included in the v3 rule spec.

The static review records two trusted-configuration boundaries: callers must supply the complete rule-owned PIT/policy hash map and required snapshot-table set; the store bounds and validates these inputs but cannot know a rule's registration set independently. Also, `CatalogAdapter.scan_columns` limits row count but has no per-row byte cap, so an oversized out-of-band row is materialized by the adapter before store validation. This slice does not claim bounded reads for hostile out-of-band catalog rows.

## Integrated candidate verification

On `codex/e1-phase1-progress@39bf84f`:

```text
uv run pytest -q tests/infrastructure/quality/test_report_projection.py tests/infrastructure/quality/test_report_streams.py tests/infrastructure/quality/test_reporter.py tests/infrastructure/quality/test_listing_report.py tests/infrastructure/quality/test_manifest_store.py
137 passed in 38.02s

uv run ruff check infrastructure/quality/manifest_store.py infrastructure/quality/report_projection.py tests/infrastructure/quality/test_manifest_store.py tests/infrastructure/quality/test_report_projection.py
All checks passed!

uv run ruff format --check infrastructure/quality/manifest_store.py infrastructure/quality/report_projection.py tests/infrastructure/quality/test_manifest_store.py tests/infrastructure/quality/test_report_projection.py
4 files already formatted

uv run mypy --follow-imports=silent infrastructure/quality/manifest_store.py infrastructure/quality/report_projection.py tests/infrastructure/quality/test_manifest_store.py tests/infrastructure/quality/test_report_projection.py
Success: no issues found in 4 source files

git diff --check HEAD~3 HEAD
```

The diff check exited 0 with no output.

## Remaining gates

- Integrate the manifest store and identity helper into the registered canonical-partition v3 reporter, supplying its complete PIT/policy rule-hash map and required input tables.
- Prove end-to-end evidence-gap completeness and connect Dataset v3 reads/writes to manifest-backed reports.
- Measure the complete process on a main-matching clean SHA against the 32 MiB E1-CAP-1 threshold.

E1-CAP-1 remains unproven and Phase 1 remains open.
