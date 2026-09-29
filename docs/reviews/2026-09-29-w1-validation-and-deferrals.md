# W1 validation and deferred failures — 2026-09-29

## Scope and branch state

- Validation ran on `codex/w1-stabilization` at base `e584187`; the working tree contains the current W1 edits.
- Local `main` is also at `e584187`, two commits ahead of `origin/main`. No changes were merged to `main` or pushed.
- Coordination commits on this branch: `c8bb210` (report DTO payload shapes) and `6173349` (bounded revision snapshot-history fallback). The latter's focused store tests passed (`41 passed in 6.84s`).
- Isolated agent branches remain unmerged: `codex/e1-history-bounds` (`27b8b54`), `codex/pit-edge-validation` (`9d27767`), `codex/report-dto-contract` (`65feeb7`), `codex/dataset-universe-bounds` (now `f4e8771`, includes Universe `7aa5457`), and `codex/canonical-position-bounds` (`506af66`, scratch decision record; implementation in progress). The Universe branch includes its PIT `RunSetBuilder` dependency. A read-only DTO branch audit found no patch unique to `65feeb7` beyond what `c8bb210` already covers; retain its worktree until final integration/cleanup accounting.
- Phase 1 remains open. E1-CAP-1 is still blocked pending full-process memory evidence at the existing 32 MiB limit.

## W1 full-suite run

Command:

```text
systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 uv run pytest -rs
```

Result: **15 failed, 8723 passed, 138 skipped, 5 warnings in 4839.81s**. The 138 skips are PostgreSQL integration cases because `HLENS_TEST_CATALOG_URI` was not set.

The 15 first-run failures were followed by one targeted `pytest --lf -q --tb=short` run: **14 failed, 1 passed, 3309 deselected**. The evidence-setups failure passed on that targeted run. Per the project manager's two-round rule, the remaining 14 are deferred; no third debugging round is scheduled in this W1 batch.

### Deferred after two failing rounds

- Report hash snapshots: `tests/research/strategies/test_market_benchmark.py::test_without_the_opt_in_every_report_is_byte_identical`; `tests/research/strategies/test_multi_instrument_validation.py::test_the_single_instrument_path_is_byte_identical`.
- Calibration and validation hashes: `tests/research/synthetic_lab/test_gate_calibration_g5.py::test_g5_mode_off_keeps_every_pre_g5_report_hash`; `tests/research/synthetic_lab/test_gate_calibration_multi.py::test_mode_off_keeps_every_single_instrument_report_hash`; `tests/research/synthetic_lab/test_gate_calibration_multi.py::test_mode_on_is_deterministic_and_pinned`; `tests/research/validation/test_robustness.py::test_no_or_zero_remainders_leave_the_capacity_check_unchanged`.
- Adapter and schema compatibility snapshots: `tests/test_adapter_contracts.py::test_pre_b3_current_schemas_are_byte_identical` for `EventSpec`, `ResearchDatasetManifest`, `StrategySpec`, and `UniverseMember`; `tests/test_adr_0077_evidence_manifest.py::test_the_v2_schemas_change_only_their_envelope_default` for `ResearchDatasetManifest` and `UniverseMember`; `tests/test_revision_contracts.py::test_reexport_is_byte_identical_for_every_current_schema` for `SyntheticMarket`.
- ADR-0088 legacy shape: `tests/test_adr_0088_contract_240.py::test_an_old_effect_without_a_kind_still_reads_as_return_autocorrelation` (the fixture has no `effects` key).

These are recorded as deferred, not marked as passed or excluded from future release gates. Reopen them in a later focused compatibility batch with an explicit owner and changed contract/hash evidence.

### Additional deferral after enabling the local PostgreSQL test catalog

The later full-suite run with the existing dedicated PostgreSQL test catalog found
`tests/apps/test_reports.py::test_an_ill_formed_or_tampered_loop_round_is_refused[not-a-round]`
failing once. A single focused rerun of `tests/apps/test_reports.py` reproduced the same failure
(`1 failed, 39 passed`): the payload is rejected for missing `round_index`, while this assertion
expects the error text to contain `LoopRoundRecord`. Per the same two-round rule, this one case is
deferred from the current batch. It is unpassed and remains a release gate; do not rerun it in this
batch. This does not change the original 14-case deferral list above.

### PostgreSQL-enabled full-suite run and focused retries

The PostgreSQL-enabled run executed 8,822 items and deselected another 72; it used the existing dedicated test catalog, with the original
14 deferred cases deselected. It completed with **7 failed, 8,814 passed, 1 skipped, 72 deselected,
5 warnings in 5,256.10s (1:27:36)**. The available run summary does not preserve all seven node IDs;
this record only names failures confirmed by the focused run below and the separately deferred
loop-round case above. The 72 deselections include parameterized cases belonging to deferred test
families and are not passing results.

