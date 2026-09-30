# Phase 11 degradation operator — implementation specification

**Status:** Implementation specification; ADR-0067 is Accepted. This document is subordinate to ADR-0067 where wording differs. Phase 11 remains unaccepted.
**Initial design baseline:** coordination branch `669704c` (historical; implementation is now included in local `main@498250a`).
**Scope:** an explicit, local, read-only-input operator which invokes the existing degradation monitor and append-only report writer. It does not schedule itself, mutate lifecycle state, or change monitor thresholds.

## 1. Decision in this specification

Provide one explicit invocation which accepts all identity, baseline, observation, and window inputs from its caller. It must validate the supplied bindings, run `DegradationMonitor.check`, and write exactly one existing `degradation_check` report. It must not discover a subject or choose a profile implicitly.

The operator may report `degraded`, `not_degraded`, or `insufficient_evidence` according to the existing monitor. All three outcomes are evidence only. There is no lifecycle writer, event publication, timer, worker job, or API write endpoint in this operator.

No report is produced unless the caller supplies a verifiable lifecycle history whose current state is `ACTIVE`, a frozen Profile, the validation report and baseline values admitted for that subject, recent metric observations, and a non-empty, explicit observation window. “Trusted” means the caller obtains these inputs from its configured authoritative local sources. The operator can validate object bindings and content hashes; it cannot authenticate the caller or prove the authority of an arbitrary path supplied to it.

## 2. Source review and current behavior

### Governing documents

- `CLAUDE.md` §0–§8: accepted ADRs outrank status and memory; Validation Profile / Constitution are frozen; new capabilities prefer plugins; changes to contracts, lifecycle, profiles, or validation rules need a Proposed ADR first.
- `PROJECT_STATUS.md` §1, §5, §6, §9: ADR-0067 is Accepted, while Phase 11 remains unaccepted; implementation is included in local `main@498250a`, with integrated tests and Phase acceptance deferred.
- `PROJECT_MEMORY.md` §5–§7: ADR-0044 / 0049 / 0050 provide worker, loop, and audit boundaries; ADR-0052 provides exact decimal threshold fields; research work does not automatically promote lifecycle state.
- `docs/research/roadmap.md` §Phase 11: scheduled continuous loop, dashboard and degradation monitoring; bounded compute / LLM / trial budgets; no automatic promotion to ACTIVE.

### Accepted ADR constraints

- **ADR-0044:** `JobRunner` consumes bus messages and provides idempotent jobs; worker does not import `research/`. Durable interrupted jobs fail closed. This operator is a direct, explicit local invocation, not an automatic job or a new worker-to-research dependency.
- **ADR-0049 §7:** `DegradationMonitor` compares recent metrics with the validation baseline; thresholds come only from the bound Profile, with no defaults; `metric` / `[>=]` and `[<=]` define direction. Missing values are not healthy. Breach and insufficient-evidence events are evidence only and do not move lifecycle state.
- **ADR-0050:** loop-round records are deterministic, append-only, hash-chained descriptions of loop work. They do not define a performance-metric baseline or a recent-observation source.
- **ADR-0052:** `lifecycle.degradation_thresholds_exact` is the preferred exact threshold map, with legacy float mapping retained when the exact field is absent. Exact thresholds must be used as supplied; no operator-level threshold, tolerance, direction, or fallback may be invented.

### Implemented surfaces

