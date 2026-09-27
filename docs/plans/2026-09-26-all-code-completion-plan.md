# HLENS-AutoResearch 全栈代码完成计划

日期：2026-09-26  
用途：交给 Claude Code Opus 作为执行目标；Claude 负责编码与每步文档 / Git，Codex 负责决策、协调、复核和恢复点把关。  
计划依据：`docs/research/roadmap.md`、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`docs/reviews/2026-09-25-framework-debug-backlog.md`、Phase 1 close evidence、当前候选分支。

## 1. 要完成什么

把路线图中**已批准范围内**的后端、研究引擎、数据管线、API、Worker 与 Web 前端代码补齐到可交付的实现状态，并为每个功能提供相应测试和文档。实现与验证分开记录：Claude 可以先完成功能代码和有针对性的单元 / 合同测试；后续由 Cursor Auto 承担独立调试、对抗测试和全量验证。任何功能在全量验收前都必须标为 `FRAMEWORK_IMPLEMENTED / NOT_VALIDATED` 或 `REVIEW_PENDING`，不得把“代码已写”说成“阶段已完成”。

这里的“全部代码”指路线图和已接受 ADR 明确批准的代码范围，不包括未经批准的市场交易能力，也不授权猜测验证阈值。Phase 13 维持模拟 / 纸面边界；不添加交易所账户、下单端点或密钥。Phase 14 的迁移目标未指定时只保留既有抽象，不臆造新基础设施迁移。

## 2. 当前基线和第一优先级

- 本计划启动时的主线基线：`10b89e8`（2026-09-27）。PR #6 已并入 Phase 0.5 / 3 / 10 / 11 收口项（合并基线 `4875e92`）；PR #7 同步合并后状态；PR #8 记录分支清理和测试 fixture 修正。当前 `main == origin/main`。全栈 B1～B67 和本批功能均不代表各 Phase 验收。
- 主线目前不是空骨架。Phase 0.5、Phase 2～14、API / Worker / Web 均有核心模型、Provider / 执行器、持久化或页面实现；Phase 8～14 审计未发现可明确追加的普通代码缺口。后续优先按证据补齐具体缺口，不再新增占位模块。
- Phase 1 当前状态：D3E（含 R1 / R2 / R3）已于 2026-09-27 验收，D4 已关闭；E1-CAP-1 仍阻断，已有 500k `resume` / `replay` RSS 增长 59.9 / 63.9 MiB 的失败结果（门槛 32 MiB）。只读代码调查发现待核实的主要增长来源是 PyIceberg table metadata 中随提交数增长的 snapshot 列表；normalizer 侧已是固定计数 / 流式遍历。调查记录见 `docs/reviews/2026-09-27-e1-resume-replay-memory-investigation.md`，未重新运行探针，也未找到可安全局部修复。不得并行修改 `infrastructure/canonical/` 或 `infrastructure/revision/`；需先设计并实现有界历史核验，再执行容量验收。
- 计划启动时（2026-09-27）的分支盘点快照：41 个本地分支、139 个 worktree；当时仅已合并且无 worktree 的冗余 `hold/adr-0054-0057-at-2.0.0` 已存入 `refs/archive/2026-09-27/` 后删除。其余分支仍由 worktree 检出或含未合并 E1 / P0.5 / Phase 7 / 文档材料；7 个 worktree-agent 由活跃 Claude 进程锁定，3 个检出目录有未提交改动。该数字是历史启动快照，不代表当前数量。
- 本轮分支清理结果：先归档再移除了 10 个补丁等价本地分支及其 worktree、68 个已在主线等价的 detached agent worktree、20 个已被主线后续实现取代的旧模块 worktree；16 个冗余远端引用也已归档清理。共清理 98 个 worktree；归档 tip 均可从 `refs/archive/2026-09-27/` 恢复。活跃 / 脏 / 锁定工作树、E1 与 Phase 0.5 独有内容、正式决定记录及 5 个独有远端分支均保留；分支审计未发现需要把旧实现整支合入当前主线的候选。
- 最近一次全量代码门禁见 §10.8 / §10.9 的各恢复点；PR #7 合并后的文档一致性检查为 7 passed。PR #8 合并前的全量检查为 7309 passed、138 skipped、1 failed；失败是 Event CLI 测试 DSN 使用 `secret` 而触发凭据卫生扫描。改为明确的测试占位符后，仓库卫生与 Event CLI 定向检查 9 passed，文档一致性检查 7 passed，Ruff、format、mypy 均通过。阶段验收、真实数据运行、Profile 数值冻结与外部 Uvicorn 安装仍分开处理。
- D3E 跨日证据边错误已在 `69f0bf0` 修复，并由 Codex 验收；不得再把该问题列为当前阻断。D-LIST / Profile 数值等属于明确决策或数据门，不通过猜测代码绕过。
- **当前收口批次已完成**：Phase 0.5 因子 / 特征草稿与 Event 草稿、Event schema 和运维 README、Loop / Router / API README、ADR-0066 与 Event 表操作入口已通过 PR #6 合并到 `main`。库草稿不是 KnowledgeItem，不含人工审阅标签 / 资产或实证结论。
- **下一优先级**：活跃 E1 会话结束后复核 bounded-memory 实现、固定规模探针结果和返修后定向测试；其余 Phase 8～14 审计未发现已批准而缺失的普通模块代码，不新增占位功能。阶段验收仍延后。

## 3. 项目红线与执行规则

1. 开始每一批前读取 `CLAUDE.md`、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md` 及该 Phase 的 ADR、契约、路线图验收项。聊天提示词不能覆盖仓库事实。
2. 只实现已接受 ADR 或已批准 roadmap 项。若方案触及宪法原则、Profile 数值、破坏性契约或未决架构决定，标记 `ARCHITECTURE_DECISION_REQUIRED` 并停止该项；不要自行填数值或假装通过。
3. `core/` 和冻结契约变更必须串行处理，并先有 ADR；不同代理不得同时编辑 `core/`。
4. 每个代理使用单独 worktree、限定文件和验收项。最多同时 4 个工作代理（Claude 子代理与 Cursor 代理合计），不允许重叠写文件；一名协调者负责集成。
5. 每个实现批次要有测试；Cursor 的后续调试可以找缺陷，但不能成为完全没有测试的理由。不得删失败实验、降级断言、修改验证门来使案例通过。
6. 每个恢复点由 Claude 更新 `PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、对应 ADR implementation note / 模块 README 与测试记录；提交描述写明 Phase、合同边界、测试原始结果和 ADR。
7. 按 Raphael 的 Git 要求，每个可审查进度提交到 WIP 分支并推送；推送前验证远程只会快进，禁止 force-push。未经 Codex 复核不得推进正式 `phase/1`；未经 Raphael 明确批准不得合并 `main` 或打 release tag。
8. 全系统工作完成后，仍按 Phase 顺序验收，不能因同一全量测试套件通过就把每个 Phase 标成完成。

## 4. 分阶段代码完成清单

以下是 Claude 的原始实施清单与验收标准，其中若干“当前边界 / 阻塞”列是在全代码合并前写的历史缺口描述，不再作为当前状态。当前进度以 §2、§10.10 和 `PROJECT_STATUS.md` 为准；不得把已实现的模块重复列为未开始。

| Phase | 代码交付范围 | 必须达到的代码验收 | 当前边界 / 阻塞 |
|---|---|---|---|
| 0 | Research Constitution | 现有宪法、生命周期与验证契约保持版本和 hash 一致 | 已关闭；不得借实现修改原则或阈值 |
| 0.5 | Public Knowledge Base：知识条目、来源 / 许可 / 证据等级、标签检索、KnowledgeProvider 插件 | 每项知识可追到来源；不可把无出处文本当证据；读写 API、持久化 / 查询、错误处理均有合同测试 | 验证来源版权和许可时不抓取或提交受版权保护全文 |
| 1 | Collector → Raw → Canonical → PIT → Dataset / Manifest → Representation / Feature；质量与证据缺口；REST repair | 逐项满足 roadmap #1–#21；跨日 R3 修复；D-NET 工具可离线重放且不消费旧输入；E2/D-LIST 保持 fail-closed | REST 历史列表假设 ADR-0051 仍暂缓；不请求 exchangeInfo；WebSocket 不实现；D3E 起须 Codex 接受门 |
| 2 | Market State：因果 StateProvider、状态值 lineage、持续时间 / 转移统计、固定训练窗口和 seed | 输入只来自 `as_of` 可见 feature；未来扰动不改变过去状态；重跑稳定；状态诊断可序列化 | 不以 Outcome 训练状态；不使用全样本回看拟合 |
| 3 | Event & Interaction：事件 provider、交互 DSL / 上游追溯、时序统计及需要的持久化接口 | 事件时间等于可观察时间；交互来源与 spec hash 可验证；缺上游证据时拒绝；补 roadmap 需要但当前缺失的持久事件表 / adapter 设计 | backlog 当前记录“一次请求只覆盖一个标的、暂无物理 Event 表”；需先核对 ADR-0036 表 / 契约边界，若需新表先立 ADR |
| 4 | Outcome 引擎、成本模型、G0–G3、sealed OOS 及必要的 Outcome / 评估持久化 | 防止 Outcome 反向成为输入；purge / embargo / walk-forward 有反例；负对照与空模型校准只产证据；失败与开封记录可靠 | P4 backlog 记录 Outcome / 开封数据存在内存路径、统计含浮点近似；先盘点现状再补。不得冻结 D-09 Profile 数字或用校准结果自动选值 |
| 5 | Strategy / Risk Provider 插件、Backtest v1、执行模型、基准对照 | 插件来源、参数空间、provider hash、成交成本 / 资金 / 容量假设可重现；失败结果进 Failure Registry | 当前 TSMOM 在 research 层；backlog 记无生产 `plugins/` StrategyProvider。部分成交按 ADR-0054；不得跳过 promotion / validation |
| 6 | State × Strategy 矩阵、条件假设和实验预登记 | 所有尝试进入 trial count；状态条件和样本支持规则显式；矩阵结果可回溯到 state 与 strategy 版本 | 不允许事后选择状态区间或隐去失败格 |
| 7 | Dynamic Discovery：组合算子 DSL、Hypothesis / ExperimentSpec 生成、LLMProvider 与 KnowledgeProvider 接线、批处理 | LLM 只提交数据；Schema 校验；来源、prompt、输出、参数、trial lineage 可审计；只执行已审查的注册算子 | `LlmCall` 当前只保证登记结构，内容取回 / hash 对应仍是已知缺口；Research Agent 不执行任意代码、不裁判验证 |
| 8 | Validation & Robustness：G4 C-R1–C-R5、回溯审计、报告接口 | 负对照、容量、跨资产、参数邻域、walk-forward、延迟 / 对齐扰动全有正负测试；配置缺失永不 PASS；异常分类为失败或 INCONCLUSIVE | backlog 记 P9 detector 异常传播、P8 单标的验证、标签因果性依赖调用方；D-PFIELDS 未冻结时保守 INCONCLUSIVE，不填数字 |
| 9 | Synthetic Market Lab：已知真值生成、纯噪声与植入效应校准、报告 / UI | 能测假阳性与检出力；报告种子、样本规模和区间；校准只呈证据不自动设阈值 | 当前小种子运行只属 smoke，不作为正式误差率保证或真实市场结论 |
| 10 | Dynamic Strategy Router（纸面）：策略资格、状态路由、切换成本、运行日志 | 路由只能选择已验证对象；结果含输入快照和规则 hash；纸面偏差可计算；无候选时明确停止 | P6/P10 backlog 记 router 身份仅由 run hash 绑定、切换成本与回测成本分开；逐条确认后补证据 |
| 11 | Continuous Research Loop：worker、调度、durable state、审计、预算、降级监控、Dashboard / API | 重启、任务重放、超预算、损坏日志、人工审批都 fail-closed 且有跨进程测试；API / UI 展示轮次和失败 | ADR-0050（审计记录 / 记忆检查点版本化）仍进行中；backlog 记非 research_loop 主题的尾部删除发现限制、无锚点限制，按 ADR 分批处理 |
| 12 | Strategy Evolution：谱系、变异 / 组合 / 退役和替换提案 | 后代是新版本；父子谱系持久、可验证；每个后代重新进入完整验证；不就地改 ACTIVE 策略 | 不自动批准或晋升 |
| 13 | Adaptive execution：隔离服务、模拟 venue、Kill Switch、独立风控、审计、监控 | 只完成模拟 / 纸面实现；kill switch 在场所端强制；风险与事件可重放；禁用实盘模式有测试 | 不实现交易端点、账户凭据、真实订单或杠杆；生产运营要 Raphael 独立批准 |
| 14 | Migration framework：Adapter / schema compatibility / golden replay | 现有迁移骨架有契约测试；每个具体迁移需 ADR、金标准实验差异报告和 rollback 证据 | roadmap 将实际完整迁移放在 P13 后；没有明确目标时只补通用接口，不引进 K8s / 新数据库等 |
| 全栈 | apps/api、apps/worker、apps/web | API 输入输出与领域 contracts / OpenAPI 一致；Worker job 生命周期、幂等、重试 / 失败可审计；前端各页面连接真实只读 API，提供 loading / empty / error 状态；类型检查、组件 / API 合同测试、生产构建通过 | 当前状态称 apps 框架已实现、5 页；backlog 仍称矩阵 / Router 页面只有 API 与计数。必须盘点实际 UI，禁止以截图 / 假数据代替服务链路 |

## 4.1 当前状态解释

- B1～B67 和对应模块已在 `main`，整体为 `CODE_COMPLETE / DEBUG_PENDING`；这表示实现批次完成，不等于 Phase 验收。
- 原表的“当前边界 / 阻塞”列不应作为新的任务队列直接执行。每项工作先对照当前主线、已接受 ADR 与 §10.10；确认缺口仍存在且属于批准范围后再分配。
- 本轮并入主线的新增内容与状态见 §10.10。真实数据、外部运行依赖、人工知识审阅和 Phase 验收仍是独立门槛。

## 5. 任务顺序与并行分组

### Wave 0：Phase 1 阻断缺陷与恢复点

1. 修复 D3E 跨日 provenance 不变量；新测试复现旧提交失败并覆盖两种日期到达顺序。
2. 单独量化 1 / 10 / 30 / 更多 edge microbatches 与无关 catalog snapshots 的成本；提出容量边界，不以性能优化削弱 provenance。
3. 硬化 D-NET offline runner：输入文件绑定日期 / 状态 / code rev / snapshots；失败前置步骤后不得消费旧 JSON；F2/报告/PIT 的网络拒绝测试真实有效。
4. 校正 R3、snapshot scan、D-NET 的过期状态 / memory / close-evidence 文案。Phase 1 仍 `REVIEW_PENDING`，直至 Codex 逐项签收。

### Wave 1：剩余批准的研究引擎代码

按依赖顺序实现或补齐 0.5 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9。Phase 4 的 Profile 阈值和初始冻结必须停在“提供可校准流程 / TBD 占位”；不得为了通过 Phase 5 设置随意阈值。每个模块先清点现有代码，再以单一任务补一个可观察的交付。

### Wave 2：Router、Loop、Evolution、Simulation、Migration

按 10 → 11 → 12 → 13 → 14 补接线、持久性、Paper / Simulation 工作流和恢复证据。任何跨阶段接线必须有一条真实对象链的集成测试；P13 仅模拟。实际 P14 migration 延后到具备目标和 ADR。

### Wave 3：API、Worker、Web 前后端闭环

按下列顺序完成纵向切片：

1. API：路由与 service/use-case 对应，错误状态稳定，响应 DTO / OpenAPI 从同一来源校验。
2. Worker：任务只调用正式 service；持久结果、重投、状态推进与 audit 绑定；API 可查询任务结果与失败原因。
3. Web：Dashboard / Knowledge / Strategy / Matrix / Router / Loop（按仓库现有页面实际盘点）逐一接真实 API；补加载、空结果、错误、权限 / 只读状态；移除与生产路径混淆的 fixture 数据。
4. 验证 Python 与前端 lockfile、lint、typecheck、unit tests、production build；至少一条端到端流程从 API 进入 Worker 再由 Web 读回结果。

## 6. Agent 分工约束

同一时段最多四个执行代理（包括 Cursor Auto），另由一名 Claude Opus 协调：

- Opus 协调者负责派单、依赖、架构审查、结果集成与阶段计划；不与子代理同时编辑同一模块。
- Sonnet / Opus 子代理各自负责一个 module / 插件 / UI 层；报告限制到的文件、验收项、测试输出和 ADR。
- Cursor Auto 用于低耦合基础实现、前端页面 / API 连接、重复模板和独立调试；高风险 D3E / validation / lifecycle 语义由 Claude 实现并由 Codex 复核。
- `core/`、冻结 contracts / Schema、ADR 和跨 Phase data flow 一次只允许一个代理处理；需要变化时必须先提出 `ARCHITECTURE_DECISION_REQUIRED`。
- 每个代理结束后先停止其 worktree 写入，协调者再 cherry-pick / 合并该工作；任何测试失败保留日志，禁止代理自动清理失败试验。

## 7. 每个工作项的定义完成条件

一项只有同时满足以下条件才可标为 `CODE_COMPLETE`：

1. 对应 roadmap / ADR 的功能可从公开入口调用，无仅用于测试的旁路或静默默认。
2. 有正向测试、拒绝 / 篡改 / 重放等负向测试，以及所需 contract / integration 测试。
3. docs、API schema、报告与实际行为一致；状态明确区分已实现、已调试、已验收。
4. Ruff、format、mypy / frontend typecheck、相关 tests 实际运行并记录精确结果。
5. 一个清晰 Git commit；无数据、密钥、`.env`、warehouse 文件或临时数据库进入 Git。
6. Codex review 未发现 P0/P1 阻断；Phase acceptance 由 Codex / Raphael 的对应门决定。

`CODE_COMPLETE` 仍不等于 `VALIDATED`、`ACCEPTED` 或项目完成。失败门、未决 ADR、Profile TBD 和 Phase 验收项必须继续显示为未完成。

## 8. Claude 执行报告格式

每个子代理与每个恢复点报告：

- Phase、任务 ID、目标验收编号；
- 修改文件与 API / Schema / ADR 触及范围；
- 实现事实、运行过的命令及原始通过 / 失败摘要；
- 新增测试如何覆盖原缺陷、旧版本是否能复现失败；
- 已知限制、容量边界、`ARCHITECTURE_DECISION_REQUIRED`；
- commit hash、branch / remote push 结果；
- 下一任务的依赖和未完成条件。

## 9. Claude 新会话的首要命令

先确认 `/home/raphael/projects/hlens-autoresearch` 当前分支，以及候选 worktree 的 HEAD；如果新会话在 `phase/1`，先读取计划并移动到已批准的候选工作区 / WIP worktree，不在正式分支叠加全部框架代码。开始任何改动之前，先向 Raphael 明确回报当前 worktree、branch、HEAD、未提交改动和你准备启动的最多四个子任务；不得清理或重置既有用户改动。


## 10. 执行记录（Claude 会话 `2026-09-26 全阶段代码完成计划`）

分支 `claude/2026-09-26-code-completion-337e38`（worktree `.claude/worktrees/2026-09-26-code-completion-337e38`），自候选基线 `50a43a4` 快进，
自成一体、不 rebase 到集成会话；WIP 只快进推送到 `wip/all-code-completion`。状态词：`CODE_COMPLETE / DEBUG_PENDING` = 代码与定向测试已写并实际运行，
完整调试、对抗复测与全量门禁留给 Cursor Auto；**不等于** VALIDATED / ACCEPTED，也不代表任何 Phase 已验收。

### 10.1 开工核查（2026-09-26）

- 正式目录 `/home/raphael/projects/hlens-autoresearch` 在 `phase/1`（`52f7477`），未改动。本分支原在 `1e208b5`（main），快进到 `50a43a4`。
- 集成会话"HLENS-AutoResearch 后续开发"（dev-c05c2b 的所有者）仍在运行，已提交 D3E-R3（`fix/d3e-r3-cross-day` `557774c`）、ADR-0053（`0d5a975`）、
  ADR-0054（`d640eff`），并持有 `docs/d3e-fact-sync`（`412cd68`）；Cursor 持有 D-NET 工具硬化（`fix/dnet-tool-state` `ae365ec`）。按 Raphael "复用、禁止重复"
  的要求，Wave 0 第 1、3、4 项和 ADR-0053 / 0054 **由它们负责，本分支不重做**；经跨会话协调确认分工：本分支只做非 `core/`、非冻结契约 / Schema / ADR 的代码缺口。
- 集成会话告知：它今天按 Raphael 最新指示只做 Phase 1 返修，不 cherry-pick 本分支；本分支因此保持基于 `50a43a4` 自成一体。

### 10.2 ARCHITECTURE_DECISION_REQUIRED（本轮发现 / 确认）

| ID | 问题 | 涉及 | 可选方案 | 推荐 | 不决定的影响 |
|---|---|---|---|---|---|
| ADR-0052-IMPL | ADR-0052（Accepted）要求契约加精确小数 / Profile 字段，需把 `CONTRACT_SCHEMA_VERSION` 2.0.0→2.1.0；该版本号写入每行 Canonical（`infrastructure/canonical/rules.py`），重新规范化按列精确比较（`normalizer._exact`），升版会使已提交的 D-NET 行不可重放、所有 request / result 哈希变化（集成会话核实，ADR-0052 §4 / ADR-0054 §5 要求升级为人类决定） | `core/`、Schema、Canonical 重放 | A：Canonical 行的规则版本与契约信封版本解耦后再升版（需 ADR）；B：接受重放断裂、重建 D-NET 数据；C：暂缓 ADR-0052 实施 | A（需 Raphael / Codex 决定） | ADR-0052 保持未实施；验证结果与 Profile 仍用浮点（同平台可复现），负对照与 G3 仍共用阈值（偏保守） |
| P3-EVTABLE | Phase 3 验收要求"补 roadmap 需要的持久事件表 / adapter"；`infrastructure/event/table.py` 明写物理 `event.*` 表是"另行决定"，计划本身要求"若需新表先立 ADR" | 数据架构冻结表清单、ADR-0036 | A：起草 Proposed ADR（Iceberg `event.*` 表的分区、身份、只追加语义），批准后实现；B：维持内存物化 + 可哈希导出 | A | 事件只能每次重算；不影响因果正确性 |
| P3-MULTISYM ✅ ADR-0057（2026-09-26，方案 A） | `EventRequest`（`core/contracts/event.py`）无标的字段，多标的只能逐序列请求；加字段 = 改冻结契约 | `core/contracts/event.py`、Schema | A：additive 可选字段 + ADR（须证明省略时哈希不变）；B：维持每序列一个请求，由调用方组合 | **A**（2026-09-26 更正：ADR-0057，一请求一标的，`subject` 为调用方提供的稳定、大小写敏感 opaque ID；Codex `648fe6c` 认可语义）；代码先在 2.0.0 下写成（按 K3 / K5 当时未接受），已于 B41 以 2.1.0 重新声明，待 Codex 复核 | 多标的事件需多次请求；`564c87c` 仅为保留恢复点 |
| P5-PLUGIN | 计划记"尚无 `plugins/` 生产 StrategyProvider"：TSMOM 在 `research/`，按 H5 研究代码只能经 Promotion 流程成为生产代码；把它搬进 `plugins/` 即是晋升 | H5、ADR-0005、ADR-0038 | A：等某策略经完整验证 + Promotion；B：另立 ADR 定义"研究库 Provider 进入 plugins 的最小晋升证据" | A | 策略只在研究层；不影响研究与验证链路 |
| P05-WRITE | 知识库写入路径（审阅后入库的 Python API / CLI）超出只读检索的 ADR-0034；CLAUDE.md §5 要求新插件能力先立 ADR。集成会话已按 Raphael 的 Phase 0.5 指示接手 0.5 并起草 ADR（tag / 资产 / 状态检索） | ADR-0034、`plugins/knowledge` | A：Proposed ADR 定义人工审阅入库流程后再实现；B：维持只读，条目由人直接提交 JSON | A（由 Phase 0.5 所有者起草） | 知识条目只能由人手工提交 JSON；本分支**不集成** infra 通道的写入提交 |
| P10-ELIG | 路由资格信任调用方给出的生命周期映射；把资格绑定到验证报告证据若需改契约则须 ADR（engines 通道在不改契约的前提下尽量加可选核对） | ADR-0043、`core/contracts` | 见 engines 通道报告 | — | 路由仍只接受调用方声明为已验证的对象 |

