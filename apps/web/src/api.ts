// Thin fetch client over the OpenAPI-generated types (src/api.d.ts, `npm run gen:api`).
// The console only talks to apps/api through these types — see apps/web/README.md. Read-only:
// the one POST is the knowledge search (a query body; ADR-0048).

import type { components } from "./api.d";
import { ApiRequestError, errorDetail } from "./lib/errors";

type Schemas = components["schemas"];

export type ReportEnvelope = Schemas["ReportEnvelope"];
export type ReportKind = Schemas["ReportKind"];
export type ReportListing = Schemas["ReportListing"];
export type InvalidReport = Schemas["InvalidReport"];
export type KnowledgeQuery = Schemas["KnowledgeQuery"];
export type KnowledgeResult = Schemas["KnowledgeResult"];
export type KnowledgeItem = Schemas["KnowledgeItem"];
export type JobList = Schemas["JobList"];
export type JobView = Schemas["JobView"];
export type JobStatus = Schemas["JobStatus"];

const BASE = "/api";

async function readBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null; // e.g. a proxy's HTML error page: the status alone is reported
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    throw new ApiRequestError(path, response.status, errorDetail(response.status, await readBody(response)));
  }
  return (await response.json()) as T;
}

export function getHealth(): Promise<{ status: string; api_version: string }> {
  return request("/health");
}

export function getContracts(): Promise<string[]> {
  return request("/contracts");
}

export function getLifecycleTransitions(): Promise<Array<{ from: string; to: string }>> {
  return request("/lifecycle/transitions");
}

/** `{kind, reports, invalid}`: well-formed reports plus every file apps/api could not serve. */
export function listReports(kind: ReportKind): Promise<ReportListing> {
  return request(`/reports/${kind}`);
}

export function getReport(kind: ReportKind, id: string): Promise<ReportEnvelope> {
  return request(`/reports/${kind}/${encodeURIComponent(id)}`);
}

/** Read-only view of the worker's results journal (503 when apps/api has none configured). */
export function listJobs(): Promise<JobList> {
  return request("/jobs");
}

export function getJob(jobId: string): Promise<JobView> {
  return request(`/jobs/${encodeURIComponent(jobId)}`);
}

// `KnowledgeQuery`'s generated type marks every defaulted field required (openapi-typescript
// treats a JSON Schema `default` as `required`); callers only need to set the fields they care
// about and let apps/api fill the rest. No provider -> 503, a provider failure -> 502
// (`ApiRequestError`), never a 200 with an error body.
export function searchKnowledge(query: Partial<KnowledgeQuery>): Promise<KnowledgeResult> {
  return request("/knowledge/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query),
  });
}

export const REPORT_KINDS: ReportKind[] = [
  "validation_report",
  "research_loop_round",
  "state_strategy_matrix",
  "router_paper_run",
  "gate_calibration",
];
