# HLENS-AutoResearch Project Memory

> 给 Claude 的长期项目记忆：只保存跨会话仍然有效的事实。
> 维护规则见 `CLAUDE.md` §8（目标 < 200 行，> 300 行必须 Compaction）。
> 当前进度看 `PROJECT_STATUS.md`；完整架构看 `docs/architecture/`；决定全文看 `docs/adr/`。
> 2026-09-24 Phase 0 收口时执行 Compaction：各 ADR 只保留 ID + 一句话，细节见 ADR 与 `docs/reviews/`。

## 1. Project Identity

- 名称：HLENS-AutoResearch
- 定位：长期演化、模块化、可插拔、可验证的加密市场自动化研究基础设施（不是交易机器人或回测框架）
- 核心目标：持续吸收公开知识、已有策略和失败经验，通过组合与实验验证产生、检验新假设
- 主要研究对象：BTCUSDT（D-09 提案中的参考标的；正式市场范围待 D-08）
- 主要时间周期：1H（同上，待 D-08 确认）
- 当前阶段：Phase 0 已完成（tag `phase-0-complete`）；**Phase 1 已开启**（2026-09-24，分支 `phase/1`），仅架构决策子阶段

## 2. Current Architecture

- 工程基线：Python 3.13 + uv；契约用 Pydantic 写在 `core/`，JSON Schema 导出到 `schemas/` 并随仓库提交
- 契约版本 `CONTRACT_SCHEMA_VERSION = 2.0.0`：随 Phase 0 收口 fast-forward 合并进 `main` 并打 tag，
  **视为已发布**（D-25）——此后任何破坏性契约变化都必须升 major 并走 ADR；仍无远程、无 v2 数据登记
- 模型只接受同 major；`1.x` 走 `core/compat/v1.py` 只读入口（`schemas/v1/` 35 份快照 + `tests/vectors/v1/`）；
  v1 与 v2 的 `content_hash` / `experiment_hash` 不可比较；读取 v1 不赋予任何 v2 登记 / 晋升资格
- current Schema 38 份，与 `CONTRACT_MODELS` 一一对应；Provider Protocol 0 个（ADR-0017 的决定，不是遗漏）
- Freeze Contracts, Evolve Implementations；四个 Plane：Data / Research / Control / Application；Research ⟂ Application
- PostgreSQL = Control Plane（不存大型行情）；Iceberg / Parquet on S3 兼容存储 = 真实来源；DuckDB / Polars 只是计算引擎
- Provider / Plugin 架构；LLM、Backtest Engine 均可替换；LLM 只产出数据，永不作裁判
- Domain 层无基础设施依赖；依赖方向 apps → application → domain ← plugins/infrastructure
- 实验必须可复现（复现元组）；Schema 全部版本化
- 全文：`docs/architecture/00-overview.md`

## 3. Current Research Direction

Market State → Feature / Event → Knowledge Retrieval → Hypothesis → Combination → Experiment → Validation → Research Memory → Strategy Evolution

- 新颖性主要来自确定性的组合 / 条件化 / 时序算子，并且每次组合都计入尝试次数
- 路线图：`docs/research/roadmap.md`

## 4. Current Phase

- Current Phase：Phase 1（Market Representation）**已开启**——Codex 依 Raphael 持续授权于 2026-09-24 开启（S0）
- Current Subphase：**架构决策**；只允许 docs-only 起草 / 复核 ADR-0021（D-01 / D-02 / D-10）、ADR-0022（D-08）、
  ADR-0023（D-28）、ADR-0024（D-31，依赖 0023）；四份接受前不实现 Collector / Provider / Iceberg / 数据库 / 网络 / 下载 / 依赖安装
- Current Objective：A1 起草 ADR-0021 ~ 0024 为 Proposed → Codex 复核接受
- Current Blocker：无（授权已记录）；Docker 未安装会影响 D-02 的可选方案
- Next Milestone：ADR-0021 ~ 0024 Accepted；首个 Provider 先交付 Protocol + DTO + contract tests（ADR-0017）

## 5. Active Decisions

- ADR-0001：重大架构决定用 ADR 记录；Agent 只能起草 Proposed
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- ADR-0003（D-06）：Python 3.13 + uv，与系统 Python 隔离
- ADR-0004（D-07）：本地 Git 仓库；不改全局配置；远程未定
- ADR-0005（D-03）：研究 / 生产边界 = Artifact + Registry + Promotion + Equivalence Gate；Q-1 / Q-2 / Q-3 / Q-7 开放
- ADR-0006（D-05）：生命周期 v2（C-1：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE；C-2：ACTIVE 带
  `execution_mode` SIMULATED|LIVE，不设 LIVE 状态）；RETIRED 进退役记录，REJECTED / FAILED 进 Failure Registry；Q-4 ~ Q-6 开放
