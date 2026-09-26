// Live-backend smoke for the console (`npm run smoke:live`; apps/web README "Live-backend smoke"),
// with no new dependency and no browser. Given a running apps/api at BASE_URL, it:
//
//   1. bundles the console's own modules (src/api.ts, every src/lib view-model helper, every page)
//      with the already-installed esbuild, exactly as scripts/test-components.mjs does;
//   2. points `fetch` at the live API the way the dev proxy does (`/api/<path>` -> BASE_URL/<path>,
//      vite.config.ts) and calls the console's real client functions;
//   3. runs every page's view-model helpers from src/lib on the live responses, asserting each
//      builds without error (and the pages that parse inline are covered by step 4);
//   4. server-renders every page (react-dom/server) seeded with the live answers through the
//      ApiSeedContext seam (src/lib/useApi.ts; the same settle loop as
//      src/components/render.test-util.tsx, but over real HTTP): no request left loading, no
//      error state, no raw-JSON fallback on a report page.
//
// Optional: BARE_BASE_URL = an apps/api with no knowledge provider and no jobs journal (the 503
// paths, through the client and the rendered pages); EXPECT_INVALID = "<kind>/<id>" of a malformed
// report file BASE_URL must list as invalid (and answer 422 for); EXPECT_JOBS = JSON
// {status: job_id} the journal must hold. tests/apps/test_live_backend_smoke.py starts both
// servers (real subprocesses on 127.0.0.1) and runs this script against them.
//
// What this does NOT prove: pixel rendering, effects (ECharts drawing), clicks or navigation in a
// real browser. Manual browser acceptance stays open.
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const BASE_URL = process.env.BASE_URL;
const BARE_BASE_URL = process.env.BARE_BASE_URL;
const EXPECT_INVALID = process.env.EXPECT_INVALID;
const EXPECT_JOBS = process.env.EXPECT_JOBS ? JSON.parse(process.env.EXPECT_JOBS) : null;
if (!BASE_URL) {
  console.error("usage: BASE_URL=http://127.0.0.1:<port> [BARE_BASE_URL=...] node scripts/live-smoke.mjs");
  process.exit(2);
}

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = join(webRoot, "src");
const outDir = join(webRoot, "node_modules", ".live-smoke"); // gitignored, rewritten every run
const outfile = join(outDir, "console.mjs");

const PAGES = [
  "Dashboard",
  "ValidationReports",
  "ResearchLoop",
  "StateStrategyMatrices",
  "RouterPaperRuns",
  "RouterStops",
  "PaperDeviations",
  "GateCalibration",
  "StateDiagnostics",
  "EventStatistics",
  "DegradationChecks",
  "Lifecycle",
  "Jobs",
  "KnowledgeSearch",
];
const LIBS = [
  "degradationCheck",
  "errors",
  "eventStatistics",
  "gateCalibration",
  "jobs",
  "loadState",
  "paperDeviation",
  "researchLoop",
  "routerEligibility",
  "routerStop",
  "stateDiagnostics",
];

mkdirSync(outDir, { recursive: true });
await build({
  stdin: {
    contents: [
      'export * as api from "./api.ts";',
      ...LIBS.map((name) => `export * as ${name} from "./lib/${name}.ts";`),
      'export { ApiSeedContext, settle } from "./lib/useApi.ts";',
      'export { InitialSelectionContext } from "./lib/initialSelection.ts";',
      ...PAGES.map((name) => `export { ${name} } from "./pages/${name}.tsx";`),
      'export { createElement } from "react";',
      'export { renderToStaticMarkup } from "react-dom/server";',
    ].join("\n"),
    resolveDir: srcDir,
    sourcefile: "live-smoke-entry.ts",
    loader: "ts",
  },
  outfile,
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node20",
  jsx: "automatic",
  logLevel: "warning",
  // CommonJS packages bundled into ES modules (react-dom/server) require Node built-ins at run time.
  banner: {
    js: 'import { createRequire as __hlensCreateRequire } from "node:module"; const require = __hlensCreateRequire(import.meta.url);',
  },
});
const c = await import(pathToFileURL(outfile).href);
const { api } = c;

// --- fetch: the dev proxy's rewrite, recorded synchronously (the seed keys requests by it) ------

