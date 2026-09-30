# Dataset v3 historical manifest replay fixtures — 2026-09-30

## Integrated slice

Candidate `codex/e1-replay-golden-port@7bfb15790921292e1d8f9d3970c6bf1273e646e3` was independently ACCEPTed by three reviewers and cherry-picked to `codex/project-consolidation` as `f19a06c`.

Only these files changed:

- `tests/golden/v2_3_0/dataset_v3_manifest.json`
- `tests/golden/v2_4_0/dataset_v3_manifest.json`
- `tests/test_adr_0077_evidence_manifest.py`

The test reconstructs each historical manifest with its recorded version, checks canonical bytes and SHA-256, parses the JSON again, and confirms the original content hash. Independent reconstruction from old baselines `e80748c` (2.3.0) and `9925f0a` (2.4.0) produced the exact pinned hashes:

- 2.3.0: `40899f7552ed35bbb5f505b2ba30c7007c5400190ed65664f947954e291c1da5`
- 2.4.0: `a2b89b7e5444399748d91e3d51021ec82970bfdc2edd29f39a2216fcf3a9930c`

This preserves the historical six-stream shape; the current 2.5 contract's `pit_conflicts` addition is not backported into the old fixtures.

## Verification and existing schema failures

The focused replay nodes passed on both candidate and consolidation after cherry-pick:

```text
uv run --offline pytest -q --tb=short tests/test_adr_0077_evidence_manifest.py -k legacy_v3_manifest_goldens_replay_at_their_recorded_version
2 passed, 104 deselected
```

Ruff, format, and diff checks passed. The full evidence-manifest test file has three existing schema failures. To verify provenance, an independent reviewer ran the same broader command on the parent and candidate:

```text
Parent f13676d: 3 failed, 104 passed
Candidate 7bfb157: 3 failed, 106 passed
```

The unchanged failures are the current-model schema description export, plus the historical 2.2.0 SHA pins for `ResearchDatasetManifest` and `UniverseMember`. The candidate adds two passing tests and changes none of the relevant schemas, pins, or existing assertions. These failures are not counted as passing and are not caused by this replay slice.

One test strips the fixture's trailing newline before byte comparison; it pins canonical manifest payload bytes and SHA, not the fixture file's final newline count.

## Acceptance boundary

This accepts only the fixed historical replay evidence. It does not accept full Dataset Quality, ADR-0094 implementation, E1-CAP-1, or Phase 1. Preserve the existing schema mismatch follow-up; do not adjust schema or legacy pins just to make this slice green.
