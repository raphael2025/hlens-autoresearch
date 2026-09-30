# infrastructure/registry

File-backed, append-only **Strategy Registry** and golden blob store (ADR-0005 §1 / §4 / §7).
Status: **CODE_COMPLETE / DEBUG_PENDING** (2026-09-26). No contract, Schema or lifecycle change.

| Module | Role |
|---|---|
| `registry.py` | `StrategyRegistry(root, anchor=None)`: hash-chained journal (`registry.jsonl`, the shared on-disk contract of `infrastructure.event_bus.journal`), records `artifact.registered` / `equivalence.recorded` / `deployment.recorded`; single writer (`.lock`) |
| `blobs.py` | `BlobStore`: write-once, SHA-256-named canonical-JSON blobs (`registry-blob:sha256:<hex>`) |
| `golden.py` | The golden payload encoding shared by the packer (`research.promotion`) and the Equivalence Gate (`apps.promotion`) |
| `profile_freeze.py` | `ProfileFreezeRegistry(root, anchor=...)` (ADR-0062, Proposed; B56): the append-only record that a **named** approver approved freezing one exact Validation Profile (ref + content hash) on one calibration report (original bytes stored write-once, kind / self-hash / `provenance.calibration_report` verified); the external anchor is **mandatory**; Promotion's authoritative freeze source |
| `retirement.py` | `RetirementRegistry(root, anchor=None)` (ADR-0086 决策 2): the append-only store for `core.domain.research.RetirementRecord` (RETIRED, distinct from Failure Registry's REJECTED / FAILED); same subject (`Ref.target_identity()`) retired twice → `DuplicateRecord`; anchor **optional**; no global singleton — the caller (e.g. `research.evolution.operators.retire`'s caller) opens the registry and calls `register_retirement` explicitly |
| `lifecycle.py` | `LifecycleRegistry(root, anchor=None)` (ADR-0098 §1): the lifecycle authority — an append-only, hash-chained journal of `core.lifecycle.strategy.LifecycleTransition`s; `append(transition, expected_head=...)` (optimistic concurrency), per-strategy replay, `active_set(head)`, pinned-head reads; anchor **optional**; read-only `verify_integrity_snapshot` |

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

## Retirement registry (ADR-0086 决策 2)

- Separate directory (`retirements.jsonl`, `.lock`); anchor **optional** (outside `root`), unlike the Profile
  freeze registry — without it, dropped trailing records are undetectable, but the hash chain still catches
  tampering and a crash mid-write on every open.
- One record type `retirement.recorded` with exact keys (`format_version` 1.0.0, `record` = the
  `RetirementRecord`'s own JSON, `record_id` = its content hash); same rules on append and replay; "same object"
  (i.e. same `subject_ref.target_identity()`) retired twice → `DuplicateRecord`, nothing written; on open any
  broken rule → `RegistryCorrupted`. No edit, delete or un-retire operation.
- `RetirementRecord` is `core.domain.research.RetirementRecord`; `research.evolution.operators.retire` only
  constructs it — this registry is what a caller explicitly opens and writes it to (no global singleton).
- Reuses `infrastructure.event_bus.journal.AppendOnlyJournal`, not `research.persistence.journal` (ADR-0086
  决策 2 names the latter, but `infrastructure/` must not import `research/` —
  `tests/test_architecture_boundaries.py::test_plugins_and_infrastructure_do_not_import_research`; see
  `retirement.py`'s module docstring for the full placement note).

Tests: `tests/infrastructure/registry/test_retirement.py`.

## Lifecycle registry (ADR-0098 §1)

- Separate directory (`lifecycle.jsonl`, `.lock`); anchor **optional** (outside `root`), as for the retirement registry.
- One record type `lifecycle.transition_recorded` with exact keys (`format_version` 1.0.0, `record_index` = journal
  `seq`, `prev_record_hash` = journal `prev_hash`, `strategy` = `str(transition.subject)`, `transition` = the
  `LifecycleTransition`'s own JSON, `transition_hash` = its content hash). A record's hash is its journal entry hash.
- Same rules on append and replay: the transition must start from the strategy's replayed state (same
  `Ref.target_identity()`; `IDEA` with no record), pass `validate_transition`, and the strategy's whole history must
  re-validate as a `LifecycleHistory`. On open any broken rule → `RegistryCorrupted`.
- `append` takes `expected_head` (a `LifecycleHead(record_count, last_record_hash)`; empty = `(0, GENESIS_HASH)`); any
  other current head → `HeadMismatch`, nothing written.
- Reads take an explicit head (`head` = latest, or a pinned one; `head_for_hash` resolves a record hash). A head not on
  the chain → `UnknownHead`. `lifecycle_of(subject, head)` replays one strategy; `active_set(head)` = every strategy
  whose replay ends in `ACTIVE` (later `DEGRADED` / `RETIRED` removes it).
- Writers are explicit calls only; the research loop, the ADR-0074 operator and the API never write it. Backfill means
  appending the real history record by record. `triggered_by` / `approved_by` are declared names (not authenticated).

Tests: `tests/infrastructure/registry/test_lifecycle.py` (not yet run; DEBUG_PENDING).
