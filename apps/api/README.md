# apps/api

FastAPI 服务。职责：Registry / Experiment / Lifecycle 的 HTTP 入口，暴露 OpenAPI。只做编排与校验，不含业务规则。

> 框架已实现（ADR-0048，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`app.py` 的 `create_app`（`/health`、`/healthz`、`/readyz`、`/contracts`、`/lifecycle/transitions`、`/knowledge/search`、`/reports/{kind}`、`/reports/{kind}/{id}`、`/jobs`、`/jobs/{job_id}`；见下方「端点一览」），OpenAPI 由 `python -m apps.api.openapi` 导出到 `openapi.json`（测试校验其为最新）。

## 端点一览

只读、无鉴权、无下单 / 转账能力（H10）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + API 版本 |
| GET | `/healthz` | 存活检查；只确认 API 进程可响应 |
| GET | `/readyz` | 就绪检查；只读核查已配置的知识、报告目录与任务日志读取源，失败返回 503 |
| GET | `/contracts` | 已注册契约模型名列表 |
| GET | `/lifecycle/transitions` | 生命周期允许的转移表 |
| POST | `/knowledge/search` | 经 `KnowledgeProvider` 检索知识条目（只读查询；未注入 provider → 503，provider 抛 `KnowledgeProviderError` → 502） |
| GET | `/reports/{kind}` | 按种类列出报告：`{kind, reports, invalid}`（见下） |
| GET | `/reports/{kind}/{id}` | 取单个报告详情 |
| GET | `/jobs` | worker 持久结果日志里的全部任务（见下；未配置 → 503，日志校验失败 → 500） |
| GET | `/jobs/{job_id}` | 单个任务（`job_id` 非 64 位小写十六进制 → 400，不存在 → 404） |

`{kind}` ∈ `validation_report` \| `research_loop_round` \| `state_strategy_matrix` \| `router_paper_run` \|
`gate_calibration` \| `router_stop` \| `state_diagnostics` \| `event_statistics` \| `paper_deviation` \| `degradation_check`（后五种 2026-09-26 加入，
CODE_COMPLETE / DEBUG_PENDING：Phase 10 路由停止记录、Phase 2 状态稳定性诊断、Phase 3 事件统计、Phase 10 纸面偏差、
Phase 11 退化检查、Phase 8 回溯审计；写入方见 `research/reports/README.md`）。除 `state_strategy_matrix` 外，每个 kind 都做契约 / 身份校验（见下）。

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
  `kind` 由 FastAPI 的枚举校验直接 422；损坏的 JSON 文件在列表接口中列入 `invalid`（`{id, reason}`，不再静默跳过），
  在详情接口中返回 422。
- API 不提供写入或触发研究作业的端点，也没有增加这类端点的计划（ADR-0048 实施决定及 [2026-09-26 决策记录](../../docs/reviews/2026-09-26-autonomous-decisions.md)）。Worker 作业在 API 之外运行；其持久结果日志与研究报告文件由 `/jobs`、`/reports` 只读提供给 Web。唯一的 POST `/knowledge/search` 是只读查询，不写入数据。

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
| `degradation_check` | `check_hash` = 去掉它之后 payload 的哈希（Phase 11 退化检查，只作证据） |
| `retro_audit` | `report_hash` = 去掉它之后 payload 的哈希（Phase 8 回溯审计；只读差异，不改变生命周期） |

诚实边界：哈希不绑定的展示字段（路由运行的权益曲线、首末权益、每个决策的 `switching_cost`）不被核对；
`state_strategy_matrix` 的 payload 形状按 ADR-0081 校验，但 `matrix_hash` 无法只凭 payload 重算。`retro_audit` 由研究侧 writer 对规范 payload 生成 `report_hash`，API 只按文件内容重算该哈希，不 import `research/`。

- **错误体不含服务器路径**：malformed 报告的 422 `detail` 为 `<kind>/<id> is malformed: <原因>`；本层所有
  `HTTPException` 经同一处理器把绝对路径缩成最后一段（`public_detail`），知识 provider 的 `OSError` 文本同样如此。
  2026-09-26 审计补充：`file:/…`、`file:///…`、`file://host/…` URI，`~/`、`~user/` 路径，以及紧跟冒号的路径也只保留
  文件名；`https://host/…` 与 `1/2` 之类的文本不变（`tests/apps/test_api.py`）。`file:` scheme 按 RFC 3986
  大小写不敏感匹配（`FILE:`、`FiLe:` 与小写同样脱敏，B46 复核修复），authority / 路径边界不变。