**10.2 状态更新（K2，2026-09-26）**：P05-WRITE → ADR-0058 Accepted、已集成（B15，`f1e71ba`）；P10-ELIG → 研究层证据模式已集成（B16，`49ccefb`）；
P3-EVTABLE → ADR-0056 Accepted、已集成（B17，`7193b65` / `af7a351`）；P3-MULTISYM → ADR-0057 代码已集成（B23，`dd2bc6a`），并于 B41 以 2.1.0 重新声明（待 Codex 复核）；ADR-0052-IMPL → 已实施：独立 Phase 1 分支 `phase1/adr-0052-versioned-replay`（M0～M3，`8a7655e`，门禁修复 `22392ea`，B38 / B40）并经 core 合并通道合入本分支（B41）；ADR-0054 / 0057 已以 2.1.0 重新声明（B41）；P5-PLUGIN、Profile 数值、实盘、合并 `main` → 决定不做（见自主决策记录）；D-LIST → ADR-0051 Proposed，Raphael 明确暂缓（B43 撤回一度的"原则接受"）。

### 10.3 批次记录

**B1 — Phase 9：检测器异常计为 INCONCLUSIVE**（`CODE_COMPLETE / DEBUG_PENDING`）

- 文件：`research/synthetic_lab/gate_calibration.py`、`research/synthetic_lab/calibration.py`、`research/synthetic_lab/README.md`；测试
  `tests/research/synthetic_lab/test_gate_calibration.py`（+4）、`test_calibration.py`（+2）。未触及 `core/`、契约、Schema、ADR。
- 行为：`detect` 抛出 → 无门 `INCONCLUSIVE` 运行 + `detector_error`，各组 `detector_errors` 计数，不计通过、不消耗封存 OOS；新键只在有错误时出现（无错误报告哈希不变）；
  `DetectorConfigurationError` / 报告 Profile 不符 / 真值不符仍抛出。`calibrate` 的异常计入 `noise_errors` / `planted_errors`。
- 实际运行：`uv run pytest -q tests/research/synthetic_lab tests/research/reports` → 44 passed, 1 warning；`uv run ruff check`、`ruff format --check`、
  `uv run mypy research/synthetic_lab tests/research/synthetic_lab` → 全部通过（mypy: no issues in 8 files）。
- 旧版本复现：新测试 `test_a_raising_detector_yields_inconclusive_runs_not_an_exception` 在 `50a43a4` 上会因 `ZeroDivisionError` 传播而失败（旧代码无捕获）。

**B2 — Phase 13：持久执行审计、fail-closed 重启与只读重放**（`CODE_COMPLETE / DEBUG_PENDING`；仍只模拟）

- 文件：`apps/execution/audit.py`、`service.py`、`__init__.py`、`README.md`；测试 `tests/apps/test_execution_durable_audit.py`（新，10 项）。未触及 `core/`、契约、Schema、ADR；
  无交易端点、无凭据、无网络（执行服务边界测试仍通过）。
- 行为：见 `apps/execution/README.md`「持久审计与重启」。旧版本复现：`50a43a4` 上 `ExecutionService` 无 `audit` 参数、`AuditTrail` 无 `path`，新测试全部因 TypeError 失败。
- 实际运行：`uv run pytest -q tests/apps/test_execution_durable_audit.py tests/apps/test_execution.py tests/apps/test_execution_strategy_source.py tests/test_architecture_boundaries.py`
  → 47 passed；`ruff check apps/execution tests/apps` → All checks passed；`ruff format --check` → 15 files already formatted；`mypy apps/execution tests/apps/test_execution_durable_audit.py` → no issues in 14 files。


**B3 — Phase 12：替换提案（只提案、恒待人工批准）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 文件：`research/evolution/proposals.py`（新）、`research/evolution/__init__.py`、`research/evolution/README.md`（新）；测试 `tests/research/evolution/test_replacement_proposals.py`（新，19 项）。
  未触及 `core/`、契约、Schema、ADR。
- 行为：`propose_replacement` 核对现任 ACTIVE / DEGRADED、候选为源自现任的新版本且谱系可完整追溯、候选在自身历史上处于 PAPER / PRODUCTION_CANDIDATE、证据 / 理由 / 提出者非空；
  产出 `status` 恒为 `PENDING_HUMAN_APPROVAL` 的 `ReplacementProposal`；`ProposalLedger` 哈希链只追加、无批准方法。限制：`require_new_version` 拒绝同版本号候选（保守，未改）；未接入循环。
- 实际运行：`uv run pytest -q tests/research/evolution` → 34 passed；此前同批 `tests/research/evolution tests/research/loop/test_loop_e2e.py` → 56 passed + 1 failed（修复前的测试夹具错误，已修，evolution 目录复跑 34 passed）；
  `ruff check` → All checks passed；`ruff format --check` → 9 files already formatted；`mypy research/evolution tests/research/evolution` → no issues in 8 files。

**B4 — Phase 11：文件事件总线的外部锚点（任意主题的尾部删除可发现）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 文件：`infrastructure/event_bus/file.py`、`infrastructure/README.md`；测试 `tests/infrastructure/event_bus/test_file_event_bus_anchor.py`（新，16 项，含锚定总线通过同一 bus contract suite）。
  未触及 `core/`、契约、Schema、ADR；不给 `anchor` 时磁盘布局与行为不变。
- 关闭 backlog C「P11 仍未做：最后一次写消费者状态之后追加、又被尾部删除的消息不可发现（`research_loop.round` 之外的主题）」——在给出锚点时。
  限制：锚点与日志一起被尾删仍不可发现；持久循环组合根尚未自动给总线配锚点（跨阶段接线，后续）。
- 实际运行：`uv run pytest -q tests/infrastructure/event_bus` → 43 passed；`ruff check` → All checks passed；`mypy infrastructure/event_bus tests/infrastructure/event_bus` → no issues in 6 files。

**B5 — Phase 3：事件运行存储与统计序列化**（`CODE_COMPLETE / DEBUG_PENDING`）

- 文件：`infrastructure/event/store.py`（新）、`infrastructure/event/__init__.py`、`research/events/stats.py`、`research/events/README.md`；
  测试 `tests/infrastructure/event/test_event_store.py`（新，6 项）、`tests/research/events/test_event_stats.py`（+2）。未触及 `core/`、契约、Schema、ADR；Iceberg `event.*` 表仍待 ADR（P3-EVTABLE）。
- 实际运行：`uv run pytest -q tests/infrastructure/event` → 49 passed；`uv run pytest -q tests/research/events` → 7 passed；`ruff check` / `ruff format --check` → 通过；
  `mypy infrastructure/event tests/infrastructure/event` → no issues in 11 files；`mypy research/events tests/research/events` → no issues in 4 files。


**B6 — engines 子代理：Phase 2 / 6 / 10**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `02daca7`、`ed11958`、`d0c940c`）

- Phase 2（`02daca7`）：`StateDiagnostics.to_payload()` / `diagnostics_hash` / `from_payload(expected_hash=)`（Decimal 精确文本、UTC ISO、拒绝浮点 / 非规范 / 哈希不符），`diagnose()` 不变；9 项新测试。
- Phase 6（`ed11958`）：`register_matrix_conditionals(ledger, matrix, *, state_spec, family_id, minimum_effect, min_support)`——单元来自声明的 `state_space` + 未知状态单元，
  先全部核对再登记（冲突即一条不登记），`min_support` 必填无默认（`None` = 全部报告为无支持阈值），低于阈值保留并标注；无按结果选标签的入口；9 项新测试。未接入循环。
- Phase 10（`d0c940c`）：`RouterStopped`（`no_validated_candidate` / `all_routes_flat`）与 `paper_run_or_stop` 返回带哈希的 `RouterStop`；`RouterPaperRun.expected_run_hash()` / `verify()`
  及逐项篡改测试；可选 `validation_reports`（给出时每个可路由策略须恰有一个报告哈希，记录并入哈希）；既有 run hash 固定测试不变；13 项新测试。
  剩余：生命周期映射未入 `run_hash`（入则改既有哈希）；`research/reports/router.py` 尚未写出 `validation_reports` / `RouterStop`；P10-ELIG 仍待决定。
- 子代理实际运行：`pytest tests/research/states tests/research/experiments tests/research/router tests/research/reports/test_writers.py tests/research/test_cross_phase_e2e.py -m "not postgres"` → 77 passed；
  `tests/research/loop/test_loop_units.py` → 4 passed；ruff / format / mypy（15 files）通过。集成后本分支复跑 `tests/research/{states,experiments,router,reports}` + cross-phase e2e → 77 passed, 1 warning；
  `ruff check research tests/research` → All checks passed；mypy（15 files）→ no issues。

**B7 — Phase 10 报告：`router_stop` 报告种类与验证报告绑定写出**（`CODE_COMPLETE / DEBUG_PENDING`）

- 文件：`research/reports/router.py`、`research/reports/__init__.py`；测试 `tests/research/reports/test_router_stop_writer.py`（新，2 项）。
- `write_router_stop` 写 `<root>/router_stop/<stop_hash>.json`（独立种类，不与纸面运行混形）；纸面运行载荷仅在提供时写 `validation_reports`（未提供的载荷逐字节不变）。
  API / 控制台对 `router_stop` 的展示待 apps 通道合入后串行接线。
- 实际运行：`uv run pytest -q tests/research/reports tests/research/router` → 43 passed, 1 warning；`ruff check` → 通过；`mypy research/reports tests/research/reports` → no issues in 10 files。

**B8 — Phase 11 接线：持久循环的自动总线可配外部锚点**（`CODE_COMPLETE / DEBUG_PENDING`）

- 文件：`research/loop/compose.py`、`research/loop/dataset_compose.py`、`research/loop/README.md`；测试 `tests/research/loop/test_loop_durable.py`（+2）。
- `open_synthetic_loop` / `open_dataset_loop` / `compose_durable` 新增可选 `bus_anchor`，交给自有 `FileEventBus(..., anchor=)`；与调用方总线同给即拒绝（在触碰 `state_dir` 之前）。不给时行为与记录哈希不变。
- 实际运行：`uv run pytest -q tests/research/loop/test_loop_durable.py` → 58 passed (75 s)；`ruff check research/loop tests/research/loop` → 通过；`mypy research/loop tests/research/loop/test_loop_durable.py` → no issues in 11 files。

**B9 — apps 子代理：API / Worker 读端点 / Web 控制台**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 apps 两个提交 + 本提交的 3 处研究测试形状修正）

- API（`apps/api/app.py`、`store.py`）：知识检索无 Provider → 503、Provider 错误 → 502、`response_model=KnowledgeResult` 与统一 `ApiError`；`GET /reports/{kind}` 改为
  `{kind, reports, invalid:[{id, reason}]}`（坏文件不再静默跳过，非 UTF-8 也归为 invalid，理由不含服务器路径）；只读 `GET /jobs`、`GET /jobs/{job_id}`（经 worker 的
  `read_job_results` 复核哈希链；未配置 503、日志不可信 500、非法 id 400、未知 404；`jobs_idempotent` 须与运行器一致，否则有重跑行时 500——已写入文档）。
  API 仍只读（测试断言唯一 POST 是知识检索）。`openapi.json` 由 `python -m apps.api.openapi` 重生成，`src/api.d.ts` 由 `npm run gen:api` 生成。
- Worker（`apps/worker/jobs.py`）：重放逻辑抽为 `_Replay`（运行器行为不变），公开只读 `read_job_results` → `JobHistory` / `JobRecord`（`JobStatus` 字面量）。
- Web：修复研究循环页读取不存在字段（`budget_used` / `failures`）的缺陷，改读 `status`、未完成阶段及错误、轮次 / 累计用量、超支、转移；全部 9 页显式加载 / 空 / 错误状态
  （`src/lib/useApi.ts`、`loadState.ts`、`components/States.tsx`、`ReportBrowser.tsx`）；列表页显示 invalid 警告；知识检索显示 503 / 502；新增只读 Jobs 页；
  校准页显示 `detector_errors` / `detector_error`；纯逻辑 `src/lib/*.ts` 用 `node --test`（无新依赖，`package-lock.json` 不变，`tsconfig.test.json` 让 tsc 检查测试文件）。
- 本分支集成后实际运行：`pytest -m "not postgres" tests/apps tests/research/reports tests/research/synthetic_lab tests/test_architecture_boundaries.py tests/test_docs_consistency.py`
  → 240 passed, 1 warning；`ruff check apps tests/apps tests/research` → All checks passed；`ruff format --check apps tests` → 273 files already formatted；
  `mypy apps tests/apps` → no issues in 36 files；`npm ci --offline` → 成功；`npm test` → tests 21 / pass 21 / fail 0；`npm run build` → ✓ built（最大 chunk echarts 375.43 kB）。
- 待 Cursor：未在浏览器对真实后端手工验证；`GET /reports/{kind}/{id}` 的 422 消息仍含文件路径（既有行为）。

**B10 — validation 子代理：Phase 8 / Phase 4**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `b69dcf3`、`bef83a8`、`fbf2ab3`、`f527d3e`）

- Phase 8（`b69dcf3`）：G4 九项检查各自隔离，意外异常 → 该检查唯一的 INCONCLUSIVE `<prefix>.check_error` 门（异常类型 + 确定性短消息），其余检查继续，整体至多 INCONCLUSIVE；
  `ValueError`（含 `UnsupportedMethod` / `ProfileFieldMissing`）、`TypeError`、`MemoryError` 仍抛出，`KeyboardInterrupt` 从不捕获；无异常时输出逐字节不变（momentum / noise 夹具哈希固定）。
  行为变化：以前意外异常会从 `validate` 传出；现在是报告中的 INCONCLUSIVE 门、不写 Failure Registry。
- Phase 4（`bef83a8`）：可选 `InSampleInput.control_seeds` / `ValidatorSetup.control_seeds`（无默认种子列表）；逐种子门 `G1.shuffle_control.seed.<s>`、`G1.shift_control.seed.<s>` 在基门 id 下判定（Profile 区间适用），
  基门按标准规则聚合并报告最小 p 值；不给时门逐字节不变（planted / noise / leaky 夹具哈希固定）；G2 空模型仍单种子。
- Phase 4（`fbf2ab3`）：`research/outcomes/store.py` 写一次、内容寻址的 Outcome 表持久化（`<root>/<table_hash>.json`，临时文件 + `os.link`，从不覆盖；读取重算表哈希并重建 `OutcomeResult` 复核；
  篡改 / 截断 / 非规范 / 伪造哈希 / NaN / 重复键均拒绝）；`OutcomeTable` 新增可选 `request_hash` / `provider_hash`，`rows()` 不变；Outcome 不作输入的守卫不变。
- 子代理实际运行：`pytest tests/research/validation tests/research/outcomes tests/research/strategies/test_backtest_validation.py -m "not postgres"` → 204 passed；边界 / 信息流 / 验证架构 / outcome 契约 → 263 passed；ruff / format / mypy（38 files）通过。
- 本分支集成后实际运行：`pytest -m "not postgres" tests/research/validation tests/research/outcomes tests/research/strategies tests/research/loop/test_loop_units.py tests/research/synthetic_lab tests/test_architecture_boundaries.py tests/test_information_flow.py tests/test_validation_architecture.py tests/test_outcome_contracts.py`
  → 511 passed, 1 warning (142.8 s)；`ruff check research tests/research` → 通过；mypy（validation / outcomes / strategies.validation + 测试）→ no issues。
- 限制：固定的哈希来自浮点，跨平台可能需重新固定（同 D-FLOAT）；浮点正态近似与 G1 / G3 共用阈值仍待 ADR-0052 实施（被 10.2 的 ADR-0052-IMPL 阻塞）。

**B11 — infra 子代理（Phase 7 / 14）+ Phase 7 循环接线**（`CODE_COMPLETE / DEBUG_PENDING`）

- 集成（infra 通道 `02048ff`、`d7770e8`、README 提交去掉 `plugins/knowledge/README.md`；知识写入提交 `0c10a65` **未集成**，见 10.2 P05-WRITE，Phase 0.5 由集成会话负责）：
  - Phase 7：`infrastructure/content/`（`LocalContentStore`：`<root>/sha256/<ab>/<hash>`，引用 `cas://sha256/<hash>`，读取重算哈希与大小，篡改 / 截断 / 缺失 / 外来引用拒绝，从不覆盖；`verify_llm_call`）；
    `ScriptedLLMProvider(store=)`（plugins 内的 `BlobSink` Protocol，不 import infrastructure；不给 store 时逐字节不变，5 个参考哈希固定）。
  - Phase 14：`save_golden` / `load_golden`（内容寻址、加载校验）、`GoldenDiff.report()`、`rollback.py` 的 `rollback_evidence`（容差 0 下与金标准逐位一致才 `RESTORED`，只是证据、不执行回滚）、
    测试侧把 event-bus 契约套件经 `run_conformance` 跑在内存与文件总线上（故意坏的总线失败）。不引入任何新基础设施。
- 本分支新增：`research/loop/llm_content.py`（`ContentVerifiedLLM`、`verify_call_content`：引用可取回且取回内容**等于**实际交换的 prompt / input / output）、`research/loop/stages.py`（假设阶段取用已审阅草稿时再核对）、
  `research/loop/compose.py`（resolver 交给假设阶段）、`research/loop/README.md`；测试 `tests/research/loop/test_llm_content.py`（5 项，含"审阅后内容消失 → 该轮假设阶段 FAILED、其后 SKIPPED、草稿不登记"）。
- 实际运行：`pytest -m "not postgres" tests/infrastructure/content tests/plugins tests/infrastructure/migration tests/infrastructure/event_bus tests/test_architecture_boundaries.py tests/test_feature_contracts.py tests/test_llm_call_bindings.py tests/test_docs_consistency.py tests/research/hypotheses`
  → 487 passed；`pytest -m "not postgres" tests/research/loop tests/research/hypotheses tests/apps/test_research_loop_durable.py tests/test_architecture_boundaries.py` → 154 passed, 1 warning (312 s)；
  `tests/research/loop/test_llm_content.py` → 5 passed；ruff check → 通过；mypy（content / migration / plugins.llm + 测试 15 files；research/loop + 新测试 12 files）→ no issues。

**B12 — reports 子代理：三个新报告种类端到端**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `1bd1dd3`）

- `research/reports/state_diagnostics.py`（`write_state_diagnostics`，写前 `from_payload(expected_hash=)` 往返校验）、`event_statistics.py`（`write_event_statistics`，重算哈希不符即拒）、`router_stop`；
  `apps/api/store.py` 的 `ReportKind` 新增 `ROUTER_STOP` / `STATE_DIAGNOSTICS` / `EVENT_STATISTICS`（仍只读，无新端点）；`openapi.json` / `api.d.ts` 重生成；控制台新页 `RouterStops` / `StateDiagnostics` / `EventStatistics`
  （复用 `ReportBrowser` / `useApi` / `States`，`REPORT_KINDS` 以 `ReportKind` 为键，API 新增种类而控制台未列出时 tsc 失败）；夹具由真实 writer 生成并有逐字节比对测试（`tests/research/reports/test_console_fixture_writers.py`）。
- 限制：未在浏览器对真实后端手工验证；API 不核对文件名与载荷哈希一致（既有种类亦然）；event_statistics 夹具用测试中的替身运行哈希；ADR-0048 仍写"四种"（ADR 未改）。

**B13 — calibration 子代理：Phase 9 校准可选运行 G5**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `dd195a2`）

- `GateCalibrationSetup.sealed_oos_g5`（默认 False；关闭时 3 个既有报告哈希固定不变）；开启时只有 G0–G4 PASS 的运行开封本族并认领唯一评估后才释放封存 bar（`SealedRelease`），
  `StrategyValidatorDetector.detect_sealed` 仿循环在研究 + 已释放封存 bar 上重跑并调用 `run_sealed_oos`；报告逐组 G5 通过 / 不确定 / 失败率与端到端 G0–G5 假阳性率 / 检出力（Clopper-Pearson）；
  提前结束 → `consumed_without_result`，`detect_sealed` 异常 → 无门 INCONCLUSIVE + `detector_error`；检测器配置错误仍抛出；没有 `detect_sealed` 的检测器在开启时被拒。仍只是证据、不选 Profile。
- 限制：每次运行是独立的模拟族（G5 上下文以 harness 族覆盖元数据族 id，已文档化）；种子数少、市场 3 天，只够冒烟。
- 两个通道合入后本分支实际运行：`pytest -m "not postgres" tests/apps tests/research/reports tests/research/synthetic_lab tests/test_architecture_boundaries.py tests/test_docs_consistency.py` → 290 passed, 1 warning；
  `ruff check .` → All checks passed；`ruff format --check .` → 634 files already formatted；mypy（apps / research.reports / research.synthetic_lab + 测试，59 files）→ no issues；`npm test` → 36 / 36 pass；`npm run build` → ✓ built。

### 10.4 授权变更（2026-09-26 晚）

Raphael 设定新目标："所有的决策都由你来决定，包括红线的事情……每个步骤推一下 git，最大允许 6 个子 agent，允许与另一会话沟通"。逐项裁决见
[自主决策记录](../reviews/2026-09-26-autonomous-decisions.md)。10.2 表中的阻塞项据此重新处理：ADR-0052 / 0053 / 0054 / 0056 / 0057 由 core 与 P3 通道实施，
P05-WRITE 由 ADR-0058 接受并集成，D-LIST 保持 Raphael 的明确暂缓（ADR-0051 Proposed；B43 撤回了一度的"原则接受"），P5-PLUGIN / Profile 数值 / 实盘 / 合并 `main` 决定不做。

**B14 — 全量门禁（不含 PostgreSQL）**：`uv run pytest -q -m "not postgres"`（HEAD `8d0d26d` 起跑，5 GB 上限）→ **5861 passed, 136 deselected, 1 warning in 1876.31s (0:31:16)**。

**B15 — Phase 0.5 写入路径（ADR-0058）**（代码提交 `5a494d2`，ADR / 决策记录提交 `f1e71ba`）：集成 infra 通道 `0c10a65`（`plugins/knowledge/store.py`、`cli.py`、`local.py` 的 `load_items`、测试）与其 README；
`uv run pytest -q -m "not postgres" tests/plugins/knowledge tests/test_docs_consistency.py tests/test_architecture_boundaries.py tests/apps/test_api.py` → 75 passed, 1 warning；ruff / mypy 通过。

**B16 — P10-ELIG：路由资格证据模式**（`CODE_COMPLETE / DEBUG_PENDING`；集成为本次 cherry-pick）

- `research/router/evidence.py`（`EligibilityEvidence`、`report_store_resolver`）：每个可路由策略须有报告哈希、报告可取、结构合法、内容哈希一致、`subject` 为该策略、`PASS`、含且通过 G5；
  首个失败即 `RouterEligibilityRefused`（`RouterStopped`，原因 `eligibility_not_evidenced`，逐项检查入记录）；证据模式下检查结果与已核验报告入 `run_hash` / `stop_hash`；
  信任模式（默认）5 个既有哈希固定不变。研究层关闭 P10-ELIG；生产资格仍属 Control Plane。控制台对新原因 / `eligibility` 键只显示原值（待 UI 标签）。
