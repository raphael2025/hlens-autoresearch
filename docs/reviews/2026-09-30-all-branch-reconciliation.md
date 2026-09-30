# All-branch reconciliation — 2026-09-30

## Snapshot and method

This is a read-only reconciliation of the local branch graph against integration base `91161b69b5581c340f1646dac9cfb39a59a6d3ca`. It inventories all local topic tips using ancestry and patch-equivalence checks, then compares unique paths with current status/review records. No branch or worktree was deleted or rewritten.

At this snapshot there were **65 local refs** (one `main`, 64 topic refs) and **77 worktrees** (62 branch-attached, 15 detached). `codex/e1-cap-pit-sqlite-selector` and `codex/project-consolidation` pointed to the same integration commit.

Of 64 topic refs, 24 tips were already ancestors of integration; 20 had diverged but all their patches were already equivalent in integration; 20 had at least one patch-unique commit. A unique commit is not proof that its code is missing: most such deltas were superseded, already selectively ported, deferred experiments, or historical status records.

## Branch disposition

### Ancestors of integration: provenance only (24)

`e1-cap-archive-parser@3a7ff8c`, `e1-cap-edge-join@8d02548`, `e1-cap-graph-stream@14a00aa`, `e1-cap-input@ece4f6f`, `e1-cap-input-next@9178f5d`, `e1-cap-input-plan@feb4068`, `e1-cap-input-stream@2bdbe4a`, `e1-cap-key-graph@fe3cd7c`, `e1-cap-key-graph-next@644ad19`, `e1-cap-keyrows@c57b041`, `e1-cap-keyrows-next@00e1899`, `e1-cap-keytree-validator@53a46aa`, `e1-cap-max-heads@bf26921`, `e1-cap-pit-record-staging@cf1c759`, `e1-cap-pit-sqlite-selector@91161b6`, `e1-cap-pit-window-stream@59287b1`, `e1-normalizer-snapshot-stream@c938f68`, `e1-normalizer-wanted-set@39948ed`, `e1-phase1-progress@2716c7d`, `e1-w3-universe-integration@b849aac`, `e1-pit-key-rows-runset@f928828`, `pit-conflict-v3@5d9dd71`, `project-consolidation@91161b6`, and `w1-independent-integration@da0071e`. `main@e584187` is also an ancestor.

### Diverged, patch-equivalent: provenance only (20)

`adr-0094-pit-conflict-stream@04e4b8a`, `canonical-scratch-integration-port@4397fd2`, `e1-adr0093-jsonl-stream@0dfbdd1`, `e1-archive-spool@2a05ccd`, `e1-bounded-iceberg-metadata@5a0cf14`, `e1-dataset-carry-runset@e12fbb8`, `e1-history-integration@b40601a`, `e1-pit-edge-runset@590dbc3`, `e1-pit-key-rows-runset-v2@7647351`, `e1-quality-gap-projection@fdd9753`, `e1-quality-manifest-store@d5be275`, `e1-quality-report-manifest-table@f6abf7b`, `e1-quality-report-projection@ed3399d`, `e1-quality-v3-reporter@5b97c06`, `e1-revision-ids-tests@59cd267`, `pit-conflict-current-version-tests@7d733c5`, `pit-conflict-test-version-fix@27beaa3`, `pit-edge-validation@34700f2`, `pit-key-stream@258b082`, and `web-dependency-security@5070d17`.

### Patch-unique commits: preserve, defer, or selectively port (20)

