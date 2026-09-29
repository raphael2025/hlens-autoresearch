# HLENS-AutoResearch 全项目验收工作分解（2026-09-28）

## 目标与当前判定

目标是交付一个可本地运行、可恢复、可复现、经确定性规则验证的研究闭环。产品是研究结论与证据，不是交易信号或下单系统；LLM 只能提交结构化研究数据，不能裁决验证结果。任何“项目完成”声明都要求源码、测试、运行、容量、安全、文档和发布证据闭合。

当前仓库已聚合到长期 `main`，但尚未达到全项目验收：Phase 1 被 E1-CAP-1（完整进程工作集不超过 32 MiB）阻断；Phase 0.5 与 Phase 2–14 多数仍是 `CODE_COMPLETE / DEBUG_PENDING` 或框架状态。单模块测试通过不代表阶段或项目验收。

## 系统模型

### 组件与依赖

```text
core contracts and domain
    ↑
infrastructure adapters / storage / registries  ←  plugins and providers
    ↑
research stages and composition
    ↑ via protocols only
apps API / worker / web / simulated execution
```

`apps` 不得运行时导入 `research`。`core` 拥有稳定领域对象与边界契约；`infrastructure` 实现持久化与 I/O；`plugins` 提供可发现的实现；`research` 组合研究流程。执行服务保持模拟模式，LIVE 被拒绝。

### 数据与控制路径

```text
Public archives / REST
 → D0 collection → D1 parse → D2/D3 revision and evidence
 → E1 canonical → F1 point-in-time selection → E3 quality
 → E2 universe → F3 dataset + manifest → F4 features
 → State / Event / Outcome → Hypothesis → Experiment
 → deterministic Validation → lifecycle decision → research memory
```

Dataset 与实验须绑定快照、政策、来源和规则版本。研究闭环的失败、恢复、晋升和拒绝都必须可审计；Research Agent 只产出 `Hypothesis` / `ExperimentSpec` 数据，确定性 Runner 与 Validation 才能执行和判定。

### 运行拓扑与数据存储

- 已实际使用的 Phase 1 本地数据面：Python 3.13 + `uv`、Iceberg / Parquet warehouse、本机 SQLite 或 PostgreSQL Iceberg Catalog；行情数据行在 Iceberg，不在 Catalog PostgreSQL。
- PostgreSQL Control Plane、NATS、容器部署、外部对象存储是后续目标，不能描述为已部署能力。当前 API 和若干 Registry 仍为只读或 file-backed 实现。
- 真实交易凭据、账户连接、订单和实盘执行均在范围外；LIVE 必须失败关闭。
- 每个本地 Python / pytest / mypy / Ruff 命令按 `docs/architecture/08-deployment.md §6.3` 用 `systemd-run` 设置 `MemoryMax` 与 `MemorySwapMax=0`。

## WBS 与验收工作包