const realFetch = globalThis.fetch;
let target = BASE_URL;
let requests = [];
globalThis.fetch = (input, init) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.pathname + input.search : input.url;
  const method = (init?.method ?? "GET").toUpperCase();
  requests.push(`${method} ${url}${typeof init?.body === "string" ? ` ${init.body}` : ""}`);
  if (!url.startsWith("/api/")) return Promise.reject(new Error(`the console fetched outside /api: ${url}`));
  return realFetch(`${target}${url.slice("/api".length)}`, init);
};

async function against(base, run) {
  const previous = target;
  target = base;
  try {
    return await run();
  } finally {
    target = previous;
  }
}

async function rejects(promise, status) {
  try {
    await promise;
  } catch (error) {
    assert.ok(error instanceof c.errors.ApiRequestError, `expected ApiRequestError, got ${error}`);
    assert.equal(error.status, status, error.message);
    assert.ok(error.detail.length > 0);
    const line = c.errors.describeError(error);
    assert.ok(line.includes(`HTTP ${status}`), line);
    return error;
  }
  assert.fail(`expected HTTP ${status}, the request succeeded`);
}

const checked = [];
function step(name) {
  checked.push(name);
}

// --- 1. the client + every page's view-model helpers on the live answers ---------------------

const health = await api.getHealth();
assert.equal(health.status, "ok");
const contracts = await api.getContracts();
assert.ok(Array.isArray(contracts) && contracts.length > 0);
const transitions = await api.getLifecycleTransitions();
assert.ok(transitions.length > 0 && transitions.every((t) => typeof t.from === "string" && typeof t.to === "string"));
step("health / contracts / lifecycle transitions");

const [invalidKind, invalidId] = EXPECT_INVALID ? EXPECT_INVALID.split("/") : [null, null];
const listings = {};
for (const kind of api.REPORT_KINDS) {
  const listing = await api.listReports(kind);
  assert.equal(listing.kind, kind);
  assert.ok(listing.reports.length > 0, `${kind}: no report served`);
  const summary = c.loadState.listingSummary({ status: "ok", data: listing });
  assert.ok(!summary.startsWith("错误"), summary);
  const warnings = c.loadState.invalidWarnings(listing.invalid);
  if (kind === invalidKind) {
    assert.deepEqual(listing.invalid.map((item) => item.id), [invalidId]);
    assert.ok(warnings[0].startsWith(`${invalidId}: `), warnings[0]);
  } else {
    assert.deepEqual(listing.invalid, [], `${kind}: unexpected invalid files`);
  }
  for (const report of listing.reports) {
    assert.deepEqual(await api.getReport(kind, report.id), report, `${kind}/${report.id}`);
  }
  listings[kind] = listing;
}
step(`list + detail of every report kind (${api.REPORT_KINDS.length})`);

function parsed(kind, parse) {
  return listings[kind].reports.map((report) => {
    const payload = parse(report.payload);
    assert.ok(payload !== null, `${kind}/${report.id}: the page's parser rejects the live payload`);
    return payload;
  });
}

// Validation Reports / State × Strategy Matrices parse inline in their page (covered by step 2);
// here only the fields those pages read.
for (const report of listings.validation_report.reports) {
  assert.ok(typeof report.payload.verdict === "string" && Array.isArray(report.payload.gates));
}
for (const report of listings.state_strategy_matrix.reports) {
  assert.ok(Array.isArray(report.payload.cells) && report.payload.cells.length > 0);
}

const rounds = c.researchLoop.roundRows(listings.research_loop_round.reports);
assert.equal(rounds.length, listings.research_loop_round.reports.length);
c.researchLoop.usageSeries(rounds);
rounds.forEach((row) => [c.researchLoop.formatUsage(row.roundUsage), c.researchLoop.formatUsage(row.totalUsage)]);

