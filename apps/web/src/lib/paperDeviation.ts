// View model of the Paper Deviation page. Payload shape written by research/reports/deviation.py
// (write_paper_deviation), i.e. research/router/deviation.py's PaperDeviation.to_payload(): the
// router's net paper result vs a reference backtest the caller declared, mark by mark, plus
// summary statistics. Decimals as exact text (`null` where not computable, never 0), times as
// ISO-8601 UTC. Descriptive only — no threshold, no verdict. Hand-typed since /reports/{kind} has
// no per-kind OpenAPI schema. Pure: tested by paperDeviation.test.ts with `node --test`.

export type DeviationMarkPayload = {
  time: string;
  paper_equity: string;
  reference_equity: string;
  equity_difference: string;
  paper_return: string | null;
  reference_return: string | null;
  return_difference: string | null;
};

export type DeviationSummaryPayload = {
  marks: number;
  initial_equity: string;
  final_equity_difference: string;
  mean_equity_difference: string;
  max_abs_equity_difference: string;
  max_abs_equity_difference_at: string;
  paper_total_return: string;
  reference_total_return: string;
  total_return_difference: string;
  return_marks: number;
  mean_return_difference: string | null;
  mean_abs_return_difference: string | null;
  tracking_error: string | null;
};

export type PaperDeviationPayload = {
  kind: string;
  schema_version: string;
  status: string;
  note: string;
  router: string;
  run_hash: string;
  paper_result_hash: string;
  reference_result_hash: string;
  reference_request_hash: string;
  reference_provider: string;
  instruments: string[];
  marks: DeviationMarkPayload[];
  summary: DeviationSummaryPayload;
  deviation_hash: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asPaperDeviationPayload(
  payload: Record<string, unknown> | undefined,
): PaperDeviationPayload | null {
  if (
    payload === undefined ||
    payload.kind !== "paper_deviation" ||
    typeof payload.deviation_hash !== "string" ||
    !Array.isArray(payload.marks) ||
    !isRecord(payload.summary)
  ) {
    return null;
  }
  return payload as unknown as PaperDeviationPayload;
}

/**
 * Exact decimal text for display: a Python `Decimal` string such as "0E-18" (a quantized zero) or
 * "-10.000000000000000000" is shown without its exponent or trailing zeros ("0", "-10"); `null`
 * (not computable) is "—". Never rounds: the digits shown are the payload's.
 */
export function decimalText(value: string | null): string {
  if (value === null) return "—";
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:E([+-]?\d+))?$/i.exec(value);
  if (match === null) return value; // not a plain decimal: shown verbatim
  const [, sign, whole, fraction = "", exponentText = "0"] = match;
  const exponent = Number(exponentText);
  let digits = whole + fraction;
  let point = whole.length + exponent; // position of the decimal point within `digits`
  if (point <= 0) {
    digits = "0".repeat(1 - point) + digits;
    point = 1;
  } else if (point > digits.length) {
    digits = digits + "0".repeat(point - digits.length);
  }
  const integer = digits.slice(0, point).replace(/^0+(?=\d)/, "");
  const decimals = digits.slice(point).replace(/0+$/, "");
  const text = decimals === "" ? integer : `${integer}.${decimals}`;
  return /^0(\.0*)?$/.test(text) ? "0" : `${sign}${text}`;
}

/** A return (a fraction) as a percentage with `places` decimals, for labels only; "—" if null. */
export function percent(value: string | null, places = 4): string {
  if (value === null) return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(places)}%` : value;
}

export type SummaryRow = { label: string; value: string };

/** The summary as label / exact value rows, in a fixed reading order. */
export function summaryRows(summary: DeviationSummaryPayload): SummaryRow[] {
  return [
    { label: "marks", value: String(summary.marks) },
    { label: "initial_equity", value: decimalText(summary.initial_equity) },
    { label: "final_equity_difference（paper − reference）", value: decimalText(summary.final_equity_difference) },
    { label: "mean_equity_difference", value: decimalText(summary.mean_equity_difference) },
    {
      label: "max_abs_equity_difference",
      value: `${decimalText(summary.max_abs_equity_difference)} @ ${summary.max_abs_equity_difference_at}`,
    },
    { label: "paper_total_return", value: decimalText(summary.paper_total_return) },
    { label: "reference_total_return", value: decimalText(summary.reference_total_return) },
    { label: "total_return_difference", value: decimalText(summary.total_return_difference) },
    { label: "return_marks", value: String(summary.return_marks) },
    { label: "mean_return_difference", value: decimalText(summary.mean_return_difference) },
    { label: "mean_abs_return_difference", value: decimalText(summary.mean_abs_return_difference) },
    { label: "tracking_error（样本标准差，n − 1）", value: decimalText(summary.tracking_error) },
  ];
}

export type ChartSeries = {
  times: string[];
  paper: number[];
  reference: number[];
  difference: number[];
};

/** Numbers for the chart only (the tables keep the exact text). */
export function chartSeries(report: PaperDeviationPayload): ChartSeries {
  const number = (text: string) => {
    const value = Number(text);
    return Number.isFinite(value) ? value : Number.NaN;
  };
  return {
    times: report.marks.map((mark) => mark.time),
    paper: report.marks.map((mark) => number(mark.paper_equity)),
    reference: report.marks.map((mark) => number(mark.reference_equity)),
    difference: report.marks.map((mark) => number(mark.equity_difference)),
  };
}

export function deviationLabel(report: PaperDeviationPayload): string {
  return `${report.router} vs ${report.reference_provider} (Δ final ${decimalText(
    report.summary.final_equity_difference,
  )})`;
}
