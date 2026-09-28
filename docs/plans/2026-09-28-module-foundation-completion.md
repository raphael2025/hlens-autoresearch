# 模块基础逻辑收敛计划（2026-09-28）

## 目标与边界

目标是把已批准范围内各 Phase 的逻辑、基础接线和项目文档补齐，统一留待 Raphael 安排测试与验收。代码进入 `main`、源码静态复核、测试通过和 Phase 验收是不同状态；本计划不把前两项写成后两项。

- 依据：`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`docs/research/roadmap.md`、已 Accepted ADR 与 Claude 的 6 子代理只读模块审计（2026-09-28）。
- 不修改 `core/` 契约、Constitution、Validation Profile 数值、冻结阈值或 Phase 运行边界；此类需求先走 ADR / 人工决定。
- 不运行测试、build、lint、typecheck、probe、数据生成或阶段验收，除非 Raphael 后续授权。
- 并行任务必须各限于一个模块 / 插件；涉及 `core/` 的任务不得并行。协调者负责在整合前复核跨模块关系。
- 外部源工作区只读。分支整合采取择取已核实的提交；不整支合并从旧基线分叉、会回退 `main` 的分支。

## 当前代码线与分支处置

- 当前整合点：本地 `main@ae66dc8`；`origin/main@44fe9a2`；当前 ahead 131，未推送。四项 E1-R 提交已进入本地 main；状态文档待同步提交后预计 ahead 132。G1 两条边界测试仍未运行。
- `phase/1@52f7477` 的所有提交都是 main 祖先，没有独有 patch；已保存至 `refs/archive/2026-09-28/branches/local/phase-1` 后删除分支，并将根 checkout 切回 `main`。根下未跟踪旧计划已移入本地 `.codex/archive/`，跟踪版计划已在主线。Cursor 会话此前确认无独有实现；IDE 进程未强行关闭。
- `docs/research-spec-completion@82bfe73` 从旧 `20bdd82` 分叉，整树与当前 main 有 124 个路径差异，包含主线后续实现的删除 / 回退。其 `7b9df59` 的 G1 测试与策略收益率修复已核对在 main；其余 README / library 文档以 main 为准。tip 由 archive ref 保存；确认工作区干净、特殊测试与 main 相同后，已关闭闲置 Claude 会话并清理 branch/worktree。
- 远端只保留 `main`；不得从 archive ref 恢复或整支合并失败的 E1 候选。E1 候选 `c3868dc` 超过 32 MiB 门槛的失败证据继续保留。
- E1 候选、D3E 审查、C05、gatewt、E1 integration、B52、Codex E1-R 和 Claude research-spec worktree 均已清理，相关恢复点保留在 archive refs。Claude worktree 无未提交内容，唯一测试差异已与 main 核对相同；闲置 Claude 会话已关闭。清理阶段仅保留 root `main`；上一轮未提交的 E1 余项（ADR-0075 Amendment 1 草案、ADR-0076/0077 草案及 bounded scan/result 代码）已于 2026-09-28 经 Raphael 批准整体归档到 `wip/e1-cap-archive@ece150e`（archive ref `refs/archive/2026-09-28/branches/local/wip-e1-cap-archive`），未审阅、未测试、不构成正式决定；E1-CAP-1 仍阻断，本轮后置。

## 模块状态总览

| Phase / 模块 | 代码是否已齐 | 仍缺的实现（仅代码） | 是否需新 ADR / 人工门 | 本轮是否动手 | 可并行？ |
|---|---|---|---|---|---|
| P0.5 Knowledge Base | CODE_COMPLETE / DEBUG_PENDING：检索、review 写入、过滤、seed loader 已实现 | 无已批准的可编码缺口 | seed tags/assets 需具名人工审阅；黄金哈希留统一验收生成 | 否：人工 / 验收门 | 否 |
| P1 D0–D4 数据基础链 | CODE_COMPLETE / DEBUG_PENDING：Storage、Catalog、Collector、Parser、Revision、PIT、Normalizer、REST / lineage 均有基础实现 | 本轮不扩 E1 有界状态；其余经审计无已批准代码缺口 | 历史 universe / volume bars 是规格门 | 否 | 可按单模块独立，但当前无任务 |
| P1 E1 / Dataset | FRAMEWORK：基础读取、selection、manifest、RSS 工具已存在；上一轮未提交的 E1 余项（ADR-0075 Amendment 1 草案、ADR-0076/0077 草案及 bounded scan/result 代码）已于 2026-09-28 经 Raphael 批准整体归档到 `wip/e1-cap-archive@ece150e`，未审阅、未测试、不构成正式决定 | 仍有 E1-CAP-1 / Dataset 有界结果工作，但本轮明确排除 | ADR-0077、容量门、32 MiB 证据；需后续单独处理 | **否：本轮跳过** | 否：本轮不派 |
| P2 State + Web diagnostics | CODE_COMPLETE / DEBUG_PENDING：执行器与 Python / Web diagnostics 字段已接线 | 无已批准的代码缺口；兼容与渲染属于后续调试 | 逐 kind Report DTO schema 需新契约决定 | 否：当前实现随本地修改收敛 | 是，若后续只改 Web |
| P3 Event | CODE_COMPLETE / DEBUG_PENDING：Provider、DSL、统计、定义及显式建表命令输出已实现 | 无；旧断言留统一调试 | 真正创建 Catalog 表需人工授权 | 否 | 是，单独命令模块 |
| P4 Outcome | CODE_COMPLETE / DEBUG_PENDING：store/source、转换辅助与验证流已实现 | 无已批准调用接线；现有纯转换函数不新造写路径 | 若增加新写入调用点，需先确认流程 / ADR 边界 | 否 | 否：无任务 |
| P5 Strategy / Validation | CODE_COMPLETE / DEBUG_PENDING：策略、回测、验证、Promotion、FailureRegistry 已实现 | 无已批准实现缺口 | Profile 数值与验证门冻结 | 否 | 是，模块间可分开调试 |
| P6 Matrix | CODE_COMPLETE / DEBUG_PENDING：矩阵运行与报告接线已实现 | 无已批准实现缺口 | 无 | 否 | 是，模块独立 |
| P7 Discovery | FRAMEWORK / DEBUG_PENDING：non-runnable parser、resolver、durable v4/v5、有限 operator 与纯 binding validator 已有 | 预期 lowered outputs 全集完整性尚无权威来源，故不补 producer / lowering | 新 ADR / 输出权威；六类算子保持关闭 | **否：本轮跳过** | 否：需先定语义 |
| P8 Retro audit | CODE_COMPLETE / DEBUG_PENDING：writer、API、Web 页面、fixture-writer registry 已有 | 无本轮可编码项；fixture ID / hash 更新属于后续生成与验收 | 不代替人工知识标签审阅 | 否 | 是，若后续只改 app |
| P9 Calibration | CODE_COMPLETE / DEBUG_PENDING：单 / 多标的校准与 G5 证据已实现；固定 Decimal context 已补 | 无已批准实现缺口 | 不选新生成器或数值 | 否：当前代码随本地修改收敛 | 是，限 synthetic_lab |
| P10 Router | FRAMEWORK：Paper、deviation、evidence、validation 已有 | deviation 尚未绑定声明范围；具体需先定义范围身份与兼容规则，当前不能安全写成代码缺口 | **需 ADR / 语义决定，本轮跳过** | 否 | 否：跨 Router 语义 |
| P11 Loop | FRAMEWORK / CODE_COMPLETE（已批准 synthetic-only operator）：durable loop、stale-writer guard、degradation evidence、有限 CLI 已有 | 无当前批准的代码项；权威 ACTIVE/source resolver、dataset operator、仓内 scheduler 未获准 | 需权威来源 / provenance 决定；外部 scheduler 既定 | **否：本轮跳过** | 否 |
| P12 Evolution | CODE_COMPLETE / DEBUG_PENDING：operators、proposal、replacement 与冲突拒绝已有；精确类型校验已补 | P12-LOOP 是明确暂缓项，不是遗漏代码 | 自动 proposal 需未来 ADR | 否：当前修改收敛 | 是，单独 research/evolution |
| P13 Execution | CODE_COMPLETE / DEBUG_PENDING：模拟执行、审计、kill switch、风险 replay 已有；准入前拒绝与原子 mark 已补 | 无已批准实现缺口 | 仅 simulated；实盘需单独授权 | 否：当前修改收敛 | 是，限 apps/execution |
| P14 Migration | FRAMEWORK / CODE_COMPLETE：golden、diff、rollback evidence 与不可变快照已有 | 无目标系统时不写 target adapter；现有 conformance 覆盖不等于迁移目标 | 目标系统与 golden data 需人工提供 | **否：本轮跳过** | 否：等待目标 |
| Apps / shared APIs | CODE_COMPLETE / DEBUG_PENDING：Reports API、只读 Web 查询面、多类页面已实现 | 无已批准基础接线缺口；P2 / P8 消费字段已同步 | report kind 版本化 DTO 需契约门；不增加写触发 | 否 | 是：后续可按单 app 页面并行 |

本表按现有模块计划、Wave A/C 交付记录与当前工作区代码盘点；“CODE_COMPLETE / DEBUG_PENDING”只表示批准范围内基础逻辑已存在，不代表测试或阶段验收通过。P1 E1 / Dataset 明确留到后续，不作为本轮代码缺口继续深挖。

### 后续模块调试顺序建议

1. P0.5 Knowledge Base：先核基础读取 / 检索，再由具名人员处理 seed tags/assets；人工审阅不并入代码测试。
2. P1 D0–D4：按 Storage / Catalog → Collector / Parser → Revision / PIT / Normalizer → D3 REST / lineage → D4 Quality 的数据依赖顺序逐层调试。E1-CAP-1 / Dataset 容量与 ADR-0077 路径单列，待本轮之后决策。
3. P2 → P3 → P4：State → Event → Outcome，先验证各 Provider 输出，再验证 Outcome 转换与最小验证接线。
4. P5 → P6：先单策略 / backtest / validation / Promotion，再跑状态 × 策略矩阵。
5. P7 → P8 → P9：Discovery 保持 non-runnable 边界，再核审计报告，最后校准证据。
6. P10 → P11 → P12 → P13：Router → Loop → Evolution → 模拟 Execution；P11 维持 synthetic-only / 外部调度，P13 不连实盘。
7. P14 与 Apps：提供迁移目标后验证 conformance / rollback；各 Apps 页面随其数据模块调试，最后做跨模块只读查询回归。

以上只是下一阶段建议顺序，不代表任何 Phase 已验收；每个模块调试结束后单独记录结果与剩余门槛。

### 本轮跳过的决定 / 人工门

| ID | 模块 | 待定问题 / 人工动作 | 本轮处理 |
|---|---|---|---|
| DP-P1-E1 | P1 E1 / Dataset | ADR-0077 与 E1-CAP-1 的后续实现和容量证据 | 本轮跳过；ADR 保持 Proposed，不继续扩写或实施 |
| DP-P0.5-REVIEW | P0.5 Knowledge Base | 由具名审阅者确认 seed tags/assets；验收窗口生成黄金哈希 | 本轮不代审、不生成 |
| DP-P3-CATALOG | P3 Event | 是否授权在真实 Catalog 创建 Event 表 | 本轮不创建 |
| DP-P7-OUTPUTS | P7 Discovery | lowered outputs 完整集合的权威来源与后续 producer/lowering 边界 | 本轮不实现 producer 或启用算子 |
| DP-P10-SCOPE | P10 Router | deviation 报告如何绑定声明范围身份及兼容版本 | 本轮不猜语义、不改阈值 |
| DP-P11-AUTHORITY | P11 Loop | ACTIVE、真实 source 与 metric 的权威来源 / provenance | 本轮不加 resolver、dataset operator 或仓内调度 |
| DP-APP-REPORT-DTO | Apps | 是否为各 report kind 建立版本化 payload DTO 契约 | 本轮不收窄现有 API payload |
| DP-P14-TARGET | P14 Migration | 迁移目标、目标环境与具名 golden data | 未提供目标前不写 adapter |
| HUMAN-PROFILE | P4/P5/P9 | D-09 / Profile 数值与验证阈值 | 保持冻结，不在代码中填默认值 |

## 已批准的实现批次

每个任务单独提交或按本地整合策略提交；实现代理只在指派的模块文件内工作。Codex 做源码复核、冲突处理和项目文档更新。未列明的跨模块修改暂停并回报 `ARCHITECTURE_DECISION_REQUIRED`。

### Wave A：不依赖新架构决定的基础逻辑

| ID | 模块 | 任务 | 文件边界 | 状态 |
|---|---|---|---|---|
| A1 | P7 hypothesis composition | 按 ADR-0073 §1 实现 pure binding validator：每个 ExperimentSpec 精确绑定一个批次 Hypothesis；Hypothesis 恰被一个 ExperimentSpec 引用；已提交的每个 lowered output 精确匹配其 ExperimentSpec 的直接依赖 ref/hash；重复、缺失关联、额外关联与错 hash fail closed。不得接入 PREPARE、TrialLedger、Runner 或改变 `runnable=False` | `research/hypotheses/plan_bindings.py`、同目录 README；不改 `core/` | 源码复核无直接绕过路径；缺权威预期输出清单，暂不能证明 outputs 集合完整 |
| A2 | P3 event operations | 在默认无副作用、`--apply` 只操作 `event.events` 的现有命令输出中加入 TableDefinition 的 `name@version` 与完整定义内容哈希；绑定或表范围不符时 fail closed | `infrastructure/event/create_event_tables.py` | 源码复核完成；既有命令测试仍期待旧输出，留待验收窗口同步；未运行 |
| A3 | P2 Web diagnostics | Web `StateDiagnosticsPayload` 增加可空/可省略的 `source_result_hash` 消费字段，与 Python 1.1.0 输出对应，同时保留 1.0.0 旧 payload 可读 | `apps/web/src/lib/stateDiagnostics.ts` | 静态复核补充：非空 `source_result_hash` 同步要求 64 位小写 SHA-256；测试与类型检查未运行 |
| A4 | P8 report fixtures | 将现有 `retro_audit` writer/报告种类加入 console fixture writer registry；不生成 fixture 文件、不改 report schema | `tests/research/reports/test_console_fixture_writers.py`、`apps/web/fixtures/README.md` | 源码复核完成；旧 1.0.0 JSON / fixture ID 登记留待验收同步 |

### Wave B：代码 / 文档状态对账

| ID | 任务 | 文件边界 | 状态 |
|---|---|---|---|
| B1 | 校正 ADR-0068 / 0073、ADR index、roadmap、状态页中 Proposed / 已实现 / operator 入口等过时措辞 | 仅事实状态与交叉链接；不得改 ADR 裁决 | Codex 统筹，Wave A 后进行 |
| B2 | 统一 Git 数字、P8 fixture 版本、P13 验收措辞、Phase 1 表数和 Phase 1 关闭条件的状态叙述 | `PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、被核实的对应文档 | 逐条读源核实后更新 |
| B3 | 整理 E1-CAP-ARCH 设计包：分离可在现有契约内处理的持有量与需新 ADR 的 metadata / retention 变化；记录失败候选数据的真实范围 | `docs/reviews/`、Proposed ADR 草案 | 首轮对账见 [`2026-09-28-e1-cap1-design-reconciliation.md`](../reviews/2026-09-28-e1-cap1-design-reconciliation.md)。E1-R 已在本地 main 有 batch-history、pinned batch reads、磁盘 positions、committed-row / close streaming、避免第二份 committed ID 列表、Verifier 快照匹配计数基础实现；公开 `snapshots_of_batches` 的 dict-of-lists 接口保持兼容。**批接口不等于内存有界**：PyIceberg 0.12.0 的 planner/task/delete 状态尚未证明有界；默认 tempfile 在本机落入 tmpfs。均未验证。32 MiB 完整工作集门槛不变；archive parse、完整结果 ID tuple、generic history fallback 与 Iceberg metadata 上界仍待处理或测量；E1-API 若改变公开结果类型先起 Proposed ADR，E1-H 需 L/H 分离证据。 |