- 子代理：`pytest tests/research/router tests/research/reports tests/research/test_cross_phase_e2e.py` → 86 passed；边界 / 文档 → 18 passed；ruff / format（636 files）/ mypy（18 files）通过。
- 本分支集成后：`pytest -m "not postgres" tests/research/router tests/research/reports tests/research/test_cross_phase_e2e.py tests/apps/test_reports.py tests/apps/test_console_fixtures.py` → 149 passed, 1 warning；ruff / mypy 通过。

**B17 — Phase 3：物理事件表 `event.events`（ADR-0056 Accepted）**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `7193b65`、`af7a351`）

- ADR-0056：9 列逻辑事件表 + 运行块（`event_index`、`event_count`、`request_hash`、`provider_hash`、`as_of`，表本身即可重建并复核整个 `EventResult`）；`month(event_time)` 分区（事件稀疏，`day` 列为被拒备选）；
  一次运行一个批次 `event.{result_hash}`，相同重写无操作、不同内容拒绝、提交竞争有界重试、空运行不写（留在 `EventResultStore`）；读取钉住快照并重建复核。
- 代码：`infrastructure/event/table_definition.py`（只读复用 catalog 帮助函数；`ensure_event_tables` 是唯一建表入口，测试之外无人调用）、`infrastructure/event/iceberg.py`；
  `docs/architecture/03-data.md` 末尾新增 §8。未改 `core/`、Schema、15 张 Phase 1 表及其哈希、`infrastructure/catalog/`、PIT 规则、供给脚本；真实 catalog 未建表。
- 实际运行：子代理 `pytest -m "not postgres" tests/infrastructure/event tests/infrastructure/catalog tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → 246 passed, 55 deselected；
  本分支集成后 `pytest -m "not postgres" tests/infrastructure/event tests/research/events tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → 95 passed；ruff / mypy（14 files）通过。
- 限制：只在 SQLite catalog 上测过（PostgreSQL catalog 与真实建表需单独执行）；表无 subject 列（多标的见 ADR-0057 核心通道）；按运行读取扫描全部月份分区，规模性能未测。

**B18 — 控制台：路由停止原因与资格证据显示**（`CODE_COMPLETE / DEBUG_PENDING`）

- `apps/web/src/lib/routerEligibility.ts`、`components/EligibilityEvidence.tsx`、`routerStop.ts`、`RouterStops.tsx`、`RouterPaperRuns.tsx`：三种停止原因中文标签 + 原码；证据模式下逐策略证据表（生命周期、报告哈希、主体、判定、G5、结果 / 拒绝原因）；
  无法解析的证据项以警告计数，不当作"无证据"；信任模式载荷不多显示任何内容。
- 本分支集成后实际运行：`npm test` → tests 43 / pass 43 / fail 0；`npm run build` → ✓ built。未在浏览器对真实后端手工验证。

### 10.5 Codex 全代码复核（`1d5427f`，`docs/reviews/2026-09-26-codex-full-code-review.md`）后的执行

本分支已快进到 Codex 复核提交 `1d5427f`，按其顺序执行：K1 → K2 文档同步 → K3（ADR-0052 按 2.1.0，旧 2.0.0 数据原样可读可重放，否则交付阻断证据）；K4（PIT 边重复）属 Phase 1，不在本分支修改。
自主决策记录中 ADR-0052 "保持 2.0.0" 一行已被 K3 取代。

**B19 — K1：知识库 `verify` 对孤立 `.review` 返回 0**（`CODE_COMPLETE / DEBUG_PENDING`）

- `plugins/knowledge/cli.py`：存在无对应条目的审阅记录（崩溃的 `add`）即拒绝，退出码 1，stderr 列出文件并提示"重跑同一 add 即补全"；恢复路径保留。
- 回归测试 `test_cli_verify_fails_on_an_orphan_review_and_the_same_add_recovers`（断言退出码、stderr、同内容 `add` 恢复后 `verify` 为 0）。
- 旧版本复现（把 `HEAD:plugins/knowledge/cli.py` 载入临时模块，同一孤立审阅目录）：`old verify exit code: 0`；修复后 `new verify exit code: 1`。
- 实际运行：`uv run pytest -q -m "not postgres" tests/plugins/knowledge` → 50 passed in 0.14s；`ruff check` → All checks passed；`mypy plugins/knowledge tests/plugins/knowledge` → no issues in 8 files。

**B20 — Phase 8：多标的验证**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 P8 通道 `7e2b4ec` 的 cherry-pick）

- `ValidatorSetup.instruments`（默认 `None` = 单标的路径，逐字节不变；4 个夹具报告哈希前后一致，2 个固定进测试）；`G0.instrument_scope`（重跑交易超出声明集合 → INCONCLUSIVE、不计算标签）；
  逐标的 manifest 绑定检查；标签事件键本就是 `<instrument>|<time>`，逐标的一个 Outcome 请求后池化（`research/validation/instruments.py`：`pool_outcomes`、`run_multi_instrument_validation`）；
  池化 G0–G3 + 各标的 G0–G3（`<gate>.instrument.<name>`，按基门 id 判定）+ 无失败才 G4，PASS 需每个标的都通过；trial 计数不变；G4 跨资产用各标的自己的重跑。
- 风险：池化的负对照按时间混合标的，完全植入的标的对上偶见失败（种子 7、17），多标的假阳性率未校准（Phase 9 待做）；多标的测试用 TEST ONLY 宽松 Profile。
- 子代理：`pytest tests/research/strategies tests/research/validation tests/research/loop/test_loop_units.py tests/test_architecture_boundaries.py` → 235 passed；synthetic_lab → 38 passed；ruff / format / mypy（45 files）通过。
- 本分支集成后：`pytest -m "not postgres" tests/research/strategies tests/research/validation tests/research/synthetic_lab tests/research/loop/test_loop_units.py tests/test_architecture_boundaries.py tests/test_docs_consistency.py`
  → 280 passed, 1 warning (187 s)；`ruff check research tests/research` → 通过；mypy（45 files）→ no issues。

**B21 — Phase 6 / 11：条件假设可选接入持续循环**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 P6 通道 `273fd0e` 的 cherry-pick）

- `LoopWiring.conditional: ConditionalPlan | None = None`（`minimum_effect`、`min_support` 均无默认）；开启时每个 trial 的矩阵算出后、读取任何单元数值前，登记全部声明状态单元 + 未知单元
  （键到该 trial 自身假设：`<parent>_given_<state>_<label|unknown_state>`），首个 trial 登记、重新评估按同一 attempt 键再计一次；全有或全无、幂等；单元计入同轮交给 G3 的族 trial 数；
  实验阶段按"单元数 × trial 数"声明预算；指纹只在有计划时带 `conditional` 键；持久核对要求每个已记录单元都在账本中。逐单元验证未做（后续）。
- 不开启时默认运行的 3 个记录哈希与指纹哈希与 `1fb7918` 一致（`test_records_without_a_conditional_plan_are_pinned` 固定）。开启需要约 4 倍 trial 预算。
- 子代理：`pytest tests/research/loop tests/research/experiments` → 130 passed (355 s)；相关套件 → 47 passed；ruff / format / mypy（25 files）通过。
- 本分支集成后：`pytest -m "not postgres" tests/research/loop tests/research/experiments tests/research/test_cross_phase_e2e.py tests/test_architecture_boundaries.py tests/test_docs_consistency.py`
  → 152 passed, 1 warning (369 s，含固定哈希——说明此前集成的 P8 / P10 未改变循环记录)；ruff / mypy（25 files）通过。

**B22 — Phase 5：横截面动量策略（研究层）**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `51b0145`）

- `research/strategies/cross_sectional_momentum.py`（`xsmom_bars@1.0.0`，唯一来源 `factor_crypto_market_size_momentum@1.0.0`）：按可见 `bar_log_return` 的 `lookback` 累计收益排序，
  多前 k / 空后 k（或只做多），权重向下取整到 18 位，参数空间 12 个点；`LibraryEntry.provider`（默认 TSMOM，既有条目不变）；不进 `strategies/` / `plugins/`（无证据不晋升）。
  `state_cross_exchange_price_deviations` 需要多交易所数据，超出 ADR-0022 范围，不实现（README 已写明）。
- 限制：TEST ONLY 宽松夹具上所有组合都被拒绝、到不了 G4；G4 跨资产检查逐标的重跑，对横截面策略单标的恒为空仓——该检查对此类策略无信息，需要 Codex / Raphael 决定处理方式（未改任何门）。
- 实际运行：子代理 `pytest -m "not postgres" tests/research/strategies tests/test_architecture_boundaries.py tests/test_strategy_contracts.py` → 116 passed；
  本分支集成后同组 + `tests/research/loop/test_loop_units.py` → 120 passed (133 s)；ruff / mypy（15 files）通过。

**B23 — core 通道：ADR-0053、ADR-0054、ADR-0052（阻断证据）、ADR-0057**（集成为 `57b95a2`、`69d8f4a`、`31943c5`、`dd2bc6a` + 本提交的冲突修正）

- ADR-0053（`57b95a2`，cherry-pick 集成会话的 `0d5a975`）：VALIDATION → FAILED 仅用于技术故障；`research/loop/trials.py` 冲突按 P6 的 `matrix` 形状合并（`SubjectRunError` 分支同时清空 `matrix`）。
- ADR-0054（`69d8f4a`，cherry-pick `d640eff`）：部分成交跨 bar 结转；Schema 135 份；不升契约版本。
- ADR-0052（`31943c5`）：按 Codex K3 以 2.1.0 实施的尝试证明**阻断**（见自主决策记录该行）：证据测试 `xfail(strict=True)`、2.0.0 金标准向量；未改契约版本与源代码；部分实现停放在本地 `wip/adr-0052-exact-fields`。
- ADR-0057（`dd2bc6a`）：可选 `EventRequest` / `Event` / `EventResult.subject`，缺省不进载荷与哈希（4 个既有哈希固定）；给出时绑定并拒绝跨标的上游；5 个事件 Provider 通过 `check_subject_is_bound`。
  合入后 `event.events`（ADR-0056）在首次建表前修订：字段 10 为可选 `subject`、运行块 11～15，定义哈希重新固定，新增绑定标的往返测试（ADR-0056 实施说明）。
- 子代理：`3501 passed, 1 xfailed`；ruff / format（641 files）/ mypy（497 files）通过。
- 本分支集成后实际运行：`pytest -m "not postgres" tests/test_*.py tests/contract_suites tests/plugins tests/research/validation tests/research/strategies tests/research/loop tests/infrastructure/event tests/infrastructure/canonical/test_contract_version_replay.py tests/apps tests/golden`
  → **3889 passed, 1 xfailed, 1 warning in 596.24s**；`ruff check .` → All checks passed；`ruff format --check .` → 662 files already formatted；`mypy`（整个配置集）→ no issues in 513 source files；Schema 135 份。

**B24 — 最终全量门禁（`564c87c`）与 Codex `942160c`～`0f590a2` 后的记录更正**

- 全量非 PostgreSQL 门禁：`uv run pytest -q -m "not postgres" -p no:cacheprovider`（HEAD `564c87c`，5 GB 上限）→ **6142 passed, 136 deselected, 1 xfailed, 1 warning in 2112.48s (0:35:12)，退出码 0**。
  xfail 是 ADR-0052 阻断证据（strict）。此前 B23 的 3889 项是目标集合，不是全量门禁。
- 更正：B22 / B23 的状态——ADR-0057（`subject`）与 ADR-0054（部分成交）的新契约字段在 2.0.0 下写成，按 Codex K3 / K5 **不接受为契约完成**，须在版本化重放后以 2.1.0 重新声明；
  `564c87c` 作为 Codex 认可的保留恢复点，不回退、不重置，也不合入 `phase/1` / `main`；另推送了隔离备份分支 `hold/adr-0054-0057-at-2.0.0`（= `564c87c`）。
- `event.events` 部署状态只读核实（生产 catalog 15 张表无 `event*`、测试 catalog 0 张、warehouse 无事件目录），写入 ADR-0056；`table_definition.py` 说明更正为十列、ID 1～10 / 11～15 / 16～17；
  ADR-0056 合规清单与 ADR-0057 状态、`subject` 语义（稳定、大小写敏感 opaque ID）同步；ADR 索引同步。
- ADR-0052：独立 Phase 1 分支 `phase1/adr-0052-versioned-replay`（基于候选 `b4d63c4`）M0 `ed1a202` 已推送；Codex 条件认可 ContextVar 版本作用域（仅包住已持久化对象重建 / 校验、`try/finally`、
  只取缺省版本、泄漏 / 未发布版本 / 组内不一致 fail closed、Canonical 列与 `RevisionRecord.schema_version` 同源），M1～M3 进行中。

**B25 — ADR-0059（Proposed）：G4 跨资产检查对横截面策略的适用方式**

- 发现：`cross_asset_check` 逐标的单独重跑；横截面策略在单标的宇宙恒为空仓，给出阈值时 G4 跨资产对任何横截面策略结构性 FAIL（证据：`research/validation/robustness.py:987` 起、
  `tests/research/strategies/test_cross_sectional_momentum.py` 固定现状）。属于验证规则变化（H2 / H3），按仓库规定只写 Proposed ADR、推荐 C（零敞口单标的 → INCONCLUSIVE）再 A（子宇宙检验），批准前不改代码。
- 并行进行（不涉及 `core/`）：P9 多标的校准臂、P6 逐单元条件验证两个子代理；ADR-0052 版本化重放（独立 Phase 1 分支）子代理独占 `core/` 与 Phase 1 基础设施。


**B26 — Phase 9：多标的校准模式**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 P9 通道 `f629b97` 的 cherry-pick）

- `MultiInstrumentCalibrationSetup` / `run_multi_instrument_calibration`（`GateCalibrationSetup` 不变，关闭时报告哈希逐位不变）；每次运行 k ≥ 2 个合成标的（`instrument_seed` 规则入报告）；
  调用方声明各臂（`all_noise` / `all_planted` / `mixed`，无默认）；`MultiInstrumentValidatorDetector` 走完整多标的验证路径；报告逐臂 / 逐标的 / 逐门（含池化 G1 负对照与逐标的子门）比率。
- 冒烟规模证据（非校准结果；k = 2、每臂 8 种子、TEST ONLY 宽松 Profile，报告 `d7b5df2d…`）：池化 `G1.shuffle_control` FAIL——噪声 1/8、植入 0/8、混合 0/8；`G1.shift_control` FAIL——噪声 0/8、植入 1/8、混合 2/8；
  区间极宽，不支持任何阈值或 Profile 选择。
- 限制：不跑 G5；全噪声臂中池化阶段先失败时逐标的子门为 `not_evaluated`；按门 id `G0.single_instrument_adapter` 识别走错路径。
- 子代理：`pytest tests/research/synthetic_lab tests/research/strategies/test_multi_instrument_validation.py` → 64 passed；边界 / 文档 → 18 passed；ruff / format / mypy（10 files）通过。
- 本分支集成后：`pytest -m "not postgres" tests/research/synthetic_lab tests/research/strategies/test_multi_instrument_validation.py tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → 82 passed, 1 warning (124 s)；ruff / mypy 通过。

**B27 — Phase 6：条件假设逐单元验证（可选）**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 P6 通道 `0fa4e55` 的 cherry-pick）

- `ConditionalPlan.validate_cells: bool`（必填无默认；`False` 时载荷 / 指纹 / 记录逐位不变）；`True` 时在 `ValidationStage` 中对达到 `min_support` 的单元用同一 `family_trial_count` 跑 G0–G3
  （单元证据 = 状态阶段因果归属到该单元的已交易决策，不重算状态），不足支持 / 无阈值的单元记 `unsupported`、从不 PASS；G4 / G5 记 `not_run`；单元结果写入报告行 `conditional_cells` 并入审计 / 检查点；
  单元判定不改变任何生命周期、不写 FailureRecord。限制：适配器门复制自试验报告（`PipelineBacktestValidator` 无公开子集入口）；数据集组合根未做端到端测试。
- 子代理：`pytest tests/research/loop tests/research/experiments` → 160 passed (586 s)；最终代码复跑 → 27 passed；ruff / format / mypy（27 files）通过。
- 本分支集成后：`pytest -m "not postgres" tests/research/loop tests/research/experiments tests/test_architecture_boundaries.py` → **171 passed, 1 warning (561.80 s)**；ruff / mypy 通过。

**B28 — ADR-0005 Promotion 链：Strategy Registry、Promotion 服务、Equivalence Gate**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 promotion 通道 `ad09332` 的 cherry-pick）

- 分层：`infrastructure/registry/`（只追加哈希链 Registry，只依赖 `core`）、`research/promotion/`（由验证证据构建 `StrategyArtifact`）、`apps/promotion/`（Equivalence Gate 与 `DeploymentRecord`，不 import `research/`）。
- 失败关闭：缺任一证据（报告 PASS 覆盖 G0–G4 且至少一份含 G5、实验与 spec / 宪法 / Profile / 研究提交一致、依赖哈希、经人工批准的 OOS → PAPER、确定性金标准）即类型化拒绝、不写入；
  门精确比较（契约无容差）；今天所有库策略都被拒（`no_validation_report`）；没有任何策略被晋升，`strategies/` / `risk/` 仍无代码（边界测试固定）。ADR-0005 已追加实施说明（开放选择逐条记录）。
- 实际运行：子代理与本分支集成后 `pytest -m "not postgres" tests/promotion tests/test_architecture_boundaries.py tests/test_lifecycle.py tests/test_lifecycle_evidence.py tests/apps/test_execution.py tests/test_docs_consistency.py`
  → **202 passed**；`ruff check .` → All checks passed；mypy（14 files）→ no issues。
- 限制：Registry 迁 PostgreSQL 待 D-01 / D-02；不核对 Git commit / tree 是否存在；重开时不复核 blob 内容（注册与读取时核对）；仅 POSIX flock。

**B29 — ADR-0059 Accepted 并实施：G4 跨资产检查对横截面策略**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 ADR-0059 通道 `1a8f96a` 的 cherry-pick）

- 决策（Claude 依 Raphael 2026-09-26 授权，通过 ADR 自主决定）：C + A 都实施。C：单标的重跑**全部零敞口**（按仓位 / 成交判定，不从收益推断；敞口未知不视为零）→ `G4.cross_asset.positive_fraction`
  INCONCLUSIVE `not_applicable_zero_exposure_single_asset`（只会把结构性不适用的 FAIL 变为 INCONCLUSIVE，不多放行）。A：`research/strategies/cross_section.py` 显式声明横截面策略（目前只有
  `xsmom_bars`，按 spec 名判定，从不按结果推断）；按排序后相邻成对切分子宇宙（奇数并入末组，规则名入报告），≥ 4 个标的才有 ≥ 2 个子宇宙，否则 INCONCLUSIVE
  `not_enough_instruments_for_subuniverses`；逐子宇宙重跑，按同一 `cross_asset.min_positive_fraction` 来源判定；trial 计数不变。
- 单标的与时间序列策略的报告 / 视图 / G4 哈希逐位不变（固定于 `4543036`）；TEST ONLY 宽松阈值下：4 个植入标的 → PASS，4 个噪声标的 → FAIL，2 个标的 → INCONCLUSIVE。
- 子代理：`pytest -m "not postgres" tests/research/validation tests/research/strategies tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → 293 passed；ruff / format / mypy（50 files）通过。
- 本分支集成后：`pytest -m "not postgres" tests/research/validation tests/research/strategies tests/research/synthetic_lab tests/research/loop/test_loop_units.py tests/test_docs_consistency.py tests/test_architecture_boundaries.py`
  → **350 passed, 1 warning (374.81 s)**；`ruff check .` → 通过；mypy（50 files）→ no issues。
- 限制：仅按名称分组（不考虑流动性 / 板块）；4 个标的时比例只能取 0 / 0.5 / 1；阈值未校准；声明是研究层名称集合（无契约字段）。

**B30 — 全代码缺口审计与新一轮派发（2026-09-26 晚）**

- 只读审计（HEAD `7703fba`）逐条核对 §4 验收列：大部分为已实现；缺口与处理——P7 被拒 LLM 调用记录 / 严格解析、批量生成 + 可执行 DSL、KnowledgeProvider 接线（L1 通道）；
  P11 跨进程测试、P14 金标准实验重放（L2 通道，仅测试）；全栈 API ↔ 契约一致性、P10 纸面偏差报告、P11 劣化报告（L3 通道）；P13 风险 / 告警重放、P10 路由自身验证适配器（L4 通道）；
  Web 组件测试（排队，用已安装的 esbuild，不加依赖）。
- 需要决策的缺口已由 Claude 通过 ADR 决定：ADR-0060（C-T4 市场基准语义，Accepted，待实施）、ADR-0061（交互 DSL，Accepted，待实施）、API → Worker 维持只读（记录于自主决策记录）。
- 仍阻塞：ADR-0055（知识库 tag / 资产检索，需改契约，集成会话的 Proposed ADR）；真实数据 bar 成交量（Phase 1 `infrastructure/bars`）与 D-PFIELDS（待 ADR-0052 2.1.0）；
  D-09 Profile 数值、D-10 NATS、D-08 实盘、P5-PLUGIN（无证据不晋升）。


**B31 — L4 通道：P13 风险 / 告警重放、P10 路由自身验证**（`CODE_COMPLETE / DEBUG_PENDING`；集成为 `80ac8b2`、`edb0c3d` 的 cherry-pick）

- P13：apps 本地 `MarkRecord`（opt-in `ExecutionService(record_marks=...)`，默认不写，既有审计头不变——固定测试）；`apps/execution/risk_replay.py` 的 `replay_risk` 按审计顺序以新的
  `SecondLineRisk` / `Monitor` 重放，逐字段复现每条拒绝、每笔成交对应已接受订单、每条告警；任何分歧 → `RiskReplayDiverged`（指出首个分歧记录）；有订单无标记的审计被拒。仍只模拟。
- P10：`research/router/validation.py`——`router_strategy_spec`（路由器以 `StrategySpec` 呈现，搜索空间为空 = 每个路由规格一个 trial）、`RouterTrialRunner`（普通调用即 `paper_run`，验证净纸面结果）、
  `validate_router`（核对规格与运行、`run.verify()`、验证并返回绑定哈希）。限制：引用 `paper.py` 两个私有帮助函数；宽松夹具上 G3 FAIL，G4 计数经 `robustness_input` 核对；尚无组件开启 `record_marks`。
- 子代理：109 passed；本分支集成后 `pytest -m "not postgres" tests/apps/test_execution*.py tests/research/router tests/test_architecture_boundaries.py tests/test_docs_consistency.py` → **116 passed**；
  `ruff check .` → 通过；mypy（38 files）→ no issues。

**B32 — L2 通道（跨进程测试 + 金标准实验重放）集成，并修复其发现的真实缺陷：循环状态目录无单写者锁**（`CODE_COMPLETE / DEBUG_PENDING`）

- L2（`f0741b7` 的 cherry-pick，仅测试）：研究循环 / worker 任务 / 文件总线的真实跨进程测试（子进程写入与重开、固定点 SIGKILL、第二进程被锁、篡改与伪造审批被另一进程拒绝、预算跨进程保持耗尽）；
  Phase 14 第一个合成 TEST ONLY 金标准实验 `tests/golden/experiments/`（TSMOM → 回测 → G0–G4，容差 0 重跑一致、扰动报告差异、回滚证据、另一进程重新生成逐字节相同）。
- 缺陷（L2 以 strict xfail 固定证据）：`state_dir` 没有自己的单写者锁，只靠自有总线的 flock；调用方注入总线（如 `InMemoryEventBus`）时，第二个进程可打开被占用的目录，两者都能追加日志，只在下次重开时才发现。
- 修复（Claude）：`research/loop/durable.py` 新增 `StateLock`（`state_dir/state.lock` 上的排他非阻塞 `fcntl.flock`，读任何文件前获取；被占用 → `LoopStateLocked`；随审计日志对象回收、`DurableLoop.close()` 或进程退出释放，崩溃不留残锁），
  `research/loop/compose.py` 的 `DurableLoop.state_lock` 在 `close()` 时释放。xfail 转为通过；原先在同一进程中未释放第一个写者就重开同一目录的 5 个测试改为先 `close()`（它们断言的重开语义不变）；跨进程测试中第二进程的拒绝现由 `LoopStateLocked`（先于总线锁）给出。
