# HLENS-AutoResearch Project Memory

> 给 Claude 的长期项目记忆：只保存跨会话仍然有效的事实。
> 维护规则见 `CLAUDE.md` §8（目标 < 200 行，> 300 行必须 Compaction）。
> 当前进度看 `PROJECT_STATUS.md`；完整架构看 `docs/architecture/`；决定全文看 `docs/adr/`。

## 1. Project Identity

- 名称：HLENS-AutoResearch
- 定位：长期演化、模块化、可插拔、可验证的加密市场自动化研究基础设施（不是交易机器人或回测框架）
- 核心目标：持续吸收公开知识、已有策略和失败经验，通过组合与实验验证产生、检验新假设
- 主要研究对象：BTCUSDT（D-09 提案中的参考标的；正式市场范围待 D-08）
- 主要时间周期：1H（同上，待 D-08 确认）
- 当前阶段：Phase 0（Research Constitution）；契约修复 B1/B2 已实施，等待验收与关闭复审

## 2. Current Architecture

- 工程基线：Python 3.13 + uv；契约用 Pydantic 写在 `core/`，JSON Schema 导出到 `schemas/` 并随仓库提交
- 契约版本 `CONTRACT_SCHEMA_VERSION = 2.0.0`（**尚未发布**：未合并 main、无 tag、无远程、无 v2 数据登记）；
  模型只接受同 major，`1.x` 走 `core/compat/v1.py` 只读入口
- Freeze Contracts, Evolve Implementations
- 四个 Plane：Data / Research / Control / Application；Research ⟂ Application
- PostgreSQL = Control Plane（不存大型行情）
- Iceberg / Parquet on S3 兼容存储 = Data / Research Plane 的真实来源
- DuckDB / Polars = 研究计算引擎，不是真实来源
- Provider / Plugin 架构；LLM、Backtest Engine 均可替换
- Domain 层无基础设施依赖；依赖方向 apps → application → domain ← plugins/infrastructure
- 实验必须可复现（复现元组）；Schema 全部版本化
- LLM 只产出数据（假设 / 规格），永不作裁判
- 全文：`docs/architecture/00-overview.md`

## 3. Current Research Direction

Market State → Feature / Event → Knowledge Retrieval → Hypothesis → Combination → Experiment → Validation → Research Memory → Strategy Evolution

- 新颖性主要来自确定性的组合 / 条件化 / 时序算子，并且每次组合都计入尝试次数
- 路线图：`docs/research/roadmap.md`

## 4. Current Phase

- Current Phase：Phase 0（进行中）
- Current Subphase：B1、B2 已实施并经 Codex 验收；ADR-0010 纠偏已实施；契约仍为未发布的 2.0.0
- Current Objective：等待 Codex 验收 B1/B2，再决定是否授权 B3 与 Phase 0 关闭复审
- Current Blocker：B3 未授权（生命周期审批等遗漏）；Constitution 1.0.0 仍待 Raphael 亲自批准
- Next Milestone：B1/B2 验收 → B3 决定 → Constitution 获批 → Phase 0 关闭

## 5. Active Decisions

- ADR-0001：重大架构决定用 ADR 记录；Agent 只能起草 Proposed
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- D-06：Python 3.13 + uv，与系统 Python 隔离；升级需独立评估实验 → ADR-0003
- D-07：本地 Git 仓库；不改全局配置；远程未定 → ADR-0004
- D-03：研究 / 生产边界（Artifact + Registry + Promotion + Equivalence Gate；生产运行时拒绝加载无法追溯的策略）→ ADR-0005 **Accepted**
- D-05：生命周期 v2 → ADR-0006 **Accepted**，取代 ADR-0002 第 5 条；RETIRED 进退役记录，REJECTED / FAILED 进 Failure Registry
- C-1（已定）：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE；PRODUCTION_CANDIDATE = 已通过研究验证 + Paper Trading，待生产部署审查
- C-2（已定）：PAPER = 单策略独立观察；ACTIVE = 组合 / Router 正式启用，带 execution_mode SIMULATED|LIVE（Phase 13 前仅 SIMULATED）；不设 LIVE 状态
- D-09 结构（H-1、H-2 已接受）：三层 = Constitution（不可变原则）/ Validation Profile（版本化阈值，被实验使用后不可变）/ Experiment Metadata（每个实验的 Profile 版本与配置）；两步冻结 = Phase 0 冻结原则与架构、Phase 4 校准后冻结初始 Profile 参数 → ADR-0007 **Accepted**；Constitution 已重组为 0.2.0-draft（纯原则），Profile 概念契约在 07-validation.md §5，Experiment Metadata 在 06-experiment.md §3
- D-09 数值（TBD-1 ~ TBD-5）：**未批准**，Phase 4 校准后冻结为 Profile 参数；Constitution 中不得出现数值阈值；提案见 `docs/research/proposals/d09-validation-threshold-proposal.md`
- ADR-0008：契约映射载荷只读、逐模型内容哈希边界与 v1 只读兼容；2026-09-23 Accepted，B1 已实施。
- ADR-0009：完整实验规格身份、运行标识与直接依赖内容绑定；实际 seeds 保留在实验哈希中；2026-09-23 Accepted，B2 已实施。
- ADR-0010：`model_copy(update=...)` 重新走完整校验（`model_construct` 明确不受支持）、唯一 ASCII
  SemVer 2.0.0 语法、v1 只读入口的顶层 shape gate、JSON Schema 表达键值格式；2026-09-23 Accepted 并实施。
