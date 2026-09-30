# ADR-0094: Dataset quality-report snapshot replay

| Field | Value |
|---|---|
| Status | **Proposed** — no approval recorded |
| Date | 2026-09-29 |
| Decision makers | Pending review by the authorized project decision maker |
| Drafter | Codex acceptance follow-up |
| Related phases | Phase 1 — Market Representation / ADR-0077 §6.1.5 |
| Affected areas | Data / Infrastructure / Research Dataset verification |
| Compatibility | No contract or schema change proposed; existing Dataset source bindings are assumed to bind one report-manifest snapshot and one QGAP snapshot. |
| Prerequisites | [ADR-0031](0031-quality-evidence-gap-table.md), [ADR-0077](0077-bounded-research-dataset-evidence.md), and formal acceptance of the ADR-0093 amendment defining `qgap_output_snapshot_id` and deterministic `existing_only` replay |

## Context

The accepted base decision in ADR-0093 establishes bounded, versioned quality-report evidence;
its amendment defining `qgap_output_snapshot_id` and deterministic `existing_only` replay remains
a prerequisite and is not made operative by this proposal. Once that amendment is accepted, its
v3 report manifest records the exact QGAP output snapshot used to replay one canonical report. A
Dataset binds one snapshot per source table and may reference multiple such canonical v3 reports,
each created at a different QGAP snapshot. The Dataset verifier therefore needs an explicit rule
relating the single Dataset-bound QGAP snapshot to each v3 report's output snapshot without adding
an unbounded per-report snapshot list to a Dataset contract.

This proposal is a separate amendment gate for the Dataset verification rules in ADR-0077
§6.1.5. It does not amend or reinterpret ADR-0077 until accepted.

## Proposed decision

This protocol applies only to canonical v3 quality-report references governed by the accepted
ADR-0093 amendment described above. It does not change the existing verification branches for
listing-v2 references or legacy inline/row report references. Those continue to be validated under
the applicable ADR-0031, ADR-0077, and ADR-0093 compatibility rules and are not resolved through
`M` or required to carry an `S_i`.

For each canonical v3 report reference in a v3 Dataset replay, use the following snapshot
identities:

- `M`: the single `quality.data_quality_report_manifests` table snapshot bound by the Dataset.
- `D`: the single `quality.availability_evidence_gaps` table snapshot bound by the Dataset.
- `S_i`: the `qgap_output_snapshot_id` in the manifest for the i-th referenced canonical report.

The Dataset spec binds `M` and `D` once each. It does not add `S_i` entries to the Dataset contract.
The report manifest row, resolved from `M`, is the authority for each report's `S_i`.

### Replay and equality protocol

1. For each canonical v3 report reference, resolve only report-manifest rows visible at `M`.
   Require exactly one matching report manifest and read its `S_i`. Do not use the current
   manifest-table head. Validate listing-v2 references and legacy inline/row report references
   through their existing compatibility branches; they do not participate in this `M`/`S_i`
   protocol.
2. Require every `S_i` to be the same snapshot as or an ancestor of `D` in the same
   `quality.availability_evidence_gaps` table lineage. Multiple reports may have distinct `S_i`
   values; they all share the one Dataset-bound `D`.
3. At each `S_i`, run that report's `existing_only` re-derivation using the input snapshots pinned
   in its report manifest. It performs no writes and reads no clock. Verify the expected event,
   revision, and gap streams against that manifest and verify its complete QGAP projection at
   `S_i` under ADR-0031's exact batch, equality, completeness, and no-delete rules.
4. Separately read the i-th report's complete QGAP projection at `D`. Stream-compare it in
   bounded order with the verified rows at `S_i`, including `quality_report_id`, `table`,
   `revision_id`, `gap`, `subject_symbol`, `subject_start`, and `batch_index`. Require equal
   counts and simultaneous exhaustion. This proves the report's rows are visible at `D` and that
   no row for the report was added, omitted, moved, or changed between `S_i` and `D`. Filter by
   the exact report ID so rows belonging to other reports at `D` do not affect the comparison.
5. A missing manifest row, unresolved `S_i`, different table lineage, unprovable ancestry,
   unreadable/expired snapshot, missing expected row at `D`, extra or changed row, or any mismatch
   fails closed. Empty gap output is checked as zero rows at both `S_i` and `D`; it is not an
   exemption from snapshot lineage validation.

