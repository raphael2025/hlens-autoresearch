// View model of the Degradation Checks page. Payload shape written by
// research/reports/degradation.py (write_degradation_check) for apps/worker/degradation.py's
// DegradationMonitor.check: every ruled metric with its baseline / recent value, decline, allowed
// decline and the threshold's source, plus the breaches and the missing metrics. Evidence only — a
// check never changes lifecycle state; missing means "evidence insufficient", never healthy. When
// every ruled metric is missing the whole check is `insufficient_evidence` (DegradationCheck
// .status; the payload then carries `"insufficient_evidence": true` next to `"degraded": false`):
// it is shown as its own state, never as healthy. Evidence strength: a 1.1.0 report's evidence may
// carry an additive `authority` block (ADR-0098 AuthorityProvenance: lifecycle head + anchor,
// source dataset + manifest hash, metric definitions, as_of / window); without it the report is
// caller-declared (ADR-0067, and every legacy 1.0.0 report). Hand-typed since /reports/{kind} has no per-kind
// OpenAPI schema. Pure: tested by degradationCheck.test.ts with `node --test`.

export type DegradationMetricPayload = {
  metric: string;
  direction: "higher_is_better" | "lower_is_better" | string;
  baseline: string;
  recent: string | null;
  decline: string | null;
  max_decline: string;
  threshold_source: string;
  breached: boolean;
  missing: boolean;
};

export type DegradationBreachPayload = Record<string, string>;

/** Known ADR-0067 fields. The index signature keeps additive evidence fields forward-compatible. */
export type DegradationEvidencePayload = {
  lifecycle_history_hash?: unknown;
  lifecycle_scope?: unknown;
  profile_ref?: unknown;
  profile_hash?: unknown;
  profile_freeze_id?: unknown;
  profile_freeze_calibration_report_hash?: unknown;
  profile_freeze_anchor_length?: unknown;
  profile_freeze_anchor_head_hash?: unknown;
  validation_report_hash?: unknown;
  baseline_gate_ids?: unknown;
  recent_observation_set_id?: unknown;
  recent_observation_set_hash?: unknown;
  recent_observation_manifest?: unknown;
  metric_method_id?: unknown;
  recent_metrics_scope?: unknown;
  window_start?: unknown;
  window_end?: unknown;
  /**
   * ADR-0098 §4 AuthorityProvenance (research/operations/authority.py `payload()`): present only
   * on the authority-resolved path. Absent = the caller-declared path (调用方声明). Read through
   * `authorityOf`, never trusted by shape alone.
   */
  authority?: unknown;
  [key: string]: unknown;
};

/** The AuthorityProvenance payload shape (format `hlens.p11.authority-provenance@1.x.0`). */
export type DegradationAuthorityPayload = {
  format: string;
  subject: string;
  lifecycle: {
    head: { record_count: number; last_record_hash: string };
    anchor: "present" | "absent";
    history_hash: string;
    record_hashes: string[];
  };
  source: {
    dataset_id: string;
    manifest_hash: string;
    contract_schema_version?: string;
    dataset_table?: string;
    dataset_snapshot_id?: string;
    data_type?: string;
    point_in_time_hash?: string;
    bindings?: unknown;
  };
  baseline: {
    run_id: string;
    manifest_hash: string;
    baseline_set_hash: string;
    validation_report_hash: string;
    profile_ref: string;
    profile_hash: string;
  };
  metrics: {
    registry: string;
    definitions: {
      metric: string;
      definition: string;
      baseline_gate_id?: string;
      gate_id?: string;
      window_scope?: string;
      evidence?: string;
      implementation?: string;
      [key: string]: unknown;
    }[];
  };
  execution?: unknown;
  recent_manifest_hash: string;
  as_of: string;
  window_start: string;
  window_end: string;
};

export type DegradationCheckPayload = {
  kind: string;
  schema_version: string;
  status: string;
  note: string;
  subject: string;
  window: string;
  degraded: boolean;
  metrics: DegradationMetricPayload[];
  breaches: DegradationBreachPayload[];
  missing: string[];
  /** present (and `true`) only when every ruled metric is missing (research/reports/degradation.py) */
  insufficient_evidence?: true;
  /** Present on schema 1.1.0 operation reports; its contents are hash-bound provenance. */
  evidence?: DegradationEvidencePayload;
  check_hash: string;
};

/**
 * The payload as a degradation check, or `null` (the page then shows the raw JSON). A check
 * always ruled at least one metric, so `metrics` must be a non-empty array (an empty one would
 * make "every metric missing" vacuously true). The additive `insufficient_evidence` key is only
 * ever written as `true`, never with `degraded: true` (DegradationCheck refuses that), and only
 * when every metric is missing and `missing` names exactly those metrics; anything else is not a
 * check this console can read.
 */
