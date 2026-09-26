# infrastructure/registry

File-backed, append-only **Strategy Registry** and golden blob store (ADR-0005 §1 / §4 / §7).
Status: **CODE_COMPLETE / DEBUG_PENDING** (2026-09-26). No contract, Schema or lifecycle change.

| Module | Role |
|---|---|
| `registry.py` | `StrategyRegistry(root, anchor=None)`: hash-chained journal (`registry.jsonl`, the shared on-disk contract of `infrastructure.event_bus.journal`), records `artifact.registered` / `equivalence.recorded` / `deployment.recorded`; single writer (`.lock`) |
| `blobs.py` | `BlobStore`: write-once, SHA-256-named canonical-JSON blobs (`registry-blob:sha256:<hex>`) |
| `golden.py` | The golden payload encoding shared by the packer (`research.promotion`) and the Equivalence Gate (`apps.promotion`) |
| `profile_freeze.py` | `ProfileFreezeRegistry(root, anchor=...)` (ADR-0062, Proposed; B56): the append-only record that a **named** approver approved freezing one exact Validation Profile (ref + content hash) on one calibration report (original bytes stored write-once, kind / self-hash / `provenance.calibration_report` verified); the external anchor is **mandatory**; Promotion's authoritative freeze source |

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

## Profile freeze registry (ADR-0062)

- Separate directory (`freezes.jsonl`, `blobs/`, `.lock`) and a **mandatory** anchor outside it — without an
  anchor, dropped trailing records are undetectable, so an unanchored registry cannot back Promotion.
- One record type `profile.frozen` with exact keys (`format_version` 1.0.0, Profile JSON / ref / hash, calibration
  kind / report_hash / sha256 / uri, `approved_by`, UTC `approved_at`, `freeze_id`); the same rules on append and
  replay (calibration blobs are re-verified on replay); duplicate → `DuplicateRecord`, same ref under another hash →
  `FreezeConflict`; on open any broken rule → `RegistryCorrupted`. No edit, delete, unfreeze or supersede.
- Honest boundary: `approved_by` is a declared name (no OS / identity authentication); not the production Control
  Plane; grants no live / funds / deployment authority; freezes no Profile value (D-09 stays Raphael's). Rolling the
  anchor back together with the registry is still undetectable.

Tests: `tests/promotion/test_strategy_registry.py`, `tests/promotion/test_profile_freeze_registry.py`.
