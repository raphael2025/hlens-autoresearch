# infrastructure/registry

File-backed, append-only **Strategy Registry** and golden blob store (ADR-0005 §1 / §4 / §7).
Status: **CODE_COMPLETE / DEBUG_PENDING** (2026-09-26). No contract, Schema or lifecycle change.

| Module | Role |
|---|---|
| `registry.py` | `StrategyRegistry(root, anchor=None)`: hash-chained journal (`registry.jsonl`, the shared on-disk contract of `infrastructure.event_bus.journal`), records `artifact.registered` / `equivalence.recorded` / `deployment.recorded`; single writer (`.lock`) |
| `blobs.py` | `BlobStore`: write-once, SHA-256-named canonical-JSON blobs (`registry-blob:sha256:<hex>`) |
| `golden.py` | The golden payload encoding shared by the packer (`research.promotion`) and the Equivalence Gate (`apps.promotion`) |

## Rules (fail closed)

- Append-only: there is no edit, delete or overwrite operation.
- The same rules run on append and on replay: duplicate identity, dangling reference, unknown record type,
  extra / missing keys, a payload that is not its contract, a recorded identity that is not the content
  hash → refused (`DuplicateRecord` / `UnknownArtifact` / `RegistryRefused`); on open the registry is
  `RegistryCorrupted` and cannot be used.
- An artifact is registered only when its two golden blobs are stored, hash to the bound hashes, decode,
  answer the artifact's own strategy / spec hash, and the positions answer the requests in order.
- Tampering, a broken chain, a partial trailing line or a shrunken file → `RegistryCorrupted`.
- Whole trailing lines removed: undetectable without `anchor=` (a hash-chained `(length, head)` journal
  **outside** `root`). With the anchor, a shorter registry or a different history is refused; one extra
  record (crash between the record and its anchor line) is re-anchored.

## Placement

Control Plane storage belongs to Infrastructure (01-system.md §3–§4); it imports only `core` and
`infrastructure`, never `research` or `apps` (`tests/promotion/test_promotion_boundaries.py`).
It is a stand-in for the PostgreSQL + object-storage registry of ADR-0005 §7 (D-01 / D-02 open).
Keep `root` outside Git (tests use `tmp_path`).

Tests: `tests/promotion/test_strategy_registry.py`.