| Surface | Existing behavior | Limitation relevant to the operator |
|---|---|---|
| `apps/worker/degradation.py` | `DegradationMonitor.from_profile`, `check`, and optional bus-publishing `observe`; exact Profile thresholds take precedence; checks finite Decimal values; missing recent metric is recorded; baseline missing metric raises. | Caller must already have resolved subject, Profile, baseline, recent values, and window. `observe` publishes events and is not used by this read-only report operation. No source identities are part of the input type. |
| `research/reports/degradation.py` | Legacy writer recomputes the check and preserves schema 1.0.0; the ADR-0067 operation writer emits schema 1.1.0 with hash-bound evidence and the complete recent manifest. | The payload binds caller-declared evidence; it does not authenticate lifecycle authority, external sources, or metric aggregation. |
| `apps/api/store.py` and `apps/api/app.py` | Read-only `GET /reports/degradation_check`; checks `check_hash`. | No write API exists or is needed. API hash validation does not prove evidence-source authority. |
| `core/contracts/validation_profile.py` | Profile includes lifecycle `paper_period`, threshold maps; exact map is additive under ADR-0052. | Profile object does not itself resolve a registry entry or authenticate that it is frozen/current. |
| `core/domain/research.py::ValidationReport` | Binds `subject`, Profile ref/hash, run and experiment identities, gates, and verdict. `GateResult` contains metric label/value and optional exact value. | A report's gate set is not by itself a complete strategy-performance baseline. Mapping a gate to a degradation metric must be explicit and unambiguous. |
| `core/lifecycle/strategy.py::LifecycleHistory` | Replays transitions and exposes `current_state`; ACTIVE is an explicit lifecycle state. | A supplied in-memory history is only as trustworthy as its source. There is no operator-integrated authoritative lifecycle resolver in this path. |
| `core/contracts/loop_audit.py::LoopRoundRecord` and `apps/worker/metrics.py` | Hash-bound round status, stage summaries, budget use; separate process-time measurements. | Neither contains the strategy's outcome metrics required by the degradation Profile. Stage or round summaries must never be transformed into baseline/recent values. |

## 3. Invocation and input DTOs

Place the composition surface in `research/operations/degradation.py` (new); keep comparison in `apps/worker/degradation.py` and report serialization in `research/reports/degradation.py`. Suggested public API:

```python
run_degradation_check(
    *,
    subject: Ref,
    lifecycle: LifecycleHistory,
    profile: ValidationProfile,
    baseline_report: ValidationReport,
    baseline: BaselineMetricSet,
    recent: RecentMetricSet,
    window: ObservationWindow,
    reports_root: Path,
) -> DegradationOperationResult
```

All parameters are required; no network, implicit registry search, default window, implicit latest report, profile-selection fallback, or default threshold is allowed. The function reads/validates supplied values and calls `write_degradation_check`; it has no bus parameter and no lifecycle mutation capability.

Use small frozen value objects local to the research operation (not `core/contracts` / JSON Schema in this batch):

```text
ObservationWindow:
  start: UTC datetime
  end: UTC datetime                 # half-open [start, end), end > start
  label: non-empty stable display string

BaselineMetricSet:
  validation_report_hash: content hash
  metrics: non-empty explicit map[str, exact decimal text]
  metric_sources: map[metric, unique GateResult.gate_id]

RecentMetricSet:
  subject: Ref
  profile_ref: Ref
  profile_hash: content hash
  window: ObservationWindow
  observation_set_id: stable caller-supplied opaque identifier
  observation_set_hash: content hash of canonical observation manifest
  method_id: caller-supplied immutable aggregation method/version identifier
  metrics: explicit map[str, exact decimal text]  # missing is represented by absence
```

The recent observation manifest must bind source IDs/hashes, per-source event time and knowledge/observation time, the metric names and values, and the aggregation method/version used to turn source rows into one window value. Raw records remain outside the report and repository. The operator receives the resulting metric set; it does not aggregate arbitrary raw rows.

`metric_sources` is explicit because `ValidationReport.gates` can contain repeated conceptual metrics under different gates. The caller must select the baseline gate for each configured degradation metric; operator validation requires one exact gate match, its `metric` label to equal the degradation metric key, and an exact `value_exact`. Float-only gates are refused. Baseline keys must equal the Profile's ruled metric set exactly. No fuzzy alias, gate ranking, averaging, fallback, or inferred key mapping.

## 4. Validation and fail-closed rules

Before the monitor runs, require all of the following:

1. `lifecycle.subject == subject`, lifecycle history is valid, and `lifecycle.current_state is ACTIVE`. Any absent, conflicting, or non-ACTIVE supplied history refuses the run. The operator does not claim that this supplied history is the latest authoritative history and does not write DEGRADED even when the result breaches.
2. `profile.ref == baseline_report.validation_profile`; `profile.content_hash() == baseline_report.validation_profile_hash`; `profile.status is FROZEN`; and an open, replay-verified `ProfileFreezeRegistry` must return an ADR-0062 freeze record for exactly this ref/hash. The report subject has the same `target_identity()` as `subject`, and `baseline_report.verdict is PASS`. A mismatch, absent/invalid anchor, non-frozen profile, or non-PASS baseline report refuses the run. `status` alone is not freeze evidence.
3. Baseline set report hash equals `baseline_report.content_hash()`. Baseline metric keys equal the Profile degradation metric set exactly. Each metric has exactly one named baseline gate; gate labels and metric names match exactly and the supplied value equals the gate's exact `value_exact`. Extra or missing metrics are refused.
4. The exact threshold map is selected exactly as `DegradationMonitor.from_profile` does today. Empty threshold configuration fails; the operator must not borrow thresholds from another Profile, generate a default, infer comparator direction, or clamp values.
5. Recent set binds the same subject, Profile ref/hash and exact window supplied to the invocation. Every present metric value is a finite exact Decimal; keys are unique and normalized only according to the current monitor's accepted key grammar. Source event time must be in `[start, end)`; source observation time must not be later than `end` (the end is excluded for event time and inclusive for the observation-time cutoff). No current/clock time is read to fill or shift the window.
6. Missing recent metrics remain missing. The existing monitor's three-valued result is preserved: an individual missing metric is not healthy; all missing produces `insufficient_evidence`; partial missing plus breach preserves both facts. No result is converted to PASS / healthy.
7. Report publication uses the existing append-only writer. Any validation, hash, JSON, identity, or write conflict stops the operation without replacing an existing file. Repeating an identical complete input is an idempotent no-op at the report file.

The operator's result is an evidence artifact, not a claim that the external source itself is truthful. The complete caller-declared observation manifest is embedded in the report and its hash can be recomputed independently. Source IDs are caller-declared references; there is no source resolver, so reacquiring original source objects depends on the caller's storage convention.

## 5. Report provenance extension

The ADR-0067 writer adds an `evidence` object to the `degradation_check` payload and includes it in `check_hash`:

```json
{
  "evidence": {
    "lifecycle_history_hash": "…",
    "lifecycle_scope": "caller-supplied history; latest authority not verified",
    "profile_freeze_id": "…",
    "profile_freeze_calibration_report_hash": "…",
    "profile_freeze_anchor_length": 1,
    "profile_freeze_anchor_head_hash": "…",
    "validation_report_hash": "…",
    "profile_ref": "profile:…@…",
    "profile_hash": "…",
    "recent_observation_set_id": "…",
    "recent_observation_set_hash": "…",
    "metric_method_id": "…",
    "window_start": "…Z",
    "window_end": "…Z",
    "baseline_gate_ids": {"metric": "gate-id"},
    "recent_observation_manifest": {
      "format": "hlens.p11.recent-metric-manifest@1.0.0",
      "subject": "…", "profile_ref": "…", "profile_hash": "…",
      "window": {"start": "…Z", "end": "…Z", "label": "…"},
      "observation_set_id": "…", "method_id": "…",
      "sources": [{"source_id": "…", "source_hash": "…", "event_time": "…Z", "observed_time": "…Z"}],
      "metrics": {"metric": "1.25"}
    }
  }
}
```

Keep current top-level `window` for existing readers, set it to the supplied stable label, and retain current metric rows / threshold source. The report schema is `1.1.0`; `apps/api` remains read-only and continues to verify `check_hash`. The Web reader accepts additive evidence. Do not add `created_at` from wall clock to the hashed payload. The legacy 1.0.0 builder accepts no evidence mapping; 1.1.0 reports are produced from an operation result. This is an accidental-misuse boundary, not signature-based authenticity.

ADR-0067 has accepted the additive report contract, ProfileFreezeRegistry anchor snapshot identity, source authority claims and compatibility. `ProfileFreezeRegistry.anchor_snapshot` exposes the verified anchor journal length and head hash without exposing its path. Existing accepted ADRs remain unchanged.

## 6. Resource budget and invocation