| ID | 工作包 / 目标 | 依赖 | 产物与验收条件 | 当前状态 |
|---|---|---|---|---|
| W0 | 项目基线和文档可信度 | 无 | README、ADR index、`PROJECT_STATUS`、模块 README 对齐主线、决策与真实运行形态；无冲突状态 | 本轮修复部分偏差，仍需全仓审计 |
| W1 | 全仓技术基线 | W0 | 在受限环境完成 pytest（记录 PostgreSQL skip）、Ruff、format、mypy；不得跳过或削弱断言；经两轮仍失败的用例转延期记录，后续仍属发布门 | 无 PostgreSQL 的历史全量为 8,723 passed / 15 failed / 138 skipped；2026-09-29 PostgreSQL-enabled 全量为 8,814 passed / 7 failed / 1 skipped / 72 deselected / 5 warnings（1:27:36）。该次执行 8,822 项，另有 72 项 deselected。focused database retry 为 3 failed / 40 passed：catalog frozen-layout 与 revision table-binding 节点已达两轮并延期。catalog metadata-only 节点在 focused retry 中失败，但全量七个失败 node IDs 未保留，轮数无法核实；后续精确尝试因 `HLENS_TEST_CATALOG_URI` 未设置而 skip，既不计通过也不计失败。原 14 项及 malformed loop-round 用例同样按两轮规则延期，均不计通过、仍属发布门。console fixture + synthetic evidence 回归 52 passed。静态检查与其余分区记录见本次 W1 review；继续不受影响的工作流 |
| W2 | 启动、停止与恢复证明 | W1 | API / worker 可启动停止重启；任务、日志、Registry、Admission 与 Iceberg 恢复测试通过；记录需外部服务的跳过项 | 部分通过：API server + worker process recovery `30 passed, 0 skipped`；真实 Uvicorn loopback HTTP、SIGTERM/SIGINT graceful shutdown exit 0、worker SIGKILL recovery 已验证。Registry / Profile Freeze / Retirement / Admission / SQLite Catalog / State / Event 定向回归 `181 passed`；新增 SQLite Catalog+warehouse reopen `1 passed`；State reopen suite `28 passed`；Event Iceberg suite `29 passed, 1 deselected`（排除历史轮次未分类的 registry-count 节点）。State/Event 结果只证明同进程临时 SQLite adapter reconnect，不是跨进程或 PostgreSQL 恢复。API Uvicorn 已输出 allowlist JSON 并关闭 access log（runtime suite 25 passed）；ADR-0093 Worker host / subprocess / cross-process recovery 定向回归 `28 passed`，Ruff 与 3-file mypy 检查通过；完整 API+worker+PostgreSQL+Iceberg restart 仍未关闭 |
| W3 | Phase 1 E1-CAP-1 | W1 | 端到端代表性工作集测量证明 RSS / 工作集 ≤ 32 MiB，包含 PyIceberg metadata、解析、完整结果与调用方持有集合；保留历史与 PIT 语义 | **阻断**。隔离候选现已串接 E1 archive spool、PIT bounded runs、Universe 外部排序 / 窗口过滤、Dataset member-span spill 和 D-E1-SCRATCH Option A。最新候选证据：E1 + W5 `178 passed`；PIT 三文件 `84 passed`；Universe/Dataset 三文件 `65 passed`；Settings + Canonical `148 passed`，另有 scratch/PIT 接线及资源关闭定向回归。独立 review 的 PIT rebind P2 已修复，复审无新发现。`test_a_v2_round_with_an_evidence_verifier_is_exactly_the_v2_round` 两轮哈希不一致后延期，未通过。增量代码通过不等于容量门通过：PIT graph/history 仍有 O(N)，Quality `existing_only` 仍整行物化 report events，scratch disk / spool bytes 没有配额或完整过程测量。既有非 main probe 峰值约 194 MiB、100k resume 超时只是诊断，不能作验收证据。完整 32 MiB 进程工作集仍未测量，Quality replay 协议仍需决定；详见 [W1 验收与延期项](../reviews/2026-09-29-w1-validation-and-deferrals.md) 与 Canonical scratch 决策包。 |
| W4 | Phase 1 数据正确性收口 | W1、W3 | D0–F4 可复现，证据与 manifest 重建一致，PostgreSQL 测试明确运行；ADR-0051 下界核实并只按显式假设使用；Phase 1 验收记录满足 roadmap | D3E 独立验收、D4 关闭；row-integrity SQLite spool 关闭 / 异常清理定向覆盖 `2 passed`，仅为局部测试证据；E1 阻断，ADR-0051 政策表为空 |
| W5 | Phase 0.5 Knowledge Base | W1 | 种子标签 / 资产经具名人工审阅；新种子 golden hash 完整；审阅和检索路径回归通过 | 部分技术回归通过：检索 / 审阅路径 `103 passed`；隔离集成分支新增种子精确 `schema_version == 2.1.0` 守卫，合并切片定向回归中的该项通过。10 条种子的 tags/assets 仍待具名人工审阅；后加 4 条种子的独立 golden 钉值缺失。Golden 修复项尚未通过验收，单独排期，不影响其他 W5 工作；原有历史 golden 不变 |
| W6 | Phase 2–6 状态、事件、Outcome、回测与矩阵 | W1 | Provider / 持久化 / 因果性 / 错误路径测试通过；质量与验证 Profile 的参数来源明确；逐 Phase 按 roadmap 单独验收 | P2 State runner/storage/diagnostics synthetic slice `44 passed`；State adapter reopen suite `28 passed`。P3 Event + P4 Outcome selected slice `203 passed, 1 failed`：registry unchanged test still asserts 15 tables while the current catalog explicitly has 17; the seven full-run failure IDs are missing, so its prior-round count is unknown and it is not retried in this batch. A separate Event Iceberg reopen slice passed `29 passed, 1 deselected` with the unclassified node excluded. Module evidence proceeds independently; no Phase acceptance claimed. See [W6 independent validation](../reviews/2026-09-29-w6-independent-validation.md) |
| W7 | Phase 7–8 自动研究与 Validation | W1、W6 | Admission / crash recovery / 完整试验产出关系 / G4 与 G5 red-team、负路径、Profile 与验证重放通过；所有未批准算子继续 fail closed | P7 focused batch `70 passed, 1 failed`; the only failure was an ADR-0083 duplicate-rejection message missing “unique”; after correcting the message, that exact node passed. The full batch was not rerun. P8 retro-audit/report verification/Promotion/console writer selected slice `148 passed`. `TypedPlan.runnable` stays False; Provider/Runner remain disabled. Full W7 acceptance still depends on W1/W6 and pending G4/G5/Profile/replay evidence; no frozen Profile |
| W8 | Phase 9–12 合成、路由、循环、演化 | W1、W7 | 合成校准可复现；真实 source / metric 权威有决定或明确保持 synthetic-only；循环恢复、失败停机、生命周期审计通过 | ADR-0080 BLOCKED；synthetic-only 边界必须维持 |
| W9 | Phase 13–14 执行与迁移 | W1、W6–W8 | Simulated-only execution drills、审计 replay、kill-switch、migration matrix 与 rollback evidence 通过；LIVE 仍不可用 | 实现待验收；实盘永久不属于本工作包 |
| W10 | 应用和运维运行时 | W1、W2 | API / worker / web build 与 smoke、配置验证、健康检查、日志、shutdown/restart；部署操作与当前阶段文档一致 | Web 独立切片：`npm run build` 通过，组件测试 120 passed；library suite 110 passed / 1 failed，retro-audit fixture 精确节点两轮失败后按规则延期，未通过；fixture-backed SSR 验证页面使用的五个图表 builder。Chrome 153 对 4 页 / 5 个 Canvas 完成 fixture-backed 渲染、报告选择与 1280px→700px resize；0 JS exception / failed HTTP response，视觉复核修复标签重叠、窄屏挤压与表格溢出；chart helpers `25 passed`。依赖修复在隔离集成分支，`npm audit` 为 0 advisories。延期 Web 节点仍未通过，API / worker 的恢复边界见 W2；Worker 尚无进程入口，PostgreSQL + Iceberg 联合重启仍缺。详见 [W10 Web validation](../reviews/2026-09-29-w10-web-validation.md)。 |
| W11 | 安全与架构红队 | W1、W2、W4、W7–W10 | 独立检查边界、PIT 泄漏、篡改、重复写、恢复、资源界限、权限与 live拒绝；每项攻击有回归证据 | 部分历史红队，需跨阶段复核 |
| W12 | 发布门与最终交接 | W2–W11 | clean build、适用集成 / 回归、静态检查、恢复 / 部署、文档与风险清单全通过；主线干净、tag / 发布说明反映真实状态 | 未开始；不因分支收敛或局部测试完成而关闭 |

