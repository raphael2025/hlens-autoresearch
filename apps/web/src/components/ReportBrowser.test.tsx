import assert from "node:assert/strict";
import { test } from "node:test";
import type { ReportEnvelope } from "../api";
import { fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { api, count, renderInitial, renderSettled } from "./render.test-util.tsx";
import { ReportBrowser } from "./ReportBrowser.tsx";

// The list + detail layout every report page shares, against the real validation_report fixture.

const KIND = "validation_report" as const;
const reports = fixtureEnvelopes(KIND);
const [fixture] = reports;
const LIST = `GET /api/reports/${KIND}`;
const DETAIL = `GET /api/reports/${KIND}/${fixture.id}`;

function browser(label?: (report: ReportEnvelope) => string) {
  return (
    <ReportBrowser
      kind={KIND}
      empty="（空列表）"
      prompt="选择一个报告。"
      label={label}
      renderDetail={(report) => <p data-detail={report.id}>detail of {report.id}</p>}
    />
  );
}

test("first paint: the list is loading, nothing is fetched yet, the detail pane shows the prompt", () => {
  const { html, requests } = renderInitial(browser());
  assert.deepEqual(requests, []);
  assert.ok(html.includes("加载报告列表中…"));
  assert.ok(html.includes("<p>选择一个报告。</p>"));
  assert.ok(!html.includes('role="alert"'));
});

test("loaded list: one button per report (default label = id), invalid files as a warning", async () => {
  const { html, requests } = await renderSettled(browser(), {
    routes: [api.listing(KIND, reports, [{ id: "broken", reason: "not valid JSON" }])],
  });
  assert.deepEqual(requests, [LIST]);
  assert.equal(count(html, "<button"), reports.length);
  assert.ok(html.includes(`>${fixture.id}</button>`));
  assert.ok(html.includes('aria-current="false"'));
  assert.ok(html.includes("1 个报告文件无效，未展示："));
  assert.ok(html.includes("<code>broken: not valid JSON</code>"));
  assert.ok(html.includes("<p>选择一个报告。</p>"), "nothing selected: the prompt, no detail request");
});

test("a custom label replaces the id", async () => {
  const { html } = await renderSettled(browser((report) => `label:${report.id.slice(0, 6)}`), {
    routes: [api.listing(KIND, reports)],
  });
  assert.ok(html.includes(`>label:${fixture.id.slice(0, 6)}</button>`));
  assert.ok(!html.includes('role="alert"'), "no invalid files: no warning");
});

test("selected report: the detail pane renders renderDetail(envelope) from GET /reports/{kind}/{id}", async () => {
  const { html, requests } = await renderSettled(browser(), {
    routes: [api.listing(KIND, reports), api.report(fixture)],
    selected: fixture.id,
  });
  assert.deepEqual(requests.sort(), [LIST, DETAIL].sort());
  assert.ok(html.includes(`<p data-detail="${fixture.id}">detail of ${fixture.id}</p>`));
  assert.ok(html.includes('aria-current="true"'));
  assert.ok(!html.includes("选择一个报告。"));
});

test("empty listing: the empty text; invalid files are still warned about", async () => {
  const empty = await renderSettled(browser(), { routes: [api.listing(KIND, [])] });
  assert.ok(empty.html.includes("（空列表）"));
  assert.equal(count(empty.html, "<button"), 0);

  const onlyInvalid = await renderSettled(browser(), {
    routes: [api.listing(KIND, [], [{ id: "x", reason: "bad" }])],
  });
  assert.ok(onlyInvalid.html.includes("（空列表）"));
  assert.ok(onlyInvalid.html.includes("<code>x: bad</code>"));
});

test("listing error: the server's detail in an alert, no list, no empty text", async () => {
  const { html } = await renderSettled(browser(), {
    routes: [api.fail("GET", `/reports/${KIND}`, 503, "no reports root configured")],
  });
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes("后端未配置该数据源（HTTP 503）：no reports root configured"));
  assert.ok(!html.includes("（空列表）"));
});

test("API unreachable: the network error is named, not shown as empty", async () => {
  const { html } = await renderSettled(browser(), {
    routes: [api.unreachable("GET", `/reports/${KIND}`, "fetch failed")],
  });
  assert.ok(html.includes("无法连接 API：fetch failed"));
  assert.ok(!html.includes("（空列表）"));
});

test("detail error: the list stays, the detail pane shows the 404", async () => {
  const { html } = await renderSettled(browser(), {
    routes: [
      api.listing(KIND, reports),
      api.fail("GET", `/reports/${KIND}/${fixture.id}`, 404, "report not found"),
    ],
    selected: fixture.id,
  });
  assert.equal(count(html, "<button"), reports.length);
  assert.ok(html.includes("不存在（HTTP 404）：report not found"));
});