- 实际运行：L2 子代理 `79 passed, 1 xfailed`；修复后 `pytest -m "not postgres" tests/research/loop/test_loop_durable.py tests/research/loop/test_loop_cross_process.py tests/research/loop/test_llm_content.py tests/apps/test_research_loop_durable.py`
  → **112 passed**；`ruff check` → 通过；`mypy research/loop` → no issues in 11 files。

**B33～B37 — L1 / L3 / L5 / L6 / L8 通道集成**（全部 `CODE_COMPLETE / DEBUG_PENDING`；cherry-pick 集成）

- B33 Phase 7（L1：`264a3bc`、`cf5e4e4`、`877e2d8`）：严格草稿结构（多余键拒绝、不强制类型），被拒 LLM 调用的 `LlmCall`（哈希与引用）记入假设阶段摘要；`research/hypotheses/batch.py` 声明式批次
  （算子 × 策略 × 参数点，经 `ReviewedOperators` 版本化白名单，构建时拒绝 `trial_point` 不能执行的条件，整批首轮预登记并按单元计预算）；`KnowledgeSource`（调用 `KnowledgeProvider.search`，
  记录查询哈希与结果哈希为假设来源）。均为可选，未设置时记录与指纹逐位不变。子代理 `tests/research/hypotheses tests/research/loop` → 202 passed。
- B34 全栈（L3：`40bf02c`、`347c9b8`、`81e6802`）：API 对 `validation_report` 按核心契约校验、文件 id = 载荷身份哈希（各种类）、错误体不含服务器路径、422 声明 `ApiError`、`/health` 等补响应模型；
  P10 纸面偏差 `research/router/deviation.py` + 报告种类 `paper_deviation` + 页面；P11 劣化检查报告 `degradation_check` + 页面（写入前重跑监控复核）。子代理 → 380 passed；npm 55 / 55。
- B35 C-T4（L5：`4139e22`）：ADR-0060 实施——规则注册表（`none` / `buy_and_hold_equal_weight` / `flat`，未知 → INCONCLUSIVE），报告项 `G2.market_benchmark.<rule>`、`G2.inverse_control`（不影响判定、不增加 trial）；
  当前为显式 opt-in `ValidatorSetup.market_benchmark`，循环 / 夹具的强制启用由 L7 通道进行。子代理 → 369 passed；既有固定哈希不变。
- B36 Phase 3（L6：`e3cb510`）：ADR-0061 交互 DSL（`plugins/events/dsl.py`、`windows.py`：`seq` / `and` 复用既有算子，新增 `event_window_end` / `event_absence` / `event_count`；编译结果可重算核对；
  逐跳 `require_full=True` 端到端；未来扰动不改变过去各层事件；`not` 在窗口结束前不存在）。D-L6-1 见 ADR-0061 补记。子代理 → 544 passed。
- B37 Web 组件测试（L8：`72ecc0d` 的 cherry-pick）：用已安装的 esbuild 打包 `*.test.tsx` 后 `node --test`，`renderToStaticMarkup` 渲染 States / ReportBrowser / 全部页面的首屏、加载后、空、错误状态；
  测试种子与初始选择经 `ApiSeedContext` / `InitialSelectionContext` 注入（默认行为不变）；`package-lock.json` 未变；变异验证（破坏三处代码均被发现）。
- 集成后实际运行（本分支）：`ruff check .` → All checks passed；`ruff format --check .` → 722 files already formatted；`mypy` → no issues in 567 source files；`npm test` → lib 55 / 55、组件 96 / 96；`npm run build` → ✓；
  组合 pytest（`tests/research tests/apps tests/plugins tests/infrastructure/{event,migration,event_bus,content} tests/promotion` + 边界 / 文档 / 事件契约）→ **1830 passed, 4 failed**（1168 s）：
  4 个失败都是 P6 新测试在同一进程中未释放前一测试的循环就重开模块级状态目录（B32 的单写者锁正确拒绝）；加 `tests/research/loop/conftest.py`（每个测试后 `gc.collect()` 释放已丢弃的写者，不改断言）后
  这两个文件 → **26 passed**。全量门禁将在全部通道集成后于最终 HEAD 运行。


**B38 — ADR-0052 版本化重放与契约 2.1.0（独立 Phase 1 分支 `phase1/adr-0052-versioned-replay`，已推送至 `8a7655e`）**（`CODE_COMPLETE / DEBUG_PENDING`；未合入 Phase 1 候选 / `main`）

- 基于 K4 候选 `45c13d7`（`7786e09` 合并）。M0 `ed1a202`：设计 V1～V7 + 2.0.0 金标准向量 + strict-xfail 证据；实测只改常量时首切片重放 24 步中 23 步失败（范围大于原先列出的 7 处）。
- M1 `e247d3a` / `dc8f073`：各行构造器显式接收写入组版本，行版本列与所建记录同源；normalizer 从已提交行读回单元版本（新单元用当前版本，部分提交单元按已记录版本补完，单元内混版 / 未发布版本 fail closed）；
  `core/domain/base.py` 的重建作用域只用于重建已持久化对象、只作用于缺省版本、作用域内开写入组即拒绝；Phase 1 已发布的政策 / 来源绑定与 `FIRST_SLICE_UNIVERSE` 固定在 2.0.0（哈希固定测试）；
  `selector.py` 只改两行把 `PIT_BINDING` 固定在 2.0.0（K4 去重逻辑未动）。结果：常量为 2.1.0 时重放 2.0.0 首切片 0 / 24 步失败、表头不动。
- M2 `e9f5c88`：`CONTRACT_SCHEMA_VERSION = 2.1.0`；原 strict-xfail 转为通过；同表 2.0.0 / 2.1.0 单元共存、2.0.0 部分单元按 2.0.0 补完、单元内混版 fail closed；整个首切片以 2.0.0 写入、2.1.0 重跑后表头与行不变、
  manifest 原哈希加载与重建重放、PIT 读取结果相同；离线 D-NET 步骤重放不变（仅计时字段不同）；Schema 134 份，`schemas/v1`、v1 向量与 2.0.0 金标准向量逐字节不变。
- M3 `8a7655e`：ADR-0052 核心字段（精确小数、`GateResult` 精确值 / 阈值、Profile `*_exact` 与 §2 / §3 新字段，只有结构范围无数值）；浮点与精确值不一致拒绝、精确值替代浮点入哈希；2.0.0 信封携带 2.1.0 字段即拒绝（`_FIELDS_SINCE`）。
- 子代理实际运行（最终）：`tests/infrastructure`（非 PostgreSQL，含 D-NET / tools）→ **2041 passed, 136 deselected (1557.69 s)**；`tests/test_*.py tests/contract_suites tests/research/validation tests/research/strategies/test_backtest_validation.py` → **3061 passed**；
  ruff / format（613 files）/ mypy（474 files）通过。集成会话正在该提交上跑真实 PostgreSQL 严格全量门禁（仅作证据，按 K3 未经 Codex 复核不并入候选）。
- 后续（core 合并通道进行中）：把该分支合并进全代码分支；以 2.1.0 重新声明 ADR-0054 / 0057 字段；固定全代码分支上已发布于 2.0.0 的研究 / 插件身份（如 `BarRealizedVolatilityProvider.spec()`）；研究侧 ADR-0052 取值（精确比较、C-A4、负对照独立阈值、G4 / 封存 OOS 字段）；最终全量门禁。

**B39 — ADR-0060 在循环与合成校准中强制启用（L7：`90a0be0` 的 cherry-pick，本分支 `2d852b3`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 循环 `ValidatorSetup(market_benchmark=True)`；`StrategyValidatorDetector` / `MultiInstrumentValidatorDetector` 对未启用的设置拒绝（`DetectorConfigurationError`），保证校准与循环同一流水线；
  `ValidatorSetup.market_benchmark` 默认仍为 `False`（默认启用会使路由自身验证全部 INCONCLUSIVE——路由回测无法用普通 `BarBacktester` 重跑复现）。
- TEST ONLY 夹具 Profile（循环、校准宽 / 严、`test_backtest_validation`、三个 PostgreSQL e2e）改为 `buy_and_hold_equal_weight` + `inverse_control_reported=True`；`tests/research/validation/fixtures.py` 与玩具检测器 Profile 有意保留 `"test-only"`（前者被未登记规则单测使用，后者保持已提交的控制台夹具逐字节不变）。
- 有意重新固定的哈希（每处带注释）：循环 3 个记录哈希与配置指纹；`PRE_G5_PIPELINE_ONE_SEED_HASH`；横截面 G4、多标的验证、市场基准测试的报告 / 视图哈希（仅因绑定新 Profile）；金标准实验重新生成
  `be2430e6…` → `c8d129e6…`（只多 5 个报告项 G2 门，32 → 37，其余输出与判定不变）。
- 实际运行：本分支集成后 `pytest -m "not postgres" tests/research tests/infrastructure/migration tests/golden tests/test_docs_consistency.py` → **858 passed, 1 warning (1376.94 s)**（L7 子代理报告的 4 个 `LoopStateLocked` 失败已由 B37 的 `conftest.py` 修复）；
  `ruff check .` → 通过；`mypy research tests/research` → no issues in 168 files。PostgreSQL e2e 的 Profile 变化未经验证（其中 `test_research_loop_real_data_g5` 断言 PASS）。
- 遗留：`research/strategies/validation.py` / `pipeline.py` 文档字符串仍称循环与校准未启用；`07-validation.md` §G2 门清单待同步（均已由 B42 `dc89d1e` 处理）。

**B40 — Phase 1 分支 2.1.0 门禁回归修复（`phase1/adr-0052-versioned-replay` 快进至 `22392ea`）**

- 证据：集成会话在 `8a7655e` 上以真实 PostgreSQL 测试 catalog 跑严格全量门禁 → **7 failed, 5845 passed, 1 warning (3468 s)**，静态检查通过；失败集中在 ADR-0052 通道未跑的 `tests/apps`、`tests/plugins`。
- 修复：`apps/api/openapi.json` / `apps/web/src/api.d.ts` 重新生成（各一行：信封 `schema_version` 默认值 2.0.0 → 2.1.0，无端点变化）；`test_the_default_backtester_is_byte_identical_to_v1`（6 例）**不覆盖**旧常量——
  在 `contract_schema_version_scope("2.0.0")` 内重建回测，断言记录的 v1 哈希（`fada3325…` / `766b48ff…` / `ab8bd307…`）不变，并断言 2.1.0 运行除版本字段及其上的哈希外内容相同，另固定 2.1.0 哈希（注明 ADR-0052 M2）。
- 修复通道实际运行：`pytest -m "not postgres" tests/apps tests/plugins tests/test_*.py tests/contract_suites` → 3310 passed；ruff / format（613 files）/ mypy（474 files）通过；`npm run build` ✓（该分支无 `npm test` 脚本）。
  已请集成会话在 `22392ea` 上重跑严格 PostgreSQL 门禁（仅作证据）。

**B41 — 契约 2.1.0 合入全代码分支（core 合并通道 `core/adr-0052-into-full-code` → 本分支合并提交 `589a53a`）**（`CODE_COMPLETE / DEBUG_PENDING`；未经 Codex 复核）

- `6c4149e` 合并 ADR-0052 分支（`8a7655e`；M0 重复材料逐字节相同只留一份，原 strict-xfail 通过，Schema 135 份）；`eae65a4` ADR-0054（`PriceBar.volume`、`BacktestResult.remainders`、`FillRemainder`、新执行模型值）
  与 ADR-0057（`subject`，大小写敏感 opaque ID）**以 2.1.0 重新声明**：2.0.0 信封携带这些字段 / 值即拒绝（`_FIELDS_SINCE`、新增 `_VALUES_SINCE`），旧 2.0.0 载荷照常读取且哈希不变（`tests/test_adr_0054_0057_versions.py`）；
  `9f23a88` 修复真实缺陷：`event.events` 按在用版本重建已存运行，升版后 2.0.0 运行无法重建——现每次运行记录其 `contract_schema_version`（运行块字段 16），混合信封的运行写入前拒绝；
  `7878310` 研究侧取值：门比较按 `hlens.validation.gate-value-quantization@1.0.0`（12 位小数、银行家舍入；表示规则非阈值）精确比较；Profile 字段存在时同时给显式 `param:` 即拒绝（C-A4），覆盖 G1 负对照、CSCV 分块、
  容量（含冲击模型）、跨资产、欠采样收益占比、封存 OOS 预算；旧 Profile 逐位不变（22 项测试）；`35d3092` 合入 L7、`4367419` 合入 Phase 1 修复 `22392ea`；`182d89e` 因 2.1.0 信封变化的测试固定值：
  以"在 2.0.0 作用域内重建仍得旧值 + 旁置 2.1.0 新值"或"改为 2.1.0 并在注释保留旧值"两种方式处理（完整清单见 ADR-0052 实施说明与该提交；每个旧值都经 2.0.0 重建复现，证明变化只来自信封）；
  文档：`07-validation.md` §5、`02-domain.md` §3.3、ADR-0052 / 0056 实施说明。
- 合并通道实际运行（`cc030ec`，本分支合并后代码树与之相同，仅多计划文档）：`uv run pytest -q -m "not postgres" -p no:cacheprovider` → **6749 passed, 136 deselected, 1 warning in 2952.64s (0:49:12)**；
  `ruff check .` → All checks passed；`ruff format --check .` → 736 files already formatted；`mypy` → no issues in 580 source files；Schema 135 份且重新导出无差异；`npm test` → lib 55 / 55、组件 96 / 96；`npm run build` ✓；`gen:api` 重跑无差异。
- 未覆盖：PostgreSQL 标记测试（本分支从不跑真实数据库；独立 Phase 1 分支由集成会话以真实 PostgreSQL 测试 catalog 取证）；ADR-0053 是状态机规则而非契约字段，未做版本门控；五个旧控制台夹具有意保留 2.0.0 以证明旧报告可读。


**B42 — ADR-0052 Phase 1 分支的真实 PostgreSQL 严格门禁（集成会话取证）**

- `22392ea`（钉住 worktree、真实 PostgreSQL 测试 catalog）：**GATE OK** —— ruff All checks passed；613 files already formatted；mypy no issues in 474 source files；lock ok；
  **pytest 5852 passed, 1 warning in 3216.90s (0:53:36)**。（此前 `8a7655e` 为 7 failed / 5845 passed，B40 修复。）仅作证据：按 Codex K3，集成会话在 Codex 复核前不并入 Phase 1 候选或 `wip/phase-1-unreviewed`。
- 另：`dc89d1e` 更正验证器文档字符串与 `07-validation.md` G2 行（ADR-0060 已在循环 / 合成校准强制启用）。

**B43 — ARCHITECTURE_DECISION_REQUIRED：D-LIST / ADR-0051（撤回 Claude 的接受）**

- 冲突：Claude 依 Raphael 的一般授权（"所有决策由你决定，含红线"）一度接受 ADR-0051 方案 A 并开独立 Phase 1 分支 `phase1/adr-0051-listing-assumption` 实施；集成会话指出 Raphael 2026-09-26 曾对 D-LIST **明确暂缓**
  （"后面再授权 我不知道这个是什么"，见 PROJECT_STATUS §6 与 ADR-0051 状态行）。具体暂缓优先于一般授权；按 CLAUDE.md §0 文档冲突即停止并提出决策包。已令该通道停止：该通道停止时尚未写任何代码、未提交、未推送，ADR-0051 状态从未被改动（仍为 Proposed / Raphael 暂缓）；其阅读后的设计要点（策略名 `hlens.listing.observed-state-backfill-assumption@1.0.0`、绑定位置 `PointInTimeSpec.availability_bindings`、`UniverseMember` 需一个 2.1.0 可选字段——与 ADR-0051"不改契约"一句冲突、需实施说明）留待 Raphael 批准后使用。
- 决策包：**问题**——是否授权 ADR-0051 方案 A（一次公开 `exchangeInfo` 调用 + 显式绑定、写入清单的"上市历史假设"，仿 ADR-0032）？**为什么重要**——没有上市历史，真实历史数据建不成研究数据集（D-NET 停在 PIT 选择）；
  **选项**——A：授权（草稿实现可在批准后完成、跑严格门禁再并入）；B：只从今天起前向采集，不用历史上市假设；**推荐**：A（默认保守：不绑定即维持现行拒绝）；**不决定时**：保持现状，真实数据只到 PIT 选择。

### 10.6 最终汇总（逐 Phase；`CODE_COMPLETE / DEBUG_PENDING` ≠ 验收）

最终分支 `claude/2026-09-26-code-completion-337e38` → 远端 `wip/all-code-completion`；独立 Phase 1 分支 `phase1/adr-0052-versioned-replay`（`22392ea`，未并入 Phase 1 候选 / `main`）。
没有合并 `phase/1` 或 `main`，没有打 tag。最终门禁见本节末尾。

| Phase | 已实现且有定向验证（批次 / 主要提交） | 未完成或被阻塞（原因） |
|---|---|---|
| 0.5 | 经审阅写入路径 ADR-0058（B15 `5a494d2`）；`verify` 孤立审阅失败（B19 `e254a26`） | tag / 资产检索：ADR-0055（集成会话的 Proposed ADR，需改契约）；审阅人无认证 |
| 1 | 不属本分支交付；ADR-0052 版本化重放与 2.1.0 在独立 Phase 1 分支（B38 / B40，真实 PostgreSQL 严格门禁 5852 passed @`22392ea`，B42） | D-LIST / ADR-0051：Raphael 明确暂缓（B43，`ARCHITECTURE_DECISION_REQUIRED`）；Phase 1 验收由 Codex / Raphael |
| 2 | 诊断载荷 / 哈希（B6）、`state_diagnostics` 报告与页面（B12） | — |
| 3 | 事件运行存储、统计序列化（B5）；物理表 `event.events` ADR-0056（B17，只读核实从未建表）；`subject` ADR-0057 以 2.1.0 声明（B41）；交互 DSL ADR-0061（B36） | 真实 catalog 未建表（只在 SQLite 测试 catalog 验证） |
| 4 | Outcome 表持久化、多种子负对照（B10）；ADR-0052 契约 2.1.0 与研究侧精确比较 / C-A4 / 负对照独立阈值（B38 / B41）；C-T4 市场基准 ADR-0060（B35 / B39） | Profile 数值 D-09 未冻结（不猜测）；浮点表示保留为弃用双字段 |
| 5 | 横截面动量 `xsmom_bars`（B22）；ADR-0054 部分成交以 2.1.0 声明（B41）；ADR-0005 Promotion 链（B28） | 无策略晋升（没有验证证据；Promotion 链今天拒绝所有库策略）；真实数据 bar 成交量属 Phase 1 |
| 6 | 全单元预登记（B6）、可选接入循环（B21）、逐单元验证（B27） | 单元 PASS 不改变生命周期（缺独立 G4 / G5 路径，设计如此） |
| 7 | LLM 内容存储与可取回核对（B11）；严格草稿、被拒调用记录、声明式批次、知识检索来源（B33） | 只有脚本化 LLM（真实 LLM 需网络与密钥，超出范围） |
| 8 | G4 逐检查隔离（B10）、多标的验证（B20）、ADR-0059 横截面跨资产（B29） | 多标的池化负对照假阳性率只有冒烟证据 |
| 9 | 检测器异常 INCONCLUSIVE（B1）、可选 G5（B13）、多标的校准模式（B26） | 只到冒烟规模；不产生阈值 |
| 10 | 明确停止 / 哈希复核 / 报告绑定（B6 / B7）、资格证据模式（B16）与显示（B18）、纸面偏差（B34）、路由自身验证（B31） | 生产资格属 Control Plane |
| 11 | 总线外部锚点（B4 / B8）、任务只读 API（B9）、跨进程测试（B32）与状态目录单写者锁（B32，真实缺陷修复）、劣化检查报告（B34）、ADR-0053（B23） | NATS / Control Plane 持久化（D-10）未做 |
| 12 | 替换提案（B3） | 提案未接入循环 |
| 13 | 持久审计 / 非空审计重开即急停 / 只读重放（B2）、风险 / 告警重放（B31）；仍只模拟 | 实盘、凭据、下单：决定不做（H10） |
| 14 | 金标准持久化 / 差异报告 / 回滚证据（B11）、金标准实验重放（B32） | 无具体迁移目标（不引入新基础设施） |
| 全栈 | 只读 API（任务、错误码、报告 invalid、契约 / 身份核对，B9 / B34）；14 个页面三态、Jobs、新报告种类（B9 / B12 / B34）；`node --test` 55 + 组件 96（B37） | 浏览器对真实后端的手工验收未做；API → Worker 维持只读（ADR-0048，记录于自主决策记录） |

仍待人工 / Codex：全部 CODE_COMPLETE 项的独立调试与对抗复测（Cursor）；ADR-0052 / 0054 / 0056 / 0057 / 0058 / 0059 / 0060 / 0061 与 Promotion 链的 Codex 复核；D-LIST（Raphael）；ADR-0055（集成会话 / Raphael）；D-09 Profile 数值；Phase 1 验收；合并 `main` / tag（Raphael）。

**最终门禁（HEAD `8983ead`，本节文档提交之前的最终代码状态）**：`uv run pytest -q -m "not postgres" -p no:cacheprovider`（5 GB 上限）→ **6749 passed, 136 deselected, 1 warning in 2792.59s (0:46:32)，退出码 0**；
`uv run ruff check .` → All checks passed；`uv run ruff format --check .` → 736 files already formatted；`uv run mypy` → Success: no issues found in 580 source files；Schema 135 份；`uv lock --check --offline` → OK；
`npm test` → lib 55 / 55、组件 96 / 96；`npm run build` → ✓。136 个 deselected 是 PostgreSQL 标记测试：本分支不接触真实数据库；ADR-0052 的独立 Phase 1 分支已由集成会话以真实 PostgreSQL 测试 catalog 取证（`22392ea`：5852 passed）。


### 10.7 审计后续（2026-09-26 晚，新目标"继续自主开发直到所有已授权可推进工作完成"）

- 开工核实：远端 `wip/all-code-completion` = 本地 `c36005b`，工作区干净；`c36005b` 是 `8983ead` 的后代，差异只有计划 / STATUS / MEMORY 三个文档文件，故 `8983ead` 的全量门禁（6749 passed）覆盖 `c36005b` 的全部代码。
  两个 Cursor worktree（`codex-research-loop-ui`、`codex-d3e-pit-tests`）自 09:50 起空闲且干净；前者的研究循环图表按维度分轴的改进由 apps 修复通道移植（不动其 worktree）。
- 两个只读审计：B（全栈 + 文档一致性，32 项）与 A（研究 / 基础设施 B1～B43 主张 vs 证据，15 项）。文档类发现已由 `820d771`、`4596c38` 修正；代码类发现分派给以下通道。

**B44 — 真实后端冒烟（`d368d16` 的 cherry-pick）**（`CODE_COMPLETE / DEBUG_PENDING`；仅测试 / 工具）

- `tests/apps/live_server.py`：标准库 asyncio 的测试用 HTTP/1.1 服务器（uvicorn 不是项目依赖，未新增依赖；非生产服务器），在**独立子进程**里运行真实 `create_app(...)`，只绑定 127.0.0.1、临时端口。
- `tests/apps/test_live_backend_smoke.py`：以真实 writer 产出的全部 10 种报告（+1 个坏文件）、真实 `JobRunner` 结果日志、知识目录，起两个服务器（第二个无知识 Provider / 无任务日志），经真实 HTTP 覆盖 openapi 的每个操作：
  列表 / 详情、invalid、400 / 404 / 422、jobs、知识 200 / 422 / 503；活的 `/openapi.json` 必须等于已提交文件；每个响应体按声明的 schema 与 pydantic 模型逐字段往返校验。
  `apps/web/scripts/live-smoke.mjs`（`npm run smoke:live`）：用已安装 esbuild 打包控制台自身的 `api.ts`、`src/lib` 与 14 个页面，对两个活服务器逐页服务端渲染，要求无加载残留 / 无错误态 / 无原始 JSON 回退。
