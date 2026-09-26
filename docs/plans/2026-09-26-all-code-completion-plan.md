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
| P3-MULTISYM | `EventRequest`（`core/contracts/event.py`）无标的字段，多标的只能逐序列请求；加字段 = 改冻结契约 | `core/contracts/event.py`、Schema | A：additive 可选字段 + ADR（须证明省略时哈希不变）；B：维持每序列一个请求，由调用方组合 | B（现状可用，另立 ADR 再议） | 多标的事件需多次请求 |
| P5-PLUGIN | 计划记"尚无 `plugins/` 生产 StrategyProvider"：TSMOM 在 `research/`，按 H5 研究代码只能经 Promotion 流程成为生产代码；把它搬进 `plugins/` 即是晋升 | H5、ADR-0005、ADR-0038 | A：等某策略经完整验证 + Promotion；B：另立 ADR 定义"研究库 Provider 进入 plugins 的最小晋升证据" | A | 策略只在研究层；不影响研究与验证链路 |
| P10-ELIG | 路由资格信任调用方给出的生命周期映射；把资格绑定到验证报告证据若需改契约则须 ADR（engines 通道在不改契约的前提下尽量加可选核对） | ADR-0043、`core/contracts` | 见 engines 通道报告 | — | 路由仍只接受调用方声明为已验证的对象 |

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
