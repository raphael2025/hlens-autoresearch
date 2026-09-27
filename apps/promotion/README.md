# apps/promotion

Application Plane side of the ADR-0005 chain: **Equivalence Gate** and **deployment record**.
Status: **CODE_COMPLETE / DEBUG_PENDING** (2026-09-26). No contract, Schema or lifecycle change.
Nothing here runs, schedules or connects any execution; a `DeploymentRecord` is an audit record.

- `run_equivalence_gate(registry, artifact_id, candidate, production_code) -> EquivalenceOutcome`: runs
  the candidate production `StrategyProvider` on the artifact's golden inputs (read from the registry and
  verified) and compares **exactly** with the golden positions (`tolerance=None`: the contract declares no
  tolerance). Writes nothing; the caller records the check (`registry.record_equivalence`), passing or not.
  A candidate class from `research.*` is refused (`candidate_is_research_code`).
- `record_deployment(registry, check, lifecycle, config_hash)`: only for a recorded, passing check, with no
  failed check of the same production code, and the strategy at PRODUCTION_CANDIDATE / ACTIVE;
  `deployment_id` = SHA-256 of `{artifact_id, production commit + tree, config_hash}`.

Imports `core` and `infrastructure.registry` only — never `research/` (01-system.md §3; statically
tested). Details: `equivalence.py` docstring. Tests: `tests/promotion/test_equivalence_gate.py`.