- 不证明：真实浏览器（像素、布局、ECharts 绘制、交互、vite 代理）——手工浏览器验收仍未做（本机无浏览器）；502 与 500（篡改日志）仍只在进程内 TestClient 覆盖。
- 实际运行：子代理 `pytest -m "not postgres" tests/apps` → 278 passed；`npm test` → 55 / 55 + 96 / 96；build ✓；本分支集成后 `pytest tests/apps/test_live_backend_smoke.py tests/test_architecture_boundaries.py tests/test_docs_consistency.py` → 20 passed；ruff / mypy（19 files）通过。

**B45 — Phase 9：校准不再吞掉配置错误（审计 A 发现 3；`20b6301` 的 cherry-pick）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 缺陷：`calibrate()` / 单标的 / 多标的 / G5 的 `except Exception` 也吞掉 `ProfileFieldMissing`、`UnsupportedMethod` 等配置错误，缺字段的候选 Profile 会得到全 INCONCLUSIVE、噪声假阳性率 0/n（偏乐观的证据），与 B1 "配置错误仍抛出"的承诺不符。
- 修复：沿用流水线自身分类——`PROPAGATED_ERRORS = (ValueError, TypeError, MemoryError)`（与 `research/validation/g4.py` 一致，漂移测试固定），原样重新抛出（附臂 / 种子 / 候选说明）；真正的运行时失败仍记 INCONCLUSIVE + `detector_error`；
  有检测器错误的臂新增 `pass_rate_bounds: [passed/n, (passed+errors)/n]`（向外取整到 6 位；无错误时不出现，既有报告哈希不变）。唯一重新固定的哈希 `PRE_G5_RAISING_HASH`（`e31fc17f…` → `03fcfad6…`）是唯一含检测器错误的报告，去掉新键即复现旧哈希。
- 实际运行（本分支集成后）：`pytest -m "not postgres" tests/research/synthetic_lab` → 79 passed, 1 warning (120 s)；`ruff check .` → 通过；`mypy research/synthetic_lab tests/research/synthetic_lab` → no issues in 10 files。
- 遗留：G5 逐臂证据（`SealedArmEvidence`）尚无同样的区间字段。

**B46 — apps：审计 B 代码发现 1～7（`588342c` / `4b9bd69` / `409f5ab` / `a83f2ab` 的 cherry-pick → `7f6af4f` / `bf43412` / `5798c36` / `4658357`；README `d45161f`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- API：`file:` URI、`~/` / `~user/`、冒号后路径只留文件名；兜底 500 `{"detail": "internal server error"}`（不含消息 / 路径 / traceback），`path.stat()` 移入读取保护；每个 operation 声明 500 为 `ApiError`（`openapi.json` / `api.d.ts` 重新生成）；只读测试遍历 `app.routes`，能发现 `include_in_schema=False` 的隐藏 DELETE。
- Fixtures：十种报告全部由提交的生成器产出；三份 2.0.0 文件保留为遗留（内容哈希文件名，`LEGACY_2_0_0`，`regenerate_legacy()` 在新进程 2.0.0 作用域逐字节重建）并各加 2.1.0 版本；Python / node / 页面详情测试读取每个种类的全部 fixture。
- Web：validation / matrix / router 解析移入 `src/lib`（`node --test`）；Validation Reports 显示精确门值（2.0.0 回退浮点）；Research Loop 每个用量维度一张带单位的图（每轮柱 + 累计线，分轴）。
- 实际运行（本分支集成后）：`pytest -m "not postgres" tests/apps tests/research/reports` → 372 passed, 1 warning；`tests/apps/test_live_backend_smoke.py` → 2 passed（含 `live-smoke.mjs`）；`npm test` → lib 75 / 75、组件 99 / 99；`npm run build` ✓；`ruff check .` / `ruff format --check .`（738 files）/ `mypy`（582 files）通过；重新生成 openapi / api.d.ts 无差异（子代理）。
- 仍不证明：真实浏览器渲染；502 / 500 仍未经真实 HTTP。

**B47 — apps：错误路径经真实 HTTP + 存储缺陷修复（`d1d22b1` 的 cherry-pick → `81161a1`；修复 `f5960a6`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 冒烟新增第三个 "broken" 子进程服务器：502（测试服务器仅测试用 `--knowledge-error` 注入失败 provider；`apps/` 无钩子）、篡改日志 500（真实 `JobRunner` 日志副本的哈希链断裂）、兜底 500；每项断言状态码在 openapi 中声明为 `ApiError`、schema / 模型往返、body 不含路径 / traceback / 异常类型名。
- 通道发现的**真实缺陷**：报告 JSON 含超过 4300 位的整数字面量时 `json.loads` 抛普通 `ValueError`（嵌套过深抛 `RecursionError`），存储未映射，整个种类的列表 500。修复：存储捕获 `ValueError`（含 `JSONDecodeError` / `UnicodeDecodeError`）与 `RecursionError` 为 malformed 条目；回归测试在修复前失败。
  冒烟的兜底 500 改用测试服务器仅测试用 `--fault-report-read`（仅该进程内 `ReportStore._read` 抛 `RuntimeError`，经真实路由）。
- 实际运行（本分支）：`pytest -m "not postgres" tests/apps tests/test_architecture_boundaries.py tests/test_docs_consistency.py` → 324 passed, 1 warning；`ruff check .` / `ruff format --check .`（738 files）/ `mypy`（582 files）通过。
- 仍不证明：控制台对 502 / 500 的呈现（`live-smoke.mjs` 不访问 broken 服务器）；兜底 500 经真实 HTTP 只以注入异常验证。

**B48 — Phase 9：G5 逐臂证据区间（`40afaba` 的 cherry-pick）**（`CODE_COMPLETE / DEBUG_PENDING`）

- `SealedArmEvidence.pass_rate_bounds = [passed/reached, (passed+detector_errors)/reached]`（与 `ArmEvidence` 同一向外取整）；仅在检测器错误 > 0 时写入 `sealed_oos_g5`，无运行到达 G5 时为 `None`；拒绝 `detector_errors` 超出 `[0, reached]`。没有固定哈希改变（没有固定报告使用 G5 模式）。
- G5 的配置错误传播核实已与 `calibrate()` 一致（`PROPAGATED_ERRORS` 原样重抛），新增测试固定该行为。
- 实际运行（本分支集成后）：`pytest -m "not postgres" tests/research/synthetic_lab tests/research/reports` → 153 passed, 1 warning (134 s)；ruff / mypy（582 files）通过。
- 遗留：端到端 G0–G5 率（`end_to_end_g0_g5`）仍无区间。

**B49 — Phase 12：替换提案作业（循环之外；`a6b27ec` 的 cherry-pick → `b3a68e4`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 核实循环生命周期守卫止于 OOS，循环内不会有 PAPER 后代，故不接入循环：新增独立研究作业 `research/evolution/replacement_job.py` `propose_replacements(...)`，全部输入由调用方给出（在任者须 ACTIVE / DEGRADED；候选的生命周期来自人工 Promotion 路径与其报告哈希；`read_lineage` 只读核验循环的 `lineage.jsonl` 哈希链）。
  证据：每个声明的报告须经 `research.router.evidence.check_report`（含 G5 的 PASS、subject = 候选、哈希一致）；非后代 / 未到 PAPER / 已提议的候选不产生提案，拒绝原因写入结果；提案恒为待人工批准，不改生命周期。
- `ProposalLedger`：`<path>.lock` 单写者锁 + 可选外部锚点（须在账本目录之外；截断 / 删除 / 回滚 / 分叉 / 锚点丢失在重开时拒绝；无锚点时尾部整行删除仍不可发现）。
- 通道发现并修复的缺陷：`propose_replacement` 要求库策略谱系中的知识条目也在策略谱系图里，导致库策略的每个后代都被拒绝；现在只要求缺失的**策略**祖先已记录。
- 实际运行（本分支集成后）：`pytest -m "not postgres" tests/research/evolution tests/research/router tests/test_architecture_boundaries.py tests/test_docs_consistency.py` → 131 passed；`tests/research/loop` → 161 passed, 1 warning (781 s)；ruff / format（740 files）/ mypy（584 files）通过。
- 遗留：循环自身的报告永远不含后代的 G5（每族一次密封 OOS），所以集成测试用测试专用的含 G5 PASS 报告支撑提案；锚点不认证新增行；循环状态目录与提案账本之间除谱系外无绑定。

**B50 — Phase 7 / 6：研究循环审计 A 发现 1 / 2 / 14（`bb9e696` 的 cherry-pick → `0220f9a`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 发现 1：内容核对失败改抛 `LlmContentUnverified(ValueError)`（携带 `.reason` / `.call`）；假设阶段只捕获 `(LlmDraftRejected, LlmContentUnverified)` 并记录 `rejected` / `call_hash` / `call`，其他 provider 错误（含普通 `ValueError`）使该阶段失败。
  行为变化：空 `llm_prompt` 原先每轮被当作拒绝记录，现在构造 `HypothesisStage` 时即拒绝（代码 / README / 测试已写明）。
- 发现 2：`llm_content_fingerprint(llm)` 仅对 `ContentVerifiedLLM` 给出 `{"llm_content_verified": true}`，并入状态目录指纹；以另一模式重开（两个方向）被拒绝，`compose_durable` 直接调用同样核对。未核验运行的指纹不变（`PINNED_FINGERPRINT_HASH`、`PINNED_RECORD_HASHES` 均未改）。
- 发现 14：新增 `tests/infrastructure/e2e/test_research_loop_dataset_conditional.py`：`ConditionalPlan(validate_cells=True)` 经 `open_dataset_loop`（SQLite 夹具，无网络 / PostgreSQL）端到端：全部单元登记、`conditional_cells` 记录、支持的单元仅以 G0–G3 验证并绑定本轮清单、重开复原相同的 trial 日志与记录哈希。
- 实际运行（本分支集成后）：`pytest -m "not postgres" tests/research/loop tests/research/hypotheses tests/infrastructure/e2e/test_research_loop_dataset_conditional.py` → 230 passed, 1 warning (875 s)；`ruff check .` / `ruff format --check .`（741 files）/ `mypy`（585 files）通过。

**B51 — 证据严格性：审计 A 发现 4 / 5 / 6 / 8 / 9 / 13（`a376da0` / `8e8799f` / `1d7b69b` / `06a3ec4` / `949cef8` 的 cherry-pick → `140b498` / `7302300` / `1d8fc8c` / `29a0592` / `1d7c90c`；集成修正 `b18fb94`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- F5 + F6（Promotion，C-A8）：`PromotionEvidence.profiles` 必填；每份报告的 Profile 须哈希 / ref 一致、状态 FROZEN、带 `provenance.calibration_report`，否则类型化拒绝（`profile_missing` / `profile_hash_mismatch` / `profile_not_frozen` / `profile_not_calibrated` / `profile_not_evidenced`）。今天没有冻结的 Profile → 所有真实晋升在此被拒；库策略即使有完整 TEST ONLY 证据也以 `profile_not_frozen` 被拒（替换原先的弱测试）。
- F4（ADR-0060）：Promotion 与路由证据模式要求报告含 Profile 规则对应的 `G2.market_benchmark.<rule>`（规则未登记时为裸 `G2.market_benchmark`），否则 `market_benchmark_missing`；证据模式 `EligibilityEvidence.profiles` 必填，报告的 Profile 未给出 → `profile_not_found`。验证器默认值与信任模式固定哈希不变。
- F9：持久 `AuditTrail(path)` 必须显式给出 `record_marks`（否则 `MarksChoiceRequired`）；内存审计默认不变，显式 `False` 保持固定审计头。
- F8：`DegradationCheck.insufficient_evidence` / `status`；所有指标缺失时绝不报告为健康，报告写 `"insufficient_evidence": true`（仅此情形，其余载荷与 `check_hash` 不变）。通道另加的新事件主题**未采纳**（扩展 ADR-0049 事件面，记为 D-DEG-IE 待 Codex）：`observe` 仍只发布退化事件。
- F13：边界测试确认 `plugins/` 与 `infrastructure/` 不 import `research/`。
- 集成修正：B49 的替换作业调用 `check_report`，F4 后必须传 `profiles` → `propose_replacements` 增加必填 `profiles`，新增 `profile_not_found` / `market_benchmark_missing` 拒绝测试。ADR-0005 与 `research/README.md` 的库策略拒绝说明同步。
- 实际运行：子代理在其基线（`820d771`）上全量非 PG → 6785 passed, 136 deselected (3091 s)；本分支集成后 `pytest -m "not postgres" tests/research/evolution tests/research/router tests/promotion tests/apps tests/research/reports` + 边界 + 文档一致性 → 624 passed；ruff / format / mypy（586 files）通过。本分支全量门禁见后续批次。
- 遗留：Profile 的 `status` 不在其内容哈希内，Promotion 信任调用方给出的对象上的状态（需 Profile 注册表才是真正权威）；路由证据模式不要求 FROZEN；两者都不要求 `inverse_control_reported` 时的 `G2.inverse_control`；控制台对新拒绝码与证据不足的显示由 web 通道处理。

**B52 — 控制台显示新拒绝码 / 证据不足；Phase 9 中等规模证据；`-m` CLI 缺陷（`a586406` / `5650358` 的 cherry-pick → `b1a3e08` / `dd6c8e1`；修复 `93477c6`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 全量门禁（本批之前的 HEAD `1cd3284`，含 B44～B51）：`uv run pytest -q -m "not postgres" -p no:cacheprovider`（6 GB 上限）→ **6889 passed, 136 deselected, 1 warning in 3036.72s (0:50:36)，退出码 0**。
- Web：`routerEligibility.ts` 为 `profile_not_found` / `market_benchmark_missing` 提供文案；G5 状态按拒绝码含义推断（不再假定 G5 是最后一项检查；未知码显示"未知"）。`degradationCheck.ts` 的 `checkStatus`：`insufficient_evidence: true`（或所有指标缺失的旧文件）显示 `INSUFFICIENT EVIDENCE` 与独立提示，绝不显示为 ok；非法组合（键不为 true、或与 `degraded: true` 同时出现）拒绝解析。
  新 fixture `degradation_check/50f53688…`（真实 writer 经提交的生成器产生，变体机制 `VARIANT_WRITERS` / `VARIANTS`；既有 fixture 全部未变）；新组件测试 `DegradationChecks.test.tsx` / `RouterStops.test.tsx`。
- Phase 9 证据（仅证据，不提阈值；`docs/research/calibration/`，驱动 `tests/research/synthetic_lab/evidence_setups.py`，TEST ONLY lax / strict Profile，95% Clopper-Pearson）：
  单标的（G5 开，每臂 250 种子，4320 根 1 分钟 bar）：lax 噪声 PASS 1/250 [0.000, 0.022]（G0–G5 0/250）；强度 0.2 检出 13/250 = 0.052 [0.028, 0.087]；强度 0.5 检出 125/250 = 0.500 [0.436, 0.564]；strict 各臂 0/250。
  双标的（每臂 200 种子）：lax all_noise 0/200、all_planted 51/200 = 0.255 [0.196, 0.321]、mixed 0/200；strict 各臂 0/200；池化 G1 负对照失败率 lax shuffle 0.060 / 0.090 / 0.060、shift 0.045 / 0.100 / 0.080。
  这些数字只描述 TEST ONLY 夹具在简单生成器上的行为，不据此选择阈值或 Profile。800 / 400 种子的尝试超时被停止，未写报告。
- 通道发现的缺陷：`python -m research.synthetic_lab.gate_calibration` 拒绝一切 setup（`-m` 以 `__main__` 执行第二份模块，setup 类不同）。修复：`__main__` 块转调包内模块的 `main`；新子进程测试修复前失败、修复后通过。
- 实际运行（本分支集成后）：`pytest -m "not postgres" tests/research/synthetic_lab tests/apps tests/research/reports` + 边界 + 文档一致性 → 584 passed, 1 warning (140 s)；`npm test` → lib 80 / 80、组件 105 / 105；`npm run build` ✓；ruff / format（746 files）/ mypy（588 files）通过。

**B53 — Codex 复核通过的四个修复（精确 cherry-pick，本地提交，未推送）**（`CODE_COMPLETE / DEBUG_PENDING`；复核结论见各 `docs/reviews/2026-09-26-*-implementation.md`）

- 基线：`1cd3284` 全量非 PostgreSQL 门禁（PID 1078055，唯一一次针对该 SHA 的全量运行）→ 6889 passed, 136 deselected, 1 warning in 3036.72s，退出码 0；只作基线证据。
- `a32a9b6` → `695add6`（B45）：`CalibrationReport` 区间在局部 28 位上下文中向外取整。定向：`pytest -m "not postgres" tests/research/synthetic_lab` + 边界 + 文档一致性 → 193 passed；ruff / format（746）/ mypy（588）通过。
- `dd4fada` → `ed25d2b`（B46）：`public_detail` 对 `file:` 协议名大小写不敏感。定向：`tests/apps tests/research/reports` + 边界 + 文档一致性 → 447 passed；ruff / format（747）/ mypy（588）通过。
- `558b099` → `7157461`（B49）：`ProposalAnchor` 自带 flock、从磁盘重读锚点、拒绝分叉或外来账本。定向：`tests/research/evolution tests/research/router` + 边界 + 文档一致性 → 149 passed；ruff / format（750）/ mypy（590）通过。
- `d389a39` → `4e6c220`（D-DEG-IE，Codex 决定）：所有指标缺失的劣化检查在 `research_loop.degradation.insufficient_evidence` 发布，ADR-0049 相应修订（取代 B51 中"不发布、待决定"的临时处理）。定向：`tests/apps tests/research/reports` + 边界 + 文档一致性 → 452 passed；ruff / format（751）/ mypy（590）通过。
- 每次 cherry-pick 后工作区干净、只含该提交的文件；新增 import 只来自标准库 / 本包（无跨平面依赖）。未改 `core/`、Schema、KnowledgeProvider 或 ADR-0055 状态。
- 注意：同一工作树中另一会话（2026-09-26 全阶段代码完成计划）并行提交了 `b1a3e08`、`dd6c8e1`、`93477c6`、`21ae9d8`、`dd158c9`，并把 `21ae9d8` 推到 `wip/all-code-completion`；这些提交不在 B53 范围内。


**B54 — Phase 9：G5 端到端区间（`dd158c9`；编号在集成会话的 B53 之后）**（`CODE_COMPLETE / DEBUG_PENDING`）

- `SealedArmEvidence.end_to_end_errors`（G0 – G4 出错运行 + G5 出错运行，两者不相交）与 `end_to_end_bounds = [e2e/n, (e2e+errors)/n]`（全部运行为分母，向外取整）；仅有错误时写 `end_to_end_bounds`，无错误报告哈希不变；不变量拒绝少于 G5 错误数、或错误 + 通过超过 n。
- 实际运行：`test_gate_calibration_g5.py` → 21 passed；ruff / format / mypy（590 files）通过。
- 证据复现：以修复后的 `python -m research.synthetic_lab.gate_calibration --setup tests.research.synthetic_lab.evidence_setups:single_instrument_evidence` 在当前代码上重跑（6 GB 上限）→ 输出与提交的 `docs/research/calibration/single_instrument_evidence.json` **逐字节相同**（`cmp`）。双标的报告未重跑。

**B55 — ADR-0055：知识标签 / 资产检索（契约 2.2.0；本地整合分支 `claude/adr-0055-integration`，基于 `a5836b2`，fast-forward 推送到 `wip/all-code-completion`）**（`CODE_COMPLETE / DEBUG_PENDING`；ADR-0055 Accepted 2026-09-26，Codex）

- 来源：Codex 决定能力方向（`docs/reviews/2026-09-26-adr-0055-codex-decision.md`，依 Raphael 授权）；独立实现在 `claude/adr-0055-tags-assets`（基于 `1cd3284`），
  隔离 worktree 全量门禁 `ed8e694` → 7029 passed, 1 skipped（控制台 live smoke 因缺 `node_modules` 跳过）, 136 deselected；Codex 复核后接受进入整合，
  并批准普通推送检查点 `origin/claude/adr-0055-tags-assets` = `ed8e694`。详见 [ADR-0055 实施说明](../reviews/2026-09-26-adr-0055-implementation.md)。
- 内容：契约 2.2.0（`KnowledgeItem.tags` / `assets`、`KnowledgeQuery.tags_all` / `assets_any`；规范 token、严格升序、无重复，不静默改写；`_FIELDS_SINCE`；
  2.0.0 / 2.1.0 载荷按记录版本逐位复现，`tests/golden/v2_1_0/`）；Provider `hlens_knowledge_local@1.1.0`（AND / OR 精确匹配）；Pydantic ≥ 2.12；
  种子显式写出 `schema_version: "2.1.0"`（哈希不变），**未**新增标签 / 资产（无具名人工审阅；分类提案只在实施说明 §5）；控制台 Knowledge Search 标签 / 资产输入。
- 2.2.0 信封的钉值影响逐项核实（实施说明 §3）；控制台 fixture 增加第二代遗留（2.1.0）。
- 组合：`8e1d653` → `48fe180`、`e02898a` → `a8490bf`（三处 fixture 冲突保留 B46 变体 / 2.0.0 遗留与 ADR-0055 的 2.1.0 遗留 / 2.2.0 当前）、`ed8e694` → `2b2a6a5`；
  新增 `62256b6`：Phase 9 证据（`dd6c8e1`）的报告与钉值由 2.1.0 代码生成，测试改为在新进程中按 2.1.0 构造 setup 核对（未修改的测试在该作用域内 11 passed），不重生成、不重钉。
- 实际运行（组合分支，文档提交前）：定向 `pytest -m "not postgres"` tests/apps、reports、synthetic_lab、evolution、router、ADR-0055 / 契约 / 文档 → `6 failed, 1710 passed, 1 skipped`（6 项为上述证据测试，修复后该文件 + 文档一致性 22 passed）；
  `npm run gen:api` 无差异；`npm test` → lib 87 / 87、组件 109 / 109；`npm run build` ✓；live-backend smoke 3 passed（组合 worktree 的 `node_modules` 为本地复制，锁文件相同，未安装）。
- 组合全量门禁（`1367dc8`，冻结 HEAD）：`START_SHA 1367dc8899353c329a0f646780b14f57fed69cee dirty=0 2026-09-26T19:12:36Z` → `7176 passed, 136 deselected, 1 warning in 2921.41s (0:48:41)`，`EXIT 0`（无 skip：live smoke 实际运行）→ `END_SHA 1367dc8899353c329a0f646780b14f57fed69cee dirty=0 2026-09-26T20:01:20Z`；静态检查、Schema、web 全部通过。
- Codex 复核修正（`test_evidence_setups.py`）：证据比较不再整类删除 `*_hash`，而是把每个派生哈希（含 `detector.strategy_hash`、`profile_hash`）绑定到其对象的 2.1.0 孪生哈希并逐字段比较；新增策略 / Profile 语义变化与篡改的反例；报告与钉值未改动（详见 [ADR-0055 实施说明](../reviews/2026-09-26-adr-0055-implementation.md) §9），提交为 `c08c589`。
- 代码冻结提交 `c08c589` 的全量非 PostgreSQL 门禁（`systemd-run --user --scope -q -p MemoryMax=5G -p MemorySwapMax=0 uv run pytest -q -m "not postgres" -p no:cacheprovider -rs`）→ `7179 passed, 136 deselected, 1 warning in 2906.24s (0:48:26)`，退出码 0；起止 SHA 均为 `c08c5895b4a60916715c2f88f65d061c40724b96`、dirty=0（2026-09-26T20:07:01Z → 20:55:29Z）；其后的 docs-only 提交**不在**该门禁覆盖范围内。
- Codex 于 2026-09-26 基于组合代码 `c08c589` 与上述门禁接受 ADR-0055（Accepted）；ADR / 代码接受**不等于** Phase 0.5 整体验收：仓库种子仍无具名人工审阅的标签 / 资产分类（只有实施说明 §5 的提案），Phase 0.5 验收仍待 Codex / Raphael。

