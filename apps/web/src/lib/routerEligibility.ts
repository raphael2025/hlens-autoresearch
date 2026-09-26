// View model of the router's evidence mode (P10-ELIG, research/router/evidence.py). A router paper
// run or router stop made with eligibility evidence carries an additive `eligibility` key: one
// check per routed strategy (EligibilityCheck.to_dict(), written by research/reports/router.py's
// `_payload` / `_stop_payload`). Trust-mode payloads have no such key, and then nothing is shown.
// Hand-typed since /reports/{kind} has no per-kind OpenAPI schema. Pure: tested by
// routerEligibility.test.ts with `node --test`.

/** One EligibilityCheck.to_dict() record, as written into the payload. */
export type EligibilityCheck = {
  strategy: string;
  /** the lifecycle state the caller claimed (its value) */
  lifecycle: string;
  /** the claimed validation report content hash; `null`: none claimed */
  report_hash: string | null;
  /** the report's subject ref, once a report was found */
  subject: string | null;
  /** the report's verdict, once a report was found */
  verdict: string | null;
  /** the report's G5 (sealed OOS) gate ids, once a report was found */
  sealed_oos_gates: string[];
  /** `null`: verified; otherwise the refusal reason code */
  refusal: string | null;
  detail: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function asCheck(value: unknown): EligibilityCheck | null {
  if (
    !isRecord(value) ||
    typeof value.strategy !== "string" ||
    typeof value.lifecycle !== "string" ||
    !isStringOrNull(value.report_hash) ||
    !isStringOrNull(value.subject) ||
    !isStringOrNull(value.verdict) ||
    !Array.isArray(value.sealed_oos_gates) ||
    !value.sealed_oos_gates.every((gate) => typeof gate === "string") ||
    !isStringOrNull(value.refusal) ||
    typeof value.detail !== "string"
  ) {
    return null;
  }
  return value as unknown as EligibilityCheck;
}

export type EligibilityView = {
  /** the well-formed checks, sorted by strategy ref */
  checks: EligibilityCheck[];
  /** entries of the `eligibility` value that are not a well-formed check (shown as a warning) */
  malformed: number;
};

/**
 * The payload's `eligibility` checks; `null` when the key is absent (trust mode — show nothing).
 * A present key that is not a list counts as one malformed entry, never as "no evidence".
 */
export function eligibilityOf(payload: Record<string, unknown> | undefined): EligibilityView | null {
  if (payload === undefined || !("eligibility" in payload) || payload.eligibility === undefined) {
    return null;
  }
  const raw = payload.eligibility;
  if (!Array.isArray(raw)) {
    return { checks: [], malformed: 1 };
  }
  const checks: EligibilityCheck[] = [];
  let malformed = 0;
  for (const entry of raw) {
    const check = asCheck(entry);
    if (check === null) malformed += 1;
    else checks.push(check);
  }
  checks.sort((a, b) => (a.strategy < b.strategy ? -1 : a.strategy > b.strategy ? 1 : 0));
  return { checks, malformed };
}

/** The refusal codes of research/router/evidence.py, in check order (the first failure wins). */
export const REFUSAL_ORDER = [
  "report_hash_missing",
  "report_not_found",
  "report_invalid",
  "report_hash_mismatch",
  "subject_mismatch",
  "verdict_not_pass",
  "sealed_oos_not_evaluated",
  "sealed_oos_not_passed",
] as const;

const REFUSALS: Record<string, string> = {
  report_hash_missing: "未声明验证报告哈希",
  report_not_found: "找不到声明哈希对应的验证报告",
  report_invalid: "验证报告格式无效",
  report_hash_mismatch: "验证报告内容哈希与声明不一致",
  subject_mismatch: "验证报告的 subject 不是该策略",
  verdict_not_pass: "验证报告判定不是 PASS",
  sealed_oos_not_evaluated: "验证报告没有 G5（密封样本外）门",
  sealed_oos_not_passed: "G5（密封样本外）门未全部通过",
};

/** A refusal code in words, with the raw code kept (an unknown code is shown as-is). */
export function refusalText(refusal: string): string {
  const text = REFUSALS[refusal];
  return text === undefined ? refusal : `${text}（${refusal}）`;
}

/** The check's result in words: verified, or refused with the reason. */
export function checkResultText(check: EligibilityCheck): string {
  return check.refusal === null ? "已核验（PASS 且含 G5）" : `拒绝：${refusalText(check.refusal)}`;
}

export type SealedOosStatus = "passed" | "not_passed" | "not_evaluated" | "not_checked";

/**
 * Where the check got with G5 (sealed OOS). G5 is checked last, so a refusal at an earlier step
 * (hash, report, subject, verdict) leaves it `not_checked` — even when the report lists G5 gates.
 */
export function sealedOosStatus(check: EligibilityCheck): SealedOosStatus {
  if (check.refusal === null) return "passed";
  if (check.refusal === "sealed_oos_not_passed") return "not_passed";
  if (check.refusal === "sealed_oos_not_evaluated") return "not_evaluated";
  return "not_checked";
}

const SEALED_OOS_TEXT: Record<SealedOosStatus, string> = {
  passed: "通过",
  not_passed: "未通过",
  not_evaluated: "未评估（无 G5 门）",
  not_checked: "未核验（更早的检查已拒绝）",
};

/** G5 status in words, followed by the report's G5 gate ids when there are any. */
export function sealedOosText(check: EligibilityCheck): string {
  const text = SEALED_OOS_TEXT[sealedOosStatus(check)];
  return check.sealed_oos_gates.length === 0 ? text : `${text} · ${check.sealed_oos_gates.join(", ")}`;
}

/** e.g. "2 个策略中 1 个已核验，1 个被拒绝" (malformed entries are counted separately). */
export function eligibilitySummary(view: EligibilityView): string {
  const verified = view.checks.filter((check) => check.refusal === null).length;
  const refused = view.checks.length - verified;
  const base = `${view.checks.length} 个策略中 ${verified} 个已核验，${refused} 个被拒绝`;
  return view.malformed === 0 ? base : `${base}；另有 ${view.malformed} 条无法解析的检查记录`;
}

/**
 * The payload's optional `validation_reports` binding (strategy ref -> report content hash), as
 * rows sorted by ref; `null` when absent or `null` (none supplied). Non-string values are dropped.
 */
export function validationReportsOf(
  payload: Record<string, unknown> | undefined,
): { strategy: string; report_hash: string }[] | null {
  const raw = payload?.validation_reports;
  if (!isRecord(raw)) return null;
  return Object.keys(raw)
    .sort()
    .flatMap((strategy) => {
      const hash = raw[strategy];
      return typeof hash === "string" ? [{ strategy, report_hash: hash }] : [];
    });
}
