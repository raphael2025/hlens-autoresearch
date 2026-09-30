# Quality v3 rule-owned registry — 2026-09-30

## Integrated slice

Candidate `codex/quality-v3-rule-registry@60301f6703d75abdd81f50e00af5ea70d7bd7442` received three independent, limit-scoped ACCEPT reviews. It was cherry-picked to `codex/project-consolidation` as `c8bfa92`.

`QualityReporterV3` now owns a fixed identity-hash registry and finite snapshot-table sets for `agg_trades` and `klines_1m`. Canonical, archive/raw and REST-response snapshots are required; precedence evidence remains allowed and optional. The reporter no longer accepts caller-supplied identity hashes, hash limits, or table sets. The registered hash mapping matches the prior mapping and report identity inputs remain unchanged. No core, schema, ADR, frozen contract, or manifest-store generic validation changed.

## Independent review and verification

All three reviewers accepted the slice. They checked the two per-data-type table sets, precedence optionality, identity/report-ID compatibility, internal call sites, and the exact candidate diff. Their focused checks included `23 passed` for reporter plus Dataset integration, Ruff clean, mypy clean, and `git diff --check` clean.

After integration, the combined direct regression was rerun:

```text
uv run --offline pytest -q --tb=short tests/infrastructure/quality/test_report_v3.py tests/infrastructure/dataset/test_quality_v3_dataset_integration.py tests/infrastructure/quality/test_manifest_store.py
58 passed in 46.11s
```

The exported constructor's four removed keyword parameters are a source-compatibility change for external callers that still pass arbitrary registry configuration. All in-repository callers were migrated. This is the intended narrowing for the accepted rule-owned registration boundary; persisted report IDs and legacy replay paths remain unchanged.

This is a Quality registry slice only. It does not prove real PostgreSQL append/replay, E1-CAP-1, or Phase 1 acceptance.
