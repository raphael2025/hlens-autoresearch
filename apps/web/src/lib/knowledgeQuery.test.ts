import assert from "node:assert/strict";
import { test } from "node:test";
import { buildKnowledgeQuery, filtersByMetadata, KNOWLEDGE_TOKEN, tokenListErrors } from "./knowledgeQuery.ts";

const form = (terms: string, tags = "", assets = "") => ({ terms, tags, assets });

test("terms only: the pre-ADR-0055 body, no filter keys", () => {
  assert.deepEqual(buildKnowledgeQuery(form("momentum  carry")), {
    ok: true,
    query: { terms: ["momentum", "carry"], limit: 50 },
  });
});

test("canonical tag / asset lists are sent as typed: tags_all (AND) and assets_any (OR)", () => {
  const built = buildKnowledgeQuery(form("momentum", " momentum  trend ", "btc crypto"));
  assert.deepEqual(built, {
    ok: true,
    query: { terms: ["momentum"], tags_all: ["momentum", "trend"], assets_any: ["btc", "crypto"], limit: 50 },
  });
  assert.equal(JSON.stringify(built.ok && built.query), '{"terms":["momentum"],"tags_all":["momentum","trend"],"assets_any":["btc","crypto"],"limit":50}');
  assert.ok(built.ok && filtersByMetadata(built.query));
  assert.ok(!filtersByMetadata({ terms: ["x"], limit: 50 }));
});

test("the token grammar is the backend's KNOWLEDGE_TOKEN_PATTERN", () => {
  for (const good of ["momentum", "equity_index", "52_week_high", "btc"]) assert.ok(KNOWLEDGE_TOKEN.test(good), good);
  for (const bad of ["Momentum", "BTCUSDT", "btc-usdt", "btc/usdt", "_btc", "btc_", "btc__usdt", "bitcoiné", "ｂtc", "btc,eth", ""]) {
    assert.ok(!KNOWLEDGE_TOKEN.test(bad), bad);
  }
});

test("non-canonical values are refused, never case-folded", () => {
  const built = buildKnowledgeQuery(form("x", "Momentum", "BTCUSDT"));
  assert.equal(built.ok, false);
  assert.ok(!built.ok);
  assert.equal(built.errors.length, 2);
  assert.match(built.errors[0], /标签（全部满足）："Momentum" 不是规范值/);
  assert.match(built.errors[1], /资产（任一满足）："BTCUSDT" 不是规范值/);
});

test("unordered lists are refused with the ascending order as a hint, never sorted", () => {
  const built = buildKnowledgeQuery(form("x", "trend momentum"));
  assert.ok(!built.ok);
  assert.deepEqual(built.errors, ["标签（全部满足）：必须按升序填写（不会自动排序），例如 momentum trend"]);
});

test("duplicates are refused, never de-duplicated", () => {
  assert.deepEqual(tokenListErrors("资产", ["btc", "btc"]), ['资产：重复的值 "btc"']);
  const built = buildKnowledgeQuery(form("x", "", "btc eth btc"));
  assert.ok(!built.ok);
  assert.deepEqual(built.errors, ['资产（任一满足）：重复的值 "btc"']);
});

test("a comma is not a separator: it makes the token invalid", () => {
  const built = buildKnowledgeQuery(form("x", "", "btc,eth"));
  assert.ok(!built.ok);
  assert.match(built.errors[0], /"btc,eth" 不是规范值/);
});
