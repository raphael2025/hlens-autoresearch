# Phase 11 degradation operator — implementation specification

**Status:** Proposed implementation specification; Phase 11 remains `CODE_COMPLETE / DEBUG_PENDING`, not accepted.
**Baseline reviewed:** coordination branch `bf68bdf` (2026-09-27).
**Scope:** an explicit, local, read-only-input operator which invokes the existing degradation monitor and append-only report writer. It does not schedule itself, mutate lifecycle state, or change monitor thresholds.

## 1. Decision in this specification

Provide one explicit invocation which accepts all identity, baseline, observation, and window inputs from its caller. It must validate the supplied bindings, run `DegradationMonitor.check`, and write exactly one existing `degradation_check` report. It must not discover a subject or choose a profile implicitly.

The operator may report `degraded`, `not_degraded`, or `insufficient_evidence` according to the existing monitor. All three outcomes are evidence only. There is no lifecycle writer, event publication, timer, worker job, or API write endpoint in this operator.

No report is produced unless the caller supplies a verifiable lifecycle history whose current state is `ACTIVE`, a frozen Profile, the validation report and baseline values admitted for that subject, recent metric observations, and a non-empty, explicit observation window. “Trusted” means the caller obtains these inputs from its configured authoritative local sources. The operator can validate object bindings and content hashes; it cannot authenticate the caller or prove the authority of an arbitrary path supplied to it.

## 2. Source review and current behavior

### Governing documents

- `CLAUDE.md` §0–§8: accepted ADRs outrank status and memory; Validation Profile / Constitution are frozen; new capabilities prefer plugins; changes to contracts, lifecycle, profiles, or validation rules need a Proposed ADR first.
- `PROJECT_STATUS.md` §1, §5, §6, §9: P11 remains unaccepted; module work is being consolidated before later acceptance; P11 still needs an explicit observation entry. No active architectural decision is recorded for this operator.
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
| `research/reports/degradation.py` | Recomputes the check from provided maps, writes canonical hash-addressed `degradation_check` JSON append-only, includes metric values and threshold sources. | Current payload names a free-form `window` and values but does not identify the baseline validation report, lifecycle history, source observations, or metric aggregation method. |
| `apps/api/store.py` and `apps/api/app.py` | Read-only `GET /reports/degradation_check`; checks `check_hash`. | No write API exists or is needed. API hash validation does not prove evidence-source authority. |
| `core/contracts/validation_profile.py` | Profile includes lifecycle `paper_period`, threshold maps; exact map is additive under ADR-0052. | Profile object does not itself resolve a registry entry or authenticate that it is frozen/current. |
| `core/domain/research.py::ValidationReport` | Binds `subject`, Profile ref/hash, run and experiment identities, gates, and verdict. `GateResult` contains metric label/value and optional exact value. | A report's gate set is not by itself a complete strategy-performance baseline. Mapping a gate to a degradation metric must be explicit and unambiguous. |
| `core/lifecycle/strategy.py::LifecycleHistory` | Replays transitions and exposes `current_state`; ACTIVE is an explicit lifecycle state. | A supplied in-memory history is only as trustworthy as its source. There is no operator-integrated authoritative lifecycle resolver in this path. |
| `core/contracts/loop_audit.py::LoopRoundRecord` and `apps/worker/metrics.py` | Hash-bound round status, stage summaries, budget use; separate process-time measurements. | Neither contains the strategy's outcome metrics required by the degradation Profile. Stage or round summaries must never be transformed into baseline/recent values. |

## 3. Proposed invocation and input DTOs

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

`metric_sources` is explicit because `ValidationReport.gates` can contain repeated conceptual metrics under different gates. The caller must select the baseline gate for each configured degradation metric; operator validation requires one exact gate match, its `metric` label to equal the degradation metric key, and a usable exact value (`value_exact`; otherwise an ADR-0052-compatible finite conversion of the legacy value). No fuzzy alias, gate ranking, averaging, or inferred key mapping.

## 4. Validation and fail-closed rules

Before the monitor runs, require all of the following:

1. `lifecycle.subject == subject`, lifecycle history is valid, and `lifecycle.current_state is ACTIVE`. Any absent, conflicting, non-current, or non-ACTIVE history refuses the run. The operator does not write DEGRADED even when the result breaches.
2. `profile.ref == baseline_report.validation_profile`; `profile.content_hash() == baseline_report.validation_profile_hash`; `profile.status is FROZEN`; report subject has the same `target_identity()` as `subject`; and `baseline_report.verdict is PASS`. A mismatch, non-frozen profile, or non-PASS baseline report refuses the run.
3. Baseline set report hash equals `baseline_report.content_hash()`. Each Profile degradation metric has exactly one named baseline gate; gate labels and metric names match exactly; no baseline is silently omitted. Extra baseline entries may be retained in the evidence manifest but are not compared unless Profile rules name them.
4. The exact threshold map is selected exactly as `DegradationMonitor.from_profile` does today. Empty threshold configuration fails; the operator must not borrow thresholds from another Profile, generate a default, infer comparator direction, or clamp values.
5. Recent set binds the same subject, Profile ref/hash and exact window supplied to the invocation. Every present metric value is a finite exact Decimal; keys are unique and normalized only according to the current monitor's accepted key grammar. No current/clock time is read to fill or shift the window.
6. Missing recent metrics remain missing. The existing monitor's three-valued result is preserved: an individual missing metric is not healthy; all missing produces `insufficient_evidence`; partial missing plus breach preserves both facts. No result is converted to PASS / healthy.
7. Report publication uses the existing append-only writer. Any validation, hash, JSON, identity, or write conflict stops the operation without replacing an existing file. Repeating an identical complete input is an idempotent no-op at the report file.

