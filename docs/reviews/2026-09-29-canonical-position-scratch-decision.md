# DECISION PACKET: Canonical position-index scratch ownership

## State

**Accepted by the project PM on 2026-09-29: Option A.** The configuration owner is
`infrastructure.Settings` and the production composition roots. The accepted choice does not
change E1-CAP-1's 32 MiB process-workset gate and does not claim a bound on total spill bytes.

## Approved scope checked

- `PROJECT_STATUS.md` §1, §4, §5, §6, and §12 keep Phase 1 open and E1-CAP-1 as its capacity
  blocker. D-E1-CAP-ARCH authorizes E1-R infrastructure slices against the current `main` baseline;
  the complete 32 MiB process-workset gate remains in force.
- ADR-0075 §8 explicitly leaves temporary-directory placement in the E1 scope. Its scan rule does
  not grant a storage lifecycle or a configured scratch root.
- ADR-0076 requires explicit stream resource ownership and cleanup, but covers the normalizer result
  API rather than defining the position-index scratch provider.
- ADR-0077 §3.5 / DQ-11 defines immutable content-addressed storage for persistent evidence objects;
  it does not grant a delete operation or define ephemeral scratch ownership. Reusing it for
  temporary position files would leave permanent objects after each call.
- `core/contracts/storage.py::StorageAdapter` is frozen and only exposes stage/publish/lookup/read.
  This task must not add scratch/delete methods to it.

## Verified implementation facts

In `infrastructure/canonical/normalizer.py`, `_PositionIndex`:

- creates a `tempfile.TemporaryDirectory(prefix="hlens-positions-")` with no explicit parent, so
  path selection follows the process-wide Python temp-directory rules;
- stores each input position in a SQLite table, caps SQLite's page cache at 1 MiB, and writes the
  sorted positions to a fixed-width rank file during finalization;
- exposes lazy rank reads and closes the SQLite connection, file descriptor, and temporary
  directory on close/destruction.

This establishes that Python position tuples were removed from the long-lived survey/index API.
It does not establish that the scratch filesystem is outside cgroup memory (the project's default
`/tmp` is documented as tmpfs), that disk usage has a configured quota, or that cleanup is timely
on all process termination paths. The E1 probe redirects `TMPDIR` to its per-run directory, but
normal production callers do not provide a scratch path.

## Question

Who owns the explicit, non-default scratch root and its capacity/cleanup policy for normalizer
position indexes?

## Options

### A. Inject an infrastructure scratch root (**accepted**)

`Settings.canonical_scratch_uri` defaults to repository-owned `data/scratch`, may be overridden by
`HLENS_CANONICAL_SCRATCH_URI`, must be an absolute local `file://` URI, and must not overlap
`warehouse_uri` or `staging_uri`. The production composition roots pass its `Path` explicitly to
`CanonicalNormalizer` / `PitSelector` / tools. Each position index creates a private child
directory under that root, uses no process-default temp location, and cleans it up on close or
failure. An unusable root is rejected before scans or writes. `StorageAdapter` remains unchanged.

- Pros: explicit location and ownership; temporary objects remain ephemeral; tests can prove all
  created files are under the supplied root and removed after close/failure.
- Costs: requires coordinated edits in existing call sites under `infrastructure/pit/`, dataset /
  quality consumers, and tools. Total scratch-disk use remains O(N); no disk-quota guarantee is
  made. Exhaustion fails closed through the underlying filesystem error.

### B. Use a dedicated private scratch provider

Introduce an infrastructure-only scratch provider with create/open/close/delete operations and
explicit filesystem/capacity policy, independent of `StorageAdapter`.

- Pros: clear lifecycle boundary and future reuse for parser / other spill paths.
- Costs: new cross-module abstraction and configuration; larger design/review surface than this
  canonical slice.

### C. Keep relying on `TMPDIR`

Require the runtime/operator to set `TMPDIR`, while retaining Python's default temp resolution.

- Pros: small code change and compatible with the existing probe behavior.
- Costs: normalizer itself cannot distinguish an intentional scratch root from a system default;
  production path safety depends on external process configuration and is not enforced by the
  API. This does not close the identified path-ownership gap.

### D. Store scratch through `StorageAdapter`

Use content-addressed immutable objects for each position run.

- Rejected recommendation: the contract has no delete/temporary-object lifecycle, so each
  normalization would leave persistent orphan objects. Adding delete semantics to the frozen
  contract is outside this scope.

## Rationale and limits

