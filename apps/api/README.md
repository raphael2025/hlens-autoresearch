# apps/api

FastAPI 服务。职责：Registry / Experiment / Lifecycle 的 HTTP 入口，暴露 OpenAPI。只做编排与校验，不含业务规则。

> 框架已实现（ADR-0048，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`app.py` 的 `create_app`（`/health`、`/contracts`、`/lifecycle/transitions`、`/knowledge/search`），OpenAPI 由 `python -m apps.api.openapi` 导出到 `openapi.json`（测试校验其为最新）。

## 端点一览

只读、无鉴权、无下单 / 转账能力（H10）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + API 版本 |
| GET | `/contracts` | 已注册契约模型名列表 |
| GET | `/lifecycle/transitions` | 生命周期允许的转移表 |
| POST | `/knowledge/search` | 经 `KnowledgeProvider` 检索知识条目（未注入 provider 时返回 `{"error": ...}`） |
| GET | `/reports/{kind}` | 按种类列出报告（见下） |
| GET | `/reports/{kind}/{id}` | 取单个报告详情 |

`{kind}` ∈ `validation_report` \| `research_loop_round` \| `state_strategy_matrix` \| `router_paper_run`。

## Report 端点（研究控制台，2026-09-25 新增）

`apps/api` 不 import `research/`（H 边界）：研究平面把产出物写成 JSON 文件到一个配置目录，
`apps/api/store.py` 的 `ReportStore` 只读取这些文件并以最小信封返回：

```json
{"kind": "...", "id": "...", "created": "...", "payload": {...}, "content_hash": "sha256(...)"}
```

- `create_app(reports_root=None)`（默认）：所有 `/reports/*` 端点返回空列表 / 404，不报错。
- 目录约定：`<reports_root>/<kind>/<id>.json`，每个文件一个 JSON 对象（payload 内部结构不由本层解释；
  唯一例外是 `research_loop_round`：它必须是合法的 `LoopRoundRecord`（ADR-0050）且 `id` 等于其 `record_hash`，
  否则视为 malformed——列表跳过、单条读取返回 422）。
- `content_hash`：payload 规范 JSON（排序键、紧凑分隔符）的 SHA-256。
- 拒绝路径穿越：`id` 必须匹配安全文件名模式且解析后仍在对应 `kind` 目录内，否则 400；未知
  `kind` 由 FastAPI 的枚举校验直接 422；损坏的 JSON 文件在列表接口中被跳过，在详情接口中报错。
- 目前没有任何写端点；实验登记 / 生命周期推进留待 P7 / P8 / P11 框架与授权服务就绪后再暴露。
