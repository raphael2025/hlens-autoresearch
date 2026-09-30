# Dataset Quality v3 consumer integration review — 2026-09-30

## Scope and disposition

Integrated into `codex/project-consolidation` as a squash of candidate `d31eef35824218eb03a6472eb609c5beed43aba7` (based on `974f708`). This is a narrow Dataset Quality consumer/replay slice, not E1-CAP-1 or Phase 1 acceptance. Three independent acceptance roles reviewed the final candidate SHA and approved within that scope.

The slice adds Quality v3 manifest consumption against the Dataset's fixed PIT snapshot, bounded readers for replay streams, sorted bounded gap joining, fail-closed behavior when the manifest table is not bound, and a source factory guard requiring the returned quality parameters to match the requested parameters. Legacy 2.3/2.4 evidence continues through the pinned legacy source; a seeded historical 2.4 manifest was replayed through production `ManifestStore.load_any` and `StreamingEvidenceVerifier` paths.

## Verification evidence

Developer aggregate command on candidate SHA `d31eef3`:

```text
uv run --offline pytest -q --tb=short tests/infrastructure/dataset/test_quality_v3_source.py tests/infrastructure/dataset/test_quality_v3_dataset_integration.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/test_adr_0077_evidence_manifest.py tests/test_adr_0094_pit_conflict_evidence.py -k 'not test_the_committed_schemas_of_the_new_models_match_the_contracts and not test_the_v2_schemas_change_only_their_envelope_default'
135 passed, 3 skipped, 6 deselected in 35.37s
```

The three skips are exactly the two point/interval variants of `test_v3_build_over_the_real_upstreams_selects_what_v2_selects` and `test_pit_keys_out_of_order_fail_the_real_build_closed`. Each had failed in two prior rounds and is explicitly deferred under the project rule; none is counted as passing and none was rerun in this slice. The six deselected schema-envelope nodes reproduce existing JSON-schema description / historical schema-hash mismatches on the clean baseline. No schema or frozen contract schema file was changed.

Candidate static checks: Ruff, formatting, targeted MyPy for `quality.py` and `sources.py`, and `git diff --check` passed. Independent reviewers separately reported the focused Quality source suite `4 passed`, and the pinned-snapshot, factory guard, historical replay, and exact-bound consumer nodes `4 passed`.

## Acceptance limits and remaining work

- The accepted decision in ADR-0093 specifies binding the Quality report-manifest table snapshot; this implementation enforces that binding for the 2.5+ manifest path while preserving 2.3/2.4 replay.
- Full rule-owned identity-hash / snapshot-table registration and whole-process working-set accounting remain incomplete.
- Real PostgreSQL append/replay was not run because no catalog URI was available.
- E1-CAP-1, its full-process 32 MiB evidence, and Phase 1 remain open.
- The three explicitly skipped nodes and six baseline schema-envelope mismatches remain visible follow-up items.

## Files integrated

`core/contracts/universe.py`, `infrastructure/dataset/builder.py`, `infrastructure/dataset/quality.py`, `infrastructure/dataset/sources.py`, `tests/infrastructure/dataset/dataset_support.py`, `tests/infrastructure/dataset/test_dataset_v3_sources.py`, `tests/infrastructure/dataset/test_quality_v3_dataset_integration.py`, `tests/infrastructure/dataset/test_quality_v3_source.py`, `tests/test_adr_0077_evidence_manifest.py`, and `tests/test_adr_0094_pit_conflict_evidence.py`.