export function asDegradationCheckPayload(
  payload: Record<string, unknown> | undefined,
): DegradationCheckPayload | null {
  if (
    payload === undefined ||
    payload.kind !== "degradation_check" ||
    typeof payload.check_hash !== "string" ||
    typeof payload.degraded !== "boolean" ||
    !Array.isArray(payload.metrics) ||
    payload.metrics.length === 0 ||
    !Array.isArray(payload.missing)
  ) {
    return null;
  }
  if ("insufficient_evidence" in payload && !insufficientEvidenceIsConsistent(payload)) {
    return null;
  }
  return payload as unknown as DegradationCheckPayload;
}

function insufficientEvidenceIsConsistent(payload: Record<string, unknown>): boolean {
  if (payload.insufficient_evidence !== true || payload.degraded) return false;
  const metrics = payload.metrics as unknown[];
  const missing = payload.missing as unknown[];
  const names: string[] = [];
  for (const metric of metrics) {
    if (typeof metric !== "object" || metric === null) return false;
    const { metric: name, missing: isMissing } = metric as Record<string, unknown>;
    if (typeof name !== "string" || isMissing !== true) return false;
    names.push(name);
  }
  if (!missing.every((name) => typeof name === "string")) return false;
  const listed = [...(missing as string[])].sort();
  names.sort();
  return listed.length === names.length && listed.every((name, index) => name === names[index]);
}

export type CheckStatus = "degraded" | "insufficient_evidence" | "not_degraded";

/**
 * The check as a whole, as DegradationCheck.status: degraded (a breach), insufficient evidence
 * (every ruled metric missing — no evidence either way) or not degraded. Insufficient evidence is
 * read from the payload's flag and, for a payload written before that key existed, from its
 * metrics: every one missing is never "not degraded".
 */
export function checkStatus(check: DegradationCheckPayload): CheckStatus {
  if (check.degraded) return "degraded";
  if (
    check.insufficient_evidence === true ||
    (check.metrics.length > 0 && check.metrics.every((metric) => metric.missing))
  ) {
    return "insufficient_evidence";
  }
  return "not_degraded";
}

export type MetricStatus = "breached" | "missing" | "within";

/** A metric's status: breached, missing (evidence insufficient) or within its allowed decline. */
export function metricStatus(metric: DegradationMetricPayload): MetricStatus {
  if (metric.breached) return "breached";
  if (metric.missing) return "missing";
  return "within";
}

const STATUS_TEXT: Record<MetricStatus, string> = {
  breached: "超出允许下降（breached）",
  missing: "无近期值 — 证据不足（missing），不是健康",
  within: "在允许下降以内",
};

export function statusText(status: MetricStatus): string {
  return STATUS_TEXT[status];
}

const DIRECTION_TEXT: Record<string, string> = {
  higher_is_better: "越高越好（decline = baseline − recent）",
  lower_is_better: "越低越好（decline = recent − baseline）",
};

/** The comparison direction in words; an unknown direction is shown verbatim. */
export function directionText(direction: string): string {
  return DIRECTION_TEXT[direction] ?? direction;
}

/**
 * The verdict line of a check: degraded, insufficient evidence, or not degraded — with the missing
 * metrics always named. Insufficient evidence never reads as "nothing exceeded".
 */
export function checkSummary(check: DegradationCheckPayload): string {
  const names = [...check.missing].sort().join(", ");
  if (checkStatus(check) === "insufficient_evidence") {
    const listed = names === "" ? "" : `（${names}）`;
    return `证据不足：全部 ${check.metrics.length} 个指标都没有近期值${listed} — 无法判断是否退化，不是健康`;
  }
  const missing = check.missing.length === 0 ? "" : `；证据不足（无近期值）：${names}`;
  if (check.degraded) {
    const metrics = check.breaches.map((breach) => breach.metric ?? "?").join(", ");
    return `退化：${check.breaches.length} 个指标超出允许下降（${metrics}）${missing}`;
  }
  return `未发现超出允许下降的指标${missing}`;
}

/** Breached first, then missing, then within; alphabetical inside each group. */
export function metricRows(check: DegradationCheckPayload): DegradationMetricPayload[] {
  const rank: Record<MetricStatus, number> = { breached: 0, missing: 1, within: 2 };
  return [...check.metrics].sort(
    (a, b) =>
      rank[metricStatus(a)] - rank[metricStatus(b)] || a.metric.localeCompare(b.metric),
  );
}

const LABEL: Record<CheckStatus, string> = {
  degraded: "DEGRADED",
  insufficient_evidence: "INSUFFICIENT EVIDENCE",
  not_degraded: "ok",
};