**B56 — ADR-0062：Validation Profile 冻结登记（Codex 决定，2026-09-27 Accepted；本地整合分支，逐批快进推送到 `wip/all-code-completion`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 背景：`ValidationProfile.status` 不进内容哈希（ADR-0008），Promotion 原先信任调用方给的 `status = FROZEN`（B51 已指出需 Profile 注册表）。
- `3e1dca8` ADR-0062（Proposed，Q1 = A 独立登记，Q2 = A 校准证据验真实性与引用一致；目录外锚点必需）；`6e482e2` `ProfileFreezeRegistry`
  （追加式哈希链、单写者、必需锚点、校准报告原始字节固化、重放同一套规则）；`a89b00a` Codex 复核返修（写路径失败即作废实例；写任何 blob 之前
  执行全部规则；故障注入测试）；`7f93629` Promotion 以登记为权威冻结来源（无有效登记 → `profile_not_frozen`；`created_at` 不早于冻结批准）。
- 实际运行：阶段 1 返修后定向 `172 passed`、冻结登记测试 `56 passed`；阶段 2 提交前 `pytest -m "not postgres" tests/promotion tests/research
  tests/apps tests/test_*.py` → `4615 passed, 1 warning in 1293.77s`（退出码 0），ruff / format（760 files）/ mypy（595 files）退出码 0；
  变异检查见 [ADR-0062 实施说明](../reviews/2026-09-27-adr-0062-implementation.md)。
- 边界：批准人只是声明（不认证身份）；不是生产 Control Plane；不授权实盘 / 资金 / 部署；登记为空，所有晋升仍被拒；未改契约 / Schema /
  Profile 数值 / 哈希 / ADR-0008。
- 接受：Codex 于 2026-09-27 接受 ADR-0062（架构与实现方案），阶段 3 文档 `0cdf19f` + 日期修正 `3c344df` 已推送；ADR / 实现的接受**不等于** Phase 4 或 Phase 5 验收：冻结登记仍为空（没有任何 `profile.frozen` 记录，所有 Promotion 仍以 `profile_not_frozen` 被拒），Validation Profile 数值（D-09 TBD-1..5）仍未冻结。

**B57 — 冻结 WIP `1551b30` 的全量门禁与 B52 双标的证据复现（docs-only；只作证据）**

- 双标的证据复现（原代码基线，**不是** `1551b30`：Phase 9 证据由 2.1.0 代码生成，2.2.0 代码重跑会得到新的输入与 `report_hash`）：
  独立 worktree `/tmp/hlens-b52-repro-dd6c8e1`，HEAD `dd6c8e13d370bc69ebc13a3cbfa23111fddea37c`；命令
  `systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline python -c 'import sys; from research.synthetic_lab.gate_calibration import main; sys.exit(main(sys.argv[1:]))' --setup tests.research.synthetic_lab.evidence_setups:multi_instrument_evidence --out /tmp/calib-b52-dd6c8e1`；scope 2026-09-26T22:34:31Z → 22:52:31Z，CPU 17 min 59.7 s，内存峰值 577.9 MB，无 OOM；exec session 12194 退出码 0。
  唯一输出 `gate_calibration/0f04d1b649d7ce6357e47d84e6ae709d343bfcbb44271398a34cb2aa83f2cecd.json`；与提交的
  `docs/research/calibration/multi_instrument_evidence.json` `cmp` 退出码 0（逐字节相同，1358906 字节），两者文件 SHA-256 均为
  `d6723bbdf619ff04b623cc8a271ecfcb28ef21dfa8798a0fb2e0ec6c65699107`；报告内 `report_hash` =
  `0f04d1b649d7ce6357e47d84e6ae709d343bfcbb44271398a34cb2aa83f2cecd`，按规范 JSON 自哈希成立，`kind = gate_calibration`。
  此前一次同命令运行（PID 1295726，退出码 130）是有意停止，不计为结果。不据此选择阈值或冻结 Profile。
- 全量非 PostgreSQL 门禁（冻结 HEAD `1551b3042d351bbceeb577d20431b6e3a0c1f81a`，工作树干净，本地 = 远端；复现结束后才启动，不重叠；
  完整日志 `~/hlens-gate-logs/1551b30/`）：`START_SHA 1551b30… dirty=0 2026-09-26T22:53:54Z`
  - `systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q -rs -m "not postgres" -p no:cacheprovider`
    → `7244 passed, 136 deselected, 1 warning in 2897.18s (0:48:17)`，退出码 0（2899 s）；无 skip（控制台 live-backend smoke
    `tests/apps/test_live_backend_smoke.py` 无标记，随之实际运行）；1 warning 为 Starlette `httpx` 弃用提示。
  - `uv run ruff check .` → `All checks passed!`，退出码 0；`uv run ruff format --check .` → `761 files already formatted`，退出码 0；
    `uv run mypy`（6 GB 上限）→ `Success: no issues found in 595 source files`，退出码 0；`uv lock --check --offline` → `Resolved 52 packages`，退出码 0。
  - `uv run python -m core.contracts.registry` → 135 份 Schema，导出后 `git status schemas` 0 处变化；`uv run python -m apps.api.openapi` →
    `openapi.json` 0 处变化；`npm --prefix apps/web run gen:api` → `api.d.ts` 0 处变化（均退出码 0）。
  - `npm --prefix apps/web test` → lib `tests 87 / pass 87 / fail 0`、组件 `tests 109 / pass 109 / fail 0`，退出码 0；
    `npm --prefix apps/web run build` → `✓ built`，退出码 0。
  - `END_SHA 1551b3042d351bbceeb577d20431b6e3a0c1f81a dirty=0 2026-09-26T23:42:21Z`。本提交为 docs-only，不在该门禁覆盖范围内。
- **未决设计边界：后代 G5（只记录，不实施）**。事实：`EvolutionStage` 的后代沿用 `source.family_id`；`OosUnsealing` / `SealedOosVault` 每个 family 只允许一次密封 OOS 评估（宪法 C-S1..3），所以循环为后代产生的报告不含 G5，替换提案作业（B49 / B53）无法用循环自身的报告支撑提案。这是有意的保护，不是缺失的报告。风险：为后代再次消耗同一密封窗口会泄漏 holdout（父代的 OOS 结果已被看过，后代的产生以它为条件）。所需前置条件（均未决定）：新的预注册 family，或独立的、未来的、未被看过的密封窗口；以及对应的 Profile 证据规则（什么证据足以评估一个后代）。本批未改 OOS / Promotion 状态、`family_id`、Profile 或宪法。

**B58 — P10 证据模式要求 Profile 所要求的 ADR-0060 反向对照项（`9c2871d`；隔离分支 `claude/p10-inverse-control`，基于 `0612cc6`，快进推送到 `wip/all-code-completion`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 缺口（复核发现）：`research/router/evidence.py` 只要求 Profile 所选的 `G2.market_benchmark` 项；ADR-0060 规定 `benchmark.inverse_control_reported = true`
  即报告含 `G2.inverse_control`，但缺该项的 PASS 报告仍能通过路由资格。
- 修复：`check_report` 在市场基准项之后新增最后一项检查——匹配到的 Profile 该标志为 true 且报告没有逐字相同的 `G2.inverse_control` 门 →
  `inverse_control_missing`（`EligibilityRefusal` 新值）。只要求存在（只报告项，不设阈值、不改判定）；为 false 时行为不变。
  `ValidationReport` 要求判定由全部门导出，INCONCLUSIVE 的该项使报告判定 INCONCLUSIVE，已在更早的 `verdict_not_pass` 被拒（测试固定）。
  替换提案作业复用 `check_report`，同样拒绝。控制台 `routerEligibility.ts` 增加该码的中文说明（G5 显示为「通过」）。
- 测试：true + 缺失 → 拒绝（近似 id 也拒绝）；true + 存在 → 路由（任何已计算的数值，含负收益）；false + 缺失 → 路由；市场基准与反向对照各自必需、
  顺序在 Profile / 市场基准之后；拒绝写入 `RouterStop` 载荷与 `stop_hash`；替换提案作业 `inverse_control_missing`。夹具：路由测试 Profile 显式声明
  `inverse_control_reported`（工厂默认 true）；`toy_report` 默认带 `G2.inverse_control`（与既有 `market_benchmark` 参数相同模式）。
  在修复前的 `evidence.py` 上新测试 4 项失败，修复后通过。信任模式钉值（`TRUST_*`、`BASELINE_RUN_HASH`）未变；没有被钉的证据模式哈希。
- 实际运行（隔离 worktree，未与门禁重叠）：`pytest -m "not postgres" tests/research/router tests/research/evolution tests/promotion tests/research/reports`
  + 文档一致性 + 架构边界 → `394 passed, 1 warning in 27.92s`，退出码 0；`ruff check .` → `All checks passed!`；`ruff format --check .` →
  `761 files already formatted`；`mypy` → `Success: no issues found in 595 source files`；`npm test` → lib 88 / 88、组件 110 / 110，退出码 0；
  `npm run build` ✓，退出码 0。未另跑全量门禁（下一次全量门禁覆盖）。
- 独立复核（Codex，`9c2871d` 之后）：`systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline pytest -q -p no:cacheprovider
  tests/research/router tests/research/evolution/test_replacement_job.py tests/promotion` → `244 passed in 9.41s`。
- 复核修正（后续提交，不改写历史）：删去"G2.inverse_control 为 INCONCLUSIVE 而报告判定为 PASS"的不可能组合用例（ADR-0013 要求判定由全部门导出，
  ADR-0060 的 INCONCLUSIVE 项参与判定）；保留有效报告中的 INCONCLUSIVE 项 → 更早的 `verdict_not_pass`，以及门为 PASS 的负收益值 → 路由（只要求存在）。
  修正后同一命令 → `244 passed in 9.76s`；`ruff check` / `ruff format` 通过。
- 边界：未改契约 / Schema / 阈值 / Profile 数值；未改 Promotion——`research/promotion/service.py` 同样只要求市场基准项、不要求 `G2.inverse_control`
  （FOLLOW-UP，未执行）。

**B59 — Promotion 要求 Profile 所要求的 ADR-0060 反向对照项（隔离分支 `claude/p10-inverse-control`，基于 `2636f4a`，快进推送到 `wip/all-code-completion`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 缺口（B58 FOLLOW-UP）：`research/promotion/service.py::_check_profiles` 只要求市场基准项；与路由证据模式相同的 ADR-0060 遗漏。
- 修复：Profile 校验、冻结登记与市场基准项之后，报告评估 G2 且 `checked.benchmark.inverse_control_reported` 为 true 而没有逐字相同的
  `G2.inverse_control` 门 → `PromotionRefusal.INVERSE_CONTROL_MISSING`（`inverse_control_missing`；detail 写明报告、Profile 与所缺的项）。
  只要求存在；首个失败的顺序不变（冻结 → 市场基准 → 反向对照）；不评估 G2 的报告（密封 OOS）不要求。生产侧仍完全失败关闭（登记为空）。
- 测试：缺失 → 拒绝（近似 id 也拒绝），密封 OOS 报告不要求；两项都缺 → `market_benchmark_missing`；未冻结 → `profile_not_frozen` 在前；
  标志为 false → 无需该项、可构建。`toy_profile` 新增 `inverse_control_reported`（默认 true = 工厂默认，默认 Profile 哈希不变）。
  在修复前的 `service.py` 上新增的缺失用例失败，修复后通过。
- 实际运行（隔离 worktree）：`systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline pytest -q -p no:cacheprovider
  -m "not postgres" tests/promotion tests/research/router tests/research/evolution tests/test_docs_consistency.py tests/test_architecture_boundaries.py`
  → `319 passed in 10.62s`，退出码 0；`tests/promotion` 单独 → `164 passed in 1.26s`；`ruff check .` → `All checks passed!`；`ruff format --check .` →
  `761 files already formatted`；`mypy` → `Success: no issues found in 595 source files`。无控制台改动（Promotion 拒绝码不在控制台显示），未跑 web。
- 边界：未改契约 / Schema / 阈值 / Profile 数值 / ADR-0008；未改冻结登记；不据此冻结任何 Profile。`research/promotion/README.md` 顺带把 ADR-0062
  的状态由过时的 Proposed 更正为 Accepted。

**B60 — 全栈：控制台对真实后端 502 / 500 的呈现（live-backend smoke 覆盖 broken 服务器；隔离分支，基于 `dcea010`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 缺口（§10.8 全栈行）：`live-smoke.mjs` 只访问完整与未配置两个服务器，控制台对 502 / 500 的呈现未经真实后端。
- 修复（只改测试脚本与其 pytest 驱动，未改 `apps/` 或 `src/`）：`BROKEN_BASE_URL` + `EXPECT_BROKEN`（声明的 detail、任务 id、报告
  `<kind>/<id>`、不得外泄的字符串：工作目录与仓库路径、`Traceback`、`injected`、`secret`、异常类型名）。客户端：知识检索 502、`/jobs` 列表 / 详情 500、
  报告列表 / 详情兜底 500，detail 逐字等于声明值、`describeError` 不外泄；页面：Knowledge Search / Jobs / State × Strategy Matrices 服务端渲染显示错误状态、
  HTTP 状态与声明的 detail（按 react-dom 转义比较），无残留 loading、无外泄。两个环境变量须同时给出，否则退出码 2。pytest 核对 503 / 502 / 500 步骤确已输出。
- 实际运行：`systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run --offline pytest -q -p no:cacheprovider -rs
  tests/apps/test_live_backend_smoke.py -s` → `3 passed in 2.55s`（11 个 `live-smoke: ok` 步骤，三个服务器）；负对照：把期望的 502 detail 临时改错 →
  `1 failed`（`ERR_ASSERTION`），恢复后通过。
- 仍未证明：真实浏览器（像素、effect、交互）；人工浏览器验收仍 open；生产 ASGI 服务器未选定。
- 冻结 HEAD `dfa432b87ccd0dd7a855fb8da395008b4258e4b1`（含 B57～B60，工作树干净、本地 = 远端）的全量非 PostgreSQL 门禁（完整日志 `~/hlens-gate-logs/dfa432b/`）：
  `START_SHA dfa432b… dirty=0 2026-09-26T23:58:13Z`；`systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q -rs -m "not postgres"
  -p no:cacheprovider` → `7253 passed, 136 deselected, 1 warning in 2946.34s (0:49:06)`，退出码 0，无 skip；`ruff check .` → `All checks passed!`；
  `ruff format --check .` → `761 files already formatted`；`mypy`（6 GB）→ `Success: no issues found in 595 source files`；`uv lock --check --offline` →
  `Resolved 52 packages`；Schema 导出 135 份、0 处变化；`openapi.json` 0 处变化；`gen:api` 后 `api.d.ts` 0 处变化；`npm test` → lib 88 / 88、组件 110 / 110；
  `npm run build` ✓；全部退出码 0；`END_SHA dfa432b… dirty=0 2026-09-27T00:47:29Z`。其后的提交不在该门禁覆盖范围内。
- 另跑：`pytest -q -rs -m "not postgres" tests/apps tests/test_docs_consistency.py tests/test_architecture_boundaries.py`（6 GB 上限）→
  `386 passed, 1 warning in 8.62s`，退出码 0，无 skip；`ruff check .` → `All checks passed!`；`ruff format --check .` → `761 files already formatted`；
  `npm test` → lib 88 / 88、组件 110 / 110（`src/` 未改）。

**B61 — ADR-0054 §4：数据集回测 bar 带 Canonical 成交量（`infrastructure/bars/dataset.py`；隔离分支，基于 `9d78839`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 来源：Accepted ADR-0054 实施说明"未做（留给调试批次）"第一项；Codex 划定本批次（无新架构决定）。
- 实现：`_ProvenBar.volume` 取自经 lineage / 键 / 事件时间核对的已选 revision 的 `item.values["volume"]`（Canonical `decimal(38, 18)` 的 `Decimal`
  原值，无新 catalog 查询）；`backtest_bars_from_dataset` 写入 `PriceBar.volume`；非 `Decimal` / 缺失 → `CatalogIntegrityError`（同 OHLC）；
  `OutcomePriceBar` 不变。未改 core / 契约 / Schema / 版本 / 哈希规则。
- 哈希：数据集回测 `request_hash` 因真实非空 volume 改变（预期）；去掉 volume 逐位复现 B61 之前在两个独立世界中算得的 `e07c52d9…`（钉在测试中）；
  没有被钉住的数据集回测哈希，未重写任何快照。
- 测试：见 ADR-0054 实施说明"dataset volume mapping"；在 B61 之前的代码上 6 / 7 项新测试失败。
- 实际运行（隔离 worktree，6 GB 上限）：`pytest -m "not postgres" tests/infrastructure/bars` → `54 passed, 47 deselected in 281.03s`；
  `pytest -m "not postgres" tests/infrastructure/e2e tests/research/strategies tests/plugins/backtest tests/test_strategy_contracts.py`
  + 文档一致性 + 架构边界 → `280 passed, 9 deselected in 515.99s`，退出码 0；mypy 修正（测试内两处类型）后 `test_dataset_bars.py` → `20 passed in 93.91s`；
  `ruff check .` → `All checks passed!`；`ruff format --check .` → `761 files already formatted`；`mypy` → `Success: no issues found in 595 source files`。
  PostgreSQL 标记测试（`test_dataset_bars_postgres.py`、真实数据端到端）**未运行**：本批次没有授权使用专用 Phase 1 测试库，未创建任何数据库。
- 边界：不声称 Phase 4 / 5 验收；G4 容量检查未改读结转结果（ADR-0054 其余未做项不变）。

**B63 — 控制台证据模式说明写明 B62 边界（Codex P10-FREEZE 决定的文字落实；隔离分支，基于 `0ced9ca`）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 缺口：资格证据表上方的说明只列市场基准项（未列 B58 的 `G2.inverse_control`），也未写明 B62：证据模式不检查 Profile 冻结登记、通过不代表
  Profile 已冻结 / 已获 Promotion / 具备生产资格。
- 改动（只改控制台文字与测试）：`routerEligibility.ts` 新增 `EVIDENCE_SCOPE_TEXT`（研究层纸面路由前提；不检查冻结登记；冻结与晋升由 Promotion
  核验，ADR-0005 / ADR-0062；生产资格归 Control Plane）；`EligibilityEvidence.tsx` 列出两项 ADR-0060 G2 报告项并显示该边界；
  lib 测试逐句固定边界文字、已核验结果文字不变；Router Stops 组件测试断言边界与 `G2.inverse_control` 出现在渲染结果中。未改 `research/router`、契约或 API。
- 实际运行：`npm test` → lib 89 / 89、组件 110 / 110，退出码 0；`npm run build` ✓，退出码 0。
- 本批次在冻结门禁 SHA `0ced9ca` 之后，不在该门禁覆盖范围内。

**B65 — ADR-0063：本机只读 API 的 ASGI 运行时 Uvicorn（Codex 决定）**（`CODE_COMPLETE / DEBUG_PENDING`；**真实 Uvicorn 运行待 Raphael 授权安装**）

- 决定（Codex）：Uvicorn 作为本机只读研究 API 的 ASGI 运行时；严格绑定 `127.0.0.1`、单 worker、默认不 reload；`apps/api` 保持框架无关；
  Uvicorn 为可选 `api-server` 依赖 + 有文档的可运行入口；公网 / TLS / 认证 / 反向代理 / HA 不在范围内。
- 离线解析探针（草稿副本，未改项目文件、未安装）：`uv lock --offline` → `Resolved 53 packages … Added uvicorn v0.53.0`，唯一新增包 `uvicorn`。
- D-UVICORN：Codex 选 A（只用本机缓存的 uvicorn 0.53.0、可选 extra、不触网、不新增其他依赖）并授权仓库改动；安装仍待 Raphael（H12）。
- 实施（仓库改动，未安装）：`pyproject.toml` extra `api-server = ["uvicorn==0.53.0"]` 与 mypy 对 `uvicorn` 的缺失导入忽略；`uv lock --offline` → `Resolved 53 packages … Added uvicorn v0.53.0`（锁文件只新增该条目）；wheel 摘要 `e8dca71e…` 与本机缓存一致；入口 `apps/api/serve.py`；测试 `tests/apps/test_api_server.py`；ADR-0063 实施说明、`apps/api/README.md`、`tests/apps/live_server.py` 文档串同步。
- 实际运行（未安装 Uvicorn）：`pytest -q -rs tests/apps/test_api_server.py` → `21 passed, 2 skipped`（跳过原因：extra 未安装）；`pytest -q -rs -m "not postgres" tests/apps` + 文档一致性 + 架构边界 → `407 passed, 2 skipped, 1 warning in 9.95s`；`ruff check .` → `All checks passed!`；`ruff format --check .` → `764 files already formatted`；`mypy` → `Success: no issues found in 597 source files`；`uv lock --check --offline` → `Resolved 53 packages`。每次运行后 `uv pip show uvicorn` → 未找到（没有被安装）。
- **未运行 / 待 Raphael 批准**：`uv sync --offline --extra api-server && uv run --offline --extra api-server pytest -q -rs -p no:cacheprovider tests/apps/test_api_server.py`（真实 Uvicorn 子进程两项）。不得记为通过。
- 注意：默认全量门禁此后会多出 2 个带原因的 skip（真实 Uvicorn 子进程测试），默认依赖与安装内容不变。

**B66 — ADR-0064（Accepted 2026-09-27，Codex）：G4 容量的成交量来源一致性（选项 A）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 发现：数据集路径上 G4 容量只读调用方的 `bar_volume`（研究循环取自特征 manifest 观察），而已执行的 `DatasetPriceBars` 自 B61 起带证明过的
  `PriceBar.volume`；两者冲突时今天静默使用前者。
- Codex 选 A：两来源在确切 `(instrument, fill_time)` 上 `Decimal` 精确相等 → 结果不变；不一致 → `G4.capacity.estimated` INCONCLUSIVE
  `bar_volume_source_mismatch`，不计算容量与冲击；缺值 → 保持 `bar_volume_missing`；合成路径逐字节不变；不改阈值 / Profile / 宪法 / 契约 / Schema。
