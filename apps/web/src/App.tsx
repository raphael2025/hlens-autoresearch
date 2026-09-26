import { lazy, Suspense, useState } from "react";

// Research console (ADR-0048, NOT_VALIDATED): read-only pages over apps/api. No order or trading
// UI of any kind lives here or ever will (H10) — see each page's SIMULATED / NOT_VALIDATED banner.
//
// Every page is code-split with React.lazy (apps/web README "Code splitting"): most pages pull in
// ECharts (apps/web/src/lib/echarts.ts), and bundling them all into the initial chunk pushed the
// vite build over the 500 kB warning threshold. Loading each tab's module on first visit keeps the
// entry chunk small; visited pages stay cached by the browser for the rest of the session.
const Dashboard = lazy(() => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })));
const ValidationReports = lazy(() =>
  import("./pages/ValidationReports").then((m) => ({ default: m.ValidationReports })),
);
const ResearchLoop = lazy(() =>
  import("./pages/ResearchLoop").then((m) => ({ default: m.ResearchLoop })),
);
const StateStrategyMatrices = lazy(() =>
  import("./pages/StateStrategyMatrices").then((m) => ({ default: m.StateStrategyMatrices })),
);
const RouterPaperRuns = lazy(() =>
  import("./pages/RouterPaperRuns").then((m) => ({ default: m.RouterPaperRuns })),
);
const Lifecycle = lazy(() => import("./pages/Lifecycle").then((m) => ({ default: m.Lifecycle })));
const KnowledgeSearch = lazy(() =>
  import("./pages/KnowledgeSearch").then((m) => ({ default: m.KnowledgeSearch })),
);
const GateCalibration = lazy(() =>
  import("./pages/GateCalibration").then((m) => ({ default: m.GateCalibration })),
);
const Jobs = lazy(() => import("./pages/Jobs").then((m) => ({ default: m.Jobs })));
const RouterStops = lazy(() =>
  import("./pages/RouterStops").then((m) => ({ default: m.RouterStops })),
);
const StateDiagnostics = lazy(() =>
  import("./pages/StateDiagnostics").then((m) => ({ default: m.StateDiagnostics })),
);
const EventStatistics = lazy(() =>
  import("./pages/EventStatistics").then((m) => ({ default: m.EventStatistics })),
);
const PaperDeviations = lazy(() =>
  import("./pages/PaperDeviations").then((m) => ({ default: m.PaperDeviations })),
);

const TABS = [
  { key: "dashboard", label: "Dashboard", render: () => <Dashboard /> },
  { key: "validation", label: "Validation Reports", render: () => <ValidationReports /> },
  { key: "research-loop", label: "Research Loop", render: () => <ResearchLoop /> },
  { key: "state-strategy", label: "State × Strategy Matrices", render: () => <StateStrategyMatrices /> },
  { key: "router-paper", label: "Router Paper Runs", render: () => <RouterPaperRuns /> },
  { key: "router-stop", label: "Router Stops", render: () => <RouterStops /> },
  { key: "paper-deviation", label: "Paper Deviation", render: () => <PaperDeviations /> },
  { key: "gate-calibration", label: "Gate Calibration", render: () => <GateCalibration /> },
  { key: "state-diagnostics", label: "State Diagnostics", render: () => <StateDiagnostics /> },
  { key: "event-statistics", label: "Event Statistics", render: () => <EventStatistics /> },
  { key: "lifecycle", label: "Lifecycle", render: () => <Lifecycle /> },
  { key: "jobs", label: "Jobs", render: () => <Jobs /> },
  { key: "knowledge", label: "Knowledge Search", render: () => <KnowledgeSearch /> },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export function App() {
  const [active, setActive] = useState<TabKey>("dashboard");
  const current = TABS.find((tab) => tab.key === active) ?? TABS[0];

  return (
    <main style={{ fontFamily: "system-ui", padding: 16, maxWidth: 1100, margin: "0 auto" }}>
      <h1>HLENS Research Console</h1>
      <nav style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActive(tab.key)}
            style={{ fontWeight: tab.key === active ? 700 : 400 }}
            aria-current={tab.key === active}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      <Suspense fallback={<p>加载中…</p>}>{current.render()}</Suspense>
    </main>
  );
}
