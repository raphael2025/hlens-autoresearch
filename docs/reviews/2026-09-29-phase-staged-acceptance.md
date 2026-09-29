# 2026-09-29 staged phase acceptance wave

## Scope and checkout identity

Three independent acceptance assignments covered Phase 1 Quality stream design, Phase 0.5/2–9, and Phase 10–14. The root checkout used by the phase reviewers was `codex/w1-stabilization@a0b88066bcc858584eb251d8f8a3c1bd66431a6c`; it had 145 dirty entries and reviewers made no changes. No full-repository suite or E1-CAP-1 capacity probe was run in this wave.

The separate Quality stream candidate was `codex/e1-quality-stream-v3@4e2db1644847cf6277bdb2c42bbe3e56d89c0301`, clean at review. Commit `22bd73d` adds the generic stream substrate; `4e2db16` fixes bounded record serialization. The code is not integrated into the root checkout.

## Phase 1 / Quality stream

Independent review returned **APPROVE for the generic stream substrate only**. The original unbounded serialization issue was fixed by capped incremental encoding. An 8 MiB scalar and an infinite lazy mapping are rejected within the configured record-byte budget without publishing a root; a failed writer clears its buffers and refuses `finish()`.

```text
uv run pytest -q tests/infrastructure/quality/test_report_streams.py
8 passed in 0.18s

uv run ruff check infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
All checks passed!

uv run ruff format --check infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
2 files already formatted

uv run mypy infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
Success: no issues found in 2 source files

git diff --check 22bd73d8fd65a2dbed27dce5f7586edca00bc7c9...HEAD
exit 0, no output
```

This does not approve the manifest schema, reporter integration, Dataset bindings, ADR-0077 §6.1.5, Phase 1, or E1-CAP-1.

ADR-0093 amendment remains **BLOCKED / under revision**. The independent architecture review still requires: exact JSON escaping and event-ID domain input; a bounded ADR-0031 QGAP completeness seal; a byte-bounded Iceberg path for row groups and nested Raw `symbols` / listing `tradable_intervals`; and a precise ordering/join rule for listing revision-prefix verification. Do not implement report schema/reporter/Dataset wiring against the current draft.

### W3 Universe streaming port

The previously approved W3 Universe streaming implementation was ported to the current coordination base in isolated commit `faf630cb9f19d5214e5a132136f4e7c9f1230975` (`codex/e1-w3-universe-integration`), parent `7b4cc9c`. The 10-file change adds the shared Universe run-parameter type, bounded `RunSetBuilder` refs, Universe streaming, Dataset cursor wiring, and affected tests. It does not change contracts or touch the root checkout. Independent review **APPROVED this port only**; this is not E1-CAP-1 or Phase 1 acceptance.

```text
uv run pytest -q tests/infrastructure/pit/test_bounded_runs.py tests/infrastructure/universe/test_universe_builder_v3.py tests/infrastructure/universe/test_universe_listing_assumption.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/dataset/test_dataset_v3_builder.py tests/infrastructure/redteam/test_rt_listing_assumption_universe.py -k 'not test_v3_build_over_the_real_upstreams_selects_what_v2_selects and not test_owner_and_event_times_are_the_canonical_rows_times and not test_pit_keys_out_of_order_fail_the_real_build_closed'
87 passed, 4 deselected in 37.40s

uv run ruff check infrastructure/dataset/sources.py infrastructure/pit/runs.py infrastructure/universe/builder.py infrastructure/universe/run_params.py
All checks passed!

uv run ruff format --check infrastructure/dataset/sources.py infrastructure/pit/runs.py infrastructure/universe/builder.py infrastructure/universe/run_params.py
4 files already formatted

uv run mypy --follow-imports=silent infrastructure/dataset/sources.py infrastructure/pit/runs.py infrastructure/universe/builder.py infrastructure/universe/run_params.py
Success: no issues found in 4 source files

git diff --check 7b4cc9ce79583050eebd55ab9320be003b059900...HEAD
exit 0, no output
```

