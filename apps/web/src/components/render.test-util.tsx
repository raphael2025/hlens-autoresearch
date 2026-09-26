// Component-test helper (not a test file, not imported by the app; bundled into the
// src/**/*.test.tsx runs by scripts/test-components.mjs). Renders a page / component with
// react-dom/server — effects never run there, so:
//
// - `renderInitial` is the first paint a browser sees: every request still `loading`.
// - `renderSettled` installs a fetch stub serving typed `Route`s, and re-renders through the
//   `ApiSeedContext` seam (src/lib/useApi.ts) until every request issued by a `useApi` call has
//   settled — the loaded / empty / error states the effect would reach in the browser. Requests are
//   keyed by the fetches their `load` issues, so the order of `useApi` calls does not matter.
//
// API contract check: every body a `Route` serves is typed by the OpenAPI-generated schema
// (src/api.d.ts, via src/api.ts), and its path mirrors the matching src/api.ts client function, so
// `tsc -p tsconfig.test.json` (part of `npm run build`) fails when a fixture or inline body no
// longer fits the API a page parses.
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type {
  ContractNames,
  Health,
  InvalidReport,
  JobList,
  JobView,
  KnowledgeResult,
  LifecycleTransition,
  ReportEnvelope,
  ReportKind,
  ReportListing,
} from "../api";
import { InitialSelectionContext } from "../lib/initialSelection";
import type { LoadState } from "../lib/loadState";
import { ApiSeedContext, settle, type ApiSeed } from "../lib/useApi";

type Method = "GET" | "POST";
type Reply = { status: number; body: unknown } | { network: string };

/** One stubbed apps/api endpoint: `method` + path under `/api`, and what it answers. */
export type Route = { method: Method; path: string; reply: Reply };

function ok<T>(method: Method, path: string, body: T): Route {
  return { method, path, reply: { status: 200, body } };
}

/** Typed routes, one per src/api.ts client function (the body types come from src/api.d.ts). */
export const api = {
  health: (body: Health): Route => ok("GET", "/health", body),
  contracts: (body: ContractNames): Route => ok("GET", "/contracts", body),
  transitions: (body: LifecycleTransition[]): Route => ok("GET", "/lifecycle/transitions", body),
  listing: (kind: ReportKind, reports: ReportEnvelope[], invalid: InvalidReport[] = []): Route => {
    const body: ReportListing = { kind, reports, invalid };
    return ok("GET", `/reports/${kind}`, body);
  },
  report: (envelope: ReportEnvelope): Route =>
    ok("GET", `/reports/${envelope.kind}/${encodeURIComponent(envelope.id)}`, envelope),
  jobs: (body: JobList): Route => ok("GET", "/jobs", body),
  job: (body: JobView): Route => ok("GET", `/jobs/${encodeURIComponent(body.job_id)}`, body),
  knowledge: (body: KnowledgeResult): Route => ok("POST", "/knowledge/search", body),
  /** A non-2xx answer with apps/api's `{"detail": ...}` error body. */
  fail: (method: Method, path: string, status: number, detail: string): Route => ({
    method,
    path,
    reply: { status, body: { detail } },
  }),
  /** fetch itself rejects (API down / connection refused). */
  unreachable: (method: Method, path: string, message: string): Route => ({
    method,
    path,
    reply: { network: message },
  }),
};

export type Rendered = {
  html: string;
  /** Every request made, in first-seen order: `METHOD /api/path[ body]`. */
  requests: string[];
};

function requestUrl(input: unknown): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.pathname + input.search;
  return (input as Request).url;
}

function withFetch<T>(routes: readonly Route[], requests: string[], run: () => T): T {
  const unmatched: string[] = [];
  const previous = globalThis.fetch;
  // Records synchronously (before the first await), so the ApiSeed below can key a `load` by the
  // fetches it issues.
  const stub = (input: unknown, init?: RequestInit): Promise<Response> => {
    const method = (init?.method ?? "GET").toUpperCase();
    const url = requestUrl(input);
    const body = typeof init?.body === "string" ? ` ${init.body}` : "";
    requests.push(`${method} ${url}${body}`);
    const route = routes.find((r) => r.method === method && `/api${r.path}` === url);
    if (route === undefined) {
      unmatched.push(`${method} ${url}`);
      return Promise.reject(new Error(`no stub for ${method} ${url}`));
    }
    const { reply } = route;
    if ("network" in reply) return Promise.reject(new TypeError(reply.network));
    return Promise.resolve(
      new Response(JSON.stringify(reply.body), {
        status: reply.status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  };
  globalThis.fetch = stub as typeof fetch;
  try {
    const result = run();
    if (unmatched.length > 0) throw new Error(`unstubbed request(s): ${unmatched.join(", ")}`);
    return result;
  } finally {
    globalThis.fetch = previous;
  }
}

function unique(requests: readonly string[]): string[] {
  return [...new Set(requests)];
}

/** The first paint: no seed, so every `useApi` request is `loading` and nothing is fetched. */
export function renderInitial(element: ReactElement): Rendered {
  const requests: string[] = [];
  const html = withFetch([], requests, () => renderToStaticMarkup(element));
  return { html, requests: unique(requests) };
}

/**
 * Renders `element` after every request its `useApi` calls issue has settled against `routes`.
 * `selected` is the page's initial selection (InitialSelectionContext: a report / job id, or the
 * submitted knowledge search terms). Throws on a request no route serves.
 */
export async function renderSettled(
  element: ReactElement,
  { routes, selected = null }: { routes: readonly Route[]; selected?: string | null },
): Promise<Rendered> {
  const requests: string[] = [];
  const settled = new Map<string, LoadState<unknown>>();
  for (let pass = 0; pass < 10; pass++) {
    const pending = new Map<string, Promise<LoadState<unknown>>>();
    const seed: ApiSeed = (load) => {
      const start = requests.length;
      const promise = settle(load());
      const key = requests.slice(start).join(" + ");
      if (key === "") throw new Error("a useApi load issued no fetch synchronously; cannot key it");
      const known = settled.get(key);
      if (known !== undefined) return known;
      pending.set(key, promise);
      return undefined;
    };
    const html = withFetch(routes, requests, () =>
      renderToStaticMarkup(
        <InitialSelectionContext.Provider value={selected}>
          <ApiSeedContext.Provider value={seed}>{element}</ApiSeedContext.Provider>
        </InitialSelectionContext.Provider>,
      ),
    );
    if (pending.size === 0) return { html, requests: unique(requests) };
    for (const [key, promise] of pending) settled.set(key, await promise);
  }
  throw new Error("requests did not settle after 10 render passes");
}

/** `text` as React escapes it in markup, for `html.includes(escaped(...))`. */
export function escaped(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

/** How many times `needle` occurs in `html`. */
export function count(html: string, needle: string): number {
  return html.split(needle).length - 1;
}