- `91c5630` 提交 ADR 草案（Proposed）；Codex 接受原文措辞（精确 `Decimal` 比较、不一致优先于缺失、原因名）后记为 Accepted（`eaf239b`），随后实施。
- 实施：见 ADR-0064 Implementation note（`robustness.py` 新 INCONCLUSIVE 分支、`_capacity_fills` 在数据集路径逐笔比较、测试与夹具）。
- 实际运行：（6 GB 上限，未过滤管道、`set -o pipefail`，pytest 自身退出码）`pytest -q -rs -m "not postgres" tests/research/strategies tests/research/validation tests/research/synthetic_lab tests/research/loop tests/infrastructure/e2e/test_research_loop_dataset_conditional.py tests/infrastructure/e2e/test_research_loop_dataset_g5_units.py` + 文档一致性 + 架构边界：第一次（修正 `test_cross_sectional_momentum.py` 的数据集路径夹具之前）→ `2 failed, 711 passed, 1 warning in 1363.20s`，**退出码 1**（两项为 `test_validated_end_to_end_on_the_multi_instrument_path[p,n]` / `[p,p,n]`：交易输入不带 volume，G0.manifest_binding 正确地判 FAIL）；修正后该文件 `29 passed`（退出码 0）；第二次完整运行 → `713 passed, 1 warning in 1360.32s`，**退出码 0**，无 skip。在 B66 之前的源码上 6 项新测试中 4 项失败（相等与只比较成交 bar 两项为不变量）。`ruff check .` → `All checks passed!`（退出码 0）；`ruff format --check .` → `765 files already formatted`（退出码 0）；`mypy` → `Success: no issues found in 597 source files`（退出码 0）。真实数据端到端 `test_research_pipeline_real_data.py`（PostgreSQL 标记）**未运行**：其交易输入即 `backtest_bars_from_dataset` 的 bar、`bar_volume` 取自同一选择的观察，按构造两来源一致。
- 未运行：PostgreSQL 标记测试；B65 的两项真实 Uvicorn 测试仍因未安装而跳过（待 Raphael H12 批准）。
- 冻结门禁（只覆盖 `255ce1a2fe76bc655901ad6e7960e8359bc54b34` = B63～B66；**不含** B67）：独立 detached checkout `.claude/worktrees/gate-255ce1a`，完整日志 `~/hlens-gate-logs/255ce1a/`。`START_SHA 255ce1a… dirty=0 2026-09-27T03:14:47Z`；`systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q -rs -m "not postgres" -p no:cacheprovider` → `7287 passed, 2 skipped, 136 deselected, 1 warning in 3212.68s (0:53:32)`，退出码 0；**2 skipped** 为预期的真实 Uvicorn 子进程测试（`tests/apps/test_api_server.py:218`：可选 api-server extra 未安装，安装待 Raphael 按 H12 批准），**不是通过**；`ruff check .` → `All checks passed!`；`ruff format --check .` → `765 files already formatted`；`mypy` → `Success: no issues found in 597 source files`；`uv lock --check --offline` → `Resolved 53 packages`；Schema 135 份 0 处变化；`openapi.json` 0 处变化；`api.d.ts` 0 处变化；`npm test` → lib 89 / 89、组件 110 / 110；`npm run build` ✓；全部退出码 0；`END_SHA 255ce1a… dirty=0 2026-09-27T04:08:42Z`。PostgreSQL 标记测试未运行。

**B67 — ADR-0065（Accepted 2026-09-27，Codex）：G4 容量遇到结转未成交余量时失败关闭（数据集路径）**（`CODE_COMPLETE / DEBUG_PENDING`）

- 缺口：ADR-0054 实施说明未做项"G4 容量检查未改读结转结果"——结转模型下目标可留有未成交余量，容量只看已执行成交，可能高估。
- Codex 选方案 A：数据集路径上任一正余量 → `G4.capacity.estimated` INCONCLUSIVE `carry_over_unfilled`，不计算容量 / 冲击；无 / 零余量、合成路径、默认
  `next_bar_open` 结果与哈希不变；复核决定不记录跨标的余量合计。先后：`profile_field_missing` → `bar_volume_source_mismatch` → `carry_over_unfilled` →
  `bar_volume_missing` → `no_trades`。
- 实施与测试：见 ADR-0065 Implementation note（B67）；权威基线：数据集路径报告 `8ed6bf10…`、合成路径报告 `f46de6b1…`、容量载荷 `847eefcf…`（B67 之前源码上两次计算一致）。
- 原始运行记录（含失败）：见 ADR-0065 Implementation note 第 1～8 条；要点：第一次定向运行 `1 failed, 14 passed`（退出码 1，测试夹具违反有符号数量不变量）；
  第一次基线计算在错误源码上运行、作废；最终代码完整回归 `1 failed, 719 passed`（退出码 1，ADR 索引缺 ADR-0065 行），补行后文档测试 `20 passed`（退出码 0）。