The four deselected Dataset source failures were reproduced on clean base `7b4cc9c` in the unchanged PIT selector (`orphan mapped edge`); the failed nodes were not rerun after the baseline reproduction. They remain deferred under the two-round rule. Default transitive mypy reports 19 errors in 8 unmodified dependency files; only the four touched production files passed with `--follow-imports=silent`. Ruff, format, and diff checks passed.

### PIT row/edge run-reference port

The same isolated branch then ported the approved E1-PIT-RUNSET-REFS slice as commit `b849aac1a0c54ac94b17707a356d4bff6ccd75db` (parent `faf630c`). Row and edge spill references now compact into bounded RunSet roots. Consumption keeps at most two root readers open and closes them on normal exit, early exit, and failure. The two-file diff is limited to `infrastructure/pit/selector.py` and `tests/infrastructure/pit/test_selector_v3.py`; it does not change `_heads`, `maximal_heads`, or conflict output.

Focused PIT selector + Dataset source run: `36 passed, 5 deselected in 23.75s`.
Independent reviewer command and result:

```text
systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 uv run pytest -q tests/infrastructure/pit/test_selector_v3.py -k 'empty_row_and_edge_roots or run_set_roots_compact or run_set_failure_releases_builders or closes_its_generator_on_an_early_context_exit'
5 passed, 15 deselected in 5.85s

Ruff check: All checks passed!
Ruff format: 2 files already formatted
mypy --follow-imports=silent infrastructure/pit/selector.py: Success: no issues found in 1 source file
git diff --check: exit 0, no output
```

Independent review **APPROVED the row/edge run-ref slice only**. Five cases remain deferred: four known Dataset source/orphan-edge nodes and one multi-key history case whose legacy v2 lineage-count assertion was `5` expected / `4` actual after its second failed round. Default mypy reports 14 errors in unmodified dependency files. Per-window materialization, single-key graph/history, and conflict-head state remain open; this port does not pass E1-CAP-1 or Phase 1.

## Phase 0.5 and Phase 2–9

All current-checkout reruns below are focused evidence, not phase acceptance:

| Phase | Command/result | Acceptance state and remaining gates |
|---|---|---|
| 0.5 | `tests/contract_suites/knowledge.py tests/plugins/knowledge tests/research/hypotheses/test_knowledge_source.py tests/research/loop/test_loop_knowledge_source.py`: `80 passed in 8.65s` | **NOT ACCEPTED** — named human review of seed tags/assets and golden hashes for added seeds remain open. |
| 2 | `tests/contract_suites/state.py tests/test_state_contract_suite.py tests/plugins/states tests/research/states tests/infrastructure/state`: `139 passed in 1.34s` | **NOT ACCEPTED** — real Research Dataset validation, CLI/catalog, type checks, and phase acceptance remain open. |
| 3 | Historical `c22d903` evidence: `268 passed in 7.24s` | **NOT ACCEPTED** — historical checkout only; deferred Iceberg event test and production catalog creation remain open. |
| 4 | Historical `c22d903` evidence: `375 passed in 7.34s` | **NOT ACCEPTED** — historical checkout only; Profile numeric/calibration/freeze gates remain open. |
| 5 | Historical run: `215 passed in 237.32s`, then interrupted in `test_default_model_has_no_remainders_and_is_unchanged` | **NOT ACCEPTED** — incomplete run; two deferred hash nodes remain release gates. |
| 6 | Historical `c22d903` evidence: `11 passed in 0.20s` | **NOT ACCEPTED** — historical checkout; no real integrated P2/P5/P4 experiment. |
| 7 | Current focused typed-plan/durable set: `82 passed in 104.00s` | **NOT ACCEPTED** — six operators remain fail-closed/non-runnable; output completeness and a frozen Profile are unproven. |
| 8 | `tests/research/validation/test_g4_check_isolation.py tests/research/validation/test_g4_review_fixes.py tests/research/validation/test_cross_asset_cross_sectional.py tests/research/validation/test_impact_exact_comparison.py tests/research/validation/test_retro_audit.py`: `67 passed in 3.88s` | **NOT ACCEPTED** — report fixtures/component/live-smoke registry coverage is incomplete. |
| 9 | `tests/plugins/synthetic/test_random_walk.py tests/research/synthetic_lab/test_calibration.py tests/research/synthetic_lab/test_gate_calibration.py tests/research/synthetic_lab/test_evidence_setups.py`: `153 passed, 1 warning in 50.37s` | **NOT ACCEPTED** — adequate calibration evidence and Profile values remain open; deferred G5/multi-calibration tests were not run. |

