# research/promotion

Research Plane side of the ADR-0005 chain: **evidence → immutable `StrategyArtifact` → Strategy Registry**.
Status: **CODE_COMPLETE / DEBUG_PENDING** (2026-09-26). No contract, Schema or lifecycle change.

- `build_artifact(PromotionEvidence, *, freezes) -> PromotionPackage` — pure, no I/O (the open freeze
  registry answers from its verified records); `promote(evidence, registry, *, freezes)` stores the golden
  blobs and registers the artifact.
- Evidence: `StrategySpec`; `ValidationReport`s (all PASS, G0–G4 each evaluated, one PASS with G5 sealed
  OOS — `research.validation.report.promotion_blocked_reason`); the `ExperimentSpec` of every report; the
  `ValidationProfile` of every report (`profiles`: hash = the report's `validation_profile_hash`, status
  `FROZEN`, `provenance.calibration_report` present — Constitution C-A8; `profile_missing` /
  `profile_hash_mismatch` / `profile_not_frozen` / `profile_not_calibrated` / `profile_not_evidenced`); the
  research `GitCodeRevision`; dependency hashes; the `LifecycleHistory` (human-approved OOS → PAPER, now
  PAPER / PRODUCTION_CANDIDATE / ACTIVE); the research `StrategyProvider` and the declared golden inputs.
- Anything missing / non-PASS / mismatched → `PromotionRefused(reason: PromotionRefusal, detail)`;
  nothing is written, no partial artifact exists.
- Today **no** library strategy (`research/strategies/library.py`) can be promoted: complete TEST ONLY
  evidence under a non-frozen Profile is refused `profile_not_frozen` (tested; no Profile is frozen yet).
  **ADR-0062 (Proposed; B56):** `build_artifact(evidence, *, freezes)` / `promote(evidence, registry, *, freezes)`
  take an open, anchored `infrastructure.registry.ProfileFreezeRegistry`. A Profile's own `status = FROZEN` is not
  authoritative (`status` is outside its content hash, ADR-0008): each report's Profile must also have a verified
  `profile.frozen` record for exactly its ref **and** content hash whose calibration report is the one
  `provenance.calibration_report` cites — none, another hash of the ref, or a closed / poisoned registry →
  `profile_not_frozen`; `created_at` may not precede the freeze approval. The registry records a named human
  approval only (no identity authentication, not a production Control Plane).
  The toy happy path runs under a TEST ONLY Profile marked FROZEN, citing a TEST ONLY calibration report
  (`tests.promotion.fixtures.toy_calibration_report`) and registered in a temporary freeze registry (`toy_freezes`).

Never imported by `apps/`; the production side is `apps/promotion`. Details: `service.py` docstring,
ADR-0005 "Implementation note (2026-09-26)". Tests: `tests/promotion/test_promotion_service.py`
(TEST ONLY fixtures in `tests/promotion/fixtures.py`).
