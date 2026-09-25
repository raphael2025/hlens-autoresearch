import { useState } from "react";
import { Dashboard } from "./pages/Dashboard";
import { KnowledgeSearch } from "./pages/KnowledgeSearch";
import { Lifecycle } from "./pages/Lifecycle";
import { ResearchLoop } from "./pages/ResearchLoop";
import { ValidationReports } from "./pages/ValidationReports";

// Research console (ADR-0048, NOT_VALIDATED): read-only pages over apps/api. No order or trading
// UI of any kind lives here or ever will (H10) — see each page's SIMULATED / NOT_VALIDATED banner.
const TABS = [
  { key: "dashboard", label: "Dashboard", render: () => <Dashboard /> },
  { key: "validation", label: "Validation Reports", render: () => <ValidationReports /> },
  { key: "research-loop", label: "Research Loop", render: () => <ResearchLoop /> },
  { key: "lifecycle", label: "Lifecycle", render: () => <Lifecycle /> },
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
      {current.render()}
    </main>
  );
}