- 契约 2.0.0 已随 B2 发布：v1 与 v2 的 `content_hash` / `experiment_hash` **不可比较**；v1 只读路径 =
  `schemas/v1/`（35 份快照）+ `tests/vectors/v1/`（固定载荷与旧哈希）+ `core/compat/v1.py`。
  读取 v1 不赋予任何 v2 登记 / 晋升资格。

## 6. Active Constraints

- 硬性规则全文见 `CLAUDE.md` §3（H1–H14），摘要如下：
- 研究代码永不直接成为生产代码
- LLM 不作最终裁决
- 不因回测结果修改 Constitution、Profile 或验证规则
- Outcome 不得作为 Feature / State / Event 的输入
- 所有实验可复现；所有 Schema 版本化；失败实验与生命周期历史不可删除
- 环境变更（安装、系统配置、Docker、数据库、全局 Git 配置）需 Raphael 授权
- 不修改旧项目与外部数据
- Claude 不替 Raphael 做架构决策
- Git：main 为稳定基线，实现工作走 `phase/*` 分支，合并进 main 需 Raphael 批准；无远程仓库（细节见 CLAUDE.md §10）
- Raphael 于 2026-09-23 指定：Codex 决定项目方向、技术栈、架构、功能、逻辑与文档并控制 Claude Code；编码和测试实现交给 WSL Claude Code 的 Opus 模型。Constitution 原则、实盘/风险预算、环境安装、历史数据删除与 main 合并仍由 Raphael 亲自批准。

## 7. Current Known Risks

- pypi.org 的索引域名在本机被网络阻断（files.pythonhosted.org 可达）：依赖安装很慢，离线安装需用 uv.lock 中的精确版本
- WSL 内存约 15 GiB：大数据集需分区和流式处理
- Docker 未安装：Phase 1 之后的本地服务依赖它（D-02）
- 外部数据盘未挂载：`~/BTC` → `/mnt/wsl/PHYSICALDRIVE1p1/BTC` 当前不可访问
- Git 没有全局提交身份：提交使用一次性 `-c` 参数，长期做法待定
- Constitution 仍是草案：批准前不能判定任何实验
- 契约层只校验直接引用的内容绑定；传递依赖闭包、trial 权威账本、`run.repro` ↔ Spec 一致性、
  Registry 存在性均为未实现的 Runner / Registry 义务（06-experiment.md §7）
- `LlmCall` 仍只存哈希，"完整输入输出"仍是未关闭缺口（B3）
- v1 只读 gate 只做顶层形状检查，不是完整 JSON Schema 递归校验
- 外部是否存在 v1 历史数据证据不足：不得宣称迁移路径已在真实数据上验证

## 8. Important Historical Context

- 旧研究项目 `alpha-autoquant` 位于 `/mnt/e/alpha-autoquant`（只读参考，不得修改）
- 现有系统 `hlens-cryptoplus` 位于 `/mnt/e/hlens-cryptoplus`（只读参考，不得修改）
- 旧研究中的 anti-leakage / red-team 规范可能成为新系统素材（Raphael 提及，尚未评估内容）
- 旧研究可能已看过 BTC 全部历史，因此历史封存区在认知上不完全干净（D-09 H-3）
- 旧策略或旧结论进入新系统时必须重新登记并重新验证，不能直接信任

## 9. Last Known Good State

- Date：2026-09-23
- Git Commit：ADR-0010 纠偏提交（phase/0 分支 HEAD）；上一恢复点 `4e0f6e3`（B2）、`4f83e18`（B1）
- Phase：Phase 0；B1/B2 + ADR-0010 已实施，契约 2.0.0 仍未发布，等待 Codex 复验与关闭复审
- State：core 契约、状态机、只读载荷、完整实验身份、规范版本语法、v1 只读兼容入口均已实现；
  545 测试、ruff check、ruff format --check、mypy strict 全绿；
  Schema current 36 份（2.0.0）+ legacy 35 份（`schemas/v1/`，1.0.0）；
  无 Feature / Strategy / Backtest / Runner / Registry / 存储实现
- Notes：ADR-0001 ~ 0010 Accepted；B3 未授权；Constitution 0.2.0-draft 待批准为 1.0.0；
  D-09 数值、H-3 ~ H-7、Q-1 ~ Q-7 仍开放；未合并 main、未创建 tag
