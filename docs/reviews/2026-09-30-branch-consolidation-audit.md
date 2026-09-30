# Branch and worktree consolidation audit — 2026-09-30

## Decision and result

The project is now being carried on one isolated integration line, `codex/project-consolidation`, rooted at E1 candidate `2716c7d`. Commit `4eeea33` merges the clean W1/module line `codex/w1-independent-integration@da0071e` and resolves conflicts by retaining the newer E1 contract and composing compatible W1 boundedness work. Follow-up commits selectively port safe root W1 cases, implement ADR-0096, add real durable-gate coverage, reconcile E1 cross-module test seams, fix shared RunSet merge lifecycle, and integrate the Dataset Quality v3 consumer/replay slice. The latest integration commit is recorded in git history; this document and [Dataset Quality v3 consumer review](2026-09-30-dataset-quality-v3-consumer.md) record its scoped evidence.

This is a consolidation line, not a release or Phase acceptance. It has not been pushed or merged to `main`. In addition to targeted ADR-0096 verification (ledger `12 passed`, durable/loop `89 passed`, one real closed-gate test `1 passed`), the first Normalizer/PIT/Dataset focused run returned `236 passed, 7 failed, 1 skipped`. Five stale API-seam/parity fixtures were corrected; two shared RunSet defects were fixed in `2fa58c6` and independently approved. The rerun returned `247 passed, 1 skipped`. No full integration suite or E1-CAP-1 measurement has been run. The merge index passed `git diff --cached --check`; the implementation reviewer also parsed the 11 initially touched implementation files as Python syntax, but that was not runtime verification. Detailed results are in the [foundation integration regression review](2026-09-30-foundation-integration-regression.md).

## Repository topology

The initial audit found 57 local branch refs, 70 worktrees, 15 detached worktrees, and 370 `refs/archive/*` refs. Creating the consolidation branch and worktree brings the audited snapshot to **58 local branch refs and 71 worktrees**. Of these worktrees, 56 are attached to a branch and 15 are detached. The four pre-existing dirty worktrees remain preserved; the consolidation worktree was clean after its merge commit and now contains the follow-up commits above.

After the initial snapshot, five isolated branches were added: `codex/streaming-runs-iterable-fix` was independently approved and integrated by `2fa58c6`; the Dataset consumer candidates `codex/dataset-quality-v3-consumer-seam`, `codex/dataset-quality-legacy-row-bound`, and `codex/dataset-quality-v3-replay-guards` were reviewed in sequence, with only the final candidate integrated; `codex/canonical-scratch-integration-port` supplied the narrowly approved reader-cleanup commit `2f98461`. The current repository topology is **63 local branch refs and 75 worktrees** (60 branch-attached, 15 detached). The appendix below intentionally remains the original 58-ref snapshot at consolidation creation; these later branches are recorded here rather than rewriting that baseline inventory.

The initial dirty worktrees were:

| Worktree | Branch / tip | Dirty entries | Disposition |
|---|---|---:|---|
| `/home/raphael/projects/hlens-autoresearch` | `codex/w1-stabilization@2d263bf` | 146 | Preserved unchanged; 145 tracked edits plus one unique golden experiment JSON. Its remaining unique delta still needs review against the consolidated line. |
| `~/.codex/worktrees/e1-phase1-progress/hlens-autoresearch` | `codex/e1-phase1-progress@2716c7d` | 15 | Preserved unchanged. Contains unfinished Dataset Quality consumer wiring and Normalizer/PIT fixes; not yet independently reviewed or integrated. |
| `~/.codex/worktrees/e1-pit-stream-hardening/hlens-autoresearch` | `codex/e1-pit-stream-hardening@8c7fefe` | 148 | Preserved unchanged. Of 145 paths shared with the root dirty tree, 142 contents were byte-identical; only three shared paths differed, so it was not merged as a second whole tree. |
| `~/.codex/worktrees/adr-bounded-quality-reports/hlens-autoresearch` | `codex/adr-bounded-quality-reports@652f8c6` | 3 | Preserved unchanged. Its uncommitted ADR-0094 draft collides with the adopted PIT ADR number and was not merged. |

The 370 archived refs are historical evidence, not active integration inputs. 292 tips were outside `main`, representing 182 unique tips. Detached review/baseline worktrees and archive refs were not deleted.

## Branch family dispositions

### Integrated into the consolidation line

