// Research-loop rounds with the optional P12 replacement trigger (TEST ONLY values; no committed
// fixture carries it): an `evolution` stage whose summary holds `replacement_trigger` in the shape
// research/loop/replacement.py `ReplacementTriggerStage.run` / `_row` / `_open_one` /
// `_evaluate_one` write (format_version 2; the job payload is
// research/evolution/replacement_job.py `ReplacementJobResult.payload()`, its proposals
// `ReplacementProposal.to_payload()`). The base is the committed LoopRoundRecord fixture, so every
// other field is real writer output; the record_hash id is not recomputed (the console never
// recomputes it — apps/api does).
import type { ReportEnvelope } from "../api";
import { clone } from "./fixtures.test-util.ts";

const H = (c: string) => c.repeat(64);

export const CANDIDATE = "strategy:trend_a_child@1.1.0";
export const INCUMBENT = "strategy:trend_a@1.0.0";
export const PROPOSAL_HASH = H("e");

function baseRow(phase: "open" | "evaluate", candidate: string): Record<string, unknown> {
  return {
    phase,
    candidate,
    candidate_hash: H("1"),
    candidate_state: "paper",
    incumbents: [INCUMBENT],
    report_hashes: [H("2")],
    trial: {
      hypothesis: `hypothesis:replacement_${candidate.replace(/[^a-z0-9_]/g, "_")}@1.0.0`,
      hypothesis_hash: H("3"),
      family_id: "family-trend",
    },
    window: null,
    window_opening: null,
    window_opening_hash: null,
    window_consumption: null,
    window_consumption_hash: null,
    job: null,
    error: null,
    refusal: null,
  };
}

const WINDOW = {
  window_id: "oos-2026q3",
  profile_ref: "validation_profile:p@1.1.0",
  profile_hash: H("4"),
  start: "2026-07-01T00:00:00+00:00",
  end: "2026-10-01T00:00:00+00:00",
  registration_hash: H("5"),
};

const OPENING = {
  format_version: 3,
  window_id: WINDOW.window_id,
  registration_hash: WINDOW.registration_hash,
  subject: CANDIDATE,
  subject_hash: H("1"),
  trial: "hypothesis:replacement@1.0.0",
  trial_hash: H("3"),
  loop_id: "loop-x",
  round_index: 0,
  opened_at: "2026-01-01T00:00:00+00:00",
};

/** One proposed (evaluate) row, one window_opened (open) row and one refused (open) row. */
export function triggerSummary(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const proposed = {
    ...baseRow("evaluate", CANDIDATE),
    window: WINDOW,
    window_opening: OPENING,
    window_opening_hash: H("6"),
    window_consumption: {
      format_version: 3,
      window_id: WINDOW.window_id,
      opening_hash: H("6"),
      subject: CANDIDATE,
      report_hashes: [H("2")],
      round_index: 2,
    },
    window_consumption_hash: H("7"),
    status: "proposed",
    job: {
      status: "PENDING_HUMAN_APPROVAL",
      recorded: [
        {
          kind: "replacement_proposal",
          schema_version: "1.0.0",
          status: "PENDING_HUMAN_APPROVAL",
          incumbent: INCUMBENT,
          candidate: CANDIDATE,
          proposal_hash: PROPOSAL_HASH,
        },
      ],
      already_proposed: [],
      not_descendant: [],
      refused: [],
    },
  };
  const opened = {
    ...baseRow("open", "strategy:trend_a_child@1.2.0"),
    window: { ...WINDOW, window_id: "oos-2026q4" },
    window_opening: { ...OPENING, window_id: "oos-2026q4" },
    window_opening_hash: H("8"),
    status: "window_opened",
  };
  const refused = {
    ...baseRow("open", "strategy:trend_a_child@1.3.0"),
    status: "refused",
    refusal: "the global unsealing budget (2) is used up: 2 unsealings and window openings are recorded (C-S2)",
  };
  return {
    due: true,
    format_version: 2,
    status: "PENDING_HUMAN_APPROVAL",
    proposed_by: "automation:loop:loop-x",
    triggers: [proposed, opened, refused],
    not_eligible: [{ candidate: "strategy:other@1.0.0", reason: "descends from no given incumbent" }],
    already_triggered: [],
    family_trials: 4,
    unsealing_count: 2,
    unsealing_budget: 2,
    ...overrides,
  };
}

/** `envelope` (the committed round) with an `evolution` stage carrying `trigger` (or not due). */
export function withTrigger(envelope: ReportEnvelope, trigger: unknown, id = "round-with-trigger"): ReportEnvelope {
  const payload = clone(envelope.payload);
  const stages = payload.stages as Record<string, unknown>[];
  const zero = { compute_seconds: "0", llm_cost_units: "0", trials: 0 };
  stages.push({
    charged: null,
    error: null,
    estimate: zero,
    name: "evolution",
    overrun: null,
    refused: [],
    status: "COMPLETED",
    summary: { stage: "evolution", replacement_trigger: trigger },
    usage: zero,
  });
  return { ...envelope, id, payload };
}
