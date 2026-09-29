# 2026-09-29 staged phase acceptance wave

## Scope and checkout identity

Three independent acceptance assignments covered Phase 1 Quality stream design, Phase 0.5/2–9, and Phase 10–14. The root checkout used by the phase reviewers was `codex/w1-stabilization@a0b88066bcc858584eb251d8f8a3c1bd66431a6c`; it had 145 dirty entries and reviewers made no changes. No full-repository suite or E1-CAP-1 capacity probe was run in this wave.

The separate Quality stream candidate was `codex/e1-quality-stream-v3@4e2db1644847cf6277bdb2c42bbe3e56d89c0301`, clean at review. Commit `22bd73d` adds the generic stream substrate; `4e2db16` fixes bounded record serialization. The code is not integrated into the root checkout.

## Phase 1 / Quality stream

Independent review returned **APPROVE for the generic stream substrate only**. The original unbounded serialization issue was fixed by capped incremental encoding. An 8 MiB scalar and an infinite lazy mapping are rejected within the configured record-byte budget without publishing a root; a failed writer clears its buffers and refuses `finish()`.

```text
uv run pytest -q tests/infrastructure/quality/test_report_streams.py
8 passed in 0.18s

uv run ruff check infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
All checks passed!

uv run ruff format --check infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
2 files already formatted

uv run mypy infrastructure/quality/report_streams.py tests/infrastructure/quality/test_report_streams.py
Success: no issues found in 2 source files

git diff --check 22bd73d8fd65a2dbed27dce5f7586edca00bc7c9...HEAD
exit 0, no output
```

This does not approve the manifest schema, reporter integration, Dataset bindings, ADR-0077 §6.1.5, Phase 1, or E1-CAP-1.

ADR-0093 amendment remains **BLOCKED / under revision**. The independent architecture review still requires: exact JSON escaping and event-ID domain input; a bounded ADR-0031 QGAP completeness seal; a byte-bounded Iceberg path for row groups and nested Raw `symbols` / listing `tradable_intervals`; and a precise ordering/join rule for listing revision-prefix verification. Do not implement report schema/reporter/Dataset wiring against the current draft.

### W3 Universe streaming port

The previously approved W3 Universe streaming implementation was ported to the current coordination base in isolated commit `faf630cb9f19d5214e5a132136f4e7c9f1230975` (`codex/e1-w3-universe-integration`), parent `7b4cc9c`. The 10-file change adds the shared Universe run-parameter type, bounded `RunSetBuilder` refs, Universe streaming, Dataset cursor wiring, and affected tests. It does not change contracts or touch the root checkout. Independent review **APPROVED this port only**; this is not E1-CAP-1 or Phase 1 acceptance.

```text
uv run pytest -q tests/infrastructure/pit/test_bounded_runs.py tests/infrastructure/universe/test_universe_builder_v3.py tests/infrastructure/universe/test_universe_listing_assumption.py tests/infrastructure/dataset/test_dataset_v3_sources.py tests/infrastructure/dataset/test_dataset_v3_builder.py tests/infrastructure/redteam/test_rt_listing_assumption_universe.py -k 'not test_v3_build_over_the_real_upstreams_selects_what_v2_selects and not test_owner_and_event_times_are_the_canonical_rows_times and not test_pit_keys_out_of_order_fail_the_real_build_closed'
87 passed, 4 deselected in 37.40s

uv run ruff check infrastructure/dataset/sources.py infrastructure/pit/runs.py infrastructure/universe/builder.py infrastructure/universe/run_params.py
All checks passed!

uv run ruff format --check infrastructure/dataset/sources.py infrastructure/pit/runs.py infrastructure/universe/builder.py infrastructure/universe/run_params.py
4 files already formatted

uv run mypy --follow-imports=silent infrastructure/dataset/sources.py infrastructure/pit/runs.py infrastructure/universe/builder.py infrastructure/universe/run_params.py
Success: no issues found in 4 source files

git diff --check 7b4cc9ce79583050eebd55ab9320be003b059900...HEAD
exit 0, no output
```