for (const report of [...listings.router_paper_run.reports, ...listings.router_stop.reports]) {
  const view = c.routerEligibility.eligibilityOf(report.payload);
  if (view !== null) c.routerEligibility.eligibilitySummary(view);
  c.routerEligibility.validationReportsOf(report.payload);
}
for (const stop of parsed("router_stop", c.routerStop.asRouterStopPayload)) {
  c.routerStop.reasonText(stop.reason);
  c.routerStop.strategyRows(stop);
  c.routerStop.routerStopLabel(stop);
}
for (const report of parsed("paper_deviation", c.paperDeviation.asPaperDeviationPayload)) {
  assert.ok(c.paperDeviation.summaryRows(report.summary).length > 0);
  c.paperDeviation.chartSeries(report);
  c.paperDeviation.deviationLabel(report);
}
for (const report of parsed("gate_calibration", c.gateCalibration.asCalibrationPayload)) {
  assert.ok(report.candidates.length > 0);
  for (const candidate of report.candidates) {
    c.gateCalibration.hasDetectorErrors(candidate);
    c.gateCalibration.detectorErrorRuns(candidate);
    for (const arms of Object.values(candidate.gates)) {
      for (const evidence of Object.values(arms)) {
        const rate = c.gateCalibration.passRate(evidence);
        assert.ok(rate !== null, "a gate arm without false_positive_rate / power");
        c.gateCalibration.ciCell(rate.rate);
      }
    }
  }
}
for (const report of parsed("state_diagnostics", c.stateDiagnostics.asStateDiagnosticsPayload)) {
  assert.ok(c.stateDiagnostics.stateRows(report).length > 0);
  c.stateDiagnostics.transitionRows(report);
  c.stateDiagnostics.flickerSummary(report);
  c.stateDiagnostics.diagnosticsLabel(report);
}
for (const report of parsed("event_statistics", c.eventStatistics.asEventStatisticsPayload)) {
  assert.ok(report.statistics.length > 0);
  report.statistics.forEach((stat) => c.eventStatistics.statisticView(stat));
  c.eventStatistics.statisticsLabel(report);
}
for (const check of parsed("degradation_check", c.degradationCheck.asDegradationCheckPayload)) {
  for (const metric of c.degradationCheck.metricRows(check)) {
    c.degradationCheck.statusText(c.degradationCheck.metricStatus(metric));
    c.degradationCheck.directionText(metric.direction);
  }
  c.degradationCheck.checkSummary(check);
  c.degradationCheck.degradationLabel(check);
}
step("every page's src/lib view model over the live report payloads");

const jobList = await api.listJobs();
const counts = c.jobs.statusCounts(jobList);
assert.equal(c.jobs.jobRows(jobList).length, jobList.jobs.length);
for (const status of ["succeeded", "failed", "interrupted"]) {
  assert.equal(c.jobs.jobRows(jobList, status).length, counts[status]);
}
for (const job of jobList.jobs) {
  const detail = await api.getJob(job.job_id);
  assert.deepEqual(detail, job);
  c.jobs.jobRow(detail);
}
if (EXPECT_JOBS !== null) {
  for (const [status, jobId] of Object.entries(EXPECT_JOBS)) {
    assert.equal(jobList.jobs.find((job) => job.job_id === jobId)?.status, status, `${status} job`);
  }
}
step(`jobs list + detail (${jobList.jobs.length} jobs)`);

const knowledge = await api.searchKnowledge({ terms: ["momentum"], limit: 50 });
assert.ok(knowledge.items.length > 0 && knowledge.provider);
step("knowledge search (200)");

if (invalidKind !== null) await rejects(api.getReport(invalidKind, invalidId), 422);
await rejects(api.getJob("not-a-hash"), 400);
await rejects(api.getJob("0".repeat(64)), 404);
step("error answers through the client (422 malformed report, 400, 404)");

let bareJobs = null;
let bareKnowledge = null;
if (BARE_BASE_URL) {
  await against(BARE_BASE_URL, async () => {
    bareKnowledge = await rejects(api.searchKnowledge({ terms: ["momentum"] }), 503);
    bareJobs = await rejects(api.listJobs(), 503);
  });
  step("unconfigured server: knowledge / jobs 503 through the client");
}

// --- 2. every page server-rendered over the live answers ---------------------------------------

// src/components/States.tsx ErrorState as react-dom/server renders it (other crimson cells, e.g. a
// failed job's status, are data, not an error state).
const ERROR_STATE = 'role="alert" style="color:crimson;white-space:pre-wrap"';