## Phase 10–14

Focused checks passed, but **all phases remain NOT ACCEPTED**:

| Phase | Result | Remaining gates |
|---|---|---|
| 10 | `47 passed, 7 deselected, 1 warning in 0.62s` | Full router acceptance and deferred loop-round report nodes. |
| 11 | `99 passed, 1 warning in 101.15s` | Authoritative source resolver/metric provenance and a usable frozen Profile. |
| 12 | `35 passed in 0.30s` | Phase acceptance and human approval path for replacement proposals. |
| 13 | `111 passed in 3.55s` | Synthetic/simulated rejection only; no live venue or authorization, as required by current scope. |
| 14 | `10 passed in 22.32s` | No migration target or complete migration matrix. |

Web checks: `npm test` exited 1 (`110 passed, 1 failed`); `retroAudit.test.ts` expected one fixture but found two. Not rerun. `npm run build` exited 0. `npm audit` was not run; previously recorded dependency vulnerabilities remain unresolved.

## Overall acceptance

No phase was accepted by this wave. Deferred two-round failures remain failed release gates and were not retried. Phase 1 remains blocked by unresolved PIT graph/conflict-head and bounded Quality integration work plus the unrun 32 MiB E1-CAP-1 measurement. Profile values remain unfrozen. These results must not be presented as a percentage-based acceptance claim.

## Current-checkout acceptance refresh (2026-09-29)

This independent refresh used the root checkout `codex/w1-stabilization@8180b073faeb80e5865a41d97b336ce70a536eb6`, which had 145 dirty entries. Reviewers made no changes. It did not run a full-repository suite or the E1-CAP-1 capacity probe, and it did not retry any node already deferred after two failures.

### Phase 7–14 focused verification

The following current-checkout focused groups ran successfully. These results do not close their phases:

| Coverage | Result |
|---|---|
| P7 durable ledger, plan binding/lowering, retry admission | `81 passed in 85.57s` |
| P11 loop, durable recovery, API and cross-process worker | `170 passed, 1 warning in 6.74s` |
| P10 router/paper deviation/API reports, excluding the deferred malformed loop-round cases | `120 passed, 7 deselected, 1 warning in 2.58s` |
| P12 evolution/promotion and P13 execution/risk/audit | `250 passed in 10.72s` |
| P8 G4, retro-audit, cross-asset/impact and API/report, excluding the deferred malformed loop-round cases | `166 passed, 7 deselected, 1 warning in 4.48s` |
| P14 golden and versioned migration replay | `10 passed in 18.77s` |

Selected Ruff checks reported `All checks passed!`; `git diff --check` exited 0 with no output. P7–P14 remain **NOT ACCEPTED**: Profile, source-authority, complete validation/smoke, failure-drill, migration/rollback, and deployment gates remain open as applicable. In particular, P11 still lacks an authoritative ACTIVE head/source/metric definition; P13 remains simulation-only with LIVE disabled; P14 lacks the target deployment migration matrix and rollback evidence. The exact reviewer command log is preserved in the task record; no run above is a full phase acceptance.

### Phase 1 acceptance matrix refresh

The independent read-only matrix review confirmed the current recorded status: #1–#12 and #17 have historical batch-level pass evidence; #13 and #14 have their D3E/D4 gate decisions recorded. #15, #18, #19 and #20 remain partial against the current dirty checkout; #16 and #21 remain not accepted. `PROJECT_STATUS.md` §10's parenthetical identifies a completed subset and does not relax the requirement that all #1–#21 pass. The full Phase 1 remains **NOT ACCEPTED**.

