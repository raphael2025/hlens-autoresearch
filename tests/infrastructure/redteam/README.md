# G2 cross-stage red-team suite (Phase 1)

Composite attacks on the whole chain: D0/D1/D2 archives, D3D/D3E REST, E1 normalizer, D-33
reconciler, E2 listings, E3 reports, F1 PIT, F2 universe, F3 dataset + manifest, F4 features.
Each test runs the real stages on one tiny SQLite world (`dataset_support.World`); only transports,
clocks and the crash / tamper injection points are test code. Component attacks already covered by
the per-stage suites are not repeated here.

Every refused attack also asserts that the dataset's own tables (`research.dataset_selections`,
`research.dataset_manifests`) keep their heads: no partial success state.

An attack that **succeeds** is a strict `xfail` (`raises=` pins the current failure mode) named by
its finding id `RT-n`: fixing the defect turns it into XPASS, which fails the run until the marker
is removed.

| Module | Attack | Refused by |
|---|---|---|
| `test_rt_time_units` | ms ticks in a 2025-01-01 archive; μs ticks in a 2023 archive; a trade of the neighbouring day on either side of the switch | D1 parser (`ArchiveRejected`, nothing in Raw); F3 has no Canonical snapshot to bind |
| | sub-millisecond archive trade vs its ms REST copy | D-33 reconciler writes no edge → F1 conflict → F3 `PitConflictError` |
| | μs archive + ms REST copy on the switch day (control) | builds one archive head per key |
| `test_rt_replacement` | archive replaced (same path, new checksum) after a build | old manifest replays bit-identically; new build F1 `PitConflictError` |
| | original / replacement in either arrival order | F1 `PitConflictError` |
| | ADR-0032 assumption bound + replacement | F1 `PitConflictError` (both archive revisions become candidates) |
| | replacement in Raw, not yet normalized | E3 `RawNotDerived` (fixed RT-2, G2-R1a) |
| `test_rt_arrival_orders` | all 16 lawful orders of ingest / normalize / reconcile | none needed: identical rows and manifest shape in every order |
| | REST first, archive later (and the reverse), builds in between | F1 `PitConflictError` until the D-33 edge exists |
| | manifest built before the first D-33 edge, rebuilt after it | the rebuild replays (RT-5 fixed: a materialized selection is judged at its own build); a new build omitting the edges: F3 `DatasetSpecError` |
| `test_rt_crash_points` | process death after each of the 19 commits of a full run, then rerun | every stage's idempotent recovery; no manifest while dead; orphan selection batch adopted |
| | archive rows stopped half-way, read by E1 | E1 (`not exactly the N lines of its object`) |
| | Canonical unit stopped half-way, read by E3 / F3 | E3 `RawNotDerived`; F1 `CanonicalUnitIncomplete` (fixed RT-1, G2-R1a) |
| | REST elements stopped half-way, read by E1 / E3 / F3 | E1 `CanonicalUnitIncomplete`, nothing committed (fixed RT-3, G2-R1a) |
| | REST response committed, died before its first element, read by E3 / F3 | E3 / F3 `CanonicalUnitIncomplete` (page check shared with E1; RT-3 residual fixed, G2-R3a) |
| `test_rt_tamper` | forged / deleted Canonical row | F1 unit re-normalization (`CatalogIntegrityError`) |
| | deleted evidence-gap row; deleted quality report | F3 report re-derivation (`CatalogIntegrityError` / `QualityReportMissing`) |
| | deleted listing row | E2 listing proof (`not a listing derivation batch`) |
| | rows appended under a dataset's selection id | immune: the manifest binds its own snapshot |
| | Canonical / dataset Parquet file rewritten in place; archive object bit-flipped | F1 proofs / F3 read-back / D2 object checksum (`CatalogIntegrityError`) |
| | forged row under a genuine manifest hash | `ManifestStore.load` / `persist` |
| | self-consistent manifest no build produced (exclusion dropped) | `ManifestStore.persist` / `load` re-derive it (`DatasetBuilder.verify_manifest`, RT-4 fixed) |
| `test_rt_specs` | snapshot of another table; malformed snapshot id | catalog `SnapshotNotFound` |
| | stale evidence / listing / exchangeInfo / gap / Raw element / Canonical snapshot | F1 conflict, F2 `LISTING_NOT_DERIVED`, E2 history proof, F3 gap-batch check, D-33 edge re-derivation, `QualityReportMissing` |
| | forged hash of each of the 9 policy bindings; missing normalizer binding | F3 `KNOWN_BINDINGS` / F1 `REQUIRED_BINDINGS` |
| `test_rt_listings` | symbol absent from every snapshot; interval opening before the first observation; knowledge cutoff before the listing is known | F2 `NO_VISIBLE_LISTING` |
| | late snapshot moving a change point after a build | F2 `COMPETING_HEADS`; the old manifest replays |
| `test_rt_assumption` | ADR-0032 assumption bound over REST-only data | F1: REST revisions never move (rows identical to the unbound spec) |
| `test_rt_features` | feature run bound to a forged / missing manifest, or to another spec's manifest | `feature_request_from_dataset`: `ManifestStore.load` / PIT spec check (RT-6, fixed in G2-R1c) |

Runtime is dominated by the 19 crash points and the 16 arrival orders (about five minutes in total).
