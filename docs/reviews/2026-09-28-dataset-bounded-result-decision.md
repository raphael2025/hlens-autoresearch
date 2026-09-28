# Decision packet: bounded Dataset selection results

**Status:** Raphael approved Option B on 2026-09-28: draft an ADR and implement the bounded contract. ADR-0077 is being drafted. No contract or code change is made in this packet itself.

## Question

Should HLENS add a bounded Dataset selection / build path that changes how complete rows and lineage are returned or persisted?

## Evidence

- `DatasetSelection.rows` and `.lineage` are complete tuples; `DatasetBuilt` retains the selection.
- `ResearchDatasetManifest.lineage` is also a complete tuple in frozen `core/contracts/universe.py`.
- `DatasetBuilder.build()` and `verify_manifest()` materialize and compare complete Arrow tables / expected rows.
- Moving only `seen_keys` to SQLite or adding an iterator leaves the full returned DTO and lineage as O(N). Temporary files also count in the complete working set and can be memory-backed.
- Removing or weakening duplicate, lineage, or manifest completeness checks is not acceptable.

## Options

**A. Preserve the frozen API.** Keep the current tuple results and integrity checks. Record Dataset selection as materializing and do not claim it is bounded. This avoids a contract migration, but it does not satisfy a bounded-workset requirement for this path.

**B. Design a new bounded API and persistence representation.** Draft an ADR for chunked selection / build consumption, a bounded lineage representation or externally referenced canonical lineage, integrity verification, and legacy API compatibility. Do not change `core/` or production behavior until Raphael explicitly approves the ADR and contract versioning.

## Decision

Option B. Preserve exact lineage, duplicate rejection, manifest completeness, and row verification while replacing default all-in-memory results with a bounded representation. The ADR must define how complete evidence remains verifiable without returning or retaining O(N) tuples, and how persisted v2 manifests remain readable. Do not implement a cosmetic spill-only change.

## Impact and boundary

This touches `DatasetSelection`, `DatasetBuilt`, and `ResearchDatasetManifest` semantics. `AGENTS.md` / `CLAUDE.md` require an ADR and Raphael approval for frozen Domain Contract changes; approval has now been given for the ADR and implementation route. Exact artifact, schema, and version decisions must be recorded in ADR-0077 before contract edits.

## Codex review outcome (2026-09-28)

ADR-0077 is **BLOCKED** at DQ-1. The bounded evidence manifest must be a typed Research Dataset
contract to satisfy ADR-0023 §6, and the compatible route adds a model and schema version in
`core/contracts/universe.py`. This task packet explicitly prohibits changing `core/` and requires
a Raphael decision before any frozen contract change. No Dataset runtime code or table definition
was changed. The v2 manifest read-only path remains intact.

DQ-2 through DQ-8 and DQ-10 through DQ-12 are recorded as conservative design decisions in
ADR-0077; DQ-9 remains open until capacity evidence can justify resource constants. These
decisions do not authorize implementation while DQ-1 is blocked.