E1-CAP-1 is **NOT ACCEPTED**: no current-line complete-process measurement proves the 32 MiB limit across the required N/M matrix and repeated runs. Existing local stream tests and small synthetic smoke results are insufficient; old over-limit measurements on another candidate cannot be projected onto this checkout. The PIT window/per-key state, bounded Quality reporter/Dataset integration, and complete current-checkout evidence remain outstanding.

### D1 Parser source audit

A focused read-only lifecycle audit found that the current default `parse_archive()` still retains every 65,536-row Arrow `RecordBatch` in `_ColumnBuffer._batches` and then returns a complete `pa.Table` through `ParsedArchive.rows`; D2 writes the full table in microbatches, and the verifier may cache a full `ParsedArchive`. `parse_archive_bytes()` additionally receives the complete compressed input bytes from its caller. Existing archive/member/line size limits are integrity bounds, not a 32 MiB process working-set bound.

The clean `codex/e1-history-integration@b40601a` candidate contains a private IPC batch-spool path and D2/verifier consumption, but still uses default `TemporaryFile` locations, retains a full `row_commits` tuple, and has not proved whether scratch is backed by tmpfs or disk. It is not current-root evidence and does not pass E1-CAP-1. Any follow-up must preserve the public complete-result API and D1 length/SHA, ZIP EOF/CRC, row rejection/line-number, timestamp-unit/coverage, and cross-row checks; D2 must not consume partial output before full archive validation. A private bounded reader/spool path may be implemented under existing infrastructure scope, but scratch ownership/cleanup, caller-held verifier rows, commits, and actual full-process memory remain separate bounds to close. No parser tests or capacity probe were run in this audit.

### PIT window-closure staging candidate

The isolated candidate `codex/e1-cap-pit-window-stream@78120b3149faf666c372f6ee08a5552a604f026f` (parent `1715d39`; original base `3a7ff8c`) stages wanted keys, closure rows/chains, event days, and final row/edge outputs in external RunSets. It processes canonical proof and edge mapping one observation key at a time; a follow-up change removed completed-chain callbacks from an `ExitStack` so disjoint chain count does not grow retained contexts. The first independent review found missing legacy time-window validation; commit `78120b3` restores the same UTC, non-empty, forward-window checks without materializing a date list. The corrected candidate is not integrated into the root checkout and is awaiting incremental independent review.

Developer verification on the candidate:

```text
uv run pytest -q tests/infrastructure/pit/test_selector_v3.py -k 'not test_canonical_key_closure_is_spilled_and_matches_v2_rows and not test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
27 passed, 2 deselected in 28.14s

Invalid-window parity and no-day-list checks:
4 passed in 2.96s

3-day spanning-edge parity node with the same two explicit exclusions:
1 passed in 10.45s

32-disjoint-chain writer-bound node:
1 passed in 2.05s

Ruff check / format: passed
mypy --follow-imports=silent infrastructure/pit/selector.py:
Success: no issues found in 1 source file
git diff --check: exit 0, no output
```

The two excluded nodes remain deferred: the newly introduced closure comparison node failed twice and was not rerun; the pre-existing multi-key lineage node (`4` actual vs `5` expected) was accidentally included once in a combined run (`1 failed, 4 passed in 9.46s`) and was then excluded from all later commands. It must not be counted as passing. Full test-file mypy reported 20 test-side/missing-stub errors; production selector mypy passed.

This slice does not close single-key history/graph/maximal-head materialization, `ChannelReconciler._plan`'s full-day tuple, Arrow batch or Iceberg row-group/stripe byte bounds, or complete-process memory. It is not E1-CAP-1 or Phase 1 acceptance evidence.

### Phase 0.5–6 status review

A separate static review found no newly accepted phase. Phase 0.5 remains partial pending named human seed-tag/asset review and missing seed golden hashes. Phase 2 lacks real Research Dataset validation and CLI/catalog/type evidence; Phase 3 lacks the deferred Iceberg Event and production Catalog evidence; Phase 4 still lacks calibrated, frozen Profile evidence; Phase 5's long run remains incomplete with deferred hash gates; Phase 6 lacks a real integrated P2 × P5 × P4 experiment and its C-R2/trial-count evidence. Historical focused test counts do not close these phases.