### Wave C：额外模块静态审计发现的契约内缺口

| ID | 模块 | 任务 | 文件边界 | 状态 |
|---|---|---|---|---|
| C1 | P12 evolution | mutate 搜索空间成员校验改为精确类型和值；combine 对会沿用父代同一 `name@version` 的子代在创建前 fail closed，不猜版本演进规则 | `research/evolution/operators.py` 单模块 | 静态复核通过；未运行测试 |
| C2 | P13 execution | 未准入 deployment 必须在调用任何注入 target-position source 之前拒绝 | `apps/execution/service.py` 单模块 | 静态复核通过；未运行测试 |
| C3 | P14 migration | `GoldenRecord` 持有深度不可变输出快照；比较前验证 baseline 内容哈希；`GoldenDiff.differences` 复制、检查并冻结，防止报告结果被外部映射改写 | `infrastructure/migration/golden.py` 单模块 | Codex / Claude 静态复核未发现阻断项；未运行测试。公开类实例化的其他字段仍由 compare/rollback 的消费路径校验 |
| C4 | P9 docs | 把 ADR-0042 implementation note 对照现行代码更新为准确事实，保留当前报告证据规模仍仅适于 smoke 的限制 | `docs/adr/0042-synthetic-market-provider.md` | 事实同步已完成，待文档统一提交 |
| C5 | P9 calibration | false-positive rate / power 点估计使用固定 28 位 `ROUND_HALF_EVEN` context，不受调用方 ambient Decimal context 影响；不改阈值、报告字段或校准含义 | `research/synthetic_lab/calibration.py`、同目录 README | 静态复核完成；测试留后 |
| C6 | P13 second-line risk | 标记价格整批复制并预先校验，再更新 PositionBook；无效批次不得留下部分 mark 状态 | `apps/execution/risk.py`、同目录 README | 静态复核完成；测试留后 |