- 未运行：PostgreSQL 标记测试。本批次在冻结门禁 `255ce1a` 之后，最终 HEAD 另做全量门禁。
- **最终门禁（完整代码，只覆盖 `6d887b70f9a1b925a602476204773a321d8ce4b2` = B67 代码提交）：通过**。独立 detached checkout `.claude/worktrees/gate-6d887b7`，日志 `~/hlens-gate-logs/6d887b7/`。`START_SHA 6d887b7… dirty=0 2026-09-27T04:10:38Z`；`systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q -rs -m "not postgres" -p no:cacheprovider` → `7294 passed, 2 skipped, 136 deselected, 1 warning in 3128.68s (0:52:08)`，退出码 0；**2 skipped** 为预期的真实 Uvicorn 子进程测试（可选 extra 未安装，待 Raphael H12 批准），不是通过；`ruff check .` → `All checks passed!`；`ruff format --check .` → `766 files already formatted`；`mypy` → `Success: no issues found in 597 source files`；`uv lock --check --offline` → `Resolved 53 packages`；Schema 135 份 0 处变化；`openapi.json` 0 处变化；`api.d.ts` 0 处变化；`npm test` → lib 89 / 89、组件 110 / 110；`npm run build` ✓；全部退出码 0；`END_SHA 6d887b7… dirty=0 2026-09-27T05:03:09Z`。PostgreSQL 标记测试未运行。
- 其后的 docs-only 提交 `2158945`（门禁运行中标记）与 `04cf1c0`（§12.1 同步）**不在**该全量门禁覆盖范围内；只对它们运行了文档检查：Codex 在干净的 WIP HEAD `04cf1c0` 上 `uv run --offline pytest -q -rs -p no:cacheprovider tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → `20 passed in 0.46s`，退出码 0。

**P12-LOOP — Codex 决定（2026-09-27，docs-only，无代码）：替换提案不接入持续循环**

- 决定：替换提案保留在持续循环之外的显式作业中（`research/evolution/replacement_job.py`）。ADR-0045：循环的 `LifecycleGuard` 只到 `OOS`，`OOS → PAPER` 须人工批准（ADR-0006），循环内在此之前生成 `ReplacementProposal` 会虚假宣称资格；后代与父代同属一个 family，不得复用密封窗口；不得为规避这一点新建 family 或猜测阈值 / Profile 规则。
- 性质：循环内自动触发是**有意暂缓**，不是当前范围内未实现的代码任务（取代 B57 中"未决设计边界"的表述：边界本身不变，处理方式已决定）。
- 将来：自动触发须另立 ADR，定义独立、预先登记的密封评估以及证据 / Profile 规则。

### 10.8 审计后续汇总（取代 10.6 中下列各行；其余行不变）

| Phase | 本轮新增（批次） | 仍未完成 / 待决 |
|---|---|---|
| 7 | 内容核对失败记录调用、核对模式写入状态目录指纹（B50） | 只有脚本化 LLM |
| 6 | 数据集组合根上的条件计划端到端测试（B50） | 同 10.6 |
| 5 | Promotion 要求 FROZEN 且带校准报告的 Profile 与 ADR-0060 市场基准项（B51）；冻结以 ADR-0062 的追加式、带锚点的 Profile 冻结登记为权威（B56，ADR-0062 Accepted）；Profile 要求时还须有 `G2.inverse_control`（B59） | 今天登记为空 → 所有晋升被拒（设计如此）；批准人只是声明，登记不是生产 Control Plane |
| 0.5 | 按标签 / 资产检索（ADR-0055 Accepted，契约 2.2.0，含控制台；B55） | Phase 0.5 未验收：种子尚无具名人工审阅的标签 / 资产 |
| 9 | 配置错误不再被吞（B45，区间取整由 Codex 复核修复 B53）；G5 逐臂与端到端区间（B48 / B54）；中等规模证据报告（单标的 250 / 双标的 200 种子，B52）；`-m` CLI 修复（B52） | 不产生阈值（D-09）；双标的报告已在原基线 `dd6c8e1` 上逐字节复现（B57，只作证据） |
| 10 | 证据模式要求报告的 Profile 与市场基准项（B51）；Profile 要求时还须有 `G2.inverse_control`（B58） | **已决定（B62，Codex）**：不要求 Profile `FROZEN` / 冻结登记；冻结登记仅由 Promotion 作权威核验，路由通过不构成生产资格证明（ADR-0043） |
| 11 | 劣化检查证据不足绝不显示为健康（B51 / B52）；D-DEG-IE 由 Codex 决定：在 `research_loop.degradation.insufficient_evidence` 发布，ADR-0049 修订（B53，集成会话）；持久审计须显式 `record_marks`（B51，Phase 13 侧） | NATS / Control Plane（D-10） |
| 12 | 循环之外的替换提案作业：逐份核验证据、账本单写者锁 + 外部锚点（锚点自带 flock、重读、拒绝分叉 / 外来账本，B53）、库策略后代谱系缺陷修复（B49） | 循环内触发提案：**Codex 决定有意暂缓**（P12-LOOP，2026-09-27；提案留在循环外的显式作业，循环只到 OOS、OOS → PAPER 须人工批准；后代不复用密封窗口；将来须新 ADR 定义独立预注册密封评估与证据 / Profile 规则）；锚点不认证新增行 |
| 全栈 | 真实进程 + 真实 HTTP 冒烟含 502 / 篡改日志 500 / 兜底 500（B44 / B47）；报告存储解码缺陷修复（B47）；`file:` 协议名大小写不敏感（B53）；兜底 500、路径清除、按路由只读检查、逐维度用量图、精确门值、十种 fixture 与 2.0.0 遗留 fixture（B46）；新拒绝码与证据不足显示（B52）；`node --test` 80 + 组件 105 | 浏览器手工验收未做（控制台对 502 / 500 的呈现已经真实后端 + 服务端渲染验证，B60，非浏览器）；本机 ASGI 运行时已由 ADR-0063 选定 Uvicorn（仅 127.0.0.1；入口与测试已实施，真实运行待安装授权，B65）；公网部署 / TLS / 认证未决 |

架构边界：本轮（`c36005b..` 最终 HEAD `3be497b`）没有改动 `core/` 或 `schemas/`（Schema 仍 135 份，契约 2.1.0）；B55 在组合分支上改动 `core/`（契约 2.2.0，ADR-0055）与全部 135 份 Schema 的信封默认值及三份知识 Schema；新增 `plugins/` / `infrastructure/` 不得 import `research/` 的边界测试（B51）。

工作树协调：2026-09-26 19:39 起集成会话（依 Codex 复核）在同一工作树中直接 cherry-pick / 提交（B53：`695add6`、`ed25d2b`、`7157461`、`4e6c220`、`90edc6b`、`ec2a8b0`），与本会话提交交错；本会话首次在 `dd158c9` 上启动的全量门禁在 5% 处被 SIGTERM 终止（退出码 143，HEAD 已移至 `ec2a8b0`），不计为结果。最终门禁见下。

**最终门禁（审计后续，HEAD `3be497b`）**：在该提交的独立 detached checkout 中（本工作树的门禁被外部按命令行模式发出的 SIGTERM 连续终止四次——三次约 2.5 分钟、一次 25 分钟，均退出码 143、无 OOM、峰值内存 < 700 MB，不计为结果；已通知相关会话），
经 `pytest.main(["-q", "-m", "not postgres", "-p", "no:cacheprovider", "tests"])` 包装脚本、6 GB 上限运行 → **7031 passed, 136 deselected, 1 warning in 3048.69s (0:50:48)，退出码 0**（无 skip：控制台 live smoke 也实际运行）。
同一 HEAD：`ruff check .` 通过；`ruff format --check .` → 751 files already formatted；`mypy` → no issues in 590 source files；`uv lock --check --offline` 通过；Schema 135 份（`core/` / `schemas/` 自 `c36005b` 未变）；`npm test` → lib 80 / 80、组件 105 / 105；`npm run build` ✓。

### 10.9 Codex 决策（2026-09-27）：P10 证据模式不要求 Profile 冻结登记（B62）

- 决定：`research/router` 的证据模式是研究层纸面资格检查；它核验报告哈希、subject、PASS、G5、报告所用 Profile 身份 / 内容哈希，以及该 Profile 声明所要求的 ADR-0060 G2 报告项，但**不**要求 Profile `status = FROZEN`，也不读取 `ProfileFreezeRegistry`。
- 原因：Profile 冻结登记是 ADR-0062 为 Promotion 定义的权威生产晋升门；将其加入研究层路由会把研究证据检查与生产晋升耦合。Router 的 lifecycle 映射仍由调用方提供，故该模式通过只表明研究报告满足路由前提，不证明 Profile 已冻结、策略已晋升或具备生产资格。
- 边界：不改 Constitution、Profile 数值、契约、Schema、Promotion 或 Control Plane；不授权生产部署或实盘。生产资格仍由 Promotion / Control Plane 决定。详细决策记录见 ADR-0043。
- 冻结门禁（只覆盖 `0ced9ca983e420206992b2b96ac48bb6f4804bef` = B61 + 本决定；**不含** B63～B66）：在独立 detached checkout `.claude/worktrees/gate-0ced9ca` 中运行，完整日志 `~/hlens-gate-logs/0ced9ca/`。`START_SHA 0ced9ca… dirty=0 2026-09-27T01:20:23Z`；`systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q -rs -m "not postgres" -p no:cacheprovider` → `7260 passed, 136 deselected, 1 warning in 3094.07s (0:51:34)`，退出码 0，无 skip；`ruff check .` → `All checks passed!`；`ruff format --check .` → `761 files already formatted`；`mypy` → `Success: no issues found in 595 source files`；`uv lock --check --offline` → `Resolved 52 packages`；Schema 135 份 0 处变化；`openapi.json` 0 处变化；`api.d.ts` 0 处变化；`npm test` → lib 88 / 88、组件 110 / 110；`npm run build` ✓；全部退出码 0；`END_SHA 0ced9ca… dirty=0 2026-09-27T02:12:21Z`。B61 的 PostgreSQL 测试仍未运行。
136 个 deselected 为 PostgreSQL 标记测试（本分支不接触真实数据库）。

### 10.10 2026-09-27 当前代码 / 内容收口（PR #6 / #7 已合并；阶段验收延后）

| 范围 | 当前结论 | 本批动作 / 边界 |
|---|---|---|
| Phase 0.5 | Knowledge Base 主体已在 `main`；因子 / 特征人类可读库此前缺条目 | 增补带引用的横截面动量与 Amihud illiquidity 文档草稿；均为 `UNVERIFIED / NOT_VALIDATED`，不是 KnowledgeItem，不含人工标签 / 资产审阅或本项目复现结论 |
| Phase 1 | D3E 已接受、D4 已关闭；E1-CAP-1 仍阻断 | 已有 500k resume / replay RSS 增长 59.9 / 63.9 MiB，超过 32 MiB 门槛；只读调查指向 PyIceberg 全量 snapshot metadata，尚待设计有界精确历史索引；未重跑探针 |
| Phase 2～7 | 核心模块逻辑已在 B1～B67 主线；本轮只发现内容索引缺口 | 增补一个 `bar_log_return` 向上穿越零的 Event 定义草稿，标注未注册 / 未验证、无市场效果主张；不改策略、风险、状态默认值或阈值 |
| Phase 3 | Event Provider、DSL、运行存储、`event.events` 逻辑表及独立操作入口已实现 | ADR-0066 明确单独 CLI：默认不读配置、不连接 catalog；`--apply` 只调用 Event 表 ensure 函数，不接入 Phase 1 / 自动启动。生产 catalog 操作仍未授权、未执行 |
| Phase 8～14 / apps | 只读差距审计未发现明确、已批准而缺失的普通模块代码 | 同步 Event 字段、Research Loop / Router / API README 的实际行为，修正文档过时描述 |

PR #6 本批独立分支集成检查：`uv run pytest -q tests/infrastructure/event/test_create_event_tables.py tests/test_docs_consistency.py` → **11 passed in 1.27s**；`git diff --check` → 通过。PR #7 仅同步合并后状态（docs-only），没有改功能代码。没有运行 Phase 验收或真实 catalog 操作；最近一次全量代码门禁仍是 B67 `6d887b7` 的已记录结果，不代表当前主线 `f58e8ec` 上运行过全量门禁。

### 10.11 P6 矩阵报告接入（2026-09-27；独立分支实现，待复核）

只读审计发现 loop 的 `ExperimentStage` 已计算 `StateStrategyMatrix`，但实验摘要只保留 `matrix_hash`；已有 `write_state_strategy_matrix` 与只读 API / 页面没有真实循环的调用接线。补充 `run_unattended_and_report(..., reports_root=...)`：对该次运行产生的完整矩阵调用既有 writer，文件沿用 `state_strategy_matrix/<matrix_hash>.json`；相同内容保持幂等 no-op，内容冲突继续由 append-only writer 拒绝。矩阵只作为独立报告写入，不改变 LoopRecord 的摘要、载荷或 hash。为让研究侧 wrapper 读取刚完成的 ExperimentStage 产物，`ResearchLoop.stages` 提供只读阶段视图；只有 wrapper 在 `reports_root` 存在时才临时安装矩阵 callback，将本次矩阵存入局部列表供 writer 消费。无报告 sink 时不保存矩阵对象；循环正常返回或抛错都会恢复原 callback。实现位置：`apps/worker/loop.py`、`research/loop/trials.py`、`research/loop/compose.py`、本 README。未修改 core/contracts、Phase 1 或 status；未运行 / 新增测试，待后续独立验证。

### 10.12 全模块差额审计与 P8 报告链（2026-09-27；本地协调分支，待验证）

本轮以干净主线 `44fe9a2` 对照 `PROJECT_STATUS.md`、roadmap、相关 ADR、执行路径和控制台实际读写链。**项目不是空骨架**：P0.5、P2～P14 与 apps 已有大量真实计算、持久账本、验证、报告和只读页面；当前集中问题是少数跨模块接线、少数尚未定义的运行语义，以及未验收 / 未跑真实数据。代码完成不能替代 Phase 验收。

| 范围 | 深审结论 | 当前动作 / 边界 |
|---|---|---|
| P0.5 | 检索、审阅写入与标签 / 资产路径已实现；余项为种子的具名人工标签审核、资料草稿核验 | 不自动给种子分类；等待具名审阅者 |
| P2 | 状态计算、诊断、Arrow 表物化已实现；ADR-0035 原批次明确排除 catalog 持久化 | 当前批次按 ADR-0035 已完成其批准范围；额外 Iceberg 持久化属于可选扩展，本轮暂缓，不作为当前代码缺口（未采纳 Proposed ADR-0067 草案） |
| P3 / P4 / P5 | 事件引擎 / 显式 Event 表操作、Outcome 持久化 / 验证门、策略到回测验证链均有实质逻辑 | 生产 catalog 建表需单独授权；Profile、真实历史数据和阶段验收不是普通代码缺口 |
| P6 | 矩阵计算、条件试验预登记与逐单元验证已实现；循环原先只写 `matrix_hash`，没有把完整矩阵交给报告 writer | 本地协调分支补上可选报告接线；复用现有 `matrix_hash` writer / API / 页面，不改变 `LoopRecord` 身份；未测试 |
| P7 | 严格草稿、人工审阅、参数点 batch 和知识来源核验已实现；conditioning / interaction / temporal / transformation / ensemble / negation 目前作为数据规格生成，但不进入试验执行，batch 明确 fail closed | 不把自然语言转成代码。任何让这些算子进入 loop 的能力，都要先有封闭、类型化、确定性语义；跨 Phase 编排 / Strategy / Feature 执行改变需 Proposed ADR。`ContentVerifiedLLM` 已有但可选，未包装引用不满足完整可复现审计，状态文档不得称全闭环 |
| P8 | 回溯审计纯逻辑已实现且保护“旧拒绝不翻案”；缺少报告 writer、API kind 与 Web 页面 | 本地协调分支补齐显式 `RetroAuditReport` 输入 → append-only writer → 只读 API / 页面；gate diff 展示 metric、实际 / exact value 与阈值及来源。无自动扫描、无生命周期转换；未测试。原十种报告 fixture 生成器、页面组件测试和 live-smoke 清单未更新，需在验收阶段补齐 |
| P9 / P10 / P12～P14 | 合成验证、纸面 Router、进化提案、仅模拟执行与迁移框架存在 | 合成证据不是市场结论；P12 循环内替换提案有意暂缓；无具体迁移目标；实盘仍禁止 |
| P11 / Worker | 劣化检查本身有独立 API、报告 writer 和页面；新 Profile 的 Decimal 阈值读取已修正。仍缺活跃对象、近期窗口和验证基线的数据来源；Worker 没有独立启动器去组合预算、报告目录与 loop 持久目录 | 不将监控强塞进审计链，不添加 API 写 / 启动 endpoint；先决定本机 operator 的数据输入、预算、Profile、状态目录及触发方式，再提设计 / ADR |
| Apps | 当前是只读研究报告与知识 / job 视图，遵循 ADR-0048 | 只读约束是有意选择，不算应用写功能缺失；公网认证、TLS、HA 不在当前范围 |
| Phase 1 | D3E 已接受、D4 已关闭，E1-CAP-1 仍阻断 | Codex 子代理的只读调查报告已记录；无安全局部修复，不并行改 canonical / revision，后续需有界历史核验设计和容量证据 |

P6 / P8 子任务提交：`cc94b226`、`396b9730`、`8ed72247`；P11 阈值修正源提交：`ccf45a1`。集成到本地协调分支 `codex/module-completion-coordination-2026-09-27`（P6 / P8 提交 `347b538`、`15bec90`、`3dc3e67`；P11 提交 `b70c530`），**未推送 / 未合并 `main`**。Codex 的静态复核发现 P6 缺少 `StateStrategyMatrix` 导入及一处格式问题，已修复；触及的 5 个 Python 文件 `ruff check` / `ruff format --check` 通过，`git diff --check` 通过。P11 子代理另报告目标文件 Ruff、format 与 mypy 通过。本轮没有运行测试；P8 子代理原始静态结果也为 Ruff 通过、format check 3 files already formatted。OpenAPI 类型生成因环境中缺少 `openapi-typescript` 未运行，`api.d.ts` 手动同步，待验收复核。

独立复核发现 P8 初版 gate diff 未展示具体阈值，且比较未包含 ADR-0052 exact values；已在 `e319314` 让报告增加 recorded/current metric、value、threshold（含 exact 文本）及来源，报告 schema 升至 1.1.0，旧 1.0.0 页面解析保持兼容。Python Ruff / format / mypy 通过；`npm ci --offline` 成功，`npm run build` 通过。未运行测试；接受前仍需补组件 / writer 验收用例并检查真实报告页面。

**下一批顺序**：先完成 P6 / P8 / P11 独立代码复核；后续验收时同步 P8 fixture 生成器、组件测试与 live-smoke 清单。并行推进 P7 类型化算子规格 / 基础实现与 P11 显式观测输入设计前，先划清文件边界。E1 需单独设计精确保留历史校验语义的有界 metadata 索引；P2 Iceberg 持久化扩展暂缓。不得将未验收阶段、人工标签、Profile 数值、真实数据结果或生产建表描述为代码已完成。

### 10.14 P7 typed operators 与 P11 degradation operator 设计（2026-09-27）

- **P7：** Proposed [ADR-0068](../adr/0068-phase7-typed-operator-plans.md) 定义闭世界、类型化 AST、实现身份 / hash、全量预登记和拒绝边界；由于六类组合 DSL（conditioning / interaction / temporal / transformation / ensemble / negation）的业务语义和 Provider lowering 不完整，全部仍 fail closed。既有 `parameter_point` 路径不变；本提案不是其余六类已实现或已验收的声明。[ADR-0070](../adr/0070-p7-partial-experiment-fail-stop.md) 为最后一条 experiment stage 失败的 audit 增加持久可推导的 `recovery_required` fail-stop，避免下一轮重复已登记 attempt；不做自动 outcome 恢复。
- **P11：** [显式 degradation operator 规格](p11-degradation-operator-spec.md) 已由 Accepted ADR-0067 落地为协调 worktree 实现：显式提供 ACTIVE 历史、冻结 Profile、PASS baseline、近期观测 manifest 与窗口；报告 evidence 内嵌完整 manifest 并参与 schema 1.1.0 hash。legacy 1.0.0 hash 保持不变。历史是否最新、外部来源真实性、指标聚合真实性仍不验证；未并入本地 `main`，未测试、未验收。

### 10.15 本地 main 模块收敛与分支保全（2026-09-27）

- Codex 与 Claude 子代理并行只读复核后，本地 `main` worktree 从 `44fe9a2` 快进到协调分支，包含 P6 / P8 / P11 接线、研究库文档、ADR-0068 Proposed 与项目状态记录。协调分支保留作为恢复点；`origin/main` 未更新，也未创建 PR。
- 这是代码整合，不是 Phase 验收。此次没有运行测试；已有 Python 静态检查和 Web build 结果见 §10.12。E1-CAP-1 仍阻断 Phase 1；其他 Phase 按本文件所列状态继续待验收。
- P0.5 合同复核确认主线已有测试要求“已存在的空目录”返回空知识库；ADR-0034 没有要求知识库必须非空。独立分支 `de146f9` 拒绝空目录的行为不符合主线既有测试，未合入。当前实现对缺失目录报错、对空目录和空列表返回空知识库；空列表行为尚无专门的接受用例。
- 分支审计快照为 27 个本地分支、253 个归档引用和 33 个 worktree。活跃或脏工作区（含根 `phase/1`、Claude 与 Cursor 工作区）、E1 / P0.5 独有实现及远端 refs 均予保留；后续清理须在确认 owner 已结束并逐支确认内容后再做。本地与远端分支统计会随并行会话变化，以实际 Git 状态为准。
- 两项均为设计材料，未修改实现 / frozen contract，未运行测试；不会改变 Phase 7 或 Phase 11 的未验收状态。

### 10.16 当前本地收敛快照（2026-09-27；更新 §10.15 后状态）

- Raphael 已授权 Codex 完成经核实的分支整合与本地代码收敛；Phase 验收和全量测试暂缓。本地 `main@669704c` 比 `origin/main@44fe9a2` 超前 18 个提交、尚未推送；协调分支 `codex/module-completion-coordination-2026-09-27@31717d0` 比本地 `main` 多 4 个提交（P7/P11/P12 实现与文档同步），工作区干净。GitHub PR #1～#9 均已合并。
- 当前 Git 盘点：16 个本地分支、8 个远端分支、22 个 worktree、264 个归档引用。此前已把 11 个确认与主线补丁等价的本地分支和干净 worktree 先归档再移除。根 `phase/1` worktree 有重要未跟踪材料；Claude / Cursor 工作区仍有活动进程或归属未明；这些都保留。
- ADR-0055 Claude worktree `ed8e694` 与远端同 SHA、工作区干净、PID `1111692` 仍存活。直接与本地 `main` 比较后，可执行知识 tags/assets 能力与测试已经在 `main`；该分支仅有两项净差异：将 ADR 状态恢复为 Proposed，以及删除知识库目录缺失时的 fail-closed 检查。两者均不移植；待 Claude 结束后再考虑归档其分支。P0.5 `p05-cite`、研究规格分支的验收用例 / 文档、E1 容量候选仍需逐文件处理。`codex-research-loop-ui` 的图表实现已被 main 更新取代，但 Cursor owner 未确认，暂留。
- 模块判断更新：项目不是空骨架。P6 / P8 / P11 Decimal 阈值逻辑已在 `main@669704c`；P13 模拟执行与 P14 通用迁移框架已存在。协调分支含 P7 参数网格冻结 / outcome 驱动调度改动；ADR-0070 令最后一条 experiment stage FAILED 的 audit 在当前进程和重开后均停止于 `recovery_required`，不重复 trial，但自动 outcome 恢复和人工修复工具仍缺。P11 显式 degradation operation、FreezeRegistry 锚点快照和 schema 1.1.0 evidence writer；P12 `combine()` 冲突拒绝逻辑。ADR-0067 / 0069 / 0070 已 Accepted。新代码的 Ruff、format、mypy、diff whitespace 静态检查已通过，未跑测试；六类 P7 DSL 算子仍 fail closed，逐项业务语义 / Provider lowering 尚未定义；E1 仍未达到 32 MiB 上限。Profile 数值、人工审核数据、真实历史市场结论与实盘能力不由代码猜测。
- 本轮未运行测试或 Phase 验收；不改写任何旧门禁记录。不要清理活跃或 owner 未明的分支 / worktree；对 E1 继续以 500k `resume/replay` 峰值增量为问题目标，不把两个独立读取上界保护误报为容量问题已解决。

### 10.17 ADR-0055 分支净差异复核（2026-09-27）

- Claude 分支 `claude/adr-0055-tags-assets@ed8e694` 基于 `1cd3284`，含 3 个后续提交；其 worktree clean，`origin/claude/adr-0055-tags-assets` 与本地 HEAD 相同，Claude session PID `1111692` 仍在运行。实现说明标为 CODE_COMPLETE / DEBUG_PENDING；该提交点针对性 Python 检查曾报告 3028 passed、1 skipped、32 deselected，Web helper 7/7，但同一说明要求在最终 HEAD 再跑全门禁。
- 用当前本地 `main` 与 `ed8e694` 做直接树比较，可见唯一文件差异是 `docs/adr/0055-knowledge-tags-assets.md` 与 `plugins/knowledge/local.py`：前者把已接受状态改回 Proposed，后者移除了缺失条目目录时报错的检查。Contract、Provider 标签 / 资产过滤、Schema、测试及 Web 页面实现均已在本地 `main`；因此不 cherry-pick 该旧基线分支提交，也不接受它引入的两处回退。
- 不改动仍有活跃进程的 Claude worktree。Claude 明确结束任务后，可归档该 branch，再按 Git 工作流确认是否删除分支 / worktree；当前阶段不将重复实现视为待合并代码。

### 10.18 P0.5 本地分支归档与 E1 续作（2026-09-27）

- 逐文件复核确认本地 `p05-cite@f375e95` 的种子已与主线一致；旧审计报告有历史价值但状态过时；其 provider / ADR / 状态文件会回退当前语义。未采纳测试候选（统一留待验收）；将完整 branch tip 保存到 `refs/archive/2026-09-27/branches/p05-cite` 后，移除干净的本地 `p05-cite` worktree / branch。远端 `origin/wip/phase-0.5-knowledge` 暂保留，避免删掉唯一远端审计证据。研究规格分支的唯一 C-L5 验收用例也保留在本地分支，测试后置。
- 本地快照现为 16 个分支、8 个远端分支、22 个 worktree、265 个 archive refs；根 `phase/1` 的 `.codex/` / `docs/plans/` 未跟踪内容保持不动。坐标分支 `codex/module-completion-coordination-2026-09-27` 相对 `main@669704c` 含本轮集成提交；代码仍未合并进 `main` 或推送。
- E1 复核确认四个 Codex 候选均未证明 500k `resume/replay` 峰值增量低于 32 MiB。协调分支代码比集成候选旧；当前候选的全量快照历史仍 O(H)，并且 `scan_columns()` 也会经 `load_table()` 装载元数据。文档将 PyIceberg snapshots 标为候选原因而非已隔离的唯一根因。Claude 已在独立 `codex/e1-single-load-history-2026-09-27` worktree 开始单次 history-load 优化；范围不含流式 metadata、不会关闭容量门、不加测试/探针、不推送。提交后由 Codex 复核，随后仍需重新设计/测量满足 E1 的有界方案。
- P7 只读复核确认六类 DSL 尚无端到端 typed plan → Provider lowering → 批次预登记 / 持久化恢复路径。最小 negation 控制组候选需先补 ADR 语义与 lineage / resolver 协议，不直接编码；现有 fail closed 保持。

### 10.19 E1 history 优化审阅与本地整合（2026-09-27）

- Claude 在隔离分支实现 `PyIcebergCatalogAdapter.history()`，让 `row_integrity.history_from()` 及 `PinnedCatalogView` 优先用同一份 table metadata 沿 parent snapshot 遍历；其他 `RevisionCatalog` 保留逐快照 fallback。首版 O(H) ID 索引被 Codex 审阅退回；第二版改为线性 first-match 查找和 Floyd 常数空间环检测，修订两个本地提交后 cherry-pick 进入协调分支：`c7bbcfe`、`8261f32`。源 branch tip `5870c62` 保存到 `refs/archive/2026-09-27/branches/codex/e1-single-load-history-2026-09-27`，干净 worktree / local branch 已移除。
- 最终代码的独立静态检查原样结果：`ruff check` → `All checks passed!`；`ruff format --check` → `3 files already formatted`；`mypy` → `Success: no issues found in 3 source files`；未添加或运行 pytest / PostgreSQL 测试 / 容量探针。
- 该更改减少 H 次 metadata load 为一次 history metadata load，并避免新建 O(H) 索引；代价是每一步在线性 snapshot 列表中查找，history 遍历时间仍 O(L·H)。PyIceberg 仍一次加载整张 `metadata.snapshots`；主线的 normalizer 批次字典、其他 scan 的 table metadata 装载也未解决。因此这只是待验收的性能批次，**不关闭、不解决** 500k `resume/replay` 59.9 / 63.9 MiB 对 32 MiB 的 E1-CAP-1 阻断。
- 当前本地盘点：15 个本地分支、8 个远端分支、21 个 worktree、266 个 archive refs。协调分支 `codex/module-completion-coordination-2026-09-27` 相对 `main@669704c` 超前 9 个提交；不 push。P0.5 `origin/wip/phase-0.5-knowledge` 仍保留远端唯一旧审计内容；活动 / owner 未确认 worktree 继续保留。

### 10.20 E1 normalizer 已提交计划常数级化（2026-09-27；本地协调分支，未验收）

- Claude 在隔离 worktree `codex/e1-normalizer-bounded-2026-09-27` 实现，Codex 复核后 cherry-pick 到协调分支，提交 `6ab7f5b`。`_CommittedPlan` 不再留存每个 batch 的 `SnapshotInfo` 映射；只保留 unit 大小、chunk 大小与 committed count。证明和 `ALREADY_COMMITTED` 报告路径从 pinned history 流式重读目标快照，并比较证明阶段摘要；写入器按已提交 batch 数补齐。完整实现与语义说明见 [`e1-normalizer-bounded-implementation.md`](../reviews/e1-normalizer-bounded-implementation.md)。
- 顺序校验要求 committed batch 为从 0 开始、按写入顺序的连续前缀。唯一 normalizer writer 以递增 index 在 expected parent 上提交；乱序历史 fail closed。尚未通过测试覆盖该约束，需在后续验收确认与历史数据兼容。
- 独立静态复核原样结果：目标文件 `ruff check` → `All checks passed!`；`ruff format --check` → `1 file already formatted`；`mypy` → `Success: no issues found in 1 source file`；`git show --check` → 通过。没有添加或运行测试、PostgreSQL 测试或容量探针。
- Codex 又做了一轮独立只读算法审查，未发现可具体触发的正确性回归；核对了连续前缀 / 重复检测、部分 batch 恢复、冻结视图缓存、快照二次遍历、digest 及按序补写。后续定向覆盖仍需包含 maintenance snapshot 混入、不同 chunk 与尾批、补写顺序、frozen cache 淘汰、两次 history 遍历差异时 fail closed、digest 字段变化。该审查不能替代测试或容量验证。
- **E1-CAP-1 仍阻断，32 MiB 门槛未证明。** 本次只移除 normalizer 对每 batch 快照映射和 `done` 字典的保存；positions、返回 revision ids、PyIceberg table metadata 全量 snapshots 等仍随数据规模增长。pinned history 现在会被重复流式遍历，单次遍历仍可能 O(L·H)，暂未计时。不得将本次 static pass 写成 E1 修复验收或容量通过。

### 10.21 ADR-0055 已完成本地 worktree 清理（2026-09-27）

- Claude 的 `claude/adr-0055-tags-assets@ed8e694` 会话已结束、worktree clean；能力与测试已在 `main`，该分支的两项净差异会回退 Accepted 状态和缺目录 fail-closed 行为，故不移植。先保存 tip 到 `refs/archive/2026-09-27/branches/claude/adr-0055-tags-assets`，再移除本地 worktree 和分支。远端同名分支保留。
- 本地盘点变为 15 个分支、8 个远端 refs、21 个 worktree；协调分支 `codex/module-completion-coordination-2026-09-27` 相对本地 `main@669704c` 多 12 个提交，未推送。根 `phase/1` 的未跟踪材料与 owner 未确认的 Claude / Cursor worktree 保持原样。

### 10.22 P10 已合入本地主线的旧 Claude worktree 清理（2026-09-27）

- `claude/p10-inverse-control@20bdd82` 的 worktree clean；Claude session 已结束。`git merge-base` 核实该 tip 是本地 `main@669704c` 的祖先（无仅存在于该分支的提交），故先归档到 `refs/archive/2026-09-27/branches/claude/p10-inverse-control`，再移除本地 worktree 与分支。
- 本地清理后盘点为 14 个分支、8 个远端 refs、20 个 worktree；归档引用增加 1。远端 `main` 未变，Claude / Cursor 活动或 owner 未确认的工作区仍保留。

### 10.23 P7 typed operator 基础边界决定（2026-09-27）

- Codex 依 Raphael 的项目全权委托接受 [ADR-0068](../adr/0068-phase7-typed-operator-plans.md) 的闭世界类型化计划机制与拒绝规则。六类 DSL 算子仍全部 `NOT_RUNNABLE`；不改 `core/`、Schema、Provider contracts、`ExperimentSpec`、`parameter_point` 或 trial 计数。
- 本批只实现纯 research 层 typed AST / 严格解析与 validation，结果显式为 `non-runnable`；allowlist 保持空，不产生执行审计、持久化或 trial。执行计划审计存储和与 TrialLedger 的崩溃原子性，必须在首个 operator 语义决议前另行决定并实现。此项把安全基础代码从尚未定义的研究行为中隔离出来。
- P7 六类 operator 的逐项时间、缺失值 / 对齐、算法与 Provider lowering 语义仍需单独决定；当前代码不把自然语言 Hypothesis 转为执行计划。

### 10.24 P11 provenance 页面与 E1 容量范围（2026-09-27）

- P11 degradation provenance 页面已由独立 worktree 完成并 cherry-pick 到协调分支，提交 `99fe9fe`。页面读取 ADR-0067 schema 1.1.0 的 profile freeze、baseline、生命周期哈希、观测窗口 / 方法与完整 manifest；明确标注 caller-declared/content-bound、不认证来源真实性；未知 evidence 字段以转义文本完整披露；legacy 1.0.0 展示保持兼容。实现记录：[`p11-degradation-provenance-ui.md`](../reviews/p11-degradation-provenance-ui.md)。子代理 `git diff --check` 通过；本轮未运行测试或 build，未做 Phase 验收。
- Codex 依 Raphael 项目委托正式决定 **D-E1-HIST = FULL_PROCESS_WORKSET**：原 E1-CAP-1 的 32 MiB 峰值增量包含 PyIceberg metadata、Parser / scan 临时状态、normalizer 状态和 API 返回对象。拒绝 `e1-bounded-history-options.md` 提出的选项 A，不将依赖内部状态排除出容量目标，也不修改既有关闭标准；选项 B / C 未获批准。E1 持续阻断，直到完整工作集满足门槛或形成另行批准的架构决定。
- P9 detector 单次异常归类仍由隔离子任务处理。Knowledge Search 缺少 Provider 的稳定错误响应经复核已在当前基线实现（503 + 常量错误 detail，OpenAPI / README 同步），无需新代码；全项目缺口审计中的该项属基线版本过时。P2 报告 API DTO / OpenAPI 类型不一致另由隔离子任务审查，避免将 opaque dict 当作已修复。本批未运行测试 / build。

### 10.25 P7 typed plan 解析器与缺口核对（2026-09-27）

- 在 Accepted ADR-0068 边界内新增 `research/hypotheses/typed_plan.py`（协调提交 `23b4256`）。严格解析显式限额的 JSON AST、拒绝重复 / 未知键、类型错误、越界结构与引用错误；计算结构内容哈希；`compile_plan()` 对六类算子一律拒绝。没有 Provider、Registry、TrialLedger、持久化、loop 或 runnable allowlist 接入。实现说明：[`p7-typed-plan-foundation-implementation.md`](../reviews/p7-typed-plan-foundation-implementation.md)。Ruff / format / mypy / diff 检查通过，未跑测试或 build。
- P9 backlog 曾称 detector 异常会终止校准。本基线已在单标的、可选 G5、多标的入口按既有可传播错误边界把检测错误记录为 `INCONCLUSIVE` 并继续其余运行；该旧 backlog 项为过期描述。复核记录：[`2026-09-27-p9-detector-exception-handling.md`](../reviews/2026-09-27-p9-detector-exception-handling.md)。子代理的 Ruff / format / mypy / diff 检查通过，未跑测试。
- Knowledge API 的无 Provider 响应已在基线实现为稳定 503 常量错误，README 与 OpenAPI 一致；无代码改动。P2 report DTO 审查认为现有 report kind 缺乏统一版本化 schema，任意新增严格模型会拒收既有 payload；逐 kind DTO 延后到 schemas 经批准后实施。当前 API envelope 仍按动态 report kind 返回 opaque JSON object。
- 已吸收的 P7 / P11 / P9 Codex worktree 先将 branch tip 归档到 `refs/archive/2026-09-27/branches/codex/` 再移除；P2 无改动审计 worktree 与已成为协调分支祖先的旧 Knowledge API worktree 同样按此方式清理。归档保留全部提交，未碰 Claude / Cursor 的活动 worktree；本地快照回到 14 个分支、20 个 worktree，主线与远端未更新。

### 10.26 Cursor 分支审计与 E1 候选复核（2026-09-27）

- Cursor `codex-d3e-pit-tests@60246a5` 的两个提交 patch-id 分别等同于已在协调分支和本地 main 中的 `69f0bf0` / `9baad12`，无独有代码；干净 worktree 的完整 branch tip 先归档到 `refs/archive/2026-09-27/branches/codex-d3e-pit-tests`，再移除本地分支 / worktree。
- Cursor `codex-research-loop-ui@e398386` 有独有 commit，但增加的 parser / 测试夹具 / 页面行为已由更完整的主线实现覆盖，整体摘取会重复逻辑并可能丢掉当前 `InvalidReports`、AsyncView 与报告行为。已归档 branch tip 到 `refs/archive/2026-09-27/branches/codex-research-loop-ui` 后移除本地 worktree / 分支；`origin/codex-research-loop-ui` 保留。
- E1 分支审计确认 `codex/e1-bounded-scan-integration` 是两组尚未进入协调线的 bounded-scan / source-proof 代码累计候选，但没有 PyIceberg 有界 metadata 解析，也无 32 MiB 证据；`fix/e1-cap1` 有另一套实现且仍报告 59.9 / 63.9 MiB 超门槛。候选彼此重叠，当前不整支合并；所有相关 worktree 均干净，活动 Claude history-options worktree 保留。
- 当前仓库为 **12 个本地分支、18 个 worktree**；`main` 与远端未变。其余 E1 候选保留等待独立代码审查与方案收敛。

### 10.13 分支收敛与研究库规格补全（2026-09-27；本地协调分支）

Raphael 授权 Codex 整合有价值的代码和内容、清理冗余分支，并在后续统一验收。本轮仍保持 `main` / `origin/main` 基线 `44fe9a2` 不变；所有代码先进入 `codex/module-completion-coordination-2026-09-27`，状态为未推送、未验收。归档删除 `worktree-fix-e1-cap1` 并在择取内容后归档移除 Codex P7 / P11 设计 worktree 后，当前快照为 **27 个本地分支、33 个 worktree**；主项目、Claude 脏目录、已锁定 / 会话归属不明的工作树均保留。

| 分支组 | 核对结果 | 处理 |
|---|---|---|
| P6 / P8 / P11 | P6 报告接线、P8 显式回溯审计报告链、P11 Decimal 阈值修正均已进入本地协调分支；独立复核发现 P6 回调失败会被误归因为候选错误，已改为向上抛出并做静态检查 | 只保留协调分支作为后续主候选，避免重复并入三个源分支；P8 页面验收注册仍待后续补齐 |
| 研究库规格文档 | `docs/research-spec-completion` 含有效的 Feature / Factor / State / Event / Outcome / Strategy / Risk 内容与 capability map；主线已有的 seed 文件不可丢 | 按文件吸收 10 份研究库 / README 内容到协调分支；修正 capability 基线和 P7 执行状态；跳过该源分支的 `knowledge-base.md`、`roadmap.md`、测试改动和删除 seed 的操作；`signals.py` 精度修复已在主线，不重复搬运 |
| Research Loop UI | 源分支直接取代现有解析器、坏报告告警和累计用量信息，且没有保留更完整的主线页面行为 | 判断为被主线实现取代，不合并代码；来源 worktree 会话归属未确认，暂不删除 |
| P7 参数点批次 | 该能力与代码已在主线；旧 agent 分支比主线少近期 loop / persistence 逻辑 | 不整支合并；保留或归档前继续按等价补丁和会话状态清理 |
| E1-CAP-1 | Codex 候选的 source revision 与 archive metadata 两项修复在 `fix/e1-cap1` 中均有等价 patch；该分支还带失败诊断。500k `resume` / `replay` 增长 59.9 / 63.9 MiB，均超过 32 MiB 上限；只读代码调查将主要增长来源定位到精确历史核验加载的 Iceberg 全量 snapshot metadata（未重跑探针） | `REVIEW_PENDING`；不把多个历史分叉整支并入 main。需要设计精确且有界的历史索引 / 读取方案，再用固定规模探针验证，保留原失败证据 |
| P0.5 / ADR-0055 | 主线已有 `seed-2026-09-26.json`；`p05-cite` 后续树与其源提交会删文件或覆盖其他更新。`claude/adr-0055-tags-assets` 会把主线 Accepted 状态改回 Proposed | 不整支合并；已有研究库内容按审核后的文件迁移；具名标签 / 资产审核仍待人工 |
| P2 旧 Proposed ADR-0067 草案（编号冲突） | Arrow `state_table` 满足 ADR-0035 当前范围，Iceberg 持久化是可选扩展 | 这是 2026-09-27 归档的旧 P2 草案，和当前已 Accepted 的 P11 ADR-0067 无关；草案存于 `refs/archive/2026-09-27/branches/codex/adr-0067-state-persistence`，不进入本次整合 |

此前 Codex 本轮创建的 P6 / P8 / P11 源分支（`codex/p6-matrix-report-wiring-2026-09-27`、`codex/retro-audit-report-chain-2026-09-27`、`codex/degradation-exact-thresholds`）及 ADR-0067 草案分支，已先归档再移除分支和干净源 worktree；恢复点在 `refs/archive/2026-09-27/branches/`。没有删除任何脏目录或已锁定的 Claude / Cursor worktree。剩余冗余分支只在完成会话确认后继续清理。

新增提交 `e6b06e3` 修复 P6 回调错误归因；`b554454` 将研究规格文档补全并保留 Event 未登记草稿。静态检查：协调分支的 3 个触及 Python 文件 Ruff 与格式检查通过，`git diff --check` 通过；本轮未运行测试。以上不表示模块或 Phase 验收完成。

分支审计确认当前只有一个可安全立即归档的历史本地冗余引用：`worktree-fix-e1-cap1`，其 `c5dbebe` 已包含在 `fix/e1-cap1`，无 worktree；已备份后删除。P7 / P11 设计提交中的唯一 ADR / plan 内容已逐文件并入协调分支，且 blob 完全一致，两个干净的 Codex 设计 worktree 与分支 tip 随后也归档移除。审计同时发现 `origin/codex/full-code-review-2026-09-26` 的全代码审查文件比 main 同名文件多 239 行追加历史记录，已按文件择取到本协调分支；它保留 ADR-0052 授权 / 复核来源及 K5 决策演变。当前 ADR-0052 / ADR-0057 已在主线记录其有效决定，ADR-0055 与 D-DEG-IE 决策文件也已存在于主线。远端分支不整支合并；其余 patch-equivalent 分支仍有活跃 Claude / Cursor 会话，不因未锁定或内容重复而删除。
