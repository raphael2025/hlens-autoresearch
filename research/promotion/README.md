# research/promotion

Research Plane side of the ADR-0005 chain: **evidence → immutable `StrategyArtifact` → Strategy Registry**.
Status: **CODE_COMPLETE / DEBUG_PENDING** (2026-09-26). No contract, Schema or lifecycle change.

- `build_artifact(PromotionEvidence) -> PromotionPackage` — pure, no I/O; `promote(evidence, registry)`
  stores the golden blobs and registers the artifact.
- Evidence: `StrategySpec`; `ValidationReport`s (all PASS, G0–G4 each evaluated, one PASS with G5 sealed
  OOS — `research.validation.report.promotion_blocked_reason`); the `ExperimentSpec` of every report; the
  research `GitCodeRevision`; dependency hashes; the `LifecycleHistory` (human-approved OOS → PAPER, now
  PAPER / PRODUCTION_CANDIDATE / ACTIVE); the research `StrategyProvider` and the declared golden inputs.
- Anything missing / non-PASS / mismatched → `PromotionRefused(reason: PromotionRefusal, detail)`;
  nothing is written, no partial artifact exists.
- Today **no** library strategy (`research/strategies/library.py`) can be promoted:
  `no_validation_report` (tested).

Never imported by `apps/`; the production side is `apps/promotion`. Details: `service.py` docstring,
ADR-0005 "Implementation note (2026-09-26)". Tests: `tests/promotion/test_promotion_service.py`
(TEST ONLY fixtures in `tests/promotion/fixtures.py`).