### 暂不实施，留到语义决定或验收窗口

- P0.5：不可替人工写 tags/assets/review；09-26 种子黄金哈希要留到允许运行 Python 的验收窗口。
- P1：E1-R 按对账文档开始基础实现；当前本地 main 增加 committed ID 重建与有界 snapshot match index，但仍未验证。archive parse、完整 result ID tuple、generic history fallback 的 `seen` set 与 builder 的 snapshot-ID set 仍在 E1 容量审计范围；D-LIST / ADR-0051 与新数据范围保留原门禁。
- P4/P5：不冻结 D-09/Profile 数值，不改变成本、split、threshold 或 outcome 选择语义。
- P7：六类组合算子、producer、execution lowering、失败修复与重试入口均待单独 ADR。
- P10/P11/P14：分别等待 deviation scope、ACTIVE/source resolver、migration target；P13 roadmap 验收措辞已与 ADR-0046 对齐。
- 所有已有测试文件缺口 / 兼容测试 / fixture 生成 / phase acceptance：集中留到后续验收批次；本阶段不运行。

## 并行与交付规约

- A1、A2、A3 可并行，因为分别属于 `research/hypotheses`、`infrastructure/event`、`apps/web`，不接触 `core/`。
- A1–A4 已完成源码复核，未运行测试 / build / lint / typecheck；C1–C3 由不相交模块并行处理，随后统一整合状态文档。

