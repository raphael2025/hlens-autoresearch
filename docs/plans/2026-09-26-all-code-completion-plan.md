# HLENS-AutoResearch 全栈代码完成计划

日期：2026-09-26  
用途：交给 Claude Code Opus 作为执行目标；Claude 负责编码与每步文档 / Git，Codex 负责决策、协调、复核和恢复点把关。  
计划依据：`docs/research/roadmap.md`、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`docs/reviews/2026-09-25-framework-debug-backlog.md`、Phase 1 close evidence、当前候选分支。

## 1. 要完成什么

把路线图中**已批准范围内**的后端、研究引擎、数据管线、API、Worker 与 Web 前端代码补齐到可交付的实现状态，并为每个功能提供相应测试和文档。实现与验证分开记录：Claude 可以先完成功能代码和有针对性的单元 / 合同测试；后续由 Cursor Auto 承担独立调试、对抗测试和全量验证。任何功能在全量验收前都必须标为 `FRAMEWORK_IMPLEMENTED / NOT_VALIDATED` 或 `REVIEW_PENDING`，不得把“代码已写”说成“阶段已完成”。

这里的“全部代码”指路线图和已接受 ADR 明确批准的代码范围，不包括未经批准的市场交易能力，也不授权猜测验证阈值。Phase 13 维持模拟 / 纸面边界；不添加交易所账户、下单端点或密钥。Phase 14 的迁移目标未指定时只保留既有抽象，不臆造新基础设施迁移。

## 2. 当前基线和第一优先级

- 正式工作目录 `/home/raphael/projects/hlens-autoresearch` 当前为 `phase/1`，HEAD `52f7477`；它尚未包含全部框架代码。
- 全框架候选在 `/home/raphael/projects/hlens-autoresearch/.claude/worktrees/hlens-autorecearch-dev-c05c2b`，分支 `claude/hlens-autorecearch-dev-c05c2b`，审阅基线 HEAD `50a43a4`。新会话必须先检查分支和 worktree；不得直接在 `main` 或正式 `phase/1` 上实现。
- Phase 0.5、Phase 2–14、apps 已有大量框架代码，但状态仍是 `FRAMEWORK_IMPLEMENTED / NOT_VALIDATED`。Phase 1 的 D3E 起和后续批次仍待 Codex 验收。
- 最新记录的全量门禁是 `1d4fe0e`：5,718 passed、1 warning、45:58。它不覆盖之后提交的 D-NET 能力工具和后续文档，也不覆盖本计划发现的跨日错误。
- **阻断修复**：D3E-R3 在 `infrastructure/revision/channel_reconcile.py::_verify_edge_provenance` 中将当前日期的 batch prefix 与跨日同键证据混为一谈。aggTrade observation key 不含日期；同一键可在 UTC 午夜两侧有 REST revision。按两种到达顺序都能复现合法跨日 evidence 被误报为篡改，之后两个日期的 reconcile、`verified_edges` 和 PIT 路径会失败。边表 append-only，不能靠删除记录恢复。保持完整性校验强度，先补跨日回归再修实现。
- D3E 的容量探针此前只有 1,000 条、一个 evidence batch。现有 provenance 扫描按 evidence-table snapshot 历史逐个回读，必须测多批和无关历史增长；这是待量化风险，不能把小样本结果外推为生产容量证明。
- D-NET 实测只跑到 F2 标的池停止，未调用 `exchangeInfo`，未形成市场结论。离线工具测试只有 2 项；工具需补旧状态防误读、运行参数 / 日期绑定、快照与代码版本记录，以及对非采集步骤的非空网络拒绝断言。

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

以下是 Claude 的实施清单。每一项开始时先用当前代码和测试核实状态；已实现的功能不得重复造轮子，应补缺、接口集成或测试，然后更新本表的状态与证据。

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