The PM selected **A** to make scratch placement explicit and keep transient index files out of
`StorageAdapter`'s immutable object lifecycle. The SQLite page cache remains fixed; its ordered
read uses an explicit database index so SQLite does not need a default-location sort B-tree. The
database, index, and rank file still consume O(N) persistent scratch bytes. This is not a disk quota
and not E1-CAP-1 evidence.

## Acceptance criteria after a decision

1. No normalizer position-index path resolves to the process default temp directory implicitly.
2. The configured scratch root is validated before any Raw scan or write; missing/unusable root
   fails closed before side effects.
3. SQLite database, SQLite sort temporaries, and rank file all use a documented filesystem. The
   implementation must account for SQLite's temporary-file placement, not only the database path.
4. Index close, early iterator close, read/write exceptions, and normalizer cache eviction release
   connections/descriptors and remove per-index files; tests assert directory contents and no
   retained references.
5. Structural tests prove no Python container grows with unit positions; disk usage is reported as
   O(N) spill and is not misrepresented as a hard byte bound.
6. Run canonical non-PostgreSQL focused tests, lint and type checks; later run the approved isolated
   E1 memory matrix on the current code line. These checks remain separate from PostgreSQL tests.

## Implementation and acceptance record

Commands run read-only against the isolated `main` worktree:

```text
git worktree add -b codex/canonical-position-bounds \
  /home/raphael/.codex/worktrees/canonical-position-bounds/hlens-autoresearch main
```

Output:

```text
Preparing worktree (new branch 'codex/canonical-position-bounds')
HEAD is now at e584187 test(state): repair persistence regression fixtures
```

```text
rg -n 'class StorageAdapter|TemporaryDirectory|tempfile' \
  core infrastructure/canonical tests/infrastructure/canonical
```

Relevant output:

```text
infrastructure/canonical/normalizer.py:49:import tempfile
infrastructure/canonical/normalizer.py:184:        self._temporary = tempfile.TemporaryDirectory(prefix="hlens-positions-")
core/contracts/storage.py:228:class StorageAdapter(Protocol):
```

```text
rg -n 'CanonicalNormalizer\(' --glob '*.py'
```

This found direct construction in canonical tests, `infrastructure/tools/` and
`infrastructure/pit/selector.py`; supplying an explicit root cannot be completed inside the
canonical module alone.

Implementation / verification results on `codex/canonical-position-bounds`:

- `uv run pytest -q tests/infrastructure/test_settings.py tests/infrastructure/canonical/test_normalizer.py -k 'not recovery_follows_the_committed_plan and not an_empty_rest_page and not a_head_moved_mid_read and not a_unit_is_proven_and_written_in_bounded_windows and not windows_and_one_window_normalize_identically and not proof_windows_never_split_a_position_and_cover_all'`
  → `127 passed, 9 deselected in 36.03s`. The deselected node IDs failed on this task branch's `main@e584187` baseline: four `recovery_follows_the_committed_plan` cases reference undefined `n`; four tests call the canonical test helper without its required `clock`; and the proof-window test expects 2-tuples while the implementation yields `(low, high, count)`. The coordinating branch `codex/w1-stabilization` has repaired these test defects. They are branch-local baseline differences, not project-level release deferrals; the coordinating branch's current integration tests cover them. No production semantics were changed to make them pass.