// ---- evidence strength (ADR-0067 caller-declared vs ADR-0098 authority-resolved) ----------------

export const AUTHORITY_FORMAT_PREFIX = "hlens.p11.authority-provenance@";
/** The provenance formats this console reads (apps/api/report_dto.py DEGRADATION_AUTHORITY_FORMATS). */
export const KNOWN_AUTHORITY_FORMATS: readonly string[] = ["1.0.0", "1.1.0", "1.2.0"].map(
  (version) => `${AUTHORITY_FORMAT_PREFIX}${version}`,
);

export type EvidenceStrength =
  /** evidence.authority of a known format, bound to the evidence around it */
  | "authority_resolved"
  /** an authority block this console cannot read (unknown format, missing fields, disagreeing
   * bindings): never shown as authority evidence */
  | "authority_unreadable"
  /** no authority block: the ADR-0067 caller-declared path, or a legacy 1.0.0 report */
  | "caller_declared";

export type MetricDefinitionRow = {
  metric: string;
  definition: string;
  gateId: string | null;
  windowScope: string | null;
  evidence: string | null;
};

/** The authority block as displayed: every field `null` when absent, plus what does not hold. */
export type AuthorityView = {
  format: string | null;
  formatKnown: boolean;
  subject: string | null;
  headRecordCount: number | null;
  headLastRecordHash: string | null;
  anchor: string | null;
  historyHash: string | null;
  recordHashCount: number | null;
  datasetId: string | null;
  manifestHash: string | null;
  dataType: string | null;
  snapshotId: string | null;
  baselineRunId: string | null;
  baselineManifestHash: string | null;
  baselineSetHash: string | null;
  metricRegistry: string | null;
  definitions: MetricDefinitionRow[];
  recentManifestHash: string | null;
  asOf: string | null;
  windowStart: string | null;
  windowEnd: string | null;
  /** why the block cannot be read as authority evidence (empty when it can) */
  problems: string[];
};

