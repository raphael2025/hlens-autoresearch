# ADR-0094 implementation review ledger

**Status:** implementation in progress on `codex/pit-conflict-v3`. This record does not claim E1-CAP-1 or Phase 1 acceptance.

## Compatibility blocker deferred after two failed runs

The ADR-0077 historical v2 schema SHA pins fail for `ResearchDatasetManifest` and `UniverseMember`. The same failure exists in the clean ADR coordinator base (`3b3efb2`), so this implementation does not change the pins or classify full historical v2 schema replay as accepted.

### Run 1 — implementation worktree

Command:

```text
uv run pytest -q tests/test_adr_0094_pit_conflict_evidence.py tests/test_adr_0077_evidence_manifest.py
```

Raw result:

```text
3 failed, 104 passed in 1.25s
```

The three failures were: a newly exposed fixture issue where evidence records were constructed at 2.5.0 before entering the historical 2.4.0 scope; and the `ResearchDatasetManifest` / `UniverseMember` historical 2.2.0 schema SHA pins. The fixture issue was corrected by explicitly rebuilding the nested references at 2.4.0. The two schema pin failures were not edited.

### Run 2 — clean base worktree

Environment: detached clean worktree at `3b3efb2` in `/tmp/hlens-pit-base-check`.

Command:

```text
uv run pytest -q tests/test_adr_0077_evidence_manifest.py::test_the_v2_schemas_change_only_their_envelope_default --tb=short
```

Raw result:

```text
2 failed, 3 passed in 0.86s
```

The same two tests failed with the same expected and actual hashes. The baseline committed schema normalized from 2.4.0 to 2.2.0 also produces the observed hashes, confirming the mismatch predates this branch. Current 2.5.0 schemas normalized to 2.2.0 match that baseline normalized output byte-for-byte.

**Disposition: DEFERRED after two failures**, per the project two-run rule. Follow-up owner: project coordinator / compatibility QA after this implementation branch is reviewed. Do not alter old pins to make this green. The ADR-0094 implementation does not claim historical v2 schema/hash acceptance from this check.

## Separate pre-existing PIT test deferred after two failures

`tests/infrastructure/pit/test_selector_v3.py::test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer` expects five selected lineages, including a lone REST key. Both the implementation branch and clean base produce four.

Run 1 command:

```text
uv run pytest -q tests/infrastructure/pit/test_selector_v3.py
```

Raw result before focused fixes to four unrelated expectation/monkeypatch failures:

```text
5 failed, 27 passed in 102.11s
```

Run 2 command in `/tmp/hlens-pit-base-check` at detached `3b3efb2`:

```text
uv run pytest -q tests/infrastructure/pit/test_selector_v3.py::test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer --tb=short
```

Raw result:

```text
1 failed in 4.96s
```

Both runs report `len(legacy.lineage) == 4`, not the fixture comment's expected 5. **Disposition: DEFERRED after two failures.** Do not change the asserted acceptance number in this ADR-0094 branch; follow-up belongs to PIT test ownership.

## Scope notes

- `schemas/v1/` has no modified files in this worktree.
- All six-stream manifest replay paths are selected from their recorded 2.3.0 / 2.4.0 envelope; 2.5.0 manifests use seven streams.
- Per-key record / edge / availability input remains a separate O(N) capacity question.

## Implementation verification before freeze

The following command outputs were collected on `codex/pit-conflict-v3` after the focused fixes. The deferred failures above were excluded explicitly where noted; their golden/assertion inputs were not changed.

```text
uv run pytest -q tests/infrastructure/dataset/test_evidence_tree.py tests/infrastructure/dataset/test_dataset_v3_builder.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/dataset/test_verify_v3.py tests/infrastructure/dataset/test_v2_golden_compat.py
124 passed in 117.54s
```

```text
uv run pytest -q tests/test_contracts.py tests/test_contract_version_scope.py tests/test_adr_0094_pit_conflict_evidence.py
324 passed in 0.97s
```

```text
uv run pytest -q tests/test_adr_0094_pit_conflict_evidence.py tests/test_adr_0077_evidence_manifest.py -k 'not the_v2_schemas_change_only_their_envelope_default'
102 passed, 5 deselected in 0.86s
```

```text
uv run pytest -q tests/test_adr_0077_evidence_manifest.py -k 'not the_v2_schemas_change_only_their_envelope_default'
99 passed, 5 deselected in 0.38s
```

```text
uv run pytest -q tests/infrastructure/pit/test_selector_v3.py -k 'not iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
31 passed, 1 deselected in 95.87s
```

```text
uv run pytest -q tests/infrastructure/pit/test_precedence_runs.py tests/infrastructure/pit/test_bounded_runs.py
23 passed in 5.96s
```

```text
uv run pytest -q tests/infrastructure/pit/test_precedence_runs.py::test_iter_bounded_keeps_parity_without_unmeasured_external_traversal tests/infrastructure/pit/test_selector_v3.py::test_iter_bounded_spills_and_emits_every_conflict_head_without_a_tuple
2 passed in 2.24s
```

```text
uv run python -m compileall -q core infrastructure tests/infrastructure/dataset tests/infrastructure/pit
exit code 0

uv run ruff check <changed Python files>
All checks passed!

uv run mypy --follow-imports=silent core/contracts/universe.py core/contracts/registry.py core/domain/base.py infrastructure/pit/precedence_runs.py infrastructure/pit/selector.py infrastructure/dataset/builder.py infrastructure/dataset/evidence.py infrastructure/dataset/sources.py infrastructure/dataset/verify_v3.py
Success: no issues found in 9 source files
```

The normal dependency-following mypy command below remains nonzero on 13 diagnostics in untouched files: `infrastructure/catalog/iceberg_adapter.py`, `infrastructure/revision/row_integrity.py`, `infrastructure/pit/view.py`, and `infrastructure/canonical/normalizer.py`. The clean base run had 15 diagnostics, including pre-existing errors in baseline `infrastructure/dataset/evidence.py` and `infrastructure/dataset/builder.py`; this branch removes those two dataset diagnostics. **Disposition: baseline-deferred**, not an ADR-0094 implementation failure.

```text
uv run mypy core/contracts/universe.py core/contracts/registry.py core/domain/base.py infrastructure/pit/precedence_runs.py infrastructure/pit/selector.py infrastructure/dataset/builder.py infrastructure/dataset/evidence.py infrastructure/dataset/sources.py infrastructure/dataset/verify_v3.py
base 3b3efb2: 15 errors in 6 files (checked 9 source files)
implementation branch: 13 errors in 4 untouched files (checked 9 source files)
```

The branch-only changed-file check with `--follow-imports=silent` passes all nine target source files.