### Current dirty-tree focused reruns (2026-09-29)

Independent acceptance roles reran focused suites against `codex/w1-stabilization@7a4f67d6a1c37c31e8416ba3070f4fbddce98129`; the checkout had 145 pre-existing dirty paths before and after testing. No files were changed by the reviewers. These results are current regression evidence for that dirty tree, not a clean commit or Phase acceptance. `HLENS_TEST_CATALOG_URI` was unset, so PostgreSQL-backed paths were not run.

| Phase | Focused result | Remaining acceptance gate |
|---|---|---|
| 0.5 | `80 passed` | Named human review of seed tags/assets and complete golden hashes remain open. |
| 2 | `139 passed` | Real Research Dataset validation, CLI/catalog, typing, and phase evidence remain open. |
| 3 | `233 passed`; `tests/infrastructure/event/test_event_iceberg.py` excluded | Iceberg Event and production Catalog evidence remain open. |
| 4 | `327 passed, 1 deselected` | Calibrated and frozen Validation Profile remains open. |
| 5 | `432 passed, 2 deselected` | Both report-hash nodes remain deferred / NOT PASSED; no frozen Profile or promotion evidence. |
| 6 | `29 passed` | No real P2 × P5 × P4 experiment, trial-count evidence, or C-R2 result. |
| 7 | `123 passed` | Six operators remain fail-closed; end-to-end producer/admission binding and usable frozen Profile remain open. The complete-output API itself has focused test evidence in this result. |
| 8 | Python `100 passed, 7 deselected`; Web components `120 passed`; Web library `110 passed, 1 failed` | `retroAudit.test.ts` fixture-count assertion failed for the second round and is deferred / NOT PASSED; report fixtures, smoke registration, and full validation remain open. |
| 9 | `191 passed, 3 deselected` | Calibration evidence and Profile values remain open; deselected calibration/hash nodes are not passed. |
| 10 | `106 passed, 7 deselected` | Full Router acceptance remains open; malformed loop-round cases remain deferred / NOT PASSED. |
| 11 | `195 passed` | Authoritative ACTIVE/source/metric definitions, frozen Profile, and PostgreSQL recovery remain open. |
| 12 | `256 passed` | Human replacement-proposal workflow remains open. Automatic G5 descendant evaluation is intentionally deferred by the accepted project decision and is not a current implementation gate. |
| 13 | `81 passed` | Kill Switch drill and second-line risk rejection audit/replay remain open. No live venue or credential path was exercised. |
| 14 | `36 passed` | No concrete migration target, complete matrix, rollback, or deployment evidence. |

The reviewers excluded all previously deferred nodes. Deselects are not passing results. Exact command transcripts for P0.5–P14 are preserved below. They were recovered from the original execution record; no tests were rerun for this documentation update. The new Web library failure is added to the W1 deferral ledger. No phase changed to ACCEPT.

### Independent report consistency review

A read-only Phase 7–14 review confirmed every phase remains **INCOMPLETE** and found no basis to change a phase to ACCEPT. It identified that older `PROJECT_STATUS.md` implementation-batch notes said tests had not run; those notes are now labeled as historical and the current focused results are shown alongside them. P8's current component run included the retro-audit page (`120 passed`); fixture-generator and live-smoke registration remain open, while the Web library fixture-count node is deferred / NOT PASSED. P7's open binding item is the end-to-end producer/admission relationship, not the complete-output API in isolation. P12's G5 descendant loop evaluation is intentionally deferred by decision; the human replacement-proposal workflow remains an acceptance concern.

### Exact current-snapshot command record — Phase 0.5–6

Commands below are copied from the original reviewer execution record for `codex/w1-stabilization@7a4f67d6a1c37c31e8416ba3070f4fbddce98129`; each exited `0`. This snapshot had 145 dirty paths before and after the test run. The recorded passes and deselections do not accept any phase.

**Phase 0.5**