- `codex/e1-phase1-progress@2716c7d` is the E1 spine. It contains accepted ADR-0093 Quality evidence streams and ADR-0094 PIT conflict-head evidence, plus the prior E1 candidate slices. It remains incomplete: E1-CAP-1 and Phase 1 are not accepted.
- `codex/w1-independent-integration@da0071e` was merged once in `4eeea33`. W1 code and module wiring were composed with E1 semantics; overlapping trees were not replayed a second time.
- The `codex/pit-conflict-current-version-tests@7d733c5` 2.5.0 compatibility patch had no remaining source delta when applied to the E1 candidate: DTO, OpenAPI/Web type, legacy/current fixture and version-test content were already represented in the candidate tree. Its historical acceptance evidence remains in project records, but was not rerun on the consolidation tree.
- `codex/canonical-position-bounds@991b126` is a separately approved source for explicit canonical scratch ownership. The consolidation merge carries the compatible explicit scratch-path threading through Normalizer, PIT, Dataset, Quality and tools. Its branch is retained for provenance; it was not merged wholesale.
- `codex/canonical-scratch-integration-port@4397fd2` contributed only the `_positions()` index-init failure cleanup and direct regression test. Three independent reviewers approved it; the new test passed independently in all three review runs. It was cherry-picked as `2f98461`; the branch remains as provenance.

### Preserved for focused follow-up; not yet integrated

- The root dirty W1 worktree (`codex/w1-stabilization`) has many unique changes. Initial comparison found 127 of its 145 tracked dirty files did not exactly match five candidate branch tips. These edits and the unique golden JSON remain intact; they require a file-by-file delta review before porting.
- The dirty E1 candidate has 15 uncommitted paths. Dataset Quality consumer wiring and Normalizer `scan_columns` fixes remain WIP until reviewed against the merged APIs.
- The dirty `codex/e1-pit-stream-hardening` tree is mostly a duplicate of the root dirty W1 tree. Review only its three differing shared files: `docs/adr/README.md`, `infrastructure/dataset/sources.py`, and `tests/infrastructure/dataset/test_dataset_v3_sources.py`.
- `codex/p7-acceptance@220d967` is a separate retry-uniqueness/refusal slice dependent on durable ledger and worker behavior. It remains a later integration candidate; its slice evidence is not Phase 7 acceptance.
- `codex/web-dependency-security@5070d17` has prior audit/build/component-test evidence, but the Web library `retroAudit` failure remains a deferred release gate. Its dependency changes need compatibility review after the current merge.
- `codex/report-dto-contract@65feeb7` is retained as provenance. The effective DTO work is in the integrated W1 tree and 2.5 compatibility sources; no second DTO patch was replayed.

### Superseded, already ancestral, or historical candidate branches

- ADR-0094 parallel branches (`codex/adr-0094-pit-conflict-stream`, `codex/e1-pit-conflict-contract`, `codex/pit-conflict-v3`, `codex/pit-conflict-test-version-fix`) are not merged wholesale. The accepted design and implementation are represented by the E1 spine; the bounded conflict output and 2.5 replay compatibility are retained there.
- `codex/e1-bounded-iceberg-metadata` is a parallel history. E1 carries a later implementation; do not replay the older tree over it.
- Early E1 CAP, key-row, archive, history, Dataset carry, Quality stream/reporter, replay, revision-ID, PIT-edge and Universe branches are retained as slice provenance. The latest E1 spine already contains their relevant ancestral or later work; any missing patch must be identified by file/commit, not by branch-name merge.
- `codex/dataset-universe-bounds` and `codex/e1-canonical-scratch-integration` contain useful earlier bounded-run/Universe work. E1 contains later equivalents; compare individual deltas before reuse.
- Remaining `feature/*`, `local/*`, detached, and archived refs are historical candidates or review snapshots. No bulk merge or branch/worktree deletion was performed.

## ADR number collision resolved

The incoming W1 line used ADR-0093 for a Worker decision, while the E1 line uses ADR-0093 for Quality evidence streams and ADR-0094 for PIT conflict-head evidence. The integration preserves the accepted E1 numbering. The distinct Worker decision is now [ADR-0095](../adr/0095-worker-runtime-host.md), with an explicit note that its source branch used ADR-0093. The later E1 ADR-0094 remains canonical and absorbs the compatible sorted-run limits, reader-integrity, and orphan handling requirements from the earlier PIT decision packet. The duplicate PIT ADR file was not retained as a second numbered decision.

## Merge invariants and acceptance status