- No LLM, trial, compute-heavy runner, provider, event bus, or subprocess is involved. The operation is bounded by the supplied metric count plus local hash/report I/O.
- Do not reuse `LoopBudget`: it budgets research rounds, not this one-shot local evidence check.
- The local CLI is an explicit call adapter for this operation, not a loop launcher. Invoke it with `python -m research.operations.degradation_cli`; it must not load a loop config, reuse `LoopBudget`, or accept a loop state directory.
- Require explicit flags for subject, lifecycle history, Profile, baseline report, baseline exact metrics + gate mapping, recent observation manifest + hash, UTC window start/end + stable label, freeze registry root + external anchor, and reports root. Input JSON must reject repeated keys, non-finite JSON constants, unknown fields in CLI-owned objects, and non-canonical / floating-point metric values. The baseline file shape is `{"validation_report_hash":"<sha256>","metrics":{"<metric>":"<canonical decimal>"},"gate_ids":{"<metric>":"<gate id>"}}`. The recent file's `manifest` object has the exact `RecentMetricManifest.payload()` fields defined in §4; its envelope is `{"manifest":<manifest object>,"manifest_hash":"<sha256>"}`.
- The subject ref is canonical `kind:name@version`. The lifecycle, Profile, and validation report files contain their direct JSON model payloads. `--window-start` and `--window-end` require explicit UTC offsets; no relative dates or current-clock defaults.
- Refuse a missing freeze registry, lock file, journal, blob directory, or anchor before opening it so this CLI does not initialize registry storage. The anchor must be outside the registry root. Opening an existing registry takes its exclusive lock; its constructor may complete its documented recovery for a journal record committed immediately before a crash but not yet anchored.
- Require an existing reports root separate from the freeze-registry root; after successful operation validation, write exactly one report through `write_degradation_operation`. Print only status, check hash, and report path. Input refusal, registry errors, and I/O failures return non-zero without printing source payloads. Use one CLI process per reports root at a time; the report writer has no cross-process directory lock.
- No `--latest`, date-relative shortcut, Profile selector fallback, implicit working-directory data discovery, or network access. Do not connect to exchange APIs, trading services, or external services.

## 7. File boundary and implementation order

1. **Accepted ADR:** `docs/adr/0067-p11-degradation-evidence-operator.md`; implementation and documentation follow this decision.
2. **Operation composition:** new `research/operations/degradation.py`; local frozen DTOs, binding checks, explicit invocation. It may depend on `apps.worker.degradation`, `core`, and `research.reports`; it must not add reverse imports from `apps/worker` to `research/`.
3. **Report writer:** `research/reports/degradation.py`; the 1.1.0 path accepts only the operation result and includes its provenance in the hash-bound payload. Keep all threshold evaluation in `DegradationMonitor`.
4. **Focused tests and acceptance are deferred** per Raphael's instruction; the later acceptance batch should cover source mismatch / wrong state / unfrozen profile / duplicate baseline gate / empty Profile thresholds / exact Decimal / missing evidence / idempotent report / conflict refusal. No API write route or lifecycle mutation is in scope.
5. **Local CLI adapter:** `research/operations/degradation_cli.py` reads only explicit caller paths, invokes the accepted operation, then the existing writer. It does not resolve authoritative ACTIVE state, verify external source truth, aggregate observations, schedule work, mutate lifecycle, publish events, or interact with a loop.
6. **Docs after implementation:** operations README, this spec, and PROJECT_STATUS. State remains `CODE_COMPLETE / DEBUG_PENDING` until Phase 11 acceptance.

Do not modify `core/contracts/`, `core/domain/`, Schema exports, accepted ADRs, validation rules, or lifecycle transitions for this slice. If implementing provenance requires a new durable observation contract or trusted lifecycle registry, stop and propose that as a separate decision instead of expanding this interface silently.

## 8. Remaining gaps and explicit decision points

- There is no authoritative resolver in the current local operation path for “which validation report admitted this currently ACTIVE subject”; the caller supplies a valid lifecycle snapshot, and output explicitly does not claim it is latest.
- There is no metric aggregation registry, source URI resolver, or persisted baseline metric projection. The caller supplies the content-hashed manifest and method ID; the report embeds the complete manifest but does not define metric meaning or authenticate sources.
- `LifecycleHistory` is a value object, not a signed registry. A local operator can check that the supplied replay ends in ACTIVE but cannot guarantee it is the latest history unless an authoritative local source is selected.
- Profile's `paper_period` does not automatically define this operator's window. The caller must explicitly provide the interval; selecting its duration from Profile or from the most recent loop round would need a separately documented rule.
- No numerical degradation threshold is recommended here. The only accepted source is the actual bound Profile's current threshold mapping.

## 9. Implementation state (2026-09-27)