```bash
uv run pytest -q tests/contract_suites/knowledge.py tests/plugins/knowledge tests/research/hypotheses/test_knowledge_source.py tests/research/loop/test_loop_knowledge_source.py
```

Result: `80 passed in 7.99s`.

**Phase 2**

```bash
uv run pytest -q tests/contract_suites/state.py tests/test_state_contract_suite.py tests/plugins/states tests/research/states tests/infrastructure/state
```

Result: `139 passed in 1.23s`.

**Phase 3**

```bash
uv run pytest -q tests/plugins/events tests/infrastructure/event --ignore=tests/infrastructure/event/test_event_iceberg.py
```

Result: `233 passed in 1.68s`. `test_event_iceberg.py` was excluded; this is not evidence that it passed.

**Phase 4**

```bash
uv run pytest -q tests/plugins/outcomes tests/research/outcomes tests/research/validation --deselect=tests/research/validation/test_robustness.py::test_no_or_zero_remainders_leave_the_capacity_check_unchanged
```

Result: `327 passed, 1 deselected in 15.31s`. The deselected node remains deferred / NOT PASSED.

**Phase 5**

```bash
uv run pytest -q tests/research/strategies tests/promotion --deselect=tests/research/strategies/test_market_benchmark.py::test_without_the_opt_in_every_report_is_byte_identical --deselect=tests/research/strategies/test_multi_instrument_validation.py::test_the_single_instrument_path_is_byte_identical
```

Result: `432 passed, 2 deselected in 456.69s (0:07:36)`. Both hash nodes remain deferred / NOT PASSED.

**Phase 6**

```bash
uv run pytest -q tests/research/experiments/test_matrix_conditionals.py tests/research/experiments/test_trial_conditionals.py tests/research/experiments/test_state_strategy.py
```

Result: `29 passed in 0.17s`.

### Exact current-snapshot command record — Phase 7–14

The commands below are copied from the reviewer record for `codex/w1-stabilization@7a4f67d6a1c37c31e8416ba3070f4fbddce98129`. They were not rerun to create this record. Asynchronous session captures preserved pytest summaries but not the wrapper exit code; unavailable exit codes are not inferred. The P8 Web library command returned exit 1; P8 components and P13 returned exit 0.

**P7**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short tests/research/hypotheses/test_batch.py tests/research/hypotheses/test_durable_ledger.py tests/research/hypotheses/test_plan_bindings.py tests/research/hypotheses/test_typed_plan_lowering.py tests/research/hypotheses/test_strict_llm_drafts.py tests/research/loop/test_loop_llm_rejection.py tests/research/loop/test_loop_retry_admission.py tests/research/loop/test_retry_admission.py
```

**P8 Python**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short -k 'not ill_formed_or_tampered_loop_round' tests/research/validation/test_g4_check_isolation.py tests/research/validation/test_g4_review_fixes.py tests/research/validation/test_cross_asset_cross_sectional.py tests/research/validation/test_impact_exact_comparison.py tests/research/validation/test_retro_audit.py tests/apps/test_reports.py
```

**P8 Web library and components**

```bash
npm run test:lib
npm run test:components
```

**P9**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short -k 'not test_g5_mode_off_keeps_every_pre_g5_report_hash and not test_mode_off_keeps_every_single_instrument_report_hash and not test_mode_on_is_deterministic_and_pinned' tests/plugins/synthetic/test_random_walk.py tests/research/synthetic_lab/test_calibration.py tests/research/synthetic_lab/test_gate_calibration.py tests/research/synthetic_lab/test_gate_calibration_g5.py tests/research/synthetic_lab/test_gate_calibration_multi.py tests/research/synthetic_lab/test_evidence_setups.py
```

**P10**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short -k 'not ill_formed_or_tampered_loop_round' tests/research/router/test_router.py tests/research/router/test_router_completion.py tests/research/router/test_router_eligibility.py tests/research/router/test_router_validation.py tests/research/reports/test_deviation_writer.py tests/apps/test_reports.py
```

