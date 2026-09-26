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
| POST | `/knowledge/search` | 经 `KnowledgeProvider` 检索知识条目（只读查询；未注入 provider → 503，provider 抛 `KnowledgeProviderError` → 502） |
| GET | `/reports/{kind}` | 按种类列出报告：`{kind, reports, invalid}`（见下） |
| GET | `/reports/{kind}/{id}` | 取单个报告详情 |
| GET | `/jobs` | worker 持久结果日志里的全部任务（见下；未配置 → 503，日志校验失败 → 500） |
| GET | `/jobs/{job_id}` | 单个任务（`job_id` 非 64 位小写十六进制 → 400，不存在 → 404） |

`{kind}` ∈ `validation_report` \| `research_loop_round` \| `state_strategy_matrix` \| `router_paper_run` \|
`gate_calibration` \| `router_stop` \| `state_diagnostics` \| `event_statistics` \| `paper_deviation`（后四种 2026-09-26 加入，
CODE_COMPLETE / DEBUG_PENDING：Phase 10 路由停止记录、Phase 2 状态稳定性诊断、Phase 3 事件统计；写入方见
`research/reports/README.md`）。除 `state_strategy_matrix` 外，每个 kind 都做契约 / 身份校验（见下）。

## Report 端点（研究控制台，2026-09-25 新增）

`apps/api` 不 import `research/`（H 边界）：研究平面把产出物写成 JSON 文件到一个配置目录，
`apps/api/store.py` 的 `ReportStore` 只读取这些文件并以最小信封返回：

```json
{"kind": "...", "id": "...", "created": "...", "payload": {...}, "content_hash": "sha256(...)"}
```

- `create_app(reports_root=None)`（默认）：所有 `/reports/*` 端点返回空列表 / 404，不报错。
- 目录约定：`<reports_root>/<kind>/<id>.json`，每个文件一个 JSON 对象。`research_loop_round` 必须是合法的
  `LoopRoundRecord`（ADR-0050）且 `id` 等于其 `record_hash`；其他 kind 的校验见下节。不通过即 malformed——
  列表中列入 `invalid`、单条读取返回 422。
- `content_hash`：payload 规范 JSON（排序键、紧凑分隔符）的 SHA-256。
- 拒绝路径穿越：`id` 必须匹配安全文件名模式且解析后仍在对应 `kind` 目录内，否则 400；未知
  `kind` 由 FastAPI 的枚举校验直接 422；损坏的 JSON 文件在列表接口中被跳过，在详情接口中报错。
- 目前没有任何写端点；实验登记 / 生命周期推进留待 P7 / P8 / P11 框架与授权服务就绪后再暴露。

## 错误映射、报告列表与任务端点（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

仍然只读（ADR-0048）：唯一的 POST 是知识检索（查询体，不写任何东西），测试
`test_the_api_has_no_mutation_endpoints_besides_the_read_only_search` 固定这一点。

- **错误体**：本层自己抛出的错误一律 `{"detail": "<message>"}`（OpenAPI 中的 `ApiError`）；只有
  FastAPI 自身的请求校验以 422 + 列表形式的 `detail` 回答。400 非法 id / 404 不存在 / 422 损坏的报告文件 /
  500 任务日志校验失败 / 502 知识 provider 无法诚实回答 / 503 未配置 provider 或任务日志。503 的 `detail`
  是稳定常量（`KNOWLEDGE_NOT_CONFIGURED`、`JOBS_NOT_CONFIGURED`）。
- **知识检索**：`response_model=KnowledgeResult`（OpenAPI 现在给出响应类型），不再以 200 返回 `{"error": ...}`。
- **报告列表**：`GET /reports/{kind}` 返回 `ReportListing` —— `reports`（合法报告，新到旧）+ `invalid`
  （`[{id, reason}]`：每个无法提供的文件及原因，不含服务器路径）。损坏文件不再被静默跳过；详情端点仍 422。
  `ReportStore.list` 仍只返回合法信封，`ReportStore.listing` 是列表端点背后的新方法。
- **任务端点**：`create_app(jobs_results=<path>, jobs_idempotent=(...))`。`jobs_results` 是 worker
  `JobRunner(results=...)` 的持久结果日志；每次请求经 `apps.worker.jobs.read_job_results` 重新读取并以**与运行器
  重开时同一套重放校验**核对（不缓存、不写入、缺失文件 = 空日志且不创建）。每个任务：`status`
  （`succeeded` / `failed` / `interrupted` —— 已开始无结果：运行中或中途死亡待审）、`attempts`、`result`、
  `error`、`starts`、`reruns`、`first_seq` / `last_seq`；列表另带 `head_hash` 与 `lines`。篡改 / 链断 /
  不合法的任务历史 → 500（`detail` 给出原因，路径只保留文件名），绝不返回部分数据。
- **部署要点：`jobs_idempotent` 必须与运行器的 `idempotent=` 完全一致。** 日志里一旦出现某处理器的
  `job_rerun` 行，而 API 没有声明该处理器幂等，重放会（与运行器一样）拒绝这段历史 → 每个 `/jobs` 请求都 500。
  API 没有处理器表，无法核对这些名字；这是 fail closed 的有意选择，不是 bug。
- 已知限制（DEBUG_PENDING）：读取与运行器的追加并发时可能读到半行 → 该次 500，重试即可；知识检索与任务端点
  尚未在真实部署中验证。

## 报告契约 / 身份校验与错误体（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`research/reports` 的每个写入方都用报告自身的内容身份命名文件。`ReportStore` 只依据 payload 字段
重新计算该身份（不 import `research/`，用 `core.domain.base.content_hash` 复述每个写入方的哈希规则），
文件名、记录的哈希与字段三者不一致即 malformed：

| kind | 校验（文件 id 必须等于该身份） |
|---|---|
| `validation_report` | 合法的 `core.domain.research.ValidationReport`，规范 JSON 往返一致；id = `content_hash()` |
| `router_paper_run` | `run_hash` = 其绑定字段的哈希（`research/router/paper.py` `_run_hash`） |
| `router_stop` | `stop_hash` = `{"kind": "router_stop", 其余全部字段}` 的哈希 |
| `state_diagnostics` | id = 整个 payload 的哈希（`diagnostics_hash`） |
| `event_statistics` / `gate_calibration` | `report_hash` = 去掉它之后 payload 的哈希 |
| `paper_deviation` | `deviation_hash` = 去掉它之后 payload 的哈希（Phase 10 纸面偏差，只描述） |

诚实边界：哈希不绑定的展示字段（路由运行的权益曲线、首末权益、每个决策的 `switching_cost`）不被核对；
`state_strategy_matrix` 仍不透明提供（其 `matrix_hash` 无法只凭 payload 重算）。

- **错误体不含服务器路径**：malformed 报告的 422 `detail` 为 `<kind>/<id> is malformed: <原因>`；本层所有
  `HTTPException` 经同一处理器把绝对路径缩成最后一段（`public_detail`），知识 provider 的 `OSError` 文本同样如此。
- **OpenAPI**：`GET /reports/{kind}/{id}` 的 422 声明为 `ApiError | HTTPValidationError`（存储拒绝，或未知
  `kind` 的请求校验）。`/health` → `Health`、`/contracts` → `ContractNames`、`/lifecycle/transitions` →
  `LifecycleTransition[]`，JSON 与之前逐字段相同。