After the full run exited, one focused retry of
`tests/infrastructure/catalog/test_phase1_tables_postgres.py` and
`tests/infrastructure/revision/test_rest_store_postgres.py` returned **3 failed, 40 passed in
14.96s**. The previously failing exact cases reproduced and are now deferred under the two-round
rule:

- `tests/infrastructure/catalog/test_phase1_tables_postgres.py::test_fifteen_tables_are_created_idempotently_with_the_frozen_layout`
- `tests/infrastructure/revision/test_rest_store_postgres.py::test_all_fifteen_phase1_tables_exist_with_their_bindings`

The same focused retry also exposed
`tests/infrastructure/catalog/test_phase1_tables_postgres.py::test_catalog_database_holds_only_iceberg_metadata`.
The broad-run summary does not preserve the seven failing node IDs, so its result there and its
failure-round count cannot be confirmed. A later exact-node attempt in the current shell environment
was skipped because `HLENS_TEST_CATALOG_URI` was not set; a skip is neither a pass nor a failure.
Do not rerun the two already deferred nodes or either entire module. Revisit this node only when the
dedicated PostgreSQL test catalog is available and the prior run's failure-node evidence has been
reconciled.

The post-fix console fixture and synthetic evidence regression completed separately with
**52 passed, 1 warning in 24.32s**. PostgreSQL failures and all earlier deferred cases remain
unpassed release gates; continue W2/W3/W5 work that does not depend on them.

## Follow-up checks on the current working tree

- API report DTO and identity checks: `90 passed, 1 warning`.
- Web library tests: `111 passed, 0 failed`.
- `tests/test_docs_consistency.py`: `7 passed` after updating the W1 deferral ledger and current workstream status.
- `ruff check .`: passed.
- `ruff format --check .`: `947 files already formatted`.
- strict mypy: `Success: no issues found in 731 source files`.

The report DTO follow-up restored required fields for every report kind and aligned StateDiagnostics with the writer payload (`schema_version`, `kind`, `state_space`); no `diagnostics_hash` payload field is assumed. Three virtual-version kinds reject an in-payload `schema_version` consistently in API and Web.

A separate W1 change audit found that the fixture refresh had removed the two original `paper_deviation` 1.0.0 records and the original `retro_audit` 1.0.0 record. The files were restored; the fixture registry now pins the original IDs alongside additive current outputs, and all five paper-deviation reports are covered. Focused API / console fixture tests then passed (`127 passed, 1 warning`). The audit also found that `TrialLedger.register()` checked its duplicate fast path before enforcing the LLM-reviewed-draft rule. That rule now runs before the fast path; regression coverage in `test_durable_ledger.py` passed (`10 passed`). These fixes do not change the 14 deferred W1 outcomes.

## Parallel acceptance review: W3-E1DS

The read-only review found the ADR-0077 Dataset flow present but identified remaining unbounded holds in PIT window ingestion/run references, Universe change instants, per-key history grouping, and per-report quality gaps. These can proceed within infrastructure where the accepted ADR permits. DQ-9 values (`chunk_rows`, `leaf_max_records`, `leaf_max_bytes`, `fanout`) remain pending capacity evidence. The review ran no tests or probes and does not change E1-CAP-1 status.

## Stream-task acceptance findings