**P11**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short tests/research/loop/test_loop_durable.py tests/research/loop/test_loop_cross_process.py tests/research/loop/test_loop_e2e.py tests/apps/test_research_loop.py tests/apps/test_research_loop_durable.py tests/apps/test_research_loop_retry.py tests/apps/test_worker_jobs.py tests/apps/test_worker_jobs_cross_process.py
```

**P12**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short tests/research/evolution tests/promotion
```

**P13**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short tests/apps/test_execution.py tests/apps/test_execution_durable_audit.py tests/apps/test_execution_live_venue.py tests/apps/test_execution_marks_required.py tests/apps/test_execution_risk_replay.py tests/apps/test_execution_strategy_source.py
```

**P14**

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline --no-sync pytest -q --tb=short tests/infrastructure/migration/test_golden_experiments.py tests/infrastructure/migration/test_golden_persistence.py tests/infrastructure/migration/test_migration.py
```

Captured results: P7 `123 passed in 107.06s`; P8 Python `100 passed, 7 deselected, 1 warning in 4.11s`; P8 Web library `110 passed, 1 failed` (second failure of the same fixture-count assertion; deferred / NOT PASSED); P8 components `120 passed`; P9 `191 passed, 3 deselected, 1 warning in 95.03s`; P10 `106 passed, 7 deselected, 1 warning in 2.83s`; P11 `195 passed in 286.13s`; P12 `256 passed in 7.99s`; P13 `81 passed in 0.35s`; P14 `36 passed in 6.15s`. Deselects are not passes; deferred nodes were not rerun.

### E1 input-stream candidate review — 2026-09-29

Independent review of `9178f5d8ecd31b0cf64a6a508bfc047becc405ae` used a detached, read-only worktree. The change from `feb4068` removes the per-key `equal` mapping and `current_edges = list(...)`; equal-pair IDs spill through a sorted RunSet and committed evidence rows are merged one at a time. Static review found checks for duplicate committed edges, unmatched extra edges, source revision binding, and re-derived comparison / edge identity. Non-empty end-to-end selection/lineage/gap parity was exercised indirectly by the PIT v3 suite. No dedicated duplicate/extra-edge counterexample test was added.

Exact independent review commands and results:

```bash
uv run --offline pytest -q --tb=short tests/infrastructure/revision/test_channel_reconcile_bounded_stream.py
# 2 passed in 8.17s

uv run --offline pytest -q --tb=short tests/infrastructure/pit/test_selector_v3.py -k 'not test_canonical_key_closure_is_spilled_and_matches_v2_rows and not test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
# 27 passed, 2 deselected in 72.26s
```

Review verdict: **INCOMPLETE**. `rest_revisions`, `rest_records`, `archive_revisions`, `archive_records`, and `graph_evidence` still grow with one observation key's history/edges. The change reduces several O(H) holds but does not make one-key working state bounded; no full-process RSS/byte measurement or 32 MiB evidence exists. The deferred `tests/infrastructure/revision/test_channel_reconcile.py` module and two excluded PIT nodes were not run. E1-CAP-1 and Phase 1 remain open.

### E1 high-fan-in regression follow-up — 2026-09-29

Commit `8d02548e9149d845c9f37820c30e717752e32119` adds only a test to the bounded-stream module; it changes no production source. It creates four archive and four REST revisions for one observation key, expects four committed edges, compares sorted edge IDs and complete persisted `row()` values, and observes that each `_existing_edges` call receives one row and one comparison.

The exact test node was run twice:

```bash
uv run --offline pytest -q --tb=short tests/infrastructure/revision/test_channel_reconcile_bounded_stream.py::test_single_key_edge_fan_in_is_merged_one_edge_at_a_time
# first run: 1 failed in 3.18s; the original full ChannelEdge object equality failed at index 0

uv run --offline pytest -q --tb=short -vv tests/infrastructure/revision/test_channel_reconcile_bounded_stream.py::test_single_key_edge_fan_in_is_merged_one_edge_at_a_time
# after changing the assertion to IDs/order + persisted-row equality: 1 passed in 2.90s
```