- Keep contract 2.5.0, ADR-0094 full conflict-head evidence streams, and 2.3/2.4 replay compatibility.
- Keep E1's bounded PIT proof/selector and neutral streaming RunSet APIs. Port compatible W1 bounded archive handling, explicit scratch ownership, Dataset member-span replay, and Universe in-window event filtering.
- Preserve E1 and W1 non-overlapping regression cases; make PIT scratch directories explicit in test call sites. These tests were edited as part of the merge but **not executed**.
- Preserve historical failed/deferred evidence. W1's historical full run had 7 failures; Web library history had 1 deferred `retroAudit` failure. Neither is a pass.
- E1-CAP-1 remains open. Existing partial capacity diagnostics exceeded the 32 MiB budget and do not match the consolidated tree. No Phase 1 acceptance is claimed.

## Current local branch refs

This list records the 58 local heads immediately after creating the consolidation line; archive refs are excluded.

```text
codex/adr-0094-pit-conflict-stream 04e4b8a
codex/adr-bounded-quality-reports 652f8c6
codex/canonical-position-bounds 991b126
codex/dataset-universe-bounds c69f63b
codex/e1-adr0093-jsonl-stream 0dfbdd1
codex/e1-archive-spool 2a05ccd
codex/e1-bounded-iceberg-metadata 5a0cf14
codex/e1-canonical-scratch-integration bd90e96
codex/e1-cap-archive-parser 3a7ff8c
codex/e1-cap-edge-join 8d02548
codex/e1-cap-graph-stream 14a00aa
codex/e1-cap-input ece4f6f
codex/e1-cap-input-next 9178f5d
codex/e1-cap-input-plan feb4068
codex/e1-cap-input-stream 2bdbe4a
codex/e1-cap-key-graph fe3cd7c
codex/e1-cap-key-graph-next 644ad19
codex/e1-cap-keyrows c57b041
codex/e1-cap-keyrows-next 00e1899
codex/e1-cap-keytree-validator 53a46aa
codex/e1-cap-max-heads bf26921
codex/e1-cap-pit-record-staging cf1c759
codex/e1-cap-pit-window-stream 59287b1
codex/e1-cap-status-refresh 1b42e04
codex/e1-dataset-carry-runset e12fbb8
codex/e1-history-bounds 27b8b54
codex/e1-history-integration b40601a
codex/e1-key-row-index-deferred ad46151
codex/e1-normalizer-proof-deferred cadf48e
codex/e1-normalizer-snapshot-stream c938f68
codex/e1-normalizer-wanted-set 39948ed
codex/e1-phase1-progress 2716c7d
codex/e1-pit-conflict-contract 64124ef
codex/e1-pit-edge-runset 590dbc3
codex/e1-pit-key-rows-runset f928828
codex/e1-pit-key-rows-runset-v2 7647351
codex/e1-pit-stream-hardening 8c7fefe
codex/e1-quality-gap-projection fdd9753
codex/e1-quality-manifest-store d5be275
codex/e1-quality-report-manifest-table f6abf7b
codex/e1-quality-report-projection ed3399d
codex/e1-quality-stream-v3 4e2db16
codex/e1-quality-v3-reporter 5b97c06
codex/e1-replay-25-tests 5b11da0
codex/e1-revision-ids-tests 59cd267
codex/e1-w3-universe-integration b849aac
codex/p7-acceptance 220d967
codex/pit-conflict-current-version-tests 7d733c5
codex/pit-conflict-test-version-fix 27beaa3
codex/pit-conflict-v3 5d9dd71
codex/pit-edge-validation 34700f2
codex/pit-key-stream 258b082
codex/project-consolidation 4eeea33
codex/report-dto-contract 65feeb7
codex/w1-independent-integration da0071e
codex/w1-stabilization 2d263bf
codex/web-dependency-security 5070d17
main e584187
```

## Next sequence

1. Implement ADR-0097's caller-owned SQLite graph scratch index on an isolated branch; preserve current graph invariants, complete ADR-0094 heads stream, v2 replay, explicit path ownership, and orphan-marker rules.
2. Independently review and test that PIT slice; measure long-chain and repeated-cutoff query cost. Then proceed to Quality rule identity/input-table binding and remaining active bounded-path gaps.
3. Reconcile the root W1 dirty delta and E1's 15-path dirty candidate file by file; keep source worktrees untouched. The three differing paths in the dirty PIT hardening copy remain a separate comparison.
4. After the active code slices are integrated, run relevant regressions and full-process E1-CAP-1 on the exact consolidation HEAD. Do not infer acceptance from branch-local runs.