### W1 证据边界

- 上述是分区测试结果，不能相加当作全仓通过数；不同运行存在测试范围重叠，且策略/验证整套回归尚未完成。
- 初始 188 个失败数仅描述 2026-09-28 的全仓基线。2026-09-29 无 PostgreSQL 全量复验得到 15 个失败；一次 `--lf` 后 14 项仍失败、1 项通过。后续 PostgreSQL-enabled 全量执行 8,822 项，另有 72 项 deselected，结果为 7 failed / 8,814 passed / 1 skipped；focused database retry 后 catalog frozen-layout 与 revision table-binding 两个 exact nodes 达两轮失败并延期。catalog metadata-only 节点在 focused retry 中失败，但全量七个失败 node IDs 未保留，轮数无法核实；后续精确尝试因 `HLENS_TEST_CATALOG_URI` 未设置而 skip。延期失败均不计通过、保留为发布门；两轮失败节点不再占用当前实现批次。清单与输出见 [`W1 validation and deferred failures`](../reviews/2026-09-29-w1-validation-and-deferrals.md)。
- Ruff 使用 `uv run ruff check . --output-format=concise`，结果 `All checks passed!`；格式检查结果 `947 files already formatted`；`git diff --check` 无输出。Python 版本固定为 3.13，最后 4 个 Ruff `UP047` 已改用 PEP 695 泛型语法，另修复 1 个文档行宽项。
- revision snapshot history fallback 的有界遍历专项：`tests/infrastructure/revision/test_store_unit.py` 为 `41 passed in 6.84s`。API DTO 90 passed、Web lib 111 passed 与这些专项结果是独立测试集，不应与全仓结果相加。`systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 uv run mypy` 最终结果为 `Success: no issues found in 731 source files`。PostgreSQL 测试因 `HLENS_TEST_CATALOG_URI` 未设置仍属未验证路径；138 项 skip 记录在全仓报告中。

## 依赖 DAG 与执行顺序

```text
W0 → W1 → W2 ──────────────────────────────────────┐
       ├→ W3 → W4 ────────────────────────────────┤
       ├→ W5                                         │
       ├→ W6 → W7 → W8                              ├→ W11 → W12
       │      └→ W9                                 │
       └→ W10 ─────────────────────────────────────┘
```

优先顺序：先建立全仓测试事实（W1），并行修复与外部基础设施无关的模块回归；E1 容量（W3）作为 Phase 1 独立关键路径；随后逐 Phase 验收，再做跨组件 red-team 和运行时发布门。凡需产品语义、验证阈值或权威数据定义的事项，必须通过已有 Accepted ADR 或形成新的决策记录，不以实现推断替代决策。

## 当前明确阻断与风险

- **E1-CAP-1**：32 MiB 完整工作集容量未证明，Phase 1 不得标为验收。
- **ADR-0080**：Phase 11 缺权威 lifecycle head、source identity 与 metric 算法，维持 synthetic-only；不得构造伪 resolver。
- **Profile / Constitution 数值**：未冻结的阈值不能被猜测；没有有效冻结登记时 Promotion 失败关闭。
- **历史上市事实**：ADR-0051 只允许显式假设；它不能证明真实上市日期、消除幸存者偏差或支撑横截面结论。
- **测试 / CI / 部署证据**：局部通过不代表全仓通过；CI 未配置，真实 Uvicorn、PostgreSQL 集成和运行时故障恢复必须明确验证或如实列为未验证。

本计划是工程执行与验收跟踪，不接受或修改架构决策，也不代表任何 Phase 已完成。