ADR-0067 is Accepted. The explicit operation, report writer, Profile freeze anchor snapshot, hash-bound manifest evidence, and explicit-input local CLI are included in local `main@498250a`. The CLI static checks are recorded in completion plan §10.28; integrated tests / build and Phase 11 acceptance remain deferred. The fast-forward is code consolidation, not Phase acceptance; `origin/main` remains `44fe9a2`.

## 10. ADR-0098 authority mode (2026-09-30)

ADR-0098 (Accepted) supersedes ADR-0080's block and adds a second, mutually exclusive input mode. The caller-declared mode above is unchanged: its evidence has no `authority` key and keeps the caller-declared scope texts.

- **Lifecycle:** `infrastructure/registry/lifecycle.py` `LifecycleRegistry` — append-only hash-chained transitions, `append(..., expected_head=...)`, replay at an explicit head (`latest` is read once and then pinned), ACTIVE set; a head not on the chain is refused.
- **Resolver:** `research/operations/authority.py` `resolve_degradation_inputs` returns the `LifecycleHistory`, a `RecentMetricSet` (method id `hlens.p11.monitoring-metrics@2.0.0` since ADR-0100 item 3, one source = the pinned v3 manifest) and an `AuthorityProvenance` (head identity, replayed history and record hashes, source identity and bindings, baseline run / manifest, metric definition ids, backtest / cost / strategy identities, the explicit as-of, the `BaselineMetricSet` content hash, window). `run_degradation_check(..., authority=...)` binds it and writes `evidence.authority`; the scope texts become `AUTHORITY_LIFECYCLE_SCOPE` / `AUTHORITY_RECENT_METRICS_SCOPE`.
- **Metrics (ADR-0100 item 3):** closed registry `MONITORING_METRIC_DEFINITIONS`, method id `hlens.p11.monitoring-metrics@2.0.0` (provenance format `hlens.p11.authority-provenance@1.1.0`). A definition is selected by the ruled metric **and** the gate its baseline cites (the same label is reported by several gates, e.g. `breakeven_cost_multiple` by G2 / G4 / G5); it calls the validation function that produced that gate with the same Profile parameters on the window's data and records `definition_id@version`, the concrete gate id, `evidence` and `window_scope`. No formula is invented; a function's own insufficient-evidence gate or a stage the validator did not run is *missing*; a gate without an exact value is `metric_refused`.

  | Metric @ gate | Function | Evidence | Window scope |
  |---|---|---|---|
  | `breakeven_cost_multiple` @ `G4.cost_stress.breakeven`; `breakeven_cost_multiple_vs_stress` @ `G4.cost_stress.<i>` | `robustness.cost_stress_check` | window backtest | `observation_window` |
  | `positive_window_fraction` @ `G4.walk_forward.positive_fraction`; `max_window_pnl_share` @ `G4.walk_forward.max_window_share` | `robustness.walk_forward_check` | window backtest | Profile walk-forward test windows within the window (window must lie in the research window) |
  | `shuffle_timing_p_value` / `shift_timing_p_value` @ `G1.<c>_control` and `G1.<c>_control.seed.<seed>`; `<c>_timing_p_value_min_over_seeds` @ `G1.<c>_control` | `pipeline.run_in_sample` (G1 controls) | window validation | all computable labels of the window |
  | `effective_independent_trades` @ `G2.effective_sample_size`; `breakeven_cost_multiple` @ `G2.breakeven_cost_multiple`; `breakeven_cost_multiple_vs_stress` @ `G2.cost_stress.<i>`; `percentile_vs_random_entry_null` @ `G2.null_model_percentile`; `net_mean_hac_p_greater_adjusted` @ `G3.adjusted_p_value` | `pipeline.run_in_sample` (G2 / G3) | window validation | Profile walk-forward test folds within the window (window must lie in the research window) |
  | `probability_of_backtest_overfitting` / `one_minus_deflated_sharpe_ratio` @ `G4.overfitting`; `neighbor_mean_sharpe_over_chosen_sharpe` / `positive_neighbor_fraction` @ `G4.param_neighborhood.*` | `robustness.overfitting_check` / `parameter_neighborhood_check` | window validation (`robustness_diagnostic`) | declared parameter family re-run over the window |
  | `shifted_sharpe_over_base_sharpe` @ `G4.time_alignment.<i>`; `delayed_breakeven_cost_multiple` @ `G4.delay_stress`; `positive_instrument_fraction` / `positive_subuniverse_fraction` @ `G4.cross_asset.positive_fraction` | `time_alignment_check` / `delay_stress_check` / `cross_asset_check` | window validation | Profile variation re-run over the window |
  | `undersampled_state_pnl_share` @ `G4.state.undersampled_pnl_share`; `capacity_notional` @ `G4.capacity.required` | `state_decomposition_check` / `capacity_check` | window validation | `observation_window` |

  G1 – G3 definitions also accept the per-instrument gates `<gate>.instrument.<name>`. The *window validation* re-runs `PipelineBacktestValidator` (`validate` for G0 – G3 with its stage-stop rule, `robustness_diagnostic` for G4) on the window's proven bars with a `WindowValidationBinding`: spec, `ExperimentMetadata` (incl. `family_trial_count`), Outcome label spec + provider (= the run's `outcome_ref` / recorded plugin; labels are the evaluation target only, C-L2), validation seed (∈ the run's `seeds`), `RobustnessParams` (checked against every `param:` / `profile_field_missing:` gate of the report; a `param:` value for a field the Profile carries is refused), control seeds, market benchmark, declared execution model, C-R3 scope (exactly the pipeline's instruments, not a subset), optional state labeller / bar volume; the metadata's Constitution version and Profile selection must equal the run's and report's. **Unrecorded baseline inputs (2026-09-30 integrity fixes):** the baseline report and run record neither `family_trial_count`, nor which of the run's seeds the validator used, nor a `param:` `cscv_partitions` / `capacity.impact_coefficient`, nor the state labeller, so equality cannot be proven and every ruled definition depending on one is refused with `baseline_input_unrecorded`: `G3.adjusted_p_value` (trial count; seed via the G2 null model stage stop), `G4.overfitting` (PBO / 1−DSR: trial count; also `param:cscv_partitions`), `G2.null_model_percentile` (seed), the single-seed `G1.<c>_control` gates and every `G2.*` gate when the baseline ran single-seed controls (seed via the G1 stage stop), `G4.capacity.*` with a `param:` impact coefficient, `G4.state.*` with a state labeller. The per-seed `G1.<c>_control.seed.<seed>` gates and `..._min_over_seeds` (seeds recorded in the gate ids) and, with multi-seed controls, the non-null G2 gates stay monitorable. Its trial runner must reproduce the window backtest; a failed structural gate — the G0 binding / execution gates or the G1 information-flow gates `G1.outcome_not_input`, `G1.label_blind_sides` (`execution_mismatch`) and `G1.sealed_oos_excluded` (`source_scope_mismatch`), base id or per instrument — refuses every metric of that re-run. Always `metric_undefined`: every `G5.*` gate (one-shot sealed evaluation), the reported-only `G2.market_benchmark*` / `G2.inverse_control` / `G2.cost_report.*` items, any (metric, gate) pair outside the registry, and a walk-forward definition whose window is outside the Profile's research window (it would need data outside the window; the Profile windows are never re-anchored). A window-validation metric without a binding is `metric_inputs_unavailable`.
