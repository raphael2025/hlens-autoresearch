// View model of the State Diagnostics page. Payload shape written by
// research/reports/state_diagnostics.py (write_state_diagnostics), i.e.
// research/states/diagnostics.py's StateDiagnostics.to_payload(): decimals as exact text, `null`
// where undefined (never 0), times as ISO-8601 UTC. Descriptive only — no threshold decides
// whether a state is "good". Pure: tested by stateDiagnostics.test.ts with `node --test`.

export type StateRunPayload = {
  state: string;
  start: string;
  end: string;
  steps: number;
};

export type StateDiagnosticsPayload = {
  kind: string;
  schema_version: string;
  probability_places: number;
  state_space: string[];
  evaluations: number;
  not_computable: number;
  counts: Record<string, number>;
  shares: Record<string, string | null>;
  runs: StateRunPayload[];
  mean_steps: Record<string, string | null>;
  max_steps: Record<string, number>;
  transitions: Record<string, Record<string, number>>;
  transition_probabilities: Record<string, Record<string, string | null>>;
  min_run: number;
  short_run_share: string | null;
  switch_rate: string | null;
  /** 1.1.0 source binding; omitted by readable legacy 1.0.0 payloads. */
  source_result_hash?: string | null;
};

export function asStateDiagnosticsPayload(
  payload: Record<string, unknown> | undefined,
): StateDiagnosticsPayload | null {
  if (
    payload === undefined ||
    payload.kind !== "state_diagnostics" ||
    !Array.isArray(payload.state_space) ||
    !Array.isArray(payload.runs) ||
    ("source_result_hash" in payload &&
      payload.source_result_hash !== null &&
      typeof payload.source_result_hash !== "string")
  ) {
    return null;
  }
  return payload as unknown as StateDiagnosticsPayload;
}

export type StateRow = {
  state: string;
  count: number | null;
  share: string | null;
  mean_steps: string | null;
  max_steps: number | null;
  runs: number;
};

/** One row per state, in `state_space` order (the payload's semantic order). */
export function stateRows(report: StateDiagnosticsPayload): StateRow[] {
  return report.state_space.map((state) => ({
    state,
    count: report.counts[state] ?? null,
    share: report.shares[state] ?? null,
    mean_steps: report.mean_steps[state] ?? null,
    max_steps: report.max_steps[state] ?? null,
    runs: report.runs.filter((run) => run.state === state).length,
  }));
}

export type TransitionCell = { to: string; count: number | null; probability: string | null };
export type TransitionRow = { from: string; cells: TransitionCell[] };

/** The transition matrix, rows and columns in `state_space` order. */
export function transitionRows(report: StateDiagnosticsPayload): TransitionRow[] {
  return report.state_space.map((from) => ({
    from,
    cells: report.state_space.map((to) => ({
      to,
      count: report.transitions[from]?.[to] ?? null,
      probability: report.transition_probabilities[from]?.[to] ?? null,
    })),
  }));
}

export type FlickerSummary = {
  min_run: number;
  runs: number;
  short_runs: number;
  short_run_share: string | null;
  switch_rate: string | null;
};

/** The flicker metric: runs shorter than the caller's `min_run` (steps), and the switch rate. */
export function flickerSummary(report: StateDiagnosticsPayload): FlickerSummary {
  return {
    min_run: report.min_run,
    runs: report.runs.length,
    short_runs: report.runs.filter((run) => run.steps < report.min_run).length,
    short_run_share: report.short_run_share,
    switch_rate: report.switch_rate,
  };
}

/** `null` (undefined, e.g. no computable label) is shown as a dash, never as 0. */
export function cell(value: string | number | null): string {
  return value === null ? "—" : String(value);
}

export function diagnosticsLabel(report: StateDiagnosticsPayload): string {
  return `${report.state_space.join(" / ")} · ${report.evaluations} evals · min_run ${report.min_run}`;
}