### Retention and resource boundary

The report-manifest snapshot `M`, Dataset QGAP snapshot `D`, each report snapshot `S_i`, and all
content-addressed report objects referenced by those manifests must remain readable while a
committed Dataset or report manifest that references them remains replayable. Snapshot expiration
or object maintenance must not remove a reachable reference. If a configured Catalog or storage
adapter cannot retain and reopen those snapshots/objects, replay fails closed.

The ancestry proof may inspect snapshot history of size H and therefore may take O(H) time and
metadata memory. This proposal does not classify ancestry traversal or PyIceberg metadata as
bounded; both remain in the complete-process E1-CAP-1 measurement, consistent with ADR-0077 §6.2.6
and §10. No E1-CAP-1 or Phase 1 acceptance claim follows from this protocol.

### Contract boundary

This proposal adds no field to `DatasetQualityReportRef`, `ResearchDatasetEvidenceManifest`, or
`PointInTimeSpec`, and requires no schema migration if the existing Dataset source bindings can
pin one report-manifest snapshot `M` and one QGAP snapshot `D`. Each `S_i` is read from the bound
report-manifest row rather than copied into the Dataset contract. If implementation review shows
that the current contract cannot express the required `M` and `D` bindings or preserve their
replay identity, stop and propose a separate additive contract ADR; do not silently extend a
contract or infer that this ADR authorizes a schema change.

## Alternatives

| Option | Advantages | Disadvantages | Assessment |
|---|---|---|---|
| **A. One Dataset-bound `D`, with each manifest's `S_i` an ancestor of `D`** | Keeps one QGAP source binding regardless of report count; preserves each report's original commit point; permits different report creation snapshots while proving all rows are visible at `D`. | Requires lineage/ancestry proof and retention of `M`, `D`, and every reachable `S_i`; ancestry work is O(H) and must be measured. | **Recommended.** It uses existing bindings and report-manifest references without adding O(number of reports) contract state. |
| B. Add each `S_i` to `DatasetQualityReportRef` or the Dataset manifest | Makes each report's exact QGAP snapshot explicit in the Dataset object. | Adds a per-report snapshot list whose size grows with report count; changes a published contract/schema and hash; requires migration/replay design and still needs a rule to compare the Dataset-wide QGAP view. | Not recommended; it introduces unbounded Dataset metadata and a contract change without removing the need for shared-view verification. |
| C. Require every referenced report to use the same QGAP snapshot | Simplifies the ancestry check. | Reports are normally committed at different times; forcing a common snapshot requires rebuilding or re-referencing immutable reports and disrupts report commit identity/idempotence. | Not recommended; it is incompatible with append-only report commits and ordinary multi-report Dataset assembly. |

## Consequences

- A Dataset's report-manifest and QGAP bindings have one reproducible identity each; per-report
  report snapshots are obtained through the pinned manifest rows.
- The cross-snapshot rule applies only to canonical v3 report references after the ADR-0093
  amendment prerequisite is accepted; legacy branches retain their existing verification rules.
- Datasets fail closed when snapshot ancestry or required historical reads cannot be established.
- Implementations must validate retention and replay behavior before claiming Dataset verification
  compliance; O(H) ancestry work remains a capacity measurement item.
- No persisted contract hash, `DatasetQualityReportRef` field, legacy v2 replay behavior, or
  existing table schema changes under this proposal.

## Compliance checks

- [x] No core contract or schema changes are proposed.
- [x] Legacy Dataset and quality report replay semantics remain unchanged.
- [x] Fail-closed behavior is specified for missing history and mismatched snapshots.
- [x] PyIceberg O(H) metadata remains outside boundedness claims and inside E1-CAP-1 measurement.
- [ ] Authorized decision maker review and acceptance.

## References

- [ADR-0031](0031-quality-evidence-gap-table.md) — QGAP batch identity, completeness, and no-delete semantics.
- [ADR-0077](0077-bounded-research-dataset-evidence.md) — Dataset v3 verification and E1-CAP-1 boundaries.
- [ADR-0093](0093-bounded-quality-report-evidence.md) — report manifest and `qgap_output_snapshot_id` draft protocol.
