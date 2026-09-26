# apps/web

研究控制台：React + TypeScript + Vite + Apache ECharts。只通过 OpenAPI 生成的客户端（`src/api.d.ts`
+ `src/api.ts`）访问 `apps/api`；不含任何下单 / 转账 / 实盘账户 UI（H10），每个页面都带
`SIMULATED / NOT_VALIDATED` 横幅。

> 框架已实现（ADR-0048，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED；实现说明 2026-09-25 补充，
> 控制台页面 2026-09-25 再补充，Gate Calibration 页面与剩余报告种类的 fixtures 2026-09-26 再补充）：
> 14 个只读页面 —— Dashboard、Validation Reports、Research Loop、State × Strategy Matrices、
> Router Paper Runs、Router Stops、Paper Deviation（2026-09-26）、Gate Calibration、State Diagnostics、Event Statistics（后三者与
> Router Stops 2026-09-26，CODE_COMPLETE / DEBUG_PENDING）、Degradation Checks（2026-09-26）、Lifecycle、Jobs（2026-09-26）、Knowledge Search。依赖已本地安装
> （`node_modules/`，已 gitignore），`npm run gen:api` 与 `npm run build` 均已跑通。

## 页面

| 页面 | 内容 | 数据来源 |
|---|---|---|
| Dashboard | 健康检查、契约数、各类报告计数 | `/health`、`/contracts`、`/reports/{kind}` |
| Validation Reports | 报告列表 + 详情（gate 结果表） | `/reports/validation_report[/​{id}]` |
| Research Loop | round 表（status、未完成阶段及其 error、round_usage / total_usage、overrun）+ 每个用量维度一张图（trials / llm_cost_units / compute_seconds，带单位；每轮用量柱在左轴、累计线在右轴） | `/reports/research_loop_round` |
| State × Strategy Matrices | 矩阵列表 + 详情（per-state 指标热力图、样本数） | `/reports/state_strategy_matrix[/​{id}]` |
| Router Paper Runs | 运行列表 + 详情（权重 / 切换时间线、switching-cost 前后权益对比；证据模式下的资格证据与验证报告绑定） | `/reports/router_paper_run[/​{id}]` |
| Router Stops | 停止记录列表 + 详情（停止原因、spec / 状态结果哈希、每个策略的 lifecycle / 结果哈希 / 验证报告绑定；证据模式下每个策略的资格核验结果与拒绝原因）；没有模拟任何运行 | `/reports/router_stop[/​{id}]` |
| Paper Deviation | 偏差报告列表 + 详情（运行 / 参照哈希、汇总统计、paper vs reference 权益图与差值柱、逐 mark 权益 / 收益差）；只描述、无阈值 | `/reports/paper_deviation[/​{id}]` |
| Gate Calibration | 报告列表 + 详情（每个候选 Profile、每个 gate 的 FPR / power 表，附 Clopper-Pearson 区间） | `/reports/gate_calibration[/​{id}]` |
| State Diagnostics | 诊断报告列表 + 详情（每个状态的计数 / 占比 / run 持续时间、转移矩阵、flicker、runs 表）；只描述、无阈值 | `/reports/state_diagnostics[/​{id}]` |
| Event Statistics | 报告列表 + 详情（来源事件运行哈希；频率分桶、共现、领先-滞后直方、重叠 / 独立性诊断）；只描述、非 Profile 输入 | `/reports/event_statistics[/​{id}]` |
| Degradation Checks | 退化检查列表 + 详情（subject、window、每个指标的状态 / 方向 / baseline / recent / decline / 允许下降 / 阈值来源）；missing = 证据不足而非健康；只作证据、不改生命周期 | `/reports/degradation_check[/​{id}]` |
| Lifecycle | 允许的状态转移表 | `/lifecycle/transitions` |
| Jobs | worker 结果日志的任务列表（按状态筛选）+ 详情（params、result / error），只读 | `/jobs[/​{job_id}]` |
| Knowledge Search | 知识条目检索（待检验主张，非结论） | `/knowledge/search` |

State × Strategy Matrices 与 Router Paper Runs 同样带 `SIMULATED / NOT_VALIDATED` 横幅（`src/components/Banner.tsx`）；Router Paper Runs 额外标注 PAPER ONLY —— 两者都不含任何下单 / 转账 / 实盘账户 UI（H10）。

