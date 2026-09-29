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
   key/SHA-256/size. The input binding list is limited to the finite table set declared by the
   report rule, not by report contents.
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

The streams are:

| Stream | Records and order |
|---|---|
| `events` | One fixed-size event record in deterministic report order. It has no `revision_ids` array; it stores a contiguous `revision_first_ordinal` and `revision_count`. |
| `event_revisions` | One `(event_ordinal, revision_id)` per referenced revision, ordered by event ordinal then revision ID. This preserves all historical revision identities without embedding an unbounded list in one event. |
| `evidence_gaps` | One fixed-size gap record per gap, in the versioned canonical order for that report type. |

Each record uses a single documented canonical UTF-8 JSONL projection. Leaves and index nodes are
assembled under explicit byte/record/fan-out limits, SHA-256 addressed, staged with the expected
digest, and published through `StorageAdapter`. Readers resolve each reference through `lookup`,
check key/digest/size and tree structure, and expose only a closable ordered iterator. Revisions
within an event remain lossless; an event is rejected if the rule's fixed-size event fields
themselves exceed the declared record limit.

For v3, `event_id` is derived by a versioned streaming digest over the rule identity, fixed event
fields, and the complete ordered revision-ID sequence. The digest input encoding and domain
separator are part of each report rule specification. It must not depend on Python object order,
arrival order, or a truncated head list.

### 3. Commit, replay, and read semantics

1. Publish all content-addressed stream objects first. The single row in
   `quality.data_quality_report_manifests` is the report's commit point. No consumer may treat
   stream objects without that row as a report. A failed build may leave immutable orphan objects;
   writers do not delete them.
2. `existing_only` performs no writes and reads no clock. It re-derives fixed metadata and each
   expected stream, then compares the expected and stored records in order, checks counts and
   roots, and requires both streams to end together. Any missing object, extra record, mismatch,
   duplicate manifest row, or inconsistent root fails closed.
3. A retry that finds the identical committed manifest is idempotent. A different manifest for
   the same report ID is `CatalogIntegrityError`; no overwrite or repair occurs. Orphan object
   replay is `already_present` when bytes match.
4. A v3 `QualityReported` exposes only the fixed-size manifest summary and commit receipt.
   Complete events and gaps are available only through explicit ordered readers. Legacy report
   APIs remain available for their recorded versions and retain their persisted row shape.
5. Dataset v3 verification binds the manifest table snapshot in its PIT/source bindings. It
   verifies a `DatasetQualityReportRef` against exactly one legacy row or one v3 manifest
   according to the report ID's registered rule version. A v3 report's events and gaps are
   replayed through the bounded readers; raw table scans are not a supported consumer path.

### 4. Dependencies and non-claims

- The v3 reporters must derive their events from bounded PIT selection. Until PIT single-key
  graph validation and conflict-head output are bounded, a report implementation may not claim
  ADR-0077 §6.1.5 or E1-CAP-1 compliance.
- Listing findings, event revisions, and gaps must all be streamed. Keeping any full
  `verified.rows`, event list, gap list, head tuple, or report-row nested payload in the v3 path
  violates this decision.
- This decision preserves report meaning and all evidence; it does not set an event-count cap,
  discard low-priority events, alter quality thresholds, or close DQ-9 / E1-CAP-1.
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
