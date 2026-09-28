# 模块基础逻辑收敛计划（2026-09-28）

## 目标与边界

目标是把已批准范围内各 Phase 的逻辑、基础接线和项目文档补齐，统一留待 Raphael 安排测试与验收。代码进入 `main`、源码静态复核、测试通过和 Phase 验收是不同状态；本计划不把前两项写成后两项。

- 依据：`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`docs/research/roadmap.md`、已 Accepted ADR 与 Claude 的 6 子代理只读模块审计（2026-09-28）。
- 不修改 `core/` 契约、Constitution、Validation Profile 数值、冻结阈值或 Phase 运行边界；此类需求先走 ADR / 人工决定。
- 不运行测试、build、lint、typecheck、probe、数据生成或阶段验收，除非 Raphael 后续授权。
- 并行任务必须各限于一个模块 / 插件；涉及 `core/` 的任务不得并行。协调者负责在整合前复核跨模块关系。
- 外部源工作区只读。分支整合采取择取已核实的提交；不整支合并从旧基线分叉、会回退 `main` 的分支。

## 当前代码线与分支处置

- 整合基线：本地 `main@ad16813`；`origin/main@44fe9a2`；未推送。G1 两条边界测试已包含在当前 main tip；提交尚未验证。
- `phase/1@52f7477` 的 496 个提交全部是 main 祖先，没有独有 patch；但根目录是 Cursor 当前打开的工作区，含未跟踪计划资料和嵌套 worktree。保留，不切换、不移除。
- `docs/research-spec-completion@82bfe73` 从旧 `20bdd82` 分叉，整树与当前 main 有 124 个路径差异，其中有主线后续实现的删除 / 回退。其 `7b9df59` 两条 G1 边界测试已择取到 main；策略收益率修复已 patch-equivalent 在 main。其余 README / library 文档以 main 版本为准，旧 tip 已有 archive ref。该 worktree 仍有活动 Claude 进程，因此保留到会话结束后再清理 branch/worktree。
- 远端只保留 `main`；不得从 archive ref 恢复或整支合并失败的 E1 候选。E1 候选 `c3868dc` 超过 32 MiB 门槛的失败证据继续保留。
- 经确认没有活动进程后，E1 候选、D3E 审查、C05、gatewt、E1 integration 与 B52 等 6 个 detached worktree 已清理；E1 候选 tip 与 D3E detached tip 均留有 archive ref。当前仅保留 main、Cursor 检出的 `phase/1` 与 Claude 检出的 `docs/research-spec-completion` 三个 worktree；后两者等待各自会话结束后再清理。

## 模块状态总览

| 范围 | 当前基础 | 代码 / 内容缺口 | 处理方式 |
|---|---|---|---|
| Phase 0.5 知识库 | 检索、审阅写入、标签 / 资产过滤、种子加载 | 部分种子没有黄金哈希或具名审阅；不得由代理代填人工标签 | 代码不新增模拟审阅；黄金哈希留给统一验收运行，标签 / 资产由具名审阅者决定 |
| Phase 1 数据 | Storage、Catalog、Collector、Parser、Revision、PIT、Normalizer、RSS probe | E1-CAP-1 未证明；当前 500k 候选超 32 MiB；成交量 bar / 历史 universe 另有规格门 | 先整理 E1 设计包和 Proposed ADR；不移植失败候选、不改契约、不宣称通过 |
| Phase 2 State | Provider、执行器、诊断载荷 1.1.0 | Web view model 未暴露 `source_result_hash`；1.0.0 / 1.1.0 显示与兼容逻辑待统一验收 | 可直接补 Web 类型的 optional source 字段；不改诊断契约 |
| Phase 3 Event | Provider、DSL、统计、Event 表定义和显式创建命令 | 创建命令输出未携带定义的稳定版本 / 哈希，和 ADR-0066 运维可审计要求不完整 | 可直接改命令输出；不连 Catalog、不创建真实表 |
| Phase 4 Outcome | Provider、Outcome store/source、EventResult 转换辅助函数、验证流程 | 转换辅助函数当前无调用方；Outcome/Profile 端到端行为留待统一验收 | 保持纯转换边界，先由协调者检查其调用接点，不创造新的写入路径 |
| Phase 5 Strategy | TSMOM / cross-sectional、backtest、validation、Promotion、FailureRegistry | 当前主要缺验收覆盖；没有可独立批准的批量 ValidationReport 作业 | 不改 Profile / 风控门；测试留待验收批次 |
| Phase 6 Matrix | 状态×策略矩阵、逐单元验证、报告接线 | 运行 / 报告幂等性以测试和统一验收证明 | 本轮不改已具备实现；验收批次补覆盖 |
| Phase 7 Discovery | non-runnable parser / resolver、TrialLedger、durable v4/v5、lease / write gate、ADR-0074 operator | ADR-0073 要求的 ExperimentSpec↔Hypothesis↔output 一对一组合校验尚未接入；六算子 producer / lowering 仍未批准 | 可实现独立纯拒绝校验器；不接 PREPARE、runner 或开启算子 |
| Phase 8 Retro audit | writer、API、Web 页面 | fixture registry 未登记 retro_audit 1.1.0；需要对生成路径静态复核 | 可修复 fixture 声明/登记；不重新生成已提交 fixture，生成器留待验收 |
| Phase 9 Calibration | 单标的与多标的校准、G5 证据 | 多标的 G5 与新生成器需明确证据范围 | 不选数值、不添加未经 ADR 批准的生成器 |
| Phase 10 Router | Paper / deviation / evidence / validation | deviation 缺少声明范围；validation 复用 paper 私有 helper | 不改验证门槛或报告哈希；需先确认声明范围语义 |
| Phase 11 Loop | durable loop、worker journal stale-writer guard、degradation operator | ACTIVE/source resolver 和生产 launcher 没有批准规格 | 不新增常驻服务、source resolver 或写触发 |
| Phase 12 Evolution | mutate / combine / proposal / replacement | P12-LOOP 明确暂缓 | 保持既有 fail-closed / conflict refusal |
| Phase 13 Execution | simulated-only execution、audit、emergency stop、replay | roadmap 的二道风控演练及模拟与回测差异报告和 ADR-0046 文字范围不一致 | 先协调 ADR-0046 / roadmap，不引入 live 能力 |
| Phase 14 Migration | gold-standard、diff、rollback evidence | 无目标系统或真实数据金标准 | 目标选择前不编造迁移实现 |
| Apps | Reports API / Web 查询面、多类报告页面 | P8 fixture 与 P2 StateDiagnostics 类型需要同步；未进行浏览器验收 | 只修已批准 schema 的消费类型，不添 API 写触发 |

## 已批准的实现批次

每个任务单独提交或按本地整合策略提交；实现代理只在指派的模块文件内工作。Codex 做源码复核、冲突处理和项目文档更新。未列明的跨模块修改暂停并回报 `ARCHITECTURE_DECISION_REQUIRED`。

### Wave A：不依赖新架构决定的基础逻辑

| ID | 模块 | 任务 | 文件边界 | 状态 |
|---|---|---|---|---|
| A1 | P7 hypothesis composition | 按 ADR-0073 §1 实现 pure binding validator：每个 ExperimentSpec 精确绑定一个批次 Hypothesis；Hypothesis 恰被一个 ExperimentSpec 引用；已提交的每个 lowered output 精确匹配其 ExperimentSpec 的直接依赖 ref/hash；重复、缺失关联、额外关联与错 hash fail closed。不得接入 PREPARE、TrialLedger、Runner 或改变 `runnable=False` | `research/hypotheses/plan_bindings.py`、同目录 README；不改 `core/` | 源码复核无直接绕过路径；缺权威预期输出清单，暂不能证明 outputs 集合完整 |
| A2 | P3 event operations | 在默认无副作用、`--apply` 只操作 `event.events` 的现有命令输出中加入 TableDefinition 的 `name@version` 与完整定义内容哈希；绑定或表范围不符时 fail closed | `infrastructure/event/create_event_tables.py` | 源码复核完成；既有命令测试仍期待旧输出，留待验收窗口同步；未运行 |
| A3 | P2 Web diagnostics | Web `StateDiagnosticsPayload` 增加可空/可省略的 `source_result_hash` 消费字段，与 Python 1.1.0 输出对应，同时保留 1.0.0 旧 payload 可读 | `apps/web/src/lib/stateDiagnostics.ts` | 源码复核完成；未做类型检查 |
| A4 | P8 report fixtures | 将现有 `retro_audit` writer/报告种类加入 console fixture writer registry；不生成 fixture 文件、不改 report schema | `tests/research/reports/test_console_fixture_writers.py`、`apps/web/fixtures/README.md` | 源码复核完成；旧 1.0.0 JSON / fixture ID 登记留待验收同步 |

### Wave B：代码 / 文档状态对账

| ID | 任务 | 文件边界 | 状态 |
|---|---|---|---|
| B1 | 校正 ADR-0068 / 0073、ADR index、roadmap、状态页中 Proposed / 已实现 / operator 入口等过时措辞 | 仅事实状态与交叉链接；不得改 ADR 裁决 | Codex 统筹，Wave A 后进行 |
| B2 | 统一 Git 数字、P8 fixture 版本、P13 验收措辞、Phase 1 表数和 Phase 1 关闭条件的状态叙述 | `PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、被核实的对应文档 | 逐条读源核实后更新 |
| B3 | 整理 E1-CAP-ARCH 设计包：分离可在现有契约内处理的持有量与需新 ADR 的 metadata / retention 变化；记录失败候选数据的真实范围 | `docs/reviews/`、Proposed ADR 草案 | Codex 主责；不据此直接改 E1 代码 |