- ADR-0007（D-09 结构）：三层验证（Constitution / Validation Profile / Experiment Metadata）+ 两步冻结；
  Constitution 曾以 `0.2.0-draft` 按此重组为纯原则，现已由 ADR-0020 发布为 `1.0.0 / Approved`，原则与阈值零变化
- D-09 数值（TBD-1 ~ TBD-5）与 H-3 ~ H-7：**未批准**，Phase 4 校准后冻结为 Profile 参数；Constitution 中不得出现数值阈值
- ADR-0008（D-11）：映射字段只读、逐模型内容哈希排除表、规范化 JSON、v1 只读兼容
- ADR-0009（D-12）：完整实验身份与直接依赖内容绑定；实际 seeds 在实验哈希内；Report 绑定 run_id
- ADR-0010（D-13 ~ D-16）：`model_copy(update=...)` 完整校验、唯一 ASCII SemVer、v1 顶层 shape gate、Schema 表达键值格式
- ADR-0011（D-17）：生命周期主体一致、时间单调、LIVE 证据绑定主体与授权窗口；删除自报 `live_execution_enabled`
- ADR-0012（D-23）：具体规格 `kind` 为不可覆盖字面量；Feature / State / Event / Strategy 输入白名单，Outcome 不得进入
- ADR-0013（D-19、D-20.1 ~ 20.3）：`verdict` 精确等于 `derive_verdict(gates)`；`gate_id` 唯一；全局拒绝 NaN / ±Inf
- ADR-0014（D-20.4）：Validation Profile 普适结构不变量（窗口 / 封存 / 观察期为正等），不选数值
- ADR-0015（D-21、D-22）：`ContentHash` / `GitOid` / `GitCodeRevision` 分类型；Profile 绑定 = `Ref(kind=profile)` + 哈希
- ADR-0016（D-18）：`LlmCall` 三项必填 `ContentBlobRef` + 显式 `called_at`；取回与一致性延期
- ADR-0017（D-24）：Provider 方案 B——Phase 0 只冻结语义，Protocol / DTO / contract tests 随首次消费的 Phase 交付
- D-25：上述收窄属未发布的 2.0.0；**Phase 0 收口后 2.0.0 已发布，同类改变必须升 major**
- ADR-0018（D-26）：三类语义身份（选择键、`Ref` 目标、Git 代码修订）排除信封版本；全局 `==` 与内容哈希不变
- ADR-0019（D-27）：生命周期转移证据至少一项且非空；不做自报职责分离
- ADR-0020：Constitution 发布为 `1.0.0 / Approved`，第一至第九章正文 sha256 `4d603d62…259cd` 不变，只前向适用
- 开放问题（已登记，ADR 接受前仍未关闭）：D-28 修订数据语义 → ADR-0023、D-31 历史标的池 → ADR-0024；
  D-30 C-L5 embargo ↔ horizon 校验点（Phase 4 前）；D-29 worker ↔ research 边界（最迟 Phase 5 前）；
  D-01 / D-02 / D-10 → ADR-0021、D-08 → ADR-0022；D-04（Phase 4）
- Raphael 授权（2026-09-24）："授权所有"，Codex 全权接管决策 / 开发 / 测试 / 文档 / Git；Codex 解释为覆盖原则零变化的
  Constitution 1.0.0 发布与 Phase 0 收口（closure、`main` fast-forward、轻量 tag）；原则或阈值变化、实盘、资金、风险预算不在内

## 6. Active Constraints

- 硬性规则全文见 `CLAUDE.md` §3（H1–H14），摘要如下：
- 研究代码永不直接成为生产代码；LLM 不作最终裁决
- 不因回测结果修改 Constitution、Profile 或验证规则；Constitution 修改须按第九章另起 ADR 并由 Raphael 批准具体变化
- Outcome 不得作为 Feature / State / Event / Strategy 的输入
- 所有实验可复现；所有 Schema 版本化；失败实验与生命周期历史不可删除
- 环境变更（安装、系统配置、Docker、数据库、全局 Git 配置）需 Raphael 授权
- 不修改旧项目与外部数据
- Claude 不替 Raphael 做架构决策；Codex 在 Raphael 委托边界内作正式决定并记录（CLAUDE.md §0）
- Git：main 为稳定基线，实现工作走 `phase/*` 分支；合并进 main 需 Raphael 批准（或其已记录的授权）；无远程仓库
- 实盘、资金、风险预算始终需要 Raphael 亲自批准

## 7. Current Known Risks

