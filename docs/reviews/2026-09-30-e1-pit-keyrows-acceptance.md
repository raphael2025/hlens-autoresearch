# E1 PIT single-key rows and proof staging acceptance

Date: 2026-09-30

## Scope and decision

The accepted implementation is `codex/e1-phase1-progress@4a9da60`, cherry-picked from the isolated developer commit `codex/e1-pit-key-rows-runset-v2@7647351d7c3fc488069c8d0e39698b60f440e279`.

This is an acceptance of the narrow PIT v3 single-key row and private proof-staging slice only. It is not acceptance of Phase 1 or E1-CAP-1. Public `verify_unit`, frozen contracts, and ADRs were not changed. The normalizer change is limited to private bounded proof-selection and batch-ID traversal, under the coordinator authorization recorded in `PROJECT_MEMORY.md`.

Three independent acceptance roles reviewed the same developer SHA:

- Wide regression and static checks: **ACCEPT**.
- PIT/normalizer semantics and ADR boundary review: **ACCEPT**; no new O(H) Python container or unapproved storage/query protocol was found.
- Key-row, RunSet, proof, reader, and endpoint boundary checks: **ACCEPT with deferred-test and coverage notes**.

## Implementation evidence

- Canonical rows for a key and endpoint mappings now use bounded RunSet roots instead of `list(key_group)` and an O(H) `key_rows` mapping.
- Revision lookup uses bounded ordinal access; endpoint lookup preserves the previous zero/one/multiple-image behavior: missing endpoint skips the edge, one image maps it, multiple images fail closed.
- Private proof requests and staged proof batches use ordered RunSets and merge joins. Proof readers are not published before all requested units pass validation, so a late unit failure does not expose a proof prefix.
- Conflict heads are fully emitted to bounded evidence before conflict is reported. Legacy v2 tuple behavior and selector parity remain covered by direct tests.
- RunSet writes that fail may leave unreferenced immutable objects for later maintenance, consistent with ADR-0077 §6.1.3 and §9.

## Tests and checks

Developer run on `7647351`:

```text
uv run pytest tests/infrastructure/pit/test_selector_bounded_proofs.py tests/infrastructure/pit/test_selector_v3.py tests/infrastructure/pit/test_revision_record_view.py tests/infrastructure/pit/test_bounded_runs.py -q -k 'not iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
61 passed, 1 skipped, 1 deselected in 115.88s
```

Independent wide rerun on the same SHA:

```text
uv run pytest tests/infrastructure/pit/test_selector_bounded_proofs.py tests/infrastructure/pit/test_selector_v3.py tests/infrastructure/pit/test_revision_record_view.py tests/infrastructure/pit/test_bounded_runs.py -q -k 'not iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
61 passed, 1 skipped, 1 deselected in 117.93s (0:01:57)
```

The independent static rerun reported:

```text
uv run ruff check infrastructure/canonical/normalizer.py infrastructure/pit/selector.py tests/infrastructure/pit/test_revision_record_view.py tests/infrastructure/pit/test_selector_bounded_proofs.py tests/infrastructure/pit/test_selector_v3.py
All checks passed!
```

```text
uv run ruff format --check infrastructure/canonical/normalizer.py infrastructure/pit/selector.py tests/infrastructure/pit/test_revision_record_view.py tests/infrastructure/pit/test_selector_bounded_proofs.py tests/infrastructure/pit/test_selector_v3.py
5 files already formatted
```

```text
uv run mypy --follow-imports=silent infrastructure/pit/selector.py
Success: no issues found in 1 source file
```

The developer also reported `git diff --check` passing. A full-file mypy run on `normalizer.py` still reports nine existing Sequence/signature diagnostics at lines 226, 239, 251, 298, 322, 1050, 1183, 1219, and 1253. The new bisect diagnostic was fixed; the unrelated existing typing areas were left unchanged.

Independent boundary tests reported `7 passed in 17.29s`; a separate legacy/cross-day group reported `2 passed, 1 failed in 22.03s` (see deferred nodes below).

## Integrated regression

The accepted commit was cherry-picked onto the isolated integration candidate, producing `codex/e1-phase1-progress@4a9da60fdb659161e8b36a2d6315ce120bf8a9b9`. No changes were made to `main` or pushed.

Command:

```text
uv run pytest tests/infrastructure/parser/test_binance_archive_parser.py tests/infrastructure/parser/test_binance_archive_storage.py tests/infrastructure/revision/test_row_integrity.py tests/infrastructure/revision/test_rest_store_unit.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/pit/test_selector_bounded_proofs.py tests/infrastructure/pit/test_selector_v3.py tests/infrastructure/pit/test_revision_record_view.py tests/infrastructure/pit/test_bounded_runs.py -q -k 'not iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer and not iter_bounded_maps_three_day_spanning_edges_once'
```

Result:

```text
341 passed, 1 skipped, 1 deselected, 3 failed in 213.55s
```

The three failures were REST-store snapshot/head-movement tests, outside the changed files. The same three nodes were run on the pre-integration candidate `9e862f55632d3db1ad5cab89d1f51465e5fc4485` and failed with the same assertions (`3 failed in 1.72s`). They are pre-existing failures, not regressions from this slice. Per the two-run policy, retain the tests and failure evidence, defer these nodes, and do not include them as passing evidence.

## Deferred tests and remaining coverage

- `test_single_key_history_over_run_capacity_proof_and_legacy_parity` remains in the source with an explicit skip reason after two attempts. The first attempt expected a conflict where legacy last-write-wins behavior selects a single head; the second showed that duplicate REST ingest is folded and the fixture creates one Canonical row, not eight. This does not prove a real REST-ingest single-key history larger than RunSet capacity; that end-to-end capacity fixture remains open.
- `test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer` is an existing two-round deferred node; it was deselected, not counted as passing.
- `test_iter_bounded_maps_three_day_spanning_edges_once` failed on both the candidate (`2 passed, 1 failed in 22.03s`) and exact pre-change baseline `163efa25a9b3dc1871624367bb3661c512a7a326` (`1 failed in 18.57s`) with the same `CONFLICT/head_count=3` versus legacy `SELECTED` difference. This is a pre-existing deferred parity failure, not introduced by this commit.
- The integrated REST-store nodes `test_a_head_moved_between_element_and_lineage_reads_is_read_again`, `test_a_mixed_view_is_never_judged_the_newer_snapshot_is`, and `test_heads_that_keep_moving_end_in_a_bounded_conflict` each failed on both integration candidate and pre-integration baseline. They remain deferred, not passed.
- The new boundary tests directly cover repeated endpoint rejection; no new direct missing-endpoint test was added. ADR-0028 §3.2 and the prior `_mapped_edges` implementation define missing endpoints as skipped edges, which the new code preserves.

## Still blocking E1 / Phase 1

E1-CAP-1 has not passed. The partial 5d9dd71 probe remains non-main evidence and is not a formal capacity verdict. A full 32 MiB complete-process measurement on a clean SHA whose production paths match `main` is still required. Dataset lineage cardinality, full `revision_ids` output behavior, ADR-0093 Quality/Dataset integration gates, and other complete-workset costs remain open. No Phase 1 completion claim follows from this slice.