This confirms the tested IDs/order, serialized row values, and one-row/one-comparison observer bounds. It does **not** explain the original object-level equality mismatch; that remains under independent QA review. The passing retry is not evidence of `ChannelEdge` object equality. No other deferred tests were run. This regression adds useful per-key edge fan-in evidence but does not close the remaining O(H) revision/record/graph state or the E1-CAP-1 32 MiB gate.

### E1 per-key revision staging — `fe3cd7c` independent review

Commit `fe3cd7cc9d07cd277807e81073c6bb2106a4a1f8` stages one key's REST/archive `ChannelRevision` inputs in revision-ID-sorted RunSets and reads the runs for pairwise D3E comparison. Independent review used a clean detached worktree at that exact SHA; no files were changed.

```bash
uv run --offline pytest -q --tb=short tests/infrastructure/revision/test_channel_reconcile_key_run_spill.py
# 1 passed in 21.05s

uv run --offline pytest -q --tb=short tests/infrastructure/revision/test_channel_reconcile_bounded_stream.py -k 'not test_single_key_edge_fan_in_is_merged_one_edge_at_a_time'
# 2 passed, 1 deselected in 8.12s
```

The dedicated test streams 16 REST rows for one key with capacity 2 and forbids fallback to `_plan`; source review found no ordering / comparison defect. It does not directly assert flush count, RunSet root record/leaf counts, maximum buffered rows, or long-history Archive/REST fan-in. The excluded high-fan-in node remains unresolved at the `ChannelEdge` object-equality level; its prior retry only proved ID/order and serialized-row parity.

Review verdict: **INCOMPLETE**. `rest_records`, `archive_records`, `graph_evidence`, and the full `assemble_channel_graph` / `RevisionGraph` state are still O(H). A single row may also contain unbounded nested evidence. This commit does not prove physical byte bounds or complete-process 32 MiB memory. No deferred module or PIT node was run.

### E1 cross-key revision claim check — `644ad19` independent review

Commit `644ad19121945f6e385940a0b892f9b2a6f582e8` adds a day-scoped RunSet claim pass to the bounded verified-edge path. It records REST and Archive revision IDs, `supersedes` endpoints, and both endpoints of each verified precedence-evidence edge; it externally sorts by `(revision_id, observation_key)` and rejects a second key before exposing the staged output root. The conflict diagnostic retains only the first two sorted keys.

The new helper regression was executed twice by the developer. The final version covers three distinct keys claiming one ID and stable first-two-key reporting:

```text
uv run --offline pytest -q --tb=short tests/infrastructure/revision/test_channel_reconcile_cross_key_claims.py::test_cross_key_revision_claim_is_rejected_after_sorted_spill
1 passed in 0.16s

uv run --offline pytest -q --tb=short tests/infrastructure/revision/test_channel_reconcile_cross_key_claims.py::test_cross_key_revision_claim_is_rejected_after_sorted_spill
1 passed in 0.14s
```

Independent review did not rerun that two-pass node. It ran:

```text
uv run --offline ruff check infrastructure/revision/channel_reconcile.py tests/infrastructure/revision/test_channel_reconcile_cross_key_claims.py
All checks passed!

git diff --check HEAD^..HEAD
exit 0; no output
```

The test exercises the external-sorted claim helper directly rather than routing a malformed row through `iter_verified_edges`. Independent review traced the lawful D3E producers and persisted-row verifiers: REST and Archive IDs are re-derived from key-bound identities, their lawful supersedes are empty in this path, and precedence evidence is re-derived from same-key channel comparison. A conflicting cross-key fixture cannot pass those verifiers, so the absence of an end-to-end malformed-row fixture is not an acceptance blocker for this bounded D3E slice. The static audit confirmed all four production claim sources are wired into the day-scoped RunSet before output is returned.

Review verdict: **ACCEPT, narrowly scoped to the current D3E pinned-day path**. This is not generic cross-partition `RevisionGraph` coverage, E1-CAP-1, or Phase 1 acceptance. Remaining E1 gates include bounded PIT key/head and per-key graph state, byte/RSS evidence, DQ-9 capacity basis, and the complete-process 32 MiB probe. The earlier unresolved `ChannelEdge` object-equality test remains deferred and is not passed by this slice.
