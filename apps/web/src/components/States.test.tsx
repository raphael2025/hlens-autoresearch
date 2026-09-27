import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { LoadState } from "../lib/loadState.ts";
import { AsyncView, Empty, ErrorState, InvalidReports, Loading } from "./States.tsx";

// The shared loading / empty / error / invalid-file states every page renders through: each state
// looks different, and none of them is silently blank.

function view(state: LoadState<string[]>, idle?: string): string {
  return renderToStaticMarkup(
    <AsyncView
      state={state}
      what="报告"
      isEmpty={(items) => items.length === 0}
      empty="（空）"
      idle={idle}
    >
      {(items) => <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>}
    </AsyncView>,
  );
}

test("Loading is a status line naming what is loading", () => {
  assert.equal(renderToStaticMarkup(<Loading />), '<p role="status" style="color:#555">加载数据中…</p>');
  assert.match(renderToStaticMarkup(<Loading what="报告列表" />), /role="status".*>加载报告列表中…</);
});

test("ErrorState is an alert that keeps the message verbatim (escaped, line breaks kept)", () => {
  const html = renderToStaticMarkup(<ErrorState message={"不存在（HTTP 404）：<x> & y\nnext"} />);
  assert.match(html, /^<p role="alert" style="color:crimson;white-space:pre-wrap">/);
  assert.ok(html.includes("不存在（HTTP 404）：&lt;x&gt; &amp; y\nnext"));
});

test("Empty renders its children, not an error or a spinner", () => {
  const html = renderToStaticMarkup(<Empty>（无报告）</Empty>);
  assert.equal(html, '<p style="color:#555">（无报告）</p>');
});

test("InvalidReports renders nothing without invalid files", () => {
  assert.equal(renderToStaticMarkup(<InvalidReports invalid={[]} />), "");
});

test("InvalidReports lists every invalid file as `id: reason` in an alert", () => {
  const html = renderToStaticMarkup(
    <InvalidReports
      invalid={[
        { id: "bad-1", reason: "payload is not a JSON object" },
        { id: "bad-2", reason: "id does not match content hash <x>" },
      ]}
    />,
  );
  assert.match(html, /^<div role="alert"/);
  assert.ok(html.includes("<strong>2 个报告文件无效，未展示：</strong>"));
  assert.ok(html.includes("<li><code>bad-1: payload is not a JSON object</code></li>"));
  assert.ok(html.includes("<li><code>bad-2: id does not match content hash &lt;x&gt;</code></li>"));
});

test("AsyncView: idle renders the idle node (nothing when none given)", () => {
  assert.equal(view({ status: "idle" }, "选择一个报告"), "选择一个报告");
  assert.equal(view({ status: "idle" }), "");
});

test("AsyncView: loading renders Loading with `what`", () => {
  assert.equal(view({ status: "loading" }), '<p role="status" style="color:#555">加载报告中…</p>');
});

test("AsyncView: error renders ErrorState, never the children or the empty text", () => {
  const html = view({ status: "error", error: "后端未配置该数据源（HTTP 503）：no root" });
  assert.match(html, /role="alert"/);
  assert.ok(html.includes("后端未配置该数据源（HTTP 503）：no root"));
  assert.ok(!html.includes("（空）"));
  assert.ok(!html.includes("<ul>"));
});

test("AsyncView: ok + isEmpty renders the empty text; default text without one", () => {
  assert.equal(view({ status: "ok", data: [] }), '<p style="color:#555">（空）</p>');
  const html = renderToStaticMarkup(
    <AsyncView state={{ status: "ok", data: [] as number[] }} isEmpty={(d) => d.length === 0}>
      {() => "never"}
    </AsyncView>,
  );
  assert.equal(html, '<p style="color:#555">（无数据）</p>');
});

test("AsyncView: ok with data renders the children with that data", () => {
  assert.equal(view({ status: "ok", data: ["a", "b"] }), "<ul><li>a</li><li>b</li></ul>");
});
