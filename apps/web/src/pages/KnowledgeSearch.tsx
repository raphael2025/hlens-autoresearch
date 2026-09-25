import { useState } from "react";
import { searchKnowledge, type KnowledgeItem } from "../api";
import { SimulatedBanner } from "../components/Banner";

export function KnowledgeSearch() {
  const [terms, setTerms] = useState<string>("momentum");
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const search = () => {
    setError(null);
    searchKnowledge({ terms: terms.split(/\s+/).filter(Boolean), limit: 50 })
      .then((body) => setItems(body.items ?? []))
      .catch((err) => {
        setItems([]);
        setError(String(err));
      });
  };

  return (
    <section>
      <SimulatedBanner />
      <h2>知识检索（待检验主张，不是已验证结论）</h2>
      <div>
        <input value={terms} onChange={(e) => setTerms(e.target.value)} />
        <button onClick={search}>检索</button>
      </div>
      {error && <p style={{ color: "crimson" }}>{error}</p>}
      <ul>
        {items.map((item) => (
          <li key={`${item.name}@${item.version}`}>
            <strong>{item.name}</strong> [{item.evidence_level} · {item.status}] — {item.claim}
            <br />
            <small>{item.source}</small>
          </li>
        ))}
      </ul>
    </section>
  );
}
