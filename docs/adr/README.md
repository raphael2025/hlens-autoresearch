# Architecture Decision Records

模板：[0000-template.md](0000-template.md)。规则：[ADR-0001](0001-record-architecture-decisions.md)。

## 索引

| ADR | 标题 | 状态 |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | 使用 ADR 记录架构决策 | Accepted |
| [0002](0002-architecture-baseline.md) | 架构基线 | Accepted（第 5 条被 ADR-0006 取代） |
| [0003](0003-python-version-and-uv.md) | Python 3.13 + uv（D-06） | Accepted |
| [0004](0004-git-repository-baseline.md) | 本地 Git 仓库与 Bootstrap Baseline（D-07） | Accepted |
| [0005](0005-research-production-boundary.md) | Research / Production Boundary（D-03） | Accepted（2026-09-23） |
| [0006](0006-strategy-lifecycle.md) | Strategy Lifecycle v2（D-05） | Accepted（2026-09-23）；取代 ADR-0002 第 5 条 |
| [0007](0007-validation-architecture-three-layers.md) | 三层验证架构与两步冻结（D-09 结构部分） | Accepted（2026-09-23） |
| [0008](0008-contract-payload-immutability.md) | 契约载荷的只读表示与内容哈希载荷定义 | Accepted（2026-09-23，Codex 依授权批准方案 A） |
| [0009](0009-experiment-identity-binding.md) | 实验规格身份、运行标识与依赖内容绑定 | Accepted（2026-09-23，Codex 依授权批准方案 A） |
| [0010](0010-contract-construction-and-canonical-versioning.md) | 契约构造路径、规范版本语法、v1 顶层 shape gate 与 Schema 格式表达 | Accepted（2026-09-23，Codex 验收 B1/B2 后裁决） |

## 待决事项（ARCHITECTURE_DECISION_REQUIRED）

以下冲突或空白在 Architecture Bootstrap 中被发现，**未自行解决**。每一项决定后应形成 ADR。

| ID | 问题 | 选项 | 阻塞 / 状态 |
|---|---|---|---|
| D-01 | **Iceberg Catalog 选择**。Iceberg 必须有 Catalog；若用 PostgreSQL 作 SQL Catalog，则 Control Plane 与 Data Plane 共享数据库，模糊 P3/P8 边界。另外 Python 生态对 Iceberg 写入（PyIceberg）与 DuckDB 的 Iceberg 写支持成熟度不一。 | (a) PG 独立 database 作 SQL Catalog；(b) REST Catalog（如 Lakekeeper / Polaris）；(c) Phase 1 先用"Parquet + 自有快照清单"，Phase 2 再上 Iceberg | Phase 1 |
| D-02 | **没有 Docker 时的对象存储**。S3 兼容存储（MinIO）通常以容器运行，但 Docker 未安装且本阶段禁止安装。 | (a) Phase 0–1 用本地文件系统作为 StorageAdapter；(b) 授权安装 Docker 后使用 MinIO；(c) 使用 MinIO 单二进制（非容器） | Phase 1 |
| D-03 | ✅ **已决定（ADR-0005）** 研究 / 生产边界。相关实现选择 Q-1/Q-2/Q-7 仍按 ADR 保持开放。 | 见 ADR-0005 | → ADR-0005 Accepted |
| D-04 | **Validation 时序冲突**。Phase 8 才是 Validation & Robustness，但 Phase 5–7 已产生实验与晋升判断。 | (a) 最小验证门在 Phase 4 交付，Phase 8 做扩展（当前 roadmap 采用此解释）；(b) 把 Phase 8 前移到 Phase 5 之前 | Phase 4 |
| D-05 | ✅ **已决定（ADR-0006）** Lifecycle 细节；Q-4～Q-6 按 ADR 保持开放，现有已接受规则继续适用。 | 见 ADR-0006 与 07-validation.md §3 | → ADR-0006 Accepted |
| D-06 | **Python 版本**。系统 Python 为 3.14.4；部分科学计算 / 数据库驱动 / PyIceberg 等对最新版本支持可能滞后。 | (a) 用 uv 固定 3.12 或 3.13 的项目内解释器；(b) 直接使用 3.14 | ✅ 已决定 → ADR-0003 |
| D-07 | **Git 仓库**。项目目录尚未 `git init`（本阶段未授权）；复现元组依赖 commit SHA。另需决定远程托管位置。 | 授权后 `git init`（不修改全局 Git 配置） | ✅ 已决定 → ADR-0004 |
| D-08 | **市场与执行范围**。覆盖哪些交易所 / 标的 / 频率（现货、永续、期权、链上）？Phase 13 是否包含实盘，由谁授权，风险预算上限？ | 需用户定义 | Phase 1 / 13 |
| D-09 | **Constitution 数值**。TBD-1 至 TBD-5。结构部分（H-1、H-2）已定 → ADR-0007 Accepted；**数值仍未批准**，将在 Phase 4 校准后按 Profile 版本冻结。 | 需用户批准 | → 提案 `docs/research/proposals/d09-validation-threshold-proposal.md`（未批准） |
| D-10 | **NATS 引入时机**。早期单机研究不需要事件总线，过早引入增加运维负担。 | (a) Phase 1–6 用进程内任务队列（接口为 EventBusAdapter），Phase 7/11 引入 NATS；(b) 从 Stage 2 开始即引入 | Phase 1 |
| D-11 | ✅ **已决定（ADR-0008）** 契约只读载荷与哈希边界；下一次未发布的 2.0.0 与旧版本只读兼容。 | 方案 A | Phase 0；Accepted，B1 已实施 |
| D-12 | ✅ **已决定（ADR-0009）** 实验身份与复现绑定；种子、运行标识和直接依赖内容覆盖。 | 方案 A | Phase 0；Accepted，B2 已实施（契约版本号提升到 2.0.0，该版本尚未发布） |
| D-13 | ✅ **已决定（ADR-0010）** `model_copy(update=...)` 必须重新走完整校验；`model_construct` 明确为不受支持的可信数据逃生口。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-14 | ✅ **已决定（ADR-0010）** 唯一、ASCII、完整 SemVer 2.0.0 语法；major 从已验证的正则分组读取。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-15 | ✅ **已决定（ADR-0010）** v1 只读入口增加基于已提交快照的顶层 shape gate，快照缺失 fail closed。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-16 | ✅ **已决定（ADR-0010）** 三类映射字段的键值格式必须出现在导出的 JSON Schema 中，且与运行时同源。 | Codex 裁决 | Phase 0；Accepted，已实施 |

## 冲突记录

| ID | 冲突 | 涉及 | 状态 |
|---|---|---|---|
| C-1 | D-03 边界链为 Paper Trading → Production Candidate；D-05 生命周期为 PRODUCTION_CANDIDATE → PAPER。两份 Raphael 规格顺序相反 | ADR-0005、ADR-0006 | ✅ 2026-09-21 Raphael 选 B：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE |
| C-2 | D-05 规则 5（Phase 13 前 ACTIVE 仅为模拟）使 PAPER 与 ACTIVE 的区别不明确 | ADR-0006 | ✅ 2026-09-21 Raphael 选 A：PAPER = 单策略独立观察；ACTIVE = 组合 / Router 正式启用，带 execution_mode；不设 LIVE 状态 |
