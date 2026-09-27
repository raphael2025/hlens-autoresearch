# P11 degradation provenance UI implementation note

**Phase:** 11 — Continuous Research Loop
**Decision:** Accepted ADR-0067
**Status:** `CODE_COMPLETE / DEBUG_PENDING`; Phase 11 remains unaccepted.

## Scope

The degradation report detail page now renders the known provenance fields from schema `1.1.0` reports: Profile freeze record and anchor snapshot, baseline report and gate mapping, lifecycle history hash, explicit observation window and method, observation set identity, and the complete recent-observation manifest (source identifiers / hashes / event and observation times, plus metric values).

The page labels the evidence as caller-declared and content-bound. It explicitly says that hash binding does not authenticate external sources, source IDs, or aggregation truth. A disclosure also renders the complete `evidence` JSON as text, so additive unknown fields remain inspectable without being interpreted as markup. Known fields with an unexpected shape are omitted from structured rows and remain visible in that raw disclosure.

Legacy `1.0.0` reports without evidence keep their existing rendering. The UI does not recompute hashes, resolve source IDs, or make claims about lifecycle recency or source authenticity. No API, report schema, or domain contract changed.

## Files

- `apps/web/src/lib/degradationCheck.ts` — known evidence fields are described while preserving an open index signature for additive fields.
- `apps/web/src/pages/DegradationChecks.tsx` — read-only evidence sections and safely escaped raw JSON disclosure.

## Verification

- Tests and build were not run, as requested.
- `git diff --check` is the only requested static check for this slice.