- **Default environment (ADR-0100 item 4):** `research/operations/authority_environment.py` `default_environment` (for `--authority-environment research.operations.authority_environment:default_environment`) returns a context manager that builds, and on exit closes, the Iceberg catalog adapter (`open_postgres_catalog_adapter(Settings(), PHASE1_REGISTRY)`), the `LocalFileStorageAdapter` and the `DatasetBuilder` → `DatasetCatalog`. The v3 evidence verifier comes from a trusted `HLENS_AUTHORITY_EVIDENCE_VERIFIER` factory (`(adapter, storage) -> StreamingEvidenceVerifier`), because its Dataset rule, run bounds and Quality factory are not `Settings`. `HLENS_AUTHORITY_*` names the baseline run, baseline manifest hash, strategy spec (+ risk policy), cost model, label spec (checked against the run's `outcome_ref`), initial equity and decision grid (step, warmup; horizon = label horizon). The backtest provider is resolved from the plugin registry by the run's recorded `name@version` and constructed from the composition root's table (recorded descriptor hash required); the decision pipeline (`StrategyWindowTargetSource`) uses the recorded research strategy / risk provider built with exactly the baseline spec, `CandidateTrialRunner`, signals from the window's bars only and the baseline manifest's universe as instruments. `HLENS_AUTHORITY_VALIDATION_METADATA` (+ `VALIDATION_SEED`, `ROBUSTNESS_PARAMS`, optional `CONTROL_SEEDS`, `MARKET_BENCHMARK`, `BAR_VOLUME`) adds the validation binding. Any missing or invalid setting refuses with `authority_environment_unavailable` naming every missing variable; the CLI keeps the refusal code (it no longer wraps a deliberate `AuthorityRefused` from the factory) and prints the missing variable names, never values.
- **CLI:** `--authority-registry <root> --authority-head <hash>|latest --dataset-id <selection_id> --manifest-hash <sha256> --authority-as-of <UTC ISO-8601>` (修订 2: required, not before `--window-end`). The catalog, decision pipeline and backtest provider come from `--authority-environment MODULE:CALLABLE` (修订 1: a trusted deployment factory resolved like the ADR-0095 worker `--factory` via `apps.worker.serve.load_factory`, returning an `AuthorityEnvironment` or a context manager yielding one) or from `main(..., authority_environment=...)`; never both. Without either, or when the factory cannot be loaded / fails / produces something else, the mode refuses with `authority_environment_unavailable` before any report.
- **Anchor (修订 1, 修订 2 §6):** optional `--authority-anchor <path>` (existing file outside the registry and the other roots) verifies the read-only snapshot against its external anchor; any mismatch (including the writer's one-record crash window) is refused as `lifecycle_unavailable`. `evidence.authority.lifecycle.anchor` is `present` or `absent` (stdout `anchor=...`); `latest` without an anchor runs and records `absent`.
- **Audit (修订 1):** `infrastructure.tools.registry_audit --lifecycle-root <root> [--lifecycle-anchor <path>]` adds the Lifecycle Registry's read-only `verify_integrity_snapshot` to the ADR-0091 audit (report `schema_version` 1.1.0).
- **修订 2 (as-of, truncation, scope binding):**
  - *Evaluation time:* explicit `as_of` (UTC, `>= window.end`). Bars are read with `price_cutoff=as_of`; only bars with `interval_end <= window.end` are kept and they must tile `[window.start, window.end)` per instrument (`source_incomplete` otherwise); `as_of` after the manifest's point-in-time view is `source_incomplete`. The authority recent manifest carries `as_of` (payload key present only when set, so caller-declared manifests and hashes are unchanged) and its sources may be observed up to `as_of`.
  - *Lifecycle:* the read-only snapshot (`LifecycleRegistry.open_snapshot`: no writer lock, no anchor crash-recovery write; the resolver refuses a writer instance) is replayed at the pinned head and truncated to `occurred_at <= as_of`; it must end in ACTIVE, the transition into it must have `occurred_at <= window.start`, and no transition may fall in `(window.start, as_of]` (`lifecycle_not_active`). The truncated history is the one `run_degradation_check` binds by hash; the provenance lists only the kept record hashes.
  - *Scope:* the declared instruments must equal the baseline and source manifests' universe members (Canonical symbols via `DegradedEpisodeKey`, as in `research.loop.dataset_source`; a member naming no Canonical symbol is refused) → `source_scope_mismatch`.
  - *Identity:* params compared as canonical JSON; `AuthorityProvenance.baseline_set_hash` must equal the supplied `BaselineMetricSet.content_hash()` and the recent manifest's `as_of` must equal the provenance's, both checked in `run_degradation_check`.
- **Execution grid (2026-09-30 integrity fixes):** `TargetSourceIdentity` also declares the decision grid (`decision_step`, `decision_warmup`) and the initial equity; they are in its payload (hence `evidence.authority.execution.target_source`) and must equal the window execution's equity and the baseline run's recorded values. The frozen `ReproducibilityTuple` records none of them (nor does the report carry the baseline backtest request), so an environment value is only a caller declaration: the resolution is refused with `execution_unrecorded` until the baseline run records them (a contract change, ADR). **Consequence: the authority mode currently refuses every resolution.**
- **Rule 5 matching (修订 1 item 4, `09753e6`):** the baseline check strips the single `[>=]` / `[<=]` comparator suffix `compare_gate` writes before comparing the metric name; ADR-0067 rule 5's meaning is unchanged.
