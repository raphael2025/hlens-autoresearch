# ADR-0097: PIT bounded graph validation with an invocation-scoped SQLite scratch index

| Field | Value |
|---|---|
| Status | **Accepted (2026-09-30, Codex under Raphael's current project PM authorization)** |
| Related Phase | Phase 1 — Market Representation / E1-CAP-1 |
| Scope | `infrastructure/pit/`; no `core/` or public catalog protocol changes |
| Compatibility | PIT v2 and persisted replay unchanged; PIT v3 continues to emit the complete ADR-0094 conflict-head stream |

## Context

ADR-0077 requires bounded Dataset and PIT processing. ADR-0094 fixes the v3 conflict-head result shape, but does not choose the mutable index used for per-key graph validation and reachability. The current RunSet prototype avoids graph-sized Python maps, but its repeated frontier-by-edge-run scans can cause excessive long-chain I/O. A mutable adjacency index is needed to preserve graph checks without retaining O(V+E) Python objects.

The separately accepted D-E1-CANONICAL-SCRATCH decision assigns an explicit local scratch root through `Settings` and composition roots. This decision reuses that owner and path. It does not add lifecycle operations to the frozen `StorageAdapter` or create a general-purpose storage provider.

The locked PyIceberg 0.12.0 metadata model represents snapshots as a Python list, and its `snapshot_by_id` lookup walks that list. This is relevant to later complete-process capacity measurement, but does not change the graph-index decision. Upstream source: [PyIceberg 0.12.0 `TableMetadata`](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/pyiceberg/table/metadata.py#L1872-L1905) and [`snapshot_by_id`](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/pyiceberg/table/metadata.py#L1963-L1968).

## Decision

Use a per-invocation SQLite index under the caller-supplied `canonical_scratch_directory` for bounded PIT graph validation and reachability.

1. The composition root must supply the configured scratch directory. No fallback to `tempfile`, `TMPDIR`, or a default process directory is allowed. The current consolidation line carries the accepted D-E1-CANONICAL-SCRATCH wiring; this ADR does not authorize a fallback or a `StorageAdapter` change.
2. Each invocation owns a uniquely created child directory and database. The SQLite connection must disable mmap and use an explicit fixed page-cache limit. Query results are consumed with bounded fetches. Adjacency, reverse adjacency, graph proof, and frontier operations must use explicit indexes; production queries must not create an unbounded Python result, CTE, or sort buffer.
3. The index must retain the invariants currently enforced by the graph validator: unique revision and arrival identities, payload uniqueness, ownership and claim consistency, declared-edge/evidence matching, cutoff-time eligibility, and cycle rejection. Reachability must include intermediate revisions that are not PIT candidates. V3 heads are emitted in complete canonical order to the ADR-0094 stream; no truncation is permitted. V2 behavior remains unchanged.
4. Graph construction must occur once per key invocation. Each cutoff evaluation may consult the SQLite indexes, but the implementation must not rescan the full edge run once per frontier node. Runtime complexity is not declared accepted by this ADR; long-chain and repeated-cutoff cost must be measured and reported.
5. Normal completion, exceptions, and explicit iterator close must close cursors and the database before removing the invocation's own directory. An abrupt process termination may leave an orphan. The system must not auto-resume or auto-delete an orphan; retain it for explicit operator inspection and cleanup. The ownership marker must let an operator distinguish this invocation's files from unrelated scratch data.
6. Scratch disk use is O(V+E) for the invocation and is not given a fixed byte quota by this ADR. Filesystem exhaustion fails closed. SQLite page-cache memory is fixed; the database, journal, file cache, and configured filesystem remain included in the later full-process/cgroup capacity measurement.
7. Do not add methods to `StorageAdapter`, alter contracts or ADR-0077/0094, change v2 replay, or claim E1-CAP-1 from this implementation decision.

## Alternatives

- **Immutable RunSet scans only:** rejected for the primary implementation because repeated full edge scans can cause O(V·E) external I/O on long chains.
- **A generic mutable scratch provider:** deferred. The already accepted explicit path owner is sufficient for this single infrastructure consumer; introduce a reusable provider only when another consumer requires the same lifecycle.
- **In-memory graph maps:** rejected because their retained Python objects grow with V and E.

## Required implementation evidence

- Structural tests inspect deep object graphs and prove Python containers/cursors stay within configured row/page bounds as V and E grow.
- Direct graph tests cover duplicate IDs/arrival sequences/payloads, ownership, mismatched or late evidence, cycles, unavailable intermediate nodes, stable ordered heads, conflicts, early close, exceptions, and orphan-marker ownership.
- Long-chain, wide-DAG, and repeated-cutoff diagnostics report query count, visited nodes/edges, database bytes, runtime, SQLite cache settings, RSS/cgroup sampling, and scratch filesystem.
- Run PIT and Dataset caller regressions on the integration line. These are slice checks only; the full E1-CAP-1 matrix remains a separate gate.

## Consequences

The graph's mutable lookup state moves from Python containers to caller-owned scratch. The approach adds local disk I/O and leaves crash orphans for explicit handling. It does not bound PyIceberg metadata, Arrow batches/row groups, other pipeline stages, or total process RSS; E1-CAP-1 remains open until the full accepted 32 MiB measurement passes.
