// The state of one request (useApi.ts) and the report-listing helpers every list page shares.
// Pure: tested by loadState.test.ts with `node --test`.
import type { InvalidReport, ReportEnvelope, ReportListing } from "../api";

export type LoadState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "ok"; data: T };

/** The data of an `ok` state, else `null`. */
export function dataOf<T>(state: LoadState<T>): T | null {
  return state.status === "ok" ? state.data : null;
}

/** The well-formed reports of a listing (`[]` while loading / failed). */
export function reportsOf(state: LoadState<ReportListing>): ReportEnvelope[] {
  return state.status === "ok" ? state.data.reports : [];
}

/** One warning line per report file apps/api found but could not serve. */
export function invalidWarnings(invalid: readonly InvalidReport[]): string[] {
  return invalid.map((item) => `${item.id}: ${item.reason}`);
}

/** The Dashboard's per-kind cell: the count, with the invalid files called out, or the error. */
export function listingSummary(state: LoadState<ReportListing>): string {
  switch (state.status) {
    case "idle":
    case "loading":
      return "…";
    case "error":
      return `错误：${state.error}`;
    case "ok": {
      const { reports, invalid } = state.data;
      return invalid.length === 0
        ? String(reports.length)
        : `${reports.length}（另有 ${invalid.length} 个无效文件）`;
    }
  }
}