function obj(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

const SHA256 = /^[0-9a-f]{64}$/;

/**
 * The `evidence.authority` block of a check, or `null` when there is none (caller-declared or a
 * legacy 1.0.0 report). The API already refuses a known-format block whose shape or bindings do
 * not hold (apps/api/report_dto.py); the console re-checks the same points so that a block it
 * cannot read (an unknown format the API passes through, or a payload from elsewhere) is shown as
 * unreadable — never as authority-resolved.
 */
export function authorityOf(check: DegradationCheckPayload): AuthorityView | null {
  const evidence = obj(check.evidence);
  if (evidence === null || !("authority" in evidence)) return null;
  const authority = obj(evidence.authority) ?? {};
  const lifecycle = obj(authority.lifecycle) ?? {};
  const head = obj(lifecycle.head) ?? {};
  const source = obj(authority.source) ?? {};
  const baseline = obj(authority.baseline) ?? {};
  const metrics = obj(authority.metrics) ?? {};
  const format = str(authority.format);
  const formatKnown = format !== null && KNOWN_AUTHORITY_FORMATS.includes(format);
  const definitions: MetricDefinitionRow[] = [];
  const problems: string[] = [];
  if (obj(evidence.authority) === null) problems.push("authority 不是对象");
  if (!formatKnown) problems.push(`未识别的 provenance 格式：${format ?? "（无 format）"}`);
  if (Array.isArray(metrics.definitions)) {
    for (const entry of metrics.definitions) {
      const item = obj(entry);
      const definition = item === null ? null : str(item.definition);
      if (item === null || definition === null) {
        problems.push("metrics.definitions 中有无法读取的条目");
        continue;
      }
      definitions.push({
        metric: str(item.metric) ?? "?",
        definition,
        gateId: str(item.gate_id) ?? str(item.baseline_gate_id),
        windowScope: str(item.window_scope),
        evidence: str(item.evidence),
      });
    }
  } else {
    problems.push("缺少 metrics.definitions");
  }
  const view: AuthorityView = {
    format,
    formatKnown,
    subject: str(authority.subject),
    headRecordCount: int(head.record_count),
    headLastRecordHash: str(head.last_record_hash),
    anchor: str(lifecycle.anchor),
    historyHash: str(lifecycle.history_hash),
    recordHashCount: Array.isArray(lifecycle.record_hashes) ? lifecycle.record_hashes.length : null,
    datasetId: str(source.dataset_id),
    manifestHash: str(source.manifest_hash),
    dataType: str(source.data_type),
    snapshotId: str(source.dataset_snapshot_id),
    baselineRunId: str(baseline.run_id),
    baselineManifestHash: str(baseline.manifest_hash),
    baselineSetHash: str(baseline.baseline_set_hash),
    metricRegistry: str(metrics.registry),
    definitions,
    recentManifestHash: str(authority.recent_manifest_hash),
    asOf: str(authority.as_of),
    windowStart: str(authority.window_start),
    windowEnd: str(authority.window_end),
    problems,
  };
  if (view.headRecordCount === null || view.headRecordCount < 0 || !SHA256.test(view.headLastRecordHash ?? "")) {
    problems.push("lifecycle.head 不是 (record_count, last_record_hash) 身份");
  }
  if (view.anchor !== "present" && view.anchor !== "absent") {
    problems.push(`lifecycle.anchor 必须是 present / absent：${view.anchor ?? "（缺失）"}`);
  }
  if (view.datasetId === null || !SHA256.test(view.manifestHash ?? "")) {
    problems.push("source 不是 (dataset_id, manifest_hash) 身份");
  }
  if (!SHA256.test(view.baselineSetHash ?? "")) problems.push("缺少 baseline.baseline_set_hash");
  if (view.asOf === null) problems.push("缺少 as_of");
  // the bindings run_degradation_check enforces (research/operations/degradation.py)
  const bindings: [unknown, unknown, string][] = [
    [lifecycle.history_hash, evidence.lifecycle_history_hash, "lifecycle history"],
    [authority.recent_manifest_hash, evidence.recent_observation_set_hash, "recent manifest"],
    [baseline.profile_hash, evidence.profile_hash, "Profile"],
    [baseline.validation_report_hash, evidence.validation_report_hash, "validation report"],
    [authority.window_start, evidence.window_start, "window start"],
    [authority.window_end, evidence.window_end, "window end"],
  ];
  for (const [bound, declared, what] of bindings) {
    if (typeof bound !== "string" || bound !== declared) {
      problems.push(`authority 与 evidence 的 ${what} 不一致`);
    }
  }
  if (view.asOf !== null && view.windowEnd !== null) {
    const asOf = Date.parse(view.asOf);
    const end = Date.parse(view.windowEnd);
    if (!Number.isFinite(asOf) || !Number.isFinite(end) || asOf < end) {
      problems.push("as_of 早于窗口结束（ADR-0098 修订 2 要求 as_of ≥ window_end）");
    }
  }
  return view;
}

/** Authority-resolved only when the block is there, of a known format, and every check holds. */
export function evidenceStrength(check: DegradationCheckPayload): EvidenceStrength {
  const authority = authorityOf(check);
  if (authority === null) return "caller_declared";
  return authority.problems.length === 0 ? "authority_resolved" : "authority_unreadable";
}

const STRENGTH_LABEL: Record<EvidenceStrength, string> = {
  authority_resolved: "AUTHORITY-RESOLVED（权威解析）",
  authority_unreadable: "AUTHORITY UNREADABLE（无法读取的权威记录 — 不作权威证据）",
  caller_declared: "CALLER-DECLARED（调用方声明）",
};

export function evidenceStrengthLabel(strength: EvidenceStrength): string {
  return STRENGTH_LABEL[strength];
}

/**
 * What the badge does and does not claim. Authority-resolved is about where the inputs came from
 * (ADR-0098); it is still evidence only, says nothing about validity, and without an anchor a
 * rollback of whole trailing lifecycle records cannot be detected.
 */
export function evidenceStrengthText(check: DegradationCheckPayload): string {
  const strength = evidenceStrength(check);
  if (strength === "authority_resolved") {
    const anchor =
      authorityOf(check)?.anchor === "present"
        ? "生命周期 head 已用外部 anchor 核验"
        : "anchor absent：未用外部 anchor 核验，无法检测整段尾部记录的回滚";
    return (
      "生命周期取自 Lifecycle Registry 的钉定 head，近期指标由闭集 metric 定义在钉定的 v3 Dataset manifest 上计算" +
      `（ADR-0098）；${anchor}；登记处声明的操作者未经认证。仍然只是证据，不改变生命周期状态。`
    );
  }
  if (strength === "authority_unreadable") {
    return "报告带有 authority 字段，但控制台无法确认其格式或绑定，按调用方声明对待；原始内容见下方完整 evidence JSON。";
  }
  if (check.evidence === undefined) {
    return `schema ${check.schema_version}：没有哈希绑定的证据记录；生命周期与近期指标均为调用方声明，未经核验。`;
  }
  return "生命周期历史与近期指标由调用方声明（ADR-0067）：哈希绑定只证明内容一致，不证明它们是最新的权威记录或真实来源。";
}

/** The list label: subject, the check's status (never "ok" for insufficient evidence), window. */
export function degradationLabel(check: DegradationCheckPayload): string {
  return `${check.subject} — ${LABEL[checkStatus(check)]} (${check.window})`;
}
