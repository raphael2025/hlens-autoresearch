// View model of the Event Statistics page. Payload shape written by
// research/reports/event_statistics.py (write_event_statistics), i.e. research/events/stats.py's
// EventStatsReport.to_payload(): each statistic tagged with its `kind`, times as ISO-8601 UTC,
// durations as integer microseconds, decimals as exact text, `null` where undefined (never 0).
// Descriptive only: no validation threshold, not a Validation Profile input.
// Pure: tested by eventStatistics.test.ts with `node --test`.

export type EventFrequency = {
  kind: "event_frequency";
  start: string;
  end: string;
  count: number;
  per_day: string;
  /** [bucket start, count] */
  buckets: [string, number][];
};

export type CoOccurrence = {
  kind: "co_occurrence";
  window: number;
  n_a: number;
  n_b: number;
  a_with_b: number;
  b_with_a: number;
  expected_a_with_b: string;
  lift: string | null;
};

export type LeadLag = {
  kind: "lead_lag";
  max_lag: number;
  bin: number;
  pairs: number;
  a_leads: number;
  b_leads: number;
  simultaneous: number;
  /** [lag in microseconds, count] */
  bins: [number, number][];
};

export type OverlapDiagnostics = {
  kind: "overlap_diagnostics";
  horizon: number;
  n: number;
  overlapping_neighbours: number;
  overlap_fraction: string | null;
  independent_count: number;
  mean_gap_seconds: string | null;
  dispersion: string | null;
};

export type Statistic = EventFrequency | CoOccurrence | LeadLag | OverlapDiagnostics;

export type EventStatisticsPayload = {
  kind: string;
  schema_version: string;
  status: string;
  note: string;
  source_result_hashes: string[];
  statistics: Array<Statistic | ({ kind: string } & Record<string, unknown>)>;
  report_hash: string;
};

export function asEventStatisticsPayload(
  payload: Record<string, unknown> | undefined,
): EventStatisticsPayload | null {
  if (
    payload === undefined ||
    payload.kind !== "event_statistics" ||
    !Array.isArray(payload.statistics) ||
    !Array.isArray(payload.source_result_hashes)
  ) {
    return null;
  }
  return payload as unknown as EventStatisticsPayload;
}

const UNITS: [number, string][] = [
  [86_400_000_000, "d"],
  [3_600_000_000, "h"],
  [60_000_000, "min"],
  [1_000_000, "s"],
  [1_000, "ms"],
  [1, "µs"],
];

/** A duration in integer microseconds in the largest unit that divides it exactly. */
export function formatMicros(us: number): string {
  if (us === 0) return "0s";
  const [size, unit] = UNITS.find(([candidate]) => us % candidate === 0) ?? UNITS[UNITS.length - 1];
  return `${us / size}${unit}`;
}

function text(value: string | number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}

export type StatisticView = {
  kind: string;
  title: string;
  /** [field, value] pairs; durations formatted, `null` shown as a dash. */
  fields: [string, string][];
  /** bucket / lag histogram rows, when the statistic has one */
  histogram: { label: string; count: number }[];
};

const TITLES: Record<string, string> = {
  event_frequency: "Frequency（频率）",
  co_occurrence: "Co-occurrence（共现）",
  lead_lag: "Lead-lag（领先-滞后）",
  overlap_diagnostics: "Overlap / independence（重叠与独立性）",
};

/** One display block per statistic; an unknown kind is shown field by field, never dropped. */
export function statisticView(stat: EventStatisticsPayload["statistics"][number]): StatisticView {
  const title = TITLES[stat.kind] ?? stat.kind;
  switch (stat.kind) {
    case "event_frequency": {
      const s = stat as EventFrequency;
      return {
        kind: s.kind,
        title,
        fields: [
          ["start", s.start],
          ["end", s.end],
          ["count", text(s.count)],
          ["per_day", s.per_day],
        ],
        histogram: s.buckets.map(([start, count]) => ({ label: start, count })),
      };
    }
    case "co_occurrence": {
      const s = stat as CoOccurrence;
      return {
        kind: s.kind,
        title,
        fields: [
          ["window", formatMicros(s.window)],
          ["n_a", text(s.n_a)],
          ["n_b", text(s.n_b)],
          ["a_with_b", text(s.a_with_b)],
          ["b_with_a", text(s.b_with_a)],
          ["expected_a_with_b", s.expected_a_with_b],
          ["lift", text(s.lift)],
        ],
        histogram: [],
      };
    }
    case "lead_lag": {
      const s = stat as LeadLag;
      return {
        kind: s.kind,
        title,
        fields: [
          ["max_lag", formatMicros(s.max_lag)],
          ["bin", formatMicros(s.bin)],
          ["pairs", text(s.pairs)],
          ["a_leads", text(s.a_leads)],
          ["b_leads", text(s.b_leads)],
          ["simultaneous", text(s.simultaneous)],
        ],
        histogram: s.bins.map(([lag, count]) => ({ label: formatMicros(lag), count })),
      };
    }
    case "overlap_diagnostics": {
      const s = stat as OverlapDiagnostics;
      return {
        kind: s.kind,
        title,
        fields: [
          ["horizon", formatMicros(s.horizon)],
          ["n", text(s.n)],
          ["overlapping_neighbours", text(s.overlapping_neighbours)],
          ["overlap_fraction", text(s.overlap_fraction)],
          ["independent_count", text(s.independent_count)],
          ["mean_gap_seconds", text(s.mean_gap_seconds)],
          ["dispersion", text(s.dispersion)],
        ],
        histogram: [],
      };
    }
    default:
      return {
        kind: stat.kind,
        title,
        fields: Object.entries(stat)
          .filter(([key]) => key !== "kind")
          .map(([key, value]): [string, string] => [
            key,
            typeof value === "object" && value !== null
              ? JSON.stringify(value)
              : text(value as string | number | null | undefined),
          ]),
        histogram: [],
      };
  }
}

export function statisticsLabel(report: EventStatisticsPayload): string {
  return `${report.statistics.length} statistics · ${report.source_result_hashes.length} runs`;
}
