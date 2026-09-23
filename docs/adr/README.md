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

## 待决事项（ARCHITECTURE_DECISION_REQUIRED）

以下冲突或空白在 Architecture Bootstrap 中被发现，**未自行解决**。每一项决定后应形成 ADR。

| ID | 问题 | 选项 | 阻塞 / 状态 |
|---|---|---|---|
| D-01 | **Iceberg Catalog 选择**。Iceberg 必须有 Catalog；若用 PostgreSQL 作 SQL Catalog，则 Control Plane 与 Data Plane 共享数据库，模糊 P3/P8 边界。另外 Python 生态对 Iceberg 写入（PyIceberg）与 DuckDB 的 Iceberg 写支持成熟度不一。 | (a) PG 独立 database 作 SQL Catalog；(b) REST Catalog（如 Lakekeeper / Polaris）；(c) Phase 1 先用"Parquet + 自有快照清单"，Phase 2 再上 Iceberg | Phase 1 |
| D-02 | **没有 Docker 时的对象存储**。S3 兼容存储（MinIO）通常以容器运行，但 Docker 未安装且本阶段禁止安装。 | (a) Phase 0–1 用本地文件系统作为 StorageAdapter；(b) 授权安装 Docker 后使用 MinIO；(c) 使用 MinIO 单二进制（非容器） | Phase 1 |
| D-03 | ✅ **已决定（ADR-0005）** 代码位置歧义。Feature/State/Event 实现放 `research/<kind>/` 还是 `plugins/`？`strategies/`、`risk/` 是研究代码还是生产代码？这直接关系 H5（研究代码不得直接变成生产代码）。 | (a) `research/*` = 探索期插件，`plugins/` = 基础设施/引擎类插件，`strategies/`、`risk/` = 仅晋升后重新实现的代码；(b) 所有 Provider 实现统一放 `plugins/`，`research/` 只放实验编排与 notebook | → ADR-0005 Proposed |
| D-04 | **Validation 时序冲突**。Phase 8 才是 Validation & Robustness，但 Phase 5–7 已产生实验与晋升判断。 | (a) 最小验证门在 Phase 4 交付，Phase 8 做扩展（当前 roadmap 采用此解释）；(b) 把 Phase 8 前移到 Phase 5 之前 | Phase 4 |
| D-05 | ✅ **已决定（ADR-0006）** Lifecycle 细节。VALIDATION 与 OOS 是否为两个状态；DEGRADED 能否回到 ACTIVE；RETIRED 是否进入 Failure Registry；REJECTED 与 FAILED 的区分；ACTIVE 在 Phase 10–12 表示"纸面"还是"实盘"。 | 见 07-validation.md §3 草案 | → ADR-0006 Proposed |
| D-06 | **Python 版本**。系统 Python 为 3.14.4；部分科学计算 / 数据库驱动 / PyIceberg 等对最新版本支持可能滞后。 | (a) 用 uv 固定 3.12 或 3.13 的项目内解释器；(b) 直接使用 3.14 | ✅ 已决定 → ADR-0003 |
| D-07 | **Git 仓库**。项目目录尚未 `git init`（本阶段未授权）；复现元组依赖 commit SHA。另需决定远程托管位置。 | 授权后 `git init`（不修改全局 Git 配置） | ✅ 已决定 → ADR-0004 |
| D-08 | **市场与执行范围**。覆盖哪些交易所 / 标的 / 频率（现货、永续、期权、链上）？Phase 13 是否包含实盘，由谁授权，风险预算上限？ | 需用户定义 | Phase 1 / 13 |
| D-09 | **Constitution 数值**。TBD-1 至 TBD-5（OOS 长度、最小样本、基准、参数稳定性、成本压力）。 | 需用户批准 | → 提案 `docs/research/proposals/d09-validation-threshold-proposal.md`（未批准） |
| D-10 | **NATS 引入时机**。早期单机研究不需要事件总线，过早引入增加运维负担。 | (a) Phase 1–6 用进程内任务队列（接口为 EventBusAdapter），Phase 7/11 引入 NATS；(b) 从 Stage 2 开始即引入 | Phase 1 |

## 冲突记录

| ID | 冲突 | 涉及 | 状态 |
|---|---|---|---|
| C-1 | D-03 边界链为 Paper Trading → Production Candidate；D-05 生命周期为 PRODUCTION_CANDIDATE → PAPER。两份 Raphael 规格顺序相反 | ADR-0005、ADR-0006 | ✅ 2026-09-21 Raphael 选 B：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE |
| C-2 | D-05 规则 5（Phase 13 前 ACTIVE 仅为模拟）使 PAPER 与 ACTIVE 的区别不明确 | ADR-0006 | ✅ 2026-09-21 Raphael 选 A：PAPER = 单策略独立观察；ACTIVE = 组合 / Router 正式启用，带 execution_mode；不设 LIVE 状态 |
