# ADR-0093: Bounded, versioned quality report evidence streams

| Field | Value |
|---|---|
| Status | **Accepted** (2026-09-29, Codex under Raphael's continuing project delegation) |
| Date | 2026-09-29 |
| Decision makers | Codex (technical delegation recorded in `CLAUDE.md` §0) |
| Related phases | Phase 1 — Market Representation (E3 / E1-CAP-1) |
| Affected areas | Infrastructure / Iceberg registry / quality reports / Dataset v3 |
| Compatibility | Additive. Existing `quality.data_quality_reports` schema, definition hash, rows, and v1/v2 report replay remain unchanged. |
| Prerequisites | [ADR-0031](0031-quality-evidence-gap-table.md), [ADR-0077](0077-bounded-research-dataset-evidence.md) |

**Amendment status:** The `Accepted` status above applies to the base decision recorded on
2026-09-29. The additional protocol text currently marked as an amendment draft in this file is
not accepted and does not amend that decision until it is separately reviewed and accepted. The
base decision remains in force within its original scope.

## Context

ADR-0077 §6.1.5 requires `existing_only` quality-report re-derivation to compare the report's
complete contents in bounded memory, or to prove a contract-level fixed upper bound for one
report. The current report row stores `events` and (for legacy listing reports) `evidence_gaps` as
nested Arrow lists. Their cardinality is not bounded by a contract. Reading one Iceberg row
therefore decodes an unbounded payload even when rows are fetched in record batches. The current
reporters also build these lists in memory. A Python iterator around the existing nested list
would not change this boundary.

The single-key PIT conflict result has the same issue: a conflict can have an unbounded number of
heads, and each quality event may refer to all of them. The Dataset v3 bounded path must not
silently truncate this evidence.

## Decision

### 1. Preserve legacy reports and add a v3 report store

1. `quality.data_quality_reports` and its registered definition remain unchanged. Existing
   `hlens.quality.canonical-partition@1.0.0` and `@2.0.0` reports and
   `hlens.quality.listing-history@1.0.0` reports remain readable by their legacy path; their
   persisted rows are never rewritten or migrated.
2. New bounded reports use an additive fixed-size Iceberg table
   `quality.data_quality_report_manifests`. It is append-only and contains report identity,
   rule/version/hash, subject and pinned source bindings, `knowledge_time`, record counts, and
   three fixed-size stream references: `events`, `event_revisions`, and `evidence_gaps`. Each
   reference contains the format version, record/leaf counts, tree depth, and root object
   key/SHA-256/size. Canonical v3 manifests separately record `qgap_output_snapshot_id`, the
   exact output snapshot of `quality.availability_evidence_gaps` captured after all batches for
   this report have been verified and before the manifest commit. It is distinct from the pinned
   input/source snapshot bindings; it is not an input to report derivation. This field is required
   even when the expected gap count is zero. If no addressable QGAP snapshot exists, report
   creation fails closed without a manifest; table provisioning must establish a readable snapshot
   before enabling canonical v3 reports. A retry that finds a committed manifest reuses its
   recorded output snapshot and compares against it; it must not substitute the current table head.
   `existing_only` reads that same recorded output snapshot. Listing v2 manifests do not write
   QGAP rows and have no QGAP output snapshot. Bounds are part of the v1 row format:
   `report_id` ≤ 512 UTF-8 bytes; rule id
   ≤ 128; rule version ≤ 32; every table name ≤ 255; every snapshot id ≤ 256; every object key
   ≤ 1024; symbol ≤ 64; hashes are exactly 64 lowercase hex bytes; the input-binding list has at
   most 16 entries; record/count fields are signed int64; and the canonical serialized manifest
   row is at most 8192 bytes. The writer and reader enforce each limit and fail closed on excess
   before commit / return. The same 8192-byte limit applies to the public summary receipt. These
   are format bounds, not DQ-9 defaults. The input binding list is also limited to the finite
   table set declared by the report rule, not by report contents.
3. Bounded canonical-partition reports use
   `hlens.quality.canonical-partition@3.0.0`; bounded listing-history reports use
   `hlens.quality.listing-history@2.0.0`. Each new report ID includes its rule version/hash and
   exact pinned inputs, so a legacy report ID can never be mistaken for a v3 report. There is no
   fallback from a missing v3 manifest to a legacy row.
4. Add `quality.data_quality_report_manifests` after the existing Phase 1 table definitions;
   do not change any existing table schema, partition, or definition hash. The new table's
   definition hash and row schema are golden-registered before use.

### 2. Bounded evidence streams

Use the content-addressed, ordered, fixed-leaf/fan-out tree principles of ADR-0077, with an
independent format identifier `hlens.quality.report-jsonl@1.0.0` and explicit rule parameters.
No stream limit receives an implicit default while DQ-9 is open.

Canonical v3 reports continue to write and verify `quality.availability_evidence_gaps` as
required by ADR-0031; the `evidence_gaps` stream is its exact content commitment. Listing v2
reports retain their historical inline gaps; listing v2 bounded reports preserve every gap in
the stream because the canonical gap table's symbol/day columns do not represent listing-history
gaps.

The streams are:

| Stream | Record projection and order |
|---|---|
| `events` | Exactly `event_ordinal`, `event_id`, `event_type`, `table`, `observation_key`, `event_start`, `event_end`, `detail`, `revision_first_ordinal`, and `revision_count`; null optional values remain explicit nulls. For canonical v3, ordered by `(event_start null-first, table UTF-8 bytes null-first, observation_key UTF-8 bytes null-first, event_type UTF-8 bytes, event_id UTF-8 bytes)`; for listing v2, `observation_key` is the venue symbol and records are ordered by `(observation_key UTF-8 bytes, event_start, event_type UTF-8 bytes, event_id UTF-8 bytes)`. No `revision_ids` array is embedded. |
| `event_revisions` | Exactly `event_ordinal` and `revision_id`, ordered by event ordinal then revision ID in UTF-8 byte order. This preserves all historical revision identities without embedding an unbounded list in one event. |
| `evidence_gaps` | Exactly `table`, `revision_id`, and `gap`, ordered by `(table, revision_id)` in UTF-8 byte order. A duplicate identity is an integrity error. |

The record byte format is part of `hlens.quality.report-jsonl@1.0.0`: UTF-8 without BOM; one compact JSON object per line; object keys sorted by Unicode code point; no insignificant whitespace; non-ASCII scalar values are emitted directly as UTF-8. Strings escape `U+0022` as `\"`, `U+005C` as `\\`, backspace/tab/LF/form-feed/CR as `\b`, `\t`, `\n`, `\f`, `\r`, and every other `U+0000..U+001F` as lowercase `\u00xx`; `/`, `U+2028`, and `U+2029` are not escaped. JSON numbers are finite integers only (no float or Decimal); timestamps are UTC RFC 3339 with exactly six fractional digits and `Z`; each record ends in exactly one LF byte. Invalid Unicode scalars, non-finite or non-integer numeric values, unknown fields, and records over the rule's explicit byte limit fail closed. Hash inputs use the exact emitted record bytes with the terminal LF removed and a versioned domain separator; no platform newline or object insertion order participates.

For canonical v3, the expected QGAP projection preserves the ADR-0031 time-slice/table batch order and each batch's `revision_id` order. Each expected gap is assigned its exact expected `batch_index` while deriving those batches. For equality, the expected projection and pinned QGAP rows are externally sorted by `(batch_index numeric ascending, table UTF-8 byte order, revision_id UTF-8 byte order)` in UTF-8 byte order and merge-compared row by row. Compare `quality_report_id`, `table`, `revision_id`, `gap`, `subject_symbol`, `subject_start`, and `batch_index` exactly; no row may be omitted, duplicated, moved to another batch, or added. If the Iceberg scan does not produce this order, sort its projected rows through the bounded RunSet mechanism; neither side may be collected in memory. The report's public `evidence_gaps` stream drops the storage-only `batch_index` only after this equality check and has the canonical `(table, revision_id)` order. This merge proves the batch-index completeness seal without `combine_chunks`, a report-wide list/dict/set, or full materialization of `batch_index`; ADR-0031's existing v1/v2 implementation remains unchanged.

The QGAP scan projection is bounded by the explicit rule parameter `max_qgap_projection_record_bytes`, which covers the UTF-8 byte lengths of every projected string and the fixed-width encoding of `subject_start` and `batch_index`. The adapter must establish this bound from file/page metadata and nested offsets before decoding or materializing projected string values; applying the limit after constructing Python strings is insufficient. If the pinned snapshot format or adapter cannot establish the bound before decode, canonical v3 report verification is unsupported and fails closed without returning a report. This parameter has no implicit default while DQ-9 is open.

Leaves and index nodes are assembled under explicit byte/record/fan-out limits, SHA-256 addressed, staged with the expected digest, and published through `StorageAdapter`. Readers resolve each reference through `lookup`, check key/digest/size and tree structure, and expose only a closable ordered iterator. Revisions within an event remain lossless; an event is rejected if its fixed-size fields exceed the declared record limit.

For v3, `event_id` is exactly `event_type + "." + first_32_lowercase_hex(SHA-256(input))`. The input is the byte concatenation of ASCII domain separator `hlens.quality.report-event-id@1.0.0\0`, the canonical JSON bytes (without LF) of exactly `{rule_id, rule_version, rule_hash, event_type, table, observation_key, event_start, event_end, detail, revision_count}`, then each revision ID in UTF-8 byte order encoded as an unsigned 64-bit big-endian byte length followed by its UTF-8 bytes. `event_id`, `event_ordinal`, `revision_first_ordinal`, and arrival order are excluded. The fixed identity fields and complete sorted revision sequence are therefore bound without a self-reference or an inline list.

### 3. Commit, replay, and read semantics

1. For canonical v3, write and verify the `quality.availability_evidence_gaps` batches using
   ADR-0031's batch IDs, row equality, completeness seal, and no-delete semantics. Then publish all
   content-addressed stream objects. The single row in
   `quality.data_quality_report_manifests` is the unique v3 report reference and commit point,
   extending ADR-0031's legacy rule that the report row is the only reference. A QGAP batch without
   this v3 manifest is an unreferenced orphan and no report reader may return it. Listing v2
   bounded reports write no QGAP table rows; their complete gaps are committed by the manifest's
   `evidence_gaps` stream. A failed build may leave immutable orphan objects / unreferenced QGAP
   rows; writers do not delete them.
For a v3 report, every QGAP read is pinned to the exact `qgap_output_snapshot_id` named in the report manifest, not to a source/input binding or the current table head. Each expected batch is checked one at a time by batch ID and exact row comparison, with a maximum of 25,000 rows. The bounded sorted merge in §2 proves exact batch membership and completeness; readers must not use the current `_snapshots()` dictionary or materialize snapshot history. On an initial write, the writer records the QGAP table snapshot after all report batches have been verified and before committing the manifest. On retry after a manifest exists, it uses that manifest's recorded snapshot and performs comparison only; it does not append or choose a newer snapshot. If a write failed before the manifest commit, batch replay follows ADR-0031 idempotency, then the writer records the resulting verified snapshot in the new manifest. This v3 rule supersedes only the in-memory QGAP completeness implementation; ADR-0031 and the legacy report path remain unchanged for v1/v2.

The listing source cursor has explicit rule parameters for `max_data_file_bytes`, `max_data_file_uncompressed_bytes`, `max_scan_batch_bytes`, and `max_source_record_bytes`; none has an implicit default while DQ-9 is open. Its Iceberg adapter preflights each pinned file's physical byte size and the total uncompressed live-column-chunk size from Parquet/ORC metadata before decode, reads one bounded file/batch at a time, and rejects a file/page/record before materializing nested values if a limit is exceeded. The Raw `symbols` and listing `tradable_intervals` nested columns are exposed as child-value cursors over Arrow offsets, not converted with `to_pylist()` or `as_py()` for a complete list. The existing `scan_column_batches` API is not sufficient because its row limit does not bound row-group bytes. If the adapter cannot establish these bounds for a pinned snapshot, v2 report derivation fails closed and writes no manifest.

2. `existing_only` performs no writes and reads no clock. Canonical v3 re-derives each expected
   stream from bounded PIT output. Listing v2 uses a new `ListingDeriver.iter_quality_evidence`
   cursor over the same pinned listing and Raw snapshots; it must not call `verify()` or construct
   `ListingsVerified`. The cursor processes listing snapshot history oldest-to-newest by
   incrementally producing one snapshot descriptor at a time and spilling it into a RunSet; it
   must not call a source API that returns the complete history as a list/tuple before spilling.
   Equal-time observation ties are external-sort groups consumed incrementally; implementations
   must not collect a complete tie group with `list()`, `groupby()` prefetch, or equivalent. Any
   state retained while consuming a tie group must be bounded by the explicit record/byte limits.
   The cursor then verifies one snapshot at a time.
   For every snapshot it verifies the derivation batch against the named pinned Raw snapshot,
   checks the prior committed rows as an exact prefix ordered by contiguous `arrival_seq` starting
   at zero, checks new rows in existing batch order (venue symbol then chain transition order)
   with `arrival_seq = prior_row_count + ordinal`, and checks the batch fingerprint. Revision-keyed
   joins use bounded external sorts and compare every projected scalar and streamed interval child
   value; the committed revision index is carried forward as a RunSet root. Raw observations are
   externally sorted by `(venue_symbol, retrieved_at, snapshot_revision_id)` and fed to the same
   transition rules as `derive_chain`: equal-time ties are consumed as one unbounded but externally
   sorted group, unresolved observations produce the same findings and do not alter state, and
   resolved status transitions produce the same revision identities and episode intervals.
   Episode interval history and each finding's revision IDs are represented as bounded child
   streams, not Python tuples/lists or nested Arrow `List` values. The cursor emits report-input
   event, chain finding, divergence finding, revision, and gap records in separate ordered passes;
   finding order is `(venue_symbol, observed_at, code, event_id)`, and event identity binds the
   complete sorted revision stream. All Arrow scans use explicit batch-byte limits and project
   nested Raw symbols/listing intervals as child streams; a scan that cannot enforce those limits
   is unsupported and fails closed. The cursor externally sorts findings into the specified order,
   then compares metadata and records in ordinal order, checks counts and roots, and requires both
   sides to end together. Canonical v3 also merge-compares the `evidence_gaps` stream with all QGAP
   rows for the report, proving exact one-to-one equality and count. Any missing object, extra
   record, mismatch, duplicate manifest row, or inconsistent root fails closed.
3. A retry that finds the identical committed manifest is idempotent. A different manifest for
   the same report ID is `CatalogIntegrityError`; no overwrite or repair occurs. Orphan object
   replay is `already_present` when bytes match.
4. A v3 `QualityReported` exposes only the fixed-size manifest summary and commit receipt.
   Complete events and gaps are available only through explicit ordered readers. Legacy report
   APIs remain available for their recorded versions and retain their persisted row shape.
5. Dataset v3 verification binds the legacy report table, the v3 report-manifest table, and the
   QGAP table snapshots in its PIT/source bindings. It verifies a `DatasetQualityReportRef`
   against exactly one legacy row or one v3 manifest according to the report ID's registered rule
   version; both references, neither reference, or a version/row mismatch are integrity errors.
   A v3 report's events and gaps are replayed through the bounded readers; raw nested-list reads
   are not a supported consumer path.

For the v3 Dataset replay protocol, let `M` be the single report-manifest table snapshot bound by
the Dataset, `D` the single `quality.availability_evidence_gaps` table snapshot bound by the
Dataset, and `S_i` the `qgap_output_snapshot_id` in the manifest for referenced canonical report
`i`. These bindings have distinct roles and are not interchangeable:

1. Resolve each `DatasetQualityReportRef` only from the report-manifest rows visible at `M`.
   Require exactly one matching report manifest and read its `S_i`; do not resolve the report from
   the current report-manifest table head.
2. Require `S_i` and `D` to be snapshots in the same QGAP table lineage, with `S_i` equal to or
   an ancestor of `D`. At `S_i`, run that report's `existing_only` re-derivation using the report
   manifest's pinned input snapshots and compare its expected QGAP rows with the QGAP rows read at
   `S_i`, using the exact ADR-0031 projection and completeness rules. This operation is read-only
   and reads no clock.
3. Separately read that report's QGAP rows from the Dataset-bound snapshot `D`. Compare the full
   projected rows at `D` against the verified rows at `S_i` in bounded order, including
   `quality_report_id`, `table`, `revision_id`, `gap`, `subject_symbol`, `subject_start`, and
   `batch_index`; require equal counts and simultaneous exhaustion. This proves every row
   committed for the report is visible at `D` and that `D` contains no extra or changed row for
   that report. Other reports' rows visible at `D` are ignored by the exact `quality_report_id`
   filter.
4. Fail closed if `S_i` cannot be resolved, lineage or ancestry cannot be proven, a required
   snapshot is unreadable, the report has no rows visible at `D` when rows are expected, or any
   ordered comparison differs. When the expected gap stream is empty, the same zero-count check
   applies at both `S_i` and `D`.

Replay requires the report-manifest snapshot `M`, Dataset QGAP snapshot `D`, and every referenced
canonical report snapshot `S_i` to remain readable while reachable from a committed v3 report
manifest or Dataset manifest. Snapshot expiration / maintenance must not remove a reachable
snapshot; if the configured Catalog cannot retain or reopen these snapshots, replay fails closed.
This retention condition does not bound PyIceberg metadata history.

This cross-snapshot Dataset verifier protocol extends the Dataset verification rules in ADR-0077
§6.1.5 beyond the accepted ADR-0093 report-store base decision. It is a proposed amendment only:
the protocol must not be implemented or treated as an ADR-0077 compliance claim until a separate
ADR-0077 amendment is reviewed and accepted. This text does not change ADR-0077 or any core
contract.

### 4. Dependencies and non-claims

- The v3 reporters must derive their events from bounded PIT selection. Until PIT single-key
  graph validation and conflict-head output are bounded, a report implementation may not claim
  ADR-0077 §6.1.5 or E1-CAP-1 compliance.
- Listing findings, event revisions, and gaps must all be streamed. The v3 implementation must
  use the bounded `ListingDeriver.iter_quality_evidence` cursor defined in §3; its projections
  must match `ListingsVerified` exactly, including divergence findings and each finding's complete
  snapshot revision IDs. Keeping any full `verified.rows`, chain map, findings list, event list,
  gap list, head tuple, or report-row nested payload in the v3 path violates this decision. The
  legacy `verify()` path remains unchanged for v1 readers.
- This decision preserves report meaning and all evidence; it does not set an event-count cap,
  discard low-priority events, alter quality thresholds, or close DQ-9 / E1-CAP-1.
- Incremental snapshot-descriptor production does not bound PyIceberg's own `TableMetadata.snapshots`
  collection or total process RSS. As ADR-0077 §6.2.6 and §10 specify, PyIceberg metadata history
  remains an O(H) E1-CAP-1 measurement item; this amendment makes no whole-process boundedness or
  capacity claim.
- The old v1/v2 report path is explicitly materializing and is outside E1-CAP-1. v3 Dataset
  construction may not silently consume it for a new bounded manifest.

## Alternatives considered

| Option | Decision |
|---|---|
| Add versioned content-addressed event/revision/gap streams and a fixed-size report manifest | **Accepted.** Retains complete evidence and provides a bounded read/write shape. |
| Impose a maximum event count and reject larger reports | Rejected: no existing market-data contract supplies that limit; it would change report coverage. |
| Add an iterator over the existing `List<Struct>` row | Rejected: the nested list is decoded as one field before iteration. |
| Keep the existing schema and leave v3 quality reports outside the bounded claim | Rejected as the target implementation because ADR-0077 requires the report verification path to be bounded. |

## Consequences and implementation gates

- Add one production table and an object-stream format; the legacy report table remains byte-for-
  byte compatible.
- Update the Phase 1 registry, golden table-definition tests, report ID registration, and
  Dataset v3 source bindings before enabling writes.
- Update readers, Dataset verification, tools, and tests to distinguish fixed summaries from
  complete event streams. Do not claim this ADR is implemented until restart/replay, tamper,
  missing-root, orphan, reader-close, high-cardinality, and cross-version tests pass.
- E1-CAP-1 must include the report generation and full event/revision/gap outputs in its measured
  working set; passing this design or focused tests is not capacity evidence.

## Decision record

Codex selected the lossless streaming design because it is the only option that satisfies the
bounded-workset requirement without changing report coverage or weakening fail-closed validation.
The choice is additive, keeps all persisted legacy identities readable, and places a fixed-size
manifest row at the commit boundary. Implementation and independent review remain required.