Gate Calibration 页面同样带 `SIMULATED / NOT_VALIDATED` 横幅，并额外叠加一条更醒目的
`EvidenceOnlyBanner`（`src/components/Banner.tsx`，红底）："EVIDENCE ONLY — NOT A PROFILE
DECISION"：这些 FPR / power 数字是 Phase 9 校准证据（`research/synthetic_lab/gate_calibration.py`
的 `DISCLAIMER`），不是 Validation Profile 的数值来源。页面本身不给出、也不计算任何推荐值 / 默认值 /
最优值 —— 只原样展示 payload 里每个候选 Profile、每个 gate 的通过率与区间；不含任何下单 / 转账 /
实盘账户 UI（H10）。

## 开发 / 构建

```bash
cd apps/web
npm install                 # 项目本地安装，node_modules/ 已 gitignore；用 systemd-run 包裹见下
npm run gen:api              # 从 ../api/openapi.json 生成 src/api.d.ts（提交该生成文件）
npm run dev                  # 本地开发服务器；/api/* 反代到 http://127.0.0.1:8000（vite.config.ts）
npm run build                # tsc（应用 + 测试文件）&& vite build -> dist/
npm test                     # = npm run test:lib && npm run test:components（两套都跑）
npm run test:lib             # node --test src/lib/（Node 原生运行 TypeScript，无新依赖）
npm run test:components      # 组件测试：esbuild 打包 src/**/*.test.tsx 后 node --test（见下文「组件测试」）
```

内存受限环境下（WSL，16GB 共享）用 `systemd-run` 包裹每条 npm 命令：

```bash
systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0 npm install
systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0 npm run gen:api
systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0 npm run build
```

后端需单独启动才能让 `npm run dev` 的代理生效（`apps.api.create_app` 的一个工厂实例，`reports_root` 指向一个
写有 JSON 报告文件的目录）。这个目录由研究侧的 `research/reports`（[README](../../research/reports/README.md)）
写入 —— `apps/api` 从不 import `research/`，两边只通过 `<reports_root>/<kind>/<id>.json` 这份文件格式约定耦合。

项目**没有**选定或依赖任何 ASGI 生产服务器（uvicorn 不是项目依赖，留待后续决定）。本地开发可用仅供测试的最小
stdlib 服务器 `tests/apps/live_server.py`（只绑定 127.0.0.1，每连接一个请求，不是生产服务器）：

```bash
# 先用研究侧的 writer 把报告写进某个目录（例如 research.reports.write_validation_report(Path("var/reports"), report)），
# 再让 apps/api 指向同一个目录（仓库根目录下运行）：
uv run python -m tests.apps.live_server --port 8000 --reports-root var/reports
# 可选：--jobs-results <worker 结果日志> [--jobs-idempotent 名字 ...]、--knowledge docs/research/knowledge
```

`reports_root=None`（工厂默认值）等价于没有配置报告目录：所有 `/reports/*` 端点返回空列表 / 404，
而不是报错。

### 用 `apps/web/fixtures/` 快速起一个有数据的后端

`apps/web/fixtures/` 下按 `<kind>/<id>.json` 的真实报告目录布局提交了全部报告种类的示例
（`validation_report/`、`research_loop_round/`、`state_strategy_matrix/`、`router_paper_run/`、
`gate_calibration/`、`router_stop/`、`state_diagnostics/`、`event_statistics/`、`paper_deviation/`、`degradation_check/`），内容均由 `research/reports` 的真实 writer 对测试用固定对象生成 —— 与
`ReportStore` 实际读到的文件逐字节一致，不是手写的示例数据（生成方式见
[fixtures/README.md](fixtures/README.md)）。可以直接把它当 `reports_root` 起后端：

```bash
uv run python -m tests.apps.live_server --port 8000 --reports-root apps/web/fixtures
```

再在另一个终端 `npm run dev`，各报告页面都能看到数据（Jobs 需要另给 `--jobs-results <worker 结果日志>
[--jobs-idempotent <与运行器相同的名字>]`，即 `create_app(jobs_results=..., jobs_idempotent=...)`，否则显示 503；
Knowledge Search 需要 `--knowledge docs/research/knowledge`（注入 `LocalKnowledgeProvider`），否则显示 503）。
`tests/apps/test_console_fixtures.py` 保证每个 `ReportKind` 在这个目录下至少有一份 fixture，并且每份
都能通过 `ReportStore` 与 `/reports/...` 端点正常读回。

