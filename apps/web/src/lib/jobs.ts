// View model of the read-only Jobs page (GET /jobs; apps/api README "任务端点"). Pure: tested by
// jobs.test.ts with `node --test`. Nothing here starts, retries or cancels a job — the console
// only shows what the worker's verified results journal records.
import type { JobList, JobStatus, JobView } from "../api";

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  succeeded: "succeeded",
  failed: "failed",
  // started without a recorded result: still running, or died and awaiting review / a re-run
  interrupted: "interrupted（无结果：运行中或中途中断，待审查）",
};

export type JobRow = {
  jobId: string;
  shortId: string;
  name: string;
  status: JobStatus;
  statusLabel: string;
  attempts: string;
  outcome: string;
  starts: number;
  reruns: number;
  lines: string;
};

/** A compact one-line rendering of a JSON value (the job's params / result). */
export function compactJson(value: unknown, max = 80): string {
  const text = value === undefined ? "—" : JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function jobRow(job: JobView): JobRow {
  const outcome =
    job.status === "failed"
      ? (job.error ?? "—")
      : job.status === "succeeded"
        ? compactJson(job.result)
        : "—";
  return {
    jobId: job.job_id,
    shortId: job.job_id.slice(0, 12),
    name: job.name,
    status: job.status,
    statusLabel: JOB_STATUS_LABELS[job.status],
    attempts: job.attempts === null ? "—" : String(job.attempts),
    outcome,
    starts: job.starts,
    reruns: job.reruns,
    lines: job.first_seq === job.last_seq ? String(job.first_seq) : `${job.first_seq}–${job.last_seq}`,
  };
}

/** Count per status (every status present, zero included), in a fixed order. */
export function statusCounts(list: JobList): Record<JobStatus, number> {
  const counts: Record<JobStatus, number> = { succeeded: 0, failed: 0, interrupted: 0 };
  for (const job of list.jobs) counts[job.status] += 1;
  return counts;
}

/** The jobs table: optionally filtered to one status, in the journal's order of first start. */
export function jobRows(list: JobList, status: JobStatus | "all" = "all"): JobRow[] {
  return list.jobs.filter((job) => status === "all" || job.status === status).map(jobRow);
}