- One-time exact-node retest of the initial 12 failures → `9 failed, 3 passed in 5.21s`. Passed after this task's test fixes: `test_canonical_scratch_uri_rejects_staging_overlap[parent]`, `test_inprocess_settings_suite_does_not_leak_hlens_env`, and `test_position_index_keeps_database_and_ordered_read_under_explicit_scratch`. The nine repeated baseline failures were `test_recovery_follows_the_committed_plan_whatever_the_configuration[None]`, `[1]`, `[2]`, `[3]`, `test_an_empty_rest_page_is_a_unit_without_rows`, `test_a_head_moved_mid_read_does_not_move_the_call`, `test_a_unit_is_proven_and_written_in_bounded_windows`, `test_windows_and_one_window_normalize_identically`, and `test_proof_windows_never_split_a_position_and_cover_all`. The exact command and full output are in the task runner transcript. No third attempt was made.
- `uv run pytest -q tests/infrastructure/test_settings.py -k 'canonical_scratch_uri or defaults_with_only_catalog_dsn or exact_uppercase_environment_overrides' tests/infrastructure/canonical/test_normalizer.py -k 'position_index_keeps_database_and_ordered_read_under_explicit_scratch or normalizer_refuses_unusable_scratch_before_any_catalog_access'`
  → `2 passed, 134 deselected in 0.03s` (pytest's final `-k` expression applies to the combined collection).
- `uv run ruff check infrastructure/settings.py infrastructure/canonical/normalizer.py infrastructure/pit/selector.py infrastructure/quality/reporter.py infrastructure/dataset/builder.py infrastructure/dataset/sources.py infrastructure/bars/dataset.py infrastructure/feature/dataset.py infrastructure/tools/capacity_probe.py infrastructure/tools/dnet_capability_run.py infrastructure/tools/normalizer_memory_probe.py`
  → `All checks passed!`
- `uv run mypy infrastructure/settings.py` → `Success: no issues found in 1 source file`.
- Broader mypy invocation over implementation entry modules still reports 23 issues in existing typing paths (including iterator `.close()`, generic `Sequence.__getitem__`, existing dataset / adapter signatures, and probe instrumentation); no error was reported in `infrastructure/settings.py` or the newly added scratch configuration property. This check is not a green project-wide type gate.
- Initial AST audit over `infrastructure/` and `tests/infrastructure/` → `AST callsite audit: all explicit scratch Path arguments present; syntax OK`.
- `git diff --check` → no output.

The root checkout's PostgreSQL full-suite process was not touched. No PostgreSQL or full suite was run.

## Independent-review follow-up

The independent reviewer found a missing scratch injection in the v2 manifest branch at
`research/loop/dataset_source.py::_dataset_observations`. It now passes
`catalog.builder.canonical_scratch_directory` to `PitSelector`. A regression test uses a v2
`ResearchDatasetManifest` and a selector spy to assert that exact Path is forwarded.

Follow-up checks:

- `uv run pytest -q tests/research/loop/test_dataset_source_v3.py::test_v2_manifest_reselection_passes_the_builder_scratch_path`
  → `1 passed in 0.87s`.
- `uv run ruff check research/loop/dataset_source.py tests/research/loop/test_dataset_source_v3.py`
  → `All checks passed!`.
- `rg -n 'PitSelector\(' --glob '*.py' --glob '!**/.venv/**' .` enumerated every repository call
  site; the new v2 call was the only missed production call.
- Whole-repository AST audit parsed 731 Python files and confirmed all
  `CanonicalNormalizer`, `PitSelector`, `QualityReporter`, `DatasetBuilder`, `PinnedQualityEvidence`
  and `dataset_evidence_sources` construction sites pass the required explicit scratch Path.
- An attempted broader `test_dataset_source_v3.py` run failed three tests before exercising this
  branch, in the existing dataset fixture with `CatalogIntegrityError: a mapped edge references an
  observation_key with no corresponding Canonical rows in this window`; no production data path was
  changed for that unrelated failure.

At the coordinator's request, the three exact nodes were retried once:

```text
uv run pytest -q \
  tests/research/loop/test_dataset_source_v3.py::test_v2_manifest_reselection_passes_the_builder_scratch_path \
  tests/research/loop/test_dataset_source_v3.py::test_a_v3_round_computes_the_v2_rounds_feature_values \
  tests/research/loop/test_dataset_source_v3.py::test_a_v3_round_reads_the_data_of_the_v2_round
```

```text
2 failed, 1 passed in 12.42s
```

The new v2 scratch-path regression passed again. The two existing v2/v3 end-to-end cases repeated the
same `CatalogIntegrityError` before reaching this v2 reselection path. Read-only comparison against
the coordinating `codex/w1-stabilization` worktree found its pending `infrastructure/pit/selector.py`
change advances the mapped-edge cursor only after matching the current observation key; this
addresses the cursor behavior behind the failure. That coordinating change was not copied into this
task's branch. This is a branch-local integration baseline issue, not a project-level release
deferral. No further retry was made.

The second independent review approved the change. The isolated branch is ready for coordination;
this change is not merged to `main`.

## Handoff

- Decision: Option A accepted by the PM; ADR-0075/0076/0077 and `StorageAdapter` remain unchanged.
- Phase / criterion: Phase 1 E1-CAP-1; source implementation / tests do not constitute capacity
  acceptance. The 32 MiB gate and complete workset accounting remain open.
- Reviewer / integration: second independent review approved; do not claim E1 passed. The isolated
  branch is not merged to `main`.
- Unresolved: measure the E1 memory matrix; report disk usage as O(N), with no quota claim.