2026-09-26 审计补充：十种报告的 fixture 全部由提交的生成器产生，并由 `tests/apps/report_fixtures.py` 固定。
`validation_report`、`state_strategy_matrix`、`gate_calibration` 各保留一份契约 2.0.0 的**遗留** fixture（文件名是内容哈希，
不改名，列在 `LEGACY_2_0_0` 中；`regenerate_legacy()` 在新进程中以 2.0.0 版本作用域逐字节重建），并各新增一份 2.1.0 fixture
（validation 的一份带测试用精确门值）。报告解析在 `src/lib/validationReport.ts` / `stateStrategyMatrix.ts` /
`routerPaperRun.ts`（`node --test`）；Validation Reports 在有 `value_exact` / `threshold_exact` 时显示精确值，2.0.0 报告回退到浮点，
并显示 exact / float、`threshold_source` 与契约版本。Python 测试、node 测试与报告页面的详情测试读取每个种类的全部 fixture。

## 代码分割（Code splitting）

每个页面在 `src/App.tsx` 里用 `React.lazy` 单独懒加载：大多数页面都会拉入 ECharts
（`src/lib/echarts.ts`，只 `echarts/core` + 用到的 chart / component 子集，而不是整个包），把全部 14 个
页面都塞进入口 chunk 会让构建产物超过 500 kB 的警告阈值。Gate Calibration 页面本身不用 ECharts（纯
表格），所以它的 chunk 很小（约 4 kB），不需要额外拆分。`vite.config.ts` 的
`build.rollupOptions.output.manualChunks` 额外把 `zrender`（ECharts 的渲染层依赖）拆成独立 chunk ——
两者合在一个自动生成的共享 chunk 里时仍然单个超过 500 kB，分开后每个 chunk 都在阈值以下。
`npm run build` 应当不再出现 "chunks are larger than 500 kB" 的警告；如果新增页面又把某个 chunk
推过阈值，先检查是否可以复用 `src/lib/echarts.ts` 已经注册的 chart 类型，而不是加宽
`manualChunks` 或调高 `chunkSizeWarningLimit`。

## OpenAPI 类型

`src/api.d.ts` 是 `npm run gen:api` 的产物并已提交；改动 `apps/api` 的端点后必须先
`python -m apps.api.openapi`（重新导出 `apps/api/openapi.json`），再 `npm run gen:api` 重新生成,
两者都提交。`src/api.ts` 是在生成类型之上的一层薄 fetch 封装（唯一允许直接写 HTTP 调用的地方）。

## 页面状态、错误与测试（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

- **统一的加载 / 空 / 错误状态**：`src/lib/useApi.ts`（`useApi(load, deps)` → `LoadState`：`idle` / `loading` /
  `error` / `ok`，迟到的响应被丢弃）+ `src/components/States.tsx`（`AsyncView`、`Loading`、`ErrorState`、`Empty`、
  `InvalidReports`）。九个页面全部经由它们渲染请求；Dashboard 每个单元格独立显示自己的错误（`settle`），不再吞掉错误。
- **错误信息**：`src/api.ts` 对非 2xx 抛 `ApiRequestError`（status + 服务端 `detail`，`src/lib/errors.ts`），页面显示
  状态含义 + `detail`：Knowledge Search 的 503（未配置 provider）/ 502（provider 无法诚实回答）、Jobs 的 503 / 500
  （日志校验失败）等都原样可见。
- **报告列表新形状**：`GET /reports/{kind}` 返回 `{kind, reports, invalid}`；列表页（`src/components/ReportBrowser.tsx`，
  Validation / Matrices / Router / Gate Calibration 共用，Research Loop 直接使用）把 `invalid` 作为黄色警告列出
  （`id: reason`），Dashboard 计数注明无效文件数。
- **Research Loop 修复**：旧页面读取 `payload.budget_used` / `payload.failures`，而 `LoopRoundRecord`（ADR-0050）
  没有这两个字段（图表恒为 0）。现在读真实字段：`status`、`stages[].status/error`（未完成阶段及其错误、跳过数）、
  `round_usage` / `total_usage`（字符串小数被解析）、`overrun.stage`、`transitions` 数（`src/lib/researchLoop.ts`）。
- **Gate Calibration**：每个 arm 的 `detector_errors` 与每次运行的 `detector_error` 在存在时显示（为零时这两个键
  不存在 —— 不显示该列 / 该表）；仍然只展示证据，没有任何推荐值。
- **Jobs 页面**（只读）：`GET /jobs` 列表（按状态筛选、计数、`head_hash`、日志行数）+ `GET /jobs/{job_id}` 详情；
  没有提交 / 重试 / 取消控件。
- **测试（无新依赖）**：纯视图模型逻辑在 `src/lib/*.ts`（只 `import type` 引用 API 类型，Node 可直接剥离类型运行），
  由 `src/lib/*.test.ts` 用 `node --test` 测试（`npm test`），输入是 `apps/web/fixtures/` 的真实报告文件
  （`src/lib/fixtures.test-util.ts`；Jobs 的响应体在测试中内联构造，因为 fixtures 目录只放报告种类）。测试文件被
  `tsconfig.json` 排除、不进 vite 包；`tsconfig.test.json` + `test-types/node-test.d.ts`（几行 Node 内置模块的
  最小声明，代替 `@types/node`）让 `npm run build` 同时对测试文件做类型检查。`package-lock.json` 未变。