The operator's result is an evidence artifact, not a claim that the external source itself is truthful. The caller is responsible for using authoritative local inputs; the output must retain enough references/hashes for an independent reviewer to reopen those inputs.

## 5. Report provenance extension

The current report is reproducible from its displayed values but not traceable to source artifacts. Before implementing this operator, add an additive `evidence` object to the `degradation_check` payload and include it in `check_hash`:

```json
{
  "evidence": {
    "lifecycle_history_hash": "…",
    "validation_report_hash": "…",
    "profile_ref": "profile:…@…",
    "profile_hash": "…",
    "recent_observation_set_id": "…",
    "recent_observation_set_hash": "…",
    "metric_method_id": "…",
    "window_start": "…Z",
    "window_end": "…Z",
    "baseline_gate_ids": {"metric": "gate-id"}
  }
}
```

Keep current top-level `window` for existing readers, set it to the supplied stable label, and retain current metric rows / threshold source. Bump this report payload's own `schema_version` additively (proposed `1.1.0`); `apps/api` remains read-only and continues to verify `check_hash`. Before implementation, inspect `apps/web/src/lib/degradationCheck.ts` and the report page to ensure unknown additive fields remain accepted; update the type only if strict decoding requires it. Do not add `created_at` from wall clock to the hashed payload.

Because the payload shape and operator provenance semantics are a new report contract, draft a **Proposed** ADR before implementation. Do not mark it Accepted in this docs-only task. The ADR should settle evidence references, source authority claims, schema-version compatibility and lifecycle-state snapshot semantics. Existing accepted ADRs remain unchanged.

## 6. Resource budget and invocation

- No LLM, trial, compute-heavy runner, provider, event bus, or subprocess is involved. The operation is bounded by the supplied metric count plus local hash/report I/O.
- Do not reuse `LoopBudget`: it budgets research rounds, not this one-shot local evidence check.
- CLI is a later implementation detail. The first interface should be a Python function invoked by an explicit local composition script; every required input should come from caller-provided artifact paths/objects. No daemon or scheduled invocation.
- If a CLI is added in the same implementation, require explicit flags for subject, lifecycle history, Profile, baseline report, baseline mapping, recent observation manifest, half-open window start/end, and reports root. No `--latest`, date-relative shortcut, profile selector fallback, or implicit working-directory data discovery.
- Output only the report identity/path and status. Never print source payloads or secrets. Do not connect to exchange APIs, trading services, or external services.

## 7. File boundary and implementation order

1. **Proposed ADR only:** `docs/adr/00xx-p11-degradation-operator.md` (number assigned by coordinator); document evidence binding, payload 1.1.0, lifecycle snapshot requirement, and compatibility.
2. **Operation composition:** new `research/operations/degradation.py`; local frozen DTOs, binding checks, explicit invocation. It may depend on `apps.worker.degradation`, `core`, and `research.reports`; it must not add reverse imports from `apps/worker` to `research/`.
3. **Report writer:** `research/reports/degradation.py`; accept the explicit provenance DTO or a plain validated evidence mapping and include it in the hash-bound payload. Keep all threshold evaluation in `DegradationMonitor`.
4. **Tests, only after the implementation task is authorized:** operation source mismatch / wrong state / unfrozen profile / duplicate baseline gate / empty profile thresholds / exact Decimal / missing evidence / idempotent report / conflict refusal; no API write route and no lifecycle mutation.
5. **Docs after implementation:** worker / research reports / API README and PROJECT_STATUS. State remains `CODE_COMPLETE / DEBUG_PENDING` until Phase 11 acceptance.

Do not modify `core/contracts/`, `core/domain/`, Schema exports, accepted ADRs, validation rules, or lifecycle transitions for this slice. If implementing provenance requires a new durable observation contract or trusted lifecycle registry, stop and propose that as a separate decision instead of expanding this interface silently.

## 8. Remaining gaps and explicit decision points

- There is no authoritative resolver in the current local operation path for “which validation report admitted this currently ACTIVE subject”; caller must supply it and its identity is checked, but its authority source remains an implementation-time decision.
- There is no standard immutable recent-observation manifest, metric aggregation registry, or persisted baseline metric projection. This specification requires the caller to supply an explicit, content-hashed observation manifest and method ID; it does not define the financial/statistical meaning of those metrics.
- Current `degradation_check` payload omits evidence references. Provenance payload extension requires Proposed ADR review before code.
- `LifecycleHistory` is a value object, not a signed registry. A local operator can check that the supplied replay ends in ACTIVE but cannot guarantee it is the latest history unless an authoritative local source is selected.
- Profile's `paper_period` does not automatically define this operator's window. The caller must explicitly provide the interval; selecting its duration from Profile or from the most recent loop round would need a separately documented rule.
- No numerical degradation threshold is recommended here. The only accepted source is the actual bound Profile's current threshold mapping.

## 9. No tests run

This is a read-only analysis/specification task. No implementation or tests were run. Validation of this specification is deferred to the later acceptance phase as requested.