### 2026-09-28 跨阶段复核结论

- Phase 0.5–7：独立审计没有发现额外可在不触发人工审阅、运行验收或新架构决定的前提下直接编码的逻辑缺口。P4 converter 虽无生产调用点，但当前没有批准的运行时接点；保留纯函数，不新建写路径。P1 infrastructure Protocol / 测试代理接线已在 `34b95c3` 完成。ADR-0075 已接受，限定由 adapter 实现固定快照流式扫描；扫描实现尚未完成，E1-CAP-1 仍阻断。
- Phase 8–14：P10 deviation scope、P11 权威 ACTIVE/source resolver/dataset operator、P14 migration target 仍需语义或目标决定；P13 roadmap 已按 ADR-0046 修正为现有模拟功能范围，不需扩 ADR-0046。
- P10 只读审计确认 paper deviation 未绑定 ADR-0043/roadmap 所说的 P8 声明范围；需要先定范围身份与兼容策略，不增门槛。P11 只读审计确认 ADR-0074 synthetic-only 有限批次 CLI 已实现，缺口是未来的数据源真实性与 ACTIVE 权威语义，外部 scheduler 是既定边界。P13 roadmap 已对齐 ADR-0046。P14 无迁移目标前不写 target adapter；通用 GoldenRecord / GoldenDiff 不可变性已补到 C3，Claude 只读复核无阻断项。
- Phase 1 E1-CAP-1：对账文档列明 positions、committed columns、archive parse、batch index 和 result IDs 的代码持有量，以及 metadata / manifest 的未测边界。已加入 committed ID 重建与 Verifier 磁盘快照计数；`snapshots_of_batches` 公开 dict/list 接口保持不变。当前开发分支已将 `scan_column_batches` 加入 infrastructure `RevisionCatalog` Protocol，并给 revision `ProxyCatalog` 与 manifest-cache `_Recording` 代理补了转发 / head-read 记录；静态复核中、未运行检查。normalizer 的旧测试仍引用已删除的 `_same_numbers`。批次读取 API 的 PyIceberg planner、并发 task 和 delete 状态未证明有界；本机默认 tempfile 路径是 tmpfs。完整结果 tuple 仍计入 32 MiB 门槛；archive parse、generic history fallback 与 builder caller-held set 仍有未解决增长项。main 尚无容量证据，候选失败数据不外推。
- 每个开发代理一次只领一个 ID；Codex 复核后才进本地 `main`。Claude 可用最多 6 个子代理并行，但不能跨写同一模块。Cursor 此前确认 `phase/1` 没有独有实现；相关分支和 worktree 已归档清理。
- 交付须含：改动文件、任务 ID/Phase、对应 ADR/ROADMAP 约束、静态复核结果、未解决事项。测试、类型检查、lint、build 和验收状态统一标为“未运行”。

