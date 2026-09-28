# ADR-0081: Report payload DTO versions and compatibility

| Field | Value |
|---|---|
| Status | **Accepted** (2026-09-28) |
| Date | 2026-09-28 |
| Decision maker | Codex, decided under Raphael's 2026-09-28 authorization |
| Phase | Apps / shared reports |
| Scope | `research/reports/**`, `apps/api/**`, `apps/web/src/**` |
| Compatibility | Additive DTO registry; known legacy payload versions remain readable |

## Context

ADR-0048 established a generic read-only envelope whose `payload` is a JSON object. The report
registry has since grown to eleven kinds. Some payloads already carry `schema_version`; others
pre-date that field, and each page has accumulated separate structural guards. ADR-0079 also
raised `paper_deviation` to payload schema 2.0.0, but the API did not have a central per-kind version
registry and the Web parser did not require the scope-bound 2.0.0 shape.

The DTO boundary belongs to `research/reports` and the Apps plane. No Domain Contract, frozen
contract, Constitution, Validation Profile value, validation threshold, or write endpoint changes.

## Decision

1. Every report kind has a versioned payload DTO registration: baseline version, versions the
   consumer knows, and required top-level fields. Existing payload `schema_version` values are
   authoritative. For kinds whose historic payload has no `schema_version`, the registered
   baseline is a **virtual DTO version**: readers use it for validation and display but never add it
   to stored JSON or content identity.
2. API resolves the DTO version before version-specific identity rules. A malformed payload on a
   known version is invalid (listed under `invalid`; detail returns 422). A version not in the
   registration is **opaque read-only pass-through**: API computes the envelope `content_hash` but
   does not assume the current kind-specific hash rule; Web displays raw JSON with an
   unknown-version notice. It is never interpreted as a supported typed view. This preserves
   forward read access without treating unknown data as evidence.
3. Web uses per-kind DTO guards before rendering kind-specific pages. Unknown versions and malformed
   known DTOs render as raw JSON with a notice. No consumer writes, migrates, or normalizes a report.
4. Compatibility registrations are:

   | Kind | DTO versions | Baseline / notes |
   |---|---|---|
   | `validation_report` | 2.0.0, 2.1.0, 2.2.0 | Current contract schema is 2.2.0; legacy 2.0.0 / 2.1.0 remain readable |
   | `research_loop_round` | virtual 1.0.0 | Flattened ADR-0050 audit payload |
   | `state_strategy_matrix` | virtual 1.0.0 | Hash remains opaque to API; DTO checks shape |
   | `router_paper_run` | virtual 1.0.0 | Optional evidence fields remain additive |
   | `gate_calibration` | 1.0.0 | Evidence only |
   | `router_stop` | virtual 1.0.0 | Optional eligibility evidence remains additive |
   | `state_diagnostics` | 1.0.0, 1.1.0 | 1.1.0 may carry `source_result_hash` |
   | `event_statistics` | 1.0.0 | Existing payload version |
   | `paper_deviation` | 1.0.0, 2.0.0 | 1.0.0 legacy descriptive only; 2.0.0 is scope-bound per ADR-0079 |
   | `degradation_check` | 1.0.0, 1.1.0 | 1.1.0 carries operation evidence |
   | `retro_audit` | 1.0.0, 1.1.0 | The checked-in 1.0.0 fixture remains readable; current writer is 1.1.0 |

5. `paper_deviation` writer must emit `schema_version=2.0.0`; API additionally verifies the
   declared scope DTO version 1.0.0 and recomputes its nested `scope_hash`, on top of the existing
   outer `deviation_hash` check. Web requires the scope fields and hash syntax before using its
   typed 2.0.0 view. Legacy 1.0.0 remains displayable as descriptive legacy material; any consumer
   claiming ADR-0079 scope compliance must still require 2.0.0 and perform the full scope checks.

6. Additive fields within a supported major version may be ignored by older DTO readers. Removing,
   renaming, changing meaning, or changing required field types requires a major version and ADR.
   A DTO upgrade never rewrites existing files; fixture bytes and their content-addressed ids change
   only when the writer actually emits changed payload bytes.

## Consequences

- API and Web now share an explicit inventory of versions and required fields while `apps/api` still
  does not import `research/`.
- Existing legacy payloads are retained and readable. Unsupported future versions remain available
  as raw data, but no typed page treats them as understood.
- `paper_deviation` 2.0.0 is checked consistently by source writer, API registry, and Web consumer;
  its schema change does not alter deviation calculations or scope policy.
- No API write or research-trigger path is added.

## Verification boundary

No test, typecheck, lint, fixture writer, or build is run in this implementation task, per the
W2-DTO packet. The existing `apps/web/fixtures/**` JSON remains untouched. The fixture regeneration
list is: add a current 2.0.0 output under `paper_deviation/` and update its id registry, retaining
the two committed 1.0.0 legacy reports; regenerate the stale `retro_audit/` 1.0.0 fixture to match
its existing 1.1.0 writer and update its id registry (AUD-1 M2). No fixture JSON or id registry is
changed in this task, and no other writer's payload bytes change here.