- **Router Stops / State Diagnostics / Event Statistics**（2026-09-26）：三个只读页面，同样经由
  `ReportBrowser` / `useApi` / `States.tsx`；视图模型在 `src/lib/routerStop.ts`、`src/lib/stateDiagnostics.ts`、
  `src/lib/eventStatistics.ts`（`node --test`，输入是对应 fixtures）。未定义值（`null`）显示为 “—”，从不显示为 0；
  未知的统计种类逐字段显示而不是丢弃。纯表格，不引入 ECharts。`src/api.ts` 的 `REPORT_KINDS` 现在以
  `Record<ReportKind, …>` 定义：`apps/api` 新增 kind 而控制台未列出时 `tsc` 直接失败（Dashboard 按它计数）。
- **路由资格证据显示**（P10-ELIG 证据模式，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：Router Stops 的停止原因
  （`no_validated_candidate` / `all_routes_flat` / `eligibility_not_evidenced`）以中文说明 + 原始值显示；当报告载荷带
  附加的 `eligibility` 键时，Router Stops 与 Router Paper Runs 详情多一张「资格证据」表（每个策略：声明的 lifecycle、
  报告哈希、subject、判定、G5 状态、核验结果 / 拒绝原因的中文说明 + 原始代码、detail），Router Paper Runs 另显示
  `validation_reports` 绑定；信任模式载荷（无该键）不显示任何额外内容。视图模型在 `src/lib/routerEligibility.ts`
  （`node --test`，证据模式载荷在测试中按 `research/reports/router.py` 的形状内联构造；fixtures 未改），表格组件在
  `src/components/EligibilityEvidence.tsx`。无法解析的检查记录被计数并警告，从不当作「无证据」。
- **Paper Deviation**（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：`src/pages/PaperDeviations.tsx`，经由
  `ReportBrowser` / `useApi` / `States.tsx`；视图模型 `src/lib/paperDeviation.ts`（`node --test`，输入是
  `fixtures/paper_deviation/`）。表格显示 payload 的精确十进制文本（`decimalText` 去掉 `0E-18` 之类的指数与尾零，不做舍入），
  不可计算值（`null`）显示为 “—”；ECharts 折线 / 柱图只用于图形。
- **Degradation Checks**（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：`src/pages/DegradationChecks.tsx`，经由
  `ReportBrowser` / `useApi` / `States.tsx`；视图模型 `src/lib/degradationCheck.ts`（`node --test`，输入是
  `fixtures/degradation_check/`）。指标按 breached → missing → within 排序；missing 明示「证据不足，不是健康」；
  每个阈值显示其来源。纯表格，不引入 ECharts。
- DEBUG_PENDING：尚未在浏览器中对真实后端逐页人工验证（已跑 `npm run build`、`npm test` 与下文的 live-backend
  smoke；后者不是浏览器验收）。

## 组件测试（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`npm run test:components`（`npm test` 也会跑）对共享组件与全部 14 个页面做渲染测试，**无新依赖**，
`package-lock.json` 未变：

- **运行器** `scripts/test-components.mjs`：Node 不能剥离 JSX，所以用已安装的 esbuild（vite 自带依赖）的 JS API
  把每个 `src/**/*.test.tsx` 打包成 ESM（platform node、jsx automatic、共享 chunk 拆分）到
  `node_modules/.component-tests/`（已 gitignore，每次清空；与 `src/lib/` 同为 apps/web 下两层，因此
  `fixtures.test-util.ts` 的相对路径照样指向 `apps/web/fixtures/`，产物平铺、测试文件不得重名），再
  `node --test` 运行。额外参数透传给 `node --test`（如 `npm run test:components -- --test-name-pattern=Jobs`）。
- **渲染**：`react-dom/server` 的 `renderToStaticMarkup`（不需要 DOM）。`src/components/render.test-util.tsx`：
  `renderInitial` = 浏览器首帧（全部请求 `loading`，且断言尚未发出任何请求）；`renderSettled` 安装 fetch stub，
  经 `useApi` 的测试接缝反复渲染直到所有请求都已完成，得到 loaded / empty / error 状态。
