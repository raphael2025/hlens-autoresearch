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
- 当前阶段：Architecture Bootstrap 已完成；Phase 0 尚未开启

## 2. Current Architecture

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

- Current Phase：Phase 0 之前（Architecture Bootstrap 完成）
- Current Subphase：Phase 0 决策处理中
- Current Objective：批准 D-03、D-05，然后决定 D-09
- Current Blocker：ADR-0005、ADR-0006 待批准；D-09 提案待审；Python 3.13 未授权安装
- Next Milestone：Raphael 说"开启 Phase 0"

## 5. Active Decisions

- ADR-0001：重大架构决定用 ADR 记录；Agent 只能起草 Proposed
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- D-06：Python 3.13 + uv，与系统 Python 隔离；升级需独立评估实验 → ADR-0003
- D-07：本地 Git 仓库；不改全局配置；远程未定 → ADR-0004
- D-03：研究 / 生产边界（Artifact + Registry + Equivalence Gate）→ ADR-0005 **Proposed**
- D-05：生命周期 v2 → ADR-0006 **Proposed**
- C-1（已定）：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE；PRODUCTION_CANDIDATE = 已通过研究验证 + Paper Trading，待生产部署审查
- C-2（已定）：PAPER = 单策略独立观察；ACTIVE = 组合 / Router 正式启用，带 execution_mode SIMULATED|LIVE（Phase 13 前仅 SIMULATED）；不设 LIVE 状态
- D-09：阈值提案（Constitution + Validation Profile + Experiment Metadata 三层）→ `docs/research/proposals/d09-validation-threshold-proposal.md`，**未批准**

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

## 7. Current Known Risks

- WSL 内存约 15 GiB：大数据集需分区和流式处理
- Docker 未安装：Phase 1 之后的本地服务依赖它（D-02）
- uv 中尚未安装 Python 3.13（需在 Phase 0 开启时授权安装）
- 外部数据盘未挂载：`~/BTC` → `/mnt/wsl/PHYSICALDRIVE1p1/BTC` 当前不可访问
- Git 没有全局提交身份：提交使用一次性 `-c` 参数，长期做法待定
- Constitution 仍是草案：批准前不能判定任何实验

## 8. Important Historical Context

- 旧研究项目 `alpha-autoquant` 位于 `/mnt/e/alpha-autoquant`（只读参考，不得修改）
- 现有系统 `hlens-cryptoplus` 位于 `/mnt/e/hlens-cryptoplus`（只读参考，不得修改）
- 旧研究中的 anti-leakage / red-team 规范可能成为新系统素材（Raphael 提及，尚未评估内容）
- 旧研究可能已看过 BTC 全部历史，因此历史封存区在认知上不完全干净（D-09 H-3）
- 旧策略或旧结论进入新系统时必须重新登记并重新验证，不能直接信任

## 9. Last Known Good State

- Date：2026-09-21
- Git Commit：0351341（Architecture Bootstrap Baseline）
- Phase：Bootstrap 完成，Phase 0 未开启
- State：仅有文档和元数据，无业务代码，未安装依赖
- Notes：ADR-0001 ~ 0004 Accepted；ADR-0005、0006 Proposed；D-09 提案未批准