### 暂不实施，留到语义决定或验收窗口

- P0.5：不可替人工写 tags/assets/review；09-26 种子黄金哈希要留到允许运行 Python 的验收窗口。
- P1：E1 先有 Codex 接受的设计，再开始 implementation；D-LIST / ADR-0051 与新数据范围各自保留原门禁。
- P4/P5：不冻结 D-09/Profile 数值，不改变成本、split、threshold 或 outcome 选择语义。
- P7：六类组合算子、producer、execution lowering、失败修复与重试入口均待单独 ADR。
- P10/P11/P13/P14：分别等待 deviation scope、ACTIVE/source resolver、ADR-0046 验收措辞、migration target。
- 所有已有测试文件缺口 / 兼容测试 / fixture 生成 / phase acceptance：集中留到后续验收批次；本阶段不运行。

## 并行与交付规约

- A1、A2、A3 可并行，因为分别属于 `research/hypotheses`、`infrastructure/event`、`apps/web`，不接触 `core/`。
- A1–A4 已完成源码复核，未运行测试 / build / lint / typecheck；随后先对齐事实文档，再进入下一批。
- 每个开发代理一次只领一个 ID；Codex 复核后才进本地 `main`。Claude 可用最多 6 个子代理并行，但不能跨写同一模块。Cursor 活动的 `phase/1` 与 `docs/research-spec-completion` worktree 仅保留，不调度任务。
- 交付须含：改动文件、任务 ID/Phase、对应 ADR/ROADMAP 约束、静态复核结果、未解决事项。测试、类型检查、lint、build 和验收状态统一标为“未运行”。

## 阶段目标

1. 清理确认无活动会话的冗余分支/worktree，保留历史 archive ref 与失败证据。
2. 完成 Wave A 逻辑代码和 Wave B 文档对账，逐模块追加后续发现的已批准基础任务。
3. 形成明确的 handoff：哪些模块已实现但未验收、哪些仍待人工/ADR/数据门，以及建议的统一验收顺序。

本计划本身不表示 Phase 0.5 或 Phase 1–14 任一验收通过。