- **兜底 500**：任何未处理异常一律返回 500 `{"detail": "internal server error"}`，不带异常消息、路径或 traceback；
  报告读取的 `path.stat()` 移入同一错误保护内。因为兜底处理器作用于全部路由，每个 operation 都在 OpenAPI 中声明 500
  为 `ApiError`（`openapi.json` 与 `apps/web/src/api.d.ts` 已重新生成）。
- **只读边界测试**：遍历 `app.routes`（不只看 OpenAPI），只允许普通 HTTP GET / 知识检索 POST 路由；一个
  `include_in_schema=False` 的隐藏 DELETE 会被测试发现。
- **OpenAPI**：`GET /reports/{kind}/{id}` 的 422 声明为 `ApiError | HTTPValidationError`（存储拒绝，或未知
  `kind` 的请求校验）。`/health` → `Health`、`/contracts` → `ContractNames`、`/lifecycle/transitions` →
  `LifecycleTransition[]`，JSON 与之前逐字段相同。

## Live-backend smoke（真实进程 + 真实 HTTP，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`tests/apps/test_live_backend_smoke.py`（默认 `pytest` 运行，约 3 秒，不需要 PostgreSQL、不访问外网）：

- **数据**：临时报告目录 = `apps/web/fixtures/` 的副本（每个 `ReportKind`，均为 `research/reports` 真实 writer 的输出）
  + 一个损坏文件；真实 `JobRunner` 写的结果日志（succeeded / failed / interrupted 各一）；知识目录 =
  `docs/research/knowledge/` 的副本。
- **服务器**：`create_app(...)` 在**子进程**中运行（`python -m tests.apps.live_server --port 0 ...`），只绑定
  `127.0.0.1`、临时端口，等 `/health` 后测试，最后 SIGTERM 并要求退出码 0。`tests/apps/live_server.py` 是**仅供测试的
  最小 stdlib 服务器**（asyncio 解析简单 HTTP/1.1 请求，经 ASGI `http` 协议驱动真实应用，每连接一个请求、
  `Connection: close`）——不是生产服务器。本机运行时见下文「本机运行（Uvicorn，ADR-0063）」。
- **覆盖**：`httpx` 走真实 socket 调用 committed `openapi.json` 的**每个 operation**（全部至少一次 200）：每种报告的
  列表 + 每个详情、`invalid` 列表与损坏文件的 422、非法 id 400、不存在 404、未知 kind 422；`/jobs` 列表 / 详情 /
  400 / 404；知识检索 200 与请求校验 422；第二个未配置 provider / 日志的服务器给出知识检索与 `/jobs` 的 503。线上
  `/openapi.json` 必须与 committed 文件完全相同；每个响应的状态码必须在该 operation 中声明，响应体必须符合 committed
  schema（`jsonschema` 未安装：用覆盖该文档全部关键字的小型子集校验器，遇到未知关键字即失败），并且经应用声明的
  pydantic 响应模型校验后重新序列化必须与返回的 JSON 完全一致（多余字段也会被发现）。
- **错误路径（第三个 "broken" 服务器，同样是子进程 + 真实 HTTP）**：
  - **502**：`live_server.py` 的**仅测试用** `--knowledge-error MESSAGE` 参数注入一个 `search` 抛
    `KnowledgeProviderError(MESSAGE)` 的 provider（`apps/` 中没有任何注入钩子）；MESSAGE 含临时目录的绝对路径，
    `POST /knowledge/search` 的 `detail` 必须恰为 `knowledge provider could not answer: items.json: unreadable: [Errno 13]
    Permission denied: 'items.json'`。
  - **500（日志被篡改）**：真实 `JobRunner` 结果日志的副本，第 2 行 `prev_hash` 被改写（哈希链断裂）；`GET /jobs` 与
    `GET /jobs/{job_id}` 的 `detail` 必须恰为 `job results journal failed verification: results.jsonl:2 breaks the hash chain`。
  - **兜底 500**：`live_server.py` 的**仅测试用** `--fault-report-read MESSAGE` 参数只在该服务器进程内让
    `ReportStore._read` 抛 `RuntimeError(MESSAGE)`（无路由映射它；MESSAGE 含临时目录路径），于是真实路由
    `GET /reports/{kind}` 与 `GET /reports/{kind}/{report_id}` 都落入兜底处理器，body 必须恰为
    `{"detail": "internal server error"}`；同时断言异常与 traceback 只出现在服务器 stderr，确认走的确实是未处理异常路径。
    `apps/` 中没有注入钩子。（最初用超长整数字面量的报告文件触发——那其实是存储缺陷：`json.loads` 抛的普通
    `ValueError` / 过深嵌套的 `RecursionError` 未被映射，整类列表 500。已修复为 malformed 条目，
    `tests/apps/test_reports.py` 覆盖。`live_server.py` 像 ASGI 服务器那样处理 Starlette 在兜底处理器发出响应后
    重新抛出的异常：写 stderr，并把应用已发出的响应交给客户端。）
  - 每个用例：该状态码在 committed `openapi.json` 中对该 operation 声明为 `ApiError`，body 通过 schema 与
    `ApiError` 模型往返校验，且不含临时目录 / 仓库路径、`Traceback`、`File "`、`.py` 或异常类型名。兜底 500 的 body
    不含异常消息；502 与日志 500 的 `detail` 按设计带有**去路径后的**原因（见上文错误映射），测试逐字固定它。
