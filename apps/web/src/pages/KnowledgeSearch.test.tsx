import assert from "node:assert/strict";
import { test } from "node:test";
import type { KnowledgeItem, KnowledgeResult } from "../api";
import { api, escaped, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { KnowledgeSearch } from "./KnowledgeSearch.tsx";

const ITEM: KnowledgeItem = {
  kind: "knowledge",
  name: "momentum-persists",
  version: "1.0.0",
  schema_version: "2.0.0",
  claim: "Momentum persists after <high> volume days",
  conditions: [],
  evidence_level: "E1",
  status: "unverified",
  source: "paper: example 2020",
  license: "CC-BY-4.0",
  lineage: [],
  links: [],
  // ADR-0055 (2.2.0): required in the generated type (a schema default); apps/api omits them when empty
  tags: [],
  assets: [],
};
const RESULT: KnowledgeResult = {
  items: [ITEM],
  provider: "local-json",
  query_hash: "1".repeat(64),
  result_hash: "2".repeat(64),
  schema_version: "1.0.0",
};

test("first paint: idle prompt with the default terms, nothing searched", () => {
  const { html, requests } = renderInitial(<KnowledgeSearch />);
  assert.deepEqual(requests, []);
  assert.ok(html.includes("SIMULATED / NOT_VALIDATED"));
  assert.ok(html.includes('value="momentum"'));
  assert.ok(html.includes("输入关键词后检索。"));
});

test("without a submitted search, settling issues no request (idle, not loading)", async () => {
  const { html, requests } = await renderSettled(<KnowledgeSearch />, { routes: [] });
  assert.deepEqual(requests, []);
  assert.ok(html.includes("输入关键词后检索。"));
  assert.ok(!html.includes('role="status"'));
});

test("submitted terms: POST /knowledge/search with the split terms; items listed as claims", async () => {
  const { html, requests } = await renderSettled(<KnowledgeSearch />, {
    routes: [api.knowledge(RESULT)],
    selected: "momentum  carry",
  });
  assert.deepEqual(requests, ['POST /api/knowledge/search {"terms":["momentum","carry"],"limit":50}']);
  assert.ok(html.includes('value="momentum  carry"'));
  assert.ok(html.includes(`provider local-json · 1 条 · result_hash <code>${"2".repeat(12)}…</code>`));
  assert.ok(html.includes(`<strong>${ITEM.name}</strong> [E1 · unverified] — ${escaped(ITEM.claim)}`));
  assert.ok(html.includes(`<small>${ITEM.source}</small>`));
});

test("no match: the empty text", async () => {
  const { html } = await renderSettled(<KnowledgeSearch />, {
    routes: [api.knowledge({ ...RESULT, items: [] })],
    selected: "nothing",
  });
  assert.ok(html.includes("（没有匹配的知识条目）"));
});

test("503 (no provider) and 502 (provider cannot answer honestly) are errors, not empty results", async () => {
  const none = await renderSettled(<KnowledgeSearch />, {
    routes: [api.fail("POST", "/knowledge/search", 503, "no knowledge provider configured")],
    selected: "momentum",
  });
  assert.ok(none.html.includes("后端未配置该数据源（HTTP 503）：no knowledge provider configured"));
  assert.ok(!none.html.includes("（没有匹配的知识条目）"));

  const dishonest = await renderSettled(<KnowledgeSearch />, {
    routes: [api.fail("POST", "/knowledge/search", 502, "provider returned a truncated index")],
    selected: "momentum",
  });
  assert.ok(dishonest.html.includes("上游 provider 无法诚实回答（HTTP 502）：provider returned a truncated index"));
});

// --- ADR-0055: tag (AND) / asset (OR, exact) filters -------------------------------------------

test("submitted filters: POST /knowledge/search carries tags_all and assets_any exactly as typed", async () => {
  const tagged: KnowledgeItem = { ...ITEM, tags: ["momentum", "trend"], assets: ["btc", "crypto"] };
  const { html, requests } = await renderSettled(
    <KnowledgeSearch initialTags="momentum trend" initialAssets="btc crypto" />,
    { routes: [api.knowledge({ ...RESULT, items: [tagged] })], selected: "momentum" },
  );
  assert.deepEqual(requests, [
    'POST /api/knowledge/search {"terms":["momentum"],"tags_all":["momentum","trend"],"assets_any":["btc","crypto"],"limit":50}',
  ]);
  assert.ok(html.includes('value="momentum trend"') && html.includes('value="btc crypto"'));
  assert.ok(html.includes("标签 momentum trend") && html.includes("资产 btc crypto"));
});

test("invalid filters: no request, the reasons shown as an alert (no case folding, sorting or dedup)", async () => {
  const { html, requests } = await renderSettled(
    <KnowledgeSearch initialTags="trend momentum" initialAssets="BTC btc btc" />,
    { routes: [], selected: "momentum" },
  );
  assert.deepEqual(requests, []);
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes("检索条件不合法，未发送请求："));
  assert.ok(html.includes(escaped("标签（全部满足）：必须按升序填写（不会自动排序），例如 momentum trend")));
  assert.ok(html.includes(escaped('资产（任一满足）："BTC" 不是规范值')));
  assert.ok(html.includes(escaped('资产（任一满足）：重复的值 "btc"')));
  assert.ok(!html.includes("输入关键词后检索。"));
});

test("a filtered search with no match says the seed may simply be uncategorized", async () => {
  const filtered = await renderSettled(<KnowledgeSearch initialAssets="btc" />, {
    routes: [api.knowledge({ ...RESULT, items: [] })],
    selected: "momentum",
  });
  assert.ok(filtered.html.includes("（没有匹配的知识条目）按标签 / 资产筛选为空，可能只说明条目尚未分类"));
  const plain = await renderSettled(<KnowledgeSearch />, {
    routes: [api.knowledge({ ...RESULT, items: [] })],
    selected: "momentum",
  });
  assert.ok(!plain.html.includes("尚未分类"));
});

test("a 422 from apps/api (a query it refuses) is an error with the server's reasons", async () => {
  const { html } = await renderSettled(<KnowledgeSearch initialTags="momentum" />, {
    routes: [api.fail("POST", "/knowledge/search", 422, "tags_all 必须严格升序")],
    selected: "momentum",
  });
  assert.ok(html.includes(escaped("数据不合法（HTTP 422）：tags_all 必须严格升序")));
  assert.ok(!html.includes("（没有匹配的知识条目）"));
});
