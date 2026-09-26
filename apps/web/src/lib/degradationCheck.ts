// View model of the Degradation Checks page. Payload shape written by
// research/reports/degradation.py (write_degradation_check) for apps/worker/degradation.py's
// DegradationMonitor.check: every ruled metric with its baseline / recent value, decline, allowed
// decline and the threshold's source, plus the breaches and the missing metrics. Evidence only — a
// check never changes lifecycle state; missing means "evidence insufficient", never healthy.
// Hand-typed since /reports/{kind} has no per-kind OpenAPI schema. Pure: tested by
// degradationCheck.test.ts with `node --test`.

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
  check_hash: string;
};

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
  return payload as unknown as DegradationCheckPayload;
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

/** The verdict line of a check: degraded, or not — with the missing metrics always named. */
export function checkSummary(check: DegradationCheckPayload): string {
  const missing =
    check.missing.length === 0 ? "" : `；证据不足（无近期值）：${[...check.missing].sort().join(", ")}`;
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

export function degradationLabel(check: DegradationCheckPayload): string {
  return `${check.subject} — ${check.degraded ? "DEGRADED" : "ok"} (${check.window})`;
}
