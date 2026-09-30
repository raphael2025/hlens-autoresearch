# E1 Dataset PIT lineage carry acceptance

Date: 2026-09-30

## Scope and decision

The accepted implementation is `codex/e1-phase1-progress@7552a6c`, cherry-picked from the isolated developer commit `codex/e1-dataset-carry-runset@e12fbb8ff5cddefaff200a8861308819d153e98a`.

Three independent acceptance roles gave a scoped **ACCEPT**:

- Wide Dataset v3 sources regression and static checks.
- Boundedness, lineage/gap semantics, ordering, and reader-lifecycle review.
- Independent boundary and real-source parity regression.

The change is limited to `infrastructure/dataset/sources.py` and its direct tests. It does not modify ADRs, core contracts, `DatasetBuilder` v2 materialization behavior, or other public frozen contracts. It implements the already accepted ADR-0077 §6.1/§10 requirement that one key's history cannot be used as a memory bound.

## Implementation

`_evaluations` streams non-selected records without buffering. At the first `SELECTED` record, `_selected_evaluations` stages that key's selected suffix into bounded RunSets, groups occurrences by revision, validates the first and repeated lineage/gap evidence, joins selected occurrences to unique lineage records, restores evaluation ordinal order, and only then yields the selected suffix. It does not retain a revision-to-lineage dictionary or a full evaluation list.

All run capacities, fanout, and object limits come explicitly from `PitRunParams`; there is no O(H) fallback, implicit temporary directory, or new StorageAdapter query API. Reader and builder lifetimes use context managers and the upstream iterator is closed in `finally`.

The selected suffix must be fully spooled and validated before its first evaluation is returned. This increases first-selected latency and external object I/O as key history grows. The RunSet index stack grows with its derived tree depth (approximately depth × fanout); this is within ADR-0077's index-stack design but is not a claim of constant memory or a passed E1-CAP-1 gate.

## Tests and checks

Developer run on `e12fbb8`:

```text
uv run pytest tests/infrastructure/dataset/test_dataset_v3_sources.py -q
28 passed in 41.08s
```

Independent wide rerun on the same SHA:

```text
uv run pytest tests/infrastructure/dataset/test_dataset_v3_sources.py -q
28 passed in 41.25s
```

Independent boundary command:

```text
uv run pytest -q tests/infrastructure/dataset/test_dataset_v3_sources.py::test_one_large_key_streams_evaluations_with_only_one_record_lookahead tests/infrastructure/dataset/test_dataset_v3_sources.py::test_selected_key_history_spills_and_replays_in_original_order tests/infrastructure/dataset/test_dataset_v3_sources.py::test_late_duplicate_lineage_or_gap_fails_closed_and_closes_readers tests/infrastructure/dataset/test_dataset_v3_sources.py::test_first_interval_conflict_does_not_pull_the_next_conflict_instant tests/infrastructure/dataset/test_dataset_v3_sources.py::test_dataset_conflict_seals_only_first_interval_evaluation_and_closes_selector tests/infrastructure/dataset/test_dataset_v3_sources.py::test_v3_build_over_the_real_upstreams_selects_what_v2_selects
9 passed in 29.54s
```

It covered a selected key larger than capacity, ordinal replay, lineage/gap carry and conflicting evidence rejection, missing first lineage, 10,000-row ABSENT lazy behavior, conflict no-prefetch, early close, and real `PitSelectorKeySource` parity with the v2 point/interval path. No deferred node was run.

Independent static review accepted the algorithm and confirmed the scoped files, explicit `PitRunParams`, fail-closed conditions, and cleanup behavior. It did not run tests.

The developer's static checks:

```text
uv run ruff check infrastructure/dataset/sources.py tests/infrastructure/dataset/test_dataset_v3_sources.py
All checks passed!
```

```text
uv run ruff format --check infrastructure/dataset/sources.py tests/infrastructure/dataset/test_dataset_v3_sources.py
2 files already formatted
```

```text
uv run mypy --follow-imports=silent infrastructure/dataset/sources.py
Success: no issues found in 1 source file
```

`git diff --check` passed with no output.

After cherry-picking, the integrated candidate ran:

```text
uv run pytest tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/dataset/test_dataset_v3_builder.py -q
52 passed in 40.78s
```

The integrated candidate static rerun also passed: Ruff `All checks passed!`, format `2 files already formatted`, sources mypy `Success: no issues found in 1 source file`, and `git diff --check` exit 0.

## Preserved first-run failures

The first full source-suite attempt on the uncommitted implementation reported `1 failed, 21 passed, 2 errors in 199.81s`. The 10,000-row ABSENT lazy test incorrectly entered four RunSet staging layers and exhausted the test filesystem (`OSError: [Errno 28] No space left on device`); two later conflict fixtures then failed setup because the same pytest temp directory was full. Only that run's own 2.4 GiB pytest temp directory was cleaned. The implementation was changed to stream no-lineage prefixes, preserving the original lazy assertions. The original nodes and full suite subsequently passed; these were resource failures, not accepted product failures.

The new selected-history test initially had a wrong expected value for carried gap evidence (`1 failed, 5 passed in 0.51s`). The assertion was corrected to match the existing carry semantics; the same node passed on its second run (`1 passed in 0.45s`). A later targeted extension for contradictory gap attachment passed (`4 passed in 0.44s`). The test remains in the suite.

## Limits

No selected-run write-failure injection or oversized single-lineage-record test was run in the independent boundary selection. E1-CAP-1 still requires a clean SHA with production paths matching `main` and a complete-process 32 MiB measurement. This slice is not a Phase 1 acceptance.