- **两个测试接缝（生产默认行为不变）**：`ApiSeedContext`（`src/lib/useApi.ts`：provider 可给出请求的初始状态；
  控制台从不提供，所以仍然从 `loading` 开始、由 effect 发请求）与 `InitialSelectionContext`
  （`src/lib/initialSelection.ts`：ReportBrowser / Jobs 的初始选中 id、Knowledge Search 的已提交检索词；
  默认 `null` = 未选中 / 未检索，与之前相同）。
- **覆盖**：`States.test.tsx`（Loading / ErrorState / Empty / InvalidReports / AsyncView 各状态）、
  `ReportBrowser.test.tsx`（列表 + 无效文件警告、默认 / 自定义 label、选中详情、空、列表错误、网络错误、详情 404）、
  `pages/reportPages.test.tsx`（9 个 ReportBrowser 页面逐一：首帧、真实 fixture 列表及其由 payload 推出的 label、
  选中 fixture 的详情确实被解析（不是原始 JSON 回退）、空、500、详情 404），以及 `Dashboard` / `ResearchLoop` /
  `Lifecycle` / `Jobs` / `KnowledgeSearch` 各自的首帧、loaded、empty、error（含 503 / 502 / 500 的状态含义）。
  报告页面的 loaded 状态用 `apps/web/fixtures/` 的真实报告；Jobs / Lifecycle / Knowledge / Dashboard 的非报告
  响应体在测试中内联构造。
- **API 契约检查**：stub 只通过 `render.test-util.tsx` 的 `api.*` 路由构造，每个响应体的类型取自
  `src/api.d.ts`（经 `src/api.ts`），路径与 `src/api.ts` 对应的客户端函数一致；`tsconfig.test.json` 包含
  `src/**/*.test.tsx` 与 `*.test-util.tsx`，所以 `npm run build` 的 `tsc -p tsconfig.test.json` 在 fixture / 内联
  响应体与页面解析所依据的 API 类型不符时直接失败。
- 局限：服务端渲染不执行 effect，ECharts 图表只验证容器存在，不验证绘制结果；点击等交互未覆盖（初始选中通过接缝注入）。

## Live-backend smoke（真实后端进程，无浏览器；2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`scripts/live-smoke.mjs`（`BASE_URL=http://127.0.0.1:<port> npm run smoke:live`，可选 `BARE_BASE_URL` = 一个未配置
知识 provider / 任务日志的后端）对**正在运行的真实 `apps/api`** 做一遍控制台侧检查，**无新依赖**：

1. 用已安装的 esbuild（同 `scripts/test-components.mjs`）把控制台自己的 `src/api.ts`、全部 `src/lib` 视图模型与
   全部 14 个页面打包到 `node_modules/.live-smoke/`（已 gitignore）；
2. 把 `fetch` 按开发代理的规则改写（`/api/<path>` → `BASE_URL/<path>`，同 `vite.config.ts`），调用控制台真实的
   客户端函数：每种报告的列表 + 每个详情、`/jobs` 列表 + 详情、知识检索、422 / 400 / 404，以及 `BARE_BASE_URL`
   上的两个 503（`ApiRequestError` + `describeError`）；
3. 在这些线上响应上运行每个页面的 `src/lib` 视图模型函数（payload 解析器不得拒绝、行 / 标签 / 图表序列可构建）；
4. 用 `react-dom/server` 渲染全部 14 个页面，经 `ApiSeedContext` 接缝以线上响应逐轮填充（与
   `render.test-util.tsx` 的 settle 循环相同，但走真实 HTTP）：没有残留的 loading、没有错误状态、报告页确实请求了
   列表与所选详情且没有退回原始 JSON；未配置的后端上 Knowledge Search / Jobs 页面显示 503 错误状态。

`tests/apps/test_live_backend_smoke.py` 启动三个真实后端子进程（`tests/apps/live_server.py`，仅测试用的最小
stdlib 服务器，127.0.0.1 + 临时端口），本脚本只对其中完整配置与未配置的两个运行（第三个 broken 服务器只由 pytest
检查 502 / 500 错误路径，见 [apps/api/README.md](../api/README.md)「Live-backend smoke」）；没有 `node` 或未安装 `node_modules` 时该用例带原因 skip。

**证明了什么**：真实 `apps/api` 进程经真实 socket 返回的 JSON，能被控制台自己的客户端、错误映射、视图模型和页面
组件（服务端渲染）完整消费，与 fixtures 上的测试结论一致。**没有证明什么**：没有真实浏览器 —— 不验证像素 / 布局 /
样式、不执行 effect（ECharts 实际绘制、`useApi` 的 effect 路径）、没有点击 / 切换 tab / 输入等交互、不经过 vite
开发代理本身。**人工浏览器验收仍然未完成（open）。**
