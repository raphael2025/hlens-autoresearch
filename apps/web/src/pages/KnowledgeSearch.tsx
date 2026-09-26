import { useContext, useState } from "react";
import { searchKnowledge, type KnowledgeQuery } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView } from "../components/States";
import { InitialSelectionContext } from "../lib/initialSelection";
import { buildKnowledgeQuery, filtersByMetadata, type KnowledgeForm } from "../lib/knowledgeQuery";
import { useApi } from "../lib/useApi";

const UNCATEGORIZED_NOTE =
  "按标签 / 资产筛选为空，可能只说明条目尚未分类：仓库种子条目目前没有经人工审阅的标签 / 资产，" +
  "这不表示不存在相关知识。";

// apps/api answers 503 when no knowledge provider is configured, 502 when the provider cannot
// answer honestly and 422 when it refuses the query; all reach the error state with the server's
// `detail` (src/lib/errors.ts). Tag / asset lists are checked first by src/lib/knowledgeQuery.ts
// with the same strict rules (no case folding, sorting or de-duplication): a list that is not
// canonical is shown as a form error and nothing is sent.
export function KnowledgeSearch({
  initialTags = "",
  initialAssets = "",
}: {
  /** Component-test seam, like `InitialSelectionContext`: the filters the page starts with. */
  initialTags?: string;
  initialAssets?: string;
} = {}) {
  // `submitted` is null in the console (nothing searched yet); only component tests set it.
  const submitted = useContext(InitialSelectionContext);
  const [form, setForm] = useState<KnowledgeForm>({
    terms: submitted ?? "momentum",
    tags: initialTags,
    assets: initialAssets,
  });
  const initial = submitted === null ? null : buildKnowledgeQuery(form);
  const [query, setQuery] = useState<Partial<KnowledgeQuery> | null>(
    initial !== null && initial.ok ? initial.query : null,
  );
  const [problems, setProblems] = useState<string[]>(
    initial !== null && !initial.ok ? initial.errors : [],
  );
  // a new object per click, so searching the same terms again re-runs the request
  const result = useApi(query === null ? null : () => searchKnowledge(query), [query]);

  const search = () => {
    const built = buildKnowledgeQuery(form);
    setProblems(built.ok ? [] : built.errors);
    setQuery(built.ok ? built.query : null);
  };
  const field = (name: keyof KnowledgeForm) => ({
    value: form[name],
    onChange: (e: { target: { value: string } }) => setForm({ ...form, [name]: e.target.value }),
  });

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
        <label>
          关键词 <input name="terms" {...field("terms")} />
        </label>{" "}
        <label>
          标签（全部满足） <input name="tags_all" placeholder="momentum trend" {...field("tags")} />
        </label>{" "}
        <label>
          资产（任一满足，精确） <input name="assets_any" placeholder="btc crypto" {...field("assets")} />
        </label>{" "}
        <button type="submit">检索</button>
      </form>
      <p style={{ color: "#555", fontSize: 13 }}>
        标签 / 资产用空格分隔，须为小写 snake case 并按升序填写（不会自动改写）；资产是研究范围标识，不是交易所符号或上市记录。
      </p>
      {problems.length > 0 ? (
        <div role="alert" style={{ color: "crimson" }}>
          <p>检索条件不合法，未发送请求：</p>
          <ul>
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <AsyncView
        state={result}
        what="检索结果"
        isEmpty={(body) => body.items.length === 0}
        empty={
          query !== null && filtersByMetadata(query)
            ? `（没有匹配的知识条目）${UNCATEGORIZED_NOTE}`
            : "（没有匹配的知识条目）"
        }
        idle={problems.length > 0 ? null : <p style={{ color: "#555" }}>输入关键词后检索。</p>}
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
                  {(item.tags ?? []).length > 0 ? <> · 标签 {(item.tags ?? []).join(" ")}</> : null}
                  {(item.assets ?? []).length > 0 ? <> · 资产 {(item.assets ?? []).join(" ")}</> : null}
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
