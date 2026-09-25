import { useEffect, useState } from "react";

// Framework skeleton (ADR-0048, NOT_VALIDATED): health, lifecycle transitions and knowledge
// search through apps/api. Types come from `npm run gen:api` (src/api.d.ts) once installed.
type KnowledgeItem = { name: string; claim: string; source: string; evidence_level: string; status: string };

export function App() {
  const [health, setHealth] = useState<string>("…");
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [terms, setTerms] = useState<string>("momentum");

  useEffect(() => {
    fetch("/api/health").then((r) => r.json()).then((b) => setHealth(b.status)).catch(() => setHealth("down"));
  }, []);

  const search = () => {
    fetch("/api/knowledge/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms: terms.split(/\s+/).filter(Boolean), limit: 50 }),
    })
      .then((r) => r.json())
      .then((b) => setItems(b.items ?? []))
      .catch(() => setItems([]));
  };

  return (
    <main style={{ fontFamily: "system-ui", padding: 16, maxWidth: 960, margin: "0 auto" }}>
      <h1>HLENS Research Console</h1>
      <p>API: {health}</p>
      <section>
        <h2>知识库检索（待检验主张，不是结论）</h2>
        <input value={terms} onChange={(e) => setTerms(e.target.value)} />
        <button onClick={search}>检索</button>
        <ul>
          {items.map((item) => (
            <li key={item.name}>
              <strong>{item.name}</strong> [{item.evidence_level} · {item.status}] — {item.claim}
              <br />
              <small>{item.source}</small>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