- 随后在有 `node` 与已安装的 `apps/web/node_modules` 时运行 `apps/web/scripts/live-smoke.mjs`（控制台自己的客户端、
  视图模型与页面渲染，见 [apps/web/README.md](../web/README.md)「Live-backend smoke」），否则带原因 skip。

**不证明的内容**：502 只用测试服务器注入的失败 provider 触发，未用真实 `LocalKnowledgeProvider` 的故障（如不可读的
条目文件）触发；兜底 500 经真实 HTTP 只用注入的 `RuntimeError` 验证，没有已知的真实数据能触发它（能找到的都已作为
缺陷修复）；日志 500 只覆盖哈希链断裂这一种篡改；`live-smoke.mjs`（控制台侧）自 2026-09-27（B60）起也访问 broken
服务器（客户端 + 服务端渲染的 Knowledge Search / Jobs / State × Strategy Matrices 页面），但仍不是浏览器验证。没有并发 / 长连接 / 性能测试；也不代表任何生产服务器
配置已被验证。

## 报告 payload DTO（ADR-0081，2026-09-28）

`apps/api/report_dto.py` 为全部 11 种 `ReportKind` 注册基线版本、可读取版本和必需字段；API 对已知 DTO 执行结构与现有身份校验。已有 `schema_version` 原样使用；历史 payload 没有该字段的 kind 使用注册表里的虚拟基线版本，不向文件注入字段、不改变其哈希。已知版本缺字段会按 malformed 处理（列表 `invalid`、详情 422）。未知版本以 JSON 原样只读透传并计算 envelope `content_hash`，不假设当前版本的 kind 身份规则；Web 仅显示原始 JSON 和版本提示，不把它解释成已知 DTO。版本与兼容矩阵见 [ADR-0081](../../docs/adr/0081-versioned-report-payload-dtos.md)。

`paper_deviation` 的 2.0.0 DTO 必须包含 ADR-0079 声明范围；API 校验嵌套 `scope_schema_version=1.0.0` 和 `scope_hash`，以及外层 `deviation_hash`。1.0.0 历史报告仍可读，但不构成声明范围证据。

## 本机运行（Uvicorn，ADR-0063，B65；CODE_COMPLETE / DEBUG_PENDING）

```bash
uv run --extra api-server python -m apps.api.serve --port 8000 \
    --reports-root <报告根目录> --jobs-results <任务结果日志> --jobs-idempotent <名称 ...> --knowledge <知识目录>
```

- **只在本机**：API 没有认证，主机固定 `127.0.0.1`（没有 `--host`；任何其他地址——`0.0.0.0`、`localhost`、`::1`、局域网地址——被拒绝）；
  单 worker、不 reload、不信任代理头。公网部署、TLS / 认证、反向代理、高可用不在范围内，需要另行决定。
- 各选项直接接到 `create_app`；未给出的设置按 `create_app` 的既有语义回答（空报告列表、任务 / 知识 503）。`--port 0` 绑定临时端口（Uvicorn 日志给出实际端口）。
- Uvicorn 是可选 `api-server` extra（`uvicorn==0.53.0`），不在默认依赖中；`apps.api` 不 import 它。未安装时入口以退出码 2 与说明退出。
- **当前状态**：当前开发 `.venv` 已按开发期授权安装锁定的 `api-server` extra。真实 Uvicorn 子进程在回环地址提供健康 / 报告 / 知识 HTTP，SIGTERM / SIGINT 优雅停止与 worker 跨进程恢复组合回归为 `30 passed`、无 skip。复验命令：`uv run --extra api-server pytest -q -rs -p no:cacheprovider tests/apps/test_api_server.py tests/apps/test_worker_jobs_cross_process.py`。干净默认环境没有安装该可选 extra 时，两个真实 Uvicorn 用例仍按设计跳过。