- pypi.org 索引域名在本机被阻断（files.pythonhosted.org 可达）：离线安装需用 uv.lock 的精确版本
- WSL 内存约 15 GiB：大数据集需分区和流式处理
- Docker 未安装：Phase 1 之后的本地服务依赖它（D-02）
- 外部数据盘未挂载：`~/BTC` → `/mnt/wsl/PHYSICALDRIVE1p1/BTC` 当前不可访问
- Git 没有全局提交身份：提交使用一次性 `-c` 参数；无远程，无异地备份、无 PR / CI
- Constitution 1.0.0 只是原则：验证流水线、泄漏门、多重检验校正、trial 账本均未实现，Profile 数值要到 Phase 4；
  在那之前没有实验能被实际判定
- 契约层只校验**结构与声明**：传递依赖闭包、trial 权威账本、`run.repro` ↔ Spec 一致性、Registry 存在性、
  物化数据泄漏检测、哈希与真实内容一致、Git 对象存在、Profile 已 frozen、跨对象绑定一致，
  都是未实现的 Runner / Registry / Control Plane / 验证服务义务（06-experiment.md §7），不得宣称已防住
- 判定只保证**报告内部**自洽：门集合完整性、`threshold_source` 真实性、`value` 由 `metric` 算出，属未来验证服务
- `LlmCall` 只保证**登记结构**：内容可取回、内容与 `sha256` 一致、不可覆盖、外发合规、调用登记完整均未实现；
  06-experiment.md §2 的「完整输入输出」不得描述为已满足
- 生命周期证据只保证结构非空：存在性、支持结论与否、`approved_by` 真实性与职责分离属未来授权服务
- 契约层不再拒绝 `to_mode = LIVE`：Phase 13 红线在 Control Plane 落地前只靠人与流程
- JSON Schema 弱于运行时的几处：首尾空白去除（02-domain.md §3.7）、时长符号与 `cost_model.kind`
  （07-validation.md §5.4）、只由跨字段相等约束的 `declared_research_class`；权威校验必须经过运行时模型
- `venue` / `symbol` / `timeframe` 区分大小写、不做规范化（ADR-0018 边界）：未来 Adapter 必须产出规范值
- v1 只读 gate 只做顶层形状检查；旧哈希只对完整的 v1 持久化规范载荷复现历史身份（C1 F2）
- 外部是否存在 v1 历史数据证据不足：不得宣称迁移路径已在真实数据上验证
- C3 关闭复验与 C2c / C2d 实现出自同一 Claude 会话，独立性有限；以 Codex 复核为最终把关

## 8. Important Historical Context

- 旧研究项目 `alpha-autoquant` 位于 `/mnt/e/alpha-autoquant`（只读参考，不得修改）
- 现有系统 `hlens-cryptoplus` 位于 `/mnt/e/hlens-cryptoplus`（只读参考，不得修改）
- 旧研究中的 anti-leakage / red-team 规范可能成为新系统素材（Raphael 提及，尚未评估内容）
- 旧研究可能已看过 BTC 全部历史，因此历史封存区在认知上不完全干净（D-09 H-3）
- 旧策略或旧结论进入新系统时必须重新登记并重新验证，不能直接信任
- Phase 0 审查链：C1（`fce4f81`，`FIX_BEFORE_CLOSE`）→ ADR-0018 / 0019 实施 → C3（`4a2951a`，
  `READY_FOR_HUMAN_CONSTITUTION_GATE`）→ ADR-0020 发布 Constitution 1.0.0；记录见 `docs/reviews/`

## 9. Last Known Good State

- Date：2026-09-24
- Stable recovery point：**轻量 tag `phase-0-complete`**（指向 Phase 0 closure commit，即 `main` 与 `phase/0`
  的共同 HEAD；以 `git rev-parse phase-0-complete` 为准，本文件不写该提交自身的 SHA）
- closure commit 的父提交：`3257e6e`（ADR-0020 / Constitution 1.0.0，Codex 已复核）；
  其前：`4a2951a`（ADR-0019，C3 复验）、`9581773`（ADR-0018）、`1ad9f59`（ADR-0016，Codex 独立复验）
- State：契约、状态机、只读载荷、实验身份、版本语法、生命周期主体 / 授权 / 证据、信息流白名单、确定性判定、
  Profile 结构不变量、审计身份、`LlmCall` 登记、语义身份、v1 只读兼容均已实现；
  1433 测试、ruff check、ruff format --check、mypy strict 全绿；
  Schema current 38 份（2.0.0）逐字节一致 + legacy 35 份（`schemas/v1/`，1.0.0，逐字节不变）；
  Constitution `1.0.0 / Approved`；ADR-0001 ~ 0020 全部 Accepted
- 未实现（按 roadmap 延期）：Provider Protocol、Feature / Strategy / Backtest、Runner、Registry、存储、Control Plane
- Git：`phase/0` 保留；`main` 由 `2e2a0ad` fast-forward 到 closure commit `1e208b5`（= `phase-0-complete`）；
  `phase/1` 从该 commit 创建（Phase 1 工作分支）；无远程、未 push
