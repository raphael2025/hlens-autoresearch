# Codex decision: ADR-0055 knowledge tags and asset retrieval

Date: 2026-09-26

Decision authority: Raphael delegated project architecture and technical decisions to Codex; implementation remains with Claude.

## Decision

Accept the **capability direction** in ADR-0055: reviewed knowledge items may carry topic tags and canonical asset identifiers, and queries may filter by all requested tags and any exact-matching asset. Keep `KnowledgeStatus` limited to research-claim evidence status. Keep the reviewed-entry boundary: no new knowledge-write API, no unreviewed LLM content in the local knowledge set.

Do **not** implement the current ADR text as written. Its version and dependency statements are stale. Claude must amend ADR-0055 and the related status / plan documents before changing the frozen contract, then implement the amended decision.

## Required amendment before implementation

1. The current domain contract is `2.1.0`, and `docs/architecture/02-domain.md` lists both `2.0.0` and `2.1.0` as published. Add `tags` and `assets` to `KnowledgeItem` and `tags_all` and `assets_any` to `KnowledgeQuery` as fields introduced in **2.2.0**, using `_FIELDS_SINCE` and the project's published-version rules. Do not leave the ADR claiming `CONTRACT_SCHEMA_VERSION` is `2.0.0`, and do not place these new fields in a `2.1.0` envelope.
2. Raise the declared Pydantic minimum from `>=2.9` to `>=2.12` if using `Field(exclude_if=...)`. `uv.lock` currently resolves 2.13.5, but the package declaration must not promise support for versions that lack the required serialization behavior. The local Pydantic 2.13.5 check confirmed `exclude_if` omits an empty tuple and serializes a non-empty tuple.
3. Preserve legacy semantics deliberately. A parsed 2.1.0 item/query with no new fields must retain its 2.1.0 envelope and reproduce its old content/query hash. A 2.1.0 payload containing a 2.2.0 field must fail closed. New current-version records use 2.2.0; their changed envelope hashes are expected and must be called out, not described as unchanged.
4. Require tests for the version boundary and the full hash chain: 2.1.0 fixtures and hashes remain unchanged; 2.2.0 tagged items and filtered queries hash deterministically; `KnowledgeResult.result_hash` changes with query or item metadata; exact asset matching does not substring-match; invalid, duplicate, or non-canonical tags/assets are rejected; empty filters preserve the historical payload shape where the serialized model version is held constant.
5. Update Schema snapshots, version registries, ADR index, domain-version docs, knowledge-base docs, and the Phase 0.5 acceptance matrix together. Seed metadata may be added only as reviewed data with rationale; do not imply that asset tags are validated historical listings or market data.

## Rationale and evidence

- Phase 0.5 roadmap acceptance explicitly requires retrieval by tag, status, and asset. ADR-0034 already covers status; `KnowledgeItem` and `KnowledgeQuery` currently lack structured tag / asset fields and filters.
- The current ADR's compatibility claim is not sufficient for the published-contract rules. In this repository, `schema_version` participates in content hashes; preserving empty-field serialization does not make a new 2.2.0 object hash-identical to an old 2.1.0 object.
- `pyproject.toml` currently says `pydantic>=2.9`, while the ADR relies on `exclude_if` and cites 2.12 as its minimum. The lock resolves Pydantic 2.13.5, which proves the current environment only; it does not make the declared lower bound valid.
- The proposed retrieval semantics are deterministic and fail closed without changing the Validation Constitution or adding a network / write capability.

## Scope boundaries

- This decision does not accept D-LIST / ADR-0051 and does not authorize `exchangeInfo` or any other external request. Raphael's explicit deferral remains controlling.
- This decision does not freeze Profile values, select a Strategy promotion, authorize a trading endpoint, merge `phase/1` or `main`, or create a release tag.
- ADR-0055 remains `Proposed` until Claude applies the amendments and the schema/hash compatibility tests pass. The capability direction is decided; implementation evidence is still required before Phase 0.5 can be called complete.

## Current documentation conflict

The latest pushed `wip/all-code-completion` (`02aacb7`) still says in `PROJECT_STATUS.md` §7 / §8 and the completion plan §10.6 that ADR-0055 awaits Raphael's decision. That status is superseded by this Codex decision under Raphael's delegation. The next documentation update must say: **capability direction decided by Codex; ADR amendment, version-boundary tests, and implementation remain outstanding**. Keep the ADR itself Proposed until those artifacts are complete; do not treat this status correction as permission to write contract code before the ADR amendment is committed.
