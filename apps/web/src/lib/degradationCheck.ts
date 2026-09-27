// View model of the Degradation Checks page. Payload shape written by
// research/reports/degradation.py (write_degradation_check) for apps/worker/degradation.py's
// DegradationMonitor.check: every ruled metric with its baseline / recent value, decline, allowed
// decline and the threshold's source, plus the breaches and the missing metrics. Evidence only — a
// check never changes lifecycle state; missing means "evidence insufficient", never healthy. When
// every ruled metric is missing the whole check is `insufficient_evidence` (DegradationCheck
// .status; the payload then carries `"insufficient_evidence": true` next to `"degraded": false`):
// it is shown as its own state, never as healthy. Hand-typed since /reports/{kind} has no per-kind
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
  check_hash: string;
};

/**
 * The payload as a degradation check, or `null` (the page then shows the raw JSON). The additive
 * `insufficient_evidence` key is only ever written as `true` and never with `degraded: true`
 * (DegradationCheck refuses that); anything else is not a check this console can read.
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
    !Array.isArray(payload.missing)
  ) {
    return null;
  }
  if ("insufficient_evidence" in payload && (payload.insufficient_evidence !== true || payload.degraded)) {
    return null;
  }
  return payload as unknown as DegradationCheckPayload;
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
  if (check.insufficient_evidence === true || check.metrics.every((metric) => metric.missing)) {
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

/** The list label: subject, the check's status (never "ok" for insufficient evidence), window. */
export function degradationLabel(check: DegradationCheckPayload): string {
  return `${check.subject} — ${LABEL[checkStatus(check)]} (${check.window})`;
}
