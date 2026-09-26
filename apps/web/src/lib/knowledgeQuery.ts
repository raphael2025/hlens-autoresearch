// The Knowledge Search form -> a `KnowledgeQuery` body (ADR-0055). Pure: no React, no fetch — tested
// by knowledgeQuery.test.ts with `node --test`.
//
// Tag / asset filters keep apps/api's strict semantics: each whitespace-separated token must already
// be canonical (ASCII lower-case snake case, `KNOWLEDGE_TOKEN_PATTERN` in core/domain/research.py),
// and a list must be strictly ascending with no duplicates. Nothing is case-folded, sorted or
// de-duplicated here: a list that is not canonical is refused with a message naming the problem, and
// no request is sent. `tags_all` = the item carries every tag (AND); `assets_any` = the item carries
// at least one asset, by exact equality (OR). Assets are research-scope ids (`crypto`, `btc`), not
// exchange symbols or listings.
import type { KnowledgeQuery } from "../api";

export const KNOWLEDGE_TOKEN = /^[a-z0-9]+(?:_[a-z0-9]+)*$/;

export type KnowledgeForm = { terms: string; tags: string; assets: string };

export type BuiltQuery =
  | { ok: true; query: Partial<KnowledgeQuery> }
  | { ok: false; errors: string[] };

function tokens(text: string): string[] {
  return text.split(/\s+/).filter(Boolean);
}

/** Problems of one tag / asset list; empty when it is canonical (or empty). */
export function tokenListErrors(label: string, values: readonly string[]): string[] {
  const errors: string[] = [];
  const invalid = values.filter((value) => !KNOWLEDGE_TOKEN.test(value));
  if (invalid.length > 0) {
    errors.push(
      `${label}：${invalid.map((value) => JSON.stringify(value)).join("、")} 不是规范值` +
        "（只允许小写 ASCII 字母、数字与单个下划线分隔，如 momentum、equity_index）",
    );
  }
  const duplicates = [...new Set(values.filter((value, index) => values.indexOf(value) !== index))];
  if (duplicates.length > 0) {
    errors.push(`${label}：重复的值 ${duplicates.map((value) => JSON.stringify(value)).join("、")}`);
  }
  const ascending = values.every((value, index) => index === 0 || values[index - 1] < value);
  if (duplicates.length === 0 && !ascending) {
    const sorted = [...values].sort();
    errors.push(`${label}：必须按升序填写（不会自动排序），例如 ${sorted.join(" ")}`);
  }
  return errors;
}

/** The request body for the form, or every reason it cannot be sent. */
export function buildKnowledgeQuery(form: KnowledgeForm): BuiltQuery {
  const tags = tokens(form.tags);
  const assets = tokens(form.assets);
  const errors = [...tokenListErrors("标签（全部满足）", tags), ...tokenListErrors("资产（任一满足）", assets)];
  if (errors.length > 0) return { ok: false, errors };
  const query: Partial<KnowledgeQuery> = { terms: tokens(form.terms) };
  if (tags.length > 0) query.tags_all = tags;
  if (assets.length > 0) query.assets_any = assets;
  query.limit = 50;
  return { ok: true, query };
}

/** Whether a query filters by tags or assets (an empty answer may then mean "not yet tagged"). */
export function filtersByMetadata(query: Partial<KnowledgeQuery>): boolean {
  return (query.tags_all?.length ?? 0) > 0 || (query.assets_any?.length ?? 0) > 0;
}