/** The page's settled markup plus every live request it issued (`METHOD /api/path[ body]`). */
async function renderLive(page, selected) {
  const settled = new Map();
  const issued = new Set();
  for (let pass = 0; pass < 10; pass++) {
    requests = [];
    const pending = new Map();
    const seed = (load) => {
      const start = requests.length;
      const promise = c.settle(load());
      const key = requests.slice(start).join(" + ");
      if (key === "") throw new Error("a useApi load issued no fetch synchronously; cannot key it");
      const known = settled.get(key);
      if (known !== undefined) return known;
      pending.set(key, promise);
      return undefined;
    };
    const html = c.renderToStaticMarkup(
      c.createElement(
        c.InitialSelectionContext.Provider,
        { value: selected },
        c.createElement(c.ApiSeedContext.Provider, { value: seed }, c.createElement(c[page])),
      ),
    );
    requests.forEach((request) => issued.add(request));
    if (pending.size === 0) return { html, issued };
    for (const [key, promise] of pending) settled.set(key, await promise);
  }
  throw new Error(`${page}: requests did not settle after 10 render passes`);
}

const REPORT_PAGES = {
  ValidationReports: "validation_report",
  StateStrategyMatrices: "state_strategy_matrix",
  RouterPaperRuns: "router_paper_run",
  RouterStops: "router_stop",
  PaperDeviations: "paper_deviation",
  GateCalibration: "gate_calibration",
  StateDiagnostics: "state_diagnostics",
  EventStatistics: "event_statistics",
  DegradationChecks: "degradation_check",
};

function selectionOf(page) {
  if (page in REPORT_PAGES) return listings[REPORT_PAGES[page]].reports[0].id;
  if (page === "Jobs") {
    return (jobList.jobs.find((job) => job.status === "succeeded") ?? jobList.jobs[0])?.job_id ?? null;
  }
  if (page === "KnowledgeSearch") return "momentum";
  return null;
}

for (const page of PAGES) {
  const { html, issued } = await renderLive(page, selectionOf(page));
  assert.ok(issued.size > 0, `${page}: issued no live request`);
  assert.ok(html.includes("SIMULATED / NOT_VALIDATED"), `${page}: no banner`);
  assert.ok(!html.includes('role="status"'), `${page}: a request is still loading`);
  assert.ok(!html.includes(ERROR_STATE), `${page}: an error state is shown`);
  if (page in REPORT_PAGES) {
    const kind = REPORT_PAGES[page];
    const id = selectionOf(page);
    assert.ok(issued.has(`GET /api/reports/${kind}`), `${page}: listing not requested`);
    assert.ok(issued.has(`GET /api/reports/${kind}/${id}`), `${page}: detail not requested`);
    assert.ok(!html.includes("<pre>{"), `${page}: the detail fell back to raw JSON`);
    if (kind === invalidKind) assert.ok(html.includes(invalidId), `${page}: invalid file not listed`);
  }
  if (page === "Dashboard") {
    assert.ok(html.includes(`ok (v${health.api_version})`), "Dashboard: health cell");
    assert.ok(!html.includes("错误："), "Dashboard: a report count cell shows an error");
  }
  if (page === "Jobs" && selectionOf(page) !== null) assert.ok(html.includes(selectionOf(page)), "Jobs detail");
  if (page === "KnowledgeSearch") assert.ok(html.includes(knowledge.result_hash.slice(0, 12)), "search result");
}
step(`every page server-rendered over the live API (${PAGES.length} pages)`);

if (BARE_BASE_URL) {
  await against(BARE_BASE_URL, async () => {
    const { html: search } = await renderLive("KnowledgeSearch", "momentum");
    assert.ok(search.includes(ERROR_STATE), "KnowledgeSearch: no error state");
    assert.ok(search.includes("HTTP 503") && search.includes(bareKnowledge.detail), "KnowledgeSearch 503");
    const { html: jobs } = await renderLive("Jobs", null);
    assert.ok(jobs.includes(ERROR_STATE), "Jobs: no error state");
    assert.ok(jobs.includes("HTTP 503") && jobs.includes(bareJobs.detail), "Jobs 503");
  });
  step("unconfigured server: Knowledge Search / Jobs pages show the 503 error state");
}

for (const name of checked) console.log(`live-smoke: ok - ${name}`);
console.log(`live-smoke: OK (${BASE_URL}${BARE_BASE_URL ? ` + ${BARE_BASE_URL}` : ""})`);