- `codex/pit-edge-validation@9d27767`: `test_selector_v3.py` + `test_bounded_runs.py` passed (`41 passed`), and the two Raw-scan proxy regressions passed separately (`2 passed`). The bounded path now yields evaluation results one at a time and compacts per-key run refs. Single-key history is still rematerialized through `key_rows` / `RevisionRecord` tuple / edge and availability mappings, then `effective_available_times → _evaluate → _heads → RevisionGraph/maximal_heads`; this is an ADR-0077 acceptance blocker. No capacity claim.
- `codex/e1-history-bounds@27b8b54`: after parser `8f9265a` and D2 writer `e0aab8d`, the verifier follow-up now reparses archives serially, closes each spool, and returns metadata-only lineage while preserving the prior rejection order. Parser + row-integrity + store regression run: `177 passed in 9.82s`; Ruff `check --ignore E501` and `git diff --check` passed. Full process RSS and spool/tmpfs bytes remain unmeasured; `ruff format --check` still reports formatter drift in `row_integrity.py`, with unrelated lines left untouched.
- `codex/e1-history-bounds@e0aab8d`: D2 `RawRevisionStore.ingest()` now consumes spooled batches and preserves archive-first / microbatch write order; `test_store_unit.py` reported `49 passed`. `ingest_parsed()` keeps its `ParsedArchive` API. The D2 connection removes the complete Arrow Table from this writer path but does not bound spool bytes or multi-archive verifier retention, so this is not E1-CAP-1 evidence.
- `codex/dataset-universe-bounds@bf72bd5` adds follow-ups to `2a112fa` and its PIT `RunSetBuilder` integration: the four Universe sorts now fold into one run root, and exact first-seen lineage dedupe uses a SQLite scratch store. The four-file run passed (`51 passed in 57.82s`); a separate canonical same-revision gap invariant test passed (`1 passed in 1.38s`), with Ruff checks passing. Run-ref and in-memory per-symbol growth findings are closed for this slice; SQLite scratch bytes still grow with unique revision count and may be on tmpfs. This is not E1-CAP-1 capacity evidence.
- `codex/dataset-universe-bounds@7aa5457` filters pre-window Universe events before they enter the bounded sorter; the targeted v3 module passed (`18 passed`) and an independent read-only review approved the boundary, cutoff and cleanup behavior. This does not bound source scan time or full process memory.
- `codex/dataset-universe-bounds@f4e8771` stores Dataset member spans behind `RunSetBuilder` root refs instead of a per-symbol list; focused Dataset v3 tests passed (`47 passed in 32.08s`), Ruff / format / diff-check passed, and independent review found no P1/P2. Scoped mypy still reports one existing Literal-key error at `builder.py:2040`; the change adds no new mypy error. A non-blocking coverage gap is that the member-span regression does not directly assert root depth and early-close cleanup on that exact path.
- `codex/canonical-position-bounds@991b126` implements PM decision D-E1-CANONICAL-SCRATCH: `Settings.canonical_scratch_uri` defaults to repository-owned `data/scratch`, is overridable with `HLENS_CANONICAL_SCRATCH_URI`, and is injected through normalizer / PIT / Dataset / Quality / tools without `tempfile` / `TMPDIR` fallback. An independent review first found the research-loop v2 manifest caller missing the path (P2); the fix and selector-spy regression test were added, and a second review approved the full diff. Focused run: `127 passed, 9 deselected`; the nine branch-local `main@e584187` test defects were retried once (`9 failed, 3 passed`) and are not project-level release deferrals because the coordinating branch contains their test fixes. New v2 scratch-call test: `1 passed`; production Ruff, targeted Ruff, `mypy infrastructure/settings.py`, 731-file AST callsite audit, and `git diff --check` passed. Broad mypy still reports 23 pre-existing issues. Disk scratch remains O(N); no E1 capacity claim.

## PIT bounded-run integration candidate — 2026-09-29

The five PIT commits from `codex/pit-edge-validation` were cherry-picked in dependency order onto `codex/w1-independent-integration` as `0476eaa`, `0b8e3c3`, `74e1ae7`, `62b8982`, and `97f741b`. This imports the shared `RunSetBuilder` API needed by subsequent Universe/Dataset work without pulling unrelated branch changes. The focused integration-candidate run of `tests/infrastructure/pit/test_bounded_runs.py`, `tests/infrastructure/pit/test_selector.py`, and `tests/infrastructure/pit/test_selector_v3.py` passed: **84 passed in 52.78s**.

An independent readiness audit found no frozen/domain contract changes and approved integration as an incremental code slice. The half-open availability regression had separately passed on source-branch HEAD (`1 passed in 1.92s`); the full PIT suite was not rerun there. Residual memory remains O(N): per-key `RevisionRecord` and mapped-edge tuples plus `RevisionGraph` / `maximal_heads` structures; conflict output and run-ref levels also remain potentially O(N) / depth-dependent. The audit notes possible repeated-search cost. Thus this does not establish bounded single-key history, ADR-0077 acceptance, or E1-CAP-1's 32 MiB complete-process limit. Next, port only the Universe/Dataset commits after their shared PIT foundation; do not replay duplicate commit `0270e69` or blindly apply their selector edits.

The dependent Universe/Dataset commits were then ported in order: `2a112fa`, `63fbcdc`, `bf72bd5`, `7aa5457`, and `f4e8771`, recorded on the candidate as `52124e9`, `3778a8d`, `a60ef87`, `2afc033`, and `652de29`. The duplicate PIT foundation `0270e69` and already-present W5 seed guard commits were not replayed. On the integration candidate, Universe v3, Dataset v3 builder, and Dataset v3 source tests passed together: **65 passed in 44.71s**. Ruff check passed, Ruff format reported `6 files already formatted`, and `git diff --check` passed.

The source-branch reviews found no P1/P2 issue for Universe and Dataset. These results are focused integration evidence, not full E1 capacity evidence. Dataset SQLite scratch bytes still grow with unique revision count and may use tmpfs; PIT retains O(N) single-key graph/history state. Dataset member-span root depth and early-close cleanup are not directly asserted by its regression test. Scoped mypy retains the pre-existing Literal-key error at `infrastructure/dataset/builder.py:2040`. E1-CAP-1 remains open.