The four deselected Dataset source failures were reproduced on clean base `7b4cc9c` in the unchanged PIT selector (`orphan mapped edge`); the failed nodes were not rerun after the baseline reproduction. They remain deferred under the two-round rule. Default transitive mypy reports 19 errors in 8 unmodified dependency files; only the four touched production files passed with `--follow-imports=silent`. Ruff, format, and diff checks passed.

## Phase 0.5 and Phase 2–9

All current-checkout reruns below are focused evidence, not phase acceptance:

| Phase | Command/result | Acceptance state and remaining gates |
|---|---|---|
| 0.5 | `tests/contract_suites/knowledge.py tests/plugins/knowledge tests/research/hypotheses/test_knowledge_source.py tests/research/loop/test_loop_knowledge_source.py`: `80 passed in 8.65s` | **NOT ACCEPTED** — named human review of seed tags/assets and golden hashes for added seeds remain open. |
| 2 | `tests/contract_suites/state.py tests/test_state_contract_suite.py tests/plugins/states tests/research/states tests/infrastructure/state`: `139 passed in 1.34s` | **NOT ACCEPTED** — real Research Dataset validation, CLI/catalog, type checks, and phase acceptance remain open. |
| 3 | Historical `c22d903` evidence: `268 passed in 7.24s` | **NOT ACCEPTED** — historical checkout only; deferred Iceberg event test and production catalog creation remain open. |
| 4 | Historical `c22d903` evidence: `375 passed in 7.34s` | **NOT ACCEPTED** — historical checkout only; Profile numeric/calibration/freeze gates remain open. |
| 5 | Historical run: `215 passed in 237.32s`, then interrupted in `test_default_model_has_no_remainders_and_is_unchanged` | **NOT ACCEPTED** — incomplete run; two deferred hash nodes remain release gates. |
| 6 | Historical `c22d903` evidence: `11 passed in 0.20s` | **NOT ACCEPTED** — historical checkout; no real integrated P2/P5/P4 experiment. |
| 7 | Current focused typed-plan/durable set: `82 passed in 104.00s` | **NOT ACCEPTED** — six operators remain fail-closed/non-runnable; output completeness and a frozen Profile are unproven. |
| 8 | `tests/research/validation/test_g4_check_isolation.py tests/research/validation/test_g4_review_fixes.py tests/research/validation/test_cross_asset_cross_sectional.py tests/research/validation/test_impact_exact_comparison.py tests/research/validation/test_retro_audit.py`: `67 passed in 3.88s` | **NOT ACCEPTED** — report fixtures/component/live-smoke registry coverage is incomplete. |
| 9 | `tests/plugins/synthetic/test_random_walk.py tests/research/synthetic_lab/test_calibration.py tests/research/synthetic_lab/test_gate_calibration.py tests/research/synthetic_lab/test_evidence_setups.py`: `153 passed, 1 warning in 50.37s` | **NOT ACCEPTED** — adequate calibration evidence and Profile values remain open; deferred G5/multi-calibration tests were not run. |

## Phase 10–14

Focused checks passed, but **all phases remain NOT ACCEPTED**:

| Phase | Result | Remaining gates |
|---|---|---|
| 10 | `47 passed, 7 deselected, 1 warning in 0.62s` | Full router acceptance and deferred loop-round report nodes. |
| 11 | `99 passed, 1 warning in 101.15s` | Authoritative source resolver/metric provenance and a usable frozen Profile. |
| 12 | `35 passed in 0.30s` | Phase acceptance and human approval path for replacement proposals. |
| 13 | `111 passed in 3.55s` | Synthetic/simulated rejection only; no live venue or authorization, as required by current scope. |
| 14 | `10 passed in 22.32s` | No migration target or complete migration matrix. |

Web checks: `npm test` exited 1 (`110 passed, 1 failed`); `retroAudit.test.ts` expected one fixture but found two. Not rerun. `npm run build` exited 0. `npm audit` was not run; previously recorded dependency vulnerabilities remain unresolved.

## Overall acceptance

No phase was accepted by this wave. Deferred two-round failures remain failed release gates and were not retried. Phase 1 remains blocked by unresolved PIT graph/conflict-head and bounded Quality integration work plus the unrun 32 MiB E1-CAP-1 measurement. Profile values remain unfrozen. These results must not be presented as a percentage-based acceptance claim.
