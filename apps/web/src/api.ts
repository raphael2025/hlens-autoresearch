// Thin fetch client over the OpenAPI-generated types (src/api.d.ts, `npm run gen:api`).
// The console only talks to apps/api through these types — see apps/web/README.md.

import type { components } from "./api.d";

export type ReportEnvelope = components["schemas"]["ReportEnvelope"];
export type ReportKind = components["schemas"]["ReportKind"];
export type KnowledgeQuery = components["schemas"]["KnowledgeQuery"];

// `/knowledge/search` is declared `response_model=None` (apps/api/app.py) so its response has no
// OpenAPI schema to generate from; this mirrors `core.domain.research.KnowledgeItem` by hand.
export type KnowledgeItem = {
  name: string;
  version: string;
  source: string;
  license: string;
  claim: string;
  conditions: string[];
  evidence_level: string;
  status: string;
  links: unknown[];
};

const BASE = "/api";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`);
  if (!response.ok) {
    throw new Error(`${path}: HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export function getHealth(): Promise<{ status: string; api_version: string }> {
  return getJson("/health");
}

export function getContracts(): Promise<string[]> {
  return getJson("/contracts");
}

export function getLifecycleTransitions(): Promise<Array<{ from: string; to: string }>> {
  return getJson("/lifecycle/transitions");
}

export function listReports(kind: ReportKind): Promise<ReportEnvelope[]> {
  return getJson(`/reports/${kind}`);
}

export function getReport(kind: ReportKind, id: string): Promise<ReportEnvelope> {
  return getJson(`/reports/${kind}/${encodeURIComponent(id)}`);
}

// `KnowledgeQuery`'s generated type marks every defaulted field required (openapi-typescript
// treats a JSON Schema `default` as `required`); callers only need to set the fields they care
// about and let apps/api fill the rest.
export async function searchKnowledge(
  query: Partial<KnowledgeQuery>,
): Promise<{ items: KnowledgeItem[] }> {
  const response = await fetch(`${BASE}/knowledge/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query),
  });
  if (!response.ok) {
    throw new Error(`/knowledge/search: HTTP ${response.status}`);
  }
  return (await response.json()) as { items: KnowledgeItem[] };
}

export const REPORT_KINDS: ReportKind[] = [
  "validation_report",
  "research_loop_round",
  "state_strategy_matrix",
  "router_paper_run",
  "gate_calibration",
];