## PM 任务板（2026-09-28 起）

Raphael 于 2026-09-28 指定 Claude Code 以 PM 身份协调本轮：只有 PM 对 Raphael 汇报；Codex（`gpt-6-luna`）担任 Tech Lead，只执行 PM 签发的 Task Packet，不自设平行 GOAL；sonnet 子代理做只读审计与小切片修补，一人一任务、一个文件边界；涉及 `core/` 的任务串行。全员不跑测试 / probe / 验收（Raphael 2026-09-28：全部代码完成后再统一调试）、不宣称 E1-CAP-1 通过、不 push。Raphael 2026-09-28 决定所有模块底层代码都要补齐（含 E1），不得在单个模块死循环；新 ADR 由 Codex 依既有授权决定。子代理不能直接写根 checkout；PM 在自己的 worktree 分支整合后，由根 checkout 以 `--ff-only` 快进。

| ID | 模块 | 目标 | 执行者 | 文件边界 | 禁止项 | Done 定义 | 状态 |
|---|---|---|---|---|---|---|---|
| CL-1 | Git | 上一轮 21 个未提交 E1 路径整体归档，phase 分支恢复干净 | PM（仅 git 操作） | 根 checkout git 状态 | 改内容、stash、push | `wip/e1-cap-archive@ece150e` + archive ref；`git status` clean | ✅ |
| CL-2 | Docs | 挑回非 E1 事实同步（C5/C6、P9/P13），E1 叙述改为“余项已归档、后置”，记录协调方式 | sonnet | STATUS、MEMORY、本计划 | 代码、ADR、把 ADR-0076 写成 Accepted | 仅文档单提交 | ✅ `8a0adc4` |
| CL-3 | PM | 任务板落档（本节） | PM | 本计划 | — | 已提交 | ✅ |
| AUD-1 | 跨模块 | 静态列出测试与已提交实现的漂移（调试入口清单），只读不修 | sonnet | 无写入 | 运行任何检查、改测试、评估 E1 归档 | 每条带文件:行号证据 | ✅ 见下方清单 |
| W1-E1 | P1 E1 / Dataset | 审阅择取 `wip/e1-cap-archive`，定稿 ADR-0075 Amendment 1 / 0076 / 0077 并实现有界代码；单轮时间盒 | Codex | `feature/e1-bounded`；infrastructure E1 路径与测试、ADR-0075~0077 | 容量探针、宣称 E1-CAP-1 通过、弱化完整性验证、core/ | ADR 状态明确、代码与测试引用一致（未运行） | 🔄 |
| W1-P7 | P7 Discovery | ADR-0078：lowered outputs 权威；producer / lowering 与完整性校验 | Codex | `feature/p7-outputs`；research/hypotheses、research/experiments（lowering） | 改 `runnable=False` 默认、运行时启用算子、core/ | ADR + 实现 + 测试更新 | 🔄 |
| W1-P10 | P10 Router | ADR-0079：deviation 绑定 P8 声明范围身份与兼容版本 | Codex | `feature/p10-scope`；research/router | 新增 / 改变阈值、推翻 P10-FREEZE、core/ | ADR + 实现 + 测试更新 | 🔄 |
| W2-P11 | P11 Loop | ADR-0080：ACTIVE / source / metric 权威与 resolver | Codex | `feature/p11-authority`；research/loop（不含 dataset_*）、research/operations | 仓内 scheduler、伪造 Profile、dataset operator | ADR + 实现 + 测试更新 | 🔄 |
| W2-DTO | Apps | 各 report kind 版本化 payload DTO（ADR-0081） | Codex | 待 W1-P10 完成后签发 | — | — | ⏳ 排队（与 P10 共享 research/reports） |
| W2-P4 | P4 Outcome | 按 roadmap 判定并补 converter 调用接线 | Codex | 待 W1-P7 完成后签发 | 改 outcome 选择语义 / 阈值 | — | ⏳ 排队（与 P7 共享 research/experiments） |
| W3-P11D | P11 Loop | dataset operator（依赖 E1 新 Dataset API） | Codex | 待 W1-E1 合入后签发 | — | — | ⏳ 排队 |
| HUMAN | P14 / P0.5 | 迁移目标与 golden data；seed tags/assets 具名审阅 | Raphael / 具名审阅者 | — | 不代写、不猜 | — | ⏸ 需人工输入 |
| DBG-* | 各模块 | 逐模块调试 | — | — | — | — | ⏸ Raphael 决定：全部代码完成后再开始 |

