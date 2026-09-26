import { useState } from "react";
import { searchKnowledge, type KnowledgeQuery } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView } from "../components/States";
import { useApi } from "../lib/useApi";

// apps/api answers 503 when no knowledge provider is configured and 502 when the provider cannot
// answer honestly; both reach the error state with the server's `detail` (src/lib/errors.ts).
export function KnowledgeSearch() {
  const [terms, setTerms] = useState<string>("momentum");
  const [query, setQuery] = useState<Partial<KnowledgeQuery> | null>(null);
  // a new object per click, so searching the same terms again re-runs the request
  const result = useApi(query === null ? null : () => searchKnowledge(query), [query]);

  const search = () => setQuery({ terms: terms.split(/\s+/).filter(Boolean), limit: 50 });

  return (
    <section>
      <SimulatedBanner />
      <h2>知识检索（待检验主张，不是已验证结论）</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          search();
        }}
      >
        <input value={terms} onChange={(e) => setTerms(e.target.value)} />
        <button type="submit">检索</button>
      </form>
      <AsyncView
        state={result}
        what="检索结果"
        isEmpty={(body) => body.items.length === 0}
        empty="（没有匹配的知识条目）"
        idle={<p style={{ color: "#555" }}>输入关键词后检索。</p>}
      >
        {(body) => (
          <>
            <p style={{ color: "#555", fontSize: 13 }}>
              provider {body.provider} · {body.items.length} 条 · result_hash{" "}
              <code>{body.result_hash.slice(0, 12)}…</code>
            </p>
            <ul>
              {body.items.map((item) => (
                <li key={`${item.name}@${item.version}`}>
                  <strong>{item.name}</strong> [{item.evidence_level} · {item.status}] — {item.claim}
                  <br />
                  <small>{item.source}</small>
                </li>
              ))}
            </ul>
          </>
        )}
      </AsyncView>
    </section>
  );
}
