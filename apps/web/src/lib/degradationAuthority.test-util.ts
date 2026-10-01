// Schema 1.1.0 degradation_check payloads in the shapes the research side writes (TEST ONLY
// values; no committed fixture carries them yet): `DegradationEvidence.as_mapping()`
// (research/operations/degradation.py) with and without the additive `authority` block
// (`AuthorityProvenance.payload()`, research/operations/authority.py, format 1.2.0). The base is a
// committed 1.0.0 fixture payload, so the metrics / summary parts are real writer output; the
// `check_hash` is not recomputed (the console never recomputes it — apps/api does).
import { clone } from "./fixtures.test-util.ts";

const H = (c: string) => c.repeat(64);

export const AUTHORITY_START = "2026-01-01T00:00:00Z";
export const AUTHORITY_END = "2026-02-01T00:00:00Z";

/** The caller-declared ADR-0067 evidence map (no `authority`). */
export function callerDeclaredEvidence(): Record<string, unknown> {
  return {
    lifecycle_history_hash: H("1"),
    lifecycle_scope:
      "the caller-supplied lifecycle history replays to ACTIVE; it is not verified to be the latest authoritative lifecycle record",
    profile_ref: "validation_profile:p11_test@1.0.0",
    profile_hash: H("3"),
    profile_freeze_id: "freeze-1",
    profile_freeze_calibration_report_hash: H("c"),
    profile_freeze_anchor_length: 2,
    profile_freeze_anchor_head_hash: H("d"),
    validation_report_hash: H("4"),
    baseline_gate_ids: { sharpe: "G1.sharpe" },
    recent_observation_set_id: "obs-1",
    recent_observation_set_hash: H("2"),
    recent_observation_manifest: { format: "hlens.p11.recent-metric-manifest@1.0.0", sources: [], metrics: {} },
    metric_method_id: "hlens.p11.monitoring-metrics@2.0.0",
    recent_metrics_scope: "caller-declared manifest",
    window_start: AUTHORITY_START,
    window_end: AUTHORITY_END,
  };
}

/** An `AuthorityProvenance.payload()` bound to `callerDeclaredEvidence()`. */
export function authorityBlock(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    format: "hlens.p11.authority-provenance@1.2.0",
    subject: "strategy:trend_a@1.0.0",
    lifecycle: {
      head: { record_count: 3, last_record_hash: H("5") },
      anchor: "absent",
      history_hash: H("1"),
      record_hashes: [H("6"), H("7")],
    },
    source: {
      dataset_id: "ds-btcusdt-1h",
      manifest_hash: H("8"),
      contract_schema_version: "2.5.0",
      dataset_table: "research.bars",
      dataset_snapshot_id: "snap-42",
      data_type: "bars",
      point_in_time_hash: H("9"),
      bindings: {},
    },
    baseline: {
      run_id: "run-1",
      manifest_hash: H("a"),
      baseline_set_hash: H("b"),
      validation_report_hash: H("4"),
      profile_ref: "validation_profile:p11_test@1.0.0",
      profile_hash: H("3"),
    },
    metrics: {
      registry: "hlens.p11.monitoring-metrics@2.0.0",
      definitions: [
        {
          metric: "sharpe",
          definition: "p11.window_validation.sharpe@1.0.0",
          baseline_gate_id: "G1.sharpe",
          baseline_gate_metric: "sharpe",
          per_instrument: false,
          implementation: "research.validation.stats:sharpe",
          evidence: "window_validation",
          window_scope: "observation_window",
          value_representation: "exact",
          gate_id: "G1.sharpe",
        },
      ],
    },
    execution: { target_source: {} },
    recent_manifest_hash: H("2"),
    as_of: "2026-02-02T00:00:00Z",
    window_start: AUTHORITY_START,
    window_end: AUTHORITY_END,
    ...overrides,
  };
}

/** `base` (a committed 1.0.0 payload) as a 1.1.0 operation report with `evidence`. */
export function withEvidence(
  base: Record<string, unknown>,
  evidence: Record<string, unknown>,
): Record<string, unknown> {
  return { ...clone(base), schema_version: "1.1.0", evidence };
}