### 调试入口清单（AUD-1，2026-09-28，静态只读，PM 抽查 M1–M3 属实）

覆盖 `44fe9a2..9e9bb9e` 各实现提交触及的模块。以下不是本轮实现缺口：修正需要运行测试或 writer 才能验证（M2 须重新生成 fixture），统一放到逐模块调试阶段。

| ID | 模块 | 位置 | 不一致 | 判断 |
|---|---|---|---|---|
| M1 | P3 Event（A2） | `tests/infrastructure/event/test_create_event_tables.py:48,78` ↔ `infrastructure/event/create_event_tables.py:66-75` | 测试桩无 `.definition`，仍断言旧输出；命令已输出 `name@version` 与 `definition_hash` | 确定漂移 |
| M2 | P8 retro_audit fixture（A4） | `tests/research/reports/test_console_fixture_writers.py:284-301` ↔ `apps/web/fixtures/retro_audit/05545674….json` | writer 已是 1.1.0，已提交 fixture 仍为 1.0.0 | 确定漂移；需运行 writer 重新生成 |
| M3 | P1 Normalizer | `tests/infrastructure/canonical/test_normalizer.py:932-933` ↔ `infrastructure/canonical/normalizer.py:1858` | 测试引用已删除的 `_same_numbers`，现为签名不同的 `_same_index_numbers` | 确定漂移 |
| M11 | P1 row_integrity | `infrastructure/revision/row_integrity.py:293-406` | SQLite spool / newest-match / close 路径无任何测试覆盖（经三次自我修正） | 覆盖盲区，调试时补测试 |

其余 8 项（P2 Web hash 校验、P9 Decimal context、P13 mark / admission、P12 精确类型、P14 golden、RevisionCatalog Protocol 代理、mixed-symbol 校验）静态核实与测试一致，部分新分支尚无覆盖。

## 阶段目标

1. 清理确认无活动会话的冗余分支/worktree，保留历史 archive ref 与失败证据。
2. 完成 Wave A 逻辑代码和 Wave B 文档对账，逐模块追加后续发现的已批准基础任务。
3. 形成明确的 handoff：哪些模块已实现但未验收、哪些仍待人工/ADR/数据门，以及建议的统一验收顺序。

本计划本身不表示 Phase 0.5 或 Phase 1–14 任一验收通过。
