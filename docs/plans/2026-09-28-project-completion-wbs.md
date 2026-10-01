# HLENS-AutoResearch 全项目验收工作分解（2026-09-28）

> 本文追踪测试、运行与 Phase / 项目验收，不是当前代码缺口清单。代码补全的唯一当前计划见[剩余底层代码完成计划](2026-09-28-remaining-code-gaps.md)，模块状态见[模块底层代码完成计划](2026-09-28-module-foundation-completion.md)。2026-10-01 PR #17–#19 已合入 `main@b5f80fe`；该合并不构成最新 HEAD 的测试结果或 Phase 验收。

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
| W1 | 全仓技术基线 | W0 | 在受限环境完成 pytest（记录 PostgreSQL skip）、Ruff、format、mypy；不得跳过或削弱断言；经两轮仍失败的用例转延期记录，后续仍属发布门 | 无 PostgreSQL 的历史全量为 8,723 passed / 15 failed / 138 skipped；2026-09-29 PostgreSQL-enabled 全量为 8,814 passed / 7 failed / 1 skipped / 72 deselected / 5 warnings。focused retry 为 3 failed / 40 passed：catalog frozen-layout 与 revision table-binding 节点已达两轮并延期；catalog metadata-only 节点轮次无法核实，后续因 URI 未配置而 skip。原 14 项及 malformed loop-round 用例同样延期，均不计通过、仍属发布门。console fixture + synthetic evidence 回归 52 passed。以上均为历史候选结果，整合树尚未复验；详见 W1 review。 |
| W2 | 启动、停止与恢复证明 | W1 | API / worker 可启动停止重启；任务、日志、Registry、Admission 与 Iceberg 恢复测试通过；记录需外部服务的跳过项 | 部分通过：历史 API/worker process recovery `30 passed, 0 skipped`，另一轮 `32 passed, 1 warning`；API runtime `25 passed`。Uvicorn loopback、SIGTERM/SIGINT graceful shutdown、worker SIGKILL recovery 已验证。Registry / Profile Freeze / Retirement / Admission / SQLite Catalog / State / Event `181 passed`；SQLite Catalog+warehouse reopen `1 passed`；State reopen `28 passed`；Event Iceberg `29 passed, 1 deselected`（同进程临时 SQLite adapter reconnect，不是 PostgreSQL 跨进程证据）。ADR-0095 Worker host/subprocess recovery `28 passed`；另有 PostgreSQL Catalog + 本地 Iceberg result-before-ack crash restart 子标准 `1 passed`。W2 整体验收仍开放，完整 API+worker+PostgreSQL+Iceberg restart 及生产恢复证据未闭合；这些均为候选历史结果，整合树尚未复验。 |
| W3 | Phase 1 E1-CAP-1 | W1 | 端到端代表性工作集测量证明 RSS / 工作集 ≤ 32 MiB，包含 PyIceberg metadata、解析、完整结果与调用方持有集合；保留历史与 PIT 语义 | **阻断，未验收。** SQLite graph validator 与 `PitSelector.iter_bounded()` indexed maximal-head traversal 已整合并经三方限范围验收；PIT SQLite direct + diagnostics `24 passed`，过滤后的 PIT/Dataset caller `63 passed, 4 skipped, 2 deferred deselected`。bounded harness 覆盖长链、宽 DAG、重复 cutoff，64 节点 ext4 smoke 仅验证实现/计数语义，不是压力测量。Quality v3 manifest → Dataset v3 stream consumer 与 rule-owned identity-hash / snapshot-table 注册均已整合并通过三方限范围审查；Quality reporter/Dataset/manifest-store `58 passed`。仍未测重复 cutoff 更大规模 I/O/RSS、长链/宽图容量、完整进程或总 scratch 工作集。D1 parser / `ParsedArchive`、PyIceberg metadata/history、normalizer 全路径和完整返回对象的隔离进程 probe 尚未在当前整合 SHA 完成。局部回归、历史候选 probe 与 200 行 smoke 均不替代既有 32 MiB gate；DQ-9 参数仍开放。ADR-0100 §6 的 E1 路径有界化已实现，但未测量，不构成通过证据。 |
| W4 | Phase 1 数据正确性收口 | W1、W3 | D0–F4 可复现，证据与 manifest 重建一致，PostgreSQL 测试明确运行；ADR-0051 下界核实并只按显式假设使用；Phase 1 验收记录满足 roadmap | D3E 独立验收、D4 关闭；row-integrity SQLite spool 关闭 / 异常清理定向覆盖 `2 passed`，仅为局部测试证据；E1 阻断；ADR-0051 政策 1.1.0 已填入 BTCUSDT / ETHUSDT 下界（ADR-0100 §5，未测试） |
| W5 | Phase 0.5 Knowledge Base | W1 | 种子标签 / 资产经具名人工审阅；新种子 golden hash 完整；审阅和检索路径回归通过 | 部分技术回归通过：检索 / 审阅路径 `103 passed`，seed schema guard 的候选切片通过。10 条种子的 tags/assets 仍待具名人工审阅；后加 4 条种子的独立 golden 钉值缺失。Golden 与人工审阅未完成；W5 未验收。 |
| W6 | Phase 2–6 状态、事件、Outcome、回测与矩阵 | W1 | Provider / 持久化 / 因果性 / 错误路径测试通过；质量与验证 Profile 的参数来源明确；逐 Phase 按 roadmap 单独验收 | P2 State slice `44 passed`、State adapter reopen `28 passed`。P3 Event + P4 Outcome selected slice `203 passed, 1 failed`（registry count 15-vs-17，历史失败轮次无法核实）；另有 Event Iceberg reopen `29 passed, 1 deselected`，仅临时 SQLite。模块证据不构成 Phase acceptance；见 [W6 independent validation](../reviews/2026-09-29-w6-independent-validation.md)。 |
| W7 | Phase 7–8 自动研究与 Validation | W1、W6 | Admission / crash recovery / 完整试验产出关系 / G4 与 G5 red-team、负路径、Profile 与验证重放通过；所有未批准算子继续 fail closed | P7 focused batch 曾有 `70 passed, 1 failed`；修正文案后仅精确失败节点通过，完整批次未重跑。P8 selected slice `148 passed`。`TypedPlan.runnable` 仍 False，Provider/Runner 关闭；W7 验收仍依赖 W1/W6、G4/G5/Profile/replay 证据，且无 frozen Profile。 |
| W8 | Phase 9–12 合成、路由、循环、演化 | W1、W7 | 合成校准可复现；真实 source / metric 权威有决定或明确保持 synthetic-only；循环恢复、失败停机、生命周期审计通过 | ADR-0080 已被 ADR-0098 取代：P11 权威登记处与真实 source / metric 解析已实现，ADR-0100 §3/§4 补指标闭集与默认环境；P12 循环内替换提案为可选触发（ADR-0100 §7，默认关闭）；均未测试、未验收 |
| W9 | Phase 13–14 执行与迁移 | W1、W6–W8 | Simulated-only execution drills、审计 replay、kill-switch、migration matrix 与 rollback evidence 通过；LIVE 仍不可用 | 实现待验收；实盘永久不属于本工作包 |
| W10 | 应用和运维运行时 | W1、W2 | API / worker / web build 与 smoke、配置验证、健康检查、日志、shutdown/restart；部署操作与当前阶段文档一致 | Web 独立切片：`npm run build` 通过，组件测试 `120 passed`；library suite `110 passed / 1 failed`，retro-audit fixture 精确节点两轮失败后延期，未通过。fixture-backed SSR 覆盖页面使用的五个图表 builder；Chrome 153 对 4 页 / 5 个 Canvas 做 fixture-backed 渲染、报告选择与 1280px→700px resize；0 JS exception / failed HTTP response，视觉复核修复标签重叠、窄屏挤压与表格溢出；chart helpers `25 passed`。依赖修复候选 `npm audit` 为 0 advisories。延期 Web 节点仍未通过；Worker host 已由 ADR-0095 建立，但 PostgreSQL + Iceberg 全链路联合恢复与 W10 整体验收仍未关闭。详见 [W10 Web validation](../reviews/2026-09-29-w10-web-validation.md)。 |
| W11 | 安全与架构红队 | W1、W2、W4、W7–W10 | 独立检查边界、PIT 泄漏、篡改、重复写、恢复、资源界限、权限与 live拒绝；每项攻击有回归证据 | 部分历史红队，需跨阶段复核 |
| W12 | 发布门与最终交接 | W2–W11 | clean build、适用集成 / 回归、静态检查、恢复 / 部署、文档与风险清单全通过；主线干净、tag / 发布说明反映真实状态 | 未开始；不因分支收敛或局部测试完成而关闭 |

### 当前底层代码实现索引（不构成验收）

W3 的历史容量与测试证据仍按记录日期保留。当前代码执行视图以[剩余代码计划](2026-09-28-remaining-code-gaps.md)为准：截至 `main@b5f80fe`，E1 bounded scan、normalizer ID stream、生产归档 spool 与 v3 Dataset streaming pipeline 已有仓库内实现；本地 `phase/1@775245e` 已提交 `P7-CS-EXEC` 编译接线。两者均未因静态源码存在或本地分支提交而获得测试 / 容量 / Phase 验收状态；P7 尚未合入 `main`，E1-CAP-1 与 DQ-9 仍开放。

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