| Branch | Disposition |
|---|---|
| `adr-bounded-quality-reports@652f8c6` | Preserve provenance. Current tree has later Quality stream/projection/reporter work. Its dirty ADR-0094 draft conflicts with adopted numbering; inspect only a concrete missing path. |
| `adr0097-pit-sqlite-graph@cef7b11` | Preserve provenance. Validator foundation is integrated as `889b268`; subsequent selector work is separate. |
| `canonical-position-bounds@991b126` | Preserve provenance. Accepted scratch wiring is carried by the integration line. |
| `dataset-quality-legacy-row-bound@efc5689` | Preserve provenance; final consumer candidate superseded it. |
| `dataset-quality-v3-consumer-seam@ed58257` | Preserve provenance; later candidate superseded it. |
| `dataset-quality-v3-replay-guards@d31eef3` | Preserve provenance; its accepted consumer slice is represented in integration. |
| `dataset-universe-bounds@c69f63b` | Preserve provenance; later PIT/Universe bounded-run work is integrated. |
| `e1-canonical-scratch-integration@bd90e96` | Preserve provenance; later scratch, Universe, and PIT implementations supersede it. |
| `e1-cap-status-refresh@1b42e04` | Preserve old evidence; do not overwrite current status with historical docs. |
| `e1-history-bounds@27b8b54` | Preserve provenance; archive spooling and reader cleanup have later integration equivalents. |
| `e1-key-row-index-deferred@ad46151` | Preserve deferred experiment and capacity evidence; do not port without new measurement. |
| `e1-normalizer-proof-deferred@cadf48e` | Preserve deferred experiment and evidence; do not port without new measurement. |
| `e1-pit-conflict-contract@64124ef` | Contract shape is represented by ADR-0094. Review only its fixed historical replay-test delta below. |
| `e1-pit-stream-hardening@8c7fefe` | Preserve provenance; later per-key streaming work supersedes this candidate. |
| `e1-quality-stream-v3@4e2db16` | Preserve provenance; current Quality implementation is later. |
| `e1-replay-25-tests@5b11da0` | Selectively port the fixed 2.3/2.4 manifest goldens and replay assertions below. Do not merge the branch. |
| `p7-acceptance@220d967` | Hold for a separate P7 review after durable ledger/worker dependencies are active. |
| `report-dto-contract@65feeb7` | Preserve provenance; effective DTO changes are integrated. |
| `streaming-runs-iterable-fix@d772eff` | Preserve provenance; integrated through `2fa58c6`. |
| `w1-stabilization@2d263bf` | Its branch-tip unique commit is historical status/review material; preserve it without replacing current docs. |

## Small code and test deltas found outside clean branch tips

The dirty root W1 checkout is a separate source of changes; it is not represented by its branch tip or the patch-equivalence counts above. At the audit snapshot it had 146 dirty entries: 145 tracked edits plus one unique golden JSON. Its content differs from the consolidation tree across 130 files, mixing old docs/pins, already integrated code, and a few narrow candidate fixes.

- **BatchGrid bug confirmed:** `research/hypotheses/batch.py` freezes parameter points as `FrozenMapping`, then passes them to `content_hash`, whose JSON encoding cannot serialize that mapping. A minimal reproduction on the integration base returned `TypeError: Object of type FrozenMapping is not JSON serializable`. The fix is limited to hashing the ordinary dict representation (`dict(p)`), with `tests/research/hypotheses/test_batch.py` as the direct regression target. This narrow fix is queued after the active PIT selector slice.
- **PluginManifest delta rejected as redundant:** current `infrastructure/plugins/manifest.py` already rejects non-string name/version/contract-version inputs as `PluginManifestError`; a direct integer-input check confirmed all three cases. No patch is needed.
- **Do not copy a set-to-dict “optimization”:** the dirty `row_integrity.py` replaces `set(history.prefixes)` with `dict.fromkeys(...)` for an annotation, while both grow with prefix count and the dict has higher overhead. Keep the current set and, if needed, correct the helper's type to a membership protocol.
- Other dirty differences include older contract 2.4 pins, formatting/type changes, and code equivalent to later E1 implementations. Do not transplant them as a group.

## Selective historical replay evidence

The integration base lacked two fixed fixtures: `tests/golden/v2_3_0/dataset_v3_manifest.json` and `tests/golden/v2_4_0/dataset_v3_manifest.json`. Branch `codex/e1-replay-25-tests@5b11da0` adds these fixtures and assertions to `tests/test_adr_0077_evidence_manifest.py` for 2.3/2.4 persisted v3 replay and 2.5 current-shape/refusal behavior. The integration tree has dynamic version tests, but fixed historical evidence improves the already accepted ADR-0052/0077/0094 compatibility proof. Port only these fixtures/assertions after review against the current contract; keep legacy hashes intact.

## Decision

No old branch should be merged wholesale. Keep all branches, archived refs, failed experiments, and dirty worktrees intact as provenance. Selectively integrate only a verified, phase-relevant file-level delta. At this audit snapshot, the actionable items are SQLite selector/head integration, fixed historical Dataset v3 replay fixtures/tests, and the BatchGrid hash fix. P7 and Web release gates stay with their own review phases. E1-CAP-1 and Phase 1 remain open.
