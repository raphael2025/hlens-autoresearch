import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiRequestError, describeError, errorDetail } from "./errors.ts";

test("the ApiError body's detail is used verbatim", () => {
  assert.equal(errorDetail(503, { detail: "no knowledge provider is configured" }), "no knowledge provider is configured");
  assert.equal(
    errorDetail(502, { detail: "knowledge provider could not answer: items.json: unreadable item file" }),
    "knowledge provider could not answer: items.json: unreadable item file",
  );
});

test("FastAPI's request-validation 422 list is joined; anything else falls back to the status", () => {
  assert.equal(
    errorDetail(422, { detail: [{ msg: "field required", loc: ["body"] }, { msg: "bad kind" }] }),
    "field required; bad kind",
  );
  assert.equal(errorDetail(500, "<html>proxy error</html>"), "HTTP 500");
  assert.equal(errorDetail(500, null), "HTTP 500");
  assert.equal(errorDetail(404, { detail: "" }), "HTTP 404");
  assert.equal(errorDetail(422, { detail: [{ nope: 1 }] }), "HTTP 422");
});

test("knowledge search 503 / 502 read as not-configured / provider failure", () => {
  const notConfigured = describeError(
    new ApiRequestError("/knowledge/search", 503, "no knowledge provider is configured"),
  );
  assert.equal(notConfigured, "后端未配置该数据源（HTTP 503）：no knowledge provider is configured");
  const providerFailed = describeError(
    new ApiRequestError("/knowledge/search", 502, "knowledge provider could not answer: x"),
  );
  assert.match(providerFailed, /^上游 provider 无法诚实回答（HTTP 502）：knowledge provider could not answer: x$/);
});

test("a tampered job journal (500) and unknown statuses are still described", () => {
  assert.match(
    describeError(new ApiRequestError("/jobs", 500, "job results journal failed verification: x")),
    /^服务端校验失败（HTTP 500）/,
  );
  assert.match(describeError(new ApiRequestError("/x", 418, "teapot")), /^请求失败（HTTP 418）：teapot$/);
  assert.equal(describeError(new TypeError("Failed to fetch")), "无法连接 API：Failed to fetch");
  assert.equal(describeError("boom"), "无法连接 API：boom");
});

test("ApiRequestError keeps status, detail and path", () => {
  const error = new ApiRequestError("/jobs/abc", 400, "invalid job id: 'abc'");
  assert.equal(error.status, 400);
  assert.equal(error.detail, "invalid job id: 'abc'");
  assert.equal(error.path, "/jobs/abc");
  assert.ok(error instanceof Error);
  assert.equal(error.message, "/jobs/abc: HTTP 400 — invalid job id: 'abc'");
});
