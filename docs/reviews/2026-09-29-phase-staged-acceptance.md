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

The isolated candidate `codex/e1-cap-pit-window-stream@1715d398fa9d26b3af391e8efcefb0ce06d925c5` (parent `3a7ff8c`) stages wanted keys, closure rows/chains, event days, and final row/edge outputs in external RunSets. It processes canonical proof and edge mapping one observation key at a time; a follow-up change also removed completed-chain callbacks from an `ExitStack` so disjoint chain count does not grow retained contexts. This candidate is not integrated into the root checkout; independent review is in progress.

Developer verification on the candidate:

```text
uv run pytest -q tests/infrastructure/pit/test_selector_v3.py -k 'not test_canonical_key_closure_is_spilled_and_matches_v2_rows and not test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer'
23 passed, 2 deselected in 27.04s

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
