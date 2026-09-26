// View model of the Router Stops page. Payload shape written by research/reports/router.py
// (write_router_stop) for research/router/paper.py's RouterStop — a paper run that did NOT happen
// because the router stopped (no validated candidate, every route flat, or — evidence mode — a
// routed strategy's eligibility not evidenced; the per-strategy checks of the optional
// `eligibility` key are read by src/lib/routerEligibility.ts). Hand-typed since
// /reports/{kind} has no per-kind OpenAPI schema (ReportEnvelope.payload is `dict[str, Any]`).
// Pure: tested by routerStop.test.ts with `node --test`.

export type RouterStopPayload = {
  router: string;
  router_spec_hash: string;
  reason: string;
  detail: string;
  /** strategy ref -> lifecycle state value, as declared by the caller */
  lifecycle: Record<string, string>;
  state_result_hash: string;
  strategy_result_hashes: Record<string, string>;
  /** strategy ref -> validation report content hash; `null` when none was supplied */
  validation_reports: Record<string, string> | null;
  stop_hash: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asRouterStopPayload(
  payload: Record<string, unknown> | undefined,
): RouterStopPayload | null {
  if (
    payload === undefined ||
    typeof payload.reason !== "string" ||
    typeof payload.stop_hash !== "string" ||
    !isRecord(payload.lifecycle) ||
    !isRecord(payload.strategy_result_hashes)
  ) {
    return null;
  }
  return payload as unknown as RouterStopPayload;
}

const REASONS: Record<string, string> = {
  no_validated_candidate: "没有已验证的候选策略",
  all_routes_flat: "所有状态路由与 fallback 对每个策略的权重都为零",
  eligibility_not_evidenced: "证据模式：至少一个可路由策略的资格未被其验证报告证实",
};

/** The stop reason in words, with the raw value kept (an unknown reason is shown as-is). */
export function reasonText(reason: string): string {
  const text = REASONS[reason];
  return text === undefined ? reason : `${text}（${reason}）`;
}

export type StrategyRow = {
  strategy: string;
  lifecycle: string | null;
  result_hash: string | null;
  validation_report: string | null;
};

/** One row per strategy named anywhere in the stop record, sorted by ref; missing cells `null`. */
export function strategyRows(stop: RouterStopPayload): StrategyRow[] {
  const reports = stop.validation_reports ?? {};
  const names = new Set([
    ...Object.keys(stop.lifecycle),
    ...Object.keys(stop.strategy_result_hashes),
    ...Object.keys(reports),
  ]);
  return [...names].sort().map((strategy) => ({
    strategy,
    lifecycle: stop.lifecycle[strategy] ?? null,
    result_hash: stop.strategy_result_hashes[strategy] ?? null,
    validation_report: reports[strategy] ?? null,
  }));
}

export function routerStopLabel(stop: RouterStopPayload): string {
  return `${stop.router} — ${stop.reason}`;
}
